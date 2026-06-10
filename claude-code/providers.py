#!/usr/bin/env python3
"""
Threnody Claude Code provider.

Maps tier labels to models available via Claude Code CLI.
Detects cross-provider availability (Copilot CLI) for free-tier fallback.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

from shared.planner import Subtask
from shared.orchestrator import Provider
from shared.discovery import get_registry, ProviderRegistry
from shared.model_registry import bootstrap_tier_map

log = logging.getLogger(__name__)

# Tier → model mapping for Claude Code
CLAUDE_TIER_MAP = bootstrap_tier_map("claude-code")

# If Copilot CLI is available, prefer gpt-5-mini for low tier (free)
CLAUDE_TIER_MAP_WITH_COPILOT = dict(CLAUDE_TIER_MAP)

CLAUDE_FALLBACKS: dict[str, str] = {
    "high": "sonnet",
    "medium": "haiku",
}

DAILY_TOKEN_BUDGET: int = 1_000_000
BUDGET_DEGRADATION_THRESHOLD: float = 0.70


def _is_cli_available(binary: str) -> bool:
    return shutil.which(binary) is not None


class ClaudeCodeProvider(Provider):
    """Execute subtasks via Claude Code CLI."""

    def __init__(self) -> None:
        self._claude_available: bool | None = None
        self._copilot_available: bool | None = None
        self._tier_map: dict[str, str] | None = None
        self._estimated_tokens: int = 0
        self._daily_budget: int = DAILY_TOKEN_BUDGET
        self._degradation_threshold: float = BUDGET_DEGRADATION_THRESHOLD

    def _check_claude(self) -> bool:
        if self._claude_available is None:
            self._claude_available = _is_cli_available("claude")
        return self._claude_available

    def _check_copilot(self) -> bool:
        if self._copilot_available is None:
            self._copilot_available = _is_cli_available("gh")
        return self._copilot_available

    def _get_tier_map(self) -> dict[str, str]:
        if self._tier_map is None:
            if self._check_copilot():
                self._tier_map = dict(CLAUDE_TIER_MAP_WITH_COPILOT)
                log.info("Copilot CLI detected — using gpt-5-mini for low tier (free)")
            else:
                self._tier_map = dict(CLAUDE_TIER_MAP)
        return self._tier_map

    def resolve_model(self, tier: str) -> str:
        """Map tier label to model name, applying budget degradation when needed."""
        tier_map = self._get_tier_map()
        model = tier_map.get(tier, tier_map["medium"])

        modifier = self.get_complexity_cutoff_modifier()
        if modifier < 1.0:
            original_tier = tier
            if modifier < 0.75 and tier == "medium":
                tier = "low"
            elif modifier < 0.85 and tier == "high":
                tier = "medium"
            if tier != original_tier:
                downgraded_model = tier_map.get(tier, model)
                log.info(
                    "Budget degradation (modifier=%.2f): downgrading tier %r → %r "
                    "(%s → %s)",
                    modifier, original_tier, tier, model, downgraded_model,
                )
                model = downgraded_model

        return model

    def track_tokens(self, tokens: int) -> None:
        """Accumulate token usage toward the daily budget."""
        self._estimated_tokens += tokens

    def budget_usage(self) -> float:
        """Return the fraction of the daily token budget consumed (0.0–1.0+)."""
        if self._daily_budget == 0:
            return 0.0
        return self._estimated_tokens / self._daily_budget

    def get_complexity_cutoff_modifier(self) -> float:
        """Return a 0.0–1.0 multiplier reflecting remaining budget headroom.

        Zones:
          usage < 0.70          → 1.0  (full capacity)
          usage 0.70–0.85       → linear 1.0 → 0.85
          usage 0.85–0.95       → linear 0.85 → 0.70
          usage >= 0.95         → 0.70 (floor)
        """
        usage = self.budget_usage()
        if usage < 0.70:
            return 1.0
        if usage < 0.85:
            # interpolate 1.0 → 0.85 over [0.70, 0.85)
            t = (usage - 0.70) / (0.85 - 0.70)
            return 1.0 - t * (1.0 - 0.85)
        if usage < 0.95:
            # interpolate 0.85 → 0.70 over [0.85, 0.95)
            t = (usage - 0.85) / (0.95 - 0.85)
            return 0.85 - t * (0.85 - 0.70)
        return 0.70

    def reset_budget(self) -> None:
        """Reset the token counter (call at the start of each billing day)."""
        self._estimated_tokens = 0

    def execute(self, subtask: Subtask, model: str,
                timeout: int = 120) -> str | None:
        """Execute a subtask via the appropriate CLI."""
        # If model is gpt-5-mini and Copilot is available, use gh copilot
        if model == "gpt-5-mini" and self._check_copilot():
            return self._execute_via_copilot(subtask, model, timeout)
        # Otherwise use claude CLI
        return self._execute_via_claude(subtask, model, timeout)

    def _execute_via_claude(self, subtask: Subtask, model: str,
                            timeout: int = 120) -> str | None:
        if not self._check_claude():
            log.error("claude CLI not available")
            return None
        try:
            cmd = ["claude", "-p", subtask.description, "--model", model]
            cwd = getattr(subtask, "workspace_root", None) or None
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return None
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            log.warning("Agent #%d timed out (claude)", subtask.id)
            return None

    def _execute_via_copilot(self, subtask: Subtask, model: str,
                             timeout: int = 120) -> str | None:
        """Cross-route to Copilot CLI for free-tier models."""
        try:
            cmd = ["gh", "copilot", "agent", subtask.description]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def execute_via_registry(self, subtask: Subtask, tier: str,
                             timeout: int = 120) -> str | None:
        """Execute via cheapest available provider, with budget-aware tier adjustment.
        
        Applies budget degradation before routing to the registry.
        Falls back to direct execution if registry fails.
        """
        effective_tier = tier
        modifier = self.get_complexity_cutoff_modifier()
        if modifier < 1.0:
            if modifier < 0.75 and tier == "medium":
                effective_tier = "low"
            elif modifier < 0.85 and tier == "high":
                effective_tier = "medium"
            if effective_tier != tier:
                log.info(
                    "Budget degradation (modifier=%.2f): tier %s → %s",
                    modifier, tier, effective_tier,
                )

        try:
            registry = get_registry()
            result = registry.execute_cheapest(
                prompt=subtask.description,
                tier=effective_tier,
                prefer_free=True,
                timeout=timeout,
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
                effective_tier,
            )
            model = self.resolve_model(effective_tier)
            return self.execute(subtask, model, timeout)

    def provider_info(self) -> dict:
        """Return provider availability info including cross-provider registry."""
        try:
            registry = get_registry()
            return {
                "primary": "claude-code",
                "claude_available": self._check_claude(),
                "copilot_available": self._check_copilot(),
                "budget_usage": self.budget_usage(),
                "budget_modifier": self.get_complexity_cutoff_modifier(),
                "registry": registry.to_dict(),
            }
        except Exception:
            return {
                "primary": "claude-code",
                "claude_available": self._check_claude(),
                "copilot_available": self._check_copilot(),
                "budget_usage": self.budget_usage(),
                "budget_modifier": self.get_complexity_cutoff_modifier(),
                "registry": None,
            }

    def available_tiers(self) -> list[str]:
        """Return tiers available via Claude Code CLI."""
        tiers = []
        if self._check_claude() or self._check_copilot():
            tiers.append("low")
        if self._check_claude():
            tiers.extend(["medium", "high"])
        return tiers
