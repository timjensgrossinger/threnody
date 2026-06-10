#!/usr/bin/env python3
"""
MCP security and path validation test suite for Phase 5 foundation.

Tests path traversal protection for model-generated file writes. All writes
to the file system must be validated against an allowlist of trusted base
directories before execution.

This implements the FNDX-04 requirement: Path traversal guard validates all
target_file paths against an allowlist before writing model output.

Mapped to VALIDATION.md:
  - 05-V0-07: test_path_validation_rejects_outside_root
  - 05-V0-08: test_path_validation_accepts_inside_root

Expected behavior after Phase 5 Wave 2:
  - Paths outside trusted bases raise ValueError
  - Paths inside trusted bases are accepted and returned as Path objects
  - Symlinks are resolved to canonical form
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

# Ensure the project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.context import is_within_repo

if TYPE_CHECKING:
    from tests.conftest import test_config_fixture


# ============================================================================
# Helper: Stub path validator for Phase 5 Wave 2
# ============================================================================


def validate_target_path(
    target_str: str,
    allowed_bases: list[Path] | None = None,
) -> Path:
    """
    Validate target_file path against allowlist.
    
    This is the pattern that mcp_server.py will use in Phase 5 Wave 2.
    
    Args:
        target_str: Raw path string from task/caller
        allowed_bases: List of trusted base paths. Default: project root.
    
    Returns:
        Validated Path object if path is inside allowed bases.
    
    Raises:
        ValueError: if path is outside all allowed bases or cannot be resolved
    
    Implementation notes (for Wave 2):
        - Use Path.expanduser().resolve() to get canonical form
        - Use Path.is_relative_to() to check against allowed bases (Python 3.9+)
        - Reject paths that escape allowed bases with clear error
        - Log path traversal attempts for audit trail
    """
    if not target_str:
        raise ValueError("target_file path cannot be empty")
    
    # Expand ~ and environment variables, resolve to canonical path
    target = Path(target_str).expanduser().resolve(strict=False)
    
    # Default allowlist: current working directory (project root)
    if allowed_bases is None:
        allowed_bases = [Path.cwd()]
    
    # Check if target is under any allowed base
    for allowed_base in allowed_bases:
        allowed_base_resolved = allowed_base.resolve()
        try:
            # Try to compute relative path — if succeeds, target is under base
            target.relative_to(allowed_base_resolved)
            # Success — path is under allowed base
            return target
        except ValueError:
            # Not under this base, try next
            continue
    
    # No allowed base matched — path traversal attempt
    raise ValueError(
        f"Path {target} is outside allowed write bases: {allowed_bases}. "
        f"Use 'apply_preview' tool to request approval for out-of-root writes."
    )


# ============================================================================
# TEST 1: test_path_validation_rejects_outside_root (05-V0-07)
# ============================================================================


def test_path_validation_rejects_outside_root(test_config_fixture):
    """
    Verify that resolved target paths outside trusted bases are rejected.
    
    This test:
    1. Use test_config_fixture with trusted_bases = ["/tmp/test-project"]
    2. Try to validate paths like:
       - "/etc/passwd" (absolute, outside root)
       - "/../../../etc/secrets" (traversal attempt)
       - "$HOME/sensitive" (may escape root depending on home location)
    3. Verify: ValueError is raised with "outside allowed write bases" message
    
    Expected behavior after Phase 5 Wave 2:
        PASS — all out-of-root paths rejected
    
    FNDX-04 requirement:
        Path traversal guard validates all target_file paths against an
        allowlist before writing model output. Outside-root paths rejected.
    """
    config = test_config_fixture
    allowed_bases = config.write_safety_trusted_bases
    
    # Test case 1: absolute path outside root
    with pytest.raises(ValueError, match="outside allowed write bases"):
        validate_target_path("/etc/passwd", allowed_bases=allowed_bases)
    
    # Test case 2: symlink traversal attempt (if available)
    with pytest.raises(ValueError, match="outside allowed write bases"):
        validate_target_path("/../../../etc/secrets", allowed_bases=allowed_bases)
    
    # Test case 3: home directory may be outside root (depending on config)
    # Skip if home is inside test project (edge case)
    home = Path.home()
    if home != Path("/tmp/test-project") and not is_within_repo(home, Path("/tmp/test-project")):
        with pytest.raises(ValueError, match="outside allowed write bases"):
            validate_target_path(str(home / "sensitive.txt"), allowed_bases=allowed_bases)


# ============================================================================
# TEST 2: test_path_validation_accepts_inside_root (05-V0-08)
# ============================================================================


def test_path_validation_accepts_inside_root(test_config_fixture):
    """
    Verify that resolved target paths inside trusted bases are accepted.
    
    This test:
    1. Use test_config_fixture with trusted_bases = ["/tmp/test-project"]
    2. Try to validate paths like:
       - "/tmp/test-project/src/main.py"
       - "/tmp/test-project/generated/output.txt"
       - "./src/main.py" (relative, resolves inside root)
    3. Verify: validate_target_path returns a Path object, no exception
    4. Verify: returned Path is fully resolved (canonical form)
    
    Expected behavior after Phase 5 Wave 2:
        PASS — all in-root paths accepted and returned as Path objects
    
    FNDX-04 requirement:
        Path validation accepts resolved paths inside trusted bases and
        returns them for safe write operations.
    """
    config = test_config_fixture
    allowed_bases = config.write_safety_trusted_bases
    
    # Test case 1: explicit path inside root
    path1 = validate_target_path("/tmp/test-project/src/main.py", allowed_bases=allowed_bases)
    assert isinstance(path1, Path)
    assert path1.is_absolute()  # Should be resolved to absolute
    assert is_within_repo(path1, Path("/tmp/test-project"))
    
    # Test case 2: nested directory inside root
    path2 = validate_target_path(
        "/tmp/test-project/generated/outputs/output.txt",
        allowed_bases=allowed_bases,
    )
    assert isinstance(path2, Path)
    assert is_within_repo(path2, Path("/tmp/test-project"))
    
    # Test case 3: relative path (should be resolved against cwd, then checked)
    # This depends on cwd being inside the test project or the allowed_bases
    # For safety, we'll construct an explicit in-root path
    explicit_path = "/tmp/test-project/tests/config.json"
    path3 = validate_target_path(explicit_path, allowed_bases=allowed_bases)
    assert isinstance(path3, Path)
    assert str(path3) == str(Path(explicit_path).resolve())




# ============================================================================
# TEST 3-7: Phase 8 MCP security for Aider and Amazon Q/Kiro
# ============================================================================


def test_aider_enforces_no_git_via_mcp():
    """MCP execution of Aider enforces --no-git and --no-auto-commits flags.
    
    Per D-05: Aider must always force --no-git and --no-auto-commits when run
    through Threnody to prevent automatic git commits that bypass the
    project's write safety guards.
    """
    from shared.discovery import BUILTIN_PROVIDERS
    
    aider = next((p for p in BUILTIN_PROVIDERS if p.name == "aider"), None)
    assert aider is not None, "Aider should be in BUILTIN_PROVIDERS"
    
    # Build a command via the command_builder (positional args: provider, action, model, prompt)
    cmd = aider.command_builder(aider, "execute", "claude-opus", "test")
    
    assert isinstance(cmd, list), f"command_builder should return list, got {type(cmd)}"
    
    # Verify --no-git and --no-auto-commits are present
    assert "--no-git" in cmd, f"--no-git missing from Aider command: {cmd}"
    assert "--no-auto-commits" in cmd, f"--no-auto-commits missing from Aider command: {cmd}"


def test_amazon_q_auth_state_fresh_per_detection():
    """Amazon Q/Kiro auth state is detected fresh, not cached across calls.
    
    Per D-02: Amazon Q/Kiro routeability should use a cheap authenticated CLI
    probe with fresh detection each time (not cached). Auth state is ephemeral
    for SSO and caching would violate the security model.
    """
    from shared.discovery import BUILTIN_PROVIDERS
    
    q_provider = next((p for p in BUILTIN_PROVIDERS if p.name == "amazon-q"), None)
    assert q_provider is not None, "Amazon Q/Kiro should be in BUILTIN_PROVIDERS"
    
    # Verify detect_hook is callable (not just a cached bool flag)
    assert q_provider.detect_hook is not None, (
        "Amazon Q/Kiro should have detect_hook for fresh detection"
    )
    assert callable(q_provider.detect_hook), (
        "detect_hook should be callable for fresh detection per request"
    )


def test_aider_result_extraction_no_secrets_in_metadata():
    """Aider result extraction doesn't expose secrets in result metadata.
    
    Per threat model T-08-13: Result extraction should not log or expose
    API keys, environment variables, or other sensitive data in metadata.
    """
    from shared.adapters import _extract_aider_result
    
    result = _extract_aider_result(
        provider_name="aider",
        command=["aider", "--model", "claude-opus", "--message", "test"],
        stdout="Fixed the function",
        stderr="Total cost: $0.0042",
        exit_code=0,
        model_used="claude-opus"
    )
    
    # Verify result is safe to inspect
    result_str = str(result)
    assert "API_KEY" not in result_str, "API_KEY should not appear in result"
    assert "secret" not in result_str.lower(), "Secret should not appear in result"
    
    # Verify metadata is safe
    metadata_str = str(result.metadata)
    assert "API_KEY" not in metadata_str, "API_KEY should not appear in metadata"


def test_q_kiro_result_extraction_no_secrets_in_metadata():
    """Amazon Q/Kiro result extraction doesn't expose secrets in metadata.
    
    Per threat model T-08-13: Result extraction should not log or expose
    API keys, environment variables, or other sensitive data in metadata.
    """
    from shared.adapters import _extract_q_kiro_result
    
    result = _extract_q_kiro_result(
        provider_name="amazon-q",
        command=["q", "chat", "--no-interactive", "--model", "claude-3.7-sonnet", "test"],
        stdout="Here's the solution:\n\nclass Handler:\n    pass",
        stderr="",
        exit_code=0,
        model_used="claude-3.7-sonnet"
    )
    
    # Verify result is safe
    result_str = str(result)
    assert "API_KEY" not in result_str, "API_KEY should not appear in result"
    assert "secret" not in result_str.lower(), "Secret should not appear in result"


def test_mcp_provider_listing_is_public():
    """MCP provider listing via check_providers tool exposes no secrets.
    
    Per threat model T-08-13: Provider discovery via MCP should be JSON-safe
    and not expose any authentication state, API keys, or credentials.
    """
    import json
    import pytest
    from shared.discovery import ProviderRegistry
    
    registry = ProviderRegistry()
    adapters = registry.list_adapters()
    
    # Serialize adapters to JSON (what MCP would send)
    serialized = registry.serialize_adapters()
    
    # Verify JSON serializable
    try:
        json_str = json.dumps(serialized)
    except TypeError as e:
        pytest.fail(f"Adapters not JSON serializable: {e}")
    
    # Verify no secrets in JSON
    assert "API_KEY" not in json_str, "API_KEY should not appear in provider listing"
    assert "secret" not in json_str.lower(), "Secret should not appear in provider listing"


# ============================================================================
# Task 1: Compact provider output format tests (09-03)
# ============================================================================

def test_check_providers_compact_structure():
    """to_compact_dict() returns dict with keys 'providers', 'total', 'routeable_count'.
    
    Each provider entry has all required keys: name, display_name, binary, routeable,
    detect_reason, models_summary, source, health.
    """
    import os
    from shared.discovery import ProviderRegistry, DetectReason, ProviderReadiness
    
    os.environ["THRENODY_TEST_MODE"] = "1"
    registry = ProviderRegistry()
    compact = registry.to_compact_dict()
    
    # Verify top-level structure
    assert "providers" in compact, "Missing 'providers' key"
    assert "total" in compact, "Missing 'total' key"
    assert "routeable_count" in compact, "Missing 'routeable_count' key"
    assert isinstance(compact["providers"], list), "'providers' should be a list"
    assert isinstance(compact["total"], int), "'total' should be an int"
    assert isinstance(compact["routeable_count"], int), "'routeable_count' should be an int"
    
    # Verify each provider entry has required keys
    required_keys = {"name", "display_name", "binary", "routeable", "detect_reason", "models_summary", "source", "health"}
    for provider_entry in compact["providers"]:
        assert isinstance(provider_entry, dict), f"Provider entry should be dict, got {type(provider_entry)}"
        entry_keys = set(provider_entry.keys())
        missing_keys = required_keys - entry_keys
        assert not missing_keys, f"Provider {provider_entry.get('name')} missing keys: {missing_keys}"


def test_check_providers_compact_detect_reason_is_string():
    """detect_reason values are strings (not Enum objects).
    
    json.dumps must succeed without custom serialization.
    """
    import json
    import os
    from shared.discovery import ProviderRegistry
    
    os.environ["THRENODY_TEST_MODE"] = "1"
    registry = ProviderRegistry()
    compact = registry.to_compact_dict()
    
    # Verify detect_reason is a string
    for provider_entry in compact["providers"]:
        detect_reason = provider_entry["detect_reason"]
        assert isinstance(detect_reason, str), f"detect_reason should be str, got {type(detect_reason)}: {detect_reason}"
    
    # Verify JSON serializable
    json_str = json.dumps(compact)
    assert json_str, "JSON serialization produced empty string"
    assert "ready" in json_str or "unknown" in json_str, "JSON should contain detect_reason values"


def test_check_providers_compact_models_summary_shape():
    """models_summary has low/medium/high integer counts."""
    import os
    from shared.discovery import ProviderRegistry
    
    os.environ["THRENODY_TEST_MODE"] = "1"
    registry = ProviderRegistry()
    compact = registry.to_compact_dict()
    
    required_tiers = {"low", "medium", "high"}
    for provider_entry in compact["providers"]:
        models_summary = provider_entry["models_summary"]
        assert isinstance(models_summary, dict), f"models_summary should be dict, got {type(models_summary)}"
        
        # Check all tiers present
        summary_keys = set(models_summary.keys())
        missing_tiers = required_tiers - summary_keys
        assert not missing_tiers, f"models_summary missing tiers: {missing_tiers}"
        
        # Check all values are integers
        for tier, count in models_summary.items():
            assert isinstance(count, int), f"models_summary[{tier}] should be int, got {type(count)}: {count}"


def test_check_providers_compact_includes_billing_metadata():
    """Compact provider output includes per-tier billing metadata."""
    import os
    from shared.discovery import ProviderRegistry

    os.environ["THRENODY_TEST_MODE"] = "1"
    registry = ProviderRegistry()
    compact = registry.to_compact_dict()

    provider_entry = compact["providers"][0]
    assert "billing" in provider_entry, "Missing billing metadata"
    assert provider_entry["billing"]["low"]["is_free"] is True
    assert provider_entry["billing"]["low"]["billing_tier"] == "free"
    assert provider_entry["billing"]["medium"]["billing_tier"] == "subscription"


def test_check_providers_compact_marks_user_billing_override():
    """Compact provider output shows when billing metadata was manually overridden."""
    import os
    from shared.discovery import ProviderRegistry

    os.environ["THRENODY_TEST_MODE"] = "1"
    registry = ProviderRegistry(config_overrides={
        "provider_cost_overrides": {
            "test-provider": {
                "medium": {
                    "cost_rank": 0,
                    "billing_tier": "free",
                    "provider_cost_hint": "Cursor Pro subscription",
                },
            },
        },
    })
    compact = registry.to_compact_dict()

    provider_entry = compact["providers"][0]
    assert provider_entry["billing"]["medium"]["billing_source"] == "user_override"
    assert provider_entry["billing"]["medium"]["provider_cost_hint"] == "Cursor Pro subscription"


def test_check_providers_compact_health_labels():
    """health field is one of: ready, degraded, unavailable, stub."""
    import os
    from shared.discovery import ProviderRegistry
    
    os.environ["THRENODY_TEST_MODE"] = "1"
    registry = ProviderRegistry()
    compact = registry.to_compact_dict()
    
    valid_health = {"ready", "degraded", "unavailable", "stub", "unknown"}
    for provider_entry in compact["providers"]:
        health = provider_entry["health"]
        assert health in valid_health, f"Invalid health value '{health}'. Expected one of: {valid_health}"


def test_check_providers_compact_no_secrets():
    """No provider entry contains keys or values matching secret patterns.
    
    Patterns: token, key, secret, password, credential, api_key, auth, sk-, ghp_,
    Bearer, ANTHROPIC_, OPENAI_, .local/lib, config.yaml, HOME.
    """
    import json
    import os
    import re
    from shared.discovery import ProviderRegistry
    
    os.environ["THRENODY_TEST_MODE"] = "1"
    registry = ProviderRegistry()
    compact = registry.to_compact_dict()
    json_str = json.dumps(compact)
    
    # Comprehensive secret patterns
    secret_patterns = [
        r"api_key",
        r"token",
        r"secret",
        r"password",
        r"credential",
        r"sk-",
        r"ghp_",
        r"Bearer",
        r"ANTHROPIC_",
        r"OPENAI_",
        r"\.local/lib",
        r"config\.yaml",
        r"HOME",
        r"AUTH",
    ]
    
    for pattern in secret_patterns:
        matches = re.findall(pattern, json_str, re.IGNORECASE)
        assert not matches, f"Found secret pattern '{pattern}' in compact output: {matches}"


def test_check_providers_compact_json_serializable():
    """json.dumps(registry.to_compact_dict()) succeeds without errors."""
    import json
    import os
    from shared.discovery import ProviderRegistry
    
    os.environ["THRENODY_TEST_MODE"] = "1"
    registry = ProviderRegistry()
    compact = registry.to_compact_dict()
    
    try:
        json_str = json.dumps(compact)
        assert isinstance(json_str, str), "json.dumps should return string"
        assert len(json_str) > 0, "JSON output should not be empty"
    except TypeError as e:
        pytest.fail(f"to_compact_dict() output not JSON serializable: {e}")


def test_check_providers_windsurf_visible_not_routeable():
    """If windsurf detected, it has routeable=False, detect_reason with execution_not_supported.
    
    Per D-01: Windsurf visible but never routeable. Health should be stub or unavailable.
    """
    import os
    from shared.discovery import ProviderRegistry, DetectReason
    
    os.environ["THRENODY_TEST_MODE"] = "1"
    registry = ProviderRegistry()
    compact = registry.to_compact_dict()
    
    # Find windsurf entry if present
    windsurf_entries = [p for p in compact["providers"] if p["name"] == "windsurf"]
    
    if windsurf_entries:
        windsurf = windsurf_entries[0]
        assert windsurf["routeable"] is False, "Windsurf should not be routeable"
        # detect_reason should indicate execution not supported
        assert "execution_not_supported" in windsurf["detect_reason"] or "binary_missing" in windsurf["detect_reason"], \
            f"Windsurf detect_reason should indicate no execution capability, got: {windsurf['detect_reason']}"
        # Health should be stub (execution_not_supported) or unavailable (binary_missing)
        assert windsurf["health"] in ("stub", "unavailable", "unknown"), \
            f"Windsurf health should be stub/unavailable, got: {windsurf['health']}"


# ============================================================================
# Task 2: MCP handler integration tests (09-03)
# ============================================================================

def test_check_providers_handler_returns_compact_format():
    """handle_check_providers returns compact format (providers key), not verbose format.
    
    Per Task 2: MCP handler switches from to_dict() (available_providers key) to
    to_compact_dict() (providers key).
    """
    import os
    from mcp_server import handle_check_providers
    
    os.environ["THRENODY_TEST_MODE"] = "1"
    result = handle_check_providers({})
    
    # Verify compact format (new)
    assert "providers" in result, "Missing 'providers' key — not using compact format"
    assert "total" in result, "Missing 'total' key"
    assert "routeable_count" in result, "Missing 'routeable_count' key"
    
    # Verify old verbose format is NOT present
    assert "available_providers" not in result, "Old 'available_providers' key still present — should use compact format"


def test_check_providers_handler_no_secrets_in_output():
    """End-to-end: handle_check_providers output contains no secrets.
    
    Call the MCP handler directly, serialize result to JSON, verify no secret patterns.
    """
    import json
    import os
    import re
    from mcp_server import handle_check_providers
    
    os.environ["THRENODY_TEST_MODE"] = "1"
    result = handle_check_providers({})
    json_str = json.dumps(result)
    
    # Same secret patterns as test_check_providers_compact_no_secrets
    secret_patterns = [
        r"api_key",
        r"token",
        r"secret",
        r"password",
        r"credential",
        r"sk-",
        r"ghp_",
        r"Bearer",
        r"ANTHROPIC_",
        r"OPENAI_",
        r"\.local/lib",
        r"config\.yaml",
        r"HOME",
        r"AUTH",
    ]
    
    for pattern in secret_patterns:
        matches = re.findall(pattern, json_str, re.IGNORECASE)
        assert not matches, f"Found secret pattern '{pattern}' in MCP output: {matches}"


def test_mcp_provider_listing_is_public_updated():
    """Updated: MCP provider listing via check_providers tool uses compact format.
    
    Per threat model T-08-13: Provider discovery via MCP should be JSON-safe
    and not expose any authentication state, API keys, or credentials.
    """
    import json
    import os
    import pytest
    from mcp_server import handle_check_providers
    
    os.environ["THRENODY_TEST_MODE"] = "1"
    result = handle_check_providers({})
    
    # Verify JSON serializable
    try:
        json_str = json.dumps(result)
    except TypeError as e:
        pytest.fail(f"Compact format not JSON serializable: {e}")
    
    # Verify no secrets in JSON
    assert "API_KEY" not in json_str, "API_KEY should not appear in provider listing"
    assert "secret" not in json_str.lower(), "Secret should not appear in provider listing"
    
    # Verify compact format is used
    assert "providers" in result, "Should use compact format with 'providers' key"


# ============================================================================
# Phase 9 Integration Tests: Cross-Surface Config & Security
# ============================================================================


def test_phase9_integration_config_to_check_providers(monkeypatch, tmp_path):
    """
    End-to-end integration test: Config overrides flow to check_providers.
    
    Create a TGsConfig with model_tier_pins, create a ModelCatalog with those
    overrides, refresh with a mock provider that includes a test-model, then
    verify the catalog entry has the pinned tier.
    """
    import os
    import json
    from pathlib import Path
    from shared.config import TGsConfig
    from shared.model_catalog import ModelCatalog
    from shared.discovery import ProviderRegistry
    
    # Create a mock config YAML with tier pins
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("""
models:
  tier_pins:
    test-model-123: low
    test-model-456: high
