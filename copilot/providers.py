#!/usr/bin/env python3
"""
Threnody Copilot CLI provider.

Maps tier labels to models available via GitHub Copilot CLI (ghcs/ghce/ghcag).
Detects cross-provider availability (Claude CLI) for extended routing.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

from shared.planner import Subtask
from shared.orchestrator import Provider
from shared.discovery import (
    get_registry,
    ProviderRegistry,
    _build_gh_copilot_command,
    _clean_output,
    _copilot_effort_applied,
)
from shared.model_registry import bootstrap_tier_map
from shared.effort_support import default_routed_effort

log = logging.getLogger(__name__)

# Tier → model mapping for Copilot CLI
COPILOT_TIER_MAP = bootstrap_tier_map("github-copilot")

# Fallback if a tier's primary model is unavailable
COPILOT_FALLBACKS: dict[str, str] = {
    "high": "claude-sonnet-4.6",   # if opus unavailable, use sonnet
    "medium": "gpt-5-mini",        # if sonnet unavailable, use mini
}


def _is_cli_available(binary: str) -> bool:
    """Check if a CLI binary is on PATH and responds."""
    return shutil.which(binary) is not None


class CopilotProvider(Provider):
    """Execute subtasks via GitHub Copilot CLI."""

    def __init__(self) -> None:
        self._gh_available: bool | None = None
        self._claude_available: bool | None = None

    def _check_gh(self) -> bool:
        if self._gh_available is None:
            self._gh_available = _is_cli_available("gh")
        return self._gh_available

    def _check_claude(self) -> bool:
        if self._claude_available is None:
            self._claude_available = _is_cli_available("claude")
        return self._claude_available

    def resolve_model(self, tier: str) -> str:
        """Map tier label to Copilot model name."""
        return COPILOT_TIER_MAP.get(tier, COPILOT_TIER_MAP["medium"])

    effort_provider_id = "github-copilot"

    def effort_applied_for(self, model: str, effort: str | None = None) -> bool:
        """True when ``execute(..., effort=effort)`` puts ``--effort`` on the argv.

        Per model: Copilot takes only the levels models.dev lists for *model*
        (``claude-haiku-4.5`` has a token budget, no effort), and an unknown
        model gets none. Without *effort*, whether the model takes any level.
        """
        if effort is None:
            from shared.model_capabilities import copilot_effort_levels

            return bool(copilot_effort_levels(model))
        return _copilot_effort_applied(model, effort)

    def execute(self, subtask: Subtask, model: str,
                timeout: int = 120, effort: str | None = None) -> str | None:
        """Execute a subtask via gh copilot non-interactive prompt mode."""
        if not self._check_gh():
            log.error("gh CLI not available")
            return None

        try:
            cmd = _build_gh_copilot_command(subtask.description, model, effort)
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            raw = result.stdout.strip()
            if not raw:
                if result.stderr.strip():
                    log.debug("gh copilot stderr: %s", result.stderr.strip()[:200])
                return None
            return _clean_output(raw) or raw
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            log.warning("Agent #%d timed out after %ds", subtask.id, timeout)
            return None

    def execute_via_registry(self, subtask: Subtask, tier: str,
                             timeout: int = 120) -> str | None:
        """Execute a subtask via the cheapest available provider for the tier.
        
        Falls back to the standard execute() method if the registry fails.
        """
        try:
            registry = get_registry()
            result = registry.execute_cheapest(
                prompt=subtask.description,
                tier=tier,
                prefer_free=True,
                timeout=timeout,
                routed_effort=default_routed_effort(tier),
            )
            log.info(
                "Registry routed subtask #%d to %s (model=%s, fallback=%s)",
                subtask.id, result["provider"], result["model"],
                result["fallback_used"],
            )
            return result["result"]
        except RuntimeError:
            log.warning(
                "Registry failed for tier=%s, falling back to direct execution",
                tier,
            )
            model = self.resolve_model(tier)
            return self.execute(subtask, model, timeout)

    def provider_info(self) -> dict:
        """Return provider availability info including cross-provider registry."""
        try:
            registry = get_registry()
            return {
                "primary": "github-copilot",
                "gh_available": self._check_gh(),
                "claude_available": self._check_claude(),
                "registry": registry.to_dict(),
            }
        except Exception:
            return {
                "primary": "github-copilot",
                "gh_available": self._check_gh(),
                "claude_available": self._check_claude(),
                "registry": None,
            }

    def available_tiers(self) -> list[str]:
        """Return tiers available via Copilot CLI."""
        if not self._check_gh():
            return []
        return ["low", "medium", "high"]
