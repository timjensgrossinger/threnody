"""Journal -> database replay handlers.

Split out of :mod:`shared.learning_journal` so the journal's *write* path stays
free of any database import. A PostToolUse hook, a one-shot CLI, or the MCP
server mid-corruption can all append events; only a deliberate replay pulls the
DB layer in.

Each handler must be safe to run against a database that may already hold the
row. For keyed rows that means an upsert; for the EMA-backed bias tables it is
not achievable at all, which is why ``learning_journal.replay`` only replays
those in a full rebuild that resets the table first.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .learning_journal import (
    KIND_HANDOFF_AGENT,
    KIND_HYBRID,
    KIND_MODEL_QUALITY,
    KIND_REVIEW_TIER,
    register_handler,
)

log = logging.getLogger(__name__)


def _replay_model_quality(db: Any, event: dict[str, Any]) -> None:
    """Re-insert one ledger row, keyed on ``event_id`` so repeats are no-ops."""
    from .model_quality import write_quality_row

    write_quality_row(
        db,
        event,
        event_id=str(event.get("event_id") or "") or None,
        ts=float(event.get("ts") or 0.0),
    )


def _replay_handoff_agent(db: Any, event: dict[str, Any]) -> None:
    """Restore one per-agent spawn snapshot.

    These snapshots are the join key the whole learning path depends on: a
    hook-captured run-log line carries no model, tier, role or dimension, and
    ``host_learning`` recovers all four by matching touched files against them.
    Losing the DB therefore made every surviving ``run_log`` unreplayable — the
    logs were intact and worthless. Journaling the snapshot is what breaks that
    dependency.
    """
    snapshot = event.get("snapshot")
    if not isinstance(snapshot, dict):
        return
    run_id = str(event.get("run_id") or "")
    if not run_id:
        return
    index = event.get("agent_index")
    try:
        worker_index = int(index)
    except (TypeError, ValueError):
        worker_index = 0
    db.persist_worker_snapshot(
        run_id,
        worker_index,
        json.dumps(snapshot, sort_keys=True, default=str),
        ts=float(event.get("ts") or 0.0),
    )


def _replay_review_tier(db: Any, event: dict[str, Any]) -> None:
    """Re-apply one review-tier observation (rebuild mode only)."""
    from .review_learning import record_review_tier_outcome

    record_review_tier_outcome(
        db,
        profile_key=str(event.get("profile_key") or ""),
        dimension=str(event.get("dimension") or ""),
        tier=str(event.get("tier") or ""),
        findings_high=int(event.get("findings_high") or 0),
        findings_total=int(event.get("findings_total") or 0),
        # Tri-state: a missing/None verdict must NOT collapse to False here, or the
        # rebuild would invert every unadjudicated observation the live path recorded.
        kept_by_synthesis=(
            None
            if event.get("kept_by_synthesis") is None
            else bool(event.get("kept_by_synthesis"))
        ),
        journal=False,  # we are reading the journal; re-appending would grow it
    )


def _replay_hybrid(db: Any, event: dict[str, Any]) -> None:
    """Re-apply one hybrid diagnose->implement observation (rebuild mode only)."""
    from .hybrid_learning import record_hybrid_outcome

    record_hybrid_outcome(
        db,
        profile_key=str(event.get("profile_key") or ""),
        delta=int(event.get("delta") or 0),
        clean=bool(event.get("clean")),
        journal=False,  # we are reading the journal; re-appending would grow it
    )


register_handler(KIND_MODEL_QUALITY, _replay_model_quality)
register_handler(KIND_HANDOFF_AGENT, _replay_handoff_agent)
register_handler(KIND_REVIEW_TIER, _replay_review_tier)
register_handler(KIND_HYBRID, _replay_hybrid)
