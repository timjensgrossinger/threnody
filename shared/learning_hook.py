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
import re
import sys
import time
from typing import Any

log = logging.getLogger(__name__)

# apply_patch envelope (Codex): "*** Add File: path" / "*** Update File: path" /
# "*** Delete File: path". The edited path lives inside the command text rather
# than a clean field.
_APPLY_PATCH_FILE = re.compile(
    r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s+(.+?)\s*$", re.MULTILINE
)


def _extract_target_files(raw: dict[str, Any]) -> list[str]:
    """Resolve edited file path(s) across host CLI payload shapes.

    Handles: Claude (`tool_input.file_path`), Cursor (top-level `file_path`),
    Copilot (`toolArgs.*`), and Codex (`tool_input.command` apply_patch text).
    """
    files: list[str] = []

    # Top-level (Cursor afterFileEdit) + explicit hint.
    for key in ("file_path", "filePath", "path", "target_file"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            files.append(val.strip())

    tool_input = raw.get("tool_input") or raw.get("toolInput") or {}
    tool_args = raw.get("toolArgs") or raw.get("tool_args") or {}
    for src in (tool_input, tool_args):
        if not isinstance(src, dict):
            continue
        for key in ("file_path", "filePath", "path", "file", "target_file"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                files.append(val.strip())
        # Codex apply_patch: paths are embedded in the command text.
        cmd = src.get("command")
        if isinstance(cmd, str) and "*** " in cmd:
            files.extend(m.strip() for m in _APPLY_PATCH_FILE.findall(cmd))

    # De-dup, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _extract_success(raw: dict[str, Any]) -> bool:
    """Resolve success across shapes; default True (notification hooks omit it)."""
    tr = raw.get("tool_response") or raw.get("toolResponse")
    if isinstance(tr, dict) and "success" in tr:
        return bool(tr.get("success"))
    # Copilot: toolResult.resultType == "success".
    tres = raw.get("toolResult") or raw.get("tool_result")
    if isinstance(tres, dict) and tres.get("resultType"):
        return str(tres.get("resultType")).lower() == "success"
    if isinstance(tr, bool):
        return tr
    return True


def parse_hook_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract capture fields from a post-edit hook payload (any host CLI shape)."""
    tool_name = raw.get("tool_name") or raw.get("toolName") or raw.get("hook_event_name")
    files = _extract_target_files(raw)
    return {
        "tool_name": tool_name,
        "cwd": raw.get("cwd"),
        "target_file": files[0] if files else None,
        "target_files": files,
        "success": _extract_success(raw),
        "run_id": raw.get("run_id"),
    }


def capture_edit(fields: dict[str, Any]) -> dict[str, Any]:
    """Append one run-log record for a file-edit event. Best-effort, never raises."""
    from . import run_log

    run_id = fields.get("run_id") or run_log.get_active_run()
    if not run_id:
        return {"captured": False, "reason": "no active run"}
    targets = fields.get("target_files") or (
        [fields["target_file"]] if fields.get("target_file") else []
    )
    targets = [str(t) for t in targets if t]
    if not targets:
        return {"captured": False, "reason": "no target file"}

    record = {
        "wave": 0,  # hook events are not wave-attributed; import folds to wave 1
        "spawn_id": "",
        "task_id": "",
        "tier": None,
        "model": None,
        "success": bool(fields.get("success", True)),
        "touched_files": targets,
        "output_excerpt": "",
        "source": "post_tool_use_hook",
        "ts": time.time(),
    }
    run_log.append_agent_record(str(run_id), record)
    return {"captured": True, "run_id": str(run_id), "files": targets}


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

    # Accept file-edit events across host CLIs: Claude (Edit/Write/MultiEdit/
    # NotebookEdit), Codex (apply_patch), Copilot (edit/write/create), Cursor
    # (afterFileEdit event, no tool_name). Fall through on anything that still
    # yields an editable file path; reject the rest (e.g. Bash/Read).
    name = str(
        payload.get("tool_name") or payload.get("toolName")
        or payload.get("hook_event_name") or ""
    ).lower()
    _EDIT_TOKENS = ("edit", "write", "create", "apply_patch", "patch", "afterfileedit", "notebook")
    try:
        fields = parse_hook_payload(payload)
        if not any(tok in name for tok in _EDIT_TOKENS) and not fields.get("target_files"):
            return _emit({"captured": False, "reason": f"ignored tool {name}"})
        result = capture_edit(fields)
    except Exception as exc:  # never break the tool
        log.debug("learning hook capture failed", exc_info=True)
        result = {"captured": False, "reason": f"{type(exc).__name__}: {exc}"}
    return _emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