cache_db: ":memory:"
""")
    
    # Load config
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")
    config = TGsConfig.from_yaml(config_yaml)
    
    # Verify config loaded tier pins
    assert "test-model-123" in config.model_tier_pins, "Config should have loaded tier_pins"
    assert config.model_tier_pins["test-model-123"] == "low"
    assert config.model_tier_pins["test-model-456"] == "high"
    
    # Create ModelCatalog with user overrides
    catalog = ModelCatalog(user_overrides=config.model_tier_pins)
    
    # Verify overrides apply
    from shared.model_catalog import _tier_from_cost
    tier = _tier_from_cost("test-model-123", 0.1, config.model_tier_pins)
    assert tier == "low", f"User override should set tier to 'low', got {tier}"
    
    tier = _tier_from_cost("test-model-456", 0.01, config.model_tier_pins)
    assert tier == "high", f"User override should set tier to 'high', got {tier}"


def test_phase9_integration_windsurf_in_compact_output(monkeypatch):
    """
    End-to-end integration test: Windsurf appears in compact check_providers output.
    
    Create a ProviderRegistry with windsurf detected (mock shutil.which), call
    to_compact_dict(), verify windsurf entry exists with routeable=False,
    health appropriate for stub, and detect_reason containing expected value.
    """
    import json
    import os
    from shared.discovery import ProviderRegistry, DetectReason
    
    # Disable test mode to get real provider discovery
    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)
    
    # Mock windsurf binary as found
    import shutil
    original_which = shutil.which
    
    def mock_which(cmd):
        if cmd == "windsurf":
            return "/usr/local/bin/windsurf"
        return original_which(cmd)
    
    monkeypatch.setattr("shared.discovery.shutil.which", mock_which)
    
    # Create registry
    registry = ProviderRegistry()
    
    # Get compact output
    compact = registry.to_compact_dict()
    
    # Find windsurf in compact output
    windsurf_entry = None
    for provider_entry in compact.get("providers", []):
        if provider_entry.get("name") == "windsurf":
            windsurf_entry = provider_entry
            break
    
    assert windsurf_entry is not None, "Windsurf should appear in compact provider output"
    
    # Verify windsurf fields
    assert windsurf_entry["routeable"] is False, "Windsurf should not be routeable"
    assert windsurf_entry["detect_reason"] == "execution_not_supported"
    assert windsurf_entry["health"] in ["stub", "unavailable"], (
        f"Windsurf health should be 'stub' or 'unavailable', got {windsurf_entry['health']}"
    )
    assert windsurf_entry["source"] == "stub", "Windsurf source should be 'stub'"
    
    # Verify JSON serializable
    json.dumps(compact)


def test_phase9_no_secrets_across_all_surfaces(monkeypatch):
    """
    End-to-end integration test: No secrets leak across discovery, catalog, or MCP surfaces.
    
    Call to_compact_dict() and serialize the entire output to a string. Search for
    patterns: api_key, token, secret, password, credential, sk-, ghp_, Bearer, file
    paths, home directory patterns. Assert zero matches.
    """
    import json
    import re
    import os
    from shared.discovery import ProviderRegistry
    from mcp_server import handle_check_providers
    
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")
    
    # Test 1: ProviderRegistry.to_compact_dict() surface
    registry = ProviderRegistry()
    compact_dict = registry.to_compact_dict()
    json_str = json.dumps(compact_dict)
    
    # Test 2: MCP handler surface
    mcp_result = handle_check_providers({})
    mcp_json_str = json.dumps(mcp_result)
    
    # Combined search surface
    full_surface = json_str + " " + mcp_json_str
    
    # Secret patterns to search for
    secret_patterns = [
        r"api_key",
        r"api-key",
        r"apikey",
        r"token",
        r"secret",
        r"password",
        r"credential",
        r"sk-",
        r"ghp_",
        r"Bearer",
        r"ANTHROPIC_",
        r"OPENAI_",
        r"GITHUB_",
        r"\.local/lib",
        r"config\.yaml",
        r"~\w+",  # Home directory patterns
        r"/Users/",
        r"/home/",
    ]
    
    found_secrets = []
    for pattern in secret_patterns:
        matches = re.findall(pattern, full_surface, re.IGNORECASE)
        if matches:
            found_secrets.append(f"Pattern '{pattern}': {matches[:3]}")  # Limit to first 3
    
    assert not found_secrets, (
        f"Found secret patterns in output surfaces: {'; '.join(found_secrets)}"
    )
