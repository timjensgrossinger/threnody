"""
codex/providers.py — OpenAI Codex provider implementation for Threnody.

Implements command building, auth detection, and output cleaning for Codex
CLI using the Phase 6 CLIProvider pluggable hooks pattern.

References:
- D-01: Truthful routing (auth required for routeable)
- D-08: Normalized result contract
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from shared.discovery import DetectReason, ProviderReadiness
from shared.orchestrator import Provider
from shared.planner import Subtask
from shared.model_registry import bootstrap_tier_map

logger = logging.getLogger(__name__)

CODEX_TIER_MAP = bootstrap_tier_map("codex")


def _build_codex_command(provider, action: str, model: str, prompt: str, effort: str | None = None) -> list[str]:
    """Build a non-interactive Codex CLI command with file-backed output.

    Args:
        provider: CLIProvider instance (for logging/context)
        action: Execution action type (e.g., "execute_code_only")
        model: Model name (for example, "gpt-5.5")
        prompt: User prompt/task to execute
        effort: Optional reasoning effort value to map to provider-native flag

    Returns:
        Command list for subprocess execution

    Notes:
        - Uses -o FILE instead of --json to avoid JSONL parsing.
        - Runs read-only because Threnody consumes stdout and owns file writes.
        - Ignores user config so a nested Codex process cannot reconnect to this MCP.
        - Temp file cleanup is responsibility of caller (provider.execute() handles this)
        - If `effort` is provided, map it to Codex's config override.
    """
    # Create temporary file for output
    fd, output_file = tempfile.mkstemp(suffix=".txt")
    os.close(fd)  # Close the file descriptor; we'll use the path

    # Register the temp file on the provider so that execute() can (a) read it
    # as a stdout fallback and (b) guarantee cleanup in a finally block.
    # execute() clears this attribute after each attempt.
    provider._pending_output_file = output_file

    command = [
        "codex",
        "exec",
        "-m",
        model,
        "-s",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "-o",
        output_file,
    ]

    if effort is not None:
        command.extend([
            "-c",
            f"model_reasoning_effort={json.dumps(str(effort))}",
        ])

    # Append prompt as final argument
    command.append(prompt)

    # Per action parameter, may add additional flags in future (currently no-op)
    # if action == "execute_code_only":
    #     command.append("--sandbox")  # if supported

    logger.debug(
        "Codex command: %s (output to %s)",
        " ".join(command[:-1]),
        output_file,
    )
    return command


def _detect_codex(provider) -> ProviderReadiness:
    """Detect Codex availability and auth state per D-01 truthfulness.

    Detection order:
    1. Check OPENAI_API_KEY env var (fastest, most reliable)
    2. Fall back to `codex login status` (slower, confirms CLI+auth)
    3. Return AUTH_FAILED if neither succeeds

    Args:
        provider: CLIProvider instance

    Returns:
        ProviderReadiness with routeable=True/False and reason

    References:
        - D-01: Auth detection is truthful — unauthenticated = not routeable
    """
    # Step 1: Check OPENAI_API_KEY environment variable
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if api_key:
        logger.debug("Codex: OPENAI_API_KEY present → routeable")
        return ProviderReadiness(routeable=True, reason=DetectReason.READY)
    
    # Step 2: Try login status probe (fallback)
    try:
        result = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True,
            timeout=5,
            text=True,
        )
        if result.returncode == 0:
            logger.debug("Codex login status: OK → routeable")
            return ProviderReadiness(routeable=True, reason=DetectReason.READY)
    except FileNotFoundError:
        logger.debug("Codex: binary not found on PATH")
        return ProviderReadiness(routeable=False, reason=DetectReason.BINARY_MISSING)
    except subprocess.TimeoutExpired:
        logger.debug("Codex login probe timed out")
        return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_UNKNOWN)
    except Exception as e:
        logger.debug("Codex login probe failed: %s", e)
    
    # Step 3: Auth failed
    logger.debug("Codex: no auth found → not routeable")
    return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_FAILED)


def _clean_codex_output(raw: str) -> str:
    """Clean Codex output (plain text from -o FILE).

    Codex with -o FILE outputs plain text (not markdown), so minimal cleaning
    needed. Just strip whitespace.

    Args:
        raw: Raw output from Codex CLI

    Returns:
        Cleaned output string

    References:
        - D-08: Normalized result contract (plain text output)
    """
    cleaned = raw.strip()
    if not cleaned:
        logger.debug("Codex output was empty after strip")
        return ""
    logger.debug("Codex output cleaned: %d chars", len(cleaned))
    return cleaned


class CodexProvider(Provider):
    """Concrete orchestrator provider backed by the Codex CLI."""

    def resolve_model(self, tier: str) -> str:
        return CODEX_TIER_MAP.get(tier, CODEX_TIER_MAP["medium"])

    def execute(
        self,
        subtask: Subtask,
        model: str,
        timeout: int = 120,
    ) -> str | None:
        if shutil.which("codex") is None:
            raise RuntimeError("codex CLI not available")

        command = _build_codex_command(
            self,
            "execute_code_only",
            model,
            subtask.description,
        )
        output_file = getattr(self, "_pending_output_file", None)
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[:500]
                logger.warning(
                    "Codex agent #%d failed: %s",
                    subtask.id,
                    detail,
                )
                raise RuntimeError(
                    f"Codex agent #{subtask.id} exited {result.returncode}: {detail}"
                )
            raw_output = result.stdout
            if output_file:
                try:
                    file_output = Path(output_file).read_text(encoding="utf-8")
                except OSError:
                    file_output = ""
                if file_output.strip():
                    raw_output = file_output
            return _clean_codex_output(raw_output) or None
        except FileNotFoundError:
            raise RuntimeError("codex CLI not available")
        except subprocess.TimeoutExpired as exc:
            logger.warning("Codex agent #%d timed out after %ds", subtask.id, timeout)
            raise RuntimeError(
                f"Codex agent #{subtask.id} timed out after {timeout}s"
            ) from exc
        finally:
            if output_file:
                try:
                    Path(output_file).unlink(missing_ok=True)
                except OSError:
                    logger.debug("Could not remove Codex output file", exc_info=True)
            self._pending_output_file = None

    def available_tiers(self) -> list[str]:
        if shutil.which("codex") is None:
            return []
        return ["low", "medium", "high"]

    def provider_info(self) -> dict[str, object]:
        return {
            "primary": "codex",
            "codex_available": shutil.which("codex") is not None,
        }
