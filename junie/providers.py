"""
junie/providers.py — JetBrains Junie provider implementation for Threnody.

Implements command building, dual-auth detection (BYOK + JetBrains-managed),
JSON parsing with telemetry extraction, and output cleaning using the Phase 6
CLIProvider pluggable hooks pattern.

References:
- D-06: Junie is single-tier provider with no --model flag (truthful coverage)
- D-07: Support both JetBrains-managed and BYOK auth setups
- D-08: Normalized result + supplemental metadata in shared contract
- D-09: Missing telemetry doesn't block execution correctness
- D-10: Summarized metadata only; no raw provider payloads
"""

import json
import logging
import os
import subprocess
from pathlib import Path

from shared.discovery import DetectReason, ProviderReadiness

logger = logging.getLogger(__name__)


def _build_junie_command(provider, action: str, model: str, prompt: str, effort: str | None = None) -> list[str]:
    """Build Junie CLI command with single-tier, no --model flag per D-06.

    Args:
        provider: CLIProvider instance
        action: Execution action type (ignored for Junie)
        model: Model name (ignored for Junie — configured at auth time)
        prompt: User prompt/task to execute
        effort: Optional reasoning effort value (not supported by Junie here)

    Returns:
        Command list for subprocess execution

    Notes:
        - Per D-06: Junie has no per-call --model flag; model is configured at auth time
        - action and model parameters are intentionally ignored (Junie doesn't support them)
        - effort is accepted for API compatibility but intentionally ignored here
        - This is the truthful command pattern for Junie per D-06
    """
    # Per D-06: Junie doesn't support per-call model selection
    # Command: junie PROMPT --output-format=json
    command = ["junie", prompt, "--output-format=json"]

    if effort is not None:
        # Junie does not support an explicit per-call effort flag at this layer;
        # note it in logs but do not modify the command (higher layers handle unsupported effort).
        logger.debug("Junie: effort parameter provided but unsupported here; ignoring")
    
    logger.debug("Junie command: %s (single-tier, no --model)", " ".join(command[:2]))
    return command


def _detect_junie(provider) -> ProviderReadiness:
    """Detect Junie availability and auth state per D-06, D-07.

    Detection order per D-07 (support both JetBrains-managed and BYOK):
    1. Check JUNIE_API_KEY env var (BYOK setup)
    2. Check ~/.local/share/junie/config.json (BYOK config file)
    3. Check ~/.jetbrains/junie/auth (JetBrains-managed setup)
    4. Probe login status via `junie login status`
    5. Return AUTH_FAILED if none succeed

    Args:
        provider: CLIProvider instance

    Returns:
        ProviderReadiness with routeable=True/False and reason

    References:
        - D-06: Truthful routing — no auth = not routeable
        - D-07: Support both BYOK and JetBrains-managed auth
    """
    # Step 1: Check JUNIE_API_KEY env var (BYOK)
    api_key = os.getenv("JUNIE_API_KEY", "").strip()
    if api_key:
        logger.debug("Junie: JUNIE_API_KEY present → routeable (BYOK)")
        return ProviderReadiness(routeable=True, reason=DetectReason.READY)
    
    # Step 2: Check BYOK config file
    byok_config = Path.home() / ".local" / "share" / "junie" / "config.json"
    if byok_config.exists():
        logger.debug("Junie: BYOK config file exists → routeable")
        return ProviderReadiness(routeable=True, reason=DetectReason.READY)
    
    # Step 3: Check JetBrains-managed auth
    jb_auth = Path.home() / ".jetbrains" / "junie" / "auth"
    if jb_auth.exists():
        logger.debug("Junie: JetBrains-managed auth found → routeable")
        return ProviderReadiness(routeable=True, reason=DetectReason.READY)
    
    # Step 4: Try login status probe (fallback)
    try:
        result = subprocess.run(
            ["junie", "login", "status"],
            capture_output=True,
            timeout=5,
            text=True,
        )
        if result.returncode == 0:
            logger.debug("Junie login status: OK → routeable")
            return ProviderReadiness(routeable=True, reason=DetectReason.READY)
    except FileNotFoundError:
        logger.debug("Junie: binary not found on PATH")
        return ProviderReadiness(routeable=False, reason=DetectReason.BINARY_MISSING)
    except subprocess.TimeoutExpired:
        logger.debug("Junie login probe timed out")
        return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_UNKNOWN)
    except Exception as e:
        logger.debug("Junie login probe failed: %s", e)
    
    # Step 5: Auth failed
    logger.debug("Junie: no auth found → not routeable")
    return ProviderReadiness(routeable=False, reason=DetectReason.AUTH_FAILED)


def _parse_junie_json_output(raw: str) -> tuple[str, dict]:
    """Parse Junie JSON output and extract normalized result + summarized telemetry.

    Per D-08 (normalized result + supplemental metadata) and D-10 (summarized metadata only).

    Args:
        raw: Raw JSON output from Junie CLI

    Returns:
        Tuple of (result_text, metadata_dict):
        - result_text: Plain text code/result to be returned to caller
        - metadata_dict: Summarized telemetry (model, tokens_used, cost, session_id)
               Empty dict on JSON parse error per D-09

    References:
        - D-08: Normalized result in shared contract
        - D-09: Missing telemetry doesn't block execution
        - D-10: Summarized metadata only, no raw payloads
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.debug("Junie JSON parse error: %s (graceful fallback per D-09)", e)
        return raw.strip(), {}
    
    # Extract normalized result
    result = data.get("result", "").strip()
    
    # Extract summarized metadata (per D-10: not raw payload)
    metadata = {}
    
    # Session ID
    session_id = data.get("sessionId", "")
    if session_id:
        metadata["session_id"] = session_id
    
    # Telemetry from llmUsage array
    llm_usage = data.get("llmUsage", [])
    if llm_usage and isinstance(llm_usage, list):
        # Extract from first usage entry (Junie typically has 1)
        first_usage = llm_usage[0] if llm_usage else {}
        
        if "model" in first_usage:
            metadata["model"] = first_usage["model"]
        
        # Combine input and output tokens
        input_tokens = first_usage.get("inputTokens", 0)
        output_tokens = first_usage.get("outputTokens", 0)
        if input_tokens or output_tokens:
            metadata["tokens_used"] = {
                "input": input_tokens,
                "output": output_tokens,
            }
        
        # Cost
        cost = first_usage.get("cost")
        if cost is not None:
            metadata["cost"] = cost
    
    logger.debug(
        "Junie JSON parsed: result=%d chars, metadata keys=%s",
        len(result),
        list(metadata.keys()),
    )
    
    return result, metadata


def _clean_junie_output(raw: str) -> str:
    """Clean Junie output by parsing JSON and extracting normalized result.

    Per D-08, the shared contract receives the plain result; telemetry is
    handled separately by the caller (Wave 2+ MCP adapter wiring).

    Args:
        raw: Raw output from Junie CLI

    Returns:
        Normalized result string (code/text without metadata)

    References:
        - D-08: Normalized result contract
        - D-10: Telemetry handled separately; only result returned here
    """
    result, metadata = _parse_junie_json_output(raw)
    logger.debug("Junie cleaned output: %d chars (metadata in caller)", len(result))
    return result
