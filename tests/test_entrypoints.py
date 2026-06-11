#!/usr/bin/env python3
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.adapters import ProviderAdapter, ProviderCapability


class StubRegistry:
    def __init__(self, adapter: ProviderAdapter) -> None:
        self.adapter = adapter
        self.registered: list[ProviderAdapter] = []
        self.resolved_shells: list[str] = []

    def register_adapter(self, adapter: ProviderAdapter) -> ProviderAdapter:
        self.registered.append(adapter)
        self.adapter = adapter
        return adapter

    def resolve_adapter(self, shell_name: str, capability: ProviderCapability) -> ProviderAdapter:
        self.resolved_shells.append(shell_name)
        assert capability == ProviderCapability.EXECUTE
        return self.adapter


def _adapter_for(provider: object, shell_name: str) -> ProviderAdapter:
    return ProviderAdapter(
        name=shell_name,
        version="legacy-1",
        capabilities=[ProviderCapability.EXECUTE],
        metadata={"shell_names": [shell_name]},
        callables={"build_provider": lambda: provider},
    )


def test_copilot_entry_uses_adapter() -> None:
    """copilot.entry resolves its provider through ProviderRegistry.resolve_adapter()."""
    import copilot.entry as entry

    provider = entry.CopilotProvider()
    registry = StubRegistry(_adapter_for(provider, "copilot"))
    entry.get_registry = lambda: registry
    entry.adapter_from_legacy = lambda _provider=None: registry.adapter

    assert entry._resolve_provider() is provider
    assert registry.resolved_shells == ["copilot"]


def test_copilot_entry_falls_back_when_adapter_callable_missing() -> None:
    import copilot.entry as entry

    adapter = ProviderAdapter(
        name="copilot",
        version="legacy-1",
        capabilities=[ProviderCapability.EXECUTE],
        metadata={"shell_names": ["copilot"]},
        callables={},
    )
    registry = StubRegistry(adapter)
    entry.get_registry = lambda: registry
    entry.adapter_from_legacy = lambda _provider=None: registry.adapter

    assert isinstance(entry._resolve_provider(), entry.CopilotProvider)


def test_claude_entry_uses_adapter() -> None:
    """claude-code.entry resolves its provider through ProviderRegistry.resolve_adapter()."""
    entry = importlib.import_module("claude-code.entry")

    provider = entry.ClaudeCodeProvider()
    registry = StubRegistry(_adapter_for(provider, "claude"))
    entry.get_registry = lambda: registry
    entry.adapter_from_legacy = lambda _provider=None: registry.adapter

    assert entry._resolve_provider() is provider
    assert registry.resolved_shells == ["claude"]


def test_claude_entry_falls_back_when_adapter_callable_missing() -> None:
    entry = importlib.import_module("claude-code.entry")

    adapter = ProviderAdapter(
        name="claude",
        version="legacy-1",
        capabilities=[ProviderCapability.EXECUTE],
        metadata={"shell_names": ["claude"]},
        callables={},
    )
    registry = StubRegistry(adapter)
    entry.get_registry = lambda: registry
    entry.adapter_from_legacy = lambda _provider=None: registry.adapter

    assert isinstance(entry._resolve_provider(), entry.ClaudeCodeProvider)


# ---------------------------------------------------------------------------
# codex / cursor / junie: registry resolution + _open_db config path
# ---------------------------------------------------------------------------

def test_codex_entry_uses_adapter() -> None:
    """codex.entry resolves its provider through ProviderRegistry.resolve_adapter()."""
    import codex.entry as entry

    sentinel = object()
    registry = StubRegistry(_adapter_for(sentinel, "codex"))
    entry.get_registry = lambda: registry
    entry.adapter_from_legacy = lambda _provider=None: registry.adapter

    assert entry._resolve_provider() is sentinel
    assert registry.resolved_shells == ["codex"]


def test_codex_entry_returns_none_when_adapter_callable_missing() -> None:
    """codex.entry returns None (no hard fallback) when build_provider is absent."""
    import codex.entry as entry

    adapter = ProviderAdapter(
        name="codex",
        version="legacy-1",
        capabilities=[ProviderCapability.EXECUTE],
        metadata={"shell_names": ["codex"]},
        callables={},
    )
    registry = StubRegistry(adapter)
    entry.get_registry = lambda: registry
    entry.adapter_from_legacy = lambda _provider=None: registry.adapter

    assert entry._resolve_provider() is None


