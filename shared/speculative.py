#!/usr/bin/env python3
"""
Threnody speculative execution engine — Phase 6.

For subtasks whose complexity score falls within SPECULATION_MARGIN of a tier
boundary, this module immediately runs the cheaper (lower-tier) model while
simultaneously queuing the higher-tier call in a background thread.  If the
lower-tier output passes basic quality checks, the higher-tier result is
discarded, saving tokens.

**Critical constraint**: speculation only fires when the lower tier is FREE
(i.e., the resolved model name contains "mini").  When both tiers cost tokens
the higher-tier call is used directly without speculation.

Usage::

    executor = SpeculativeExecutor(provider, config, db=db)
    result = executor.execute_speculative(subtask, score)
    if result is None:
        # not borderline, or speculation not possible — use normal routing
        ...
    else:
        output = result.output

"""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace as dc_replace
from typing import TypeGuard

from .config import (
    SPECULATION_ERROR_PATTERNS,
    SPECULATION_MARGIN,
    SPECULATION_MIN_OUTPUT_CHARS,
    TGsConfig,
    ThresholdConfig,
)
from .db import Database
from .orchestrator import Provider
from .planner import Subtask

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compiled error pattern cache
# ---------------------------------------------------------------------------
_COMPILED_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p) for p in SPECULATION_ERROR_PATTERNS
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class SpeculativeResult:
    """Outcome of a speculative execution attempt."""

    output: str
    """The final output string (from whichever tier was used)."""

    tier_used: str
    """The tier label that produced the accepted output (``"low"``, ``"medium"``, etc.)."""

    model_used: str
    """The model name that produced the accepted output."""

    speculated: bool
    """True when speculation was attempted (lower tier ran concurrently with higher tier)."""

    lower_tier_passed: bool
    """True when the lower-tier output satisfied quality checks and was accepted."""

    token_estimate: int
    """Rough token count for the accepted output (len(output) // 4)."""


# ---------------------------------------------------------------------------
# Public helper functions
# ---------------------------------------------------------------------------

def is_borderline(
    score: float,
    thresholds: ThresholdConfig,
) -> tuple[bool, str, str] | None:
    """Determine whether *score* is within :data:`SPECULATION_MARGIN` of a tier boundary.

    Checks two boundaries:

    * ``thresholds.low_max``   — the low / medium boundary
    * ``thresholds.medium_max`` — the medium / high boundary

    Parameters
    ----------
    score:
        Complexity score in [0, 1].
    thresholds:
        Current tier boundary configuration.

    Returns
    -------
    ``None``
        Score is comfortably within a single tier; no speculation warranted.
    ``(True, lower_tier, higher_tier)``
        Score is within :data:`SPECULATION_MARGIN` of a boundary.  *lower_tier*
        and *higher_tier* are the tier labels on either side, e.g.
        ``(True, "low", "medium")`` or ``(True, "medium", "high")``.
    """
    # low / medium boundary
    low_med_dist = abs(score - thresholds.low_max)
    if low_med_dist <= SPECULATION_MARGIN:
        log.debug(
            "score %.4f is within margin %.4f of low/medium boundary %.4f",
            score,
            SPECULATION_MARGIN,
            thresholds.low_max,
        )
        return (True, "low", "medium")

    # medium / high boundary
    med_high_dist = abs(score - thresholds.medium_max)
    if med_high_dist <= SPECULATION_MARGIN:
        log.debug(
            "score %.4f is within margin %.4f of medium/high boundary %.4f",
            score,
            SPECULATION_MARGIN,
            thresholds.medium_max,
        )
        return (True, "medium", "high")

    return None


def check_output_quality(output: str | None) -> TypeGuard[str]:
    """Return ``True`` only when *output* looks usable.

    This function is a :class:`typing.TypeGuard` — a ``True`` return narrows
    the caller's ``str | None`` binding to ``str``, eliminating the need for
    a separate ``None`` assertion in the happy path.

    Fails when:

    * *output* is ``None`` or an empty string.
    * ``len(output) < SPECULATION_MIN_OUTPUT_CHARS``.
    * Any :data:`SPECULATION_ERROR_PATTERNS` pattern matches inside *output*.

    Parameters
    ----------
    output:
        Raw text returned by the model, or ``None`` on execution failure.
    """
    if not output:
        log.debug("quality check failed: output is None or empty")
        return False

    if len(output) < SPECULATION_MIN_OUTPUT_CHARS:
        log.debug(
            "quality check failed: output length %d < minimum %d",
            len(output),
            SPECULATION_MIN_OUTPUT_CHARS,
        )
        return False

    for pattern in _COMPILED_ERROR_PATTERNS:
        if pattern.search(output):
            log.debug(
                "quality check failed: error pattern %r matched in output",
                pattern.pattern,
            )
            return False

    return True


# ---------------------------------------------------------------------------
# SpeculativeExecutor
# ---------------------------------------------------------------------------

