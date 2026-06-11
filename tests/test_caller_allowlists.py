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
        pa = _make_provider("aider")
        pb = _make_provider("mistral-vibe")
        pc = _make_provider("blackbox-ai")
        reg = _make_registry(pa, pb, pc)
        with patch.object(reg, "get_providers_for_tier", return_value=[pa, pb, pc]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, excluded = reg._ordered_execution_candidates(
                    "low", caller="github-copilot",
                    caller_allowlists={"github-copilot": ["aider", "blackbox-ai"]},
                )
        assert [p.name for p in selected] == ["aider", "blackbox-ai"]
        assert len(excluded) == 1
        assert excluded[0]["provider"] == "Mistral Vibe"
        assert (
            "not in caller allowlist for github-copilot" in excluded[0]["reason"]
            or "router-only host" in excluded[0]["reason"]
        )

    def test_exclusion_reason_includes_caller(self):
        pa, pb = _make_provider("aider"), _make_provider("mistral-vibe")
        reg = _make_registry(pa, pb)
        with patch.object(reg, "get_providers_for_tier", return_value=[pa, pb]):
            with patch.object(reg, "list_adapters", return_value=[]):
                _, excluded = reg._ordered_execution_candidates("low", caller="my-caller", caller_allowlists={"my-caller": ["aider"]})
        assert excluded[0]["provider"] == "Mistral Vibe"
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
        pa = _make_provider("aider")
        pb = _make_provider("mistral-vibe")
        reg = _make_registry(pa, pb)
        with patch.object(reg, "get_providers_for_tier", return_value=[pa, pb]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, _ = reg._ordered_execution_candidates("low", caller="GITHUB-COPILOT", caller_allowlists={"github-copilot": ["aider"]})
        assert len(selected) == 1 and selected[0].name == "aider"

    def test_allowlist_applies_to_caller_aliases(self):
        pa = _make_provider("aider")
        pb = _make_provider("mistral-vibe")
        reg = _make_registry(pa, pb)
        with patch.object(reg, "get_providers_for_tier", return_value=[pa, pb]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, excluded = reg._ordered_execution_candidates(
                    "low",
                    caller="github-copilot-cli",
                    caller_allowlists={"github-copilot": ["aider"]},
                )

        assert [p.name for p in selected] == ["aider"]
        assert len(excluded) == 1
        assert excluded[0]["provider"] == "Mistral Vibe"
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
        aider = _make_provider("aider", 3)
        mistral = _make_provider("mistral-vibe", 4)
        blackbox = _make_provider("blackbox-ai", 0)
        reg = _make_registry(aider, mistral, blackbox)
        reg._config_overrides = {
            "providers": {
                "preferred_routing_by_caller": {
                    "claude-code": {
                        "low": [
                            {"provider": "aider"},
                            {"provider": "mistral-vibe"},
                        ],
                    },
                },
            },
        }

        with patch.object(reg, "list_adapters", return_value=[]):
            claude_selected, _ = reg._ordered_execution_candidates("low", caller="claude-code")
            copilot_selected, _ = reg._ordered_execution_candidates("low", caller="github-copilot")

        assert [p.name for p in claude_selected[:2]] == ["aider", "mistral-vibe"]
        assert copilot_selected[0].name == "blackbox-ai"

    def test_anti_recursion_matches_caller_aliases(self):
        aider = _make_provider("aider", 0)
        blackbox = _make_provider("blackbox-ai", 1)
        reg = _make_registry(aider, blackbox)

        with patch.object(reg, "list_adapters", return_value=[]):
            selected, excluded = reg._ordered_execution_candidates("low", caller="github-copilot-cli")

        assert [p.name for p in selected] == ["aider", "blackbox-ai"]
        assert excluded == []

    def test_allowlist_overrides_router_only(self):
        aider = _make_provider("aider")
        blackbox = _make_provider("blackbox-ai")
        reg = _make_registry(aider, blackbox)
        with patch.object(reg, "get_providers_for_tier", return_value=[aider, blackbox]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, excluded = reg._ordered_execution_candidates(
                    "low",
                    caller="github-copilot",
                    caller_allowlists={"github-copilot": ["aider", "blackbox-ai"]},
                )
        assert [p.name for p in selected] == ["aider", "blackbox-ai"]


class TestPassThrough:

    def test_select_provider_for_tier_passes_allowlists(self):
        pa = _make_provider("aider")
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
