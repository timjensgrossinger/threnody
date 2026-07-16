#!/usr/bin/env python3
"""Tests for the Phase 36 execute_swarm MCP surface."""
from __future__ import annotations

import json
import time
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
from mcp_server import prepare_swarm_execution_request
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
    assert props["task"]["type"] == "string"
    assert props["task_spec"]["type"] == "object"
    assert "max_agents" in props
    assert "workspace_root" in props
    assert "budget_limit" in props
    assert "preview_token" in props
    assert tool["inputSchema"].get("required") == ["task"]


def test_coerce_execute_swarm_task_input_prefers_task_spec() -> None:
    payload = mcp_server._coerce_execute_swarm_task_input(
        {"task": "plain text", "task_spec": {"id": "structured"}}
    )
    assert payload == {"id": "structured"}


def test_coerce_execute_swarm_task_input_parses_json_string_task() -> None:
    payload = mcp_server._coerce_execute_swarm_task_input(
        {"task": '{"id":"json-task","priority":1}'}
    )
    assert payload == {"id": "json-task", "priority": 1}


def test_normalize_mcp_tool_arguments_migrates_object_task_to_task_spec() -> None:
    args = mcp_server._normalize_mcp_tool_arguments(
        "execute_swarm",
        {"task": {"id": "legacy-object-task"}, "max_agents": 2},
    )
    assert args["task_spec"] == {"id": "legacy-object-task"}
    assert "task" not in args


def test_normalize_mcp_tool_arguments_accepts_string_arguments() -> None:
    args = mcp_server._normalize_mcp_tool_arguments(
        "execute_swarm",
        "refactor auth module",
    )
    assert args == {"task": "refactor auth module"}


def test_execute_swarm_initial_response_shape(monkeypatch, tmp_path: Path) -> None:
    _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "test-initial")

    result = mcp_server.handle_execute_swarm(
        {"task": "structured swarm", "task_spec": {"id": "t-1"}, "max_agents": 5}
    )

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
        {"task_spec": {"id": "t-override"}, "swarm_id": "attacker-choice"}
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
        {"task_spec": {"id": "t-3"}, "budget_limit": float("nan")}
    )

    assert result == {
        "error": "invalid_request",
        "details": "budget_limit must be a finite number",
    }


def test_execute_swarm_rejects_empty_task_text(monkeypatch, tmp_path: Path) -> None:
    _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()

    result = mcp_server.handle_execute_swarm({"task_spec": {"task": "   "}})

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
        {"task_spec": {"id": "t-unsupported", "bad": {1, 2}}}
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
        plan_calls = 0
        plan_heuristic_calls = 0

        def plan(self, _task_text: str, **_kwargs) -> FakePlan:
            self.plan_calls += 1
            return FakePlan()

        def plan_heuristic(self, _task_text: str, **_kwargs) -> FakePlan:
            self.plan_heuristic_calls += 1
            return FakePlan()

        def plan_to_dict(self, _plan: FakePlan) -> dict[str, object]:
            return {
                "subtasks": [
                    {
                        "id": "st-1",
                        "description": "auth module",
                        "tier": "medium",
                        "depends_on": [],
                        "target_file": "shared/auth.py",
                    },
                    {"id": "st-2", "description": "tests", "tier": "low", "depends_on": ["st-1"]},
                ],
                "waves": [["st-1"], ["st-2"]],
                "topology": "linear",
            }

    handoff_calls: list[str] = []

    def _record_handoff(_db, swarm_id, *_args, **_kwargs) -> None:
        handoff_calls.append(swarm_id)

    monkeypatch.setattr(mcp_server, "_spawn_execute_swarm_runtime_handoff", _record_handoff)
    fake_planner = FakePlanner()
    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (TGsConfig(db_path=tmp_path / "execute-swarm.db"), db, None, fake_planner, None),
    )

    result = mcp_server.handle_execute_swarm({"task": "refactor auth module", "max_agents": 2})

    assert handoff_calls == []
    assert result["started"] is False
    payload = result["result"]
    assert payload["host_execution_mode"] == "host_native"
    assert payload["awaiting_host_execution"] is True
    assert isinstance(payload.get("host_spawn_waves"), list)
    assert payload["host_spawn_waves"]
    assert fake_planner.plan_calls == 0
    assert fake_planner.plan_heuristic_calls == 1
    assert payload["fast_start_target_ms"] == 30000
    latency = payload.get("latency_ms")
    assert isinstance(latency, dict)
    assert {"prepare_request", "plan", "attach_host_spawn", "persist_minimal", "total_to_handoff"} <= set(latency)
    assert latency["total_to_handoff"] < payload["fast_start_target_ms"]
    assert payload.get("host_execution_contract") == "spawn_subagents"
    assert payload["host_spawn_waves"][0]["agents"][0]["spawn_required"] is True
    assert payload["host_spawn_waves"][0]["agents"][0]["method"] == "host_task"
    guard = payload.get("routing_guard")
    assert isinstance(guard, dict)
    assert guard.get("mode") == "routed_plan"
    file_hints = guard.get("file_hints") or []
    assert any("auth.py" in str(hint) for hint in file_hints)


