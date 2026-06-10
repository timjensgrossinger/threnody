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

from .config import (
    TGsConfig,
    SPEED_SIGNALS,
    QUALITY_SIGNALS,
    REASONING_SIGNALS,
    LOW_TIER_FLOOR,
    LOW_TIER_CEILING,
    MEDIUM_HIGH_BOUNDARY_FLOOR,
    MEDIUM_HIGH_BOUNDARY_CEILING,
)
from .db import Database

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
                return RoutingDecision(
                    tier="high",
                    score=0.90,
                    reason=f"keyword override → high: '{kw}'",
                    agents=1,
                    override=True,
                )
        return None

    def _check_low_overrides(
        self, task_lower: str, computed_score: float
    ) -> RoutingDecision | None:
        """Check low-tier overrides — soft: only fires when complexity score is already low."""
        if computed_score >= self._thresholds.low_max:
            return None
        for kw in self._overrides.get("low", []):
            if re.search(rf"\b{re.escape(kw)}\b", task_lower):
                return RoutingDecision(
                    tier="low",
                    score=0.15,
                    reason=f"keyword override → low: '{kw}'",
                    agents=2,
                    override=True,
                )
        return None

    # ------------------------------------------------------------------
    # Complexity scoring
    # ------------------------------------------------------------------

    def _compute_score(self, task_lower: str) -> tuple[float, list[str]]:
        """Compute raw complexity score from keyword signals."""
        score = self._base_score
        matched: list[str] = []

        for level in ("high", "medium", "low"):
            weight = self._weights.get(level, 0.0)
            keywords = self._signals.get(level, [])
            for kw in keywords:
                if kw in task_lower:
                    score += weight
                    matched.append(f"{kw}(+{weight})")

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

        return min(score, 1.0), matched

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
        """Return whether project-local learning is enabled for this project."""
        if not self._db or not project_id:
            return False
        try:
            with self._db.conn() as conn:
                row = conn.execute(
                    "SELECT learning_enabled FROM project_routing WHERE project_path = ?",
                    (project_id,),
                ).fetchone()
            return bool(row[0]) if row else False
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
    # Main classification
    # ------------------------------------------------------------------

    def classify(self, task: str, project_path: str | None = None) -> RoutingDecision:
        """Classify a task into a tier with intent, project, and time awareness."""
        task_lower = task.lower().strip()

        # 1. High-tier overrides first (hard — always win)
        high_override = self._check_high_overrides(task_lower)
        if high_override:
            return high_override

        # 2. Compute raw complexity score
        raw_score, complexity_matched = self._compute_score(task_lower)

        # 2b. Low-tier overrides (soft — only when raw score is already low)
        low_override = self._check_low_overrides(task_lower, raw_score)
        if low_override:
            return low_override

        # 3. Compute intent modifier
        intent_mod, intent_matched = self._compute_intent_modifier(task_lower)

        # 4. Compute project and time modifiers
        project_mod = self._get_project_modifier(project_path)
        time_mod = self._get_time_modifier()

        # 5. Apply all modifiers, clamp within [0.0, 1.0]
        effective_score = max(0.0, min(1.0, raw_score + intent_mod + project_mod + time_mod))
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
        # Auth changes are never low-risk, but routine implementation should
        # still score naturally instead of being forced to high tier.
        security_floor_fired = (
            tier == "low"
            and re.search(r"\b(authentication|authorization)\b", task_lower)
            is not None
        )
        if security_floor_fired:
            tier = "medium"

        # 7. Compute urgency explainability surface (Phase 14)
        urgency_score, urgency_matched = self._compute_urgency_modifier(task_lower)

        # 8. Build reason string
        all_matched = complexity_matched + intent_matched
        if reasoning_fired:
            all_matched = all_matched + reasoning_matched
        # include urgency matches in human-readable reason without changing legacy parts
        reason_parts = ", ".join(all_matched) if all_matched else "base score only"
        if urgency_matched:
            reason_parts = reason_parts + ", " + ", ".join(urgency_matched) if reason_parts != "base score only" else ", ".join(urgency_matched)

        mod_parts: list[str] = []
        if intent_mod != 0.0:
            mod_parts.append(f"intent={intent_mod:+.2f}")
        if project_mod != 0.0:
            mod_parts.append(f"project={project_mod:+.2f}")
        if time_mod != 0.0:
            mod_parts.append(f"time={time_mod:+.2f}")
        if urgency_score != 0.0:
            mod_parts.append(f"urgency={urgency_score:+.2f}")
        if reasoning_fired:
            mod_parts.append(f"reasoning={reasoning_score:+.2f}")
        if security_floor_fired:
            mod_parts.append("security_floor=medium")

        if mod_parts:
            reason = (
                f"raw={raw_score:.2f}, {', '.join(mod_parts)}, "
                f"effective={final_score} [{reason_parts}] → {tier}"
            )
        else:
            reason = f"score={final_score} [{reason_parts}] → {tier}"

        agents = 2 if tier != "high" else 1
        decision = RoutingDecision(
            tier=tier,
            score=final_score,
            reason=reason,
            agents=agents,
            override=False,
            intent_modifier=intent_mod,
            urgency_score=urgency_score,
            matched_urgency_signals=urgency_matched,
        )
        self._shadow_bandit_log(task, decision, project_path=project_path)
        return decision

    def _shadow_bandit_log(
        self,
        task: str,
        decision: "RoutingDecision",
        project_path: str | None = None,
    ) -> None:
        """Log bandit shadow pick alongside heuristic pick. Best-effort."""
        if self._db is None:
            return
        try:
            from .bandit import extract_task_features, get_bandit_policy
            features = extract_task_features(task, project_id=project_path or "")
            heuristic_arm = f"{decision.tier}:heuristic"
            # Available arms: one per tier for simplicity in shadow mode
            available_arms = [
                "low:heuristic", "medium:heuristic", "high:heuristic"
            ]
            policy = get_bandit_policy(db=self._db, mode="shadow")
            bandit_decision = policy.select(features, available_arms, heuristic_arm)
            import uuid as _uuid
            self._db.log_routing_decision(
                task_id=str(_uuid.uuid4()),
                features=features,
                heuristic_pick=bandit_decision.heuristic_arm,
                bandit_pick=bandit_decision.bandit_arm,
                chosen=bandit_decision.chosen_arm,
            )
        except Exception:
            log.debug("shadow bandit log failed", exc_info=True)
