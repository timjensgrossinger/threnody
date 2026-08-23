"""Tests for Antigravity host-native spawn integration."""
from __future__ import annotations

import pytest

from shared.config import TGsConfig
from shared.host_spawn import (
    HostSpawnSpec,
    build_host_spawn,
    derive_role_from_task,
    determine_workspace_mode,
    host_tool_for_caller,
    subagent_type_for_tier,
)


def test_host_tool_for_antigravity() -> None:
    assert host_tool_for_caller("antigravity") == "invoke_subagent"


def test_host_tool_for_claude_code() -> None:
    assert host_tool_for_caller("claude-code") == "Agent"


def test_host_tool_for_others() -> None:
    assert host_tool_for_caller("github-copilot") == "Task"
    assert host_tool_for_caller("codex") == "Task"
    assert host_tool_for_caller("cursor") == "Task"
    assert host_tool_for_caller(None) == "Task"


def test_subagent_type_for_tiers() -> None:
    assert subagent_type_for_tier("low") == "threnody-low"
    assert subagent_type_for_tier("medium") == "threnody-medium"
    assert subagent_type_for_tier("high") == "threnody-high"


class TestSmartWorkspacePicker:
    def test_read_only_returns_share(self) -> None:
        assert determine_workspace_mode({"read_only": True}) == "share"

    def test_many_files_returns_branch(self) -> None:
        assert determine_workspace_mode({
            "target_files": ["a.py", "b.py", "c.py", "d.py"],
        }) == "branch"

    def test_wildcard_returns_branch(self) -> None:
        assert determine_workspace_mode({
            "target_files": ["src/**/*.py"],
        }) == "branch"

    def test_no_files_returns_inherit(self) -> None:
        assert determine_workspace_mode({}) == "inherit"

    def test_few_files_returns_inherit(self) -> None:
        assert determine_workspace_mode({
            "target_files": ["main.py"],
        }) == "inherit"


class TestRoleDerivation:
    def test_review_keywords(self) -> None:
        assert derive_role_from_task("Review this code") == "Reviewer"
        assert derive_role_from_task("Audit the changes") == "Reviewer"
        assert derive_role_from_task("Analyze the flow") == "Reviewer"

    def test_design_keywords(self) -> None:
        assert derive_role_from_task("Design the architecture") == "Architect"
        assert derive_role_from_task("Plan the implementation") == "Architect"

    def test_test_keywords(self) -> None:
        assert derive_role_from_task("Write tests") == "Tester"
        assert derive_role_from_task("Add specs") == "Tester"

    def test_doc_keywords(self) -> None:
        assert derive_role_from_task("Update docs") == "Documenter"
        assert derive_role_from_task("Document the API") == "Documenter"

    def test_default_implementer(self) -> None:
        assert derive_role_from_task("Implement the feature") == "Implementer"
        assert derive_role_from_task("Fix the bug") == "Debugger"


class TestAgyNativeFormat:
    def test_to_agy_dict_basic(self) -> None:
        spec = HostSpawnSpec(
            tool="invoke_subagent",
            method="host_task",
            model="gemini-3.5-flash",
            subagent_type="threnody-low",
            prompt="Implement feature X",
            tier="low",
            workspace="inherit",
            role="Implementer",
            effort="low",
        )
        result = spec.to_agy_dict()
        assert result["TypeName"] == "low"
        assert result["Role"] == "Implementer"
        assert result["Prompt"] == "Implement feature X"
        assert result["Workspace"] == "inherit"
        assert result["Model"] == "gemini-3.5-flash"
        assert result["Effort"] == "low"

    def test_to_agy_dict_strips_threnody_prefix(self) -> None:
        spec = HostSpawnSpec(
            tool="invoke_subagent",
            method="host_task",
            model=None,
            subagent_type="threnody-medium",
            prompt="Do something",
            tier="medium",
        )
        result = spec.to_agy_dict()
        assert result["TypeName"] == "medium"

    def test_to_agy_dict_defaults_workspace(self) -> None:
        spec = HostSpawnSpec(
            tool="invoke_subagent",
            method="host_task",
            model=None,
            subagent_type="threnody-high",
            prompt="Review this",
            tier="high",
        )
        result = spec.to_agy_dict()
        assert "Workspace" in result

    def test_to_agy_dict_includes_id(self) -> None:
        spec = HostSpawnSpec(
            tool="invoke_subagent",
            method="host_task",
            model=None,
            subagent_type="threnody-low",
            prompt="task",
            tier="low",
            id="spawn-123",
        )
        result = spec.to_agy_dict()
        assert result["Id"] == "spawn-123"


