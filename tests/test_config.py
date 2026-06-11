#!/usr/bin/env python3
"""
Tests for shared/config.py — configuration and hard bounds.
"""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shared.config as config_module
from shared.config import (
    DEFAULT_ROUTING_EXCEPTION_FILETYPES,
    DEFAULT_ROUTING_EXCEPTION_PATHS,
    DEFAULT_PLANNER_MODEL,
    TGsConfig,
    ThresholdConfig,
    LOW_TIER_FLOOR,
    LOW_TIER_CEILING,
    MEDIUM_HIGH_BOUNDARY_FLOOR,
    MEDIUM_HIGH_BOUNDARY_CEILING,
    SUBTASK_TEMPLATES,
    SPEED_SIGNALS,
    QUALITY_SIGNALS,
    TOKEN_CEILING_LOW,
    TOKEN_CEILING_MEDIUM,
    TOKEN_CEILING_HIGH,
    load_eval_config,
    normalize_caller_id,
)


def test_hard_bounds_constants() -> None:
    """Hard bound constants should be reasonable."""
    assert LOW_TIER_FLOOR == 0.50
    assert LOW_TIER_CEILING == 0.75
    assert MEDIUM_HIGH_BOUNDARY_FLOOR == 0.75
    assert MEDIUM_HIGH_BOUNDARY_CEILING == 0.95


def test_threshold_clamp_low() -> None:
    """Low boundary should clamp to floor/ceiling."""
    t = ThresholdConfig(low_max=0.20, medium_max=0.80)
    t.clamp()
    assert t.low_max >= LOW_TIER_FLOOR
    assert t.low_max <= LOW_TIER_CEILING


def test_threshold_clamp_high() -> None:
    """High boundary should clamp to floor/ceiling."""
    t = ThresholdConfig(low_max=0.55, medium_max=0.99)
    t.clamp()
    assert t.medium_max >= MEDIUM_HIGH_BOUNDARY_FLOOR
    assert t.medium_max <= MEDIUM_HIGH_BOUNDARY_CEILING


def test_threshold_no_collapse() -> None:
    """Medium/high boundary should never be <= low/medium boundary."""
    t = ThresholdConfig(low_max=0.75, medium_max=0.75)
    t.clamp()
    assert t.medium_max > t.low_max


def test_token_ceilings() -> None:
    """Token ceilings should be ordered."""
    assert TOKEN_CEILING_LOW < TOKEN_CEILING_MEDIUM < TOKEN_CEILING_HIGH


def test_subtask_templates_exist() -> None:
    """Should have at least 5 templates."""
    assert len(SUBTASK_TEMPLATES) >= 5


def test_subtask_templates_all_low_tier() -> None:
    """All templates should route to low tier."""
    for t in SUBTASK_TEMPLATES:
        assert t.tier == "low", f"Template '{t.pattern}' has tier '{t.tier}'"


def test_speed_signals_negative() -> None:
    """All speed signals should have negative weights."""
    for keyword, weight in SPEED_SIGNALS.items():
        assert weight < 0, f"Speed signal '{keyword}' has weight {weight}"


def test_quality_signals_positive() -> None:
    """All quality signals should have positive weights."""
    for keyword, weight in QUALITY_SIGNALS.items():
        assert weight > 0, f"Quality signal '{keyword}' has weight {weight}"


def test_default_config() -> None:
    """Default config should be valid."""
    cfg = TGsConfig()
    cfg.thresholds.clamp()
    assert cfg.thresholds.low_max >= LOW_TIER_FLOOR
    assert cfg.thresholds.medium_max <= MEDIUM_HIGH_BOUNDARY_CEILING
    assert cfg.planner_model == "claude-sonnet-4-6"


def test_legacy_dict_format() -> None:
    """to_legacy_dict should produce expected keys."""
    cfg = TGsConfig()
    d = cfg.to_legacy_dict()
    assert "models" in d
    assert "providers" in d
    assert "thresholds" in d
    assert "mini" in d["models"]
    assert "sonnet" in d["models"]
    assert "opus" in d["models"]
    assert "effort_defaults" in d["providers"]
    assert "preferred_routing" in d["providers"]