class SpeculativeExecutor:
    """Runs speculative two-tier execution for borderline complexity scores.

    Speculation fires **only** when:

    1. The complexity score is within :data:`SPECULATION_MARGIN` of a tier
       boundary (as determined by :func:`is_borderline`).
    2. The lower-tier model is free (model name contains ``"mini"``).

    If both conditions hold the lower-tier call is made **synchronously** (on
    the calling thread) while the higher-tier call is queued in a single
    background :class:`~concurrent.futures.ThreadPoolExecutor` thread.  The
    lower-tier result is quality-checked; if it passes the higher-tier future
    is simply abandoned (its result discarded).  If the lower tier fails
    quality the method blocks until the higher-tier future resolves and returns
    that result instead.

    Parameters
    ----------
    provider:
        A :class:`~shared.orchestrator.Provider` implementation.
    config:
        Full Threnody config (used for threshold access).
    db:
        Optional :class:`~shared.db.Database` instance.  When provided, each
        speculation attempt is logged to the ``speculation_log`` table.
    """

    def __init__(
        self,
        provider: Provider,
        config: TGsConfig,
        db: Database | None = None,
    ) -> None:
        self._provider = provider
        self._config = config
        self._db = db
        # Single speculation thread — only one concurrent higher-tier call.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speculative")

        if db is not None:
            self._ensure_log_table()

    def close(self) -> None:
        """Shut down the internal thread pool.

        Call this when the executor is no longer needed to release the
        background worker thread promptly rather than at process exit.
        """
        self._pool.shutdown(wait=False)

    def __enter__(self) -> SpeculativeExecutor:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Schema helpers
    # ------------------------------------------------------------------

    def _ensure_log_table(self) -> None:
        """Create ``speculation_log`` table in the attached database if absent."""
        assert self._db is not None  # guarded by caller
        try:
            with self._db.conn() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS speculation_log (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_hash   TEXT,
                        score       REAL,
                        lower_tier  TEXT,
                        higher_tier TEXT,
                        lower_passed INTEGER,
                        ts          REAL
                    )
                    """
                )
            log.debug("speculation_log table ensured")
        except Exception:
            log.exception(
                "speculation_log: failed to create table — DB logging will be unavailable"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_speculate(self, score: float, lower_tier: str, higher_tier: str | None = None) -> bool:
        """Return ``True`` if speculation is permitted for the given tiers.

        By default (``speculation_require_free_lower=True``) the lower-tier model
        must be free (model name contains ``"mini"``).  When
        ``speculation_require_free_lower=False``, any two distinct models qualify.

        Parameters
        ----------
        score:
            Unused here; reserved for future policy extensions.
        lower_tier:
            The cheaper tier label (e.g. ``"low"`` or ``"medium"``).
        higher_tier:
            Optional upper tier label.  When supplied, availability of the
            higher tier is also verified.
        """
        available = self._provider.available_tiers()
        if lower_tier not in available:
            log.debug(
                "can_speculate=False: %r not in provider available_tiers %s",
                lower_tier,
                available,
            )
            return False

        if higher_tier is not None and higher_tier not in available:
            log.debug(
                "can_speculate=False: higher_tier %r not in provider available_tiers %s",
                higher_tier,
                available,
            )
            return False

        lower_model = self._provider.resolve_model(lower_tier)
        is_free = "mini" in lower_model.lower()

        require_free = getattr(self._config, "speculation_require_free_lower", True)
        if not require_free:
            # Cost-guard: if both tiers resolve to the same model, speculation is pointless
            if higher_tier is not None:
                higher_model = self._provider.resolve_model(higher_tier)
                if lower_model == higher_model:
                    log.debug(
                        "can_speculate=False: lower and higher tier resolve to same model %r",
                        lower_model,
                    )
                    return False
            log.debug(
                "can_speculate check: lower_tier=%r lower_model=%r require_free=False → True",
                lower_tier,
                lower_model,
            )
            return True

        # Default: require free lower tier
        if not is_free:
            higher_model_name = (
                self._provider.resolve_model(higher_tier) if higher_tier is not None else "unknown"
            )
            log.debug(
                "can_speculate=False: lower_tier=%r lower_model=%r is not free; "
                "higher_tier=%r higher_model=%r — skipping speculation to avoid double spend",
                lower_tier,
                lower_model,
                higher_tier,
                higher_model_name,
            )
            return False

        log.debug(
            "can_speculate check: lower_tier=%r lower_model=%r is_free=%s",
            lower_tier,
            lower_model,
            is_free,
        )
        return True

    def execute_speculative(
        self,
        subtask: Subtask,
        score: float,
    ) -> SpeculativeResult | None:
        """Attempt speculative execution for *subtask* at complexity *score*.

        Steps
        -----
        1. Call :func:`is_borderline` — returns ``None`` if not borderline.
        2. Call :meth:`can_speculate` — returns ``None`` if can't speculate.
        3. Build a modified subtask copy with ``tier=lower_tier``.
        4. Execute the lower-tier call synchronously on the calling thread.
        5. Submit the higher-tier call to the background thread pool.
        6. Quality-check the lower-tier output.
        7. If it passes → cancel/ignore the higher-tier future and return lower.
        8. If it fails → wait for the higher-tier future and return that.
        9. Log the outcome to the DB when available.

        Returns
        -------
        :class:`SpeculativeResult`
            When speculation was attempted (regardless of which tier won).
        ``None``
            When the score is not borderline, or speculation cannot proceed
            (caller should route normally).
        """
        thresholds = self._config.thresholds

        # --- Step 1: borderline check ---
        borderline = is_borderline(score, thresholds)
        if borderline is None:
            log.debug(
                "subtask %d (score=%.4f): not borderline, skipping speculation",
                subtask.id,
                score,
            )
            return None

        _, lower_tier, higher_tier = borderline

        # --- Step 2: free-tier check ---
        if not self.can_speculate(score, lower_tier, higher_tier):
            log.info(
                "subtask %d (score=%.4f): borderline %s/%s but lower tier is not free "
                "— routing normally to %s",
                subtask.id,
                score,
                lower_tier,
                higher_tier,
                higher_tier,
            )
            return None

        lower_model = self._provider.resolve_model(lower_tier)
        higher_model = self._provider.resolve_model(higher_tier)

        log.info(
            "subtask %d (score=%.4f): speculating %s(%s) vs %s(%s)",
            subtask.id,
            score,
            lower_tier,
            lower_model,
            higher_tier,
            higher_model,
        )

        # --- Step 3: build subtask copies for each tier ---
        lower_subtask = dc_replace(subtask, tier=lower_tier)
        higher_subtask = dc_replace(subtask, tier=higher_tier)

        # --- Step 4 & 5: submit higher tier first so both run in parallel ---
        # The higher-tier call is queued in the background thread NOW so that
        # it starts executing while the lower-tier call runs synchronously on
        # the calling thread.  If the lower tier passes quality we cancel/ignore
        # the future; if it fails we simply wait for the already-in-flight result.
        higher_future: Future[str | None] = self._pool.submit(
            self._provider.execute,
            higher_subtask,
            higher_model,
        )
        lower_output = self._provider.execute(lower_subtask, lower_model)

        # --- Step 6 & 7: quality check lower tier ---
        lower_passed = check_output_quality(lower_output)

        if lower_passed:
            log.info(
                "subtask %d: lower-tier (%s) passed quality check — "
                "higher-tier (%s) result discarded",
                subtask.id,
                lower_tier,
                higher_tier,
            )
            # Cancel if still queued (no-op if already running — acceptable,
            # the result will simply be dropped when the future resolves).
            higher_future.cancel()

            # TypeGuard[str] on check_output_quality means lower_output is str here.
            final_output: str = lower_output
            tier_used = lower_tier
            model_used = lower_model
        else:
            # --- Step 8: fall back to higher tier ---
            log.info(
                "subtask %d: lower-tier (%s) failed quality check — "
                "waiting for higher-tier (%s) result",
                subtask.id,
                lower_tier,
                higher_tier,
            )
            try:
                higher_output = higher_future.result(timeout=120)
            except concurrent.futures.TimeoutError:
                log.warning(
                    "subtask %d: higher-tier (%s) timed out after 120 s — "
                    "falling back to lower-tier output",
                    subtask.id,
                    higher_tier,
                )
                higher_output = None

            # If the higher tier also returned nothing, surface the lower output
            # so the caller at least has something to work with.
            if higher_output is None:
                log.warning(
                    "subtask %d: higher-tier (%s) also returned None — "
                    "returning lower-tier output as fallback",
                    subtask.id,
                    higher_tier,
                )
                final_output = lower_output or ""
                tier_used = lower_tier
                model_used = lower_model
            else:
                final_output = higher_output
                tier_used = higher_tier
                model_used = higher_model

        # --- Step 9: DB logging ---
        task_hash = _hash_subtask(subtask)
        self._log_to_db(
            task_hash=task_hash,
            score=score,
            lower_tier=lower_tier,
            higher_tier=higher_tier,
            lower_passed=lower_passed,
        )

        token_estimate = len(final_output) // 4

        return SpeculativeResult(
            output=final_output,
            tier_used=tier_used,
            model_used=model_used,
            speculated=True,
            lower_tier_passed=lower_passed,
            token_estimate=token_estimate,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_to_db(
        self,
        task_hash: str,
        score: float,
        lower_tier: str,
        higher_tier: str,
        lower_passed: bool,
    ) -> None:
        """Insert one row into ``speculation_log`` (best-effort, never raises)."""
        if self._db is None:
            return
        try:
            with self._db.conn() as conn:
                conn.execute(
                    """
                    INSERT INTO speculation_log
                        (task_hash, score, lower_tier, higher_tier, lower_passed, ts)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (task_hash, score, lower_tier, higher_tier, int(lower_passed), time.time()),
                )
            log.debug(
                "speculation_log: task_hash=%s lower_passed=%s",
                task_hash,
                lower_passed,
            )
        except Exception:
            log.exception("speculation_log: failed to write DB row (non-fatal)")


# ---------------------------------------------------------------------------
# Private utilities
# ---------------------------------------------------------------------------

def _hash_subtask(subtask: Subtask) -> str:
    """Return a short SHA-256 hex digest for *subtask* (for DB keying)."""
    raw = f"{subtask.id}:{subtask.description}:{subtask.tier}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
