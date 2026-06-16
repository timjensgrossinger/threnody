#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import TGsConfig
from shared.db import Database
from shared.host_learning import (
    _HOST_RUN_META,
    build_learning_report_contract,
    host_task_id,
    ingest_host_wave,
    inspect_host_swarm,
    plan_run_id,
    register_host_run_handoff,
)
from shared.router import TaskRouter


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "host-learning.db")
    database._init_schema(database._get_connection())
    yield database
    database.close()


def test_plan_run_id_is_stable() -> None:
    assert plan_run_id("hello") == plan_run_id("hello")
    assert plan_run_id("hello") != plan_run_id("world")


def test_register_host_run_handoff_adds_task_ids(db: Database) -> None:
    run_id = "swarm-test-1"
    waves = [
        {
            "wave": 1,
            "agents": [
                {"id": "1", "tier": "low", "model": "test-model", "prompt": "create a.py"},
                {"id": "2", "tier": "low", "model": "test-model", "prompt": "create b.py"},
            ],
        }
    ]
    register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=waves,
        planned_subtasks=2,
        workspace_root="/tmp/project",
    )
    assert waves[0]["agents"][0]["task_id"] == host_task_id(run_id, "1")
    assert waves[0]["agents"][1]["task_id"] == host_task_id(run_id, "2")

    with db.conn() as conn:
        telemetry_count = conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE session_id = ?",
            (run_id,),
        ).fetchone()[0]
        worker_count = conn.execute(
            "SELECT COUNT(*) FROM swarm_workers WHERE swarm_id = ?",
            (run_id,),
        ).fetchone()[0]
    assert telemetry_count == 2
    assert worker_count == 2


def test_ingest_host_wave_tracks_patterns_and_finalizes(db: Database) -> None:
    run_id = "swarm-test-2"
    waves = [
        {
            "wave": 1,
            "agents": [
                {"id": "1", "tier": "low", "model": "test-model", "prompt": "create greet.py"},
            ],
        }
    ]
    register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=waves,
        planned_subtasks=1,
        workspace_root="/tmp/project",
    )
    db.persist_swarm_run(
        {
            "swarm_id": run_id,
            "status": "awaiting_host_execution",
            "requested_agents": 1,
            "effective_agents": 1,
        }
    )
    router = TaskRouter(TGsConfig(), db=db)
    router.enable_learning("/tmp/project")

    result = ingest_host_wave(
        db,
        run_id=run_id,
        wave_index=1,
        agents=[
            {
                "spawn_id": "1",
                "task_id": host_task_id(run_id, "1"),
                "success": True,
                "touched_files": ["greet.py"],
                "output_excerpt": "created greet.py",
            }
        ],
        workspace_root="/tmp/project",
        terminal=True,
        outcome="accepted",
        config=TGsConfig(),
        router=router,
    )
    assert result["agents_recorded"] == 1
    assert result["finalize"]["status"] == "completed"
    assert result["finalize"]["swarm_outcome"] == {"stored": True, "task_id": run_id}

    with db.conn() as conn:
        pattern_count = conn.execute("SELECT COUNT(*) FROM subtask_patterns").fetchone()[0]
        outcome_count = conn.execute(
            "SELECT COUNT(*) FROM routing_outcomes WHERE task_id = ?",
            (run_id,),
        ).fetchone()[0]
    assert pattern_count >= 1
    assert outcome_count == 1

    assert db.routing_guard_has_executions(caller="mcp", cwd="/tmp/project") is True

    summary = inspect_host_swarm(db, run_id)
    assert summary is not None
    assert summary.get("status") in {"completed", "running", "awaiting_host_execution"}