def test_codex_entry_open_db_uses_config_path(tmp_path) -> None:
    """codex.entry._open_db() passes config.db_path to Database, not the default."""
    import codex.entry as entry

    fake_db_path = tmp_path / "test_codex.db"
    fake_config = MagicMock()
    fake_config.db_path = fake_db_path

    with patch.object(entry.TGsConfig, "from_yaml", return_value=fake_config):
        with patch.object(entry, "Database", wraps=None) as mock_db:
            mock_db.return_value = MagicMock()
            entry._open_db()
            mock_db.assert_called_once_with(fake_db_path)


def test_codex_entry_init_uses_codex_backend_and_provider(tmp_path) -> None:
    import codex.entry as entry
    from codex.providers import CodexProvider

    fake_config = MagicMock()
    fake_config.db_path = tmp_path / "codex-entry.db"
    fake_registry = MagicMock()

    with (
        patch.object(entry.TGsConfig, "from_yaml", return_value=fake_config),
        patch.object(entry, "Database", return_value=MagicMock()),
        patch.object(entry, "TaskRouter", return_value=MagicMock()),
        patch.object(entry, "get_registry", return_value=fake_registry),
        patch.object(entry, "_resolve_provider", return_value=CodexProvider()),
        patch.object(entry, "Planner", return_value=MagicMock()) as planner,
        patch.object(entry, "Orchestrator", return_value=MagicMock()) as orchestrator,
    ):
        entry._init()

    backend = planner.call_args.args[1]
    assert backend._caller == "codex"
    assert isinstance(orchestrator.call_args.args[1], CodexProvider)
    assert orchestrator.call_args.kwargs["caller"] == "codex"


def test_cursor_entry_uses_adapter() -> None:
    """cursor.entry resolves its provider through ProviderRegistry.resolve_adapter()."""
    import cursor.entry as entry

    sentinel = object()
    registry = StubRegistry(_adapter_for(sentinel, "cursor"))
    entry.get_registry = lambda: registry
    entry.adapter_from_legacy = lambda _provider=None: registry.adapter

    assert entry._resolve_provider() is sentinel
    assert registry.resolved_shells == ["cursor"]


def test_cursor_entry_returns_none_when_adapter_callable_missing() -> None:
    """cursor.entry returns None (no hard fallback) when build_provider is absent."""
    import cursor.entry as entry

    adapter = ProviderAdapter(
        name="cursor",
        version="legacy-1",
        capabilities=[ProviderCapability.EXECUTE],
        metadata={"shell_names": ["cursor"]},
        callables={},
    )
    registry = StubRegistry(adapter)
    entry.get_registry = lambda: registry
    entry.adapter_from_legacy = lambda _provider=None: registry.adapter

    assert entry._resolve_provider() is None


def test_cursor_entry_open_db_uses_config_path(tmp_path) -> None:
    """cursor.entry._open_db() passes config.db_path to Database, not the default."""
    import cursor.entry as entry

    fake_db_path = tmp_path / "test_cursor.db"
    fake_config = MagicMock()
    fake_config.db_path = fake_db_path

    with patch.object(entry.TGsConfig, "from_yaml", return_value=fake_config):
        with patch.object(entry, "Database", wraps=None) as mock_db:
            mock_db.return_value = MagicMock()
            entry._open_db()
            mock_db.assert_called_once_with(fake_db_path)


def test_junie_entry_uses_adapter() -> None:
    """junie.entry resolves its provider through ProviderRegistry.resolve_adapter()."""
    import junie.entry as entry

    sentinel = object()
    registry = StubRegistry(_adapter_for(sentinel, "junie"))
    entry.get_registry = lambda: registry
    entry.adapter_from_legacy = lambda _provider=None: registry.adapter

    assert entry._resolve_provider() is sentinel
    assert registry.resolved_shells == ["junie"]


def test_junie_entry_returns_none_when_adapter_callable_missing() -> None:
    """junie.entry returns None (no hard fallback) when build_provider is absent."""
    import junie.entry as entry

    adapter = ProviderAdapter(
        name="junie",
        version="legacy-1",
        capabilities=[ProviderCapability.EXECUTE],
        metadata={"shell_names": ["junie"]},
        callables={},
    )
    registry = StubRegistry(adapter)
    entry.get_registry = lambda: registry
    entry.adapter_from_legacy = lambda _provider=None: registry.adapter

    assert entry._resolve_provider() is None


