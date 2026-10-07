"""Routing-guard lifecycle and swarm-run supersede.

Covers the stale-guard / orphan-run defects: a routed_plan guard that lost to an
unrelated earlier ``direct`` guard (and the old row returned as the new guard),
guards that were never cleared, an active-handoff fallback that was not scoped
to a workspace, and a re-issued execute_swarm that left the first handoff
``awaiting_host_execution`` until the 24 h reaper.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server
from shared import eval as shared_eval
from shared import run_log
from shared import status as shared_status
from shared.config import TGsConfig
from shared.db import (
    ROUTING_GUARD_MODE_DIRECT,
    ROUTING_GUARD_MODE_EXECUTE_SUBTASK,
    ROUTING_GUARD_MODE_ROUTED_PLAN,
    ROUTING_GUARD_TTL_SECONDS,
    Database,
)


def _db(tmp_path: Path) -> tuple[TGsConfig, Database]:
    db_path = tmp_path / "lifecycle.db"
    return TGsConfig(db_path=db_path), Database(db_path=db_path)


def _put(db: Database, cwd: str, mode: str, task: str, task_id: str | None, **kw: object) -> dict:
    return db.routing_guard_put(
        caller="claude-code",
        cwd=cwd,
        mode=mode,
        source_tool=str(kw.pop("source_tool", "route_task")),
        task_text=task,
        task_id=task_id,
        **kw,
    )


# ---------------------------------------------------------------------------
# Rank rule
# ---------------------------------------------------------------------------


def test_routed_plan_replaces_direct_guard_of_an_earlier_task(tmp_path: Path) -> None:
    _cfg, db = _db(tmp_path)
    cwd = str(tmp_path)
    _put(db, cwd, ROUTING_GUARD_MODE_DIRECT, "Add one pin string to test_install_skills.py", "route-old")

    written = _put(
        db, cwd, ROUTING_GUARD_MODE_ROUTED_PLAN, "swarm task", "swarm-new", source_tool="execute_swarm"
    )

    assert "skipped" not in written
    assert written["task_id"] == "swarm-new"
    stored = db.routing_guard_get(caller="claude-code", cwd=cwd)
    assert stored is not None
    assert stored["mode"] == ROUTING_GUARD_MODE_ROUTED_PLAN
    assert stored["task_id"] == "swarm-new"


def test_direct_does_not_clobber_live_routed_plan(tmp_path: Path) -> None:
    _cfg, db = _db(tmp_path)
    cwd = str(tmp_path)
    _put(db, cwd, ROUTING_GUARD_MODE_ROUTED_PLAN, "plan", "plan-1", source_tool="plan_task")

    skipped = _put(db, cwd, ROUTING_GUARD_MODE_DIRECT, "side task", "route-2")

    assert skipped["skipped"] is True
    assert skipped["reason"] == "active_routed_plan"
    # The skip record must not look like a guard: no top-level guard fields.
    assert "mode" not in skipped and "task_text" not in skipped
    assert skipped["kept"]["task_id"] == "plan-1"
    assert skipped["requested"]["task_id"] == "route-2"
    assert db.routing_guard_get(caller="claude-code", cwd=cwd)["task_id"] == "plan-1"


def test_same_task_is_not_downgraded_but_a_new_task_is_written(tmp_path: Path) -> None:
    _cfg, db = _db(tmp_path)
    cwd = str(tmp_path)
    _put(db, cwd, ROUTING_GUARD_MODE_DIRECT, "task A", "route-a")

    same = _put(db, cwd, ROUTING_GUARD_MODE_EXECUTE_SUBTASK, "task A", "route-a")
    assert same["skipped"] is True
    assert same["reason"] == "same_task_no_downgrade"

    other = _put(db, cwd, ROUTING_GUARD_MODE_EXECUTE_SUBTASK, "task B", "route-b")
    assert "skipped" not in other
    assert db.routing_guard_get(caller="claude-code", cwd=cwd)["task_id"] == "route-b"


def test_legacy_guard_without_task_id_compares_task_text(tmp_path: Path) -> None:
    _cfg, db = _db(tmp_path)
    cwd = str(tmp_path)
    _put(db, cwd, ROUTING_GUARD_MODE_DIRECT, "task A", None)

    assert _put(db, cwd, ROUTING_GUARD_MODE_EXECUTE_SUBTASK, "task A", "route-a")["skipped"] is True
    assert "skipped" not in _put(db, cwd, ROUTING_GUARD_MODE_EXECUTE_SUBTASK, "task B", "route-b")


def test_skipped_write_is_never_attached_as_routing_guard() -> None:
    result: dict = {}
    mcp_server._attach_routing_guard(
        result,
        {
            "skipped": True,
            "reason": "active_routed_plan",
            "kept": {"mode": "routed_plan", "source_tool": "plan_task", "task_id": "plan-1"},
            "requested": {"mode": "direct", "task_id": "route-2"},
        },
    )
    assert "routing_guard" not in result
    assert result["routing_guard_skipped"]["kept_task_id"] == "plan-1"
    assert result["routing_guard_skipped"]["requested_mode"] == "direct"

    written: dict = {}
    mcp_server._attach_routing_guard(written, {"mode": "direct", "task_id": "route-3"})
    assert written["routing_guard"]["task_id"] == "route-3"
    mcp_server._attach_routing_guard(written, None)
    assert written["routing_guard"]["task_id"] == "route-3"


def test_route_task_reports_skip_instead_of_the_old_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The observed bug: a response carried an unrelated old guard with skipped:true."""
    cfg, db = _db(tmp_path)
    cwd = str(ROOT)
    task = "fix typo in shared/db.py"
    task_id = mcp_server.shared_outcomes.route_task_id(task)
    # Same task already routed direct; this call routes low (execute_subtask) and
    # must not downgrade — but also must not hand the old row back as the guard.
    _put(db, cwd, ROUTING_GUARD_MODE_DIRECT, task, task_id)
    router = SimpleNamespace(
        classify=lambda _task, project_path=None, evidence=None: SimpleNamespace(
            tier="low", score=0.1, reason="low", agents=1, override=False
        )
    )
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        mcp_server, "_ensure_init", lambda: (cfg, db, router, SimpleNamespace(), SimpleNamespace())
    )
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")
    monkeypatch.setattr(
        mcp_server,
        "_route_guard_mode_for_route",
        lambda **_kw: ROUTING_GUARD_MODE_EXECUTE_SUBTASK,
    )

    routed = mcp_server.handle_route_task({"task": task, "cwd": cwd})

    assert "routing_guard" not in routed
    assert routed["routing_guard_skipped"]["reason"] == "same_task_no_downgrade"


