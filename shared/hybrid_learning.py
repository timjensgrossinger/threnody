"""Learned delta for the hybrid diagnose→implement split.

The split runs a read-only high-tier *diagnosis* first, then lets a cheaper tier
*implement* against that change-spec. How much cheaper is not a constant — it is
learned per work profile, because the answer differs by file shape and language.

Two EMAs per profile key:

``rework_ema``
    Fraction of split runs at the current delta that came back dirty — the
    implementer produced rework, or the verify gate found new failures. High →
    the discount was too aggressive → pull the delta back toward 0.
``clean_ema``
    Fraction that came back clean. Consistently high → the discount is safe and
    may go one step further.

Reads happen once at plan build (cold path, one indexed query); writes happen at
swarm finalize. With no data the loader returns an empty map, so a fresh repo runs
the pure configured default — same fresh-repo discipline as
:mod:`shared.review_learning`, whose EMA shape this mirrors.
"""

from __future__ import annotations

import logging
import time

from .db import Database

log = logging.getLogger(__name__)

EMA_ALPHA = 0.10  # matches adaptive.py / review_learning.py

DEFAULT_MIN_SAMPLES = 4
# Rework above this share means the discount is costing more than it saves.
DEFAULT_REWORK_THRESHOLD = 0.35
# Clean above this share means the discount is comfortably safe.
DEFAULT_CLEAN_THRESHOLD = 0.85


def record_hybrid_outcome(
    db: Database,
    *,
    profile_key: str,
    delta: int,
    clean: bool,
) -> None:
    """EMA-update the split outcome for one profile at the delta that ran.

    ``clean`` should be True only when the implementer needed no rework AND the
    verify gate reported no new failures — a genuinely successful discount.
    Best-effort; never raises into finalize.
    """
    if not profile_key:
        return
    try:
        now = time.time()
        obs_rework = 0.0 if clean else 1.0
        obs_clean = 1.0 if clean else 0.0
        with db.conn() as conn:
            row = conn.execute(
                "SELECT rework_ema, clean_ema, sample_count FROM hybrid_tier_bias "
                "WHERE profile_key = ? AND delta = ?",
                (profile_key, int(delta)),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO hybrid_tier_bias "
                    "(profile_key, delta, rework_ema, clean_ema, sample_count, updated_at) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    (profile_key, int(delta), obs_rework, obs_clean, now),
                )
            else:
                rework, cleaned, count = row
                rework = EMA_ALPHA * obs_rework + (1 - EMA_ALPHA) * float(rework or 0.0)
                cleaned = EMA_ALPHA * obs_clean + (1 - EMA_ALPHA) * float(cleaned or 0.0)
                conn.execute(
                    "UPDATE hybrid_tier_bias SET rework_ema = ?, clean_ema = ?, "
                    "sample_count = ?, updated_at = ? WHERE profile_key = ? AND delta = ?",
                    (rework, cleaned, int(count or 0) + 1, now, profile_key, int(delta)),
                )
    except Exception:  # pragma: no cover - learning is best-effort
        log.debug("record_hybrid_outcome failed", exc_info=True)


def load_hybrid_delta_bias(
    db: Database,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    rework_threshold: float = DEFAULT_REWORK_THRESHOLD,
    clean_threshold: float = DEFAULT_CLEAN_THRESHOLD,
) -> dict[str, int]:
    """Return ``{profile_key: adjustment}`` for confident profiles only.

    ``adjustment`` is added to the configured delta: ``+1`` shrinks the discount
    (delta -2 → -1) when rework was common, ``-1`` deepens it when runs were
    consistently clean. Profiles below ``min_samples`` are omitted. Empty on any
    error or empty table, so the caller falls back to the configured default.
    """
    out: dict[str, int] = {}
    try:
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT profile_key, delta, rework_ema, clean_ema, sample_count "
                "FROM hybrid_tier_bias WHERE sample_count >= ? "
                "ORDER BY profile_key, sample_count DESC",
                (min_samples,),
            ).fetchall()
        for profile_key, _delta, rework_ema, clean_ema, _count in rows:
            key = str(profile_key)
            if key in out:
                continue  # highest-sample row per profile wins
            if rework_ema is not None and float(rework_ema) >= rework_threshold:
                out[key] = 1
            elif clean_ema is not None and float(clean_ema) >= clean_threshold:
                out[key] = -1
    except Exception:  # pragma: no cover - best-effort read
        log.debug("load_hybrid_delta_bias failed", exc_info=True)
    return out


__all__ = [
    "EMA_ALPHA",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_REWORK_THRESHOLD",
    "DEFAULT_CLEAN_THRESHOLD",
    "record_hybrid_outcome",
    "load_hybrid_delta_bias",
]
