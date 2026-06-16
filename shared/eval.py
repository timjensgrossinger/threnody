"""
shared.eval — Rework detection, eval prompts, background evaluation.

Phase 2: Quality feedback loop.

Three responsibilities:
1. Scope-aware rework filter — classify file overlap between waves.
2. Minimal eval prompt builder — diff-based, under 500 tokens.
3. Background evaluator — daemon ThreadPoolExecutor warm-path eval using lowest free model.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import (
    ThreadPoolExecutor,
    Future as ConcurrentFuture,
    as_completed,
)
import difflib
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, ClassVar

from .config import TGsConfig
from .db import Database
from .outcomes import compute_learning_outcome_snapshot

log = logging.getLogger(__name__)
_BACKGROUND_LOOP: asyncio.AbstractEventLoop | None = None

# Module-level daemon executor pool for warm-path evaluation (Wave 3: FNDX-03).
# Sized lazily from config.parallelism.warm_path_workers on first spawn.
_WARM_PATH_EXECUTOR: ThreadPoolExecutor | None = None
_WARM_PATH_EXECUTOR_WORKERS: int | None = None
_WARM_PATH_WORKER_CAP = 8
_WARM_PATH_WORKER_DEFAULT = 2


def _warm_path_worker_count(config: TGsConfig | None) -> int:
    """Return warm-path worker count from config (default 2, capped at 8)."""
    if config is None:
        return _WARM_PATH_WORKER_DEFAULT
    try:
        workers = int(config.parallelism.warm_path_workers)
    except (TypeError, ValueError):
        return _WARM_PATH_WORKER_DEFAULT
    return max(1, min(_WARM_PATH_WORKER_CAP, workers))


def _get_warm_path_executor(workers: int) -> ThreadPoolExecutor:
    """Lazy-init or resize the module-level warm-path spawn executor."""
    global _WARM_PATH_EXECUTOR, _WARM_PATH_EXECUTOR_WORKERS
    if _WARM_PATH_EXECUTOR is None or _WARM_PATH_EXECUTOR_WORKERS != workers:
        if _WARM_PATH_EXECUTOR is not None:
            _WARM_PATH_EXECUTOR.shutdown(wait=False)
        _WARM_PATH_EXECUTOR = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="warm-path-",
        )
        _WARM_PATH_EXECUTOR_WORKERS = workers
    return _WARM_PATH_EXECUTOR


# ---------------------------------------------------------------------------
# Learning Queue Batch Processor (Phase 24)
# ---------------------------------------------------------------------------


def process_learning_queue(db: Database) -> dict[str, int]:
    """
    Process pending learning queue items and apply to adaptive thresholds.
    
    Intended to run as a warm-path background task via ThreadPoolExecutor.
    Processes up to 100 items per cycle.
    
    Returns: Dict with keys processed_count, skipped_count, error_count.
    """
    from .adaptive import update_band
    
    processed = 0
    skipped = 0
    error_count = 0
    now = time.time()
    
    try:
        with db.conn() as conn:
            # Fetch pending items (batch size 100)
            pending = conn.execute(
                """
                SELECT id, task_id, tier, complexity_score, success
                FROM learning_queue
                WHERE status = 'pending'
                LIMIT 100
                """,
            ).fetchall()
            
            if not pending:
                log.debug("No pending learning queue items to process")
                return {"processed_count": 0, "skipped_count": 0, "error_count": 0}
            
            log.debug("Processing %d learning queue items", len(pending))
            
            for item_id, task_id, tier, complexity_score, success_flag in pending:
                try:
                    # Convert 0/1 to boolean if needed
                    success = bool(success_flag)
                    
                    # Apply EMA update to adaptive_thresholds
                    update_band(
                        db,
                        score=complexity_score,
                        tier=tier,
                        success=success,
                        version="shared",
                    )
                    
                    # Mark as processed
                    conn.execute(
                        """
                        UPDATE learning_queue
                        SET status = 'processed', processed_at = ?
                        WHERE id = ?
                        """,
                        (now, item_id),
                    )
                    processed += 1
                    
                except Exception as e:
                    log.error(
                        "Failed to process learning queue item id=%d (task=%s): %s",
                        item_id,
                        task_id,
                        e,
                    )
                    error_count += 1
            
            conn.commit()
    
    except Exception as e:
        log.error("Learning queue batch processor failed: %s", e)
        return {"processed_count": processed, "skipped_count": skipped, "error_count": error_count + 1}
    
    log.info(
        "Learning queue batch: processed=%d errors=%d",
        processed,
        error_count,
    )
    return {"processed_count": processed, "skipped_count": skipped, "error_count": error_count}


def run_warm_path_background_tasks(db: Database) -> dict[str, str | int | dict]:
    """
    Run background learning and outcome processing as warm-path tasks.
    
    Executes:
    1. process_learning_queue() — batch process pending learning signals
    2. compute_learning_outcome_snapshot() — aggregate recent outcomes to memory
    
    Intended to run periodically (30-60s) via warm-path executor.
    Non-blocking: errors are logged but don't propagate.
    
    Returns: Dict with results from both processing stages.
    """
    results = {}
    
    try:
        learning_result = process_learning_queue(db)
        results["learning"] = learning_result
        log.debug("Learning queue processing completed: %s", learning_result)
    except Exception as e:
        log.error("Learning queue processing failed: %s", e, exc_info=True)
        results["learning"] = {"error": str(e)}
    
    try:
        compute_learning_outcome_snapshot(db)
        log.debug("Outcome snapshot computation completed")
        results["snapshot"] = "computed"
    except Exception as e:
        log.error("Outcome snapshot computation failed: %s", e, exc_info=True)
        results["snapshot"] = {"error": str(e)}

    # Safety net: import any run-log that has a recorded terminal outcome but
    # was never imported (e.g. the MCP process crashed between the terminal
    # report and the backgrounded import). Idempotent — already-imported runs
    # are skipped. Lazy imports avoid an eval <-> host_learning/run_log cycle.
    try:
        from . import run_log
        from .host_learning import import_run_log

        imported = 0
        for rid in run_log.iter_pending_runs():
            meta = run_log.read_run_meta(rid)
            outcome = meta.get("outcome")
            if not outcome:
                continue  # run not terminal yet — leave for its terminal call
            try:
                import_run_log(db, rid, outcome=str(outcome))
                imported += 1
            except Exception:
                log.debug("warm-path run-log import failed for %s", rid, exc_info=True)
        results["run_log_import"] = imported
    except Exception as e:
        log.debug("run-log import scan failed: %s", e, exc_info=True)
        results["run_log_import"] = {"error": str(e)}

    return results


# Regex for Python function/class definitions (covers async def too)
_DEF_PATTERN = re.compile(
    r"^((?:async\s+)?def|class)\s+(\w+)", re.MULTILINE,
)


# ---------------------------------------------------------------------------
# 1. Scope-aware rework filter
# ---------------------------------------------------------------------------

class OverlapType(Enum):
    """Classification of file overlap between consecutive waves."""
    SAME_SCOPE_REWRITE = "same_scope_rewrite"
    EXTENSION = "extension"
    NONE = "none"


def _extract_symbol_names(source: str) -> set[str]:
    """Extract top-level function/class names from source."""
    return {m.group(2) for m in _DEF_PATTERN.finditer(source)}


def classify_file_overlap(
    wave_n_files: set[str],
    wave_n1_files: set[str],
    content_before: dict[str, str],
    content_after: dict[str, str],
) -> dict[str, OverlapType]:
    """Classify overlap between two consecutive waves' file sets.

    Returns a mapping from file path to OverlapType:
    - SAME_SCOPE_REWRITE if overlapping files share modified function/class names.
    - EXTENSION if files overlap but no shared symbols changed.
    - NONE for files not in the intersection.
    """
    intersection = wave_n_files & wave_n1_files
    result: dict[str, OverlapType] = {}

    for fp in wave_n1_files:
        if fp not in intersection:
            result[fp] = OverlapType.NONE
            continue

        before_src = content_before.get(fp, "")
        after_src = content_after.get(fp, "")

        before_symbols = _extract_symbol_names(before_src)
        after_symbols = _extract_symbol_names(after_src)
        shared = before_symbols & after_symbols

        if not shared:
            result[fp] = OverlapType.EXTENSION
            continue

        # Check if any shared symbol's body actually changed
        scope_changed = False
        for sym in shared:
            before_lines = _extract_symbol_body(before_src, sym)
            after_lines = _extract_symbol_body(after_src, sym)
            # Strip trailing blanks for comparison
            while before_lines and before_lines[-1] == "":
                before_lines.pop()
            while after_lines and after_lines[-1] == "":
                after_lines.pop()
            if before_lines != after_lines:
                scope_changed = True
                break

        result[fp] = (
            OverlapType.SAME_SCOPE_REWRITE if scope_changed
            else OverlapType.EXTENSION
        )

    return result


def _extract_symbol_body(source: str, symbol_name: str) -> list[str]:
    """Extract the body lines of a function/class by name (simple indent heuristic).

    Handles multi-line signatures by skipping until the colon-terminated
    definition line is found before applying indent-based body extraction.
    """
    lines = source.splitlines()
    capturing = False
    in_signature = False
    body: list[str] = []
    base_indent: int | None = None

    for line in lines:
        if not capturing:
            stripped = line.lstrip()
            if re.match(
                rf"(?:async\s+)?(?:def|class)\s+{re.escape(symbol_name)}\b",
                stripped,
            ):
                capturing = True
                base_indent = len(line) - len(stripped)
                body.append(stripped)
                # Check if signature continues on next line (no colon yet)
                if not stripped.rstrip().endswith(":"):
                    in_signature = True
            continue

        # Still in multi-line signature — skip until we see the closing colon
        if in_signature:
            body.append(line.strip())
            if line.rstrip().endswith(":"):
                in_signature = False
            continue

        # We're capturing body — stop at next line with same or less indent
        # (that isn't blank)
        if line.strip() == "":
            body.append("")
            continue
        current_indent = len(line) - len(line.lstrip())
        if base_indent is None or current_indent <= base_indent:
            break
        body.append(line.strip())

    return body


# ---------------------------------------------------------------------------
# 2. Minimal eval prompt builder
# ---------------------------------------------------------------------------

def build_eval_prompt(
    file_path: str,
    content_before: str,
    content_after: str,
    max_tokens: int = 500,
) -> str:
    """Build a minimal evaluation prompt from a unified diff.

    The prompt is capped at approximately *max_tokens* words to keep it
    within the budget for the lowest-tier model (gpt-5-mini).
    """
    diff_lines = list(difflib.unified_diff(
        content_before.splitlines(keepends=True),
        content_after.splitlines(keepends=True),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=3,
    ))
    diff_text = "".join(diff_lines)

    # Approximate token count — truncate by lines to preserve diff structure
    lines = diff_text.splitlines(keepends=True)
    total_words = 0
    truncated_lines: list[str] = []
    for line in lines:
        line_words = len(line.split())
        if total_words + line_words > max_tokens:
            break
        truncated_lines.append(line)
        total_words += line_words

    if len(truncated_lines) < len(lines):
        diff_text = "".join(truncated_lines) + "\n... [truncated]"

    return (
        f"Rate this code change on a scale of 0.0 (bad rewrite) to 1.0 (good).\n"
        f"Reply with ONLY a JSON object: {{\"score\": <float>, \"reason\": \"<brief>\"}}\n\n"
        f"EVAL DIFF for {file_path}:\n{diff_text}"
    )


# ---------------------------------------------------------------------------
# 3. Wave file tracker & rework detection
# ---------------------------------------------------------------------------

@dataclass
class WaveFileTracker:
    """Tracks which files each wave touches and stores content snapshots.

    Snapshot content is retained only for the most recent ``_RETAIN_WAVES``
    waves. Rework detection only ever compares a wave to its immediate
    predecessor, and the terminal warm-path eval only inspects the final wave
    pair — so retaining every wave's full file content for the whole run was
    pure RAM overhead. Under large fan-out (30-50+ agents across many waves)
    that cumulative retention was the dominant local memory cost; pruning
    bounds resident content to ~2 waves.
    """
    wave_files: dict[int, set[str]] = field(default_factory=dict)
    snapshots_before: dict[str, str] = field(default_factory=dict)
    snapshots_after: dict[str, str] = field(default_factory=dict)

    # Keep current + previous wave: detect_rework(N) needs N-1's content, and
    # the terminal warm-path eval needs the final wave pair. Nothing older is
    # ever read back.
    _RETAIN_WAVES: ClassVar[int] = 2

    def record_wave(
        self,
        wave_index: int,
        files: set[str],
        content_before: dict[str, str] | None = None,
        content_after: dict[str, str] | None = None,
    ) -> None:
        """Record files touched by a wave with optional content snapshots."""
        self.wave_files[wave_index] = files
        if content_before:
            self.snapshots_before.update(content_before)
        if content_after:
            self.snapshots_after.update(content_after)
        self._prune_snapshots()

    def _prune_snapshots(self) -> None:
        """Drop snapshot content (and bookkeeping) for waves older than the
        retention window so resident memory stays bounded across a long run."""
        if len(self.wave_files) <= self._RETAIN_WAVES:
            return
        recent = sorted(self.wave_files)[-self._RETAIN_WAVES:]
        keep: set[str] = set()
        for w in recent:
            keep |= self.wave_files.get(w, set())
        for store in (self.snapshots_before, self.snapshots_after):
            for stale_key in [k for k in store if k not in keep]:
                del store[stale_key]
        recent_set = set(recent)
        for old_wave in [w for w in self.wave_files if w not in recent_set]:
            del self.wave_files[old_wave]

    def detect_rework(
        self,
        wave_index: int,
        db: Database | None = None,
        session_id: str = "",
    ) -> list[dict]:
        """Detect rework between wave_index and wave_index-1.

        Returns a list of rework event dicts for any overlapping files.
        """
        if wave_index < 1:
            return []

        prev_files = self.wave_files.get(wave_index - 1, set())
        curr_files = self.wave_files.get(wave_index, set())
        intersection = prev_files & curr_files

        if not intersection:
            return []

        # Build content dicts for overlapping files
        before = {f: self.snapshots_before.get(f, "") for f in intersection}
        after = {f: self.snapshots_after.get(f, "") for f in intersection}

        overlap = classify_file_overlap(prev_files, curr_files, before, after)
        events: list[dict] = []

        for fp, otype in overlap.items():
            if otype == OverlapType.NONE:
                continue

            scope_match = otype == OverlapType.SAME_SCOPE_REWRITE
            event = {
                "wave_n": wave_index - 1,
                "wave_n1": wave_index,
                "file_path": fp,
                "scope_match": scope_match,
                "overlap_type": otype.value,
            }
            events.append(event)

            if db:
                db.log_rework(
                    session_id=session_id,
                    wave_n=wave_index - 1,
                    wave_n1=wave_index,
                    file_path=fp,
                    scope_match=scope_match,
                )
            log.info(
                "Rework detected: wave %d→%d on %s (%s)",
                wave_index - 1, wave_index, fp, otype.value,
            )

        return events


def set_background_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Register a long-lived event loop for sync warm-path scheduling."""
    global _BACKGROUND_LOOP
    _BACKGROUND_LOOP = loop


