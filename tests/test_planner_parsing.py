#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server
from shared.config import TGsConfig
from shared.db import Database
from shared.planner import CLIBackend, Planner, PlannerParseError, parse_planner_output


class MockPlannerBackend(CLIBackend):
    def __init__(self, response: str | None) -> None:
        self._response = response

    def call(self, prompt: str, model: str | None = None, timeout: int = 120) -> str | None:
        return self._response


def test_valid_planner_output_parses() -> None:
    """Delimited planner output parses into a JSON dict."""
    raw = (
        "<PLAN_JSON>\n"
        + json.dumps({
            "analysis": "Test",
            "subtasks": [
                {"id": 1, "description": "do stuff", "tier": "low", "depends_on": []},
            ],
            "strategy": "parallel",
        })
        + "\n</PLAN_JSON>"
    )

    parsed = parse_planner_output(raw)
    assert parsed["subtasks"][0]["id"] == 1


def test_malformed_planner_output_returns_parse_error(tmp_path: Path) -> None:
    """Malformed planner output raises PlannerParseError and persists diagnostics."""
    db_path = tmp_path / "planner.db"
    db = Database(db_path=db_path)
    planner = Planner(
        TGsConfig(db_path=db_path),
        MockPlannerBackend(
            '<PLAN_JSON>{"api_key":"fake_sensitive_token_12345678901234567890",oops}</PLAN_JSON>'
        ),
        db,
    )

    with pytest.raises(PlannerParseError) as exc_info:
        planner.plan("bad planner output", skip_cache=True)

    diagnostics_id = exc_info.value.parse_diagnostics_id
    assert diagnostics_id is not None

    with db.conn() as conn:
        row = conn.execute(
            "SELECT parse_diagnostics FROM telemetry WHERE id = ?",
            (diagnostics_id,),
        ).fetchone()

    assert row is not None
    assert "fake_sensitive_token_12345678901234567890" not in row[0]
    assert "<redacted>" in row[0]
    assert "oops" in row[0]


def test_handle_plan_task_surfaces_parse_error(tmp_path: Path) -> None:
    """MCP surfaces planner parse errors instead of returning a fallback plan."""
    db_path = tmp_path / "mcp-planner.db"
    cfg = TGsConfig(db_path=db_path)
    db = Database(db_path=db_path)
    planner = Planner(
        cfg,
        MockPlannerBackend("plain json without delimiters"),
        db,
    )

    mcp_server._config = cfg
    mcp_server._db = db
    mcp_server._router = None
    mcp_server._planner = planner
    mcp_server._orchestrator = None

    result = mcp_server.handle_plan_task({"task": "bad planner output"})

    assert result["error"] == "PlannerParseError"
    assert result["parse_diagnostics_id"] is not None


def test_build_plan_falls_back_for_non_string_subtask_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "planner.db"
    planner = Planner(
        TGsConfig(db_path=db_path),
        MockPlannerBackend(None),
        Database(db_path=db_path),
    )

    plan = planner._build_plan({
        "subtasks": [
            {
                "id": "1",
                "description": None,
                "tier": None,
                "depends_on": "not-a-list",
            }
        ],
        "strategy": "parallel",
    }, "fallback task")

    assert plan.subtasks[0].description == "fallback task"
    assert plan.subtasks[0].tier == "medium"
    assert plan.subtasks[0].depends_on == []


# ---------------------------------------------------------------------------
# From test_planner_host_native.py
# ---------------------------------------------------------------------------

from tempfile import TemporaryDirectory
from shared.router import TaskRouter


class RecordingBackend(CLIBackend):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, prompt: str, model: str | None = None, timeout: int = 120) -> str | None:
        self.calls.append(prompt)
        return None


class StubRegistry:
    def select_provider(self, tier: str, *, caller: str | None = None, prefer_free: bool = True):
        from types import SimpleNamespace

        return SimpleNamespace(
            name="cursor",
            display_name="Cursor",
            resolve_model=lambda _tier: "cursor-model",
            cost_rank=0,
            billing_tier="free",
            is_free=True,
        )


def test_handle_plan_task_cursor_skips_planner_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as td:
        db_path = Path(td) / "planner-host-native.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        backend = RecordingBackend()
        planner = Planner(cfg, backend, db=db)
        router = TaskRouter(cfg)
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, router, planner, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubRegistry())
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "cursor")

        result = mcp_server.handle_plan_task(
            {
                "task": (
                    "Build a calculator app: (1) models.py with Operation dataclass, "
                    "(2) ops.py with add/sub/mul/div, (3) main.py CLI entrypoint"
                )
            }
        )

        assert backend.calls == []
        assert result.get("planner_host_execution_mode") == "host_native"
        assert result.get("planner_mode") == "heuristic"
        assert len(result.get("subtasks", [])) == 3
        assert result.get("host_execution_contract") == "spawn_subagents"
        waves = result.get("host_spawn_waves")
        assert isinstance(waves, list) and waves
        for wave in waves:
            assert wave.get("execution_contract") == "spawn_subagents"
            for agent in wave.get("agents", []):
                assert agent.get("method") == "host_task"
                assert agent.get("spawn_required") is True
