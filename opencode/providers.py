"""
opencode/providers.py — OpenCode provider implementation for Threnody.

Implements command building, auth detection, and output cleaning for the
OpenCode CLI. The initial rollout is intentionally low-tier-only and targets
the free Nemotron 3 Super Free model exposed by OpenCode.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from shared.discovery import CLIProvider, DetectReason, ProviderReadiness
from shared.model_registry import bootstrap_tier_map

logger = logging.getLogger(__name__)

OPENCODE_LOW_MODEL = bootstrap_tier_map("opencode")["low"]
_OPENCODE_AUTH_PATH = Path.home() / ".local" / "share" / "opencode" / "auth.json"


def _build_opencode_command(
    provider: CLIProvider,
    action: str,
    model: str,
    prompt: str,
    effort: str | None = None,
) -> list[str]:
    """Build an OpenCode CLI command.

    OpenCode accepts provider-qualified model identifiers and runs headlessly via
    ``opencode run``. ``--dangerously-skip-permissions`` avoids interactive
    permission prompts for router-driven executions.
    """
    command = [
        "opencode",
        "run",
        "--model",
        model,
        "--dangerously-skip-permissions",
    ]
    if action == "execute_code_only":
        command.append("--pure")
    if effort is not None:
        command.extend(["--variant", str(effort)])
    command.append(prompt)
    return command


def _detect_opencode(provider: CLIProvider) -> ProviderReadiness:
    """Detect whether OpenCode is installed and authenticated."""
    if shutil.which(provider.binary) is None:
        return ProviderReadiness(
            routeable=False,
            reason=DetectReason.BINARY_MISSING,
        )

    try:
        if _OPENCODE_AUTH_PATH.exists():
            raw = _OPENCODE_AUTH_PATH.read_text(encoding="utf-8").strip()
            if raw and raw not in {"{}", "[]"}:
                return ProviderReadiness(
                    routeable=True,
                    reason=DetectReason.READY,
                )
    except OSError:
        logger.debug("OpenCode auth file unreadable", exc_info=True)

    try:
        result = subprocess.run(
            ["opencode", "providers", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return ProviderReadiness(
            routeable=False,
            reason=DetectReason.BINARY_MISSING,
        )
    except subprocess.TimeoutExpired:
        return ProviderReadiness(
            routeable=False,
            reason=DetectReason.AUTH_UNKNOWN,
        )
    except Exception:
        logger.debug("OpenCode auth probe failed", exc_info=True)
        return ProviderReadiness(
            routeable=False,
            reason=DetectReason.AUTH_UNKNOWN,
        )

    if result.returncode != 0:
        return ProviderReadiness(
            routeable=False,
            reason=DetectReason.AUTH_FAILED,
        )

    output = f"{result.stdout}\n{result.stderr}".lower()
    if "0 credential" in output:
        return ProviderReadiness(
            routeable=False,
            reason=DetectReason.AUTH_FAILED,
        )

    return ProviderReadiness(
        routeable=True,
        reason=DetectReason.READY,
    )


def _clean_opencode_output(raw: str) -> str:
    """Strip the OpenCode status preamble from stdout."""
    lines = raw.splitlines()
    if lines and lines[0].lstrip().startswith("> "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def build_opencode_provider() -> CLIProvider:
    """Create a low-tier-only CLIProvider for OpenCode."""
    return CLIProvider(
        name="opencode",
        binary="opencode",
        display_name="OpenCode",
        tier_models={
            "low": OPENCODE_LOW_MODEL,
        },
        cost_rank={
            "low": 0,
        },
        billing_model="subscription",
        command_builder=_build_opencode_command,
        detect_hook=_detect_opencode,
        output_cleaner=_clean_opencode_output,
    )
