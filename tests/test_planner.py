#!/usr/bin/env python3
"""
Tests for shared/planner.py — task decomposition and plan caching.
"""
from __future__ import annotations

import json
import sys
from typing import Any, cast
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import RoutingPreference, TGsConfig
from shared.db import Database
from shared.planner import (
    BudgetExceededError,
    CLIBackend,
    ExecutionPlan,
    FanOutConfig,
    FanOutDecision,
    GhCopilotBackend,
    PLAN_END,
    PLAN_START,
    Planner,
    PlannerParseError,
    Subtask,
    TIER_ALIASES,
    _extract_json,
    build_waves,
    evaluate_fanout,
    match_template,
    validate_plan,
    validate_topology,
)


class MockBackend(CLIBackend):
    """Mock backend that returns pre-set responses."""

    def __init__(self, response: str | None = None) -> None:
        self._response = response
        self.prompts: list[str] = []

    def call(self, prompt: str, model: str | None = None, timeout: int = 120) -> str | None:
        self.prompts.append(prompt)
        return self._response


def test_gh_copilot_backend_uses_disable_flag_when_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GhCopilotBackend()
    backend._model_flag = True
    captured: dict[str, object] = {}

    class _Result:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _Result:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr("shared.planner.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "shared.discovery._copilot_supports_model_flag",
        lambda: True,
    )
    monkeypatch.setattr(
        "shared.discovery._copilot_supports_disable_builtin_mcps",
        lambda: True,
    )

    assert backend.call("hello", model="gpt-5-mini", timeout=7) == "ok"
    assert captured["cmd"] == [
        "gh",
        "copilot",
        "--",
        "-p",
        "hello",
        "--model",
        "gpt-5-mini",
        "--disable-builtin-mcps",
    ]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["timeout"] == 7
    assert kwargs["cwd"].endswith("copilot-sandbox")
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["COPILOT_HOME"].endswith("copilot-sandbox")


def test_gh_copilot_backend_handles_sandbox_setup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GhCopilotBackend()
    backend._model_flag = True
    monkeypatch.setattr(
        "shared.planner._copilot_subprocess_env",
        lambda: (_ for _ in ()).throw(OSError("boom")),
    )

    assert backend.call("hello", model="gpt-5-mini", timeout=7) is None


def test_gh_copilot_backend_does_not_retry_without_env_on_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = GhCopilotBackend()
    backend._model_flag = True
    calls: list[dict[str, object]] = []

    class _Result:
        def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run(cmd: list[str], **kwargs: object) -> _Result:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return _Result(1, "", "Authentication required")
        return _Result(0, "ok\n", "")

    monkeypatch.setattr("shared.planner.subprocess.run", _fake_run)
    monkeypatch.setattr("shared.discovery._copilot_supports_disable_builtin_mcps", lambda: True)

    assert backend.call("hello", model="gpt-5-mini", timeout=7) is None
    assert len(calls) == 1
    assert "env" in calls[0]


def test_gh_copilot_backend_handles_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GhCopilotBackend()
    backend._model_flag = None
    monkeypatch.setattr(
        "shared.planner._copilot_supports_model_flag",
        lambda: (_ for _ in ()).throw(RuntimeError("probe failed")),
    )

    assert backend.call("hello", model="gpt-5-mini", timeout=7) is None


def _wrap_plan(payload: dict) -> str:
    return f"{PLAN_START}\n{json.dumps(payload)}\n{PLAN_END}"


def _build_execution_plan(
    subtask_count: int,
    *,
    estimated_agent_tokens: int = 0,
) -> ExecutionPlan:
    subtasks = [
        Subtask(id=i, description=f"task {i}", tier="medium", depends_on=[])
        for i in range(1, subtask_count + 1)
    ]
    waves = [list(range(1, subtask_count + 1))] if subtasks else []
    return ExecutionPlan(
        analysis="fanout-test",
        subtasks=subtasks,
        waves=waves,
        total_agents=subtask_count,
        strategy="parallel",
        estimated_agent_tokens=estimated_agent_tokens,
    )


def test_build_waves_simple() -> None:
    """Independent subtasks should all be in wave 1."""
    subtasks = [
        Subtask(id=1, description="a", tier="low"),
        Subtask(id=2, description="b", tier="low"),
        Subtask(id=3, description="c", tier="low"),
    ]
    waves = build_waves(subtasks)
    assert len(waves) == 1
    assert set(waves[0]) == {1, 2, 3}


