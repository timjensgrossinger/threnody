"""
blackbox/providers.py — Blackbox AI provider implementation for Threnody.

Implements command building, auth detection, and output cleaning for the
Blackbox AI CLI (`blackbox`).

Notes:
- Non-interactive invocation: `blackbox --model MODEL PROMPT`
  When no model is specified the CLI uses the globally configured default.
- Blackbox is an aggregated API that exposes 400+ models including its own
  `blackboxai` model (low tier) and third-party models via its bridge.
- Auth is configured via `blackbox configure` and stored in
  `~/.blackboxcli/settings.json`, or via the `BLACKBOX_API_KEY` env var.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from shared.discovery import DetectReason, ProviderReadiness

log = logging.getLogger(__name__)

_BLACKBOX_SETTINGS_PATH = Path.home() / ".blackboxcli" / "settings.json"


def _build_blackbox_command(
    provider: "Any",
    action: str,
    model: str,
    prompt: str,
    effort: str | None = None,
) -> list[str]:
    """Build a Blackbox AI CLI command.

    Passes ``--model MODEL`` when a model name is provided so that Threnody
    can route different cost tiers to different Blackbox-accessible models.
    Falls back to the user-configured default when model is empty.
    """
    if model:
        return ["blackbox", "--model", model, prompt]
    return ["blackbox", prompt]


def _detect_blackbox(provider: "Any") -> ProviderReadiness:
    """Detect whether the Blackbox AI CLI is installed and authenticated."""
    if shutil.which(provider.binary) is None:
        return ProviderReadiness(
            routeable=False,
            reason=DetectReason.BINARY_MISSING,
        )

    api_key = os.getenv("BLACKBOX_API_KEY", "").strip()
    if api_key:
        log.debug("Blackbox AI: BLACKBOX_API_KEY present → routeable")
        return ProviderReadiness(routeable=True, reason=DetectReason.READY)

    try:
        settings_path = Path.home() / ".blackboxcli" / "settings.json"
        if settings_path.exists():
            raw = settings_path.read_text(encoding="utf-8").strip()
            if raw and raw not in {"{}", "[]"}:
                try:
                    data = json.loads(raw)
                    if data:
                        log.debug("Blackbox AI: settings.json present → routeable")
                        return ProviderReadiness(
                            routeable=True, reason=DetectReason.READY
                        )
                except json.JSONDecodeError:
                    log.debug("Blackbox AI: settings.json malformed", exc_info=True)
    except OSError:
        log.debug("Blackbox AI: settings file unreadable", exc_info=True)

    log.debug("Blackbox AI: no auth found → not routeable")
    return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_FAILED)


def _clean_blackbox_output(raw: str) -> str:
    """Strip Blackbox AI session/status preamble from output."""
    lines = raw.splitlines()
    clean: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("Session:", "Model:", "Connecting", "Connected")):
            continue
        clean.append(line)
    return "\n".join(clean).strip()

