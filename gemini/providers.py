#!/usr/bin/env python3
"""Threnody Gemini CLI provider."""
from __future__ import annotations

import logging
import shutil
import subprocess

from shared.discovery import get_registry
from shared.orchestrator import Provider
from shared.planner import Subtask
from shared.model_registry import bootstrap_tier_map

log = logging.getLogger(__name__)

GEMINI_TIER_MAP = bootstrap_tier_map("gemini-cli")


class GeminiProvider(Provider):
    """Execute subtasks via the Gemini CLI."""

    def __init__(self) -> None:
        self._gemini_available: bool | None = None

    def _check_gemini(self) -> bool:
        if self._gemini_available is None:
            self._gemini_available = shutil.which("gemini") is not None
        return self._gemini_available

    def resolve_model(self, tier: str) -> str:
        return GEMINI_TIER_MAP.get(tier, GEMINI_TIER_MAP["medium"])

    def execute(self, subtask: Subtask, model: str, timeout: int = 120) -> str | None:
        if not self._check_gemini():
            log.error("gemini CLI not available")
            return None
        try:
            result = subprocess.run(
                ["gemini", "-p", subtask.description],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            log.warning("Gemini CLI timed out after %ds", timeout)
            return None
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return result.stdout.strip() if result.stdout.strip() else None

    def provider_info(self) -> dict:
        try:
            registry = get_registry()
            return {
                "primary": "gemini-cli",
                "gemini_available": self._check_gemini(),
                "registry": registry.to_dict(),
            }
        except Exception:
            return {
                "primary": "gemini-cli",
                "gemini_available": self._check_gemini(),
                "registry": None,
            }

    def available_tiers(self) -> list[str]:
        return ["low", "medium", "high"] if self._check_gemini() else []
