#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server
from shared.adapters import ProviderAdapter, ProviderCapability
from shared.config import TGsConfig
from shared.db import Database
from shared.discovery import CLIProvider, DetectReason, ProviderReadiness, ProviderRegistry


class StubRegistry:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def register_adapter(self, _adapter: ProviderAdapter) -> None:
        return None

    def execute_cheapest(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "result": "ok",
            "provider": "GitHub Copilot",
            "model": "gpt-5-mini",
            "tier": kwargs["tier"],
            "fallback_used": False,
            "excluded_providers": [],
        }

    def to_dict(self) -> dict[str, object]:
        return {"providers": []}


def test_provenance_injected() -> None:
    """handle_execute_subtask persists provenance.trace_id/depth/caller_id."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "provenance.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        registry = StubRegistry()

        with (
            patch.object(mcp_server, "_client_name", "copilot"),
            patch.object(mcp_server, "_ensure_init", return_value=(cfg, db, None, None, None)),
            patch.object(mcp_server, "get_registry", return_value=registry),
        ):
            result = mcp_server.handle_execute_subtask({"prompt": "hello"})

        assert result["provenance"]["depth"] == 1
        assert result["provenance"]["caller_id"] == "github-copilot"

        with db.conn() as conn:
            row = conn.execute(
                "SELECT provenance_trace_id, provenance_depth, provenance_caller_id "
                "FROM telemetry WHERE task_hash = ?",
                (result["task_id"],),
            ).fetchone()

        assert row is not None
        assert row[0] == result["provenance"]["trace_id"]
        assert row[1] == 1
        assert row[2] == "github-copilot"


def test_recursion_depth_enforced() -> None:
    """Depth >2 returns an explicit recursion error before provider execution."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "depth.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        registry = StubRegistry()

        with (
            patch.object(mcp_server, "_ensure_init", return_value=(cfg, db, None, None, None)),
            patch.object(mcp_server, "get_registry", return_value=registry),
        ):
            result = mcp_server.handle_execute_subtask({
                "prompt": "hello",
                "provenance": {
                    "trace_id": "trace-123",
                    "depth": 2,
                    "caller_id": "claude-code",
                },
            })

        assert result["error"] == "RecursionDepthError"
        assert registry.calls == []


def test_provider_opt_out(monkeypatch) -> None:
    """Registry skips adapters that opt out of handling the current caller."""
    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)
    monkeypatch.setattr("shared.discovery._LOCAL_ENDPOINT_CANDIDATES", ())
    skipped = MagicMock(spec=CLIProvider)
    skipped.name = "claude-code"
    skipped.display_name = "Claude Code"
    skipped.binary = "claude"
    skipped.tier_models = {"low": "haiku"}
    skipped.cost_rank = {"low": 0}
    skipped.detect.return_value = ProviderReadiness(routeable=True, reason=DetectReason.READY)
    skipped.execute.return_value = "should-not-run"

    fallback = MagicMock(spec=CLIProvider)
    fallback.name = "github-copilot"
    fallback.display_name = "GitHub Copilot"
    fallback.binary = "gh"
    fallback.tier_models = {"low": "gpt-5-mini"}
    fallback.cost_rank = {"low": 1}
    fallback.detect.return_value = ProviderReadiness(routeable=True, reason=DetectReason.READY)
    fallback.execute.return_value = "ok"

    with (
        patch("shared.discovery.BUILTIN_PROVIDERS", [skipped, fallback]),
        patch("shared.discovery._LOCAL_ENDPOINT_CANDIDATES", ()),
    ):
        registry = ProviderRegistry()

    registry.register_adapter(
        ProviderAdapter(
            name="claude",
            version="legacy-1",
            capabilities=[ProviderCapability.EXECUTE],
            metadata={
                "shell_names": ["claude", "claude-code"],
                "opt_out": True,
                "opt_out_reason": "claude-code",
            },
        )
    )

    result = registry.execute_cheapest("hello", tier="low", caller="claude-code")

    skipped.execute.assert_not_called()
    fallback.execute.assert_called_once()
    assert result["result"] == "ok"
    assert result["excluded_providers"][0]["provider"] == "Claude Code"


