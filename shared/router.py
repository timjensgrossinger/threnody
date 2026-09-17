#!/usr/bin/env python3
"""
Threnody complexity classifier with intent modifier.

Fast keyword-based classification — no LLM call, instant response.
Returns tier labels (low/medium/high), not model names.
The provider layer in each version resolves tiers to models.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config import (
    TGsConfig,
    DEFAULT_RISK_FILENAME_PATTERNS,
    SPEED_SIGNALS,
    QUALITY_SIGNALS,
    REASONING_SIGNALS,
    LOW_TIER_FLOOR,
    LOW_TIER_CEILING,
    MEDIUM_HIGH_BOUNDARY_FLOOR,
    MEDIUM_HIGH_BOUNDARY_CEILING,
    SYSTEMS_LANGUAGE_SIGNALS,
    SYSTEMS_LANGUAGE_SCORE_BONUS,
    WORD_BOUNDARY_COMPLEXITY_SIGNALS,
)
from .db import Database
from .risk_signals import (
    PROSE_RISK_TOKENS,
    TaskRiskEvidence,
    compile_risk_floor_re,
)

if TYPE_CHECKING:  # bandit is imported lazily at call sites to keep import cost off
    from .bandit import BanditDecision  # noqa: F401

log = logging.getLogger(__name__)

ACTIVATION_MIN_SAMPLES = 5


@dataclass
class RoutingDecision:
    """Result of classifying a task."""
    tier: str            # low | medium | high
    score: float
    reason: str
    agents: int
    override: bool
    intent_modifier: float = 0.0
    # Phase 14 additions: explainable urgency surface
    urgency_score: float = 0.0
    matched_urgency_signals: list[str] = field(default_factory=list)
    # Expected duration of the work: short | medium | long. Derived from signals
    # already computed (complexity score + how many files the task names), NOT from
    # a new keyword vocabulary. It deliberately does NOT influence tier or model
    # choice — it only sets reasoning effort / token budget and gates whether the
    # hybrid diagnose->implement hop is worth its extra latency.
    expected_duration_bucket: str = "medium"
    expected_file_count: int = 0
    reasoning_effort: str = "medium"
    thinking_budget: int = 2048


# Score nudge applied when a low-tier override keyword dominates a single-concern
# task. Additive (folded into the effective score), never a hard tier set — so
# downstream intent/reasoning/security-floor logic still runs.
_LOW_OVERRIDE_DELTA: float = -0.20

# Ordinal rank for tier-floor comparisons.
_TIER_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

DURATION_SHORT = "short"
DURATION_MEDIUM = "medium"
DURATION_LONG = "long"
VALID_DURATION_BUCKETS = (DURATION_SHORT, DURATION_MEDIUM, DURATION_LONG)

# File-count boundaries for the duration axis. Structural, not vocabulary: a task
# naming several files takes longer than one naming none, regardless of wording.
_DURATION_LONG_FILES = 4

# Work-file extensions for the duration proxy. An explicit allowlist rather than a
# generic `.<letters>` pattern on purpose: the loose form matches prose like "e.g."
# and attribute access like "foo.bar", which would silently inflate the file count
# and push short work into the `long` bucket. Mirrors heuristic_plan._FILE_EXT_GROUP
# minus the documentation formats, which are not work targets here.
_WORK_EXT_GROUP = (
    r"py|pyi|ts|tsx|js|jsx|mjs|cjs|html|htm|css|scss|vue|svelte|go|rs|java|kt|rb|cs"
    r"|lua|c|h|cpp|hpp|cc|sh|bash|zsh|swift|ex|exs|tf|sql|proto|yaml|yml|json|toml"
    r"|ini|cfg"
)
_FILE_TOKEN = re.compile(
    rf"(?<![\w/.])((?:[\w.-]+/)*[\w.-]+\.(?:{_WORK_EXT_GROUP}))\b",
    re.IGNORECASE,
)


def count_task_files(task: str) -> int:
    """Count distinct work-file tokens named in a task string.

    A cheap structural proxy for scope, not a path resolver: documentation formats
    are excluded so "update the README" does not read as multi-file work, and only
    recognised source/config extensions count so prose cannot inflate the number.
    """
    if not isinstance(task, str) or not task:
        return 0
    return len({m.group(1).strip().lower() for m in _FILE_TOKEN.finditer(task)})


def duration_bucket_for(
    score: float,
    *,
    file_count: int,
    thresholds: "ThresholdConfig",
) -> str:
    """Classify expected duration from the already-computed score and file count.

    ``long`` when the work is genuinely complex or spans several files; ``short``
    only when a low score AND at most one named file agree that it is a quick edit.
    Everything ambiguous is ``medium`` — the neutral bucket that changes nothing.
    """
    if file_count >= _DURATION_LONG_FILES or score > thresholds.medium_max:
        return DURATION_LONG
    if score <= thresholds.low_max and file_count <= 1:
        return DURATION_SHORT
    return DURATION_MEDIUM


def reasoning_params_for(duration_bucket: str, tier: str = "medium") -> tuple[str, int]:
    """Derive reasoning_effort and thinking_budget token allocation."""
    if duration_bucket == DURATION_SHORT or tier == "low":
        return "low", 0
    if duration_bucket == DURATION_LONG or tier == "high":
        return "high", 8192
    return "medium", 2048


#: Alias kept for call sites and tests. The implementation moved to
#: shared/risk_signals.py alongside the vocabulary it compiles, so the router's
#: task-text floor and the file-evidence collector share one matcher.
_compile_risk_floor_re = compile_risk_floor_re

# Max scoring hits per complexity level. The score should say what kind of work
# this is, not how many synonyms the caller happened to use — see _compute_score.
#
# Two is deliberate for every level. Raising the high cap to 3 was measured on
# the eval corpus: it lifts the domain fixtures (shader/firmware/tensor) from
# 0.62 to 0.74, but lifts the deliberately-borderline medium ones by the same
# amount, so the medium|high gap went from 0.05 to 0.04 rather than widening.
# Both settings classify the corpus identically, so the tighter cap wins — it
# keeps the anti-density property strongest.
_LEVEL_HIT_CAP: dict[str, int] = {"high": 2, "medium": 2, "low": 1}

# Risk-evidence weights, applied once per task from the resolved target files.
# A target mentions risk vocabulary. Weak evidence — it fired on 62% of this
# repo's shared modules, topped by the module that defines the vocabulary — so it
# only ever nudges the score; the floor needs a filename match or a real defect.
# Measured tier split for a neutral task naming one shared/*.py file:
#   0.12 -> low=26 med=48   0.06 -> low=34 med=40   0.00 -> low=58 med=16
# (the residual 16 at 0.00 are the >600 LOC files, i.e. pure size signal).
_EVIDENCE_RISK_SCORE = 0.12
_EVIDENCE_SMELL_SCORE = 0.18   # a target holds a high-severity security defect
_EVIDENCE_LOC_SCORE = 0.12     # largest target is big (halved for mid-sized)
_EVIDENCE_LOC_MID = 230        # mirrors review_fanout.tier_for banding
_EVIDENCE_LOC_LARGE = 600


class TaskRouter:
    """Classify tasks using keyword overrides, intent modifiers, and complexity scoring.

    When a Database instance is provided, uses adaptive thresholds
    computed from accumulated success/failure EMA data (Phase 3).
    """

    def __init__(self, config: TGsConfig, db: Database | None = None) -> None:
        self._config = config
        self._db = db
        self._overrides = config.overrides
        self._signals = config.signals
        self._weights = config.signal_weights
        self._base_score = config.base_score
        self._thresholds = config.thresholds
        # The task-text floor matches the *prose* subset, not the filename set.
        # "billing.py" names a payment surface worth a floor; "find files related
        # to billing logic" is a read-only question that carries none, and
        # flooring it spent a tier for nothing. An operator who overrides
        # risk_filename_patterns has opted in explicitly, so their list is used
        # verbatim for both jobs.
        configured = list(config.risk_filename_patterns or [])
        if configured == list(DEFAULT_RISK_FILENAME_PATTERNS):
            self._risk_floor_re = compile_risk_floor_re(PROSE_RISK_TOKENS)
        else:
            self._risk_floor_re = compile_risk_floor_re(configured)

        if self._db:
            try:
                with self._db.conn() as conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS time_routing (
                            hour INTEGER PRIMARY KEY,
                            bias REAL DEFAULT 0.0,
                            sample_count INTEGER DEFAULT 0,
                            ts REAL NOT NULL DEFAULT 0
                        )
                    """)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Intent modifier — scan for speed/quality keywords
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_intent_modifier(task_lower: str) -> tuple[float, list[str]]:
        """Compute intent modifier from speed/quality keywords.

        Uses word-boundary matching to avoid substring false positives
        (e.g., "rough" inside "production").
        """
        modifier = 0.0
        matched: list[str] = []

        for keyword, weight in SPEED_SIGNALS.items():
            if re.search(r'\b' + re.escape(keyword) + r'\b', task_lower):
                modifier += weight
                matched.append(f"speed:{keyword}({weight:+.2f})")

        for keyword, weight in QUALITY_SIGNALS.items():
            if re.search(r'\b' + re.escape(keyword) + r'\b', task_lower):
                modifier += weight
                matched.append(f"quality:{keyword}({weight:+.2f})")

        return modifier, matched

    # ------------------------------------------------------------------
    # Urgency modifier
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_urgency_modifier(task_lower: str) -> tuple[float, list[str]]:
        """Detect urgency signals conservatively and return (urgency_delta, matched_signals).

        We use small, additive weights per matched signal and clamp the final urgency to [0.0,1.0].
        Quality signals (e.g. 'review' from QUALITY_SIGNALS) dampen urgency by 50% when present.
        """
        URGENCY_SIGNALS: dict[str, float] = {
            "asap": 0.20,
            "by eod": 0.15,
            "today": 0.12,
            "soon": 0.08,
            "blocked": 0.15,
            "blocked by": 0.15,
            "can't proceed": 0.15,
            "cant proceed": 0.15,
            "parallelize": 0.10,
            "fan-out": 0.12,
            "fan out": 0.12,
            "parallel": 0.08,
            "production": 0.12,
            "incident": 0.15,
            "outage": 0.15,
        }
        urgency = 0.0
        matched: list[str] = []
        for keyword, weight in URGENCY_SIGNALS.items():
            if re.search(r'\b' + re.escape(keyword) + r'\b', task_lower):
                urgency += weight
                matched.append(f"{keyword}({weight:+.2f})")

        # Quality dampening per D-04: reduce urgency if quality-focused words present
        quality_found = False
        for q in QUALITY_SIGNALS.keys():
            if re.search(r'\b' + re.escape(q) + r'\b', task_lower):
                quality_found = True
                break
        if quality_found and urgency > 0.0:
            # conservative penalty: 50% reduction
            urgency *= 0.5
            matched = [m + "|quality_dampened" for m in matched]

        # Clamp
        urgency = max(0.0, min(1.0, urgency))
        return round(urgency, 2), matched

    # ------------------------------------------------------------------
    # Override check
    # ------------------------------------------------------------------

    def _check_high_overrides(self, task_lower: str) -> RoutingDecision | None:
        """Check high-tier overrides — always win, using word-boundary matching."""
        for kw in self._overrides.get("high", []):
            if re.search(rf"\b{re.escape(kw)}\b", task_lower):
                file_count = count_task_files(task_lower)
                dur_bucket = (
                    DURATION_LONG if file_count >= _DURATION_LONG_FILES else DURATION_MEDIUM
                )
                r_effort, t_budget = reasoning_params_for(dur_bucket, "high")
                return RoutingDecision(
                    tier="high",
                    score=0.90,
                    reason=f"keyword override → high: '{kw}'",
                    agents=1,
                    override=True,
                    # A hard high override is complex by definition, so it is never
                    # 'short' — the duration axis must stay populated on this early
                    # return or downstream gating would silently see the default.
                    expected_duration_bucket=dur_bucket,
                    expected_file_count=file_count,
                    reasoning_effort=r_effort,
                    thinking_budget=t_budget,
                )
        return None

    def _matched_high_signal(self, task_lower: str) -> bool:
        """True when a genuine high-tier complexity keyword matched.

        Distinct from "the raw score is high" (several medium/low signals can
        stack to the same number) — this asks specifically whether a *high*-level
        keyword fired, mirroring ``_compute_score``'s own matching for that one
        level so a caller can suppress a low-tier nudge on the actual signal that
        made this task risky, not an aggregate score that could arrive several
        other ways.
        """
        for kw in self._signals.get("high", []):
            if kw in task_lower:
                return True
        for kw in WORD_BOUNDARY_COMPLEXITY_SIGNALS.get("high", []):
            if re.search(r'\b' + re.escape(kw) + r'\b', task_lower):
                return True
        return False

    def _low_override_delta(
        self, task_lower: str, computed_score: float
    ) -> tuple[float, list[str]]:
        """Low-tier override as a score nudge, not a hard tier set.

        Returns a negative score delta (and its explainability labels) only when a
        low keyword genuinely *dominates* a single-concern task. The nudge is
        suppressed — returns ``(0.0, [])`` — when the keyword merely co-occurs with
        high-signal content, so a security-sensitive multi-file task is no longer
        dragged to low just because it also mentions e.g. "docstring":

        - a security/risk token co-occurs (``_risk_floor_re``),
        - the task references 3+ files (multi-concern; mirrors ``_compute_score``),
        - a genuine high-tier complexity keyword matched (``_matched_high_signal``)
          — e.g. "rename this concurrency-sensitive permission check" must not
          route low just because "rename" is also a low-tier keyword; the risk
          floor above only catches *filename* patterns, not concept-level signals
          like "concurrency" or "permission" in the task text itself,
        - the raw complexity score already sits at/above the low boundary.
        """
        if computed_score >= self._thresholds.low_max:
            return 0.0, []
        if self._risk_floor_re is not None and self._risk_floor_re.search(task_lower):
            return 0.0, []
        if len(re.findall(r'\b\w+\.\w{1,4}\b', task_lower)) >= 3:
            return 0.0, []
        if self._matched_high_signal(task_lower):
            return 0.0, []
        for kw in self._overrides.get("low", []):
            if re.search(rf"\b{re.escape(kw)}\b", task_lower):
                return _LOW_OVERRIDE_DELTA, [f"low_override:'{kw}'({_LOW_OVERRIDE_DELTA:+.2f})"]
        return 0.0, []

    # ------------------------------------------------------------------
    # Complexity scoring
    # ------------------------------------------------------------------

    def _compute_score(self, task_lower: str) -> tuple[float, list[str]]:
        """Compute raw complexity score from the task text alone.

        Deliberately free of file evidence. The evidence term is applied in
        ``classify`` *after* the low-tier override, because the two describe
        different things: the override asks "is this task trivially phrased?"
        and the evidence asks "is the target dangerous?". Folding evidence in
        here let a risky file push the raw score past the low boundary, which
        made ``_low_override_delta`` bail out as "already complex" and switched
        off the trivial-task cap — so "add a docstring" to a file with a SQL
        interpolation routed to the most expensive tier.

        Per-level hit counts are capped (see :data:`_LEVEL_HIT_CAP`). Uncapped,
        the score measured *vocabulary density* rather than difficulty: four
        medium keywords stacked to 0.48 while a security rewrite using none
        scored 0.10, so no single threshold could separate them. Capping makes
        the score reflect what kind of work this is, and the risk-evidence term
        below is what expresses how dangerous the target actually is.
        """
        score = self._base_score
        matched: list[str] = []

        # Level hit counts are shared between the substring and word-boundary
        # passes so a level cannot exceed its cap by splitting across the two.
        hits: dict[str, int] = {"high": 0, "medium": 0, "low": 0}

        def _credit(level: str, kw: str) -> None:
            nonlocal score
            cap = _LEVEL_HIT_CAP.get(level, 0)
            if hits.get(level, 0) >= cap:
                matched.append(f"{kw}(capped)")
                return
            weight = self._weights.get(level, 0.0)
            hits[level] = hits.get(level, 0) + 1
            score += weight
            matched.append(f"{kw}(+{weight})")

        for level in ("high", "medium", "low"):
            for kw in self._signals.get(level, []):
                if kw in task_lower:
                    _credit(level, kw)

        # Word-boundary signals: short tokens that are substrings of common words
        # (gui/tui/ffi/rails/...). Matched whole-token to avoid false positives.
        for level, keywords in WORD_BOUNDARY_COMPLEXITY_SIGNALS.items():
            for kw in keywords:
                if re.search(r'\b' + re.escape(kw) + r'\b', task_lower):
                    _credit(level, kw)

        word_count = len(task_lower.split())
        if word_count > 30:
            score += 0.10
            matched.append("long_prompt(+0.10)")
        elif word_count > 15:
            score += 0.05
            matched.append("medium_prompt(+0.05)")

        file_refs = len(re.findall(r'\b\w+\.\w{1,4}\b', task_lower))
        if file_refs >= 3:
            score += 0.10
            matched.append("multi_file(+0.10)")

        # Systems language detection — word-boundary matching avoids "trust"→"rust" etc.
        for lang in SYSTEMS_LANGUAGE_SIGNALS:
            if re.search(r'\b' + re.escape(lang) + r'\b', task_lower):
                score += SYSTEMS_LANGUAGE_SCORE_BONUS
                matched.append(f"systems_lang:{lang}(+{SYSTEMS_LANGUAGE_SCORE_BONUS})")
                break  # apply bonus once regardless of how many langs are named

        # Systems language file reference bonus (.rs / .go / .c / .cpp variants)
        _sys_file_refs = len(re.findall(r'\b\w+\.(?:rs|go|cpp|cc|cxx|c)\b', task_lower))
        if _sys_file_refs >= 1:
            score += 0.15
            matched.append(f"sys_lang_files:{_sys_file_refs}(+0.15)")

        return min(score, 1.0), matched

    @staticmethod
    def _evidence_score(
        evidence: "TaskRiskEvidence | None",
        matched: list[str],
    ) -> float:
        """Score the target files' risk and size. Zero without evidence.

        LOC bands mirror ``review_fanout.tier_for`` (<230 / 230-600 / >600) so a
        file lands in the same size class here as it would as a review cell.
        Security smells are counted once for the set rather than per hit: a file
        with 18 interpolations is not nine times more dangerous to edit than one
        with 2, and scaling linearly would let a single large file saturate the
        score on its own.
        """
        if evidence is None or not evidence.files:
            return 0.0
        delta = 0.0
        if evidence.any_risk:
            delta += _EVIDENCE_RISK_SCORE
            matched.append(f"risk_files:{evidence.risk_files}(+{_EVIDENCE_RISK_SCORE})")
        if evidence.any_security_smell:
            delta += _EVIDENCE_SMELL_SCORE
            matched.append(
                f"security_smells:{evidence.security_smells}(+{_EVIDENCE_SMELL_SCORE})"
            )
        loc = evidence.max_loc
        if loc > _EVIDENCE_LOC_LARGE:
            delta += _EVIDENCE_LOC_SCORE
            matched.append(f"large_file:{loc}(+{_EVIDENCE_LOC_SCORE})")
        elif loc > _EVIDENCE_LOC_MID:
            delta += _EVIDENCE_LOC_SCORE / 2
            matched.append(f"mid_file:{loc}(+{_EVIDENCE_LOC_SCORE / 2})")
        return delta

    # ------------------------------------------------------------------
    # Tier resolution with hard bounds
    # ------------------------------------------------------------------

    def _get_thresholds(
        self,
        *,
        score: float | None = None,
        project_path: str | None = None,
    ) -> 'ThresholdConfig':
        """Get current thresholds — adaptive only when the local project gate is satisfied."""
        if self._db:
            try:
                from .adaptive import (
                    compute_thresholds,
                    get_band_sample_count,
                    get_project_sample_count,
                    should_apply_adaptive_thresholds,
                )

                if project_path and self.is_learning_enabled(project_path) and score is not None:
                    band_sample_count = get_band_sample_count(self._db, score)
                    project_sample_count = get_project_sample_count(self._db, project_path)
                    if should_apply_adaptive_thresholds(
                        project_path,
                        band_sample_count=band_sample_count,
                        project_sample_count=project_sample_count,
                        band_min_samples=ACTIVATION_MIN_SAMPLES,
                    ):
                        return compute_thresholds(self._db, min_samples=ACTIVATION_MIN_SAMPLES)
            except Exception:
                log.debug("Adaptive thresholds unavailable, using static", exc_info=True)
        return self._thresholds

    def _tier_from_score(self, score: float, project_path: str | None = None) -> str:
        """Map effective score to tier, respecting hard bounds."""
        thresholds = self._get_thresholds(score=score, project_path=project_path)
        if score <= thresholds.low_max:
            return "low"
        if score <= thresholds.medium_max:
            return "medium"
        return "high"

    @staticmethod
    def _compute_reasoning_score(
        task_lower: str,
        enabled: bool = True,
    ) -> tuple[float, list[str]]:
        """Compute a reasoning/creativity score independent of complexity."""
        if not enabled:
            return 0.0, []
        score = 0.0
        matched: list[str] = []
        for keyword, weight in REASONING_SIGNALS.items():
            if re.search(r'\b' + re.escape(keyword) + r'\b', task_lower):
                score += weight
                matched.append(f"reasoning:{keyword}({weight:+.2f})")
        return min(score, 1.0), matched

    def report_outcome(
        self,
        score: float,
        tier: str,
        success: bool,
        version: str = "shared",
        project_id: str | None = None,
        token_cost: int = 0,
        rework_count: int = 0,
    ) -> None:
        """Report a routing outcome for adaptive threshold learning.

        Call this after an agent completes to feed the EMA system.
        """
        if not self._db:
            return
        try:
            from .adaptive import register_observation, update_band

            if project_id and self.is_learning_enabled(project_id):
                register_observation(
                    self._db,
                    project_id,
                    {
                        "rework_count": rework_count,
                        "token_cost": token_cost,
                        "success": success,
                        "timestamp": time.time(),
                    },
                )
                update_band(self._db, score, tier, success, version)
            elif not project_id:
                update_band(self._db, score, tier, success, version)
        except Exception:
            log.debug("Failed to update adaptive band", exc_info=True)

    # ------------------------------------------------------------------
    # Project routing profile
    # ------------------------------------------------------------------

    def is_learning_enabled(self, project_id: str) -> bool:
        """Return whether project-local learning is enabled for this project.

        ``project_routing.learning_enabled`` is tri-state: 1 = explicitly opted
        in, 0 = explicitly opted out, NULL/absent = no operator choice. An
        explicit value always wins, so someone who turned learning off stays
        off; everything else falls back to ``project_learning_default``. Without
        that fallback a never-configured project could never accumulate routing
        feedback — which was every project.
        """
        if not self._db or not project_id:
            return False
        try:
            with self._db.conn() as conn:
                row = conn.execute(
                    "SELECT learning_enabled FROM project_routing WHERE project_path = ?",
                    (project_id,),
                ).fetchone()
            if row is not None and row[0] is not None:
                return bool(row[0])
            return bool(getattr(self._config, "project_learning_default", True))
        except Exception:
            log.debug("Failed to read learning flag for %s", project_id, exc_info=True)
            return False

    def enable_learning(self, project_id: str) -> None:
        """Enable project-local learning for one project path."""
        if not self._db or not project_id:
            return
        try:
            with self._db.conn() as conn:
                row = conn.execute(
                    "SELECT overrides_json, learning_enabled FROM project_routing WHERE project_path = ?",
                    (project_id,),
                ).fetchone()
            overrides_json = row[0] if row and row[0] else json.dumps(
                {"tier_bias": 0.0, "sample_count": 0, "learning_sample_count": 0}
            )
            with self._db.conn() as conn:
                conn.execute(
                    """
                    INSERT INTO project_routing (project_path, overrides_json, learning_enabled, ts)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(project_path) DO UPDATE SET
                        overrides_json = excluded.overrides_json,
                        learning_enabled = 1,
                        ts = excluded.ts
                    """,
                    (project_id, overrides_json, time.time()),
                )
        except Exception:
            log.debug("Failed to enable learning for %s", project_id, exc_info=True)

    def _get_project_modifier(self, project_path: str | None) -> float:
        """Return a learned tier-bias for the given project, or 0.0."""
        if not self._db or not project_path:
            return 0.0
        if not self.is_learning_enabled(project_path):
            return 0.0
        try:
            with self._db.conn() as conn:
                row = conn.execute(
                    "SELECT overrides_json FROM project_routing WHERE project_path = ?",
                    (project_path,),
                ).fetchone()
            if not row:
                return 0.0
            data = json.loads(row[0])
            bias = float(data.get("tier_bias", 0.0))
            return max(-0.15, min(0.15, bias))
        except Exception:
            log.debug("Failed to read project_routing for %s", project_path, exc_info=True)
            return 0.0

    def learn_project_routing(
        self,
        project_path: str,
        assigned_tier: str,
        was_correct: bool,
    ) -> None:
        """Update the per-project tier bias from a routing outcome."""
        if not self._db or not project_path:
            return
        try:
            with self._db.conn() as conn:
                row = conn.execute(
                    "SELECT overrides_json, learning_enabled FROM project_routing WHERE project_path = ?",
                    (project_path,),
                ).fetchone()

            if row:
                data = json.loads(row[0])
                learning_enabled = int(row[1] or 0)
            else:
                data = {"tier_bias": 0.0, "sample_count": 0, "learning_sample_count": 0}
                learning_enabled = 0

            bias: float = float(data.get("tier_bias", 0.0))
            count: int = int(data.get("sample_count", 0))

            if was_correct:
                # EMA nudge toward 0 — no change needed
                alpha = 0.05
                bias = bias * (1.0 - alpha)
            elif assigned_tier == "low":
                # Should have been higher
                bias += 0.03
            elif assigned_tier == "high":
                # Should have been lower
                bias -= 0.03
            else:
                # Medium — direction is ambiguous, decay toward zero
                alpha = 0.05
                bias = bias * (1.0 - alpha)

            bias = max(-0.15, min(0.15, bias))
            count += 1
            data["tier_bias"] = bias
            data["sample_count"] = count
            data.setdefault("learning_sample_count", 0)

            with self._db.conn() as conn:
                conn.execute(
                    """
                    INSERT INTO project_routing (project_path, overrides_json, learning_enabled, ts)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project_path) DO UPDATE SET
                        overrides_json = excluded.overrides_json,
                        learning_enabled = excluded.learning_enabled,
                        ts = excluded.ts
                    """,
                    (project_path, json.dumps(data), learning_enabled, time.time()),
                )
            log.debug(
                "learn_project_routing: %s bias=%.3f count=%d correct=%s",
                project_path,
                bias,
                count,
                was_correct,
            )
        except Exception:
            log.debug("Failed to update project_routing for %s", project_path, exc_info=True)

    # ------------------------------------------------------------------
    # Time-based routing modifier
    # ------------------------------------------------------------------

    def _get_time_modifier(self) -> float:
        """Return a learned bias for the current local hour, or 0.0."""
        if not self._db:
            return 0.0
        hour = time.localtime().tm_hour
        try:
            with self._db.conn() as conn:
                row = conn.execute(
                    "SELECT bias FROM time_routing WHERE hour = ?",
                    (hour,),
                ).fetchone()
            if not row:
                return 0.0
            bias = float(row[0])
            return max(-0.10, min(0.10, bias))
        except Exception:
            log.debug("Failed to read time_routing for hour %d", hour, exc_info=True)
            return 0.0

    def learn_time_pattern(self, hour: int, was_quality_focused: bool) -> None:
        """Update the per-hour bias from a routing outcome."""
        if not self._db:
            return
        if not (0 <= hour <= 23):
            log.warning("learn_time_pattern: ignoring invalid hour %d", hour)
            return
        try:
            with self._db.conn() as conn:
                row = conn.execute(
                    "SELECT bias, sample_count FROM time_routing WHERE hour = ?",
                    (hour,),
                ).fetchone()

            if row:
                bias = float(row[0])
                count = int(row[1])
            else:
                bias = 0.0
                count = 0

            if was_quality_focused:
                bias += 0.02
            else:
                bias -= 0.02

            bias = max(-0.10, min(0.10, bias))
            count += 1

            with self._db.conn() as conn:
                conn.execute(
                    """
                    INSERT INTO time_routing (hour, bias, sample_count, ts)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(hour) DO UPDATE SET
                        bias = excluded.bias,
                        sample_count = excluded.sample_count,
                        ts = excluded.ts
                    """,
                    (hour, bias, count, time.time()),
                )
            log.debug(
                "learn_time_pattern: hour=%d bias=%.3f count=%d quality=%s",
                hour,
                bias,
                count,
                was_quality_focused,
            )
        except Exception:
            log.debug("Failed to update time_routing for hour %d", hour, exc_info=True)

    # ------------------------------------------------------------------
    # Risk floor
    # ------------------------------------------------------------------

    def _resolve_risk_floor(
        self,
        task_lower: str,
        evidence: "TaskRiskEvidence | None",
        *,
        trivial_task: bool = False,
    ) -> tuple[str | None, str]:
        """Resolve the minimum tier this task may run at, and why.

        Three independent pieces of evidence, strongest wins. All three are
        *floors* — none can pull a score-derived high tier back down:

        ==========================================  ====================
        Evidence                                    Floor
        ==========================================  ====================
        risk vocabulary in the task text            ``risk_floor_tier``
        risk vocabulary in a target file's content  ``risk_floor_tier``
        high-severity security smell in a target    ``risk_floor_high_tier``
        ==========================================  ====================

        The high step deliberately keys on ``code_intel``'s security-dimension
        smells rather than on the ``CONCRETE_HIGH_RISK_SIGNALS`` vocabulary.
        That regex matches any ``cursor.execute``, which appears throughout this
        repo's own database layer — flooring every task that touches it to the
        most expensive tier. A detected interpolation is evidence of a defect;
        the mere presence of the API is not.

        ``trivial_task`` caps the file-evidence step at ``risk_floor_tier``. A
        defect in a target file describes the *file*, not the edit: "add a
        docstring to db.py" would otherwise inherit that file's 18 SQL
        interpolation smells and route a comment change to the most expensive
        tier. It is set only when a low-tier override keyword genuinely fired,
        and that path already suppresses itself when the task text itself
        mentions risk, names 3+ files, or matches a high complexity signal.
        Task-text risk is unaffected and still floors at full strength.

        Returns ``(None, "")`` when the floor is disabled or nothing matched.
        """
        if not self._config.risk_floor_enabled:
            return None, ""
        generic = str(self._config.risk_floor_tier or "medium")
        high = str(getattr(self._config, "risk_floor_high_tier", "high") or "high")

        floor: str | None = None
        why: list[str] = []
        if self._risk_floor_re is not None and self._risk_floor_re.search(task_lower):
            floor = generic
            why.append("task_text")
        if evidence is not None and evidence.files:
            # A *filename* match is strong evidence — the file was named for what
            # it holds. A *content* match is not: measured across this repo's 82
            # shared modules it fired on 62% of them, topped by the module that
            # defines the vocabulary and by a static analyser, while a file with
            # two real defects scored a single hit. Raw hit count cannot separate
            # "handles secrets" from "mentions secrets", so content vocabulary
            # nudges the score (see _evidence_score) and never sets a floor.
            if evidence.any_basename_risk:
                if floor is None or _TIER_RANK.get(generic, 1) > _TIER_RANK.get(floor, 0):
                    floor = generic
                why.append("file_name")
            if evidence.any_security_smell:
                step = generic if trivial_task else high
                if floor is None or _TIER_RANK.get(step, 2) > _TIER_RANK.get(floor, 0):
                    floor = step
                why.append(
                    f"security_smells={evidence.security_smells}"
                    + ("(capped:trivial)" if trivial_task and step != high else "")
                )
        return floor, "+".join(why)

    # ------------------------------------------------------------------
    # Main classification
    # ------------------------------------------------------------------

    def classify(
        self,
        task: str,
        project_path: str | None = None,
        evidence: "TaskRiskEvidence | None" = None,
    ) -> RoutingDecision:
        """Classify a task into a tier with intent, project, and time awareness.

        ``evidence`` carries risk facts about the files the task will touch (see
        :func:`shared.risk_signals.collect_task_evidence`). It is optional and
        defaults to None so every existing caller — planner, heuristic_plan,
        tests — behaves exactly as before. Without it the router can only judge
        the caller's prose, which is how a prompt-injection hardening task came
        to route at the cheapest tier.
        """
        task_lower = task.lower().strip()

        # 1. High-tier overrides first (hard — always win)
        high_override = self._check_high_overrides(task_lower)
        if high_override:
            return high_override

        # 2. Compute raw complexity score
        raw_score, complexity_matched = self._compute_score(task_lower)

        # 2b. Low-tier override — now a score nudge (never a hard tier set), so a
        # dominating low keyword lowers the score but downstream floors still apply.
        low_delta, low_matched = self._low_override_delta(task_lower, raw_score)

        # 3. Compute intent modifier
        intent_mod, intent_matched = self._compute_intent_modifier(task_lower)

        # 4. Compute project and time modifiers
        project_mod = self._get_project_modifier(project_path)
        time_mod = self._get_time_modifier()

        # 4b. Risk evidence from the files themselves — the term the score model
        # was missing entirely. Without it the only inputs are the caller's word
        # choices, so "rewrite the scanner" scores the same whether the target is
        # a 40-line helper or a 7,000-line SQL layer. Applied here, after
        # low_delta, so file risk cannot suppress trivial-task detection.
        evidence_mod = self._evidence_score(evidence, complexity_matched)

        # 5. Apply all modifiers, clamp within [0.0, 1.0]
        effective_score = max(
            0.0,
            min(
                1.0,
                raw_score + low_delta + intent_mod + project_mod + time_mod + evidence_mod,
            ),
        )
        effective_score = round(effective_score, 2)

        # 5b. Compute reasoning score; bump tier to at least medium if it dominates
        reasoning_score, reasoning_matched = self._compute_reasoning_score(
            task_lower,
            enabled=self._config.reasoning_scoring_enabled,
        )
        final_score = effective_score
        reasoning_fired = False
        if reasoning_score > effective_score and reasoning_score > 0.15:
            final_score = round(reasoning_score, 2)
            reasoning_fired = True

        # 6. Resolve tier
        tier = self._tier_from_score(final_score, project_path=project_path)
        # When reasoning fires, enforce a minimum of "medium"
        if reasoning_fired and tier == "low":
            tier = "medium"
        # Security-sensitive work is never low-risk, but routine implementation
        # should still score naturally instead of being forced upward. Floors
        # only — see _resolve_risk_floor for the three evidence sources.
        resolved_floor, floor_evidence = self._resolve_risk_floor(
            task_lower, evidence, trivial_task=low_delta != 0.0
        )
        risk_floor = resolved_floor or str(self._config.risk_floor_tier or "medium")
        security_floor_fired = (
            resolved_floor is not None
            and _TIER_RANK.get(tier, 0) < _TIER_RANK.get(resolved_floor, 1)
        )
        if security_floor_fired:
            tier = resolved_floor

        # 7. Compute urgency explainability surface (Phase 14)
        urgency_score, urgency_matched = self._compute_urgency_modifier(task_lower)

        # 8. Build reason string
        all_matched = complexity_matched + low_matched + intent_matched
        if reasoning_fired:
            all_matched = all_matched + reasoning_matched
        # include urgency matches in human-readable reason without changing legacy parts
        reason_parts = ", ".join(all_matched) if all_matched else "base score only"
        if urgency_matched:
            reason_parts = reason_parts + ", " + ", ".join(urgency_matched) if reason_parts != "base score only" else ", ".join(urgency_matched)

        mod_parts: list[str] = []
        if low_delta != 0.0:
            mod_parts.append(f"low_override={low_delta:+.2f}")
        if intent_mod != 0.0:
            mod_parts.append(f"intent={intent_mod:+.2f}")
        if project_mod != 0.0:
            mod_parts.append(f"project={project_mod:+.2f}")
        if time_mod != 0.0:
            mod_parts.append(f"time={time_mod:+.2f}")
        if evidence_mod != 0.0:
            mod_parts.append(f"evidence={evidence_mod:+.2f}")
        if urgency_score != 0.0:
            mod_parts.append(f"urgency={urgency_score:+.2f}")
        if reasoning_fired:
            mod_parts.append(f"reasoning={reasoning_score:+.2f}")
        if security_floor_fired:
            mod_parts.append(f"security_floor={risk_floor}")
            if floor_evidence:
                mod_parts.append(f"floor_evidence={floor_evidence}")

        if mod_parts:
            reason = (
                f"raw={raw_score:.2f}, {', '.join(mod_parts)}, "
                f"effective={final_score} [{reason_parts}] → {tier}"
            )
        else:
            reason = f"score={final_score} [{reason_parts}] → {tier}"

        # 9. Duration axis — advisory only: never touches tier or model.
        file_count = count_task_files(task)
        duration_bucket = duration_bucket_for(
            final_score,
            file_count=file_count,
            thresholds=self._get_thresholds(score=final_score, project_path=project_path),
        )

        agents = 2 if tier != "high" else 1
        r_effort, t_budget = reasoning_params_for(duration_bucket, tier)
        decision = RoutingDecision(
            tier=tier,
            score=final_score,
            reason=reason,
            agents=agents,
            override=False,
            intent_modifier=intent_mod,
            urgency_score=urgency_score,
            matched_urgency_signals=urgency_matched,
            expected_duration_bucket=duration_bucket,
            expected_file_count=file_count,
            reasoning_effort=r_effort,
            thinking_budget=t_budget,
        )
        bandit_decision = self._log_bandit_decision(
            task, decision, project_path=project_path
        )
        # In live mode (and only once every arm has cleared bandit_min_updates)
        # the bandit's pick is the routing decision, not a shadow annotation.
        #
        # Safety floors still bind. A learned policy is allowed to disagree with
        # the score, never to undercut the security risk floor or the reasoning
        # minimum — those exist because some work must not run cheap regardless
        # of what past outcomes looked like.
        if bandit_decision is not None and bandit_decision.reason == "bandit":
            bandit_tier = bandit_decision.chosen_arm.split(":", 1)[0]
            floor_tier = "low"
            if security_floor_fired:
                floor_tier = risk_floor
            elif reasoning_fired:
                floor_tier = "medium"
            if (
                bandit_tier in _TIER_RANK
                and bandit_tier != decision.tier
                and _TIER_RANK[bandit_tier] >= _TIER_RANK.get(floor_tier, 0)
            ):
                decision.tier = bandit_tier
                decision.agents = 2 if bandit_tier != "high" else 1
                decision.reason = f"{reason} → bandit:{bandit_tier}"
        return decision

    def _log_bandit_decision(
        self,
        task: str,
        decision: "RoutingDecision",
        project_path: str | None = None,
    ) -> "BanditDecision | None":
        """Log the bandit pick alongside the heuristic pick. Best-effort.

        The row is keyed on :func:`shared.outcomes.route_task_id` — the same
        stable task hash ``route_task`` → ``record_outcome`` already correlate
        on. It used to be a fresh ``uuid4()``, which no outcome writer could ever
        join against, so ``outcome_score`` stayed NULL on every row ever written
        and the bandit had no training data at all.
        """
        if self._db is None:
            return
        try:
            from .bandit import extract_task_features, get_bandit_policy
            from .outcomes import route_task_id

            features = extract_task_features(task, project_id=project_path or "")
            heuristic_arm = f"{decision.tier}:heuristic"
            # One arm per tier — the arm space routing actually chooses between.
            available_arms = [
                "low:heuristic", "medium:heuristic", "high:heuristic"
            ]
            routing_cfg = getattr(self._config, "routing", None)
            policy = get_bandit_policy(
                db=self._db,
                alpha=float(getattr(routing_cfg, "bandit_alpha", 1.0)),
                mode=str(getattr(routing_cfg, "bandit_mode", "shadow")),
                min_updates=int(getattr(routing_cfg, "bandit_min_updates", 50)),
            )
            bandit_decision = policy.select(features, available_arms, heuristic_arm)
            self._db.log_routing_decision(
                task_id=route_task_id(task),
                features=features,
                heuristic_pick=bandit_decision.heuristic_arm,
                bandit_pick=bandit_decision.bandit_arm,
                chosen=bandit_decision.chosen_arm,
            )
            return bandit_decision
        except Exception:
            log.debug("bandit decision log failed", exc_info=True)
            return None
