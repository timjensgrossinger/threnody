"""
Tests for shared.mcp modular package components.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from shared.mcp import (
    BLOCKING_TOOLS,
    RETRY_LIMIT,
    RETRYABLE_TOOLS,
    handle_approval_queue_list_impl,
    handle_inspect_write_audit_impl,
    handle_memory_delete_impl,
    handle_memory_get_impl,
    handle_memory_list_impl,
    handle_memory_search_impl,
    handle_memory_set_impl,
    handle_routing_exception_add_impl,
    handle_routing_exception_list_impl,
    handle_routing_exception_remove_impl,
    heartbeat_loop,
    memory_error_response,
    normalize_memory_request,
    normalize_progress_token,
    optional_memory_string_arg,
    required_memory_string_arg,
    sanitize_tool_exception_message,
    send_error,
    send_notification,
    send_response,
    tool_failure_payload,
)
from shared.memory import MemoryNotFoundError, MemoryRequestError


def test_protocol_token_normalization() -> None:
    assert normalize_progress_token("token-123") == "token-123"
    assert normalize_progress_token("   ") is None
    assert normalize_progress_token(12345) is None
    assert normalize_progress_token("bad\nchar") is None
    assert normalize_progress_token("a" * 200) is None


def test_sanitize_tool_exception_message() -> None:
    sanitized = sanitize_tool_exception_message(Exception("apiKey=sk-1234567890abcdef"))
    assert "sk-1234567890" not in sanitized
    assert "<redacted>" in sanitized


def test_tool_failure_payload() -> None:
    payload = tool_failure_payload(
        tool_name="route_task",
        attempts=2,
        exc=ValueError("Invalid input"),
        diagnostic_id="diag-999",
    )
    assert payload["code"] == "TOOL_EXECUTION_FAILED"
    assert payload["tool"] == "route_task"
    assert payload["diagnostic_id"] == "diag-999"


def test_tool_registry_constants() -> None:
    assert "plan_task" in BLOCKING_TOOLS
    assert "execute_subtask" in BLOCKING_TOOLS
    assert "route_task" in RETRYABLE_TOOLS
    assert RETRY_LIMIT == 2


def test_memory_handlers_validation() -> None:
    with pytest.raises(MemoryRequestError):
        required_memory_string_arg({}, "missing")
    assert optional_memory_string_arg({}, "missing") is None
    assert optional_memory_string_arg({"key": "val"}, "key") == "val"

    err = memory_error_response(MemoryNotFoundError("missing key"))
    assert err["error"] == "not_found"

    err2 = memory_error_response(MemoryRequestError("bad scope"))
    assert err2["error"] == "invalid_request"


def test_governance_handlers_dispatch(tmp_path) -> None:
    mock_db = MagicMock()
    mock_db.routing_exception_list.return_value = [{"id": 1, "pattern": "*.md"}]
    mock_db.get_write_audit.return_value = [{"path": "/tmp/a", "ts": 123}]

    list_res = handle_routing_exception_list_impl({}, get_db=lambda: mock_db)
    assert list_res["count"] == 1

    audit_res = handle_inspect_write_audit_impl({"limit": 10}, get_db=lambda: mock_db)
    assert audit_res["count"] == 1
    assert audit_res["limit"] == 10


def test_emit_progress(monkeypatch) -> None:
    from shared.mcp.protocol import _request_context, emit_progress

    # Without token set -> returns False
    _request_context.progress_token = None
    assert emit_progress(1, 10, "Test step") is False

    # With token set -> sends notification
    notifications = []
    monkeypatch.setattr("shared.mcp.protocol.send_notification", lambda m, p: notifications.append((m, p)))
    _request_context.progress_token = "tok-123"
    assert emit_progress(2, 10, "Processing subtask") is True
    assert len(notifications) == 1
    assert notifications[0][0] == "notifications/progress"
    assert notifications[0][1]["progressToken"] == "tok-123"
    assert notifications[0][1]["progress"] == 2
    assert notifications[0][1]["total"] == 10
    assert notifications[0][1]["message"] == "Processing subtask"

