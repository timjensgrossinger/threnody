"""Tests for utility-only delegation (meta-harness alignment)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
from shared.config import TGsConfig
from shared.db import Database
from shared.discovery import CLIProvider, DetectReason, ProviderReadiness, ProviderRegistry
from shared.host_spawn import (
    DELEGATION_DISABLED_ERROR,
    HOST_DELEGATION_BLOCKED_ERROR,
    validate_execute_subtask_delegation,
)


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


def _make_registry(*providers: CLIProvider, **config_overrides: object) -> ProviderRegistry:
    registry = ProviderRegistry.__new__(ProviderRegistry)
    registry.available_providers = list(providers)
    registry._adapters = []
    registry._config_ref = None
    registry._config_overrides = dict(config_overrides)
    return registry


class TestDelegationTargetFiltering:
    def test_for_delegation_empty_when_utilities_disabled(self):
        opencode = _make_provider("opencode", 0)
        aider = _make_provider("aider", 1)
        reg = _make_registry(opencode, aider, providers={"delegation_utilities_enabled": False})
        with patch.object(reg, "get_providers_for_tier", return_value=[opencode, aider]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, _ = reg._ordered_execution_candidates(
                    "low",
                    caller="github-copilot",
                    for_delegation=True,
                )
        assert selected == []

    def test_for_delegation_allows_utilities_when_enabled(self):
        opencode = _make_provider("opencode", 0)
        aider = _make_provider("aider", 1)
        codex = _make_provider("codex", 2)
        reg = _make_registry(
            opencode,
            aider,
            codex,
            providers={
                "delegation_utilities_enabled": True,
                "delegation_utilities": ["opencode", "aider"],
            },
        )
        with patch.object(reg, "get_providers_for_tier", return_value=[codex, opencode, aider]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, excluded = reg._ordered_execution_candidates(
                    "low",
                    caller="github-copilot",
                    for_delegation=True,
                )
        assert [p.name for p in selected] == ["opencode", "aider"]
        assert any("host CLI executes via host_spawn" in e["reason"] for e in excluded)

    def test_delegation_candidates_exclude_host_clis(self):
        copilot = _make_provider("github-copilot", 0)
        aider = _make_provider("aider", 1)
        reg = _make_registry(
            copilot,
            aider,
            providers={
                "delegation_utilities_enabled": True,
                "delegation_utilities": ["opencode", "aider"],
            },
        )
        with patch.object(reg, "get_providers_for_tier", return_value=[copilot, aider]):
            with patch.object(reg, "list_adapters", return_value=[]):
                selected, excluded = reg._ordered_execution_candidates(
                    "low",
                    caller="claude-code",
                    for_delegation=True,
                )
        assert [p.name for p in selected] == ["aider"]
        assert any(e["provider"] == "Github Copilot" for e in excluded)


class TestValidateExecuteSubtaskDelegation:
    def test_disabled_returns_error(self):
        cfg = TGsConfig(delegation_utilities_enabled=False)
        err = validate_execute_subtask_delegation(MagicMock(), cfg, provider_id="opencode")
        assert err is not None
        assert err["error"] == DELEGATION_DISABLED_ERROR

    def test_host_provider_blocked(self):
        cfg = TGsConfig(delegation_utilities_enabled=True)
        err = validate_execute_subtask_delegation(MagicMock(), cfg, provider_id="codex")
        assert err is not None
        assert err["error"] == HOST_DELEGATION_BLOCKED_ERROR

    def test_aider_to_copilot_blocked(self):
        cfg = TGsConfig(delegation_utilities_enabled=True)
        err = validate_execute_subtask_delegation(MagicMock(), cfg, provider_id="github-copilot")
        assert err is not None
        assert err["error"] == HOST_DELEGATION_BLOCKED_ERROR


class TestHandleExecuteSubtaskDelegation:
    def test_host_target_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "delegation.db"
            cfg = TGsConfig(
                db_path=db_path,
                delegation_utilities_enabled=True,
            )
            db = Database(db_path=db_path)
            registry = MagicMock()
            registry._matches_provider = lambda _p, name: name == "codex"
            registry._provider_allowed_as_delegation_target = lambda _p: False
            registry._delegation_target_exclusion_reason = lambda _p: "blocked"
            registry.available_providers = []

            with (
                patch.object(mcp_server, "_client_name", "copilot"),
                patch.object(mcp_server, "_resolve_caller", lambda: "github-copilot"),
                patch.object(mcp_server, "_ensure_init", return_value=(cfg, db, None, None, None)),
                patch.object(mcp_server, "_get_registry_with_config", return_value=registry),
                patch.object(mcp_server, "_register_shell_adapters"),
                patch.object(mcp_server, "would_self_delegate", return_value=False),
            ):
                result = mcp_server.handle_execute_subtask(
                    {"prompt": "hello", "provider_id": "codex"},
                )

        assert result["error"] == HOST_DELEGATION_BLOCKED_ERROR

    def test_disabled_without_provider_id(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "delegation2.db"
            cfg = TGsConfig(db_path=db_path, delegation_utilities_enabled=False)
            db = Database(db_path=db_path)
            registry = MagicMock()
            registry._ordered_execution_candidates.return_value = ([], [])

            with (
                patch.object(mcp_server, "_client_name", "copilot"),
                patch.object(mcp_server, "_ensure_init", return_value=(cfg, db, None, None, None)),
                patch.object(mcp_server, "_get_registry_with_config", return_value=registry),
                patch.object(mcp_server, "_register_shell_adapters"),
                patch.object(mcp_server, "would_self_delegate", return_value=False),
            ):
                result = mcp_server.handle_execute_subtask({"prompt": "hello"})

        assert result["error"] == DELEGATION_DISABLED_ERROR