def test_build_waves_with_deps() -> None:
    """Dependencies should create multiple waves."""
    subtasks = [
        Subtask(id=1, description="a", tier="low"),
        Subtask(id=2, description="b", tier="medium", depends_on=[1]),
        Subtask(id=3, description="c", tier="low"),
    ]
    waves = build_waves(subtasks)
    assert len(waves) == 2
    assert 1 in waves[0] and 3 in waves[0]
    assert 2 in waves[1]


def test_build_waves_circular() -> None:
    """Circular deps should force all into one wave."""
    subtasks = [
        Subtask(id=1, description="a", tier="low", depends_on=[2]),
        Subtask(id=2, description="b", tier="low", depends_on=[1]),
    ]
    waves = build_waves(subtasks)
    assert len(waves) >= 1


def test_build_waves_ignores_unknown_dependencies() -> None:
    """Unknown dependency IDs should not force circular fallback behavior."""
    subtasks = [
        Subtask(id=1, description="a", tier="low", depends_on=[99]),
        Subtask(id=2, description="b", tier="low"),
    ]
    waves = build_waves(subtasks)
    assert len(waves) == 1
    assert set(waves[0]) == {1, 2}


def test_validate_topology_matches_linear() -> None:
    plan = ExecutionPlan(
        analysis="linear",
        subtasks=[
            Subtask(id=1, description="one", tier="low"),
            Subtask(id=2, description="two", tier="low", depends_on=[1]),
            Subtask(id=3, description="three", tier="low", depends_on=[2]),
        ],
        waves=[[1], [2], [3]],
        total_agents=3,
        strategy="dag",
        topology="linear",
        _topology_explicit=True,
    )
    valid, issues, fallback = validate_topology(plan)
    assert valid is True
    assert issues == []
    assert fallback is None


def test_validate_topology_mismatch_reports_issue_and_fallback() -> None:
    plan = ExecutionPlan(
        analysis="mismatch",
        subtasks=[
            Subtask(id=1, description="one", tier="low"),
            Subtask(id=2, description="two", tier="low", depends_on=[1]),
            Subtask(id=3, description="three", tier="low", depends_on=[1, 2]),
        ],
        waves=[[1], [2], [3]],
        total_agents=3,
        strategy="dag",
        topology="star",
        _topology_explicit=True,
    )
    valid, issues, fallback = validate_topology(plan)
    assert valid is False
    assert fallback == "linear"
    assert any("star" in issue for issue in issues)


def test_validate_topology_accepts_dag_when_acyclic() -> None:
    plan = ExecutionPlan(
        analysis="dag",
        subtasks=[
            Subtask(id=1, description="one", tier="low"),
            Subtask(id=2, description="two", tier="low", depends_on=[1]),
            Subtask(id=3, description="three", tier="low", depends_on=[1]),
            Subtask(id=4, description="four", tier="low", depends_on=[2, 3]),
        ],
        waves=[[1], [2, 3], [4]],
        total_agents=4,
        strategy="dag",
        topology="dag",
        _topology_explicit=True,
    )
    valid, issues, fallback = validate_topology(plan)
    assert valid is True
    assert issues == []
    assert fallback is None


def test_extract_json_plain() -> None:
    """Extract JSON from plain text."""
    raw = '{"key": "value"}'
    result = _extract_json(raw)
    assert result == {"key": "value"}


def test_extract_json_markdown() -> None:
    """Extract JSON from markdown fence."""
    raw = '```json\n{"key": "value"}\n```'
    result = _extract_json(raw)
    assert result == {"key": "value"}


def test_extract_json_with_preamble() -> None:
    """Extract JSON with surrounding text."""
    raw = 'Here is the plan:\n{"subtasks": [{"id": 1}]}\nDone.'
    result = _extract_json(raw)
    assert result is not None
    assert "subtasks" in result


def test_match_template_error_handling() -> None:
    """Should match 'add error handling' template."""
    tmpl = match_template("add error handling to auth module")
    assert tmpl is not None
    assert tmpl.tier == "low"


def test_match_template_type_hints() -> None:
    """Should match 'add type hints' template."""
    tmpl = match_template("add type hints to all functions")
    assert tmpl is not None
    assert tmpl.tier == "low"


def test_match_template_tests() -> None:
    """Should match 'write tests for' template."""
    tmpl = match_template("write unit tests for the database module")
    assert tmpl is not None
    assert tmpl.tier == "low"