# ---------------------------------------------------------------------------
# Clearing
# ---------------------------------------------------------------------------


def test_record_outcome_clears_the_tasks_guard_and_hands_over_task_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg, db = _db(tmp_path)
    cwd = str(tmp_path)
    _put(db, cwd, ROUTING_GUARD_MODE_DIRECT, "the task", "route-x")
    _put(db, str(tmp_path / "other"), ROUTING_GUARD_MODE_DIRECT, "other task", "route-y")
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")
    monkeypatch.setattr(
        mcp_server.shared_outcomes, "record_outcome", lambda *_a, **_k: {"recorded": True}
    )
    finalized: list[dict] = []
    monkeypatch.setattr(
        mcp_server.shared_direct_edit_quality,
        "schedule_finalize",
        lambda _db, task_id, **kw: finalized.append({"task_id": task_id, **kw}),
    )

    out = mcp_server.handle_record_outcome({"task_id": "route-x", "outcome": "accepted"})

    assert out == {"recorded": True}
    assert db.routing_guard_get(caller="claude-code", cwd=cwd) is None
    # Another task's guard is untouched.
    assert db.routing_guard_get(caller="claude-code", cwd=str(tmp_path / "other")) is not None
    assert finalized[0]["task_text"] == "the task"


def test_terminal_swarm_report_clears_the_run_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg, db = _db(tmp_path)
    cwd = str(tmp_path)
    _put(db, cwd, ROUTING_GUARD_MODE_ROUTED_PLAN, "swarm", "swarm-t", source_tool="execute_swarm")
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
    monkeypatch.setattr(mcp_server, "_handle_report_host_wave_impl", lambda _a: {"ok": True})

    # Non-terminal: guard stays.
    mcp_server.handle_report_host_wave({"swarm_id": "swarm-t", "wave": 1, "agents": []})
    assert db.routing_guard_get(caller="claude-code", cwd=cwd) is not None

    mcp_server.handle_report_host_swarm_complete({"swarm_id": "swarm-t", "outcome": "accepted"})
    assert db.routing_guard_get(caller="claude-code", cwd=cwd) is None


