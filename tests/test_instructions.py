#!/usr/bin/env python3
"""Tests for shell-specific managed instruction rendering."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import ShellRoutingProfile, TGsConfig
from shared.instructions import _tier_mapping_table, render_shell_instructions


def _config_from_yaml(payload: str) -> TGsConfig:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(payload, encoding="utf-8")
        return TGsConfig.from_yaml(config_path)


def test_claude_default_instructions_are_strict() -> None:
    body = render_shell_instructions(TGsConfig.defaults(), "claude-code")

    assert "These instructions apply only to **Claude Code**" in body
    assert "meta-harness" in body
    assert "Routing mode: strict" in body
    assert "ALWAYS call `route_task` before writing or editing code or other non-exempt project files." in body
    assert "Routing exemptions" in body
    assert "`.md`" in body
    assert "`.mdc`" in body
    assert "All other filetypes remain routed by default" in body
    assert "use `execute_subtask` only when delegating" in body
    assert "Agent transparency is required" in body
    assert "PreToolUse" in body
    assert "validate_routing_guard" in body


def test_copilot_default_instructions_are_advisory() -> None:
    body = render_shell_instructions(TGsConfig.defaults(), "github-copilot-cli")

    assert "These instructions apply only to **GitHub Copilot CLI**" in body
    assert "Routing mode: advisory" in body
    assert "not mandatory before edits in this shell" in body
    assert "Do not call `route_task` solely for files covered by routing exemptions." in body
    assert "You may edit directly" in body
    assert "Agent transparency tables are optional" in body
    assert "PreToolUse" not in body
    assert "Edit`/`Write" not in body
    assert "validate_routing_guard" not in body


def test_copilot_strict_mode_emits_mandatory_instructions_without_hooks() -> None:
    cfg = _config_from_yaml("routing_policy:\n  mode: strict\n")
    body = render_shell_instructions(cfg, "github-copilot-cli")

    assert "Routing mode: strict" in body
    assert "ALWAYS call `route_task` before writing or editing code or other non-exempt project files." in body
    assert "Agent transparency is required" in body
    assert "PreToolUse" not in body
    assert "validate_routing_guard" not in body


def test_custom_copilot_strict_opt_in_emits_mandatory_instructions() -> None:
    cfg = _config_from_yaml(
        "\n".join(
            [
                "routing_policy:",
                "  mode: custom",
                "  shells:",
                "    github-copilot-cli:",
                "      route_task_mandatory: true",
                "      low_tier_execute_subtask: true",
                "      agent_transparency_required: true",
            ]
        )
    )
    body = render_shell_instructions(cfg, "copilot")

    assert "Routing mode: strict" in body
    assert "ALWAYS call `route_task` before writing or editing code or other non-exempt project files." in body
    assert "For low-tier work, use `execute_subtask` only when delegating" in body
    assert "PreToolUse" not in body


def test_tier_mapping_table_tolerates_partial_profile_mapping() -> None:
    table = _tier_mapping_table(ShellRoutingProfile(
        shell_id="github-copilot-cli",
        tier_model_mapping={"low": "custom-low"},
    ))

    assert "`custom-low`" in table
    assert "`router-selected default`" in table