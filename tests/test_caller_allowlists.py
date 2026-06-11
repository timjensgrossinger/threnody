"""Unit tests for per-caller provider allowlist filtering in ProviderRegistry."""
from __future__ import annotations

import sys
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.discovery import CLIProvider, DetectReason, ProviderReadiness, ProviderRegistry


def _make_provider(name: str, cost_rank: int = 1) -> CLIProvider:
    provider = CLIProvider(
        name=name,
        binary=name,
        display_name=name.replace("-", " ").title(),
        tier_models={"low": f"{name}-m", "medium": f"{name}-m", "high": f"{name}-m"},
        cost_rank={"low": cost_rank, "medium": cost_rank, "high": cost_rank},
        detect_cmd=["true"],
    )
    provider.readiness = ProviderReadiness(
        routeable=True,
        reason=DetectReason.READY,
        last_checked=0.0,
    )
    return provider


def _make_registry(*providers: CLIProvider) -> ProviderRegistry:
    registry = ProviderRegistry.__new__(ProviderRegistry)
    registry.available_providers = list(providers)
    registry._adapters = []
    registry._config_ref = None
    registry._config_overrides = {}
    return registry


class TestOrderedExecutionCandidatesAllowlist:

    def test_no_allowlist_returns_all(self):
        pa, pb, pc = _make_provider("a"), _make_provider("b"), _make_provider("c")
        reg = _make_registry(pa, pb, pc)
        with patch.object(reg, "get_providers_for_tier", return_value=[pa, pb, pc]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, excluded = reg._ordered_execution_candidates("low", caller="github-copilot", caller_allowlists=None)
        assert len(selected) == 3 and excluded == []

    def test_caller_not_in_allowlist_returns_all(self):
        pa, pb = _make_provider("a"), _make_provider("b")
        reg = _make_registry(pa, pb)
        with patch.object(reg, "get_providers_for_tier", return_value=[pa, pb]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, _ = reg._ordered_execution_candidates("low", caller="unknown", caller_allowlists={"github-copilot": ["a"]})
        assert len(selected) == 2

    def test_allowlist_filters_to_allowed_only(self):
        pa = _make_provider("claude-code")
        pb = _make_provider("codex")
        pc = _make_provider("blackbox-ai")
        reg = _make_registry(pa, pb, pc)
        with patch.object(reg, "get_providers_for_tier", return_value=[pa, pb, pc]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, excluded = reg._ordered_execution_candidates(
                    "low", caller="github-copilot",
                    caller_allowlists={"github-copilot": ["claude-code", "blackbox-ai"]},
                )
        assert [p.name for p in selected] == ["claude-code", "blackbox-ai"]
        assert len(excluded) == 1
        assert excluded[0]["provider"] == "Codex"
        assert (
            "not in caller allowlist for github-copilot" in excluded[0]["reason"]
            or "router-only host" in excluded[0]["reason"]
        )

    def test_exclusion_reason_includes_caller(self):
        pa, pb = _make_provider("claude-code"), _make_provider("codex")
        reg = _make_registry(pa, pb)
        with patch.object(reg, "get_providers_for_tier", return_value=[pa, pb]):
            with patch.object(reg, "list_adapters", return_value=[]):
                _, excluded = reg._ordered_execution_candidates("low", caller="my-caller", caller_allowlists={"my-caller": ["claude-code"]})
        assert excluded[0]["provider"] == "Codex"
        assert (
            "my-caller" in excluded[0]["reason"]
            or "router-only host" in excluded[0]["reason"]
        )

    def test_empty_allowlist_fallback(self, caplog):
        pa, pb = _make_provider("a"), _make_provider("b")
        reg = _make_registry(pa, pb)
        with patch.object(reg, "get_providers_for_tier", return_value=[pa, pb]):
            with patch.object(reg, "list_adapters", return_value=[]):
                with caplog.at_level(logging.WARNING):
                    selected, _ = reg._ordered_execution_candidates("low", caller="c", caller_allowlists={"c": []})
        assert len(selected) == 2
        assert any("ignoring allowlist" in r.message for r in caplog.records)

    def test_case_insensitive(self):
        pa = _make_provider("Claude-Code")
        pb = _make_provider("codex")
        reg = _make_registry(pa, pb)
        with patch.object(reg, "get_providers_for_tier", return_value=[pa, pb]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, _ = reg._ordered_execution_candidates("low", caller="GITHUB-COPILOT", caller_allowlists={"github-copilot": ["claude-code"]})
        assert len(selected) == 1 and selected[0].name == "Claude-Code"

    def test_allowlist_applies_to_caller_aliases(self):
        pa = _make_provider("claude-code")
        pb = _make_provider("codex")
        reg = _make_registry(pa, pb)
        with patch.object(reg, "get_providers_for_tier", return_value=[pa, pb]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, excluded = reg._ordered_execution_candidates(
                    "low",
                    caller="github-copilot-cli",
                    caller_allowlists={"github-copilot": ["claude-code"]},
                )

        assert [p.name for p in selected] == ["claude-code"]
        assert len(excluded) == 1
        assert excluded[0]["provider"] == "Codex"
        assert (
            "not in caller allowlist for github-copilot-cli" in excluded[0]["reason"]
            or "router-only host" in excluded[0]["reason"]
        )

    def test_multiple_callers_only_matching_applied(self):
        pa, pb = _make_provider("a"), _make_provider("b")
        reg = _make_registry(pa, pb)
        with patch.object(reg, "get_providers_for_tier", return_value=[pa, pb]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, _ = reg._ordered_execution_candidates(
                    "low", caller="claude-code",
                    caller_allowlists={"github-copilot": ["a"], "claude-code": ["a", "b"]},
                )
        assert len(selected) == 2

    def test_caller_specific_preferences_only_apply_to_matching_caller(self):
        claude = _make_provider("claude-code", 3)
        mistral = _make_provider("mistral-vibe", 4)
        codex = _make_provider("codex", 0)
        reg = _make_registry(claude, mistral, codex)
        reg._config_overrides = {
            "providers": {
                "preferred_routing_by_caller": {
                    "claude-code": {
                        "low": [
                            {"provider": "claude-code"},
                            {"provider": "mistral-vibe"},
                        ],
                    },
                },
            },
        }

        with patch.object(reg, "list_adapters", return_value=[]):
            claude_selected, _ = reg._ordered_execution_candidates("low", caller="claude-code")
            copilot_selected, _ = reg._ordered_execution_candidates("low", caller="github-copilot")

        assert [p.name for p in claude_selected[:2]] == ["claude-code", "mistral-vibe"]
        assert copilot_selected[0].name == "codex"

    def test_anti_recursion_matches_caller_aliases(self):
        copilot = _make_provider("github-copilot", 0)
        codex = _make_provider("codex", 1)
        reg = _make_registry(copilot, codex)

        with patch.object(reg, "list_adapters", return_value=[]):
            selected, excluded = reg._ordered_execution_candidates("low", caller="github-copilot-cli")

        assert [p.name for p in selected] == ["codex"]
        assert excluded == [
            {
                "provider": "Github Copilot",
                "reason": "caller anti-recursion (github-copilot-cli)",
            }
        ]

    def test_allowlist_overrides_router_only(self):
        claude = _make_provider("claude-code")
        copilot = _make_provider("github-copilot")
        reg = _make_registry(claude, copilot)
        with patch.object(reg, "get_providers_for_tier", return_value=[claude, copilot]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, excluded = reg._ordered_execution_candidates(
                    "low",
                    caller="github-copilot",
                    caller_allowlists={"github-copilot": ["claude-code", "github-copilot"]},
                )
        assert [p.name for p in selected] == ["claude-code"]
        assert not any("router-only host" in e.get("reason", "") for e in excluded if e.get("provider") == "Claude Code")


class TestPassThrough:

    def test_select_provider_for_tier_passes_allowlists(self):
        pa = _make_provider("claude-code")
        reg = _make_registry(pa)
        with patch.object(reg, "_ordered_execution_candidates", return_value=([pa], [])) as mock:
            with patch.object(reg, "_selection_metadata_for_provider_with_effort", return_value={"provider": "x", "model": "y"}):
                reg.select_provider_for_tier("low", caller="github-copilot", caller_allowlists={"github-copilot": ["claude-code"]})
        assert mock.call_args[1].get("caller_allowlists") == {"github-copilot": ["claude-code"]}

    def test_execute_cheapest_passes_allowlists(self):
        pa = _make_provider("claude-code")
        reg = _make_registry(pa)
        with patch.object(reg, "_ordered_execution_candidates", return_value=([pa], [])) as mock:
            with patch.object(pa, "execute", return_value={"result": "ok", "model": "m"}):
                try:
                    reg.execute_cheapest("prompt", caller="github-copilot", caller_allowlists={"github-copilot": ["claude-code"]})
                except Exception:
                    pass
        assert mock.call_args[1].get("caller_allowlists") == {"github-copilot": ["claude-code"]}
