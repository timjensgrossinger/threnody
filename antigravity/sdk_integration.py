"""Optional Python SDK integration for Threnody + Antigravity.

This module provides a thin wrapper around the google-antigravity SDK for
programmatic agent spawning. It's an alternative to plugin hooks — use when
you want programmatic control without the CLI.

Requires: pip install google-antigravity
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

try:
    from google.antigravity import Agent, LocalAgentConfig, CapabilitiesConfig
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


def sdk_available() -> bool:
    """Check if the google-antigravity Python SDK is installed."""
    return SDK_AVAILABLE


async def spawn_threnody_agent(
    tier: str,
    prompt: str,
    workspace: str = "inherit",
    model_override: str | None = None,
    system_instructions: str | None = None,
) -> dict[str, Any]:
    """Spawn a Threnody-tiered agent via the Python SDK.
    
    This is an alternative to the plugin hook system. Use when you need
    programmatic control over agent spawning without going through the CLI.
    
    Args:
        tier: Threnody tier (low, medium, high)
        prompt: Task prompt for the agent
        workspace: Workspace isolation mode (inherit, branch, share)
        model_override: Optional model name override (e.g., "gemini-3.1-pro")
        system_instructions: Optional custom system instructions
    
    Returns:
        Dict with keys: text, model, tier, workspace
    
    Raises:
        ImportError: If google-antigravity SDK is not installed
        ValueError: If tier is invalid
    
    Example:
        >>> result = await spawn_threnody_agent(
        ...     tier="medium",
        ...     prompt="Implement feature X",
        ...     workspace="branch"
        ... )
        >>> print(result["text"])
    """
    if not SDK_AVAILABLE:
        raise ImportError(
            "google-antigravity SDK not installed. "
            "Install with: pip install google-antigravity"
        )
    
    # Map tier to model
    from shared.model_registry import bootstrap_tier_map
    tier_map = bootstrap_tier_map("antigravity")
    model = model_override or tier_map.get(tier, "gemini-3.7-flash")
    
    # Determine capabilities based on workspace
    allow_write = workspace != "share"
    
    # Build config
    config_kwargs: dict[str, Any] = {
        "model": model,
        "capabilities": CapabilitiesConfig(allow_write_tools=allow_write),
    }

    
    if system_instructions:
        config_kwargs["system_instructions"] = system_instructions
    else:
        # Default system instructions based on tier
        tier_instructions = {
            "low": "You are a low-tier agent focused on simple, efficient task completion. Use gemini-3.7-flash.",
            "medium": "You are a medium-tier agent for standard implementation tasks. Use gemini-3.7-flash with high effort.",
            "high": "You are a high-tier agent for complex reasoning and architecture. Use gemini-3.1-pro with thorough analysis.",
        }
        config_kwargs["system_instructions"] = tier_instructions.get(
            tier, "You are a Threnody agent. Follow the task prompt precisely."
        )
    
    config = LocalAgentConfig(**config_kwargs)
    
    # Spawn agent
    try:
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            text = await response.text()
            
            return {
                "text": text,
                "model": model,
                "tier": tier,
                "workspace": workspace,
            }
    except Exception as e:
        log.error("Failed to spawn agent via SDK: %s", e)
        raise


async def spawn_dynamic_agent(
    name: str,
    system_prompt: str,
    prompt: str,
    model: str = "gemini-3.5-flash",
    tools: list[str] | None = None,
    workspace: str = "inherit",
) -> dict[str, Any]:
    """Spawn a dynamically-defined agent via the Python SDK.
    
    Use this when you need a custom agent with a specific system prompt,
    not tied to a predefined tier.
    
    Args:
        name: Agent name (for logging/tracking)
        system_prompt: Custom system instructions
        prompt: Task prompt
        model: Model name (e.g., "gemini-3.5-flash", "gemini-3.1-pro")
        tools: List of allowed tools (e.g., ["read_file", "write_file"])
        workspace: Workspace isolation mode
    
    Returns:
        Dict with keys: text, model, name, workspace
    
    Example:
        >>> result = await spawn_dynamic_agent(
        ...     name="api-migration-agent",
        ...     system_prompt="You specialize in API migrations...",
        ...     prompt="Migrate the auth endpoints",
        ...     model="gemini-3.1-pro"
        ... )
    """
    if not SDK_AVAILABLE:
        raise ImportError(
            "google-antigravity SDK not installed. "
            "Install with: pip install google-antigravity"
        )
    
    allow_write = workspace != "share"
    
    config = LocalAgentConfig(
        model=model,
        system_instructions=system_prompt,
        capabilities=CapabilitiesConfig(allow_write_tools=allow_write),
    )

    
    try:
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            text = await response.text()
            
            return {
                "text": text,
                "model": model,
                "name": name,
                "workspace": workspace,
            }
    except Exception as e:
        log.error("Failed to spawn dynamic agent via SDK: %s", e)
        raise


def get_sdk_info() -> dict[str, Any]:
    """Return information about the SDK integration.
    
    Returns:
        Dict with SDK availability and version info
    """
    info: dict[str, Any] = {
        "sdk_available": SDK_AVAILABLE,
        "integration_type": "python_sdk",
    }
    
    if SDK_AVAILABLE:
        try:
            import google.antigravity
            info["sdk_version"] = getattr(google.antigravity, "__version__", "unknown")
        except Exception:
            info["sdk_version"] = "unknown"
    
    return info
