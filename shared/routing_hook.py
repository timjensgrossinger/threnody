"""Standalone PreToolUse routing guard bridge (no MCP stdio required)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

log = logging.getLogger(__name__)


def parse_hook_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract validation fields from a Claude PreToolUse hook JSON payload."""
    tool_name = raw.get("tool_name") or raw.get("toolName")
    cwd = raw.get("cwd")
    tool_input = raw.get("tool_input") or raw.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    target_file = (
        tool_input.get("file_path")
        or tool_input.get("filePath")
        or tool_input.get("path")
        or raw.get("target_file")
    )
    return {
        "tool_name": tool_name,
        "cwd": cwd,
        "target_file": target_file,
        "caller": raw.get("caller") or "claude-code",
        "skill": raw.get("skill"),
    }


def validate_routing_guard(
    *,
    caller: str | None = None,
    cwd: object | None = None,
    target_file: object | None = None,
    tool_name: object | None = None,
    skill: str | None = None,
) -> dict[str, object]:
    """Run routing guard validation using the same logic as the MCP tool."""
    import mcp_server

    _config, db, *_ = mcp_server._ensure_init()
    resolved_caller = caller or mcp_server._resolve_caller()
    return mcp_server._validate_routing_guard(
        db,
        caller=resolved_caller,
        cwd=cwd,
        target_file=target_file,
        tool_name=tool_name,
        skill=skill,
    )


def _emit_hook_result(result: dict[str, object]) -> int:
    """Return Claude hook exit code: 0 allow, 2 block."""
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result.get("valid"):
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Threnody routing guard hook bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Validate one PreToolUse event")
    validate.add_argument(
        "--stdin",
        action="store_true",
        help="Read hook JSON payload from stdin",
    )
    validate.add_argument(
        "--json",
        default="",
        help="Inline hook JSON payload (alternative to --stdin)",
    )
    validate.add_argument("--caller", default="")
    validate.add_argument("--cwd", default="")
    validate.add_argument("--target-file", default="")
    validate.add_argument("--tool-name", default="")

    args = parser.parse_args(argv)
    if args.command != "validate":
        return 1

    if args.stdin:
        raw_text = sys.stdin.read()
        if not raw_text.strip():
            result = {"valid": False, "reason": "empty hook payload"}
            return _emit_hook_result(result)
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            result = {"valid": False, "reason": f"invalid hook JSON: {exc}"}
            return _emit_hook_result(result)
        if not isinstance(payload, dict):
            result = {"valid": False, "reason": "hook payload must be a JSON object"}
            return _emit_hook_result(result)
        fields = parse_hook_payload(payload)
    elif args.json.strip():
        try:
            payload = json.loads(args.json)
        except json.JSONDecodeError as exc:
            result = {"valid": False, "reason": f"invalid hook JSON: {exc}"}
            return _emit_hook_result(result)
        if not isinstance(payload, dict):
            result = {"valid": False, "reason": "hook payload must be a JSON object"}
            return _emit_hook_result(result)
        fields = parse_hook_payload(payload)
    else:
        fields = {
            "caller": args.caller or "claude-code",
            "cwd": args.cwd or None,
            "target_file": args.target_file or None,
            "tool_name": args.tool_name or "Edit",
            "skill": None,
        }

    try:
        result = validate_routing_guard(
            caller=str(fields.get("caller") or "claude-code"),
            cwd=fields.get("cwd"),
            target_file=fields.get("target_file"),
            tool_name=fields.get("tool_name"),
            skill=fields.get("skill") if isinstance(fields.get("skill"), str) else None,
        )
    except Exception as exc:
        log.exception("routing hook validation failed")
        result = {
            "valid": False,
            "reason": f"routing hook validation error: {type(exc).__name__}: {exc}",
        }
    return _emit_hook_result(result)


if __name__ == "__main__":
    raise SystemExit(main())
