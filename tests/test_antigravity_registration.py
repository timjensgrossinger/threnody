"""Tests for Antigravity registration in discovery, config, and model registry."""
from __future__ import annotations

import pytest

from shared.discovery import (
    BUILTIN_PROVIDERS,
    HOST_PROVIDER_NAMES,
    _normalize_caller_for_discovery,
    caller_from_client_name,
)
from shared.config import (
    ROUTING_POLICY_SHELL_ALIASES,
    ROUTING_POLICY_SHELL_BOOTSTRAP_IDS,
    SUPPORTED_ROUTING_POLICY_SHELLS,
    LEARNING_HOOK_CAPABLE_SHELLS,
)
from shared.model_registry import bootstrap_models, bootstrap_tier_map


def test_antigravity_in_host_provider_names() -> None:
    assert "antigravity" in HOST_PROVIDER_NAMES


def test_antigravity_in_supported_routing_shells() -> None:
    assert "antigravity" in SUPPORTED_ROUTING_POLICY_SHELLS


def test_antigravity_in_shell_aliases() -> None:
    assert ROUTING_POLICY_SHELL_ALIASES["antigravity"] == "antigravity"
    assert ROUTING_POLICY_SHELL_ALIASES["agy"] == "antigravity"


def test_antigravity_in_bootstrap_ids() -> None:
    assert ROUTING_POLICY_SHELL_BOOTSTRAP_IDS["antigravity"] == "antigravity"


def test_antigravity_in_learning_hook_shells() -> None:
    assert "antigravity" in LEARNING_HOOK_CAPABLE_SHELLS


def test_antigravity_in_builtin_providers() -> None:
    names = [p.name for p in BUILTIN_PROVIDERS]
    assert "antigravity" in names
    agy = next(p for p in BUILTIN_PROVIDERS if p.name == "antigravity")
    assert agy.binary == "agy"
    assert agy.display_name == "Google Antigravity"


def test_antigravity_model_registry() -> None:
    models = bootstrap_models("antigravity")
    assert len(models) >= 2
    model_ids = [m.model_id for m in models]
    assert "gemini-3.7-flash" in model_ids
    assert "gemini-3.1-pro" in model_ids


def test_antigravity_tier_map() -> None:
    tier_map = bootstrap_tier_map("antigravity")
    assert tier_map.get("low") == "gemini-3.7-flash"
    assert tier_map.get("high") == "gemini-3.1-pro"



def test_caller_from_client_name_antigravity() -> None:
    assert caller_from_client_name("antigravity-cli") == "antigravity"
    assert caller_from_client_name("agy") == "antigravity"
    assert caller_from_client_name("Antigravity") == "antigravity"


def test_normalize_caller_for_discovery_agy() -> None:
    assert _normalize_caller_for_discovery("agy") == "antigravity"
    assert _normalize_caller_for_discovery("antigravity-cli") == "antigravity"
    assert _normalize_caller_for_discovery("antigravity") == "antigravity"
