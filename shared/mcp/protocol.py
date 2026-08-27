"""
JSON-RPC 2.0 protocol and stdio transport helpers for Model Context Protocol (MCP).
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
from typing import Any

log = logging.getLogger(__name__)

# Serialize all JSON-RPC writes to stdout
_stdout_lock = threading.Lock()

# Thread-local storage for request-scoped metadata (such as progressToken)
_request_context = threading.local()

_MAX_PROGRESS_TOKEN_LENGTH = 128
_MAX_TOOL_EXCEPTION_MESSAGE_LENGTH = 500

_TOOL_EXCEPTION_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(api[_-]?key|authorization|bearer|password|secret|token)\b"
            r"\s*[:=]\s*['\"]?[^'\"\s,;]+"
        ),
        r"\1=<redacted>",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
)


def send_response(request_id: int | str | None, result: dict[str, Any]) -> None:
    """Send a JSON-RPC 2.0 success response to stdout."""
    msg = {"jsonrpc": "2.0", "id": request_id, "result": result}
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def send_error(request_id: int | str | None, code: int, message: str) -> None:
    """Send a JSON-RPC 2.0 error response to stdout."""
    msg = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def send_notification(method: str, params: dict[str, Any]) -> None:
    """Send an unprompted JSON-RPC 2.0 notification to stdout."""
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def sanitize_tool_exception_message(exc: BaseException) -> str:
    """Redact secrets and clamp tool exception message length."""
    message = str(exc).strip()
    if not message:
        return ""
    message = " ".join(message.split())
    for pattern, replacement in _TOOL_EXCEPTION_REDACTIONS:
        message = pattern.sub(replacement, message)
    if len(message) > _MAX_TOOL_EXCEPTION_MESSAGE_LENGTH:
        return f"{message[:_MAX_TOOL_EXCEPTION_MESSAGE_LENGTH - 3]}..."
    return message


def normalize_progress_token(value: object) -> str | None:
    """Validate and normalize an incoming client progress token."""
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token or len(token) > _MAX_PROGRESS_TOKEN_LENGTH:
        return None
    if any(ord(char) < 32 for char in token):
        return None
    return token


def heartbeat_loop(
    progress_token: str,
    stop_event: threading.Event,
    interval: int = 15,
) -> None:
    """Send MCP progress notifications at *interval* seconds until stopped.

    Only runs when the client supplied a ``_meta.progressToken`` in the
    original ``tools/call`` request. Silently stops on write errors
    (e.g. broken pipe) to avoid crashing the worker thread.
    """
    tick = 0
    while not stop_event.wait(interval):
        tick += 1
        try:
            send_notification("notifications/progress", {
                "progressToken": progress_token,
                "progress": tick,
                "total": 0,
            })
        except Exception:
            log.debug("heartbeat write failed for token %s — stopping", progress_token)
            break


def emit_progress(
    progress: float,
    total: float | None = None,
    message: str = "",
) -> bool:
    """Stream a standardized MCP progress notification if a progressToken is active."""
    token = getattr(_request_context, "progress_token", None)
    if not token:
        return False
    payload: dict[str, Any] = {
        "progressToken": token,
        "progress": progress,
    }
    if total is not None:
        payload["total"] = total
    if message:
        payload["message"] = message
    try:
        send_notification("notifications/progress", payload)
        return True
    except Exception:
        log.debug("emit_progress notification failed for token %s", token, exc_info=True)
        return False

