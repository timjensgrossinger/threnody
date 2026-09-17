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


def _resolve_record_only(caller: str | None) -> bool:
    """True when this shell's hook should observe without blocking.

    Fails closed toward *not* blocking: if the policy cannot be resolved, a hook
    that wrongly denies an edit is a worse failure than one that wrongly allows
    it, because the routing guard is advisory by default.
    """
    try:
        from .config import CONFIG_YAML, TGsConfig

        config = TGsConfig.from_yaml(CONFIG_YAML)
        shell_id = caller or "claude-code"
        return config.routing_policy.effective_profile(shell_id).direct_edit_hook_mode != "enforce"
    except Exception:
        log.debug("hook mode resolution failed; defaulting to record-only", exc_info=True)
        return True


def _emit_hook_result(result: dict[str, object], *, record_only: bool = False) -> int:
    """Return Claude hook exit code: 0 allow, 2 block.

    ``record_only`` is the advisory mode: the same validation runs and the
    decision is still recorded downstream (inside ``_validate_routing_guard``,
    which writes to the DB — never from this stdout), but the hook must not
    block the edit. It exists because advisory mode previously installed no
    hook at all, leaving every direct edit unobserved — including a host
    editing a file an active handoff had planned to spawn a subagent for.

    **The ``hookSpecificOutput`` block is dropped in record mode.** Exit 0 is
    not sufficient to allow the call: Claude Code honours
    ``hookSpecificOutput.permissionDecision`` whenever it is present, so
    emitting ``"deny"`` beside exit 0 hard-blocked every ``Edit``/``Write`` in
    the *default* configuration — including edits to the hook's own source, so
    it could not be repaired in-session. Nothing is lost by dropping it: the
    denial reason is still carried on the ``reason``/``valid`` fields for a
    reader, and persistence happens in the validator, not here.
    """
    payload = dict(result)
    if record_only:
        payload["enforced"] = False
        # Keep the reason readable, but never let it carry a permission verdict.
        hook_output = payload.pop("hookSpecificOutput", None)
        if isinstance(hook_output, dict):
            reason = hook_output.get("permissionDecisionReason")
            if reason and not payload.get("reason"):
                payload["reason"] = reason
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if record_only or result.get("valid"):
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
    validate.add_argument(
        "--record-only",
        action="store_true",
        help="Record the decision but never block (advisory mode)",
    )
    validate.add_argument("--caller", default="")
    validate.add_argument("--cwd", default="")
    validate.add_argument("--target-file", default="")
    validate.add_argument("--tool-name", default="")

    args = parser.parse_args(argv)
    if args.command != "validate":
        return 1

    # Resolve enforce-vs-record from config unless the caller forced record-only.
    # Doing it here rather than baking a flag into the installed hook command means
    # an operator who flips routing_policy does not have to re-run install.sh for
    # the change to take effect.
    if not args.record_only:
        args.record_only = _resolve_record_only(args.caller or None)

    if args.stdin:
        raw_text = sys.stdin.read()
        if not raw_text.strip():
            result = {"valid": False, "reason": "empty hook payload"}
            return _emit_hook_result(result, record_only=args.record_only)
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            result = {"valid": False, "reason": f"invalid hook JSON: {exc}"}
            return _emit_hook_result(result, record_only=args.record_only)
        if not isinstance(payload, dict):
            result = {"valid": False, "reason": "hook payload must be a JSON object"}
            return _emit_hook_result(result, record_only=args.record_only)
        fields = parse_hook_payload(payload)
    elif args.json.strip():
        try:
            payload = json.loads(args.json)
        except json.JSONDecodeError as exc:
            result = {"valid": False, "reason": f"invalid hook JSON: {exc}"}
            return _emit_hook_result(result, record_only=args.record_only)
        if not isinstance(payload, dict):
            result = {"valid": False, "reason": "hook payload must be a JSON object"}
            return _emit_hook_result(result, record_only=args.record_only)
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
    return _emit_hook_result(result, record_only=args.record_only)


if __name__ == "__main__":
    raise SystemExit(main())
