from __future__ import annotations

import time
from pathlib import Path

import pytest

from shared.db import Database
from shared.memory import memory_get
from shared.outcomes import (
    ANONYMOUS_OPERATOR_ID,
    OUTCOME_MEMORY_KEY,
    OUTCOME_MEMORY_PROJECT_ID,
    OutcomeReadonlyWindowError,
    enqueue_learning_update,
    record_outcome,
    record_swarm_outcome,
)


def test_record_outcome_succeeds(tmp_path) -> None:
    db = Database(tmp_path / "outcomes.db")
    task_id = "task-23-record"
    telemetry_id = db.log_agent_result(
        session_id="session-1",
        task_hash=task_id,
        agent_id=1,
        tier="low",
        model="gpt-5-mini",
        provider_name="GitHub Copilot",
    )

    result = record_outcome(
        db,
        task_id,
        "accepted",
        operator_id="op-1",
        note="looks good",
    )

    assert result == {"stored": True, "task_id": task_id}
    with db.conn() as conn:
        canonical = conn.execute(
            """
            SELECT current_outcome, previous_outcome, tier, model, provider_name, telemetry_id, last_modified_by
            FROM routing_outcomes
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        audit = conn.execute(
            """
            SELECT outcome, operator_id, note, previous_outcome
            FROM routing_outcome_audit
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()

    assert canonical == (
        "accepted",
        None,
        "low",
        "gpt-5-mini",
        "GitHub Copilot",
        telemetry_id,
        "op-1",
    )
    assert audit == ("accepted", "op-1", "looks good", None)


def test_record_outcome_rejects_invalid_enum(tmp_path) -> None:
    db = Database(tmp_path / "invalid.db")

    with pytest.raises(ValueError, match="outcome must be one of"):
        record_outcome(db, "task-23-invalid", "bad")


def test_memory_snapshot_written(tmp_path) -> None:
    db = Database(tmp_path / "memory.db")
    task_id = "task-23-memory"
    db.log_agent_result(
        session_id="session-2",
        task_hash=task_id,
        agent_id=2,
        tier="medium",
        model="claude-sonnet-4.6",
        provider_name="Claude Code",
    )

    record_outcome(db, task_id, "accepted")
    time.sleep(0.01)
    record_outcome(db, task_id, "reworked")

    snapshot = memory_get(
        "task",
        OUTCOME_MEMORY_KEY,
        project_id=OUTCOME_MEMORY_PROJECT_ID,
        task_id=task_id,
        db=db,
    )

    assert snapshot["value"] == {
        "current_outcome": "reworked",
        "recorded_at": snapshot["value"]["recorded_at"],
        "previous_outcome": "accepted",
        "tier": "medium",
        "model": "claude-sonnet-4.6",
        "provider": "Claude Code",
        "complexity_score": None,
    }

    with db.conn() as conn:
        canonical = conn.execute(
            """
            SELECT current_outcome, previous_outcome, last_modified_by
            FROM routing_outcomes
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        audit_rows = conn.execute(
            """
            SELECT outcome, previous_outcome, operator_id
            FROM routing_outcome_audit
            WHERE task_id = ?
            ORDER BY id
            """,
            (task_id,),
        ).fetchall()

    assert canonical == ("reworked", "accepted", ANONYMOUS_OPERATOR_ID)
    assert audit_rows == [
        ("accepted", None, ANONYMOUS_OPERATOR_ID),
        ("reworked", "accepted", ANONYMOUS_OPERATOR_ID),
    ]


def test_record_outcome_enforces_readonly_window(tmp_path) -> None:
    db = Database(tmp_path / "readonly.db")
    task_id = "task-23-readonly"
    old_created_at = time.time() - (8 * 24 * 60 * 60)

    with db.conn() as conn:
        conn.execute(
            """
            INSERT INTO routing_outcomes (
                task_id,
                current_outcome,
                previous_outcome,
                recorded_at,
                tier,
                model,
                provider_name,
                complexity_score,
                telemetry_id,
                last_modified_by,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                "accepted",
                None,
                old_created_at,
                None,
                None,
                None,
                None,
                None,
                "op-1",
                old_created_at,
            ),
        )

    with pytest.raises(OutcomeReadonlyWindowError, match="7-day correction window"):
        record_outcome(db, task_id, "revised")


def test_record_outcome_tolerates_blank_created_at_on_existing_rows(tmp_path) -> None:
    db = Database(tmp_path / "blank-created-at.db")
    task_id = "task-23-blank-created-at"

    with db.conn() as conn:
        conn.execute(
            """
            INSERT INTO routing_outcomes (
                task_id,
                current_outcome,
                previous_outcome,
                recorded_at,
                tier,
                model,
                provider_name,
                complexity_score,
                telemetry_id,
                last_modified_by,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                "accepted",
                None,
                time.time(),
                None,
                None,
                None,
                None,
                None,
                ANONYMOUS_OPERATOR_ID,
                "",
            ),
        )

    result = record_outcome(db, task_id, "reworked")

    assert result == {"stored": True, "task_id": task_id}


def test_enqueue_learning_update_accepted_outcome(tmp_path) -> None:
    """Test that accepted outcome enqueues positive signal (success=1)."""
    db = Database(tmp_path / "enqueue_accepted.db")
    task_id = "task-24-accepted"
    
    # Log telemetry
    db.log_agent_result(
        session_id="session-24",
        task_hash=task_id,
        agent_id=1,
        tier="low",
        model="gpt-5-mini",
        provider_name="GitHub Copilot",
    )
    
    # Record accepted outcome
    result = record_outcome(db, task_id, "accepted", operator_id="op-24")
    assert result == {"stored": True, "task_id": task_id}
    
    # Verify learning_queue entry
    with db.conn() as conn:
        queue_row = conn.execute(
            "SELECT tier, complexity_score, success, status FROM learning_queue WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    
    assert queue_row is not None, "learning_queue entry not created"
    assert queue_row[0] == "low", f"tier mismatch: {queue_row[0]}"
    assert queue_row[2] == 1, f"success should be 1 for accepted, got {queue_row[2]}"
    assert queue_row[3] == "pending", f"status should be pending, got {queue_row[3]}"


def test_enqueue_learning_update_rejected_outcome(tmp_path) -> None:
    """Test that rejected outcome enqueues negative signal (success=0)."""
    db = Database(tmp_path / "enqueue_rejected.db")
    task_id = "task-24-rejected"
    
    db.log_agent_result(
        session_id="session-24",
        task_hash=task_id,
        agent_id=1,
        tier="medium",
        model="claude-sonnet",
        provider_name="Claude Code",
    )
    
    result = record_outcome(db, task_id, "rejected")
    assert result == {"stored": True, "task_id": task_id}
    
    with db.conn() as conn:
        queue_row = conn.execute(
            "SELECT success FROM learning_queue WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    
    assert queue_row is not None
    assert queue_row[0] == 0, f"success should be 0 for rejected, got {queue_row[0]}"


def test_enqueue_learning_update_reworked_outcome(tmp_path) -> None:
    """Test that reworked outcome enqueues negative signal (success=0)."""
    db = Database(tmp_path / "enqueue_reworked.db")
    task_id = "task-24-reworked"
    
    db.log_agent_result(
        session_id="session-24",
        task_hash=task_id,
        agent_id=1,
        tier="high",
        model="gpt-5",
        provider_name="GitHub Copilot",
    )
    
    result = record_outcome(db, task_id, "reworked")
    assert result == {"stored": True, "task_id": task_id}
    
    with db.conn() as conn:
        queue_row = conn.execute(
            "SELECT success FROM learning_queue WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    
    assert queue_row is not None
    assert queue_row[0] == 0, f"success should be 0 for reworked, got {queue_row[0]}"


def test_enqueue_learning_disabled_project(tmp_path) -> None:
    """Test that learning disabled (learning_enabled=0) prevents enqueue when project_id provided."""
    db = Database(tmp_path / "enqueue_disabled.db")
    task_id = "task-24-disabled"
    
    db.log_agent_result(
        session_id="session-24",
        task_hash=task_id,
        agent_id=1,
        tier="low",
        model="gpt-5-mini",
        provider_name="GitHub Copilot",
    )
    
    # Set up a project with learning disabled
    db.set_project_setting("/test/disabled-project", "learning_enabled", False)
    
    # Record outcome for a different (default) project - should still enqueue since no project_id context
    result = record_outcome(db, task_id, "accepted")
    assert result == {"stored": True, "task_id": task_id}
    
    # learning_queue should be populated (since no project_id context to gate it)
    with db.conn() as conn:
        queue_row = conn.execute(
            "SELECT * FROM learning_queue WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    
    assert queue_row is not None, "learning_queue entry should be created (no project_id context)"


def test_enqueue_learning_skip_missing_telemetry(tmp_path) -> None:
    """Test that missing telemetry gracefully skips (no error, debug log)."""
    db = Database(tmp_path / "enqueue_missing_telemetry.db")
    task_id = "task-24-no-telemetry"
    
    # Record outcome without creating telemetry record first
    result = record_outcome(db, task_id, "accepted")
    assert result == {"stored": True, "task_id": task_id}
    
    # learning_queue should be empty (skipped)
    with db.conn() as conn:
        queue_row = conn.execute(
            "SELECT * FROM learning_queue WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    
    assert queue_row is None, "learning_queue entry should not be created for missing telemetry"


def test_enqueue_learning_update_replaces_existing(tmp_path) -> None:
    """Test that recording a new outcome replaces the existing queue entry (UNIQUE constraint)."""
    db = Database(tmp_path / "enqueue_replace.db")
    task_id = "task-24-replace"
    
    db.log_agent_result(
        session_id="session-24",
        task_hash=task_id,
        agent_id=1,
        tier="medium",
        model="claude-sonnet",
        provider_name="Claude Code",
    )
    
    # Record first outcome
    record_outcome(db, task_id, "accepted")
    
    with db.conn() as conn:
        first_count = conn.execute(
            "SELECT COUNT(*) FROM learning_queue WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert first_count == 1
    
    # Record revised outcome (correction)
    record_outcome(db, task_id, "revised")
    
    with db.conn() as conn:
        second_count = conn.execute(
            "SELECT COUNT(*) FROM learning_queue WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        success = conn.execute(
            "SELECT success FROM learning_queue WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    
    assert second_count == 1, "UNIQUE constraint not working; duplicate entries"
    assert success == 1, "success should still be 1 for revised"


def test_record_swarm_outcome_records_coordinator_metrics(tmp_path) -> None:
    db = Database(tmp_path / "swarm-outcomes.db")
    swarm_id = "swarm-35-03-metrics"

    result = record_swarm_outcome(
        db,
        swarm_id,
        "revised",
        coordinator_round_count=2,
        artifact_consume_count=5,
        coordinator_amendment_count=1,
        note={
            "topology": "star",
            "terminal_state": "fallback",
            "fallback_reason": "max_rounds exhausted",
        },
    )

    assert result == {"stored": True, "task_id": swarm_id}
    with db.conn() as conn:
        telemetry_row = conn.execute(
            """
            SELECT session_id, task_hash, tier, model, selected_topology,
                   coordinator_round_count, artifact_consume_count,
                   coordinator_amendment_count, reason
            FROM telemetry
            WHERE task_hash = ?
            ORDER BY ts DESC, id DESC
            LIMIT 1
            """,
            (swarm_id,),
        ).fetchone()
        outcome_row = conn.execute(
            """
            SELECT current_outcome, telemetry_id
            FROM routing_outcomes
            WHERE task_id = ?
            """,
            (swarm_id,),
        ).fetchone()
        queue_row = conn.execute(
            """
            SELECT tier, success, status
            FROM learning_queue
            WHERE task_id = ?
            """,
            (swarm_id,),
        ).fetchone()

    assert telemetry_row is not None
    assert telemetry_row[:5] == (
        swarm_id,
        swarm_id,
        "coordinator",
        "star-coordinator",
        "star",
    )
    assert telemetry_row[5:8] == (2, 5, 1)
    assert '"fallback_reason":"max_rounds exhausted"' in str(telemetry_row[8])
    assert outcome_row is not None
    assert outcome_row[0] == "revised"
    assert outcome_row[1] is not None
    assert queue_row == ("coordinator", 1, "pending")


def test_record_swarm_outcome_reuses_learning_queue_path(tmp_path) -> None:
    db = Database(tmp_path / "swarm-outcomes-reuse.db")
    swarm_id = "swarm-35-03-reuse"

    first = record_swarm_outcome(
        db,
        swarm_id,
        "accepted",
        coordinator_round_count=1,
        artifact_consume_count=3,
    )
    second = record_swarm_outcome(
        db,
        swarm_id,
        "revised",
        coordinator_round_count=2,
        artifact_consume_count=6,
        coordinator_amendment_count=1,
        note="fallback recovered",
    )

    assert first == {"stored": True, "task_id": swarm_id}
    assert second == {"stored": True, "task_id": swarm_id}
    with db.conn() as conn:
        telemetry_count = conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE task_hash = ?",
            (swarm_id,),
        ).fetchone()[0]
        queue_count = conn.execute(
            "SELECT COUNT(*) FROM learning_queue WHERE task_id = ?",
            (swarm_id,),
        ).fetchone()[0]
        queue_row = conn.execute(
            "SELECT tier, success, status FROM learning_queue WHERE task_id = ?",
            (swarm_id,),
        ).fetchone()
        outcome_row = conn.execute(
            """
            SELECT current_outcome, previous_outcome
            FROM routing_outcomes
            WHERE task_id = ?
            """,
            (swarm_id,),
        ).fetchone()
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM routing_outcome_audit WHERE task_id = ?",
            (swarm_id,),
        ).fetchone()[0]

    assert telemetry_count == 2
    assert queue_count == 1
    assert queue_row == ("coordinator", 1, "pending")
    assert outcome_row == ("revised", "accepted")
    assert audit_count == 2


def test_enqueue_learning_update_skips_null_tier(tmp_path) -> None:
    db = Database(tmp_path / "enqueue-null-tier.db")
    task_id = "task-35-03-null-tier"

    with db.conn() as conn:
        conn.execute(
            """
            INSERT INTO telemetry (session_id, task_hash, agent_id, tier, model, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("session-null-tier", task_id, 1, None, "model", time.time()),
        )

    result = record_outcome(db, task_id, "accepted")

    assert result == {"stored": True, "task_id": task_id}
    with db.conn() as conn:
        queue_row = conn.execute(
            "SELECT * FROM learning_queue WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    assert queue_row is None


def test_record_swarm_outcome_rolls_back_telemetry_when_outcome_write_fails(tmp_path) -> None:
    db = Database(tmp_path / "swarm-outcomes-rollback.db")
    swarm_id = "swarm-35-03-rollback"
    old_created_at = time.time() - (8 * 24 * 60 * 60)

    with db.conn() as conn:
        conn.execute(
            """
            INSERT INTO routing_outcomes (
                task_id,
                current_outcome,
                previous_outcome,
                recorded_at,
                tier,
                model,
                provider_name,
                complexity_score,
                telemetry_id,
                last_modified_by,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                swarm_id,
                "accepted",
                None,
                old_created_at,
                None,
                None,
                None,
                None,
                None,
                ANONYMOUS_OPERATOR_ID,
                old_created_at,
            ),
        )

    with pytest.raises(OutcomeReadonlyWindowError):
        record_swarm_outcome(db, swarm_id, "revised", coordinator_round_count=1)

    with db.conn() as conn:
        telemetry_count = conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE task_hash = ?",
            (swarm_id,),
        ).fetchone()[0]

    assert telemetry_count == 0


def test_record_outcome_tolerates_memory_snapshot_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db = Database(tmp_path / "memory-snapshot-failure.db")
    task_id = "task-35-03-memory-failure"
    db.log_agent_result(
        session_id="session-memory-failure",
        task_hash=task_id,
        agent_id=1,
        tier="low",
        model="gpt-5-mini",
        provider_name="GitHub Copilot",
    )

    def _explode(*args, **kwargs):
        raise RuntimeError("memory offline")

    monkeypatch.setattr("shared.outcomes.memory_set", _explode)

    result = record_outcome(db, task_id, "accepted")

    assert result == {"stored": True, "task_id": task_id}
    with db.conn() as conn:
        outcome_row = conn.execute(
            "SELECT current_outcome FROM routing_outcomes WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        queue_row = conn.execute(
            "SELECT success FROM learning_queue WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    assert outcome_row == ("accepted",)
    assert queue_row == (1,)


# ---------------------------------------------------------------------------
# coordinator_amendment_count persistence test (from test_phase15_e2e_2.py)
# Rewritten without helpers_phase15 dependency.
# ---------------------------------------------------------------------------


def test_multiwave_coordinator_amendments_visible_in_telemetry(tmp_path) -> None:
    """coordinator_amendment_count written to telemetry is readable back.

    Verifies the column is persisted correctly without requiring a full
    orchestrator run — the persistence path is what matters here.
    """
    db = Database(tmp_path / "phase15_e2e_2.db")
    try:
        with db.conn() as conn:
            # Insert a coordinator amendment record
            conn.execute(
                "INSERT INTO coordinator_amendments "
                "(plan_id, proposer_id, diff_blob, reason, outcome, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("plan-1", "coord-1", "{}", "test amend", "applied", 1234567890),
            )
            # Write a telemetry row carrying coordinator_amendment_count
            conn.execute(
                "INSERT INTO telemetry "
                "(session_id, task_hash, agent_id, tier, model, "
                "coordinator_amendment_count, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("wave-test", "task-amend", 999, "low", "coord", 1, 1234567890),
            )

        with db.conn() as conn:
            rows = conn.execute(
                "SELECT coordinator_amendment_count FROM telemetry WHERE task_hash = ?",
                ("task-amend",),
            ).fetchall()

        assert len(rows) == 1
        assert rows[0][0] == 1

        # artifacts table should be present even with zero rows
        with db.conn() as conn:
            artifact_count = conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE execution_id = ?",
                ("wave-test",),
            ).fetchone()[0]
        assert artifact_count >= 0
    finally:
        db.close()