def test_effort_defaults_round_trip() -> None:
    """effort_defaults should load from YAML and round-trip to legacy dict."""
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "providers:",
                    "  effort_defaults:",
                    "    claude-code:",
                    "      low: low",
                    "      high: max",
                    "    codex:",
                    "      medium: medium",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)

        assert cfg.get_default_effort("claude-code", "low") == "low"
        assert cfg.get_default_effort("CLAUDE-CODE", "HIGH") == "max"
        assert cfg.get_default_effort("codex", "medium") == "medium"
        assert cfg.get_default_effort("junie", "low") is None
        assert cfg.to_legacy_dict()["providers"]["effort_defaults"]["claude-code"]["high"] == "max"


def test_routing_policy_legacy_defaults_are_shell_specific() -> None:
    """Missing routing_policy should preserve recommended shell defaults."""
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text("providers: {}\n", encoding="utf-8")

        cfg = TGsConfig.from_yaml(config_path)
        claude = cfg.routing_policy.effective_profile("claude-code")
        copilot = cfg.routing_policy.effective_profile("github-copilot-cli")

        assert claude.route_task_mandatory is True
        assert claude.low_tier_execute_subtask is False
        assert claude.agent_transparency_required is True
        assert claude.direct_edit_hooks is True
        assert claude.tier_model_mapping["low"] == "haiku"
        assert claude.tier_model_mapping["medium"] == "sonnet"
        assert claude.tier_model_mapping["high"] == "opus"
        assert copilot.route_task_mandatory is False
        assert copilot.low_tier_execute_subtask is False
        assert copilot.agent_transparency_required is False
        assert copilot.direct_edit_hooks is False


def test_routing_exception_defaults_exempt_docs_and_ai_instruction_files() -> None:
    cfg = TGsConfig.defaults()

    assert ".md" in cfg.routing_exceptions.filetypes
    assert ".mdc" in cfg.routing_exceptions.filetypes
    assert ".cursorrules" in cfg.routing_exceptions.paths
    assert ".github/copilot-instructions.md" in cfg.routing_exceptions.paths
    assert tuple(cfg.routing_exceptions.filetypes) == DEFAULT_ROUTING_EXCEPTION_FILETYPES
    assert tuple(cfg.routing_exceptions.paths) == DEFAULT_ROUTING_EXCEPTION_PATHS


def test_routing_exception_yaml_merges_with_defaults() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "routing_exceptions:",
                    "  filetypes:",
                    "    - .rst",
                    "  paths:",
                    "    - docs/generated/*",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)

        assert cfg.routing_exceptions.filetypes[:2] == [".md", ".mdc"]
        assert ".rst" in cfg.routing_exceptions.filetypes
        assert ".cursorrules" in cfg.routing_exceptions.paths
        assert "docs/generated/*" in cfg.routing_exceptions.paths


def test_routing_policy_global_guarded_and_advisory_modes() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"

        config_path.write_text("routing_policy:\n  mode: guarded\n", encoding="utf-8")
        guarded_cfg = TGsConfig.from_yaml(config_path)
        assert guarded_cfg.routing_policy.mode == "guarded"
        assert guarded_cfg.routing_policy.effective_profile("github-copilot-cli").route_task_mandatory is True
        assert guarded_cfg.routing_policy.effective_profile("github-copilot-cli").low_tier_execute_subtask is False
        assert guarded_cfg.routing_policy.effective_profile("github-copilot-cli").direct_edit_hooks is False
        assert guarded_cfg.routing_policy.effective_profile("claude-code").direct_edit_hooks is True

        config_path.write_text("routing_policy:\n  mode: advisory\n", encoding="utf-8")
        advisory_cfg = TGsConfig.from_yaml(config_path)
        assert advisory_cfg.routing_policy.effective_profile("claude-code").route_task_mandatory is False
        assert advisory_cfg.routing_policy.effective_profile("claude-code").direct_edit_hooks is False


def test_routing_policy_strict_alias_normalizes_to_guarded(caplog: pytest.LogCaptureFixture) -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text("routing_policy:\n  mode: strict\n", encoding="utf-8")
        cfg = TGsConfig.from_yaml(config_path)

        assert cfg.routing_policy.mode == "guarded"
        assert cfg.routing_policy.effective_profile("claude-code").route_task_mandatory is True
        assert cfg.routing_policy.effective_profile("claude-code").low_tier_execute_subtask is False
        assert any("mode 'strict' is deprecated" in record.message for record in caplog.records)


