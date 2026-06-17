"""Profile-keyed review-tier learning — the cold/warm-path feedback loop.

Review agents are read-only, so the correctness signal comes from FINDINGS, not
file rework:
  * a low/medium-tier agent that produced high-severity findings synthesis KEPT
    → that profile was under-reviewed (escalate next time);
  * a high-tier agent that repeatedly returns nothing for a profile
    → over-provisioned (de-escalate).

Both signals are tracked as EMAs per (profile_key, dimension) in review_tier_bias.
Writes happen at swarm finalize (cold); reads happen once at plan-build (cold) and
feed a clamped tier step into review_fanout.tier_for. With no data the loader
returns an empty map → pure heuristic, zero spawn-path cost. Mirrors the EMA
discipline in shared/adaptive.py and writes via db.conn() like the rest of the
data layer.
"""
from __future__ import annotations

import logging
import time

from .db import Database

log = logging.getLogger(__name__)

EMA_ALPHA = 0.10  # matches adaptive.py

# Loader thresholds (overridable by caller / config).
DEFAULT_MIN_SAMPLES = 4
DEFAULT_ESCALATE_THRESHOLD = 0.50
DEFAULT_IDLE_THRESHOLD = 0.70


def record_review_tier_outcome(
    db: Database,
    *,
    profile_key: str,
    dimension: str,
    tier: str,
    findings_high: int,
    findings_total: int,
    kept_by_synthesis: bool,
) -> None:
    """EMA-update the bias signal for one reviewed (profile, dimension, tier).

    Only the EMA relevant to the tier that ran is moved: a cheap tier feeds the
    escalate signal, a high tier feeds the idle signal. sample_count always ++.
    Best-effort — never raises into the finalize path.
    """
    tier = (tier or "").lower()
    if tier == "high":
        col = "idle_ema"
        obs = 1.0 if findings_total <= 0 else 0.0
    elif tier in ("low", "medium"):
        col = "escalate_ema"
        obs = 1.0 if (findings_high > 0 and kept_by_synthesis) else 0.0
    else:
        return

    try:
        now = time.time()
        with db.conn() as conn:
            row = conn.execute(
                "SELECT escalate_ema, idle_ema, sample_count FROM review_tier_bias "
                "WHERE profile_key = ? AND dimension = ?",
                (profile_key, dimension),
            ).fetchone()
            if row is None:
                esc = obs if col == "escalate_ema" else 0.0
                idle = obs if col == "idle_ema" else 0.0
                conn.execute(
                    "INSERT INTO review_tier_bias "
                    "(profile_key, dimension, escalate_ema, idle_ema, sample_count, updated_at) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    (profile_key, dimension, esc, idle, now),
                )
            else:
                esc, idle, count = row
                new_val = EMA_ALPHA * obs + (1 - EMA_ALPHA) * (esc if col == "escalate_ema" else idle)
                if col == "escalate_ema":
                    esc = new_val
                else:
                    idle = new_val
                conn.execute(
                    "UPDATE review_tier_bias "
                    "SET escalate_ema = ?, idle_ema = ?, sample_count = ?, updated_at = ? "
                    "WHERE profile_key = ? AND dimension = ?",
                    (esc, idle, count + 1, now, profile_key, dimension),
                )
    except Exception:  # pragma: no cover - learning is best-effort
        log.debug("record_review_tier_outcome failed", exc_info=True)


def load_review_tier_bias(
    db: Database,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    escalate_threshold: float = DEFAULT_ESCALATE_THRESHOLD,
    idle_threshold: float = DEFAULT_IDLE_THRESHOLD,
) -> dict[tuple[str, str], int]:
    """Return {(profile_key, dimension): step} for confident profiles only.

    step = +1 when the profile was repeatedly under-reviewed, -1 when a high tier
    repeatedly idled. Profiles below min_samples are omitted (no bias). Empty dict
    on any error or empty table → caller falls back to the pure heuristic.
    """
    out: dict[tuple[str, str], int] = {}
    try:
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT profile_key, dimension, escalate_ema, idle_ema, sample_count "
                "FROM review_tier_bias WHERE sample_count >= ?",
                (min_samples,),
            ).fetchall()
        for profile_key, dimension, escalate_ema, idle_ema, _count in rows:
            if escalate_ema is not None and escalate_ema >= escalate_threshold:
                out[(profile_key, dimension)] = 1
            elif idle_ema is not None and idle_ema >= idle_threshold:
                out[(profile_key, dimension)] = -1
    except Exception:  # pragma: no cover - best-effort read
        log.debug("load_review_tier_bias failed", exc_info=True)
    return out