def test_safe_self_hosted_code_only_prefers_free_copilot(monkeypatch) -> None:
    """Sandboxed code-only execution may use self-hosted Copilot to keep low tier free."""
    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)
    monkeypatch.setattr("shared.discovery._LOCAL_ENDPOINT_CANDIDATES", ())
    copilot = MagicMock(spec=CLIProvider)
    copilot.name = "github-copilot"
    copilot.display_name = "GitHub Copilot"
    copilot.binary = "gh"
    copilot.tier_models = {"low": "gpt-5-mini"}
    copilot.cost_rank = {"low": 0}
    copilot.billing_model = "subscription"
    copilot.safe_self_hosted_code_only = True
    copilot.detect.return_value = ProviderReadiness(routeable=True, reason=DetectReason.READY)
    copilot.execute.return_value = "copilot-result"

    fallback = MagicMock(spec=CLIProvider)
    fallback.name = "claude-code"
    fallback.display_name = "Claude Code"
    fallback.binary = "claude"
    fallback.tier_models = {"low": "haiku"}
    fallback.cost_rank = {"low": 1}
    fallback.billing_model = "subscription"
    fallback.safe_self_hosted_code_only = False
    fallback.detect.return_value = ProviderReadiness(routeable=True, reason=DetectReason.READY)
    fallback.execute.return_value = "claude-result"

    with patch("shared.discovery.BUILTIN_PROVIDERS", [copilot, fallback]):
        registry = ProviderRegistry()

    registry.register_adapter(
        ProviderAdapter(
            name="copilot",
            version="legacy-1",
            capabilities=[ProviderCapability.EXECUTE],
            metadata={
                "shell_names": ["copilot", "github-copilot", "gh"],
                "opt_out": True,
                "opt_out_reason": "copilot",
            },
        )
    )

    result = registry.execute_cheapest(
        "write a helper",
        tier="low",
        caller="github-copilot",
        code_only=True,
    )

    copilot.execute.assert_called_once()
    fallback.execute.assert_not_called()
    assert result["provider"] == "GitHub Copilot"
    assert result["model"] == "gpt-5-mini"
    assert result["is_free"] is True
    assert result["billing_tier"] == "free"
    assert result["provider_cost_hint"] == "free"


def test_self_hosted_code_only_bypass_stays_low_tier_only(monkeypatch) -> None:
    """Medium-tier code-only work must still honor self-hosted opt-out."""
    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)
    monkeypatch.setattr("shared.discovery._LOCAL_ENDPOINT_CANDIDATES", ())
    copilot = MagicMock(spec=CLIProvider)
    copilot.name = "github-copilot"
    copilot.display_name = "GitHub Copilot"
    copilot.binary = "gh"
    copilot.tier_models = {"medium": "gpt-5.4"}
    copilot.cost_rank = {"medium": 2}
    copilot.billing_model = "subscription"
    copilot.safe_self_hosted_code_only = True
    copilot.detect.return_value = ProviderReadiness(routeable=True, reason=DetectReason.READY)
    copilot.execute.return_value = "copilot-result"

    fallback = MagicMock(spec=CLIProvider)
    fallback.name = "claude-code"
    fallback.display_name = "Claude Code"
    fallback.binary = "claude"
    fallback.tier_models = {"medium": "sonnet"}
    fallback.cost_rank = {"medium": 3}
    fallback.billing_model = "subscription"
    fallback.safe_self_hosted_code_only = False
    fallback.detect.return_value = ProviderReadiness(routeable=True, reason=DetectReason.READY)
    fallback.execute.return_value = "claude-result"

    with patch("shared.discovery.BUILTIN_PROVIDERS", [copilot, fallback]):
        registry = ProviderRegistry()

    registry.register_adapter(
        ProviderAdapter(
            name="copilot",
            version="legacy-1",
            capabilities=[ProviderCapability.EXECUTE],
            metadata={
                "shell_names": ["copilot", "github-copilot", "gh"],
                "opt_out": True,
                "opt_out_reason": "copilot",
            },
        )
    )

    result = registry.execute_cheapest(
        "write a medium-tier helper",
        tier="medium",
        caller="github-copilot",
        code_only=True,
    )

    copilot.execute.assert_not_called()
    fallback.execute.assert_called_once()
    assert result["provider"] == "Claude Code"
    assert result["model"] == "sonnet"