def test_ingest_host_wave_batches_multi_agent_writes(db: Database) -> None:
    """A multi-agent wave writes one telemetry + routing-guard row per agent/file
    via the single batched flush — same totals as the per-agent path would."""
    run_id = "swarm-batch"
    n = 6
    agent_specs = [
        {"id": str(i), "tier": "low", "model": "m", "prompt": f"create file_{i}.py"}
        for i in range(1, n + 1)
    ]
    register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=[{"wave": 1, "agents": agent_specs}],
        planned_subtasks=n,
        workspace_root="/tmp/project",
    )
    db.persist_swarm_run(
        {"swarm_id": run_id, "status": "running", "requested_agents": n,
         "effective_agents": n, "resume_status": "running"}
    )
    result = ingest_host_wave(
        db,
        run_id=run_id,
        wave_index=1,
        agents=[
            {
                "spawn_id": str(i),
                "task_id": host_task_id(run_id, str(i)),
                "success": True,
                "touched_files": [f"file_{i}.py"],
                "output_excerpt": f"created file_{i}.py",
            }
            for i in range(1, n + 1)
        ],
        workspace_root="/tmp/project",
        terminal=False,
        config=TGsConfig(),
    )
    assert result["agents_recorded"] == n
    with db.conn() as conn:
        # Only count completion rows; register_host_run_handoff writes a
        # separate per-agent host_handoff_stub row at handoff time.
        tel = conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE session_id = ? AND reason = ?",
            (run_id, "host_agent_complete"),
        ).fetchone()[0]
        # The 6 prompts normalize to one pattern; the batched flush must
        # accumulate occurrence_count across all agents (sees in-txn writes).
        occ = conn.execute(
            "SELECT COALESCE(SUM(occurrence_count), 0) FROM subtask_patterns"
        ).fetchone()[0]
        guards = conn.execute(
            "SELECT COUNT(*) FROM routing_guard_executions WHERE task_id LIKE ?",
            (f"%{run_id}%",),
        ).fetchone()[0]
    assert tel == n          # one telemetry row per agent
    assert occ == n          # occurrence_count accumulated across the batch
    assert guards == n       # one guard row per touched file


def test_ingest_enriches_pattern_from_handoff_snapshot(db: Database) -> None:
    run_id = "swarm-test-snapshot"
    waves = [
        {
            "wave": 1,
            "agents": [
                {
                    "id": "1",
                    "tier": "low",
                    "model": "test-model",
                    "prompt": "create greet.py with hello world",
                },
            ],
        }
    ]
    register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=waves,
        planned_subtasks=1,
        workspace_root="/tmp/project",
    )
    db.persist_swarm_run(
        {
            "swarm_id": run_id,
            "status": "running",
            "requested_agents": 1,
            "effective_agents": 1,
            "resume_status": "running",
        }
    )
    ingest_host_wave(
        db,
        run_id=run_id,
        wave_index=1,
        agents=[
            {
                "spawn_id": "opaque-host-id",
                "task_id": host_task_id(run_id, "1"),
                "success": True,
                "touched_files": ["greet.py"],
            }
        ],
        workspace_root="/tmp/project",
        terminal=True,
        outcome="accepted",
        config=TGsConfig(),
    )
    with db.conn() as conn:
        row = conn.execute(
            "SELECT pattern_desc FROM subtask_patterns ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert "greet.py" in row[0]
    assert "opaque-host-id" not in row[0]


def test_host_run_meta_reloads_after_ram_clear(db: Database) -> None:
    run_id = "swarm-test-meta"
    register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=[
            {
                "wave": 1,
                "agents": [{"id": "1", "tier": "low", "model": "m", "prompt": "p"}],
            }
        ],
        planned_subtasks=1,
        workspace_root="/tmp/project",
        topology="dag",
    )
    _HOST_RUN_META.pop(run_id, None)
    db.persist_swarm_run(
        {
            "swarm_id": run_id,
            "status": "running",
            "resume_status": "running",
        }
    )
    result = ingest_host_wave(
        db,
        run_id=run_id,
        wave_index=1,
        agents=[
            {
                "spawn_id": "1",
                "task_id": host_task_id(run_id, "1"),
                "success": True,
            }
        ],
        workspace_root="/tmp/project",
        terminal=True,
        outcome="accepted",
        config=TGsConfig(),
    )
    assert result["finalize"]["status"] == "completed"
    snapshots = db.get_handoff_agent_snapshots(run_id)
    assert len(snapshots) == 1


def test_build_learning_report_contract_includes_workspace_root() -> None:
    contract = build_learning_report_contract("/tmp/project")
    assert contract["workspace_root"] == "/tmp/project"
    assert "output_excerpt" in contract["per_agent"]


def test_ingest_reads_files_from_handoff_meta_without_arg(
    db: Database,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "greet.py").write_text("print('hello')\n", encoding="utf-8")
    run_id = "swarm-meta-root"
    register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=[
            {
                "wave": 1,
                "agents": [
                    {"id": "1", "tier": "low", "model": "m", "prompt": "create greet.py"},
                ],
            }
        ],
        planned_subtasks=1,
        workspace_root=str(workspace),
    )
    db.persist_swarm_run(
        {
            "swarm_id": run_id,
            "status": "running",
            "resume_status": "running",
        }
    )
    result = ingest_host_wave(
        db,
        run_id=run_id,
        wave_index=1,
        agents=[
            {
                "spawn_id": "1",
                "task_id": host_task_id(run_id, "1"),
                "success": True,
                "touched_files": ["greet.py"],
            }
        ],
        terminal=True,
        outcome="accepted",
        config=TGsConfig(),
    )
    enrichment = result.get("learning_enrichment") or {}
    assert enrichment.get("workspace_root") == str(workspace)
    assert enrichment.get("files_read", 0) >= 1


