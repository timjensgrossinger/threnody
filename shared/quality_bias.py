"""Objective model-quality → tier bias — the missing read side of the ledger.

``shared/model_quality.py`` accumulates a 0-10 score per
``(model x effort x dimension x sub_dimension)``, but until now the only reader
was the operator CLI (``threnody quality``). Nothing fed it back into routing, so
a model that repeatedly left new verify-gate failures on a dimension kept being
picked for it.

This module closes that loop the same way ``review_learning.py`` and
``hybrid_learning.py`` do: a pure, dependency-light loader read **once at plan
build** (cold path, no LLM, no spawn cost) returning a clamped ±1 tier step per
``(model, dimension)``. With no data it returns an empty map, so a fresh repo
falls straight through to the pure heuristic.

Only OBJECTIVE sources move tiers — ``verify_gate``, ``static_recall`` and
``ladder``, all graded against something deterministic. The proxy sources
(``findings``, ``judge``) are deliberately excluded: ``findings`` already drives
``review_tier_bias`` through its own loop, and letting a model's own judgement
pick its own tier is circular.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database

log = logging.getLogger(__name__)

# Sources graded against ground truth. Mirrors model_quality.OBJECTIVE_SOURCES;
# imported lazily in the query so this module stays import-cheap.
DEFAULT_MIN_SAMPLES = 4
# Scores are 0-10. Below LOW the model is demonstrably under-performing on that
# dimension -> escalate. Above HIGH it is comfortably clearing the bar -> the
# next cheaper tier is worth trying.
DEFAULT_LOW_THRESHOLD = 4.0
DEFAULT_HIGH_THRESHOLD = 8.5
# 30 days: long enough to accumulate on a low-traffic repo, short enough that a
# model upgrade is not judged forever by its predecessor's record.
DEFAULT_WINDOW = "30d"

_TIER_ORDER = ("low", "medium", "high")


def load_model_quality_bias(
    db: Database,
    *,
    since: str = DEFAULT_WINDOW,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    low_threshold: float = DEFAULT_LOW_THRESHOLD,
    high_threshold: float = DEFAULT_HIGH_THRESHOLD,
) -> dict[tuple[str, str], int]:
    """Return ``{(model, dimension): step}`` for confident (model, dimension) pairs.

    ``step`` is ``+1`` when objective scores say the model is under-performing on
    that dimension and ``-1`` when it is comfortably over-performing. Pairs below
    ``min_samples`` are omitted entirely (no bias). Returns ``{}`` on any error or
    an empty ledger so the caller falls back to the pure heuristic.
    """
    from .model_quality import OBJECTIVE_SOURCES, parse_quality_window

    out: dict[tuple[str, str], int] = {}
    try:
        since_ts, _ = parse_quality_window(since)
        placeholders = ",".join("?" for _ in OBJECTIVE_SOURCES)
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT model, dimension, AVG(score_0_10), COUNT(*) "
                "FROM model_quality_events "
                f"WHERE source IN ({placeholders}) AND ts >= ? "
                "GROUP BY model, dimension",
                (*sorted(OBJECTIVE_SOURCES), since_ts),
            ).fetchall()
    except Exception:  # pragma: no cover - best-effort read
        log.debug("load_model_quality_bias failed", exc_info=True)
        return {}

    for model, dimension, avg_score, count in rows:
        if not model or not dimension:
            continue
        try:
            n = int(count or 0)
            avg = float(avg_score)
        except (TypeError, ValueError):
            continue
        if n < min_samples:
            continue
        if avg <= low_threshold:
            out[(str(model), str(dimension))] = 1
        elif avg >= high_threshold:
            out[(str(model), str(dimension))] = -1
    return out


def apply_quality_floor(
    db: Database,
    bias: dict[tuple[str, str], int],
    *,
    since: str = "all",
) -> dict[tuple[str, str], int]:
    """Drop de-escalations that contradict a model's graded ladder floor.

    ``model_quality.build_min_passing_tier_map`` records the cheapest tier that
    swept every case at a level. If a model has any recorded floor above ``low``,
    a ``-1`` step is speculative in a way the ladder already answered — so the
    step is dropped rather than trusted. Escalations are never filtered.
    """
    if not bias:
        return bias
    try:
        from .model_quality import build_min_passing_tier_map

        floors = build_min_passing_tier_map(db, since=since)
    except Exception:  # pragma: no cover - best-effort read
        log.debug("apply_quality_floor failed", exc_info=True)
        return bias
    if not floors:
        return bias

    out = dict(bias)
    for (model, dimension), step in bias.items():
        if step >= 0:
            continue
        levels = floors.get(model) or {}
        if any(_TIER_ORDER.index(t) > 0 for t in levels.values() if t in _TIER_ORDER):
            out.pop((model, dimension), None)
    return out


__all__ = [
    "DEFAULT_HIGH_THRESHOLD",
    "DEFAULT_LOW_THRESHOLD",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_WINDOW",
    "apply_quality_floor",
    "load_model_quality_bias",
]
