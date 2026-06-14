"""Tests for shared.workflow_emit — Workflow JS script renderer."""
from __future__ import annotations

import json
import re

import pytest

from shared.config import RoutingPolicyConfig, ShellRoutingProfile, TGsConfig
from shared.host_spawn import workflow_emit_enabled
from shared.workflow_emit import (
    MAX_TOTAL_AGENTS,
    render_workflow_script,
    workflow_slug,
)


def _emit_config() -> TGsConfig:
    """A config with workflow emission opted in for claude-code."""
    cfg = TGsConfig.defaults()
    cfg.routing_policy = RoutingPolicyConfig(
        mode="custom",
        shells={
            "claude-code": ShellRoutingProfile(shell_id="claude-code", workflow_emit=True)
        },
    )
    return cfg


def _review_plan() -> dict:
    """A review-fanout-shaped plan: 3 parallel reviewers + 1 synthesis."""
    return {
        "analysis": "Review fanout: 1 file, 3 dimensions + synthesis.",
        "topology": "dag",
        "subtasks": [
            {
                "id": 1,
                "description": "Security review of app.py",
                "tier": "high",
                "target_file": "app.py",
                "subagent_type": "review-security",
                "read_only": True,
                "depends_on": [],
            },
            {
                "id": 2,
                "description": "Logic review of app.py",
                "tier": "medium",
                "target_file": "app.py",
                "subagent_type": "review-logic",
                "read_only": True,
                "depends_on": [],
            },
            {
                "id": 3,
                "description": "Edge review of app.py",
                "tier": "low",
                "target_file": "app.py",
                "subagent_type": "review-edge-cases",
                "read_only": True,
                "depends_on": [],
            },
            {
                "id": 4,
                "description": "Synthesize findings into a ranked report.",
                "tier": "high",
                "read_only": True,
                "depends_on": [1, 2, 3],
            },
        ],
        "waves": [[1, 2, 3], [4]],
    }


def _linear_plan() -> dict:
    return {
        "analysis": "Two sequential edits.",
        "topology": "linear",
        "subtasks": [
            {"id": 1, "description": "edit auth", "tier": "medium", "target_file": "auth.py"},
            {"id": 2, "description": "add tests", "tier": "low", "target_file": "t.py", "depends_on": [1]},
        ],
        "waves": [[1], [2]],
    }


def _render(plan: dict, task: str = "REVIEW: app.py") -> str:
    cfg = TGsConfig.defaults()
    return render_workflow_script(plan, config=cfg, caller="claude-code", task_text=task)


def test_meta_is_pure_literal_and_parseable() -> None:
    script = _render(_review_plan())
    m = re.search(r"export const meta = (\{.*?\})\n", script, re.DOTALL)
    assert m, "meta block not found"
    meta = json.loads(m.group(1))  # pure literal → valid JSON
    assert meta["name"].startswith("review-")
    assert isinstance(meta["phases"], list) and len(meta["phases"]) == 2


def test_every_agent_carries_tier_resolved_model() -> None:
    script = _render(_review_plan())
    # Default claude-code profile: high→opus, medium→sonnet, low→haiku.
    models = re.findall(r"model: \"([^\"]+)\"", script)
    # 4 agents → 4 model options.
    assert len(models) == 4
    assert set(models) == {"opus", "sonnet", "haiku"}
    # No agent() call should be missing a model (no bare opts without model in this plan).
    assert script.count("await agent(") == 4


def test_no_nondeterministic_calls() -> None:
    script = _render(_review_plan())
    for forbidden in ("Date.now(", "Math.random(", "new Date()"):
        assert forbidden not in script, f"emitted script contains forbidden {forbidden}"


def test_phases_match_waves() -> None:
    script = _render(_review_plan())
    phase_calls = re.findall(r"phase\(\"([^\"]+)\"\)", script)
    assert phase_calls == ["Wave 1", "Synthesis"]


def test_parallel_wave_and_single_wave_shapes() -> None:
    script = _render(_review_plan())
    # First wave (3 agents) → parallel([...]); synthesis (1 agent) → direct await.
    assert "await parallel([" in script
    assert "const r_4 = await agent(" in script


def test_dependency_results_injected_into_prompt() -> None:
    script = _render(_review_plan())
    # Synthesis (id 4) depends on 1,2,3 → its prompt must stringify those vars.
    assert "JSON.stringify([r_1, r_2, r_3]" in script


def test_read_only_agenttype_and_instruction() -> None:
    script = _render(_review_plan())
    assert 'agentType: "review-security"' in script
    assert "READ-ONLY" in script


def test_linear_plan_all_single_waves() -> None:
    script = _render(_linear_plan(), task="refactor auth module")
    # Both waves single-agent → no parallel(), two direct awaits.
    assert "await parallel(" not in script
    assert "const r_1 = await agent(" in script
    assert "const r_2 = await agent(" in script
    # Dependency injection on the second.
    assert "JSON.stringify([r_1]" in script


def test_telemetry_collection_and_return() -> None:
    script = _render(_review_plan())
    assert "const __agents = []" in script
    assert "return { workflow: meta.name, agents: __agents }" in script
    assert script.count("__agents.push(") == 4


