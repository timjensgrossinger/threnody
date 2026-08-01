"""
Contextual bandit routing policy (plan 11).

LinUCB / Thompson sampling over (tier, provider_id) arms.
Shadow-only today: arms are updated from recorded outcomes (currently consensus
persona picks, see host_learning) and readable via ``arm_stats``, but nothing
executes a bandit choice. There is no ``config.routing.bandit_mode`` knob — that
config section does not exist; promoting this to live routing is unimplemented
work, not a setting.
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
    r"\b(refactor|rewrite|architecture|design|migrate|implement|complex|distributed)\b",
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


class BanditPolicy:
    """Manages a pool of LinUCB arm models, one per (tier, provider_id)."""

    def __init__(
        self,
        db: "Database | None" = None,
        alpha: float = 1.0,
        mode: str = "shadow",
    ) -> None:
        self._db = db
        self._alpha = alpha
        self._mode = mode  # shadow | live
        self._arms: dict[str, LinUCBArmModel] = {}

    def _get_or_create_arm(self, arm_id: str) -> LinUCBArmModel:
        if arm_id not in self._arms:
            self._arms[arm_id] = LinUCBArmModel(arm_id=arm_id, alpha=self._alpha)
        return self._arms[arm_id]

    def select(
        self,
        features: list[float],
        available_arms: list[str],
        heuristic_arm: str,
    ) -> BanditDecision:
        """Select best arm by UCB score. Always executes heuristic in shadow mode."""
        if not available_arms:
            return BanditDecision(
                bandit_arm=heuristic_arm,
                bandit_score=0.0,
                heuristic_arm=heuristic_arm,
                chosen_arm=heuristic_arm,
            )
        best_arm = heuristic_arm
        best_score = -float("inf")
        for arm_id in available_arms:
            model = self._get_or_create_arm(arm_id)
            score = model.ucb_score(features)
            if score > best_score:
                best_score = score
                best_arm = arm_id

        chosen = heuristic_arm if self._mode == "shadow" else best_arm
        return BanditDecision(
            bandit_arm=best_arm,
            bandit_score=best_score,
            heuristic_arm=heuristic_arm,
            chosen_arm=chosen,
        )

    def update(self, arm_id: str, features: list[float], reward: float) -> None:
        """Update arm model with observed reward (0..1)."""
        model = self._get_or_create_arm(arm_id)
        model.update(features, reward)

    def arm_stats(self) -> list[dict]:
        return [
            {
                "arm_id": arm_id,
                "n_updates": m.n_updates,
                "alpha": m.alpha,
            }
            for arm_id, m in sorted(self._arms.items())
        ]


# Module-level singleton for MCP process lifetime.
_bandit_policy: BanditPolicy | None = None


def get_bandit_policy(
    db: "Database | None" = None,
    alpha: float = 1.0,
    mode: str = "shadow",
) -> BanditPolicy:
    global _bandit_policy
    if _bandit_policy is None:
        _bandit_policy = BanditPolicy(db=db, alpha=alpha, mode=mode)
    return _bandit_policy
