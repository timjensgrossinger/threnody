"""
shared.adaptive — EMA-based adaptive threshold system.

Phase 3: Tracks success/failure per complexity band per tier.
Computes exponential moving averages to dynamically adjust tier boundaries.
Both Copilot and Claude Code versions share the same adaptive state.
"""
from __future__ import annotations

import json
import logging
import time

from .config import (
    ThresholdConfig,
    LOW_TIER_FLOOR,
    LOW_TIER_CEILING,
    MEDIUM_HIGH_BOUNDARY_FLOOR,
    MEDIUM_HIGH_BOUNDARY_CEILING,
)
from .db import Database

log = logging.getLogger(__name__)

# EMA smoothing factor — higher = more responsive, lower = more stable
EMA_ALPHA = 0.10

# Minimum success rate before narrowing a tier's range
SUCCESS_THRESHOLD = 0.85

# Per-project learning remains gated until a project has enough local observations.
PROJECT_SAMPLE_MIN = 3

# Complexity bands: 0.0-0.1, 0.1-0.2, ..., 0.9-1.0
BANDS = [f"{i/10:.1f}-{(i+1)/10:.1f}" for i in range(10)]


def band_for_score(score: float) -> str:
    """Map a complexity score to its band label."""
    idx = min(int(score * 10), 9)
    return BANDS[idx]


def update_band(
    db: Database,
    score: float,
    tier: str,
    success: bool,
    version: str = "shared",
) -> None:
    """Record an outcome and update the EMA for a complexity band.

    Args:
        db: Database instance.
        score: The complexity score (0.0–1.0).
        tier: The tier that was used (low/medium/high).
        success: Whether the agent succeeded (True) or reworked/failed (False).
        version: Which version produced this outcome (copilot/claude-code/shared).
    """
    band = band_for_score(score)
    outcome = 1.0 if success else 0.0

    with db.conn() as conn:
        row = conn.execute(
            "SELECT success_ema, sample_count FROM adaptive_thresholds "
            "WHERE band = ? AND version = ? AND tier = ?",
            (band, version, tier),
        ).fetchone()

        if row is None:
            # First observation for this band/version/tier
            conn.execute(
                "INSERT INTO adaptive_thresholds "
                "(band, version, tier, success_ema, sample_count, ts) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (band, version, tier, outcome, time.time()),
            )
        else:
            old_ema, count = row
            new_ema = EMA_ALPHA * outcome + (1 - EMA_ALPHA) * old_ema
            conn.execute(
                "UPDATE adaptive_thresholds "
                "SET success_ema = ?, sample_count = ?, ts = ? "
                "WHERE band = ? AND version = ? AND tier = ?",
                (new_ema, count + 1, time.time(), band, version, tier),
            )

    log.debug(
        "Band %s tier=%s version=%s: outcome=%s",
        band, tier, version, "success" if success else "failure",
    )


def get_band_sample_count(
    db: Database,
    score: float,
    version: str = "shared",
) -> int:
    """Return the total observations recorded for a score band."""
    band = band_for_score(score)
    with db.conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(sample_count), 0) FROM adaptive_thresholds "
            "WHERE band = ? AND version = ?",
            (band, version),
        ).fetchone()
    return int(row[0] or 0) if row else 0


def get_project_sample_count(db: Database, project_id: str) -> int:
    """Return the recorded local-learning sample count for one project."""
    with db.conn() as conn:
        row = conn.execute(
            "SELECT overrides_json FROM project_routing WHERE project_path = ?",
            (project_id,),
        ).fetchone()
    if not row or not row[0]:
        return 0
    try:
        overrides = json.loads(row[0])
    except json.JSONDecodeError:
        return 0
    if not isinstance(overrides, dict):
        return 0
    return int(overrides.get("learning_sample_count", 0))


def register_observation(
    db: Database,
    project_id: str,
    signal_dict: dict,
) -> int:
    """Record one project-local learning observation and return the new count."""
    with db.conn() as conn:
        row = conn.execute(
            "SELECT overrides_json, learning_enabled FROM project_routing WHERE project_path = ?",
            (project_id,),
        ).fetchone()
    if row:
        try:
            overrides = json.loads(row[0]) if row[0] else {}
        except json.JSONDecodeError:
            overrides = {}
        if not isinstance(overrides, dict):
            overrides = {}
        learning_enabled = int(row[1] or 0)
    else:
        overrides = {}
        learning_enabled = 0

    overrides.setdefault("tier_bias", 0.0)
    overrides.setdefault("sample_count", 0)
    overrides["learning_sample_count"] = int(overrides.get("learning_sample_count", 0)) + 1
    overrides["last_learning_signal"] = {
        "rework_count": int(signal_dict.get("rework_count", 0)),
        "token_cost": int(signal_dict.get("token_cost", 0)),
        "success": bool(signal_dict.get("success", False)),
        "timestamp": signal_dict.get("timestamp", time.time()),
    }

    with db.conn() as conn:
        conn.execute(
            """
            INSERT INTO project_routing (project_path, overrides_json, learning_enabled, ts)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(project_path) DO UPDATE SET
                overrides_json = excluded.overrides_json,
                learning_enabled = excluded.learning_enabled,
                ts = excluded.ts
            """,
            (project_id, json.dumps(overrides), learning_enabled, time.time()),
        )
    return int(overrides["learning_sample_count"])


def should_apply_adaptive_thresholds(
    project_id: str,
    *,
    band_sample_count: int,
    project_sample_count: int,
    band_min_samples: int = 5,
) -> bool:
    """Return True only when both global band data and local project data are mature."""
    return bool(
        project_id
        and band_sample_count >= band_min_samples
        and project_sample_count >= PROJECT_SAMPLE_MIN
    )