def get_background_loop() -> asyncio.AbstractEventLoop | None:
    """Return the registered background loop, if any."""
    return _BACKGROUND_LOOP


# ---------------------------------------------------------------------------
# 4. Background evaluator (warm path)
# ---------------------------------------------------------------------------

@dataclass
class EvalPromptData:
    """Data for a minimal eval prompt."""
    file_path: str
    content_before: str
    content_after: str
    wave_n: int
    wave_n1: int


@dataclass
class EvalResult:
    """Result from a background eval agent."""
    file_path: str
    score: float
    reason: str
    model: str


class BackgroundEvaluator:
    """Spawns non-blocking background eval agents using the lowest free model.

    Uses daemon ThreadPoolExecutor (Wave 3: FNDX-03) for non-blocking warm-path eval.
    Results are written to SQLite telemetry — never blocks the foreground routing path.
    """

    def __init__(
        self,
        db: Database | None = None,
        config: TGsConfig | None = None,
        cli_call: Callable[[str, str, int], str | None] | None = None,
    ) -> None:
        self._db = db
        self._config = config
        # cli_call(prompt, model, timeout) → output
        self._cli_call = cli_call
        # Warm-path failure tracking for temporary disable (D-08)
        self._warm_path_failures = 0
        self._warm_path_disabled_until: float | None = None

    def _log_warm_path_result(
        self,
        future: ConcurrentFuture,
    ) -> None:
        """Log warm-path completion or failure, and track failures for temporary disable."""
        try:
            results = future.result()
            log.debug("Warm path task finished: %d eval(s)", len(results))
            # Reset failure counter on success
            self._warm_path_failures = 0
        except Exception as e:
            log.warning("Warm path task failed: %s", e, exc_info=True)
            # Increment failure counter and check for temporary disable
            self._warm_path_failures += 1
            if self._warm_path_failures > 3:
                disable_until = time.time() + 300  # 5 min disable window
                self._warm_path_disabled_until = disable_until
                log.warning(
                    "Warm path disabled for 5 minutes after %d repeated failures; "
                    "check logs for details",
                    self._warm_path_failures,
                )


    def build_prompts(
        self,
        tracker: WaveFileTracker,
        rework_events: list[dict],
    ) -> list[EvalPromptData]:
        """Build eval prompts for all rework events."""
        prompts: list[EvalPromptData] = []
        for evt in rework_events:
            fp = evt["file_path"]
            before = tracker.snapshots_before.get(fp, "")
            after = tracker.snapshots_after.get(fp, "")
            if not before and not after:
                continue
            prompts.append(EvalPromptData(
                file_path=fp,
                content_before=before,
                content_after=after,
                wave_n=evt["wave_n"],
                wave_n1=evt["wave_n1"],
            ))
        return prompts

    def _eval_one(self, prompt_data: EvalPromptData, model: str) -> EvalResult:
        """Evaluate one diff synchronously via CLI backend."""
        prompt_text = build_eval_prompt(
            prompt_data.file_path,
            prompt_data.content_before,
            prompt_data.content_after,
        )
        score = 0.5
        reason = "no eval backend"

        if self._cli_call:
            try:
                raw = self._cli_call(prompt_text, model, 30)
                if raw:
                    import json
                    parsed = None
                    # Try line-by-line first (single-line JSON)
                    for line in raw.splitlines():
                        line = line.strip()
                        if line.startswith("{"):
                            try:
                                parsed = json.loads(line)
                                break
                            except (json.JSONDecodeError, ValueError):
                                continue
                    # Fallback: extract first {...} block across lines
                    if parsed is None:
                        match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
                        if match:
                            try:
                                parsed = json.loads(match.group())
                            except (json.JSONDecodeError, ValueError):
                                pass
                    if parsed:
                        score = float(parsed.get("score", 0.5))
                        reason = str(parsed.get("reason", ""))
            except Exception:
                log.warning("Eval CLI call failed for %s", prompt_data.file_path, exc_info=True)

        result = EvalResult(
            file_path=prompt_data.file_path,
            score=max(0.0, min(1.0, score)),
            reason=reason,
            model=model,
        )

        # Persist to telemetry
        if self._db:
            self._db.log_agent_result(
                session_id="eval",
                task_hash=f"eval:{prompt_data.file_path}",
                agent_id=0,
                tier="low",
                model=model,
                success=True,
                rework=score < 0.5,
                tokens_used=len(prompt_text) // 4,
                version="eval",
            )

        return result

    def _run_warm_path_sync(
        self,
        tracker: WaveFileTracker,
        rework_events: list[dict],
        model: str = "gpt-5-mini",
    ) -> list[EvalResult]:
        """Run background eval synchronously in executor thread.

        Uses thread-local database connections (Wave 1: threading.local()).
        Iterates over rework_events and evaluates each one.
        Never raises; logs errors at WARNING level.
        """
        prompts = self.build_prompts(tracker, rework_events)
        if not prompts:
            return []

        results: list[EvalResult] = []
        workers = _warm_path_worker_count(self._config)

        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="warm-path-eval-",
        ) as eval_pool:
            futures = {
                eval_pool.submit(self._eval_one, pd, model): pd
                for pd in prompts
            }
            for future in as_completed(futures):
                pd = futures[future]
                try:
                    results.append(future.result())
                except Exception:
                    log.warning(
                        "Background eval failed for %s",
                        pd.file_path,
                        exc_info=True,
                    )

        log.info("Warm path complete: %d evals processed",
                 len(results))
        return results

    def spawn_warm_path(
        self,
        tracker: WaveFileTracker,
        rework_events: list[dict],
        model: str = "gpt-5-mini",
    ) -> ConcurrentFuture | None:
        """Schedule warm path as a non-blocking background task via daemon ThreadPoolExecutor.

        Returns the Future object (for optional polling) or None
        if no rework events to process or warm-path is temporarily disabled.

        Non-blocking: returns immediately after submitting to executor.
        Never blocks the foreground routing path.
        """
        if not rework_events:
            return None

        # Check if warm-path is temporarily disabled due to repeated failures
        if self._warm_path_disabled_until is not None:
            if time.time() < self._warm_path_disabled_until:
                log.debug("Warm path is disabled; skipping warm-path scheduling")
                return None
            else:
                # Disable window expired; reset counter
                self._warm_path_disabled_until = None
                self._warm_path_failures = 0

        workers = _warm_path_worker_count(self._config)
        executor = _get_warm_path_executor(workers)
        future = executor.submit(
            self._run_warm_path_sync,
            tracker,
            rework_events,
            model,
        )

        # Add callback to log completion/failure and handle repeated failures
        future.add_done_callback(self._log_warm_path_result)

        log.info("Warm path scheduled: %d eval(s)", len(rework_events))
        return future

    async def run_warm_path(
        self,
        tracker: WaveFileTracker,
        rework_events: list[dict],
        model: str = "gpt-5-mini",
    ) -> list[EvalResult]:
        """DEPRECATED: async warm-path runner.

        Kept for backward compatibility if code still calls this.
        New code should use spawn_warm_path() directly.
        """
        log.warning("run_warm_path() is deprecated; use spawn_warm_path() instead")
        return self._run_warm_path_sync(tracker, rework_events, model)