def test_slug_deterministic_and_kebab() -> None:
    assert workflow_slug("REVIEW: src/app.py and src/db.py").startswith("review-")
    assert workflow_slug("Add JWT auth!!!") == "add-jwt-auth"
    assert workflow_slug("") == "threnody-workflow"
    # Deterministic — same input, same output.
    assert workflow_slug("hello world") == workflow_slug("hello world")


def test_missing_structure_raises() -> None:
    cfg = TGsConfig.defaults()
    with pytest.raises(ValueError):
        render_workflow_script({"subtasks": "nope"}, config=cfg, caller="claude-code")


def test_gate_enabled_only_for_optin_claude_code() -> None:
    default_cfg = TGsConfig.defaults()
    assert workflow_emit_enabled(default_cfg, "claude-code") is False  # off by default
    emit_cfg = _emit_config()
    assert workflow_emit_enabled(emit_cfg, "claude-code") is True
    # Other host shells have no Workflow-tool equivalent → always False.
    assert workflow_emit_enabled(emit_cfg, "github-copilot") is False
    assert workflow_emit_enabled(emit_cfg, "cursor") is False


def test_maybe_attach_workflow_script_wiring() -> None:
    import mcp_server

    plan = _review_plan()
    waves = [
        {"wave": 1, "agents": [{"id": "1"}, {"id": "2"}, {"id": "3"}]},
        {"wave": 2, "agents": [{"id": "4"}]},
    ]
    payload: dict = dict(plan)
    mcp_server._maybe_attach_workflow_script(
        payload, waves, config=_emit_config(), caller="claude-code", task="REVIEW: app.py"
    )
    assert payload.get("workflow_emit") is True
    assert payload.get("workflow_execution_contract") == "emit_workflow"
    assert "export const meta" in payload.get("workflow_script", "")

    # Disabled config → no emission.
    payload2: dict = dict(plan)
    mcp_server._maybe_attach_workflow_script(
        payload2, waves, config=TGsConfig.defaults(), caller="claude-code", task="REVIEW: app.py"
    )
    assert "workflow_script" not in payload2

    # Single-agent plan → no emission even when enabled.
    payload3: dict = dict(plan)
    mcp_server._maybe_attach_workflow_script(
        payload3,
        [{"wave": 1, "agents": [{"id": "1"}]}],
        config=_emit_config(),
        caller="claude-code",
        task="tiny",
    )
    assert "workflow_script" not in payload3


def test_consensus_phase_renders_persona_queens() -> None:
    script = render_workflow_script(
        _review_plan(), config=_emit_config(), caller="claude-code",
        task_text="REVIEW: app.py", include_consensus=True,
    )
    assert 'phase("Consensus")' in script
    assert "CONSENSUS_SCHEMA" in script
    assert "queen-correctness-first" in script
    assert "consensus: __consensus" in script  # returned for tally
    assert '"Consensus"' in script  # meta phase entry
    # Decision must NOT be computed in JS — only verdicts collected.
    assert "consensus_tally" not in script


def test_consensus_phase_is_deterministic() -> None:
    script = render_workflow_script(
        _review_plan(), config=_emit_config(), caller="claude-code",
        task_text="REVIEW: app.py", include_consensus=True,
    )
    for forbidden in ("Date.now(", "Math.random(", "new Date()"):
        assert forbidden not in script


def test_consensus_off_by_default() -> None:
    script = _render(_review_plan())  # include_consensus defaults False
    assert 'phase("Consensus")' not in script
    assert "__consensus" not in script


def test_consensus_in_workflow_gate() -> None:
    from shared.host_spawn import consensus_in_workflow_enabled
    from shared.config import RoutingPolicyConfig, ShellRoutingProfile

    # workflow_emit on but consensus_in_workflow off → False.
    assert consensus_in_workflow_enabled(_emit_config(), "claude-code") is False
    cfg = TGsConfig.defaults()
    cfg.routing_policy = RoutingPolicyConfig(
        mode="custom",
        shells={
            "claude-code": ShellRoutingProfile(
                shell_id="claude-code", workflow_emit=True, consensus_in_workflow=True
            )
        },
    )
    assert consensus_in_workflow_enabled(cfg, "claude-code") is True
    # consensus_in_workflow requires workflow_emit — both must be on.
    cfg2 = TGsConfig.defaults()
    cfg2.routing_policy = RoutingPolicyConfig(
        mode="custom",
        shells={
            "claude-code": ShellRoutingProfile(
                shell_id="claude-code", workflow_emit=False, consensus_in_workflow=True
            )
        },
    )
    assert consensus_in_workflow_enabled(cfg2, "claude-code") is False


def test_total_agent_cap_warning() -> None:
    # A plan over the runtime total cap should emit a warning comment.
    big = {
        "analysis": "huge",
        "topology": "dag",
        "subtasks": [
            {"id": i, "description": f"task {i}", "tier": "low"}
            for i in range(MAX_TOTAL_AGENTS + 5)
        ],
        "waves": [[i] for i in range(MAX_TOTAL_AGENTS + 5)],
    }
    cfg = TGsConfig.defaults()
    script = render_workflow_script(big, config=cfg, caller="claude-code", task_text="big")
    assert "exceeds the runtime cap" in script