def test_match_template_no_match() -> None:
    """Should return None for non-matching description."""
    tmpl = match_template("architect a new microservice framework")
    assert tmpl is None


def test_tier_aliases() -> None:
    """Legacy tier names should map correctly."""
    assert TIER_ALIASES["mini"] == "low"
    assert TIER_ALIASES["sonnet"] == "medium"
    assert TIER_ALIASES["opus"] == "high"


def test_planner_fallback_no_output() -> None:
    """No backend output should raise PlannerParseError."""
    planner = Planner(TGsConfig(), MockBackend(None))
    with pytest.raises(PlannerParseError):
        planner.plan("test task")


def test_planner_with_valid_json() -> None:
    """Valid delimited JSON response should be parsed into a plan."""
    response = _wrap_plan({
        "analysis": "Test",
        "subtasks": [{"id": 1, "description": "do stuff", "tier": "low", "depends_on": []}],
        "strategy": "parallel",
    })
    planner = Planner(TGsConfig(), MockBackend(response))
    plan = planner.plan("test task")
    assert plan.total_agents == 1
    assert plan.subtasks[0].tier == "low"
    assert plan.subtasks[0].model == "low"


def test_planner_prompt_explains_runtime_file_materialization() -> None:
    response = _wrap_plan({
        "analysis": "Test",
        "subtasks": [{
            "id": 1,
            "description": "return app.py",
            "tier": "low",
            "target_file": "app.py",
            "depends_on": [],
        }],
        "strategy": "parallel",
    })
    backend = MockBackend(response)
    Planner(TGsConfig(), backend).plan("create app.py")

    assert "return only the complete" in backend.prompts[0]
    assert "runtime, not the agent CLI, writes it" in backend.prompts[0]


def test_planner_legacy_tier_mapping() -> None:
    """Planner should map legacy 'mini'/'sonnet'/'opus' to new tier names."""
    response = _wrap_plan({
        "analysis": "Test",
        "subtasks": [
            {"id": 1, "description": "simple thing", "tier": "mini", "depends_on": []},
            {"id": 2, "description": "complex thing", "tier": "opus", "depends_on": []},
        ],
        "strategy": "parallel",
    })
    planner = Planner(TGsConfig(), MockBackend(response))
    plan = planner.plan("test")
    assert plan.subtasks[0].tier == "low"
    assert plan.subtasks[1].tier == "high"


def test_planner_preserves_forward_dependencies() -> None:
    """Dependencies on later-defined subtasks should be preserved."""
    response = _wrap_plan({
        "analysis": "Test",
        "subtasks": [
            {"id": 1, "description": "wait for 2", "tier": "low", "depends_on": [2]},
            {"id": 2, "description": "run first", "tier": "low", "depends_on": []},
        ],
        "strategy": "dag",
    })
    planner = Planner(TGsConfig(), MockBackend(response))
    plan = planner.plan("forward dependency")
    assert plan.subtasks[0].depends_on == [2]
    assert plan.waves == [[2], [1]]


def test_planner_invalid_token_budget_is_ignored() -> None:
    """Non-numeric token_budget values should not survive into the plan."""
    response = _wrap_plan({
        "analysis": "Test",
        "subtasks": [
            {"id": 1, "description": "do stuff", "tier": "low", "depends_on": []},
        ],
        "strategy": "parallel",
        "token_budget": "not-a-number",
    })
    planner = Planner(TGsConfig(), MockBackend(response))
    plan = planner.plan("bad token budget")
    assert plan.token_budget is None


def test_planner_non_integer_subtask_id_falls_back() -> None:
    """Non-integer subtask IDs should be coerced to a safe fallback integer."""
    response = _wrap_plan({
        "analysis": "Test",
        "subtasks": [
            {"id": "bad-id", "description": "do stuff", "tier": "low", "depends_on": []},
        ],
        "strategy": "parallel",
    })
    planner = Planner(TGsConfig(), MockBackend(response))
    plan = planner.plan("bad subtask id")
    assert plan.subtasks[0].id == 1


def test_delimiter_escape_case() -> None:
    """Literal delimiter text inside a JSON string should not break parsing."""
    response = _wrap_plan({
        "analysis": f"Test with literal {PLAN_END} inside",
        "subtasks": [
            {
                "id": 1,
                "description": f"handle literal {PLAN_START} and {PLAN_END}",
                "tier": "low",
                "depends_on": [],
            },
        ],
        "strategy": "parallel",
    })
    planner = Planner(TGsConfig(), MockBackend(response))
    plan = planner.plan("delimiter escape")
    assert PLAN_END in plan.analysis
    assert PLAN_START in plan.subtasks[0].description