def test_routing_policy_custom_shell_overrides() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "routing_policy:",
                    "  mode: custom",
                    "  shells:",
                    "    github-copilot-cli:",
                    "      route_task_mandatory: true",
                    "      low_tier_execute_subtask: true",
                    "      agent_transparency_required: true",
                    "      direct_edit_hooks: true",
                    "      tier_model_mapping:",
                    "        low: custom-low",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)
        copilot = cfg.routing_policy.effective_profile("copilot")

        assert copilot.route_task_mandatory is True
        assert copilot.low_tier_execute_subtask is True
        assert copilot.agent_transparency_required is True
        assert copilot.direct_edit_hooks is False
        assert copilot.tier_model_mapping["low"] == "custom-low"
        assert copilot.tier_model_mapping["medium"] == "claude-sonnet-4.6"


def test_basic_yaml_fallback_parses_floats_lists_and_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "thresholds:",
                    "  mini_max: 0.55",
                    "  sonnet_max: 0.8",
                    "providers:",
                    "  disabled:",
                    "    - windsurf",
                    "routing_policy:",
                    "  mode: custom",
                    "  shells:",
                    "    github-copilot-cli:",
                    "      route_task_mandatory: true",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(config_module, "yaml", None)

        cfg = TGsConfig.from_yaml(config_path)

        assert cfg.thresholds.low_max == 0.55
        assert cfg.thresholds.medium_max == 0.8
        assert cfg.disabled_providers == ["windsurf"]
        assert cfg.routing_policy.effective_profile("github-copilot-cli").route_task_mandatory is True


def test_preferred_routing_round_trip() -> None:
    """preferred_routing should load from YAML and round-trip to legacy dict."""
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "providers:",
                    "  preferred_routing:",
                    "    low:",
                    '      - "Claude Code / haiku"',
                    "      - provider: github-copilot",
                    "      - model: gpt-5-mini",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)
        preferred = cfg.get_preferred_routing("LOW")

        assert len(preferred) == 3
        assert preferred[0].provider == "Claude Code"
        assert preferred[0].model == "haiku"
        assert preferred[1].provider == "github-copilot"
        assert preferred[1].model is None
        assert preferred[2].provider is None
        assert preferred[2].model == "gpt-5-mini"
        assert cfg.to_legacy_dict()["providers"]["preferred_routing"]["low"][0] == {
            "provider": "Claude Code",
            "model": "haiku",
        }


def test_preferred_routing_normalizes_tier_keys() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "providers:",
                    "  preferred_routing:",
                    "    LOW:",
                    "      - provider: github-copilot",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)

        assert len(cfg.get_preferred_routing("low")) == 1
        assert len(cfg.get_preferred_routing("LOW")) == 1


def test_preferred_routing_by_caller_overrides_global() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "providers:",
                    "  preferred_routing:",
                    "    low:",
                    "      - provider: github-copilot",
                    "  preferred_routing_by_caller:",
                    "    claude-code:",
                    "      low:",
                    "        - provider: claude-code",
                    "        - provider: mistral-vibe",
                    "    github-copilot-cli:",
                    "      low:",
                    "        - provider: codex",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)

        assert [p.provider for p in cfg.get_preferred_routing("low")] == ["github-copilot"]
        assert [p.provider for p in cfg.get_preferred_routing("low", caller="claude-code")] == [
            "claude-code",
            "mistral-vibe",
        ]
        assert [p.provider for p in cfg.get_preferred_routing("low", caller="github-copilot")] == [
            "codex",
        ]
        assert cfg.to_legacy_dict()["providers"]["preferred_routing_by_caller"]["claude-code"]["low"] == [
            {"provider": "claude-code"},
            {"provider": "mistral-vibe"},
        ]


def test_preferred_routing_by_caller_missing_tier_falls_back_to_global() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "providers:",
                    "  preferred_routing:",
                    "    high:",
                    "      - provider: github-copilot",
                    "  preferred_routing_by_caller:",
                    "    claude-code:",
                    "      low:",
                    "        - provider: claude-code",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)

        assert [p.provider for p in cfg.get_preferred_routing("high", caller="claude-code")] == [
            "github-copilot",
        ]


def test_caller_normalization_matches_runtime_aliases() -> None:
    assert normalize_caller_id("GitHub Copilot CLI") == "github-copilot"
    assert normalize_caller_id("gh") == "github-copilot"
    assert normalize_caller_id("Claude") == "claude-code"


