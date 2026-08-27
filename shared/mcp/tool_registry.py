"""
MCP Tool declarations, definitions, and execution metadata.
"""
from __future__ import annotations

from typing import Any

from .protocol import sanitize_tool_exception_message

# Tools that call subprocesses and can block for 30–120 s.
BLOCKING_TOOLS = frozenset({
    "execute_subtask",
    "plan_task",
    "decompose_task",
    "fleet_plan",
    "start_task",
})

# Tools that are safe to retry on transient failure (fast heuristic, no side-effects).
RETRYABLE_TOOLS = frozenset({
    "route_task",
    "plan_task",
    "decompose_task",
    "execute_subtask",
})
RETRY_LIMIT = 2  # up to 2 retries = 3 total attempts


def tool_failure_payload(
    *,
    tool_name: object,
    attempts: int,
    exc: BaseException,
    diagnostic_id: str,
) -> dict[str, object]:
    """Format a standard JSON payload for failed tool executions."""
    normalized_tool = str(tool_name or "")
    message = sanitize_tool_exception_message(exc)
    payload: dict[str, object] = {
        "error": f"Tool '{normalized_tool}' failed after {attempts} attempt(s).",
        "code": "TOOL_EXECUTION_FAILED",
        "tool": normalized_tool,
        "attempts": attempts,
        "exception_type": type(exc).__name__,
        "diagnostic_id": diagnostic_id,
        "log_hint": f"Search the Threnody server log for diagnostic_id={diagnostic_id}.",
    }
    if message:
        payload["message"] = message
    return payload