def test_terminal_report_error_keeps_the_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg, db = _db(tmp_path)
    cwd = str(tmp_path)
    _put(db, cwd, ROUTING_GUARD_MODE_ROUTED_PLAN, "swarm", "swarm-e", source_tool="execute_swarm")
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
    monkeypatch.setattr(mcp_server, "_handle_report_host_wave_impl", lambda _a: {"error": "boom"})

    mcp_server.handle_report_host_swarm_complete({"swarm_id": "swarm-e", "outcome": "accepted"})
    assert db.routing_guard_get(caller="claude-code", cwd=cwd) is not None


def test_warm_path_purges_expired_guards_rate_limited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cfg, db = _db(tmp_path)
    _put(db, str(tmp_path), ROUTING_GUARD_MODE_DIRECT, "old", "route-old", ttl_seconds=0)
    _put(db, str(tmp_path / "live"), ROUTING_GUARD_MODE_DIRECT, "live", "route-live")
    monkeypatch.setattr(shared_eval, "_LAST_SWARM_REAP_TS", None)

    first = shared_eval.run_warm_path_background_tasks(db)
    assert first.get("routing_guard_purge") == 1
    with db.conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM routing_guards").fetchone()[0] == 1

    # Same cadence as the reaper: the next tick inside the interval skips it.
    second = shared_eval.run_warm_path_background_tasks(db)
    assert "routing_guard_purge" not in second


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_adds_columns_to_an_old_schema_db(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE swarm_runs (
                swarm_id TEXT PRIMARY KEY,
                task_hash TEXT NOT NULL DEFAULT '',
                created_ts REAL NOT NULL,
                status TEXT NOT NULL,
                requested_agents INTEGER NOT NULL,
                effective_agents INTEGER NOT NULL,
                progress_counters TEXT NOT NULL DEFAULT '{}',
                cost_summary_ref TEXT,
                topology TEXT,
                round INTEGER NOT NULL DEFAULT 0,
                resumable INTEGER NOT NULL DEFAULT 0,
                resume_status TEXT NOT NULL DEFAULT 'not_resumable',
                parent_swarm_id TEXT,
                chosen_checkpoint_index INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO swarm_runs (swarm_id, created_ts, status, requested_agents, effective_agents) "
            "VALUES ('legacy', ?, 'awaiting_host_execution', 1, 1)",
            (time.time(),),
        )
        conn.execute(
            """
            CREATE TABLE routing_guards (
                guard_key TEXT PRIMARY KEY,
                caller TEXT NOT NULL,
                cwd TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL,
                tier TEXT,
                provider TEXT,
                model TEXT,
                source_tool TEXT NOT NULL DEFAULT '',
                task_text TEXT NOT NULL DEFAULT '',
                file_hints_json TEXT NOT NULL DEFAULT '[]',
                created_ts REAL NOT NULL,
                expires_ts REAL NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO routing_guards (guard_key, caller, cwd, mode, task_text, created_ts, expires_ts) "
            "VALUES ('k', 'claude-code', '/x', 'direct', 'legacy task', ?, ?)",
            (time.time(), time.time() + 600),
        )

    db = Database(db_path=db_path)
    with db.conn() as conn:
        run_cols = {row[1] for row in conn.execute("PRAGMA table_info(swarm_runs)")}
        guard_cols = {row[1] for row in conn.execute("PRAGMA table_info(routing_guards)")}
        legacy = conn.execute(
            "SELECT workspace_root, caller FROM swarm_runs WHERE swarm_id = 'legacy'"
        ).fetchone()
    assert {"workspace_root", "caller"} <= run_cols
    assert "task_id" in guard_cols
    assert legacy == (None, None)
    guard = db.routing_guard_get(caller="claude-code", cwd="/x")
    assert guard is not None and guard["task_id"] is None


# ---------------------------------------------------------------------------
# Active-handoff fallback scope
# ---------------------------------------------------------------------------


def _handoff(db: Database, swarm_id: str, *, workspace_root: str | None, caller: str | None = None) -> None:
    row: dict[str, object] = {
        "swarm_id": swarm_id,
        "status": "awaiting_host_execution",
        "requested_agents": 1,
        "effective_agents": 1,
        "created_ts": time.time() - 30,
    }
    if workspace_root is not None:
        row["workspace_root"] = workspace_root
    if caller is not None:
        row["caller"] = caller
    db.persist_swarm_run(row)


def test_active_handoff_fallback_is_scoped_to_the_workspace(tmp_path: Path) -> None:
    _cfg, db = _db(tmp_path)
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    (project_a / "sub").mkdir(parents=True)
    project_b.mkdir()
    _handoff(db, "swarm-a", workspace_root=str(project_a.resolve()), caller="claude-code")

    assert mcp_server._caller_has_active_host_handoff(db, "claude-code", str(project_a)) is True
    assert mcp_server._caller_has_active_host_handoff(db, "claude-code", str(project_a / "sub")) is True
    assert mcp_server._caller_has_active_host_handoff(db, "claude-code", str(project_b)) is False
    # Another host's handoff in the same workspace is not this caller's.
    assert mcp_server._caller_has_active_host_handoff(db, "codex", str(project_a)) is False


def test_active_handoff_fallback_keeps_age_window_for_legacy_rows(tmp_path: Path) -> None:
    _cfg, db = _db(tmp_path)
    _handoff(db, "legacy", workspace_root=None)
    assert mcp_server._caller_has_active_host_handoff(db, "claude-code", str(tmp_path)) is True
    with db.conn() as conn:
        conn.execute(
            "UPDATE swarm_runs SET created_ts = ? WHERE swarm_id = 'legacy'",
            (time.time() - ROUTING_GUARD_TTL_SECONDS - 60,),
        )
    assert mcp_server._caller_has_active_host_handoff(db, "claude-code", str(tmp_path)) is False


# ---------------------------------------------------------------------------
# Supersede
# ---------------------------------------------------------------------------


def _registered_run(db: Database, swarm_id: str, ws: str, *, caller: str = "claude-code") -> None:
    db.persist_swarm_run(
        {
            "swarm_id": swarm_id,
            "status": "awaiting_host_execution",
            "requested_agents": 1,
            "effective_agents": 1,
            "workspace_root": ws,
            "caller": caller,
        }
    )
    for event in ("execute_swarm_requested", "host_native_handoff", "host_handoff_registered"):
        db.log_swarm_event(swarm_id, event, {})


def test_idle_previous_run_is_superseded(tmp_path: Path) -> None:
    _cfg, db = _db(tmp_path)
    runs = tmp_path / "runs"
    ws = str(tmp_path / "proj")
    _registered_run(db, "swarm-old", ws)
    _registered_run(db, "swarm-new", ws)
    # Registration-only artefacts do not count as activity: the per-planned-agent
    # stub snapshot the handoff writes, an empty artifacts dir, replayed findings.
    db.persist_worker_snapshot("swarm-old", worker_index=0, snapshot_json={"spawn_id": "1"})
    (runs / "swarm-old" / "artifacts").mkdir(parents=True)
    (runs / "swarm-old" / "findings").mkdir()
    (runs / "swarm-old" / "findings" / "replay.md").write_text("replayed\n")

    out = db.supersede_idle_swarm_runs(
        new_swarm_id="swarm-new", workspace_root=ws, caller="claude-code", runs_root=runs
    )

    assert out == {"superseded": ["swarm-old"], "concurrent_active": []}
    old = db.get_swarm_summary("swarm-old")
    assert old["status"] == Database.SWARM_STATUS_SUPERSEDED
    assert db.get_swarm_summary("swarm-new")["parent_swarm_id"] == "swarm-old"
    events = db.get_swarm_events("swarm-old", event_type="superseded_by")
    assert events and json.dumps(events[0]).count("swarm-new") >= 1
    assert Database.SWARM_STATUS_SUPERSEDED not in Database.ACTIVE_SWARM_STATUSES


@pytest.mark.parametrize("activity", ["event", "runtime", "wave_log", "artifact"])
def test_previous_run_with_activity_is_left_alone(tmp_path: Path, activity: str) -> None:
    _cfg, db = _db(tmp_path)
    runs = tmp_path / "runs"
    ws = str(tmp_path / "proj")
    _registered_run(db, "swarm-busy", ws)
    _registered_run(db, "swarm-new", ws)
    if activity == "event":
        db.log_swarm_event("swarm-busy", "wave_progress", {"wave": 1})
    elif activity == "runtime":
        db.log_swarm_event("swarm-busy", "runtime_handoff_started", {})
    elif activity == "wave_log":
        (runs / "swarm-busy").mkdir(parents=True)
        (runs / "swarm-busy" / "wave.jsonl").write_text(
            json.dumps({"source": run_log.HOOK_SOURCE, "ts": time.time()}) + "\n"
        )
    else:
        (runs / "swarm-busy" / "artifacts").mkdir(parents=True)
        (runs / "swarm-busy" / "artifacts" / "agent-1.md").write_text("output\n")

    out = db.supersede_idle_swarm_runs(
        new_swarm_id="swarm-new", workspace_root=ws, caller="claude-code", runs_root=runs
    )

    assert out == {"superseded": [], "concurrent_active": ["swarm-busy"]}
    assert db.get_swarm_summary("swarm-busy")["status"] == "awaiting_host_execution"
    assert db.get_swarm_summary("swarm-new")["parent_swarm_id"] is None


def test_supersede_ignores_other_workspaces_callers_and_running_runs(tmp_path: Path) -> None:
    _cfg, db = _db(tmp_path)
    runs = tmp_path / "runs"
    ws = str(tmp_path / "proj")
    _registered_run(db, "swarm-other-ws", str(tmp_path / "elsewhere"))
    _registered_run(db, "swarm-other-host", ws, caller="codex")
    _registered_run(db, "swarm-running", ws)
    db.persist_swarm_run({"swarm_id": "swarm-running", "status": "running"})
    _registered_run(db, "swarm-new", ws)

    out = db.supersede_idle_swarm_runs(
        new_swarm_id="swarm-new", workspace_root=ws, caller="claude-code", runs_root=runs
    )

    assert out["superseded"] == []
    assert out["concurrent_active"] == ["swarm-running"]
    assert db.get_swarm_summary("swarm-other-ws")["status"] == "awaiting_host_execution"
    assert db.get_swarm_summary("swarm-other-host")["status"] == "awaiting_host_execution"


def test_supersede_picks_up_a_legacy_run_through_the_pointer(tmp_path: Path) -> None:
    _cfg, db = _db(tmp_path)
    ws = str(tmp_path / "proj")
    db.persist_swarm_run(
        {"swarm_id": "swarm-legacy", "status": "awaiting_host_execution",
         "requested_agents": 1, "effective_agents": 1}
    )
    _registered_run(db, "swarm-new", ws)

    out = db.supersede_idle_swarm_runs(
        new_swarm_id="swarm-new", workspace_root=ws, caller="claude-code",
        extra_candidates=["swarm-legacy"], runs_root=tmp_path / "runs",
    )
    assert out["superseded"] == ["swarm-legacy"]


def test_reaper_ignores_superseded_runs(tmp_path: Path) -> None:
    _cfg, db = _db(tmp_path)
    db.persist_swarm_run(
        {"swarm_id": "swarm-s", "status": Database.SWARM_STATUS_SUPERSEDED,
         "requested_agents": 1, "effective_agents": 1, "created_ts": time.time() - 10 * 86400}
    )
    reaped = db.reap_stale_swarm_runs(runs_root=tmp_path / "runs")
    assert "swarm-s" not in reaped
    assert db.get_swarm_summary("swarm-s")["status"] == Database.SWARM_STATUS_SUPERSEDED


def test_inspect_status_counts_superseded(tmp_path: Path) -> None:
    _cfg, db = _db(tmp_path)
    for sid, status in (("s1", "superseded"), ("s2", "abandoned"), ("s3", "superseded")):
        db.persist_swarm_run(
            {"swarm_id": sid, "status": status, "requested_agents": 1, "effective_agents": 1}
        )
    summary = shared_status._load_swarm_run_summary(db)
    assert summary["superseded"] == 2
    assert summary["abandoned"] == 1


class _FakePlan:
    total_agents = 1
    topology = "linear"


class _FakePlanner:
    def plan(self, _t: str, **_k: object) -> _FakePlan:
        return _FakePlan()

    def plan_heuristic(self, _t: str, **_k: object) -> _FakePlan:
        return _FakePlan()

    def plan_to_dict(self, _p: _FakePlan) -> dict[str, object]:
        return {
            "subtasks": [
                {
                    "id": "st-1",
                    "description": "build the calculator ops module",
                    "tier": "low",
                    "depends_on": [],
                    "target_file": "ops.py",
                }
            ],
            "waves": [["st-1"]],
            "topology": "linear",
        }


def test_reissued_execute_swarm_supersedes_the_first_handoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end through handle_execute_swarm: every event the real handoff writes
    must be classified as registration, or the first run would never supersede."""
    cfg, db = _db(tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")
    monkeypatch.setattr(mcp_server, "_spawn_execute_swarm_runtime_handoff", lambda *a, **k: None)
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, _FakePlanner(), None))
    ws = str((tmp_path / "proj").resolve())

    first = mcp_server.handle_execute_swarm(
        {"task": "build calculator ops", "max_agents": 1, "workspace_root": ws}
    )["result"]
    second = mcp_server.handle_execute_swarm(
        {"task": "build calculator ops again", "max_agents": 1, "workspace_root": ws}
    )["result"]

    first_id, second_id = first["swarm_id"], second["swarm_id"]
    assert "superseded_runs" not in first
    assert second["superseded_runs"] == [first_id]
    assert "concurrent_active_run" not in second
    assert db.get_swarm_summary(first_id)["status"] == Database.SWARM_STATUS_SUPERSEDED
    new_summary = db.get_swarm_summary(second_id)
    assert new_summary["status"] == "awaiting_host_execution"
    assert new_summary["parent_swarm_id"] == first_id
    with db.conn() as conn:
        row = conn.execute(
            "SELECT workspace_root, caller FROM swarm_runs WHERE swarm_id = ?", (second_id,)
        ).fetchone()
    assert row == (ws, "claude-code")
    # The new run's guard is its own, written fresh — not the first run's.
    assert second["routing_guard"]["task_id"] == second_id


def test_reissued_execute_swarm_warns_about_a_worked_on_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg, db = _db(tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")
    monkeypatch.setattr(mcp_server, "_spawn_execute_swarm_runtime_handoff", lambda *a, **k: None)
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, _FakePlanner(), None))
    ws = str((tmp_path / "proj").resolve())

    first = mcp_server.handle_execute_swarm(
        {"task": "build calculator ops", "max_agents": 1, "workspace_root": ws}
    )["result"]
    db.log_swarm_event(first["swarm_id"], "host_agent_complete", {"wave": 1, "agents": []})
    second = mcp_server.handle_execute_swarm(
        {"task": "build calculator ops again", "max_agents": 1, "workspace_root": ws}
    )["result"]

    assert "superseded_runs" not in second
    assert second["concurrent_active_run"]["swarm_ids"] == [first["swarm_id"]]
    assert db.get_swarm_summary(first["swarm_id"])["status"] == "awaiting_host_execution"
