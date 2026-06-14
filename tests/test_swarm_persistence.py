#!/usr/bin/env python3
"""Tests for Phase 31 swarm persistence scaffolding."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.db import Database, SWARM_SCHEMA_VERSION
from shared.memory import memory_refresh_swarm_state_from_db



def test_schema_init_runs() -> None:
    """Database initialization should create the new swarm tables and schema marker."""
    with tempfile.NamedTemporaryFile(suffix=".db") as handle:
        db = Database(Path(handle.name))
        with db.conn() as conn:
            table_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            schema_versions = {
                row[0]
                for row in conn.execute(
                    "SELECT schema_version FROM swarm_schema"
                ).fetchall()
            }
        assert {
            "swarm_schema",
            "swarm_runs",
            "swarm_workers",
            "swarm_events",
            "coordinator_round_checkpoints",
        } <= table_names
        assert SWARM_SCHEMA_VERSION in schema_versions
        db.close()


def test_persist_and_query_swarm() -> None:
    """Swarm records should round-trip through SQLite and rebuild compact state."""
    from shared.swarm import (
        SwarmRun,
        WorkerSnapshot,
        get_swarm_summary,
        persist_swarm_run,
        persist_worker_snapshot,
    )

    with tempfile.NamedTemporaryFile(suffix=".db") as handle:
        db = Database(Path(handle.name))
        swarm_id = "swarm-phase31"
        persist_swarm_run(
            SwarmRun(
                swarm_id=swarm_id,
                task_hash="task-hash",
                status="running",
                requested_agents=8,
                effective_agents=6,
                progress_counters={"completed": 1, "pending": 2},
                topology="dag",
                round=1,
            ),
            db=db,
        )
        snapshot_ref = persist_worker_snapshot(
            WorkerSnapshot(
                swarm_id=swarm_id,
                worker_index=0,
                snapshot={
                    "status": "complete",
                    "summary": "worker finished",
                    "artifact_ref": "artifact:123",
                    "payload": "large output omitted",
                },
            ),
            db=db,
        )
        persist_worker_snapshot(
            WorkerSnapshot(
                swarm_id=swarm_id,
                worker_index=0,
                snapshot={
                    "status": "complete",
                    "summary": "worker finished latest",
                    "payload": "newer payload omitted",
                },
            ),
            db=db,
        )

        summary = get_swarm_summary(swarm_id, db=db)
        assert summary is not None
        assert summary["requested_agents"] == 8
        assert summary["effective_agents"] == 6
        assert summary["worker_snapshot_count"] == 1
        assert summary["resume_status"] == "not_resumable"

        rebuilt = memory_refresh_swarm_state_from_db(swarm_id, db=db)
        assert rebuilt["value"]["swarm_id"] == swarm_id
        assert rebuilt["scope"] == "project"
        assert rebuilt["project_id"] is not None
        workers = rebuilt["value"]["workers"]
        assert len(workers) == 1
        assert workers[0]["snapshot_ref"] != ""
        assert workers[0]["snapshot_summary"]["summary"] == "worker finished latest"
        assert "payload" not in workers[0]["snapshot_summary"]
        db.close()


def test_invalid_swarm_numeric_inputs_raise_clear_errors() -> None:
    """Malformed numeric swarm fields should raise explicit ValueError messages."""
    from shared.swarm import SwarmRun, persist_swarm_run

    with tempfile.NamedTemporaryFile(suffix=".db") as handle:
        db = Database(Path(handle.name))
        try:
            persist_swarm_run(
                SwarmRun(
                    swarm_id="swarm-invalid",
                    requested_agents=1,
                    effective_agents=1,
                ),
                db=db,
            )
            try:
                db.persist_worker_snapshot("swarm-invalid", "oops", {"summary": "bad"})
            except ValueError as exc:
                assert str(exc) == "worker_index must be an integer"
            else:
                raise AssertionError("Expected worker_index validation failure")

            try:
                db.persist_swarm_run(
                    {
                        "swarm_id": "swarm-invalid-ts",
                        "created_ts": "now",
                        "status": "planned",
                        "requested_agents": 1,
                        "effective_agents": 1,
                        "round": 0,
                    }
                )
            except ValueError as exc:
                assert str(exc) == "created_ts must be a number"
            else:
                raise AssertionError("Expected created_ts validation failure")
        finally:
            db.close()


def test_persist_and_load_completed_coordinator_checkpoint() -> None:
    from shared.swarm import (
        CoordinatorRoundCheckpoint,
        get_latest_completed_coordinator_checkpoint,
        list_coordinator_round_checkpoints,
        persist_coordinator_round_checkpoint,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "checkpoint.db")
        try:
            persist_coordinator_round_checkpoint(
                CoordinatorRoundCheckpoint(
                    swarm_id="swarm-35",
                    plan_revision=2,
                    round_index=1,
                    coordinator_subtask_id="phase35-plan02-task03",
                    verdict="another-pass",
                    amendment={"subtask_updates": [{"id": 7, "description": "retry"}]},
                    next_work={"rerun_subtasks": ["phase35-plan02-task07"]},
                    synthesis_summary={"summary_text": "Need another pass"},
                    artifact_refs=["artifact:1", "artifact:2"],
                    artifact_summaries=[
                        {
                            "artifact_type": "result",
                            "summary_text": "worker output",
                            "length_chars": 12,
                            "artifact_ref": "artifact:1",
                            "producer_subtask_id": "worker-1",
                        }
                    ],
                    round_counters={"round": 1, "artifacts_consumed": 2},
                ),
                db=db,
            )

            rows = list_coordinator_round_checkpoints(
                "swarm-35",
                plan_revision=2,
                db=db,
            )
            latest = get_latest_completed_coordinator_checkpoint(
                "swarm-35",
                plan_revision=2,
                db=db,
            )

            assert len(rows) == 1
            assert rows[0]["verdict"] == "another-pass"
            assert rows[0]["artifact_refs"] == ["artifact:1", "artifact:2"]
            assert rows[0]["artifact_summaries"][0]["artifact_ref"] == "artifact:1"
            assert latest is not None
            assert latest["round_index"] == 1
            assert latest["round_counters"]["artifacts_consumed"] == 2
        finally:
            db.close()


def test_checkpoint_payload_compacts_worker_summaries() -> None:
    from shared.swarm import (
        CoordinatorRoundCheckpoint,
        build_coordinator_checkpoint_payload,
        list_coordinator_round_checkpoints,
        persist_coordinator_round_checkpoint,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "checkpoint-compact.db")
        try:
            payload = build_coordinator_checkpoint_payload(
                "swarm-35",
                2,
                1,
                "phase35-plan02-task03",
                "fallback",
                synthesis_summary={"summary_text": "fallback to linear"},
                artifact_refs=["artifact:1"],
                artifact_summaries=[
                    {
                        "artifact_type": "result",
                        "summary_text": "worker output",
                        "length_chars": 12,
                        "artifact_ref": "artifact:1",
                        "producer_subtask_id": "worker-1",
                        "payload": "secret",
                        "content": "secret",
                        "full_payload": "secret",
                        "artifact_payload": "secret",
                    }
                ],
                round_counters={"round": 1},
                fallback_reason="bad verdict",
            )
            persist_coordinator_round_checkpoint(
                CoordinatorRoundCheckpoint(**payload),
                db=db,
            )

            stored = list_coordinator_round_checkpoints("swarm-35", db=db)[0]
            summary = stored["artifact_summaries"][0]

            assert "payload" not in summary
            assert "content" not in summary
            assert "full_payload" not in summary
            assert "artifact_payload" not in summary
            assert summary["summary_text"] == "worker output"
        finally:
            db.close()


def test_latest_completed_checkpoint_excludes_incomplete_rounds() -> None:
    from shared.swarm import (
        CoordinatorRoundCheckpoint,
        get_latest_completed_coordinator_checkpoint,
        persist_coordinator_round_checkpoint,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "checkpoint-latest.db")
        try:
            persist_coordinator_round_checkpoint(
                CoordinatorRoundCheckpoint(
                    swarm_id="swarm-35",
                    plan_revision=2,
                    round_index=1,
                    coordinator_subtask_id="phase35-plan02-task03",
                    verdict="complete",
                    synthesis_summary={"summary_text": "done"},
                    artifact_refs=["artifact:1"],
                    artifact_summaries=[],
                    round_counters={"round": 1},
                ),
                db=db,
            )
            with db.conn() as conn:
                conn.execute(
                    """
                    INSERT INTO coordinator_round_checkpoints (
                        swarm_id,
                        plan_revision,
                        round_index,
                        coordinator_subtask_id,
                        verdict,
                        artifact_refs_json,
                        artifact_summaries_json,
                        round_counters_json,
                        created_ts
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "swarm-35",
                        2,
                        2,
                        "phase35-plan02-task03",
                        "",
                        "[]",
                        "[]",
                        "{}",
                        0.0,
                    ),
                )

            latest = get_latest_completed_coordinator_checkpoint(
                "swarm-35",
                plan_revision=2,
                db=db,
            )

            assert latest is not None
            assert latest["round_index"] == 1
            assert latest["verdict"] == "complete"
        finally:
            db.close()
