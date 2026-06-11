"""Registry-driven concrete Provider resolver for Orchestrator construction."""
from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shared.orchestrator import Provider
    from shared.discovery import ProviderRegistry

log = logging.getLogger(__name__)

# CLIProvider.name  →  (module_path, class_name)
# Only providers with concrete Provider subclasses are listed here.
_PROVIDER_CLASS_MAP: dict[str, tuple[str, str]] = {
    "github-copilot": ("copilot.providers", "CopilotProvider"),
    "claude-code":    ("claude-code.providers", "ClaudeCodeProvider"),
    "codex":          ("codex.providers", "CodexProvider"),
}


def _instantiate(provider_name: str) -> "Provider | None":
    spec = _PROVIDER_CLASS_MAP.get(provider_name)
    if not spec:
        return None
    module_path, class_name = spec
    try:
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)()
    except Exception:
        log.debug("Could not instantiate provider %s", provider_name, exc_info=True)
        return None


def resolve_default_provider(
    registry: "ProviderRegistry",
    tier: str = "medium",
    caller: str = "claude-code",
) -> "Provider":
    """Return cheapest authenticated Provider instance for `tier`.

    Iterates registry candidates (cheapest first, respecting preferred_routing)
    and returns the first one that can be instantiated as a concrete Provider.
    Falls back to CopilotProvider if nothing else is available.
    """
    try:
        candidates = registry.get_providers_for_tier(tier, caller=caller) or []
    except Exception:
        log.debug("get_providers_for_tier failed", exc_info=True)
        candidates = []

    for cli_provider in candidates:
        name = getattr(cli_provider, "name", "")
        instance = _instantiate(name)
        if instance is not None:
            log.debug("resolve_default_provider: selected %s", name)
            return instance

    from copilot.providers import CopilotProvider
    log.warning("No registry-discovered provider instantiable; falling back to CopilotProvider")
    return CopilotProvider()
