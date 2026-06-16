"""Standalone PostToolUse learning-capture bridge (no MCP stdio required).

Mirrors ``routing_hook.py`` but for the PostToolUse event: it appends one
run-log line per Edit/Write so host-native wave learning is captured with zero
model tokens and zero round-trips. It deliberately depends only on
``shared.run_log`` (which pulls in ``shared.config`` for BASE_DIR) — it must NOT
import ``mcp_server`` or touch the DB, so it stays fast enough to run on every
file edit.

A PostToolUse hook must never block the tool, so this always exits 0.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

log = logging.getLogger(__name__)


def parse_hook_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract capture fields from a Claude PostToolUse hook JSON payload."""
    tool_name = raw.get("tool_name") or raw.get("toolName")
    cwd = raw.get("cwd")
    tool_input = raw.get("tool_input") or raw.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    tool_response = raw.get("tool_response") or raw.get("toolResponse") or {}
    if not isinstance(tool_response, dict):
        tool_response = {}

    target_file = (
        tool_input.get("file_path")
        or tool_input.get("filePath")
        or tool_input.get("path")
        or raw.get("target_file")
    )
    # PostToolUse exposes success on the response for most tools.
    success = tool_response.get("success", True)
    return {
        "tool_name": tool_name,
        "cwd": cwd,
        "target_file": target_file,
        "success": bool(success),
        "run_id": raw.get("run_id"),
    }


def capture_edit(fields: dict[str, Any]) -> dict[str, Any]:
    """Append one run-log record for an Edit/Write event. Best-effort, never raises."""
    from . import run_log

    run_id = fields.get("run_id") or run_log.get_active_run()
    if not run_id:
        return {"captured": False, "reason": "no active run"}
    target = fields.get("target_file")
    if not target:
        return {"captured": False, "reason": "no target file"}

    record = {
        "wave": 0,  # hook events are not wave-attributed; import folds to wave 1
        "spawn_id": "",
        "task_id": "",
        "tier": None,
        "model": None,
        "success": bool(fields.get("success", True)),
        "touched_files": [str(target)],
        "output_excerpt": "",
        "source": "post_tool_use_hook",
        "ts": time.time(),
    }
    run_log.append_agent_record(str(run_id), record)
    return {"captured": True, "run_id": str(run_id), "file": str(target)}


def _emit(result: dict[str, Any]) -> int:
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0  # PostToolUse must never block.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Threnody learning capture hook bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="Capture one PostToolUse event")
    capture.add_argument("--stdin", action="store_true", help="Read hook JSON from stdin")
    capture.add_argument("--json", default="", help="Inline hook JSON payload")

    args = parser.parse_args(argv)
    if args.command != "capture":
        return 0

    # Bound the read: hook payloads embed tool I/O and could be large.
    _MAX_PAYLOAD = 8 * 1024 * 1024  # 8 MB
    raw_text = sys.stdin.read(_MAX_PAYLOAD) if args.stdin else args.json
    if not raw_text or not raw_text.strip():
        return _emit({"captured": False, "reason": "empty payload"})
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return _emit({"captured": False, "reason": f"invalid JSON: {exc}"})
    if not isinstance(payload, dict):
        return _emit({"captured": False, "reason": "payload must be an object"})

    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    if tool_name not in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
        return _emit({"captured": False, "reason": f"ignored tool {tool_name}"})

    try:
        fields = parse_hook_payload(payload)
        result = capture_edit(fields)
    except Exception as exc:  # never break the tool
        log.debug("learning hook capture failed", exc_info=True)
        result = {"captured": False, "reason": f"{type(exc).__name__}: {exc}"}
    return _emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