def test_to_legacy_dict_serializes_string_backed_preferred_routing() -> None:
    cfg = TGsConfig()
    cfg.preferred_routing = {
        "low": ["github-copilot / gpt-5-mini"],
    }
    cfg.preferred_routing_by_caller = {
        "github-copilot": {
            "medium": ["claude-code / claude-sonnet-4.6"],
        },
    }

    providers = cfg.to_legacy_dict()["providers"]

    assert providers["preferred_routing"]["low"] == [
        {"provider": "github-copilot", "model": "gpt-5-mini"},
    ]
    assert providers["preferred_routing_by_caller"]["github-copilot"]["medium"] == [
        {"provider": "claude-code", "model": "claude-sonnet-4.6"},
    ]


def test_to_legacy_dict_accepts_dict_backed_usage_windows() -> None:
    cfg = TGsConfig()
    cfg.provider_usage_windows = {
        "Claude_Code": {
            "windows": [
                {
                    "hours": 5,
                    "budget_tokens": 500_000,
                    "threshold": 0.85,
                    "action": "prefer_alternatives",
                }
            ]
        }
    }

    assert cfg.to_legacy_dict()["providers"]["usage_windows"] == {
        "Claude_Code": [
            {
                "hours": 5,
                "budget_tokens": 500_000,
                "threshold": 0.85,
                "action": "prefer_alternatives",
            }
        ]
    }


def test_endpoint_providers_round_trip() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "providers:",
                    "  endpoint_providers:",
                    "    - name: studio",
                    "      kind: openai-compatible",
                    "      scope: local",
                    "      base_url: http://127.0.0.1:1234/v1",
                    "      api_key_env: LM_STUDIO_API_KEY",
                    "    - name: lab-ollama",
                    "      kind: ollama",
                    "      scope: network",
                    "      base_url: https://10.0.0.40:11434",
                    "      tier_models:",
                    "        low: qwen2.5-coder:7b",
                    "        medium: qwen2.5-coder:14b",
                    "        high: qwen2.5-coder:32b",
                    "      cost_rank:",
                    "        low: 1",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)
        serialized = cfg.to_legacy_dict()

        assert len(cfg.endpoint_providers) == 2
        assert cfg.endpoint_providers[0].kind == "openai-compatible"
        assert cfg.endpoint_providers[0].api_key_env == "LM_STUDIO_API_KEY"
        assert cfg.endpoint_providers[1].tier_models["high"] == "qwen2.5-coder:32b"
        assert serialized["providers"]["endpoint_providers"][1]["name"] == "lab-ollama"


def test_endpoint_providers_reject_invalid_network_entries() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "providers:",
                    "  endpoint_providers:",
                    "    - name: missing-tier-models",
                    "      kind: ollama",
                    "      scope: network",
                    "      base_url: https://10.0.0.40:11434",
                    "    - name: bad-local",
                    "      kind: openai-compatible",
                    "      scope: local",
                    "      base_url: http://10.0.0.41:1234/v1",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)

        assert cfg.endpoint_providers == []


def test_endpoint_providers_reject_non_http_urls_and_embedded_credentials() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "providers:",
                    "  endpoint_providers:",
                    "    - name: bad-scheme",
                    "      kind: openai-compatible",
                    "      scope: local",
                    "      base_url: file://127.0.0.1/tmp/model",
                    "    - name: bad-creds",
                    "      kind: ollama",
                    "      scope: network",
                    "      base_url: https://user:secret@10.0.0.40:11434",
                    "      tier_models:",
                    "        low: qwen2.5-coder:7b",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)

        assert cfg.endpoint_providers == []


def test_endpoint_providers_require_https_for_network() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "providers:",
                    "  endpoint_providers:",
                    "    - name: rejected-http",
                    "      kind: ollama",
                    "      scope: network",
                    "      base_url: http://10.0.0.40:11434",
                    "      tier_models:",
                    "        low: qwen2.5-coder:7b",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)

        assert cfg.endpoint_providers == []