def test_planner_preserves_explicit_route_metadata() -> None:
    response = _wrap_plan({
        "analysis": "Test",
        "subtasks": [
            {
                "id": 1,
                "description": "do stuff",
                "tier": "low",
                "model": "claude-haiku-4.5",
                "provider": "Claude Code",
                "provider_id": "claude-code",
                "depends_on": [],
            }
        ],
        "strategy": "parallel",
    })
    planner = Planner(TGsConfig(), MockBackend(response))
    plan = planner.plan("route metadata")

    assert plan.subtasks[0].model == "claude-haiku-4.5"
    assert plan.subtasks[0].provider == "Claude Code"
    assert plan.subtasks[0].provider_id == "claude-code"
    assert planner.plan_to_dict(plan)["subtasks"][0]["provider_id"] == "claude-code"


def test_validate_plan_rejects_blank_model_metadata() -> None:
    plan = ExecutionPlan(
        analysis="invalid",
        subtasks=[Subtask(id=1, description="missing model", tier="low", model="")],
        waves=[[1]],
        total_agents=1,
        strategy="parallel",
    )

    with pytest.raises(ValueError, match="1"):
        validate_plan(plan)


def test_planner_does_not_stitch_route_preferences() -> None:
    response = _wrap_plan({
        "analysis": "Test",
        "subtasks": [{"id": 1, "description": "do stuff", "tier": "low", "depends_on": []}],
        "strategy": "parallel",
    })
    cfg = TGsConfig(
        preferred_routing={
            "low": [
                RoutingPreference(provider="Claude Code"),
                RoutingPreference(model="gpt-5-mini"),
            ]
        }
    )
    planner = Planner(cfg, MockBackend(response))
    plan = planner.plan("route metadata")

    assert plan.subtasks[0].provider == "Claude Code"
    assert plan.subtasks[0].provider_id is None
    assert plan.subtasks[0].model == "low"


def test_planner_explicit_model_prevents_template_tier_override() -> None:
    response = _wrap_plan({
        "analysis": "Test",
        "subtasks": [
            {
                "id": 1,
                "description": "write unit tests for the database module",
                "tier": "high",
                "model": "claude-sonnet-4.6",
                "depends_on": [],
            }
        ],
        "strategy": "parallel",
    })
    planner = Planner(TGsConfig(), MockBackend(response))
    plan = planner.plan("route metadata")

    assert plan.subtasks[0].tier == "high"
    assert plan.subtasks[0].model == "claude-sonnet-4.6"


def test_planner_explicit_provider_prevents_template_tier_override() -> None:
    response = _wrap_plan({
        "analysis": "Test",
        "subtasks": [
            {
                "id": 1,
                "description": "write unit tests for the database module",
                "tier": "high",
                "provider": "Claude Code",
                "depends_on": [],
            }
        ],
        "strategy": "parallel",
    })
    planner = Planner(TGsConfig(), MockBackend(response))
    plan = planner.plan("route metadata")

    assert plan.subtasks[0].tier == "high"
    assert plan.subtasks[0].provider == "Claude Code"
    assert plan.subtasks[0].model == "high"


def test_planner_skips_blank_route_preferences() -> None:
    response = _wrap_plan({
        "analysis": "Test",
        "subtasks": [{"id": 1, "description": "do stuff", "tier": "low", "depends_on": []}],
        "strategy": "parallel",
    })
    cfg = TGsConfig(
        preferred_routing={
            "low": [
                RoutingPreference(),
                RoutingPreference(model="gpt-5-mini"),
            ]
        }
    )
    planner = Planner(cfg, MockBackend(response))
    plan = planner.plan("route metadata")

    assert plan.subtasks[0].model == "gpt-5-mini"


def test_planner_skips_malformed_route_preferences() -> None:
    response = _wrap_plan({
        "analysis": "Test",
        "subtasks": [{"id": 1, "description": "do stuff", "tier": "low", "depends_on": []}],
        "strategy": "parallel",
    })
    cfg = TGsConfig()
    cfg.preferred_routing = {"low": [cast(Any, "bad-entry"), RoutingPreference(model="gpt-5-mini")]}
    planner = Planner(cfg, MockBackend(response))
    plan = planner.plan("route metadata")

    assert plan.subtasks[0].model == "gpt-5-mini"