def compute_thresholds(
    db: Database,
    min_samples: int = 5,
) -> ThresholdConfig:
    """Compute adaptive tier boundaries from accumulated EMA data.

    Queries all bands and computes per-tier success aggregates.
    If a tier's average success EMA drops below SUCCESS_THRESHOLD,
    narrows its effective range (makes it harder to qualify for that tier).

    Returns a ThresholdConfig with clamped values respecting hard bounds.
    """
    with db.conn() as conn:
        rows = conn.execute(
            "SELECT tier, AVG(success_ema), SUM(sample_count) "
            "FROM adaptive_thresholds "
            "WHERE sample_count >= ? "
            "GROUP BY tier",
            (min_samples,),
        ).fetchall()

    tier_ema: dict[str, float] = {}
    tier_samples: dict[str, int] = {}
    for tier, avg_ema, total_samples in rows:
        tier_ema[tier] = avg_ema
        tier_samples[tier] = total_samples

    # Start from defaults
    low_max = (LOW_TIER_FLOOR + LOW_TIER_CEILING) / 2   # 0.625
    medium_max = (MEDIUM_HIGH_BOUNDARY_FLOOR + MEDIUM_HIGH_BOUNDARY_CEILING) / 2  # 0.85

    # If low tier success is poor, shrink its range (lower low_max)
    low_ema = tier_ema.get("low", 0.90)
    if low_ema < SUCCESS_THRESHOLD:
        deficit = SUCCESS_THRESHOLD - low_ema
        # Shrink proportionally: max 0.10 adjustment
        adjustment = min(deficit * 0.5, 0.10)
        low_max -= adjustment
        log.info(
            "Adaptive: low tier EMA=%.3f < %.2f, narrowing low_max by %.3f → %.3f",
            low_ema, SUCCESS_THRESHOLD, adjustment, low_max,
        )

    # If medium tier success is poor, shrink its range (lower medium_max)
    medium_ema = tier_ema.get("medium", 0.90)
    if medium_ema < SUCCESS_THRESHOLD:
        deficit = SUCCESS_THRESHOLD - medium_ema
        adjustment = min(deficit * 0.5, 0.10)
        medium_max -= adjustment
        log.info(
            "Adaptive: medium tier EMA=%.3f < %.2f, narrowing medium_max by %.3f → %.3f",
            medium_ema, SUCCESS_THRESHOLD, adjustment, medium_max,
        )

    # If high tier success is excellent, we could widen medium range,
    # but we keep it conservative for now
    high_ema = tier_ema.get("high", 0.90)
    if high_ema > 0.95 and medium_ema > SUCCESS_THRESHOLD:
        # High tier is overperforming — widen medium range slightly
        medium_max = min(medium_max + 0.02, MEDIUM_HIGH_BOUNDARY_CEILING)

    result = ThresholdConfig(low_max=low_max, medium_max=medium_max)
    # clamp() is called automatically via __post_init__

    log.debug(
        "Adaptive thresholds: low_max=%.3f, medium_max=%.3f "
        "(low_ema=%.3f, med_ema=%.3f, high_ema=%.3f, samples=%s)",
        result.low_max, result.medium_max,
        low_ema, medium_ema, high_ema, tier_samples,
    )
    return result


def get_band_stats(db: Database) -> list[dict]:
    """Return all band statistics for inspection/debugging."""
    with db.conn() as conn:
        rows = conn.execute(
            "SELECT band, version, tier, success_ema, sample_count, ts "
            "FROM adaptive_thresholds ORDER BY band, tier",
        ).fetchall()
    return [
        {
            "band": r[0],
            "version": r[1],
            "tier": r[2],
            "success_ema": r[3],
            "sample_count": r[4],
            "ts": r[5],
        }
        for r in rows
    ]


_TIER_UP = {"low": "medium", "medium": "high", "high": "high"}


def get_role_tier_bumps(
    db: Database,
    *,
    rework_threshold: float = 0.25,
    min_samples: int = 5,
) -> dict[str, str]:
    """Return per-role tier bumps based on rework rate from telemetry.

    For each role with >= min_samples observations and rework rate >= rework_threshold,
    return a mapping role -> bumped tier (e.g. {"Reviewer": "high"}).
    Roles below threshold or sample floor are excluded.
    """
    try:
        with db.conn() as conn:
            rows = conn.execute(
                """
                SELECT role,
                       COUNT(*) AS total,
                       SUM(CASE WHEN rework = 1 OR rework_count > 0 THEN 1 ELSE 0 END) AS rework_count
                FROM telemetry
                WHERE role IS NOT NULL AND role != ''
                GROUP BY role
                HAVING total >= ?
                """,
                (min_samples,),
            ).fetchall()
    except Exception:
        log.debug("get_role_tier_bumps query failed", exc_info=True)
        return {}

    bumps: dict[str, str] = {}
    for role, total, rework_count in rows:
        if total <= 0:
            continue
        rate = rework_count / total
        if rate >= rework_threshold:
            if role in ("low",):
                bumps[role] = "medium"
            elif role in ("medium",):
                bumps[role] = "high"
            else:
                bumps[role] = "high"
        log.debug(
            "role %s: rework rate %.2f (%d/%d) %s threshold %.2f",
            role, rate, rework_count, total,
            ">= (bumping)" if rate >= rework_threshold else "<",
            rework_threshold,
        )
    return bumps