def test_ingest_resolves_absolute_touched_files(
    db: Database,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "abs-project"
    workspace.mkdir()
    target = workspace / "api.js"
    target.write_text("export const WeatherAPI = {};\n", encoding="utf-8")
    run_id = "swarm-abs-path"
    register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=[
            {
                "wave": 1,
                "agents": [{"id": "1", "tier": "low", "model": "m", "prompt": "api.js"}],
            }
        ],
        planned_subtasks=1,
        workspace_root=str(workspace),
    )
    db.persist_swarm_run(
        {
            "swarm_id": run_id,
            "status": "running",
            "resume_status": "running",
        }
    )
    result = ingest_host_wave(
        db,
        run_id=run_id,
        wave_index=1,
        agents=[
            {
                "spawn_id": "1",
                "task_id": host_task_id(run_id, "1"),
                "success": True,
                "touched_files": [str(target)],
            }
        ],
        terminal=True,
        outcome="accepted",
        config=TGsConfig(),
    )
    enrichment = result.get("learning_enrichment") or {}
    assert enrichment.get("files_read", 0) == 1


def test_ingest_auto_output_excerpt_when_missing(
    db: Database,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "excerpt-project"
    workspace.mkdir()
    (workspace / "styles.css").write_text(
        ".weather-card { display: grid; }\n",
        encoding="utf-8",
    )
    run_id = "swarm-auto-excerpt"
    register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=[
            {
                "wave": 1,
                "agents": [{"id": "1", "tier": "low", "model": "m", "prompt": "styles.css"}],
            }
        ],
        planned_subtasks=1,
        workspace_root=str(workspace),
    )
    db.persist_swarm_run(
        {
            "swarm_id": run_id,
            "status": "running",
            "resume_status": "running",
        }
    )
    result = ingest_host_wave(
        db,
        run_id=run_id,
        wave_index=1,
        agents=[
            {
                "spawn_id": "1",
                "task_id": host_task_id(run_id, "1"),
                "success": True,
                "touched_files": ["styles.css"],
            }
        ],
        workspace_root=str(workspace),
        terminal=True,
        outcome="accepted",
        config=TGsConfig(),
    )
    assert result.get("learning_enrichment", {}).get("auto_excerpt_count") == 1
    with db.conn() as conn:
        row = conn.execute(
            "SELECT examples, eval_quality FROM subtask_patterns ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    examples = json.loads(row[0])
    assert isinstance(examples, list) and examples
    latest = examples[-1]
    assert latest.get("outcome_summary") == "completed"
    assert float(row[1]) == pytest.approx(1.0)


def test_ingest_rework_detection_two_waves(db: Database, tmp_path: Path) -> None:
    workspace = tmp_path / "rework-project"
    workspace.mkdir()
    shared = workspace / "shared.js"
    shared.write_text("const version = 1;\n", encoding="utf-8")
    run_id = "swarm-rework"
    register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=[
            {"wave": 1, "agents": [{"id": "1", "tier": "low", "model": "m", "prompt": "shared.js"}]},
            {"wave": 2, "agents": [{"id": "2", "tier": "low", "model": "m", "prompt": "wire shared.js"}]},
        ],
        planned_subtasks=2,
        workspace_root=str(workspace),
    )
    db.persist_swarm_run(
        {
            "swarm_id": run_id,
            "status": "running",
            "resume_status": "running",
        }
    )
    ingest_host_wave(
        db,
        run_id=run_id,
        wave_index=1,
        agents=[
            {
                "spawn_id": "1",
                "task_id": host_task_id(run_id, "1"),
                "success": True,
                "touched_files": ["shared.js"],
                "output_excerpt": "created shared.js v1",
            }
        ],
        workspace_root=str(workspace),
    )
    shared.write_text("const version = 2;\n", encoding="utf-8")
    result = ingest_host_wave(
        db,
        run_id=run_id,
        wave_index=2,
        agents=[
            {
                "spawn_id": "2",
                "task_id": host_task_id(run_id, "2"),
                "success": True,
                "touched_files": ["shared.js"],
                "output_excerpt": "updated shared.js v2",
            }
        ],
        workspace_root=str(workspace),
        terminal=True,
        outcome="accepted",
        config=TGsConfig(),
    )
    assert len(result.get("rework_events") or []) >= 1
    with db.conn() as conn:
        rework_count = conn.execute(
            "SELECT COUNT(*) FROM rework_events WHERE session_id = ?",
            (run_id,),
        ).fetchone()[0]
    assert rework_count >= 1