def test_execute_swarm_honors_workspace_root_arg(monkeypatch, tmp_path: Path) -> None:
    """workspace_root arg must flow into the handoff, routing_guard, and file_hints."""
    db = _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

    class FakePlan:
        total_agents = 1
        topology = "linear"

    class FakePlanner:
        def plan(self, _t: str, **_k) -> FakePlan:
            return FakePlan()

        def plan_heuristic(self, _t: str, **_k) -> FakePlan:
            return FakePlan()

        def plan_to_dict(self, _p: FakePlan) -> dict[str, object]:
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

    monkeypatch.setattr(
        mcp_server, "_spawn_execute_swarm_runtime_handoff", lambda *a, **k: None
    )
    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (TGsConfig(db_path=tmp_path / "ws.db"), db, None, FakePlanner(), None),
    )

    ws = str(tmp_path / "external-proj")
    result = mcp_server.handle_execute_swarm(
        {"task": "build calculator ops", "max_agents": 1, "workspace_root": ws}
    )
    payload = result["result"]
    assert payload["workspace_root"] == ws
    assert payload["learning_report_contract"]["workspace_root"] == ws
    guard = payload.get("routing_guard") or {}
    assert guard.get("cwd") == ws
    # file_hints resolve the relative target under the supplied workspace root.
    assert any(ws in str(hint) for hint in (guard.get("file_hints") or []))


# ---------------------------------------------------------------------------
# Topology rationale (moved from test_mcp_server_topology_explain.py)
# ---------------------------------------------------------------------------


def test_execute_swarm_auto_topology_exposes_rationale(monkeypatch, tmp_path: Path) -> None:
    _stub_init(monkeypatch, tmp_path)
    mcp_server._execute_swarm_rate_limit.clear()
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "topology-explain")

    result = mcp_server.handle_execute_swarm(
        {
            "task": "Incident blocked today, parallelize immediately.",
            "max_agents": 8,
            "urgency_hint": "ASAP outage today",
        }
    )

    assert result["started"] is True
    payload = result["result"]
    assert payload["swarm_id"].startswith("swarm-")
    assert payload["selected_topology"] == "star"
    assert payload["topology_rationale"] == "urgency_high"
    assert payload["requested_vs_effective_agent_count"] == {
        "requested": 8,
        "effective": 8,
    }
    assert payload["effective_values"]["topology"] == "star"


# ---------------------------------------------------------------------------
# Swarm cap enforcement (moved from test_swarm_config.py)
# ---------------------------------------------------------------------------


def test_prepare_request_propagates_workspace_root() -> None:
    """workspace_root arg must reach request_meta (not silently default to active root)."""
    with tempfile.NamedTemporaryFile(suffix=".db") as handle:
        db = Database(Path(handle.name))
        config = TGsConfig.defaults()

        prepared = prepare_swarm_execution_request(
            {"task": "build x", "workspace_root": "/tmp/swarm-demo"},
            config=config,
            db=db,
            swarm_id="swarm-ws-test",
        )
        assert prepared["workspace_root"] == "/tmp/swarm-demo"

        # Absent/blank arg normalizes to None so the caller falls back to active root.
        prepared_none = prepare_swarm_execution_request(
            {"task": "build x", "workspace_root": "  "},
            config=config,
            db=db,
            swarm_id="swarm-ws-none",
        )
        assert prepared_none["workspace_root"] is None


