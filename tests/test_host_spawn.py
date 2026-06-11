"""Tests for shared.host_spawn meta-harness v2 helpers."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shared.config import TGsConfig
from shared.host_spawn import (
    HOST_SPAWN_ERROR,
    build_host_native_required_response,
    build_host_spawn,
    build_host_spawn_waves,
    effective_swarm_host_execution_mode,
    host_tool_for_caller,
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
    assert waves[1]["agents"][0]["method"] == "direct_edit"
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
