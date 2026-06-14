"""Tests for shared.host_spawn meta-harness v2 helpers."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shared.config import TGsConfig
from shared.host_spawn import (
    HOST_SPAWN_ERROR,
    HOST_EXECUTION_CONTRACT,
    build_host_native_required_response,
    build_host_spawn,
    build_host_spawn_waves,
    enrich_host_spawn_waves,
    effective_swarm_host_execution_mode,
    host_tool_for_caller,
    sanitize_plan_for_host,
    would_self_delegate,
)


def test_host_tool_for_caller() -> None:
    assert host_tool_for_caller("claude-code") == "Agent"
    assert host_tool_for_caller("Claude") == "Agent"
    assert host_tool_for_caller("cursor") == "Task"
    assert host_tool_for_caller("github-copilot") == "Task"


def test_build_host_spawn_waves_from_plan() -> None:
    cfg = TGsConfig.defaults()
    plan = {
        "subtasks": [
            {"id": 1, "description": "edit auth", "tier": "medium", "model": "sonnet"},
            {"id": 2, "description": "add tests", "tier": "low", "target_file": "tests/test_auth.py"},
        ],
        "waves": [[1], [2]],
    }
    waves = build_host_spawn_waves(plan, config=cfg, caller="claude-code")
    assert len(waves) == 2
    assert waves[0]["agents"][0]["tool"] == "Agent"
    assert waves[0]["agents"][0]["subagent_type"] == "threnody-medium"
    assert waves[1]["agents"][0]["method"] == "host_task"
    assert waves[1]["agents"][0]["spawn_required"] is True
    assert waves[1]["execution_contract"] == "spawn_subagents"
    assert waves[1]["agents"][0]["target_files"] == ["tests/test_auth.py"]


def test_would_self_delegate_blocks_same_host_without_provider_id() -> None:
    provider = SimpleNamespace(name="claude-code", display_name="Claude Code")
    registry = MagicMock()
    registry._ordered_execution_candidates.return_value = ([provider], [])
    registry._caller_matches_provider.return_value = True
    assert would_self_delegate(registry, caller="claude-code", tier="medium") is True


def test_would_self_delegate_allows_cross_backend_provider_id() -> None:
    copilot = SimpleNamespace(name="github-copilot", display_name="GitHub Copilot")
    registry = MagicMock()
    registry._ordered_execution_candidates.return_value = ([copilot], [])
    registry._caller_identifiers.return_value = {"claude-code", "claude"}
    registry._provider_identifiers.return_value = {"github-copilot", "copilot"}
    assert (
        would_self_delegate(
            registry,
            caller="claude-code",
            tier="low",
            provider_id="github-copilot",
        )
        is False
    )


def test_build_host_native_required_response_shape() -> None:
    cfg = TGsConfig.defaults()
    payload = build_host_native_required_response(
        config=cfg,
        caller="cursor",
        tier="medium",
        prompt="refactor module",
        delegation_targets=["opencode"],
    )
    assert payload["error"] == HOST_SPAWN_ERROR
    assert payload["host_spawn"]["tool"] == "Task"
    assert payload["delegation_targets"] == ["opencode"]


def test_effective_swarm_host_execution_mode_defaults_host_native_for_hosts() -> None:
    cfg = TGsConfig.defaults()
    assert effective_swarm_host_execution_mode(cfg, "claude-code") == "host_native"
    assert effective_swarm_host_execution_mode(cfg, "external-caller") == "delegate"


def test_effective_swarm_host_execution_mode_per_caller_override() -> None:
    cfg = TGsConfig.defaults()
    cfg.swarm_host_execution_mode = "host_native"
    cfg.swarm_host_execution_mode_by_caller = {"claude-code": "delegate"}
    assert effective_swarm_host_execution_mode(cfg, "claude-code") == "delegate"


def test_enrich_host_spawn_waves_forces_host_task_contract() -> None:
    waves = enrich_host_spawn_waves(
        [
            {
                "wave": 1,
                "parallel": True,
                "agents": [
                    {"id": "1", "method": "direct_edit", "tier": "low"},
                    {"id": "2", "method": "direct_edit", "tier": "low"},
                ],
            }
        ]
    )
    assert waves[0]["execution_contract"] == HOST_EXECUTION_CONTRACT
    for agent in waves[0]["agents"]:
        assert agent["method"] == "host_task"
        assert agent["spawn_required"] is True


# ---- sanitize_plan_for_host: workspace-containment + fragment safety gate ----


def test_sanitize_strips_out_of_root_target(tmp_path) -> None:
    root = str(tmp_path)
    plan = {
        "subtasks": [
            {
                "id": 1,
                "description": "Update the home file as described in the task.",
                "tier": "medium",
                "target_file": "/Users/someuser/secret.py",
            }
        ],
        "waves": [[1]],
    }
    report = sanitize_plan_for_host(plan, workspace_root=root, task="do work")
    # Target escapes root -> stripped, but coherent prompt keeps the subtask.
    assert plan["subtasks"][0].get("target_file") is None
    assert any(d["id"] == 1 for d in report["dropped_targets"])
    assert plan["waves"] == [[1]]


def test_sanitize_drops_fragment_prompt_subtask(tmp_path) -> None:
    plan = {
        "subtasks": [
            {"id": 1, "description": "someuser/", "tier": "low",
             "target_file": "/Users/someuser"},
            {"id": 2, "description": "Implement the parser module fully.",
             "tier": "medium", "target_file": "src/parser.py"},
        ],
        "waves": [[1, 2]],
    }
    sanitize_plan_for_host(plan, workspace_root=str(tmp_path), task="build parser")
    ids = [st["id"] for st in plan["subtasks"]]
    assert ids == [2]
    assert plan["waves"] == [[2]]


def test_sanitize_collapses_to_single_agent_when_all_unsafe(tmp_path) -> None:
    plan = {
        "subtasks": [
            {"id": 1, "description": "someuser/", "tier": "low",
             "target_file": "/Users/someuser"},
            {"id": 2, "description": "plans/", "tier": "low",
             "target_file": "/Users/someuser/.claude/plans/x.md"},
        ],
        "waves": [[1, 2]],
        "topology": "dag",
    }
    task = "Refactor the tightly-coupled coordinator and queen modules together."
    report = sanitize_plan_for_host(plan, workspace_root=str(tmp_path), task=task)
    assert report["collapsed_to_single"] is True
    assert len(plan["subtasks"]) == 1
    assert plan["subtasks"][0]["description"] == task
    assert plan["waves"] == [[1]]
    assert plan["topology"] == "linear"


def test_sanitize_leaves_clean_fanout_untouched(tmp_path) -> None:
    plan = {
        "subtasks": [
            {"id": 1, "description": "Build module a fully.", "tier": "low",
             "target_file": "a.py"},
            {"id": 2, "description": "Build module b fully.", "tier": "low",
             "target_file": "b.py"},
            {"id": 3, "description": "Build module c fully.", "tier": "low",
             "target_file": "c.py"},
        ],
        "waves": [[1, 2, 3]],
    }
    report = sanitize_plan_for_host(plan, workspace_root=str(tmp_path), task="build")
    assert report["collapsed_to_single"] is False
    assert not report["dropped_targets"]
    assert not report["dropped_subtasks"]
    assert [st["target_file"] for st in plan["subtasks"]] == ["a.py", "b.py", "c.py"]
    assert plan["waves"] == [[1, 2, 3]]


def test_sanitize_keeps_read_only_external_target(tmp_path) -> None:
    # Read-only review subtasks may legitimately target absolute, out-of-root files.
    plan = {
        "subtasks": [
            {
                "id": 1,
                "description": "Security review of the auth module.",
                "tier": "high",
                "read_only": True,
                "target_file": "/Users/someuser/repo/auth.py",
            }
        ],
        "waves": [[1]],
    }
    report = sanitize_plan_for_host(plan, workspace_root=str(tmp_path), task="review")
    assert plan["subtasks"][0]["target_file"] == "/Users/someuser/repo/auth.py"
    assert not report["dropped_targets"]


def test_sanitize_prunes_dropped_id_from_depends_on(tmp_path) -> None:
    plan = {
        "subtasks": [
            {"id": 1, "description": "x/", "tier": "low",
             "target_file": "/etc/passwd"},
            {"id": 2, "description": "Wire the integration layer together.",
             "tier": "medium", "target_file": "main.py", "depends_on": [1]},
        ],
        "waves": [[1], [2]],
    }
    sanitize_plan_for_host(plan, workspace_root=str(tmp_path), task="integrate")
    survivors = {st["id"]: st for st in plan["subtasks"]}
    assert set(survivors) == {2}
    assert survivors[2]["depends_on"] == []
    assert plan["waves"] == [[2]]