def test_max_agents_default_and_clamp() -> None:
    """Over-cap swarm requests should clamp to the hard cap and persist telemetry."""
    with tempfile.NamedTemporaryFile(suffix=".db") as handle:
        db = Database(Path(handle.name))
        config = TGsConfig.defaults()
        config.parallelism.swarm_max_agents = 12

        prepared = prepare_swarm_execution_request(
            {"max_agents": 20},
            config=config,
            db=db,
            swarm_id="swarm-cap-test",
        )

        assert config.swarm_max_agents == 12
        assert prepared["requested_agents"] == 20
        assert prepared["effective_agents"] == 12
        assert prepared["clamped"] is True
        assert prepared["requested_vs_effective_agent_count"] == {
            "requested": 20,
            "effective": 12,
        }

        with db.conn() as conn:
            run_row = conn.execute(
                """
                SELECT requested_agents, effective_agents
                FROM swarm_runs
                WHERE swarm_id = ?
                """,
                ("swarm-cap-test",),
            ).fetchone()
            event_row = conn.execute(
                """
                SELECT payload
                FROM swarm_events
                WHERE swarm_id = ? AND event_type = ?
                """,
                ("swarm-cap-test", "cap_event"),
            ).fetchone()

        assert run_row == (20, 12)
        assert event_row is not None
        payload = json.loads(event_row[0])
        assert payload["requested"] == 20
        assert payload["effective"] == 12
        db.close()


def test_default_swarm_max_agents_unlimited() -> None:
    """Default host-native swarms do not clamp explicit fanout by size."""
    with tempfile.NamedTemporaryFile(suffix=".db") as handle:
        db = Database(Path(handle.name))
        config = TGsConfig.defaults()

        prepared = prepare_swarm_execution_request(
            {"task": "FAST_REVIEW: " + " ".join(f"src/f{i}.py" for i in range(35)), "max_agents": 36},
            config=config,
            db=db,
            swarm_id="swarm-unlimited-test",
        )

        assert config.swarm_max_agents == -1
        assert prepared["requested_agents"] == 36
        assert prepared["effective_agents"] == 36
        assert prepared["clamped"] is False
        db.close()


def test_review_run_response_is_compact(monkeypatch, tmp_path: Path) -> None:
    """Review-intent host-native response drops the heavy plan + workflow_script
    but keeps host_spawn_waves, a plan_summary, and all learning side effects."""
    from shared.planner import CLIBackend, Planner

    class _NoBackend(CLIBackend):
        def call(self, prompt, model=None, timeout=120):  # pragma: no cover
            raise AssertionError("review heuristic must not call the LLM backend")

    db_path = tmp_path / "review-compact.db"
    cfg = TGsConfig(db_path=db_path)
    db = Database(db_path=db_path)
    db._init_schema(db._get_connection())
    planner = Planner(cfg, _NoBackend())

    f1 = tmp_path / "big.py"
    f1.write_text(
        "def handle(items):\n"
        + "\n".join(
            "    if items:\n"
            "        for item in items:\n"
            "            if item:\n"
            "                return item"
            for _ in range(180)
        ),
        encoding="utf-8",
    )
    f2 = tmp_path / "small.py"
    f2.write_text("\n".join(f"line {i}" for i in range(40)), encoding="utf-8")

    task = f"REVIEW: [dims=performance] {f1} {f2}"
    out = mcp_server._execute_swarm_host_native_response(
        config=cfg,
        db=db,
        planner=planner,
        router=None,
        swarm_id="swarm-review-compact",
        task_text=task,
        caller="claude-code",
        request_meta={"topology": "dag", "workspace_root": str(tmp_path)},
        estimated_cost=0.0,
    )
    result = out["result"]

    # Compact wire payload
    assert "host_spawn_waves" in result and result["host_spawn_waves"]
    assert "plan_summary" in result
    assert "plan" not in result
    assert "workflow_script" not in result
    assert result["plan_summary"]["subtask_count"] >= 1

    # Learning setup preserved: swarm row persisted
    with db.conn() as conn:
        swarm_row = conn.execute(
            "SELECT status FROM swarm_runs WHERE swarm_id = ?", ("swarm-review-compact",)
        ).fetchone()
    assert swarm_row is not None
    assert swarm_row[0] == "awaiting_host_execution"

    # Receipt keeps full plan fidelity server-side even though the wire is trimmed.
    # The persist runs in a background daemon thread — poll briefly to avoid a race.
    import time as _time
    for _ in range(40):
        if db.get_run_receipt("swarm-review-compact") is not None:
            break
        _time.sleep(0.05)
    assert db.get_run_receipt("swarm-review-compact") is not None

    # Tiering: large reasoning-heavy perf file → high; small file → low
    tiers = {
        a.get("tier")
        for w in result["host_spawn_waves"]
        for a in w.get("agents", [])
    }
    assert "high" in tiers and "low" in tiers  # mixed tiers, not all-medium
    db.close()
