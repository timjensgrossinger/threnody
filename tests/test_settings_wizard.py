#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shared.settings_wizard as settings_wizard


def test_provider_label_tolerates_malformed_models() -> None:
    label = settings_wizard._provider_label(
        {
            "name": "broken-provider",
            "billing": "unknown",
            "models": "not-a-mapping",
        }
    )

    assert "broken-provider" in label


def test_write_config_works_without_pyyaml(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(settings_wizard, "yaml", None)

    settings_wizard._write_config(
        config_path,
        disabled=["windsurf"],
        caller_allowlists={},
        preferred_routing={},
        routing_policy={"mode": "advisory"},
    )

    body = config_path.read_text(encoding="utf-8")
    assert "routing_policy:" in body
    assert "mode: \"advisory\"" in body


def test_host_native_policy_warnings_surface_risky_overrides() -> None:
    warnings = settings_wizard._host_native_policy_warnings(
        {
            "providers": {"router_only_allow_execution": ["claude-code"]},
            "swarm": {
                "host_execution_mode": "delegate",
                "host_execution_mode_by_caller": {"github-copilot": "delegate"},
            },
        }
    )
    assert any("router_only_allow_execution" in item for item in warnings)
    assert any("host_execution_mode is delegate" in item for item in warnings)
    assert any("github-copilot" in item for item in warnings)


def test_host_native_policy_warnings_call_out_legacy_host_allowlists() -> None:
    warnings = settings_wizard._host_native_policy_warnings(
        {
            "providers": {
                "caller_allowlists": {
                    "github-copilot": ["codex", "aider"],
                },
            },
        }
    )
    assert any("host→host subprocess delegation is no longer supported" in item for item in warnings)