# ---------------------------------------------------------------------------
# 5. Cold path — threshold adjustment scaffolding
# ---------------------------------------------------------------------------

def cold_path_adjust(
    db: Database,
    config: TGsConfig,
    every_n_tasks: int = 10,
) -> bool:
    """Check if cold-path threshold adjustment should run.

    Queries telemetry for recent task count. If count is a multiple of
    *every_n_tasks*, adjusts thresholds based on accumulated rework data.

    Returns True if adjustment was applied.
    """
    if every_n_tasks < 1:
        return False

    window_start = time.time() - 86400
    with db.conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE ts > ?",
            (window_start,),
        ).fetchone()
    task_count = row[0] if row else 0

    if task_count == 0 or task_count % every_n_tasks != 0:
        return False

    # Count rework events in last 24h
    with db.conn() as conn:
        rework_row = conn.execute(
            "SELECT COUNT(*), SUM(scope_match) FROM rework_events WHERE ts > ?",
            (window_start,),
        ).fetchone()
    rework_count = rework_row[0] if rework_row else 0
    scope_count = rework_row[1] if rework_row and rework_row[1] is not None else 0

    rework_rate = rework_count / task_count

    # If rework rate is high, thresholds need tightening (promote more tasks)
    if rework_rate > 0.30:
        config.thresholds.low_max = max(
            config.thresholds.low_max - 0.02,
            0.50,  # hard floor
        )
        config.thresholds.clamp()
        log.info(
            "Cold path: high rework rate (%.2f), lowered low_max to %.2f",
            rework_rate, config.thresholds.low_max,
        )
    elif rework_rate < 0.10:
        config.thresholds.low_max = min(
            config.thresholds.low_max + 0.01,
            0.75,  # hard ceiling
        )
        config.thresholds.clamp()
        log.info(
            "Cold path: low rework rate (%.2f), raised low_max to %.2f",
            rework_rate, config.thresholds.low_max,
        )

    log.info(
        "Cold path stats: %d tasks, %d rework events (%d scope-level), rate=%.2f",
        task_count, rework_count, scope_count, rework_rate,
    )
    return True