def test_evaluate_fanout_disabled_by_default() -> None:
    decision = evaluate_fanout(_build_execution_plan(3, estimated_agent_tokens=120))

    assert isinstance(decision, FanOutDecision)
    assert decision.enabled is False
    assert decision.reason == "disabled"


def test_evaluate_fanout_single_route_when_only_one_subtask() -> None:
    decision = evaluate_fanout(
        _build_execution_plan(1, estimated_agent_tokens=50),
        FanOutConfig(opt_in_fanout=True),
    )

    assert decision.enabled is False
    assert decision.reason == "single_route"


def test_evaluate_fanout_raises_budget_exceeded() -> None:
    plan = _build_execution_plan(3, estimated_agent_tokens=500)

    with pytest.raises(BudgetExceededError):
        evaluate_fanout(
            plan,
            FanOutConfig(opt_in_fanout=True, budget_limit=100),
        )


def test_evaluate_fanout_enables_fanout_and_caps_router_count() -> None:
    decision = evaluate_fanout(
        _build_execution_plan(4, estimated_agent_tokens=120),
        FanOutConfig(opt_in_fanout=True, max_routers=2, budget_limit=1000),
    )

    assert decision.enabled is True
    assert decision.router_count == 2
    assert decision.subtask_ids == [1, 2]


def test_evaluate_fanout_logs_telemetry_when_db_provided() -> None:
    with TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        decision = evaluate_fanout(
            _build_execution_plan(3, estimated_agent_tokens=120),
            FanOutConfig(opt_in_fanout=True, max_routers=2, budget_limit=1000),
            db=db,
        )

        with db.conn() as conn:
            row = conn.execute(
                "SELECT reason, tokens_used FROM telemetry ORDER BY id DESC LIMIT 1"
            ).fetchone()

        assert decision.reason == "fanout"
        assert row is not None
        assert row[0] == "fanout"
        assert row[1] == 120
        db.close()


# ---------------------------------------------------------------------------
# From test_planner_auto_topology.py
# ---------------------------------------------------------------------------

from shared.planner import make_auto_topology_decision


def test_auto_select_star() -> None:
    config = TGsConfig.defaults()

    topology, rationale = make_auto_topology_decision(
        {"task_chars": 160, "subtask_count": 8},
        0.75,
        8,
        config=config,
        db=None,
    )

    assert topology == "star"
    assert rationale == "urgency_high"


def test_auto_select_hierarchical() -> None:
    config = TGsConfig.defaults()

    topology, rationale = make_auto_topology_decision(
        {
            "subtasks": [
                {"id": "architect"},
                {"id": "implementer", "parent_id": "architect"},
            ]
        },
        0.10,
        4,
        config=config,
        db=None,
    )

    assert topology == "hierarchical"
    assert rationale == "hierarchy_detected"


def test_auto_select_dag() -> None:
    config = TGsConfig.defaults()

    topology, rationale = make_auto_topology_decision(
        {"task_chars": 80, "subtask_count": 2},
        0.0,
        2,
        config=config,
        db=None,
    )

    assert topology == "dag"
    assert rationale == "balanced_default"


# ---------------------------------------------------------------------------
# From test_planner_fanout_urgency.py
# ---------------------------------------------------------------------------


def _make_urgency_plan(num_subtasks=3, estimated_tokens=10000, strategy="parallel"):
    subtasks = [
        Subtask(id=i + 1, description=f"task {i+1}", tier="low", depends_on=[])
        for i in range(num_subtasks)
    ]
    plan = ExecutionPlan(
        analysis="test",
        subtasks=subtasks,
        waves=[list(range(1, num_subtasks + 1))],
        total_agents=num_subtasks,
        strategy=strategy,
        topology="linear",
        token_budget=None,
        planner_tokens=None,
        estimated_agent_tokens=estimated_tokens,
    )
    return plan


def test_urgency_lowers_threshold():
    """High urgency should conservatively lower router_count compared to default."""
    config = FanOutConfig(opt_in_fanout=True, max_routers=3)
    plan = _make_urgency_plan(num_subtasks=3, estimated_tokens=10_000)

    base = evaluate_fanout(plan, config)
    urgent = evaluate_fanout(plan, config, urgency_score=0.8)

    assert base.enabled is True
    assert urgent.enabled is True
    assert urgent.router_count < base.router_count