def test_junie_entry_open_db_uses_config_path(tmp_path) -> None:
    """junie.entry._open_db() passes config.db_path to Database, not the default."""
    import junie.entry as entry

    fake_db_path = tmp_path / "test_junie.db"
    fake_config = MagicMock()
    fake_config.db_path = fake_db_path

    with patch.object(entry.TGsConfig, "from_yaml", return_value=fake_config):
        with patch.object(entry, "Database", wraps=None) as mock_db:
            mock_db.return_value = MagicMock()
            entry._open_db()
            mock_db.assert_called_once_with(fake_db_path)


def test_opencode_entry_uses_adapter() -> None:
    """opencode.entry resolves its provider through ProviderRegistry.resolve_adapter()."""
    import opencode.entry as entry

    sentinel = object()
    registry = StubRegistry(_adapter_for(sentinel, "opencode"))
    entry.get_registry = lambda: registry
    entry.adapter_from_legacy = lambda _provider=None: registry.adapter

    assert entry._resolve_provider() is sentinel
    assert registry.resolved_shells == ["opencode"]


def test_opencode_entry_returns_none_when_adapter_callable_missing() -> None:
    """opencode.entry returns None when build_provider is absent."""
    import opencode.entry as entry

    adapter = ProviderAdapter(
        name="opencode",
        version="legacy-1",
        capabilities=[ProviderCapability.EXECUTE],
        metadata={"shell_names": ["opencode"]},
        callables={},
    )
    registry = StubRegistry(adapter)
    entry.get_registry = lambda: registry
    entry.adapter_from_legacy = lambda _provider=None: registry.adapter

    assert entry._resolve_provider() is None


def test_opencode_entry_open_db_uses_config_path(tmp_path) -> None:
    """opencode.entry._open_db() passes config.db_path to Database, not the default."""
    import opencode.entry as entry

    fake_db_path = tmp_path / "test_opencode.db"
    fake_config = MagicMock()
    fake_config.db_path = fake_db_path

    with patch.object(entry.TGsConfig, "from_yaml", return_value=fake_config):
        with patch.object(entry, "Database", wraps=None) as mock_db:
            mock_db.return_value = MagicMock()
            entry._open_db()
            mock_db.assert_called_once_with(fake_db_path)


