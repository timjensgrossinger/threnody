#!/usr/bin/env python3
"""Tests for the optional user-facing `effort` setting.

Covers:
- explicit effort override rejection for unsupported providers
- config-default effort annotation (continue) for unsupported providers
- explicit effort pass-through for supported providers
"""
from pathlib import Path
import tempfile

import mcp_server
from shared.config import TGsConfig
from shared.db import Database


class StubGitHubRegistry:
    """Registry that selects GitHub Copilot (unsupported explicit effort)."""

    def select_provider_for_tier(self, tier: str, **_kwargs):
        is_free = tier == "low"
        return {
            "provider": "GitHub Copilot",
            "provider_id": "github-copilot",
            "model": "gpt-5-mini" if tier == "low" else "gpt-5.4",
            "tier": tier,
            "is_free": is_free,
            "billing_tier": "free" if is_free else "subscription",
            "provider_cost_hint": "free" if is_free else "included in subscription/quota",
            "cost_rank": 0 if is_free else 2,
            "billing_source": "user_override" if is_free else "provider_default",
            "excluded_providers": [],
        }

    def execute_cheapest(self, **_kwargs):
        return {
            "result": "ok",
            "provider": "GitHub Copilot",
            "provider_id": "github-copilot",
            "model": "gpt-5-mini",
            "tier": "low",
            "is_free": True,
            "billing_tier": "free",
            "provider_cost_hint": "free",
            "cost_rank": 0,
            "billing_source": "user_override",
            "fallback_used": False,
        }


class StubCodexRegistry:
    """Registry that selects Codex (supports explicit effort)."""

    def select_provider_for_tier(self, tier: str, **_kwargs):
        return {
            "provider": "Codex",
            "provider_id": "codex",
            "model": "codex-mini" if tier == "low" else "codex-large",
            "tier": tier,
            "is_free": False,
            "billing_tier": "subscription",
            "provider_cost_hint": "included in subscription/quota",
            "cost_rank": 2,
            "billing_source": "provider_default",
            "excluded_providers": [],
        }

    def execute_cheapest(self, **kwargs):
        # Echo back effort if present so we can assert it was passed through
        return {
            "result": "ok",
            "provider": "Codex",
            "provider_id": "codex",
            "model": kwargs.get("model", "codex-mini"),
            "tier": kwargs.get("tier", "low"),
            "is_free": False,
            "billing_tier": "subscription",
            "provider_cost_hint": "included in subscription/quota",
            "cost_rank": 2,
            "billing_source": "provider_default",
            "fallback_used": False,
            "effort": kwargs.get("effort"),
            "effort_source": "explicit" if kwargs.get("effort") else None,
        }


def test_execute_subtask_rejects_explicit_effort_for_unsupported_provider(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "effort.db"
        cfg = TGsConfig(db_path=db_path, delegation_utilities_enabled=True)
        db = Database(db_path=db_path)

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubGitHubRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Hello",
            "effort": "high",
        })

        assert result.get("error") == "UnsupportedEffortOverride"
        assert "cannot be honored" in result.get("details", "").lower()


def test_execute_subtask_annotates_config_default_effort_for_unsupported_provider(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "effort.db"
        cfg = TGsConfig(db_path=db_path, delegation_utilities_enabled=True)
        # configure a provider default for github-copilot (unsupported explicit effort)
        cfg.provider_effort_defaults = {"github-copilot": {"low": "careful"}}
        db = Database(db_path=db_path)

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubGitHubRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Hello",
            # no explicit effort override here — rely on config default
        })

        # Should continue and annotate effort from config with source=config_default
        assert result.get("provider") == "GitHub Copilot"
        assert result.get("effort") == "careful"
        assert result.get("effort_source") == "config_default"


def test_execute_subtask_allows_explicit_effort_for_supported_provider(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "effort.db"
        cfg = TGsConfig(db_path=db_path, delegation_utilities_enabled=True)
        db = Database(db_path=db_path)

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubCodexRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Hello",
            "effort": "high",
        })

        assert result.get("provider") == "Codex"
        assert result.get("effort") == "high"
        assert result.get("effort_source") == "explicit"