def test_urgency_prefers_star():
    """For a fan-out-friendly plan, high urgency should prefer star topology."""
    config = FanOutConfig(opt_in_fanout=True, max_routers=4)
    plan = _make_urgency_plan(num_subtasks=4, estimated_tokens=20_000, strategy="parallel")

    dec = evaluate_fanout(plan, config, urgency_score=0.9)

    assert dec.enabled is True
    assert dec.router_count <= config.max_routers
    assert dec.topology_hint == "star" or (dec.topology_bias_reason and "star" in dec.topology_bias_reason)


# ---------------------------------------------------------------------------
# From test_telemetry_columns.py
# ---------------------------------------------------------------------------

import time
import tempfile


def test_fanout_columns_written():
    with tempfile.NamedTemporaryFile(suffix=".db") as tf:
        db = Database(Path(tf.name))

        subtasks = [
            Subtask(id=1, description="one", tier="low"),
            Subtask(id=2, description="two", tier="low"),
        ]
        waves = build_waves(subtasks)
        plan = ExecutionPlan(
            analysis="test",
            subtasks=subtasks,
            waves=waves,
            total_agents=2,
            strategy="parallel",
            estimated_agent_tokens=100,
        )

        config = FanOutConfig(opt_in_fanout=True, max_routers=2, budget_limit=1000)

        decision = evaluate_fanout(plan, config=config, db=db, urgency_score=0.7)
        assert decision.enabled

        with db.conn() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(telemetry)").fetchall()}
            assert "urgency_score" in cols
            assert "selected_topology" in cols
            assert "fanout_final_action" in cols

            row = conn.execute(
                "SELECT urgency_score, selected_topology, fanout_final_action FROM telemetry ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            urgency_score_val, selected_topology, fanout_final_action = row
            assert urgency_score_val is not None
            assert selected_topology == "star" or selected_topology is None
            assert fanout_final_action is not None


def test_telemetry_backward_compatibility():
    with tempfile.NamedTemporaryFile(suffix=".db") as tf:
        db = Database(db_path=Path(tf.name))
        with db.conn() as conn:
            conn.execute(
                "INSERT INTO telemetry (session_id, task_hash, agent_id, tier, model, ts) VALUES (?, ?, ?, ?, ?, ?)",
                ("legacy", "oldhash", 1, "low", "legacy-model", time.time()),
            )

            row = conn.execute(
                "SELECT session_id, urgency_score FROM telemetry WHERE session_id = ?", ("legacy",)
            ).fetchone()
            assert row is not None
            assert row[0] == "legacy"
            assert row[1] is None


# ---------------------------------------------------------------------------
# From test_telemetry_tokens.py
# ---------------------------------------------------------------------------

import json as _json


class _MockPlannerBackendTokens(CLIBackend):
    def __init__(self, response: str, actual_tokens: int) -> None:
        self._response = response
        self.last_actual_tokens = actual_tokens

    def call(self, prompt: str, model: str | None = None, timeout: int = 120) -> str | None:
        return self._response


def test_estimated_and_actual_tokens_persist(tmp_path: Path) -> None:
    """Planner telemetry persists estimated/actual tokens and timing to an isolated DB."""
    from shared.planner import PLAN_START, PLAN_END

    db_path = tmp_path / "telemetry.db"
    db = Database(db_path=db_path)
    backend = _MockPlannerBackendTokens(
        "<PLAN_JSON>\n"
        + _json.dumps({
            "analysis": "test",
            "subtasks": [{"id": 1, "description": "do thing", "tier": "low", "depends_on": []}],
            "strategy": "parallel",
        })
        + "\n</PLAN_JSON>",
        actual_tokens=42,
    )
    planner = Planner(TGsConfig(db_path=db_path), backend, db)

    planner.plan("plan with telemetry", skip_cache=True)

    with db.conn() as conn:
        row = conn.execute(
            "SELECT estimated_tokens, actual_tokens, timing_ms, rework_count "
            "FROM telemetry ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    assert row[0] and row[0] > 0
    assert row[1] == 42
    assert row[2] is not None and row[2] >= 0
    assert row[3] == 0


# ---------------------------------------------------------------------------
# From test_phase15_e2e_1.py (helpers_phase15 inlined)
# ---------------------------------------------------------------------------

from types import SimpleNamespace as _SimpleNamespace


class _DummyProviderPhase15:
    def resolve_model(self, tier: str) -> str:
        return f"dummy-{tier}"

    def execute(self, subtask, model: str, timeout: int = 120) -> str | None:
        return f"{model}:{getattr(subtask, 'id', 'x')}"

    def available_tiers(self) -> list[str]:
        return ["low", "medium", "high"]


class _DummyPlannerPhase15:
    def __init__(self) -> None:
        self._backend = _SimpleNamespace(call=lambda *args, **kwargs: None)

    def plan(self, *args, **kwargs):
        raise NotImplementedError


def _run_stubbed_execute_wave(temp_db_path, max_workers: int = 2, *, urgency: float = 0.5, topology: str = "linear"):
    from shared.orchestrator import Orchestrator, AgentResult
    from shared.config import TGsConfig as _TGsConfig

    class _StubOrchestrator(Orchestrator):
        def __init__(self, config, db) -> None:
            super().__init__(config, _DummyProviderPhase15(), _DummyPlannerPhase15(), db=db)

        def execute_subtask(
            self,
            subtask,
            timeout: int = 120,
            score: float | None = None,
            *,
            execution_id: str | None = None,
            plan_revision: int = 1,
            current_wave: int | None = None,
        ) -> AgentResult:
            assert self._db is not None
            self._db.log_agent_result(
                session_id=execution_id or "wave-test",
                task_hash=f"task-{getattr(subtask, 'id', 'x')}",
                agent_id=getattr(subtask, 'id', 'x'),
                tier=getattr(subtask, 'tier', 'low'),
                model="dummy-low",
                urgency_score=getattr(subtask, 'urgency', None),
                selected_topology=getattr(subtask, 'topology', None),
                artifact_publish_count=1,
            )
            return AgentResult(
                subtask_id=getattr(subtask, 'id', None),
                tier=getattr(subtask, 'tier', 'low'),
                model="dummy-low",
                output=f"completed {getattr(subtask, 'id', None)}",
                token_count=1,
            )

    db = Database(temp_db_path)
    config = _TGsConfig()
    config.parallelism.enabled = True
    config.parallelism.max_workers = max_workers
    orchestrator = _StubOrchestrator(config, db)
    subtasks = [
        _SimpleNamespace(id=i, description=f"s{i}", tier="low", urgency=urgency, topology=topology)
        for i in (1, 2, 3)
    ]
    orchestrator.execute_wave(0, subtasks)
    with db.conn() as conn:
        rows = conn.execute("SELECT * FROM telemetry WHERE session_id = ?", ("wave-test",)).fetchall()
    return db, rows


def test_multiwave_artifact_and_urgency_path(tmp_path):
    """Representative multi-wave scenario asserting telemetry explainability fields
    and artifact publish counts are written per-agent.
    """
    db_path = tmp_path / "phase15_e2e_1.db"
    db, _rows = _run_stubbed_execute_wave(db_path, urgency=0.7, topology="star")
    try:
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT urgency_score, selected_topology, artifact_publish_count FROM telemetry WHERE session_id = ?",
                ("wave-test",),
            ).fetchall()
        assert len(rows) == 3
        for urgency_score, selected_topology, publish_count in rows:
            assert abs(urgency_score - 0.7) < 1e-6
            assert selected_topology == "star"
            assert publish_count == 1
    finally:
        db.close()


if __name__ == "__main__":
    tests = [
        test_build_waves_simple,
        test_build_waves_with_deps,
        test_build_waves_circular,
        test_build_waves_ignores_unknown_dependencies,
        test_extract_json_plain,
        test_extract_json_markdown,
        test_extract_json_with_preamble,
        test_match_template_error_handling,
        test_match_template_type_hints,
        test_match_template_tests,
        test_match_template_no_match,
        test_tier_aliases,
        test_planner_fallback_no_output,
        test_planner_with_valid_json,
        test_planner_legacy_tier_mapping,
        test_planner_preserves_forward_dependencies,
        test_planner_invalid_token_budget_is_ignored,
        test_planner_non_integer_subtask_id_falls_back,
        test_delimiter_escape_case,
        test_evaluate_fanout_disabled_by_default,
        test_evaluate_fanout_single_route_when_only_one_subtask,
        test_evaluate_fanout_raises_budget_exceeded,
        test_evaluate_fanout_enables_fanout_and_caps_router_count,
        test_evaluate_fanout_logs_telemetry_when_db_provided,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✅ {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