class TestTranslationMethods:
    def test_to_claude_dict(self) -> None:
        spec = HostSpawnSpec(
            tool="invoke_subagent",
            method="host_task",
            model="gemini-3.5-flash",
            subagent_type="threnody-low",
            prompt="Do work",
            tier="low",
        )
        result = spec.to_claude_dict()
        assert result["tool"] == "Agent"
        assert result["subagent_type"] == "threnody-low"
        assert result["prompt"] == "Do work"

    def test_to_copilot_dict(self) -> None:
        spec = HostSpawnSpec(
            tool="invoke_subagent",
            method="host_task",
            model="gemini-3.5-flash",
            subagent_type="threnody-medium",
            prompt="Do work",
            tier="medium",
        )
        result = spec.to_copilot_dict()
        assert result["tool"] == "Task"
        assert result["subagent_type"] == "threnody-medium"

    def test_to_codex_dict_matches_copilot(self) -> None:
        spec = HostSpawnSpec(
            tool="invoke_subagent",
            method="host_task",
            model=None,
            subagent_type="threnody-low",
            prompt="task",
            tier="low",
        )
        assert spec.to_codex_dict() == spec.to_copilot_dict()

    def test_to_cursor_dict_matches_copilot(self) -> None:
        spec = HostSpawnSpec(
            tool="invoke_subagent",
            method="host_task",
            model=None,
            subagent_type="threnody-low",
            prompt="task",
            tier="low",
        )
        assert spec.to_cursor_dict() == spec.to_copilot_dict()

    def test_to_junie_dict_matches_copilot(self) -> None:
        spec = HostSpawnSpec(
            tool="invoke_subagent",
            method="host_task",
            model=None,
            subagent_type="threnody-low",
            prompt="task",
            tier="low",
        )
        assert spec.to_junie_dict() == spec.to_copilot_dict()

    def test_to_opencode_dict_matches_copilot(self) -> None:
        spec = HostSpawnSpec(
            tool="invoke_subagent",
            method="host_task",
            model=None,
            subagent_type="threnody-low",
            prompt="task",
            tier="low",
        )
        assert spec.to_opencode_dict() == spec.to_copilot_dict()


class TestBuildHostSpawnWithNewFields:
    def test_build_populates_workspace_role_effort(self) -> None:
        config = TGsConfig()
        spec = build_host_spawn(
            config=config,
            caller="antigravity",
            tier="low",
            prompt="Implement feature X in main.py",
            target_files=["main.py"],
        )
        assert spec.workspace == "inherit"
        assert spec.role == "Implementer"
        assert spec.effort == "low"

    def test_build_read_only_gets_share_workspace(self) -> None:
        config = TGsConfig()
        spec = build_host_spawn(
            config=config,
            caller="antigravity",
            tier="medium",
            prompt="Review this code",
            read_only=True,
        )
        assert spec.workspace == "share"
        assert spec.role == "Reviewer"

    def test_build_multi_file_gets_branch_workspace(self) -> None:
        config = TGsConfig()
        spec = build_host_spawn(
            config=config,
            caller="antigravity",
            tier="high",
            prompt="Refactor these modules",
            target_files=["a.py", "b.py", "c.py", "d.py"],
        )
        assert spec.workspace == "branch"

    def test_build_effort_mapping(self) -> None:
        config = TGsConfig()
        for tier, expected_effort in [("low", "low"), ("medium", "high"), ("high", "high")]:
            spec = build_host_spawn(
                config=config,
                caller="antigravity",
                tier=tier,
                prompt="task",
            )
            assert spec.effort == expected_effort, f"tier {tier} should have effort {expected_effort}"
