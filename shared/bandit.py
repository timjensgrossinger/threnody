"""
Contextual bandit routing policy (plan 11).

LinUCB / Thompson sampling over (tier, provider_id) arms.
Runs in shadow mode by default — logs picks but executes the heuristic choice.
Promote with ``config.routing.bandit_mode = 'live'``.

Live selection is additionally gated on training: an arm with no observations has
``A = I`` and ``b = 0``, so its UCB score is pure exploration bonus and identical
for every tier. Selecting on that would not be "learned routing", it would be
"whichever arm was enumerated first". :meth:`BanditPolicy.select` therefore falls
back to the heuristic until every candidate arm has at least
``routing.bandit_min_updates`` observations.

Arms are persisted in ``bandit_arms`` and trained off the hot path by
:func:`train_from_decisions`, which replays scored ``routing_decisions`` rows.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature extraction (numpy-free — simple float vector)
# ---------------------------------------------------------------------------

_LANG_PATTERNS: list[tuple[str, int]] = [
    (r"\bpython\b|\bpytest\b|\bflask\b|\bdjango\b", 0),
    (r"\bjava(?:script|)\b|\btypescript\b|\bnode\b|\breact\b", 1),
    (r"\brust\b|\bcargo\b", 2),
    (r"\bgo(?:lang)?\b", 3),
    (r"\bc\+\+\b|\bcpp\b", 4),
    (r"\bsql\b|\bquery\b|\bdatabase\b|\bschema\b", 5),
    (r"\bterraform\b|\bkubernetes\b|\bdocker\b|\bci\b|\bcd\b", 6),
]

_URGENCY_WORDS = re.compile(
    r"\b(critical|urgent|hotfix|incident|asap|immediately|production\s+down)\b",
    re.I,
)

_COMPLEXITY_WORDS = re.compile(
    r"\b(refactor|rewrite|architecture|design|migrate|implement|complex|distributed|reasoning|consensus|proof|audit|security|concurrency)\b",
    re.I,
)


def extract_task_features(
    task: str,
    project_id: str = "",
    recent_outcomes: list[float] | None = None,
) -> list[float]:
    """Return a fixed-length float feature vector for the task.

    Dimensions:
        0  — normalized task length (chars / 500, capped at 1.0)
        1  — urgency flag (0/1)
        2  — complexity word density
        3  — multi-file signal (presence of multiple file extensions)
        4  — project-id hash bucket (0..7 normalized)
        5  — recent outcome mean (-1..1), 0.0 if no history
        6-12 — language signal (one-hot, 7 languages)
    """
    task_lower = task.lower()
    length_feat = min(len(task) / 500.0, 1.0)
    urgency_feat = 1.0 if _URGENCY_WORDS.search(task) else 0.0
    complexity_count = len(_COMPLEXITY_WORDS.findall(task))
    complexity_feat = min(complexity_count / 5.0, 1.0)

    ext_pattern = re.compile(r"\.[a-z]{1,5}\b")
    extensions = set(ext_pattern.findall(task_lower))
    multi_file_feat = min(len(extensions) / 3.0, 1.0)

    pid_hash = int(hashlib.sha256(project_id.encode()).hexdigest()[:8], 16) % 8 if project_id else 0
    pid_feat = pid_hash / 7.0

    if recent_outcomes:
        outcome_mean = sum(recent_outcomes[-10:]) / len(recent_outcomes[-10:])
    else:
        outcome_mean = 0.0
    outcome_feat = max(-1.0, min(1.0, outcome_mean))

    lang_feats = [0.0] * len(_LANG_PATTERNS)
    for i, (pattern, _) in enumerate(_LANG_PATTERNS):
        if re.search(pattern, task_lower):
            lang_feats[i] = 1.0

    return [length_feat, urgency_feat, complexity_feat, multi_file_feat,
            pid_feat, outcome_feat] + lang_feats


# ---------------------------------------------------------------------------
# LinUCB arm model (one per (tier, provider_id))
# ---------------------------------------------------------------------------

FEATURE_DIM = 6 + len(_LANG_PATTERNS)  # 13


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _mat_vec(M: list[list[float]], v: list[float]) -> list[float]:
    return [_dot(row, v) for row in M]


def _outer_add(M: list[list[float]], v: list[float]) -> list[list[float]]:
    """M += v * v^T in-place."""
    for i in range(len(v)):
        for j in range(len(v)):
            M[i][j] += v[i] * v[j]
    return M


def _identity(n: int) -> list[list[float]]:
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def _inverse_diagonal(M: list[list[float]]) -> list[list[float]]:
    """Approximate inverse via diagonal for efficiency (avoids numpy)."""
    n = len(M)
    inv = _identity(n)
    for i in range(n):
        diag = M[i][i]
        if abs(diag) > 1e-10:
            inv[i][i] = 1.0 / diag
    return inv


@dataclass
class LinUCBArmModel:
    """Ridge-regression model for one bandit arm."""
    arm_id: str  # f"{tier}:{provider_id}"
    alpha: float = 1.0  # exploration parameter
    # A = X^T X + I  (feature_dim x feature_dim), initialized to identity
    A: list[list[float]] = field(default_factory=lambda: _identity(FEATURE_DIM))
    # b = X^T r  (feature_dim,), reward accumulator
    b: list[float] = field(default_factory=lambda: [0.0] * FEATURE_DIM)
    n_updates: int = 0

    def update(self, features: list[float], reward: float) -> None:
        """Incorporate a new (feature, reward) observation."""
        _outer_add(self.A, features)
        for i, x in enumerate(features):
            self.b[i] += reward * x
        self.n_updates += 1

    def ucb_score(self, features: list[float]) -> float:
        """LinUCB upper confidence bound for given features."""
        A_inv = _inverse_diagonal(self.A)
        theta = _mat_vec(A_inv, self.b)
        mean = _dot(theta, features)
        Ax = _mat_vec(A_inv, features)
        variance = _dot(features, Ax)
        return mean + self.alpha * math.sqrt(max(0.0, variance))


# ---------------------------------------------------------------------------
# BanditPolicy
# ---------------------------------------------------------------------------

@dataclass
class BanditDecision:
    bandit_arm: str
    bandit_score: float
    heuristic_arm: str
    chosen_arm: str  # = heuristic_arm in shadow mode
    # Why chosen_arm is what it is: "shadow", "untrained", "no_arms" or "bandit".
    # Without this an operator cannot tell a live bandit that agreed with the
    # heuristic from one that never got to choose.
    reason: str = "shadow"


class BanditPolicy:
    """Manages a pool of LinUCB arm models, one per (tier, provider_id)."""

    def __init__(
        self,
        db: "Database | None" = None,
        alpha: float = 1.0,
        mode: str = "shadow",
        min_updates: int = 50,
    ) -> None:
        self._db = db
        self._alpha = alpha
        self._mode = mode  # shadow | live
        self._min_updates = max(0, int(min_updates))
        self._arms: dict[str, LinUCBArmModel] = {}
        self._loaded = False

    @property
    def mode(self) -> str:
        return self._mode

    def _load_persisted(self) -> None:
        """Hydrate arm models from the DB once per policy instance."""
        if self._loaded:
            return
        self._loaded = True
        if self._db is None:
            return
        try:
            for arm_id, state in self._db.load_bandit_arms().items():
                a_matrix = state.get("A")
                b_vector = state.get("b")
                if (
                    not isinstance(a_matrix, list)
                    or len(a_matrix) != FEATURE_DIM
                    or not isinstance(b_vector, list)
                    or len(b_vector) != FEATURE_DIM
                ):
                    # Feature layout changed since this row was written; a stale
                    # shape would silently score against the wrong dimensions.
                    log.debug("bandit arm %s has stale feature shape; ignoring", arm_id)
                    continue
                self._arms[arm_id] = LinUCBArmModel(
                    arm_id=arm_id,
                    alpha=self._alpha,
                    A=a_matrix,
                    b=b_vector,
                    n_updates=int(state.get("n_updates") or 0),
                )
        except Exception:  # pragma: no cover - persistence is best-effort
            log.debug("bandit arm hydration failed", exc_info=True)

    def _get_or_create_arm(self, arm_id: str) -> LinUCBArmModel:
        self._load_persisted()
        if arm_id not in self._arms:
            self._arms[arm_id] = LinUCBArmModel(arm_id=arm_id, alpha=self._alpha)
        return self._arms[arm_id]

    def select(
        self,
        features: list[float],
        available_arms: list[str],
        heuristic_arm: str,
    ) -> BanditDecision:
        """Select the best arm by UCB score.

        Executes the heuristic in shadow mode, and also in live mode while any
        candidate arm is still under ``min_updates`` — an untrained arm scores on
        its exploration bonus alone, so acting on it would be arbitrary rather
        than learned.
        """
        if not available_arms:
            return BanditDecision(
                bandit_arm=heuristic_arm,
                bandit_score=0.0,
                heuristic_arm=heuristic_arm,
                chosen_arm=heuristic_arm,
                reason="no_arms",
            )
        best_arm = heuristic_arm
        best_score = -float("inf")
        trained = True
        for arm_id in available_arms:
            model = self._get_or_create_arm(arm_id)
            if model.n_updates < self._min_updates:
                trained = False
            score = model.ucb_score(features)
            if score > best_score:
                best_score = score
                best_arm = arm_id

        if self._mode != "live":
            reason = "shadow"
        elif not trained:
            reason = "untrained"
        else:
            reason = "bandit"
        chosen = best_arm if reason == "bandit" else heuristic_arm
        return BanditDecision(
            bandit_arm=best_arm,
            bandit_score=best_score,
            heuristic_arm=heuristic_arm,
            chosen_arm=chosen,
            reason=reason,
        )

    def update(self, arm_id: str, features: list[float], reward: float) -> None:
        """Update arm model with observed reward (0..1) and persist it."""
        if len(features) != FEATURE_DIM:
            log.debug(
                "bandit update for %s ignored: %d features, expected %d",
                arm_id, len(features), FEATURE_DIM,
            )
            return
        model = self._get_or_create_arm(arm_id)
        model.update(features, reward)
        if self._db is not None:
            self._db.save_bandit_arm(arm_id, model.A, model.b, model.n_updates)

    def arm_stats(self) -> list[dict]:
        self._load_persisted()
        return [
            {
                "arm_id": arm_id,
                "n_updates": m.n_updates,
                "alpha": m.alpha,
                "trained": m.n_updates >= self._min_updates,
            }
            for arm_id, m in sorted(self._arms.items())
        ]


def train_from_decisions(db: "Database", limit: int = 500) -> dict[str, int]:
    """Replay scored ``routing_decisions`` rows into the arm models.

    This is the training step the routing arms never had: ``_log_bandit_decision``
    scores arms named ``{tier}:heuristic`` while the only historical ``update()``
    call wrote ``{tier}:persona:{winner}`` — a disjoint namespace — so the routing
    arms stayed at their identity prior forever.

    Cold path only (swarm finalize / operator CLI). Each row is replayed exactly
    once via the ``bandit_train_state`` high-water mark. Best-effort: returns
    counts and never raises into a caller.
    """
    result = {"trained": 0, "last_row_id": 0}
    try:
        cursor = db.get_bandit_train_cursor()
        rows = db.get_scored_routing_decisions(after_row_id=cursor, limit=limit)
        if not rows:
            result["last_row_id"] = cursor
            return result
        policy = get_bandit_policy(db=db)
        highest = cursor
        for row in rows:
            policy.update(row["chosen"], row["features"], row["outcome_score"])
            highest = max(highest, int(row["id"]))
            result["trained"] += 1
        db.set_bandit_train_cursor(highest)
        result["last_row_id"] = highest
    except Exception:  # pragma: no cover - training is best-effort
        log.debug("bandit training failed", exc_info=True)
    return result


# Module-level singleton for MCP process lifetime.
_bandit_policy: BanditPolicy | None = None


def get_bandit_policy(
    db: "Database | None" = None,
    alpha: float = 1.0,
    mode: str | None = None,
    min_updates: int | None = None,
) -> BanditPolicy:
    """Return the process-wide policy, reconfiguring it if the caller asks.

    The old version froze ``db`` and ``mode`` at whichever call happened to be
    first, so a later live-mode caller silently got a shadow policy (or one with
    no DB, hence no persistence).

    ``mode`` and ``min_updates`` are ``None`` by default and only applied when
    passed — a caller that just needs the policy (training, ``arm_stats``) must
    not reconfigure it. Defaulting ``mode`` to ``"shadow"`` here would mean
    ``train_from_decisions`` silently demoted a live policy every time it ran.
    """
    global _bandit_policy
    if _bandit_policy is None:
        _bandit_policy = BanditPolicy(
            db=db,
            alpha=alpha,
            mode=mode or "shadow",
            min_updates=50 if min_updates is None else min_updates,
        )
        return _bandit_policy
    if db is not None and _bandit_policy._db is None:
        _bandit_policy._db = db
        _bandit_policy._loaded = False  # hydrate from the DB we just gained
    if mode is not None:
        _bandit_policy._mode = mode
    if min_updates is not None:
        _bandit_policy._min_updates = max(0, int(min_updates))
    return _bandit_policy


def reset_bandit_policy() -> None:
    """Drop the singleton. For tests and for reconfiguration after a config reload."""
    global _bandit_policy
    _bandit_policy = None
