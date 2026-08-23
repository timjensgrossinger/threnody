"""Provider-native normalized model discovery adapters."""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .model_registry import (
    DiscoveryResult,
    load_claude_cache,
    load_codex_cache,
    normalize_claude_agent_sdk_models,
    normalize_models,
)


def _parse_json_or_lines(raw: str) -> list[dict[str, Any] | str]:
    payload = raw.strip()
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return [
            line.strip()
            for line in payload.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "Available models"))
        ]
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("models", "data", "availableModels", "available_models"):
            entries = parsed.get(key)
            if isinstance(entries, list):
                return entries
    return []


@dataclass(slots=True)
class CommandModelDiscoveryAdapter:
    provider_id: str
    command: tuple[str, ...]
    source: str = "live_provider_catalog"
    env_factory: Callable[[], dict[str, str]] | None = None
    cwd_factory: Callable[[], str] | None = None

    def discover_live(self) -> DiscoveryResult | None:
        completed = subprocess.run(
            list(self.command),
            capture_output=True,
            text=True,
            timeout=15,
            env=self.env_factory() if self.env_factory else None,
            cwd=self.cwd_factory() if self.cwd_factory else None,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{self.provider_id}: model discovery exited {completed.returncode}"
            )
        models = normalize_models(
            self.provider_id,
            _parse_json_or_lines(completed.stdout),
            source=self.source,
        )
        return DiscoveryResult(
            provider_id=self.provider_id,
            models=models,
            source=self.source,
            successful=bool(models),
        )

    def discover_official_cache(self) -> DiscoveryResult | None:
        return None


@dataclass(slots=True)
class CodexModelDiscoveryAdapter:
    provider_id: str = "codex"
    app_server_catalog: Callable[[], dict[str, Any] | list[Any] | None] | None = None

    def discover_live(self) -> DiscoveryResult | None:
        if self.app_server_catalog is None:
            return None
        payload = self.app_server_catalog()
        entries = payload.get("models") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return None
        return DiscoveryResult(
            provider_id=self.provider_id,
            models=normalize_models(
                self.provider_id,
                entries,
                source="codex_app_server",
            ),
            source="codex_app_server",
        )

    def discover_official_cache(self) -> DiscoveryResult | None:
        return load_codex_cache()


@dataclass(slots=True)
class CallbackModelDiscoveryAdapter:
    provider_id: str
    catalog: Callable[[], dict[str, Any] | list[Any] | None] | None = None
    source: str = "live_provider_catalog"

    def discover_live(self) -> DiscoveryResult | None:
        if self.catalog is None:
            return None
        payload = self.catalog()
        entries = payload.get("models") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return None
        models = normalize_models(self.provider_id, entries, source=self.source)
        return DiscoveryResult(
            provider_id=self.provider_id,
            models=models,
            source=self.source,
            successful=bool(models),
        )

    def discover_official_cache(self) -> DiscoveryResult | None:
        return None


@dataclass(slots=True)
class ClaudeModelDiscoveryAdapter:
    provider_id: str = "claude-code"
    agent_sdk_init: Callable[[], dict[str, Any] | None] | None = None

    def discover_live(self) -> DiscoveryResult | None:
        if self.agent_sdk_init is None:
            return None
        payload = self.agent_sdk_init()
        return normalize_claude_agent_sdk_models(payload) if isinstance(payload, dict) else None

    def discover_official_cache(self) -> DiscoveryResult | None:
        return load_claude_cache()
