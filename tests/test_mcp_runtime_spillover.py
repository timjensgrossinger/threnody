#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
from shared.adapters import ProviderAdapter, ProviderCapability
from shared.config import ProviderUsageWindowConfig, RoutingPreference, TGsConfig, UsageWindowEntry
from shared.orchestrator import Provider
from shared.planner import Subtask


class _FakeRuntimeProvider(Provider):
    def __init__(self, primary: str) -> None:
        self._primary = primary

    def resolve_model(self, tier: str) -> str:
        return f"{self._primary}-{tier}"

    def execute(self, subtask: Subtask, model: str, timeout: int = 120) -> str | None:
        return f"{model}:{subtask.id}"

    def available_tiers(self) -> list[str]:
        return ["low", "medium", "high"]

    def provider_info(self) -> dict[str, str]:
        return {"primary": self._primary}


class _FakeCLIProvider:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeRegistry:
    def __init__(self, config_overrides=None, db=None) -> None:
        self._config_overrides = config_overrides
        self.available_providers = [
            _FakeCLIProvider("github-copilot"),
            _FakeCLIProvider("cursor"),
        ]
        self._adapters = [
            ProviderAdapter(
                name="copilot",
                version="legacy-1",
                capabilities=[ProviderCapability.EXECUTE],
                metadata={"shell_names": ["github-copilot", "gh"]},
                callables={"build_provider": lambda: _FakeRuntimeProvider("github-copilot")},
            ),
            ProviderAdapter(
                name="cursor",
                version="legacy-1",
                capabilities=[ProviderCapability.EXECUTE],
                metadata={"shell_names": ["cursor", "cursor-agent"]},
                callables={"build_provider": lambda: None},
            ),
        ]

    def register_adapter(self, adapter: ProviderAdapter) -> ProviderAdapter:
        self._adapters.append(adapter)
        return adapter

    def list_adapters_supporting(
        self,
        capability: ProviderCapability | str,
    ) -> list[ProviderAdapter]:
        return [adapter for adapter in self._adapters if adapter.supports(capability)]


def test_build_runtime_spillover_support_filters_unbuildable_providers(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "ProviderRegistry", _FakeRegistry)
    monkeypatch.setattr(mcp_server, "_register_shell_adapters", lambda _registry: None)

    registry, providers_map = mcp_server._build_runtime_spillover_support(TGsConfig())

    assert "github-copilot" in providers_map
    assert "gh" in providers_map
    assert "cursor" not in providers_map
    assert [provider.name for provider in registry.available_providers] == ["github-copilot"]


def test_build_runtime_providers_map_skips_incompatible_provider_objects() -> None:
    class _NonProvider:
        pass

    class _Registry:
        def list_adapters_supporting(self, capability):
            return [
                ProviderAdapter(
                    name="good",
                    version="legacy-1",
                    capabilities=[ProviderCapability.EXECUTE],
                    metadata={"shell_names": ["good-shell"]},
                    callables={"build_provider": lambda: _FakeRuntimeProvider("good-shell")},
                ),
                ProviderAdapter(
                    name="bad",
                    version="legacy-1",
                    capabilities=[ProviderCapability.EXECUTE],
                    metadata={"shell_names": ["bad-shell"]},
                    callables={"build_provider": lambda: _NonProvider()},
                ),
            ]

    providers_map = mcp_server._build_runtime_providers_map(_Registry())

    assert "good-shell" in providers_map
    assert "bad-shell" not in providers_map


def test_build_runtime_providers_map_keeps_first_duplicate_alias() -> None:
    provider_one = _FakeRuntimeProvider("github-copilot")
    provider_two = _FakeRuntimeProvider("github-copilot")

    class _DuplicateRegistry:
        def list_adapters_supporting(self, capability):
            return [
                ProviderAdapter(
                    name="copilot-a",
                    version="legacy-1",
                    capabilities=[ProviderCapability.EXECUTE],
                    metadata={"shell_names": ["shared-alias"]},
                    callables={"build_provider": lambda: provider_one},
                ),
                ProviderAdapter(
                    name="copilot-b",
                    version="legacy-1",
                    capabilities=[ProviderCapability.EXECUTE],
                    metadata={"shell_names": ["shared-alias"]},
                    callables={"build_provider": lambda: provider_two},
                ),
            ]

    providers_map = mcp_server._build_runtime_providers_map(_DuplicateRegistry())

    assert providers_map["shared-alias"] is provider_one


