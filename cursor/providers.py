"""
cursor/providers.py — Cursor CLI provider implementation for Threnody.

Implements strict headless binary detection, command building with model support,
and output cleaning using the Phase 6 CLIProvider pluggable hooks pattern.

References:
- D-01: First-class execution adapter (not stub)
- D-02: Richer/workspace-write behavior allowed
- D-03: Trust boundary enforcement (handled separately in shared/context.py)
- D-04: Truthful tier coverage (no faking unsupported tiers)
- D-05: Strict headless detection only (cursor-agent binary, not IDE apps)
"""

import logging
import shutil
import subprocess

from shared.discovery import DetectReason, ProviderReadiness

logger = logging.getLogger(__name__)


def _detect_cursor(provider) -> ProviderReadiness:
    """Detect Cursor availability and verify headless binary per D-05.

    Strict detection: only cursor-agent headless binary counts; app-only
    installs and IDE wrappers are excluded.

    Detection order per D-05:
    1. Check for cursor-agent in PATH via shutil.which()
    2. Verify it's the headless binary via `cursor-agent --version` (5-second timeout)
    3. Return READY if both succeed; otherwise return appropriate failure reason

    Args:
        provider: CLIProvider instance

    Returns:
        ProviderReadiness with routeable=True/False and reason

    References:
        - D-05: Strict detection — only headless cursor-agent binary counts
        - D-01: First-class adapter implies verification before routing
    """
    # Step 1: Check for cursor-agent in PATH
    binary_path = shutil.which("cursor-agent")
    if binary_path is None:
        logger.debug("Cursor: binary 'cursor-agent' not found on PATH")
        return ProviderReadiness(routeable=False, reason=DetectReason.BINARY_MISSING)
    
    # Step 2: Verify it's the headless binary via --version probe
    try:
        result = subprocess.run(
            ["cursor-agent", "--version"],
            capture_output=True,
            timeout=5,
            text=True,
        )
        if result.returncode == 0:
            # Check for version info in output to confirm it's real
            if result.stdout or result.stderr:
                logger.debug("Cursor: headless binary verified via --version → routeable")
                return ProviderReadiness(routeable=True, reason=DetectReason.READY)
            else:
                logger.debug("Cursor: --version returned success but no output")
                return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_UNKNOWN)
    except FileNotFoundError:
        logger.debug("Cursor: binary disappeared between which() and execution")
        return ProviderReadiness(routeable=False, reason=DetectReason.BINARY_MISSING)
    except subprocess.TimeoutExpired:
        logger.debug("Cursor: --version probe timed out (5s)")
        return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_UNKNOWN)
    except Exception as e:
        logger.debug("Cursor: --version probe failed: %s", e)
        return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_FAILED)
    
    # Fallback: if we got here, --version returned non-zero
    logger.debug("Cursor: --version returned non-zero")
    return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_FAILED)


def _build_cursor_command(provider, action: str, model: str, prompt: str, effort: str | None = None) -> list[str]:
    """Build Cursor CLI command with model support and code_only flag support.

    Per D-01 (first-class adapter) and D-02 (richer behavior), Cursor supports
    per-call --model selection and optional --code-only flag for action=execute_code_only.

    Args:
        provider: CLIProvider instance
        action: Execution action type (e.g., "execute_code_only")
        model: Model name (e.g., "claude-opus", "claude-sonnet")
        prompt: User prompt/task to execute
        effort: Optional reasoning effort value to map to provider-native flag

    Returns:
        Command list for subprocess execution

    Notes:
        - Per D-01, D-02: cursor-agent supports --model flag
        - If action == "execute_code_only", --code-only flag suppresses agent behavior
        - If `effort` is provided, map it to Cursor's reasoning-effort flag (if supported)
    """
    # Base command: cursor-agent --model MODEL
    command = ["cursor-agent", "--model", model]

    # Map optional effort to Cursor native flag (if provided)
    if effort is not None:
        # The cursor-agent binary supports passing a reasoning effort; pass through
        command.extend(["--reasoning-effort", str(effort)])

    # Per action, add --code-only flag if present
    if action == "execute_code_only":
        command.append("--code-only")
        logger.debug("Cursor command with --code-only: %s", " ".join(command[:6]))
    else:
        logger.debug("Cursor command: %s", " ".join(command[:4]))

    # Append prompt at end
    command.append(prompt)

    return command


def _clean_cursor_output(raw: str) -> str:
    """Clean Cursor output (plain generated code).

    Cursor outputs generated code directly (not markdown-wrapped), so
    minimal cleaning needed. Just strip whitespace.

    Args:
        raw: Raw output from Cursor CLI

    Returns:
        Cleaned output string
    """
    cleaned = raw.strip()
    if not cleaned:
        logger.debug("Cursor output was empty after strip")
        return ""
    logger.debug("Cursor output cleaned: %d chars", len(cleaned))
    return cleaned
