#!/usr/bin/env python3
"""Swarm persistence domain helpers for Phase 31 scaffolding."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
from typing import Any

from .db import Database


def _get_db(db: Database | None) -> Database:
    if db is not None:
        return db
    from .db_client import open_database
    return open_database()


@dataclass
class SwarmRun:
    """Authoritative top-level swarm run record persisted in SQLite."""

    swarm_id: str
    task_hash: str = ""
    created_ts: float = field(default_factory=time.time)
    status: str = "planned"
    requested_agents: int = 0
    effective_agents: int = 0
    progress_counters: dict[str, Any] = field(default_factory=dict)
    cost_summary_ref: str | None = None
    topology: str | None = None
    round: int = 0
    resumable: bool = False
    resume_status: str = "not_resumable"
    parent_swarm_id: str | None = None
    chosen_checkpoint_index: int | None = None


@dataclass
class WorkerSnapshot:
    """Inspect-only worker snapshot persisted for later resume scaffolding."""

    swarm_id: str
    worker_index: int
    snapshot: dict[str, Any] | str = field(default_factory=dict)
    snapshot_ref: str | None = None
    ts: float = field(default_factory=time.time)


@dataclass
class CoordinatorRoundCheckpoint:
    """Durable, compact checkpoint for one completed coordinator round."""

    swarm_id: str
    plan_revision: int
    round_index: int
    coordinator_subtask_id: str
    verdict: str
    amendment: dict[str, Any] = field(default_factory=dict)
    next_work: dict[str, Any] = field(default_factory=dict)
    synthesis_summary: dict[str, Any] = field(default_factory=dict)
    artifact_refs: list[str] = field(default_factory=list)
    artifact_summaries: list[dict[str, object]] = field(default_factory=list)
    round_counters: dict[str, Any] = field(default_factory=dict)
    fallback_reason: str | None = None
    created_ts: float = field(default_factory=time.time)


def _compact_checkpoint_summary(summary: dict[str, Any]) -> dict[str, object]:
    blocked_keys = {"payload", "content", "full_payload", "artifact_payload"}
    compact: dict[str, object] = {}
    for key in (
        "artifact_type",
        "summary_text",
        "length_chars",
        "artifact_ref",
        "producer_subtask_id",
    ):
        if key in blocked_keys or key not in summary:
            continue
        value = summary[key]
        if key == "length_chars":
            try:
                compact[key] = int(value)
            except (TypeError, ValueError):
                pass
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            compact[key] = value
    return compact


def _coerce_checkpoint_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_coordinator_checkpoint_payload(
    swarm_id: str,
    plan_revision: int,
    round_index: int,
    coordinator_subtask_id: str,
    verdict: str,
    *,
    amendment: dict[str, Any] | None = None,
    next_work: dict[str, Any] | None = None,
    synthesis_summary: dict[str, Any] | None = None,
    artifact_refs: list[str] | None = None,
    artifact_summaries: list[dict[str, Any]] | None = None,
    round_counters: dict[str, Any] | None = None,
    fallback_reason: str | None = None,
) -> dict[str, object]:
    """Build a compact, durable coordinator checkpoint payload."""
    return {
        "swarm_id": swarm_id,
        "plan_revision": plan_revision,
        "round_index": round_index,
        "coordinator_subtask_id": coordinator_subtask_id,
        "verdict": verdict,
        "amendment": amendment or {},
        "next_work": next_work or {},
        "synthesis_summary": synthesis_summary or {},
        "artifact_refs": [
            str(ref).strip() for ref in (artifact_refs or []) if str(ref).strip()
        ],
        "artifact_summaries": [
            _compact_checkpoint_summary(summary)
            for summary in (artifact_summaries or [])
            if isinstance(summary, dict)
        ],
        "round_counters": round_counters or {},
        "fallback_reason": fallback_reason,
    }


def persist_coordinator_round_checkpoint(
    checkpoint: CoordinatorRoundCheckpoint,
    *,
    db: Database | None = None,
) -> None:
    database = _get_db(db)
    database.persist_coordinator_round_checkpoint(asdict(checkpoint))


def list_coordinator_round_checkpoints(
    swarm_id: str,
    *,
    plan_revision: int | None = None,
    db: Database | None = None,
) -> list[dict[str, object]]:
    database = _get_db(db)
    return database.list_coordinator_round_checkpoints(
        swarm_id,
        plan_revision=plan_revision,
    )


def get_latest_completed_coordinator_checkpoint(
    swarm_id: str,
    *,
    plan_revision: int | None = None,
    db: Database | None = None,
) -> dict[str, object] | None:
    database = _get_db(db)
    return database.get_latest_completed_coordinator_checkpoint(
        swarm_id,
        plan_revision=plan_revision,
    )


def get_latest_fallback_ready_coordinator_checkpoint(
    swarm_id: str,
    *,
    plan_revision: int | None = None,
    db: Database | None = None,
) -> dict[str, object] | None:
    database = _get_db(db)
    return database.get_latest_fallback_ready_coordinator_checkpoint(
        swarm_id,
        plan_revision=plan_revision,
    )


def persist_swarm_run(run: SwarmRun, *, db: Database | None = None) -> None:
    """Persist or update one swarm run record."""
    database = _get_db(db)
    database.persist_swarm_run(asdict(run))


def persist_worker_snapshot(
    snapshot: WorkerSnapshot,
    *,
    db: Database | None = None,
) -> str:
    """Persist one worker snapshot and return its stable reference."""
    database = _get_db(db)
    return database.persist_worker_snapshot(
        snapshot.swarm_id,
        snapshot.worker_index,
        snapshot.snapshot,
        snapshot.snapshot_ref,
        ts=snapshot.ts,
    )


def get_swarm_summary(
    swarm_id: str,
    *,
    db: Database | None = None,
) -> dict[str, Any] | None:
    """Return the compact operator-facing swarm summary."""
    database = _get_db(db)
    return database.get_swarm_summary(swarm_id)


def build_wave_progress_payload(
    swarm_id: str,
    wave: int,
    completed_subtasks: int,
    pending_subtasks: int,
    artifacts_produced: int,
    round: int = 0,
) -> dict[str, object]:
    """Build the stable per-wave swarm progress payload."""
    return {
        "swarm_id": swarm_id,
        "wave": wave,
        "completed_subtasks": completed_subtasks,
        "pending_subtasks": pending_subtasks,
        "artifacts_produced": artifacts_produced,
        "round": round,
    }


def rebuild_swarm_state(
    swarm_id: str,
    *,
    db: Database | None = None,
) -> dict[str, Any]:
    """Rebuild the compact swarm_state projection from SQLite."""
    database = _get_db(db)
    return database.rebuild_swarm_state_from_db(swarm_id)


def get_coordinator_round_checkpoint_by_index(
    swarm_id: str,
    checkpoint_index: int,
    *,
    plan_revision: int | None = None,
    db: Database | None = None,
) -> dict[str, object] | None:
    """Return one coordinator checkpoint by its round_index (1-based)."""
    database = _get_db(db)
    return database.get_coordinator_round_checkpoint_by_index(
        swarm_id,
        checkpoint_index,
        plan_revision=plan_revision,
    )


def list_resume_checkpoints(
    swarm_id: str,
    *,
    plan_revision: int | None = None,
    db: Database | None = None,
) -> list[dict[str, object]]:
    """Return compact operator-facing checkpoint list for a swarm (newest first).

    Each entry includes: round_index, checkpoint_index, verdict,
    fallback_reason, short_summary, and lineage.
    """
    raw_checkpoints = list_coordinator_round_checkpoints(
        swarm_id,
        plan_revision=plan_revision,
        db=db,
    )
    ordered = sorted(
        raw_checkpoints,
        key=lambda checkpoint: (
            _coerce_checkpoint_int(checkpoint.get("plan_revision")),
            _coerce_checkpoint_int(checkpoint.get("round_index")),
        ),
        reverse=True,
    )
    compact: list[dict[str, object]] = []
    for ckpt in ordered:
        synthesis = ckpt.get("synthesis_summary")
        short_summary = ""
        if isinstance(synthesis, dict):
            short_summary = str(synthesis.get("summary_text") or "").strip()
        elif isinstance(synthesis, str):
            short_summary = synthesis
        round_idx = _coerce_checkpoint_int(ckpt.get("round_index"))
        compact.append({
            "round_index": round_idx,
            "checkpoint_index": round_idx,
            "plan_revision": _coerce_checkpoint_int(ckpt.get("plan_revision")),
            "verdict": ckpt.get("verdict"),
            "fallback_reason": ckpt.get("fallback_reason"),
            "short_summary": short_summary[:140],
            "lineage": {"parent_swarm_id": swarm_id},
        })
    return compact


__all__ = [
    "CoordinatorRoundCheckpoint",
    "SwarmRun",
    "WorkerSnapshot",
    "build_coordinator_checkpoint_payload",
    "build_wave_progress_payload",
    "get_coordinator_round_checkpoint_by_index",
    "get_latest_completed_coordinator_checkpoint",
    "get_latest_fallback_ready_coordinator_checkpoint",
    "list_coordinator_round_checkpoints",
    "list_resume_checkpoints",
    "get_swarm_summary",
    "persist_coordinator_round_checkpoint",
    "persist_swarm_run",
    "persist_worker_snapshot",
    "rebuild_swarm_state",
]