def test_registry_config_overrides_include_caller_routing_and_usage_windows(tmp_path: Path) -> None:
    cfg = TGsConfig(db_path=tmp_path / "registry-overrides.db")
    cfg.preferred_routing = {
        "low": [RoutingPreference(provider="github-copilot")],
    }
    cfg.preferred_routing_by_caller = {
        "claude-code": {
            "low": [
                RoutingPreference(provider="claude-code"),
                RoutingPreference(provider="mistral-vibe"),
            ],
        },
    }
    cfg.provider_usage_windows = {
        "Claude_Code": ProviderUsageWindowConfig(
            windows=[
                UsageWindowEntry(
                    hours=5,
                    budget_tokens=500_000,
                    threshold=0.85,
                    action="prefer_alternatives",
                )
            ]
        )
    }

    overrides = mcp_server._registry_config_overrides(cfg)

    assert overrides["preferred_routing"] == {
        "low": [{"provider": "github-copilot"}],
    }
    assert overrides["preferred_routing_by_caller"] == {
        "claude-code": {
            "low": [
                {"provider": "claude-code"},
                {"provider": "mistral-vibe"},
            ],
        },
    }
    assert overrides["provider_usage_windows"] == {
        "claude-code": {
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


def test_ensure_init_wires_runtime_registry_into_orchestrator(monkeypatch, tmp_path: Path) -> None:
    cfg = TGsConfig(db_path=tmp_path / "runtime-spillover.db")
    fake_db = MagicMock()
    fake_router = MagicMock()
    fake_planner = MagicMock()
    fake_provider = MagicMock()
    fake_registry = MagicMock()
    fake_map = {"github-copilot": MagicMock()}
    captured: dict[str, object] = {}

    class _FakeOrchestrator:
        def __init__(self, config, provider, planner, db, **kwargs) -> None:
            captured["config"] = config
            captured["provider"] = provider
            captured["planner"] = planner
            captured["db"] = db
            captured.update(kwargs)

    monkeypatch.setattr(mcp_server, "_config", None)
    monkeypatch.setattr(mcp_server, "_db", None)
    monkeypatch.setattr(mcp_server, "_router", None)
    monkeypatch.setattr(mcp_server, "_planner", None)
    monkeypatch.setattr(mcp_server, "_orchestrator", None)
    monkeypatch.setattr(mcp_server, "_model_catalog", None)
    monkeypatch.setattr(mcp_server, "_ensure_bg_loop", lambda: None)
    monkeypatch.setattr(mcp_server, "_schedule_model_catalog_refresh", lambda: None)
    monkeypatch.setattr(mcp_server.TGsConfig, "from_yaml", lambda: cfg)
    monkeypatch.setattr(mcp_server, "Database", lambda _path, **_kw: fake_db)
    monkeypatch.setattr(mcp_server, "TaskRouter", lambda _config: fake_router)
    monkeypatch.setattr(
        mcp_server, "Planner", lambda _config, _backend, _db, **_kw: fake_planner
    )
    monkeypatch.setattr(mcp_server, "CopilotProvider", lambda: fake_provider)
    monkeypatch.setattr(mcp_server, "ModelCatalog", lambda _db: MagicMock())
    monkeypatch.setattr(
        mcp_server,
        "_build_runtime_spillover_support",
        lambda _config, db=None: (fake_registry, fake_map),
    )
    monkeypatch.setattr(mcp_server, "Orchestrator", _FakeOrchestrator)

    _config, _db, _router, _planner, _orchestrator = mcp_server._ensure_init()

    assert _config is cfg
    assert _db is fake_db
    assert _router is fake_router
    assert _planner is fake_planner
    assert captured["provider_registry"] is fake_registry
    assert captured["providers_map"] is fake_map