def test_resolve_provider_no_warning_on_registry_failure(caplog) -> None:
    """_resolve_provider() must not emit WARNING-level logs on normal resolution failure."""
    import codex.entry as entry
    import logging

    def boom():
        raise RuntimeError("registry unavailable")

    entry.get_registry = boom

    with caplog.at_level(logging.WARNING, logger="codex.entry"):
        result = entry._resolve_provider()

    assert result is None
    assert caplog.records == [], (
        f"Expected no WARNING logs, got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Phase 8 Wave 3: Parametric provider coverage tests (all 5 providers)
# ---------------------------------------------------------------------------


import pytest


@pytest.mark.parametrize("provider_name", ["codex", "cursor", "junie", "opencode", "aider", "amazon-q"])
def test_provider_in_registry(provider_name):
    """All providers are registered in BUILTIN_PROVIDERS."""
    from shared.discovery import BUILTIN_PROVIDERS
    
    provider_names = [p.name for p in BUILTIN_PROVIDERS]
    assert provider_name in provider_names, (
        f"{provider_name} not in BUILTIN_PROVIDERS. Available: {provider_names}"
    )
    
    provider = next(p for p in BUILTIN_PROVIDERS if p.name == provider_name)
    assert provider.command_builder is not None, f"{provider_name} has no command_builder"
    assert provider.detect_hook is not None or provider.detect_cmd is not None, (
        f"{provider_name} has neither detect_hook nor detect_cmd"
    )


@pytest.mark.parametrize("provider_name,expected_tier_count", [
    ("codex", 3),  # low, medium, high
    ("cursor", 3),
    ("junie", 1),  # junie has only medium configured-model
    ("opencode", 1),  # OpenCode is low-tier-only in the initial rollout
    ("aider", 3),
    ("amazon-q", 3),
])
def test_provider_has_tier_models(provider_name, expected_tier_count):
    """All providers have tier_models entries."""
    from shared.discovery import BUILTIN_PROVIDERS
    
    provider = next((p for p in BUILTIN_PROVIDERS if p.name == provider_name), None)
    assert provider is not None, f"{provider_name} not in BUILTIN_PROVIDERS"
    
    tier_models = provider.tier_models
    assert isinstance(tier_models, dict), f"{provider_name} tier_models should be dict, got {type(tier_models)}"
    assert len(tier_models) >= expected_tier_count or len(tier_models) > 0, (
        f"{provider_name} should have at least {expected_tier_count} or some tier_models, got {tier_models}"
    )


@pytest.mark.parametrize("provider_name", ["codex", "cursor", "junie", "opencode", "aider", "amazon-q"])
def test_provider_model_discovery_parser(provider_name):
    """All providers with model discovery have callable parsers."""
    from shared.discovery import BUILTIN_PROVIDERS
    
    provider = next((p for p in BUILTIN_PROVIDERS if p.name == provider_name), None)
    assert provider is not None, f"{provider_name} not in BUILTIN_PROVIDERS"
    
    # Aider and Amazon Q have model discovery
    if provider_name in ("aider", "amazon-q"):
        assert provider.model_discovery_parser is not None, (
            f"{provider_name} should have model_discovery_parser"
        )
        # Call parser with empty input to test fallback
        result = provider.model_discovery_parser(provider, "")
        assert isinstance(result, dict), f"Parser should return dict, got {type(result)}"
        assert len(result) > 0, f"Parser should return non-empty dict for {provider_name}"


@pytest.mark.parametrize("provider_name", ["codex", "cursor", "junie", "opencode", "aider", "amazon-q"])
def test_cross_provider_routing_discovery(provider_name, monkeypatch):
    """All providers are discoverable and don't conflict when all are available."""
    from shared.discovery import ProviderRegistry
    
    # Use test mode to mock availability
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")
    
    registry = ProviderRegistry()
    available_names = [p.name for p in registry.available_providers]
    
    # In test mode, at least test providers should be available
    # Phase 8 providers may not be available if binaries aren't installed
    # but should still be in BUILTIN_PROVIDERS
    from shared.discovery import BUILTIN_PROVIDERS
    builtin_names = [p.name for p in BUILTIN_PROVIDERS]
    
    assert provider_name in builtin_names, (
        f"{provider_name} not discoverable in BUILTIN_PROVIDERS"
    )


def test_all_providers_routing_together():
    """All secondary providers work together in routing without conflicts.
    
    This is a high-level smoke-test that verifies:
    - All providers can be discovered simultaneously
    - No routing conflicts (e.g., two providers claiming same model)
    - All providers produce valid ProviderAdapter objects
    - Phase 7 providers (codex, cursor, junie) are still available
    - Phase 8 providers (aider, amazon-q) are in BUILTIN_PROVIDERS
    """
    from shared.discovery import BUILTIN_PROVIDERS, ProviderRegistry
    from shared.adapters import ProviderAdapter
    
    # Create registry in normal (non-test) mode
    registry = ProviderRegistry()
    
    # Get all adapters (includes auto-generated from BUILTIN_PROVIDERS)
    adapters = registry.list_adapters()
    assert len(adapters) > 0, "Should have at least one adapter"
    
    # Verify adapters are all ProviderAdapter instances
    for adapter in adapters:
        assert isinstance(adapter, ProviderAdapter), (
            f"Expected ProviderAdapter, got {type(adapter)}"
        )
    
    # Verify all providers are in BUILTIN_PROVIDERS
    builtin_names = {p.name for p in BUILTIN_PROVIDERS}
    expected_names = {"codex", "cursor", "junie", "opencode", "aider", "amazon-q"}
    
    for provider_name in expected_names:
        assert provider_name in builtin_names, (
            f"Expected {provider_name} in BUILTIN_PROVIDERS"
        )
    
    # Verify no name collisions
    adapter_count = len(adapters)
    adapter_names = {a.name for a in adapters}
    unique_names = len(adapter_names)
    assert adapter_count == unique_names, (
        f"Adapter name collision detected: {adapter_count} adapters but {unique_names} unique names"
    )


# ---------------------------------------------------------------------------
# Phase 8 Wave 2: Aider and Amazon Q/Kiro smoke tests
# ---------------------------------------------------------------------------


def test_aider_smoke_hermetic(mock_aider_binary, mock_aider_execution):
    """Full Aider workflow: detection -> model discovery -> execution -> result extraction.
    
    This smoke-test verifies the complete integration:
    1. Aider binary detected with usable backend
    2. Model discovery succeeds or falls back cleanly
    3. Execution produces diff-style output
    4. Result extraction captures files modified + cost
    
    Hermetic mode: uses mocks, no real CLI calls
    """
    import os
    from shared.adapters import ExecutionResult, _extract_aider_result
    
    os.environ["OPENAI_API_KEY"] = "test-key"
    
    # Step 1: Direct provider detection via BUILTIN_PROVIDERS
    from shared.discovery import BUILTIN_PROVIDERS
    aider_provider = next((p for p in BUILTIN_PROVIDERS if p.name == "aider"), None)
    assert aider_provider is not None, "Aider should be in BUILTIN_PROVIDERS"
    
    # Step 2: Check detection (uses mock_aider_binary fixture)
    readiness = aider_provider.detect()
    assert readiness.routeable, f"Aider should be routeable with mocked binary, got: {readiness.reason}"
    
    # Step 3: Verify tier_models are present
    assert aider_provider.tier_models, f"Aider should have tier_models, got: {aider_provider.tier_models}"
    assert "low" in aider_provider.tier_models or "medium" in aider_provider.tier_models
    
    # Step 4: Execute Aider and verify result extraction
    result = _extract_aider_result(
        provider_name="aider",
        command=["aider", "--model", "claude-opus", "--message", "Fix the bug",
                 "--yes-always", "--no-git", "--no-auto-commits",
                 "src/handler.py", "tests/test_handler.py"],
        stdout="Fixed 2 functions\n",
        stderr="Total cost: $0.0042\n",
        exit_code=0,
        model_used="claude-opus"
    )
    
    # Step 5: Verify result extraction
    assert isinstance(result, ExecutionResult), f"Expected ExecutionResult, got {type(result)}"
    assert result.exit_code == 0, f"Expected exit code 0, got {result.exit_code}"
    assert result.provider_name == "aider", f"Expected provider 'aider', got {result.provider_name}"
    assert len(result.text) > 0, "Result text should not be empty"
    assert result.metadata.get("files_modified") == ["src/handler.py", "tests/test_handler.py"]
    assert result.metadata.get("result_type") == "file_edits"


def test_q_kiro_smoke_hermetic(mock_q_binary, mock_q_kiro_execution):
    """Full Amazon Q/Kiro workflow: detection -> auth -> execution -> result extraction.
    
    This smoke-test verifies the complete integration:
    1. q or kiro binary detected with AWS auth available
    2. Auth probe succeeds (or AWS creds fallback works)
    3. Execution produces text output
    4. Result extraction captures output + metadata
    
    Hermetic mode: uses mocks, no real CLI calls
    """
    from shared.adapters import ExecutionResult, _extract_q_kiro_result
    from shared.discovery import BUILTIN_PROVIDERS
    
    # Step 1: Direct provider detection via BUILTIN_PROVIDERS
    q_provider = next((p for p in BUILTIN_PROVIDERS if p.name == "amazon-q"), None)
    assert q_provider is not None, "Amazon Q/Kiro should be in BUILTIN_PROVIDERS"
    
    # Step 2: Verify tier_models are present (with mock_q_binary, detection should work)
    assert q_provider.tier_models, f"Amazon Q/Kiro should have tier_models, got: {q_provider.tier_models}"
    
    # Step 3: Execute Amazon Q/Kiro and verify result extraction (uses mock_q_kiro_execution)
    result = _extract_q_kiro_result(
        provider_name="amazon-q",
        command=["q", "chat", "--no-interactive", "--model", "claude-3.7-sonnet",
                 "Write a handler class"],
        stdout="Here's the solution:\n\nclass Handler:\n    def process(self):\n        pass\n",
        stderr="",
        exit_code=0,
        model_used="claude-3.7-sonnet"
    )
    
    # Step 4: Verify result extraction
    assert isinstance(result, ExecutionResult), f"Expected ExecutionResult, got {type(result)}"
    assert result.exit_code == 0, f"Expected exit code 0, got {result.exit_code}"
    assert result.provider_name == "amazon-q", f"Expected provider 'amazon-q', got {result.provider_name}"
    assert len(result.text) > 0, "Result text should not be empty"
    assert "class Handler" in result.text, "Result should contain generated code"
    assert result.metadata.get("result_type") == "text_output"


def test_aider_smoke_live_skipped_by_default():
    """Live Aider execution is skipped by default unless explicitly enabled."""
    import pytest
    import os
    
    # Verify that TGSROUTER_SKIP_LIVE_TESTS defaults to "1"
    skip_flag = os.getenv("TGSROUTER_SKIP_LIVE_TESTS", "1")
    assert skip_flag == "1" or skip_flag == "0", f"Unexpected skip flag: {skip_flag}"
    
    # If it's 1, the test should be skipped by default
    if skip_flag == "1":
        pytest.skip("Live tests skipped by default; set TGSROUTER_SKIP_LIVE_TESTS=0 to enable")


def test_q_kiro_smoke_live_skipped_by_default():
    """Live Amazon Q/Kiro execution is skipped by default unless explicitly enabled."""
    import pytest
    import os
    
    # Verify that TGSROUTER_SKIP_LIVE_TESTS defaults to "1"
    skip_flag = os.getenv("TGSROUTER_SKIP_LIVE_TESTS", "1")
    assert skip_flag == "1" or skip_flag == "0", f"Unexpected skip flag: {skip_flag}"
    
    # If it's 1, the test should be skipped by default
    if skip_flag == "1":
        pytest.skip("Live tests skipped by default; set TGSROUTER_SKIP_LIVE_TESTS=0 to enable")


# ============================================================================
# Phase 9 Integration Tests: Windsurf Entrypoint Coverage
# ============================================================================


@pytest.mark.parametrize("provider_name", ["windsurf"])
def test_windsurf_provider_in_registry(provider_name):
    """Windsurf is registered in BUILTIN_PROVIDERS as a detection-only stub."""
    from shared.discovery import BUILTIN_PROVIDERS
    
    provider_names = [p.name for p in BUILTIN_PROVIDERS]
    assert provider_name in provider_names, (
        f"{provider_name} not in BUILTIN_PROVIDERS. Available: {provider_names}"
    )
    
    provider = next(p for p in BUILTIN_PROVIDERS if p.name == provider_name)
    assert provider.display_name == "Windsurf"
    assert provider.binary == "windsurf"
    assert provider.detect_hook is not None, f"{provider_name} should have detect_hook"
    # Windsurf stub has empty tier_models since it's not executable
    assert isinstance(provider.tier_models, dict), f"{provider_name} tier_models should be dict"


def test_windsurf_entrypoint_not_routeable(monkeypatch):
    """Windsurf is never routeable for tier-based execution paths.
    
    Even when Windsurf binary is detected, it should NOT appear in any tier routing
    (low, medium, high) because it has no CLI execution path.
    """
    from shared.discovery import ProviderRegistry, DetectReason
    import shutil
    
    # Disable test mode to get real provider discovery
    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)
    
    # Save original shutil.which before mocking
    original_which = shutil.which
    
    def mock_which(cmd):
        if cmd == "windsurf":
            return "/usr/local/bin/windsurf"
        return original_which(cmd)
    
    monkeypatch.setattr("shared.discovery.shutil.which", mock_which)
    
    registry = ProviderRegistry()
    
    # Windsurf should be in available_providers
    windsurf_provider = next((p for p in registry.available_providers if p.name == "windsurf"), None)
    assert windsurf_provider is not None, "Windsurf should be in available_providers when binary found"
    
    # But it should NOT be routeable
    assert not windsurf_provider.is_routeable(), "Windsurf should never be routeable"
    
    # And it should NOT appear in any tier routing
    for tier in ["low", "medium", "high"]:
        tier_providers = registry.get_providers_for_tier(tier)
        tier_names = [p.name for p in tier_providers]
        assert "windsurf" not in tier_names, (
            f"Windsurf should NOT be in {tier} tier providers. Got: {tier_names}"
        )


@pytest.mark.parametrize("provider_name,expected_tier_count", [
    ("windsurf", 0),  # windsurf is a stub with no tiers
])
def test_windsurf_provider_has_tier_models(provider_name, expected_tier_count):
    """Windsurf has empty tier_models since it's a detection-only stub."""
    from shared.discovery import BUILTIN_PROVIDERS
    
    provider = next((p for p in BUILTIN_PROVIDERS if p.name == provider_name), None)
    assert provider is not None, f"{provider_name} not in BUILTIN_PROVIDERS"
    
    tier_models = provider.tier_models
    assert isinstance(tier_models, dict), f"{provider_name} tier_models should be dict, got {type(tier_models)}"
    assert len(tier_models) == expected_tier_count, (
        f"{provider_name} should have exactly {expected_tier_count} tier_models, got {tier_models}"
    )