def test_ingest_style_observe_on_rewrite(db: Database, tmp_path: Path) -> None:
    workspace = tmp_path / "style-project"
    workspace.mkdir()
    shared = workspace / "app.js"
    shared.write_text("const x = 1;\n", encoding="utf-8")
    run_id = "swarm-style"
    register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=[
            {"wave": 1, "agents": [{"id": "1", "tier": "low", "model": "m", "prompt": "app.js"}]},
            {"wave": 2, "agents": [{"id": "2", "tier": "low", "model": "m", "prompt": "integrate app.js"}]},
        ],
        planned_subtasks=2,
        workspace_root=str(workspace),
    )
    db.persist_swarm_run(
        {
            "swarm_id": run_id,
            "status": "running",
            "resume_status": "running",
        }
    )
    ingest_host_wave(
        db,
        run_id=run_id,
        wave_index=1,
        agents=[
            {
                "spawn_id": "1",
                "task_id": host_task_id(run_id, "1"),
                "success": True,
                "touched_files": ["app.js"],
            }
        ],
        workspace_root=str(workspace),
    )
    shared.write_text("const x = 2;\n", encoding="utf-8")
    with patch("shared.host_learning.StyleLearner.observe") as observe:
        ingest_host_wave(
            db,
            run_id=run_id,
            wave_index=2,
            agents=[
                {
                    "spawn_id": "2",
                    "task_id": host_task_id(run_id, "2"),
                    "success": True,
                    "touched_files": ["app.js"],
                }
            ],
            workspace_root=str(workspace),
            terminal=True,
            outcome="accepted",
            config=TGsConfig(),
        )
        assert observe.called


# ---------------------------------------------------------------------------
# host_plan_expand tests (from test_host_plan_expand.py)
# ---------------------------------------------------------------------------

from shared.host_plan_expand import expand_host_plan  # noqa: E402


def test_expand_host_plan_adds_parallel_wave(tmp_path: Path) -> None:
    database = Database(tmp_path / "host-plan-expand-1.db")
    database._init_schema(database._get_connection())
    try:
        run_id = "swarm-expand-1"
        register_host_run_handoff(
            database,
            run_id=run_id,
            host_spawn_waves=[
                {
                    "wave": 1,
                    "agents": [
                        {
                            "id": "1",
                            "tier": "low",
                            "model": "test",
                            "prompt": "scaffold contract",
                            "target_files": ["openapi.yaml"],
                        },
                    ],
                }
            ],
            planned_subtasks=1,
            workspace_root="/tmp/project",
            topology="dag",
            task_hint="build todo app",
        )
        database.persist_swarm_run(
            {
                "swarm_id": run_id,
                "status": "running",
                "requested_agents": 1,
                "effective_agents": 1,
                "resume_status": "running",
            }
        )
        result = expand_host_plan(
            database,
            run_id=run_id,
            discovered_files=["app.py", "templates/index.html", "static/js/app.js"],
            workspace_root="/tmp/project",
            config=TGsConfig(),
            caller="cursor",
        )
        assert result["expanded"] is True
        waves = result.get("host_spawn_waves")
        assert isinstance(waves, list) and len(waves) >= 1
        agent_count = sum(
            len(w.get("agents", []))
            for w in waves
            if isinstance(w, dict) and isinstance(w.get("agents"), list)
        )
        assert agent_count == 3
        snapshots = database.get_handoff_agent_snapshots(run_id)
        assert len(snapshots) == 4
    finally:
        database.close()


def test_expand_host_plan_skips_already_assigned_files(tmp_path: Path) -> None:
    database = Database(tmp_path / "host-plan-expand-2.db")
    database._init_schema(database._get_connection())
    try:
        run_id = "swarm-expand-2"
        register_host_run_handoff(
            database,
            run_id=run_id,
            host_spawn_waves=[
                {
                    "wave": 1,
                    "agents": [
                        {
                            "id": "1",
                            "tier": "low",
                            "model": "test",
                            "prompt": "create app.py",
                            "target_files": ["app.py"],
                        },
                    ],
                }
            ],
            planned_subtasks=1,
            workspace_root="/tmp/project",
        )
        database.persist_swarm_run(
            {
                "swarm_id": run_id,
                "status": "running",
                "resume_status": "running",
            }
        )
        result = expand_host_plan(
            database,
            run_id=run_id,
            discovered_files=["app.py", "style.css"],
            workspace_root="/tmp/project",
            config=TGsConfig(),
        )
        assert result["expanded"] is True
        assert result.get("new_files") == ["style.css"]
    finally:
        database.close()
