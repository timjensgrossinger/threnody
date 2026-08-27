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

    Only rows carrying a non-empty ``run_id`` are considered. A real observation
    is always attributable to a run: ``host_learning`` passes one on every
    findings/static_recall/verify_gate write. Synthetic rows are not, and this is
    the read path that turns a ledger row into a production tier change, so it is
    the right place to insist on provenance. The reference install demonstrated
    why: its ledger held 467 rows of which only 31 had a ``run_id``, and the
    remainder — test fixtures that reached the live DB through the learning
    journal — were fully eligible to move routing decisions.
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
                "AND run_id IS NOT NULL AND TRIM(run_id) != '' "
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


def load_kind_quality_bias(
    db: Database,
    *,
    since: str = DEFAULT_WINDOW,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    low_threshold: float = DEFAULT_LOW_THRESHOLD,
    high_threshold: float = DEFAULT_HIGH_THRESHOLD,
) -> dict[tuple[str, str], int]:
    """Return ``{(model, kind): step}`` from objective rows carrying a task kind.

    The sibling of :func:`load_model_quality_bias` on the *subject-matter* axis.
    It exists because that function groups by ``dimension``, and every ladder row
    writes ``dimension='general'`` — so graded evidence about "this model handles
    XSS fixes" was pooled into one meaningless bucket that no consumer matched.

    Same guards: objective sources only, a minimum sample count, and a non-empty
    ``run_id`` so a leaked fixture cannot steer routing. ``{}`` on any failure or an
    empty ledger, so a fresh repo runs the pure heuristic.
    """
    from .model_quality import OBJECTIVE_SOURCES, parse_quality_window

    out: dict[tuple[str, str], int] = {}
    try:
        since_ts, _ = parse_quality_window(since)
        placeholders = ",".join("?" for _ in OBJECTIVE_SOURCES)
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT model, kind, AVG(score_0_10), COUNT(*) "
                "FROM model_quality_events "
                f"WHERE source IN ({placeholders}) AND ts >= ? "
                "AND kind IS NOT NULL AND TRIM(kind) != '' "
                "AND run_id IS NOT NULL AND TRIM(run_id) != '' "
                "GROUP BY model, kind",
                (*sorted(OBJECTIVE_SOURCES), since_ts),
            ).fetchall()
    except Exception:  # pragma: no cover - best-effort read
        log.debug("load_kind_quality_bias failed", exc_info=True)
        return {}

    for model, kind, avg_score, count in rows:
        if not model or not kind:
            continue
        try:
            n = int(count or 0)
            avg = float(avg_score)
        except (TypeError, ValueError):
            continue
        if n < min_samples:
            continue
        if avg <= low_threshold:
            out[(str(model), str(kind))] = 1
        elif avg >= high_threshold:
            out[(str(model), str(kind))] = -1
    return out


def apply_quality_floor(
    db: Database,
    bias: dict[tuple[str, str], int],
    *,
    since: str = "all",
) -> dict[tuple[str, str], int]:
    """Drop de-escalations that contradict a model's graded ladder floor.

    The ladder records the cheapest tier that swept **every** case in a level
    (`build_min_passing_tier_map`) or a task kind (`build_min_passing_tier_by_kind`).
    A ``-1`` step says "this model is over-performing, run it cheaper"; the ladder
    can contradict that with graded evidence.

    The veto is scoped to *"the model has never swept anything at ``low``"*. It used
    to fire when the model had **any** floor above ``low`` in **any** level, which is
    almost every model that has been graded at all — one `L6 → high` result vetoed
    every de-escalation for that model across every dimension, so the ladder acted as
    a blanket ban rather than as evidence. Reading the minimum floor instead means a
    model that demonstrably handles *some* graded work from the cheap tier can still
    be de-escalated, while a model that has never managed that is left alone.

    Both axes are consulted, so kind-graded evidence counts alongside level-graded
    evidence. Escalations are never filtered — being wrong about needing *more*
    capability costs money, being wrong about needing less costs correctness.
    """
    if not bias:
        return bias
    try:
        from .model_quality import (
            build_min_passing_tier_by_kind,
            build_min_passing_tier_map,
        )

        floors = build_min_passing_tier_map(db, since=since)
        kind_floors = build_min_passing_tier_by_kind(db, since=since)
    except Exception:  # pragma: no cover - best-effort read
        log.debug("apply_quality_floor failed", exc_info=True)
        return bias
    if not floors and not kind_floors:
        return bias

    def _sweeps_anything_cheaply(model: str) -> bool:
        """True when some graded level or kind was swept at the cheapest tier."""
        for source in (floors, kind_floors):
            for tier in (source.get(model) or {}).values():
                if tier in _TIER_ORDER and _TIER_ORDER.index(tier) == 0:
                    return True
        return False

    def _has_any_floor(model: str) -> bool:
        return bool(floors.get(model)) or bool(kind_floors.get(model))

    out = dict(bias)
    for (model, dimension), step in bias.items():
        if step >= 0:
            continue
        if not _has_any_floor(model):
            continue  # no graded evidence either way — leave the step alone
        if not _sweeps_anything_cheaply(model):
            out.pop((model, dimension), None)
    return out


__all__ = [
    "DEFAULT_HIGH_THRESHOLD",
    "DEFAULT_LOW_THRESHOLD",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_WINDOW",
    "apply_quality_floor",
    "load_kind_quality_bias",
    "load_model_quality_bias",
]