def test_endpoint_providers_round_trip_verify_tls_override() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "providers:",
                    "  endpoint_providers:",
                    "    - name: lab-openai",
                    "      kind: openai-compatible",
                    "      scope: network",
                    "      base_url: https://10.0.0.41:1234/v1",
                    "      verify_tls: false",
                    "      tier_models:",
                    "        low: gpt-oss-20b",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)
        serialized = cfg.to_legacy_dict()

        assert len(cfg.endpoint_providers) == 1
        assert cfg.endpoint_providers[0].verify_tls is False
        assert serialized["providers"]["endpoint_providers"][0]["verify_tls"] is False


def test_endpoint_providers_reject_duplicate_names() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "providers:",
                    "  endpoint_providers:",
                    "    - name: studio",
                    "      kind: openai-compatible",
                    "      scope: local",
                    "      base_url: http://127.0.0.1:1234/v1",
                    "    - name: studio",
                    "      kind: openai-compatible",
                    "      scope: local",
                    "      base_url: http://127.0.0.1:8000/v1",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)

        assert len(cfg.endpoint_providers) == 1
        assert cfg.endpoint_providers[0].base_url == "http://127.0.0.1:1234/v1"


def test_code_review_settings_load_from_yaml() -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "code_review: true",
                    "code_review_tier: high",
                    "auto_approve_timeout: 0",
                ]
            ),
            encoding="utf-8",
        )

        cfg = TGsConfig.from_yaml(config_path)

        assert cfg.code_review is True
        assert cfg.code_review_tier == "high"
        assert cfg.auto_approve_timeout == 0


def test_load_eval_config_falls_back_to_defaults_in_test_mode(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text("planner_model: custom-model\n", encoding="utf-8")

        monkeypatch.setattr(config_module, "yaml", None)
        monkeypatch.setenv("THRENODY_TEST_MODE", "1")

        cfg = load_eval_config(config_path)

        assert cfg.planner_model == DEFAULT_PLANNER_MODEL


def test_verify_gate_config_round_trip(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
verify_gate:
  enabled: true
  mode: block
  signals:
    lint:
      command: ruff check .
      required: false
      timeout_seconds: 30
    tests:
      command: python3 -m pytest -q
      required: true
      timeout_seconds: 300
""",
        encoding="utf-8",
    )

    cfg = TGsConfig.from_yaml(config_path)

    assert cfg.verify_gate.enabled is True
    assert cfg.verify_gate.mode == "block"
    assert cfg.verify_gate.signals["lint"].command == "ruff check ."
    assert cfg.verify_gate.signals["lint"].required is False
    assert cfg.verify_gate.signals["lint"].timeout_seconds == 30
    assert cfg.verify_gate.signals["tests"].required is True
    assert cfg.verify_gate.signals["tests"].timeout_seconds == 300
    assert "types" in cfg.verify_gate.signals


def test_verify_gate_invalid_mode_and_timeout_use_safe_defaults(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
verify_gate:
  enabled: true
  mode: execute-anything
  signals:
    tests:
      required: true
      timeout_seconds: 0
""",
        encoding="utf-8",
    )

    cfg = TGsConfig.from_yaml(config_path)

    assert cfg.verify_gate.mode == "warn"
    assert cfg.verify_gate.signals["tests"].timeout_seconds == 120


def test_load_eval_config_raises_without_test_mode(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.yaml"
        config_path.write_text("planner_model: custom-model\n", encoding="utf-8")

        monkeypatch.setattr(config_module, "yaml", None)
        monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)

        with pytest.raises(RuntimeError, match="PyYAML is required"):
            load_eval_config(config_path)


if __name__ == "__main__":
    tests = [
        test_hard_bounds_constants,
        test_threshold_clamp_low,
        test_threshold_clamp_high,
        test_threshold_no_collapse,
        test_token_ceilings,
        test_subtask_templates_exist,
        test_subtask_templates_all_low_tier,
        test_speed_signals_negative,
        test_quality_signals_positive,
        test_default_config,
        test_legacy_dict_format,
        test_effort_defaults_round_trip,
        test_preferred_routing_round_trip,
        test_preferred_routing_normalizes_tier_keys,
        test_endpoint_providers_round_trip,
        test_endpoint_providers_reject_invalid_network_entries,
        test_endpoint_providers_reject_non_http_urls_and_embedded_credentials,
        test_endpoint_providers_require_https_for_network,
        test_endpoint_providers_round_trip_verify_tls_override,
        test_endpoint_providers_reject_duplicate_names,
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
