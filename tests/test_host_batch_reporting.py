#!/usr/bin/env python3
"""End-to-end batch-mode reporting through the MCP handlers.

Verifies that batch report_mode eliminates per-wave ingest (worker waves are
captured cheaply, not flushed) and that the single terminal report imports the
whole run log and finalizes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
from shared import run_log
from shared.config import HostNativeConfig, TGsConfig
from shared.db import Database
from shared.host_learning import host_task_id, register_host_run_handoff


@pytest.fixture(autouse=True)
def isolated_runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_log, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(run_log, "_ACTIVE_POINTER", tmp_path / "runs" / "active.json")


def _init(monkeypatch, tmp_path: Path, capture: str = "model") -> Database:
    cfg = TGsConfig(db_path=tmp_path / "batch.db")
    cfg.host_native = HostNativeConfig(report_mode="batch", learning_capture=capture)
    db = Database(db_path=tmp_path / "batch.db")
    db._init_schema(db._get_connection())
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
    return db


def _telemetry_count(db: Database, run_id: str) -> int:
    with db.conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE session_id = ? AND reason = ?",
            (run_id, "host_agent_complete"),
        ).fetchone()[0]


def test_worker_wave_is_deferred_then_terminal_imports(monkeypatch, tmp_path: Path) -> None:
    db = _init(monkeypatch, tmp_path, capture="model")
    run_id = "swarm-batch-e2e"
    register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=[
            {"wave": 1, "agents": [{"id": "1", "tier": "low", "model": "m", "prompt": "create a.py"}]},
            {"wave": 2, "agents": [{"id": "2", "tier": "low", "model": "m", "prompt": "create b.py"}]},
        ],
        planned_subtasks=2,
        workspace_root="/tmp/project",
    )
    db.persist_swarm_run({"swarm_id": run_id, "status": "running", "resume_status": "running"})

    # Worker wave 1 — must be captured, NOT ingested (no telemetry yet).
    r1 = mcp_server.handle_report_host_wave({
        "run_id": run_id, "wave": 1, "workspace_root": "/tmp/project",
        "agents": [{"spawn_id": "1", "task_id": host_task_id(run_id, "1"),
                    "success": True, "touched_files": ["a.py"], "output_excerpt": "a"}],
    })
    assert r1["deferred"] is True
    assert r1["captured"] == 1
    assert _telemetry_count(db, run_id) == 0

    # Terminal — imports the whole run log + finalizes.
    rt = mcp_server.handle_report_host_swarm_complete({
        "run_id": run_id, "wave": 2, "workspace_root": "/tmp/project", "outcome": "accepted",
        "agents": [{"spawn_id": "2", "task_id": host_task_id(run_id, "2"),
                    "success": True, "touched_files": ["b.py"], "output_excerpt": "b"}],
    })
    assert rt.get("finalize", {}).get("status") == "completed"
    assert _telemetry_count(db, run_id) == 2  # both waves imported once
    assert run_log.is_imported(run_id) is True
    assert run_log.get_active_run() is None  # pointer cleared


def test_inline_mode_still_ingests_per_wave(monkeypatch, tmp_path: Path) -> None:
    cfg = TGsConfig(db_path=tmp_path / "inline.db")
    cfg.host_native = HostNativeConfig(report_mode="inline")
    db = Database(db_path=tmp_path / "inline.db")
    db._init_schema(db._get_connection())
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))

    run_id = "swarm-inline-e2e"
    register_host_run_handoff(
        db, run_id=run_id,
        host_spawn_waves=[{"wave": 1, "agents": [{"id": "1", "tier": "low", "model": "m", "prompt": "create a.py"}]}],
        planned_subtasks=1, workspace_root="/tmp/project",
    )
    db.persist_swarm_run({"swarm_id": run_id, "status": "running", "resume_status": "running"})

    r1 = mcp_server.handle_report_host_wave({
        "run_id": run_id, "wave": 1, "workspace_root": "/tmp/project",
        "agents": [{"spawn_id": "1", "task_id": host_task_id(run_id, "1"),
                    "success": True, "touched_files": ["a.py"], "output_excerpt": "a"}],
    })
    # Inline path returns the legacy ingest shape, ingesting immediately.
    assert r1.get("agents_recorded") == 1
    assert _telemetry_count(db, run_id) == 1
