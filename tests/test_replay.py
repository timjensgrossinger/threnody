"""Tests for plan 13 — trace replay (show/replay/diff)."""
from __future__ import annotations

import json
import time
import uuid

import pytest

from shared.db import Database
from shared.replay import ReplayEngine, CheckpointEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture()
def engine(db):
    return ReplayEngine(db)


def _make_swarm(db, swarm_id: str | None = None, parent: str | None = None) -> str:
    sid = swarm_id or str(uuid.uuid4())
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO swarm_runs"
            " (swarm_id, task_hash, created_ts, status,"
            "  requested_agents, effective_agents,"
            "  progress_counters, topology, round, resumable, resume_status,"
            "  parent_swarm_id)"
            " VALUES (?, ?, ?, 'completed', 2, 2, '{}', 'linear', 3, 0,"
            "         'not_resumable', ?)",
            (sid, "hash-abc", time.time(), parent),
        )
    return sid


def _add_checkpoint(
    db,
    swarm_id: str,
    round_index: int,
    verdict: str = "continue",
    next_work: list | None = None,
) -> int:
    with db.conn() as conn:
        cursor = conn.execute(
            "INSERT INTO coordinator_round_checkpoints"
            " (swarm_id, plan_revision, round_index, coordinator_subtask_id,"
            "  verdict, next_work_json, created_ts)"
            " VALUES (?, 1, ?, ?, ?, ?, ?)",
            (
                swarm_id, round_index, f"coord-{round_index}",
                verdict,
                json.dumps(next_work or []),
                time.time(),
            ),
        )
        return cursor.lastrowid or 0


# ---------------------------------------------------------------------------
# show_checkpoints
# ---------------------------------------------------------------------------

def test_show_checkpoints_empty_for_unknown_run(engine):
    cps = engine.show_checkpoints("nonexistent")
    assert cps == []


def test_show_checkpoints_returns_entries(db, engine):
    sid = _make_swarm(db)
    _add_checkpoint(db, sid, 0, verdict="continue")
    _add_checkpoint(db, sid, 1, verdict="complete")
    cps = engine.show_checkpoints(sid)
    assert len(cps) == 2
    assert all(isinstance(c, CheckpointEntry) for c in cps)
    rounds = [c.round_index for c in cps]
    assert rounds == sorted(rounds)


def test_show_run_returns_metadata(db, engine):
    sid = _make_swarm(db)
    _add_checkpoint(db, sid, 0)
    result = engine.show_run(sid)
    assert result["run_id"] == sid
    assert result["status"] == "completed"
    assert len(result["checkpoints"]) == 1


def test_show_run_not_found_returns_error(engine):
    result = engine.show_run("no-such-run")
    assert "error" in result


def test_show_run_includes_parent(db, engine):
    parent_id = _make_swarm(db)
    child_id = _make_swarm(db, parent=parent_id)
    result = engine.show_run(child_id)
    assert result["parent_swarm_id"] == parent_id


# ---------------------------------------------------------------------------
# plan_replay
# ---------------------------------------------------------------------------

def test_plan_replay_no_checkpoints(engine):
    plan = engine.plan_replay("nonexistent", dry_run=True)
    assert plan.source_run_id == "nonexistent"
    assert plan.subtasks_to_replay == []


def test_plan_replay_classifies_by_op_class(db, engine):
    sid = _make_swarm(db)
    _add_checkpoint(db, sid, 0, verdict="continue")
    _add_checkpoint(db, sid, 1, next_work=[
        {"id": 1, "description": "read file", "op_class": "replayable"},
        {"id": 2, "description": "write file", "op_class": "side_effecting"},
        {"id": 3, "description": "deploy", "op_class": "approval_required"},
    ])
    plan = engine.plan_replay(sid, dry_run=True)
    assert len(plan.subtasks_to_replay) == 1
    assert len(plan.subtasks_to_skip) == 1
    assert len(plan.approval_gates) == 1


def test_plan_replay_from_specific_checkpoint(db, engine):
    sid = _make_swarm(db)
    cp0 = _add_checkpoint(db, sid, 0, verdict="continue")
    _add_checkpoint(db, sid, 1, next_work=[
        {"id": 1, "op_class": "replayable"},
    ])
    _add_checkpoint(db, sid, 2, next_work=[
        {"id": 2, "op_class": "side_effecting"},
    ])
    plan = engine.plan_replay(sid, from_checkpoint_id=cp0, dry_run=True)
    # should include both subsequent checkpoints
    assert len(plan.subtasks_to_replay) + len(plan.subtasks_to_skip) == 2


# ---------------------------------------------------------------------------
# execute_replay
# ---------------------------------------------------------------------------

def test_execute_replay_returns_planned_status(db, engine):
    sid = _make_swarm(db)
    _add_checkpoint(db, sid, 0)
    result = engine.execute_replay(sid)
    assert result["status"] == "planned"


def test_execute_replay_halts_on_approval_gate(db, engine):
    sid = _make_swarm(db)
    _add_checkpoint(db, sid, 0)
    _add_checkpoint(db, sid, 1, next_work=[
        {"id": 1, "op_class": "approval_required"},
    ])
    result = engine.execute_replay(sid)
    assert result["status"] == "halted"
    assert "approval_gates" in result


# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------

