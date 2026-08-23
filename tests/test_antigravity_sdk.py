"""Tests for Antigravity Python SDK integration."""
from __future__ import annotations

import pytest

from antigravity.sdk_integration import (
    get_sdk_info,
    sdk_available,
)


def test_sdk_available_returns_bool() -> None:
    """Test that sdk_available returns a boolean."""
    result = sdk_available()
    assert isinstance(result, bool)


def test_get_sdk_info_structure() -> None:
    """Test that get_sdk_info returns expected structure."""
    info = get_sdk_info()
    assert isinstance(info, dict)
    assert "sdk_available" in info
    assert "integration_type" in info
    assert info["integration_type"] == "python_sdk"
    
    if info["sdk_available"]:
        assert "sdk_version" in info


def test_sdk_info_when_unavailable() -> None:
    """Test SDK info when SDK is not installed."""
    info = get_sdk_info()
    # SDK may or may not be available in test environment
    if not info["sdk_available"]:
        assert "sdk_version" not in info


@pytest.mark.skipif(not sdk_available(), reason="google-antigravity SDK not installed")
def test_spawn_threnody_agent_signature() -> None:
    """Test that spawn_threnody_agent has correct signature (when SDK available)."""
    from antigravity.sdk_integration import spawn_threnody_agent
    import inspect
    
    sig = inspect.signature(spawn_threnody_agent)
    params = list(sig.parameters.keys())
    
    assert "tier" in params
    assert "prompt" in params
    assert "workspace" in params
    assert "model_override" in params
    assert "system_instructions" in params


@pytest.mark.skipif(not sdk_available(), reason="google-antigravity SDK not installed")
def test_spawn_dynamic_agent_signature() -> None:
    """Test that spawn_dynamic_agent has correct signature (when SDK available)."""
    from antigravity.sdk_integration import spawn_dynamic_agent
    import inspect
    
    sig = inspect.signature(spawn_dynamic_agent)
    params = list(sig.parameters.keys())
    
    assert "name" in params
    assert "system_prompt" in params
    assert "prompt" in params
    assert "model" in params
    assert "tools" in params
    assert "workspace" in params
