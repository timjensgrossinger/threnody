"""
Antigravity CLI provider implementation for Threnody.

Implements strict headless binary detection, command building with model/effort
support, and JSON output parsing using the CLIProvider pluggable hooks pattern.

Host-native only — no subprocess delegation. The plugin extends the official
agy CLI through its own extension points (plugins, skills, hooks, MCP, agents).
"""

import json
import logging
import shutil
import subprocess

from shared.discovery import DetectReason, ProviderReadiness

logger = logging.getLogger(__name__)


def _detect_agy(provider) -> ProviderReadiness:
    """Detect Antigravity CLI availability and verify headless binary.

    Detection order:
    1. Check for agy in PATH via shutil.which()
    2. Verify it's the headless binary via `agy --version` (5-second timeout)
    3. Return READY if both succeed; otherwise return appropriate failure reason
    """
    binary_path = shutil.which("agy")
    if binary_path is None:
        logger.debug("Antigravity: binary 'agy' not found on PATH")
        return ProviderReadiness(routeable=False, reason=DetectReason.BINARY_MISSING)

    try:
        result = subprocess.run(
            ["agy", "--version"],
            capture_output=True,
            timeout=5,
            text=True,
        )
        if result.returncode == 0:
            if result.stdout or result.stderr:
                logger.debug("Antigravity: headless binary verified via --version")
                return ProviderReadiness(routeable=True, reason=DetectReason.READY)
            else:
                logger.debug("Antigravity: --version returned success but no output")
                return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_UNKNOWN)
    except FileNotFoundError:
        logger.debug("Antigravity: binary disappeared between which() and execution")
        return ProviderReadiness(routeable=False, reason=DetectReason.BINARY_MISSING)
    except subprocess.TimeoutExpired:
        logger.debug("Antigravity: --version probe timed out (5s)")
        return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_UNKNOWN)
    except Exception as e:
        logger.debug("Antigravity: --version probe failed: %s", e)
        return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_FAILED)

    logger.debug("Antigravity: --version returned non-zero")
    return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_FAILED)


def _build_agy_command(provider, action: str, model: str, prompt: str, effort: str | None = None) -> list[str]:
    """Build Antigravity CLI command with model and effort support.

    Antigravity CLI supports:
    - --model: model slug (e.g., gemini-3.5-flash, gemini-3.1-pro)
    - --effort: reasoning effort (low, medium, high)
    - --output-format json: structured output with usage stats
    - --sandbox: isolation mode
    - --dangerously-skip-permissions: auto-approve all tool calls
    """
    command = ["agy", "-p", prompt]

    if model:
        command.extend(["--model", model])

    if effort is not None:
        command.extend(["--effort", str(effort)])

    command.extend([
        "--output-format", "json",
        "--sandbox",
    ])

    logger.debug("Antigravity command: %s", " ".join(command[:6]))
    return command


def _clean_agy_output(raw: str) -> str:
    """Clean Antigravity CLI output.

    Antigravity CLI returns JSON envelopes with --output-format json.
    Extract the response field from the JSON, or return raw text if parsing fails.
    """
    cleaned = raw.strip()
    if not cleaned:
        logger.debug("Antigravity output was empty after strip")
        return ""

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            response = data.get("response", "")
            if isinstance(response, str) and response.strip():
                logger.debug("Antigravity: extracted response from JSON envelope")
                return response.strip()
            status = data.get("status", "")
            if status == "ERROR":
                error = data.get("error", "unknown error")
                logger.debug("Antigravity: ERROR status: %s", error)
                return ""
    except (json.JSONDecodeError, TypeError):
        pass

    logger.debug("Antigravity output cleaned: %d chars", len(cleaned))
    return cleaned


def _detect_agy_safe(provider) -> ProviderReadiness:
    """Safe wrapper for _detect_agy that catches all exceptions."""
    try:
        return _detect_agy(provider)
    except Exception as e:
        logger.debug("Antigravity: _detect_agy failed: %s", e)
        return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_FAILED)


def _build_agy_command_safe(provider, action: str, model: str, prompt: str, effort: str | None = None) -> list[str]:
    """Safe wrapper for _build_agy_command that catches all exceptions."""
    try:
        return _build_agy_command(provider, action, model, prompt, effort)
    except Exception as e:
        logger.debug("Antigravity: _build_agy_command failed: %s", e)
        return ["agy", "-p", prompt]


def _clean_agy_output_safe(raw: str) -> str:
    """Safe wrapper for _clean_agy_output that catches all exceptions."""
    try:
        return _clean_agy_output(raw)
    except Exception as e:
        logger.debug("Antigravity: _clean_agy_output failed: %s", e)
        return raw.strip()


def _parse_agy_models(provider, output: str) -> dict[str, list[str]]:
    """Parse `agy models` output into tiered model lists.

    Expected format per line:
    Fetching available models...
    gemini-3.7-flash-high\tGemini 3.7 Flash (High)
    gemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)
    gemini-3.7-flash-low\tGemini 3.7 Flash (Low)
    ...
    """
    if not output or not output.strip():
        logger.debug("Antigravity model discovery returned empty output")
        return {}

    low_models: list[str] = []
    med_models: list[str] = []
    high_models: list[str] = []

    for line in output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("fetching", "#", "available models")):
            continue

        parts = line.split("\t") if "\t" in line else line.split(None, 1)
        model_id = parts[0].strip()
        if not model_id:
            continue

        model_lower = model_id.lower()
        if "pro" in model_lower or "opus" in model_lower:
            high_models.append(model_id)
        elif "-low" in model_lower or "flash-lite" in model_lower:
            low_models.append(model_id)
        elif "flash" in model_lower or "sonnet" in model_lower or "120b" in model_lower:
            med_models.append(model_id)
        else:
            med_models.append(model_id)

    # Fallback to defaults if a tier is empty
    if not low_models and med_models:
        low_models = [m for m in med_models if "flash" in m.lower()][:1] or med_models[:1]

    return {
        "low": low_models,
        "medium": med_models,
        "high": high_models,
    }


def _parse_agy_models_safe(provider, output: str) -> dict[str, list[str]]:
    """Safe wrapper for _parse_agy_models that catches all exceptions."""
    try:
        return _parse_agy_models(provider, output)
    except Exception as e:
        logger.debug("Antigravity: _parse_agy_models failed: %s", e)
        return {}