def test_fork_dry_run_does_not_write(db, engine):
    sid = _make_swarm(db)
    _add_checkpoint(db, sid, 0)
    result = engine.fork(sid, dry_run=True)
    assert result["status"] == "dry_run"
    fork_id = result["fork_run_id"]
    assert fork_id
    # Should NOT exist in DB
    fork_result = engine.show_run(fork_id)
    assert "error" in fork_result


def test_fork_live_creates_db_row(db, engine):
    sid = _make_swarm(db)
    _add_checkpoint(db, sid, 0)
    result = engine.fork(sid, dry_run=False)
    assert result["status"] == "forked"
    fork_id = result["fork_run_id"]
    fork_result = engine.show_run(fork_id)
    assert fork_result["run_id"] == fork_id
    assert fork_result["parent_swarm_id"] == sid


def test_fork_with_overrides_captures_in_plan(db, engine):
    sid = _make_swarm(db)
    _add_checkpoint(db, sid, 0)
    result = engine.fork(sid, overrides={"tier": "high"}, dry_run=True)
    assert result["plan"]["overrides"] == {"tier": "high"}


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def test_diff_identical_runs(db, engine):
    sid_a = _make_swarm(db)
    sid_b = _make_swarm(db)
    _add_checkpoint(db, sid_a, 0, verdict="continue")
    _add_checkpoint(db, sid_a, 1, verdict="complete")
    _add_checkpoint(db, sid_b, 0, verdict="continue")
    _add_checkpoint(db, sid_b, 1, verdict="complete")
    result = engine.diff(sid_a, sid_b)
    assert result["identical"] is True
    assert result["diffs"] == []


def test_diff_detects_divergence(db, engine):
    sid_a = _make_swarm(db)
    sid_b = _make_swarm(db)
    _add_checkpoint(db, sid_a, 0, verdict="continue")
    _add_checkpoint(db, sid_a, 1, verdict="complete")
    _add_checkpoint(db, sid_b, 0, verdict="continue")
    _add_checkpoint(db, sid_b, 1, verdict="amendment")
    result = engine.diff(sid_a, sid_b)
    assert result["identical"] is False
    assert result["diverge_at_round"] == 1
    assert len(result["diffs"]) == 1


def test_diff_handles_mismatched_checkpoint_counts(db, engine):
    sid_a = _make_swarm(db)
    sid_b = _make_swarm(db)
    _add_checkpoint(db, sid_a, 0, verdict="continue")
    _add_checkpoint(db, sid_a, 1, verdict="complete")
    _add_checkpoint(db, sid_b, 0, verdict="continue")
    # sid_b has no round 1
    result = engine.diff(sid_a, sid_b)
    assert result["identical"] is False
    assert result["total_rounds"] == 2


def test_diff_two_empty_runs(db, engine):
    sid_a = _make_swarm(db)
    sid_b = _make_swarm(db)
    result = engine.diff(sid_a, sid_b)
    assert result["identical"] is True
    assert result["total_rounds"] == 0


# ---------------------------------------------------------------------------
# Fork lineage (from test_fork.py)
# ---------------------------------------------------------------------------

def test_fork_sets_parent_swarm_id(db, engine):
    parent_id = _make_swarm(db)
    _add_checkpoint(db, parent_id, 0)
    result = engine.fork(parent_id, dry_run=False)
    fork_id = result["fork_run_id"]
    fork_info = engine.show_run(fork_id)
    assert fork_info["parent_swarm_id"] == parent_id


def test_fork_parent_remains_intact(db, engine):
    parent_id = _make_swarm(db)
    _add_checkpoint(db, parent_id, 0)
    _add_checkpoint(db, parent_id, 1, verdict="complete")
    engine.fork(parent_id, dry_run=False)
    parent_info = engine.show_run(parent_id)
    assert parent_info["status"] == "completed"
    assert len(parent_info["checkpoints"]) == 2


def test_fork_returns_unique_run_id_each_time(db, engine):
    parent_id = _make_swarm(db)
    _add_checkpoint(db, parent_id, 0)
    r1 = engine.fork(parent_id, dry_run=False)
    r2 = engine.fork(parent_id, dry_run=False)
    assert r1["fork_run_id"] != r2["fork_run_id"]


def test_fork_inherits_from_checkpoint(db, engine):
    parent_id = _make_swarm(db)
    cp0 = _add_checkpoint(db, parent_id, 0, verdict="continue")
    _add_checkpoint(db, parent_id, 1, verdict="complete")
    result = engine.fork(parent_id, from_checkpoint_id=cp0, dry_run=True)
    assert result["plan"]["from_checkpoint_id"] == cp0
    assert result["plan"]["from_round_index"] == 0


def test_multi_gen_fork_chain(db, engine):
    grandparent_id = _make_swarm(db)
    _add_checkpoint(db, grandparent_id, 0)
    parent_result = engine.fork(grandparent_id, dry_run=False)
    parent_id = parent_result["fork_run_id"]
    _add_checkpoint(db, parent_id, 0)
    child_result = engine.fork(parent_id, dry_run=False)
    child_id = child_result["fork_run_id"]

    child_info = engine.show_run(child_id)
    parent_info = engine.show_run(parent_id)
    assert child_info["parent_swarm_id"] == parent_id
    assert parent_info["parent_swarm_id"] == grandparent_id
