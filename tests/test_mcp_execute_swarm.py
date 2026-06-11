#!/usr/bin/env python3
"""Tests for the Phase 36 execute_swarm MCP surface."""
from __future__ import annotations

import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
from shared.config import TGsConfig
from shared.db import Database


def _stub_init(monkeypatch, tmp_path: Path) -> Database:
    db_path = tmp_path / "execute-swarm.db"
    cfg = TGsConfig(db_path=db_path)
    db = Database(db_path=db_path)
    db._init_schema(db._get_connection())
    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (cfg, db, None, None, None),
    )
    return db


def test_execute_swarm_tool_registered() -> None:
    tool_names = {tool["name"] for tool in mcp_server.TOOLS}

    assert "execute_swarm" in tool_names
    assert mcp_server.HANDLERS["execute_swarm"] is mcp_server.handle_execute_swarm


def test_execute_swarm_input_schema_present() -> None:
    tool = next(tool for tool in mcp_server.TOOLS if tool["name"] == "execute_swarm")

    props = tool["inputSchema"]["properties"]
    assert "task" in props
    assert "max_agents" in props
    assert "workspace_root" in props
    assert "budget_limit" in props
    assert "preview_token" in props


def test_execute_swarm_initial_response_shape(monkeypatch, tmp_path: Path) -> None:
    _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "test-initial")

    result = mcp_server.handle_execute_swarm({"task": {"id": "t-1"}, "max_agents": 5})

    assert result["started"] is True
    payload = result["result"]
    assert payload["swarm_id"].startswith("swarm-")
    assert payload["requested_vs_effective_agent_count"] == {
        "requested": 5,
        "effective": 5,
    }
    assert payload["wave_summary"][0] == {
        "wave": 1,
        "count": 5,
        "label": "start-workers",
    }
    assert payload["cost_estimate"]["method"] == "fast_heuristic"


def test_execute_swarm_ignores_caller_supplied_swarm_id(
    monkeypatch, tmp_path: Path
) -> None:
    _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "test-spoof")

    result = mcp_server.handle_execute_swarm(
        {"task": {"id": "t-override"}, "swarm_id": "attacker-choice"}
    )

    assert result["result"]["swarm_id"] != "attacker-choice"
    assert result["result"]["swarm_id"].startswith("swarm-")


def test_cost_estimate_completes_quickly(monkeypatch, tmp_path: Path) -> None:
    _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "test-cost")
    monkeypatch.setattr(
        mcp_server,
        "prepare_swarm_execution_request",
        lambda args, **_kwargs: {
            "swarm_id": "swarm-fixed",
            "requested_agents": 3,
            "effective_agents": 3,
            "clamped": False,
            "requested_vs_effective_agent_count": {
                "requested": 3,
                "effective": 3,
            },
            "topology": "dag",
        },
    )

    started = time.monotonic()
    result = mcp_server.handle_execute_swarm({"task": {"id": "t-2"}, "max_agents": 3})
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert result["result"]["cost_estimate"]["method"] == "fast_heuristic"


def test_input_size_cap_triggers_rejection(monkeypatch, tmp_path: Path) -> None:
    _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()

    result = mcp_server.handle_execute_swarm({"task": "x" * 10_001})

    assert result == {
        "error": "input_too_large",
        "details": "task must be <= 10000 characters when JSON-encoded",
    }


def test_budget_limit_must_be_finite(monkeypatch, tmp_path: Path) -> None:
    _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()

    result = mcp_server.handle_execute_swarm(
        {"task": {"id": "t-3"}, "budget_limit": float("nan")}
    )

    assert result == {
        "error": "invalid_request",
        "details": "budget_limit must be a finite number",
    }


def test_execute_swarm_rejects_empty_task_text(monkeypatch, tmp_path: Path) -> None:
    _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()

    result = mcp_server.handle_execute_swarm({"task": {"task": "   "}})

    assert result == {
        "error": "invalid_request",
        "details": "task must not be empty",
    }


def test_normalize_execute_swarm_task_text_uses_stable_json_for_nested_task() -> None:
    assert mcp_server._normalize_execute_swarm_task_text(
        {"task": {"id": "nested", "priority": 1}}
    ) == "{\"id\":\"nested\",\"priority\":1}"


def test_request_fingerprint_rejects_unsupported_nested_values(
    monkeypatch, tmp_path: Path
) -> None:
    _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()

    result = mcp_server.handle_execute_swarm(
        {"task": {"id": "t-unsupported", "bad": {1, 2}}}
    )

    assert result == {
        "error": "invalid_request",
        "details": "task must be JSON-serializable",
    }


def test_rate_limiter_triggers_conservative_path(monkeypatch, tmp_path: Path) -> None:
    _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "rate-limited-caller")

    result = None
    for idx in range(6):
        result = mcp_server.handle_execute_swarm({"task": {"id": f"t-{idx}"}})

    assert result is not None
    assert result["result"]["rate_limited"] is True
    assert result["result"]["cost_estimate"]["method"] == "fast_heuristic"


def test_execute_swarm_init_failures_are_controlled(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = mcp_server.handle_execute_swarm({"task": {"id": "t-4"}})

    assert result == {
        "error": "execution_error",
        "details": "execute_swarm initialization failed",
    }


def test_execute_swarm_host_native_skips_runtime_handoff(monkeypatch, tmp_path: Path) -> None:
    db = _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

    class FakePlan:
        total_agents = 2
        topology = "linear"

    class FakePlanner:
        def plan(self, _task_text: str) -> FakePlan:
            return FakePlan()

        def plan_to_dict(self, _plan: FakePlan) -> dict[str, object]:
            return {
                "subtasks": [
                    {"id": "st-1", "description": "auth module", "tier": "medium", "depends_on": []},
                    {"id": "st-2", "description": "tests", "tier": "low", "depends_on": ["st-1"]},
                ],
                "waves": [["st-1"], ["st-2"]],
                "topology": "linear",
            }

    handoff_calls: list[str] = []

    def _record_handoff(_db, swarm_id, *_args, **_kwargs) -> None:
        handoff_calls.append(swarm_id)

    monkeypatch.setattr(mcp_server, "_spawn_execute_swarm_runtime_handoff", _record_handoff)
    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (TGsConfig(db_path=tmp_path / "execute-swarm.db"), db, None, FakePlanner(), None),
    )

    result = mcp_server.handle_execute_swarm({"task": "refactor auth module", "max_agents": 2})

    assert handoff_calls == []
    assert result["started"] is False
    payload = result["result"]
    assert payload["host_execution_mode"] == "host_native"
    assert payload["awaiting_host_execution"] is True
    assert isinstance(payload.get("host_spawn_waves"), list)
    assert payload["host_spawn_waves"]
