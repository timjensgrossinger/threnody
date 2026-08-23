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
    Copilot (`toolArgs.*`), Codex (`tool_input.command` apply_patch text), and
    Antigravity (`tool_input.TargetFile`, `tool_input.AbsolutePath`).
    """
    files: list[str] = []

    # Top-level (Cursor afterFileEdit) + explicit hint.
    for key in (
        "file_path",
        "filePath",
        "path",
        "target_file",
        "TargetFile",
        "targetFile",
        "AbsolutePath",
    ):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            files.append(val.strip())

    tool_input = (
        raw.get("tool_input")
        or raw.get("toolInput")
        or raw.get("input")
        or raw.get("arguments")
        or raw.get("tool_args")
        or raw.get("parameters")
        or {}
    )
    tool_args = raw.get("toolArgs") or raw.get("tool_args") or {}
    for src in (tool_input, tool_args):
        if not isinstance(src, dict):
            continue
        for key in (
            "file_path",
            "filePath",
            "path",
            "file",
            "target_file",
            "TargetFile",
            "targetFile",
            "AbsolutePath",
        ):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                files.append(val.strip())
        # Codex apply_patch: paths are embedded in the command text.
        cmd = src.get("command") or src.get("CommandLine")
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
    tr = (
        raw.get("tool_response")
        or raw.get("toolResponse")
        or raw.get("result")
        or raw.get("output")
        or raw.get("tool_result")
    )
    if isinstance(tr, dict):
        if "success" in tr:
            return bool(tr.get("success"))
        if "error" in tr or tr.get("status") == "ERROR":
            return False
    if isinstance(tr, str) and tr.startswith("Encountered error"):
        return False
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


def _is_within_root(path: str, root: str) -> bool:
    """Local, dependency-light containment check.

    Deliberately not ``context.is_within_repo`` — that module pulls in config/DB
    machinery this hook must stay free of (see module docstring): it runs on
    every Edit/Write and has to add zero round-trips.
    """
    from pathlib import Path

    try:
        resolved = Path(path).expanduser().resolve(strict=False)
        base = Path(root).expanduser().resolve(strict=False)
        return resolved == base or resolved.is_relative_to(base)
    except (OSError, ValueError):
        return False


def _run_dir_for(run_id: object) -> str:
    """Absolute path of this run's own log directory, or ``""``."""
    if not isinstance(run_id, str) or not run_id:
        return ""
    try:
        from . import run_log

        return str(run_log.run_log_dir(run_id))
    except Exception:  # pragma: no cover - never block a PostToolUse hook
        log.debug("learning_hook: run dir resolution failed", exc_info=True)
        return ""


def capture_edit(fields: dict[str, Any]) -> dict[str, Any]:
    """Append one run-log record for a file-edit event. Best-effort, never raises."""
    from . import run_log

    cwd = fields.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else None
    run_id = fields.get("run_id") or run_log.get_active_run(workspace_root=cwd)
    if not run_id:
        return {"captured": False, "reason": "no active run"}
    targets = fields.get("target_files") or (
        [fields["target_file"]] if fields.get("target_file") else []
    )
    targets = [str(t) for t in targets if t]
    if cwd:
        # The run's own registered workspace_root is what actually gates this
        # (host_learning enforces it at import), but a PostToolUse hook has no DB
        # access — this cwd is the same value get_active_run just scoped the
        # pointer lookup by, so it is the best available proxy without one. This
        # is what stops a concurrent session's edits (a different cwd, hence a
        # different active-run pointer file) from ever reaching this run's log
        # even if the two pointer lookups somehow raced onto the same run_id.
        #
        # The run's OWN directory counts as in-scope even though it sits outside
        # the workspace. Review agents are read-only: the single write they ever
        # make is their findings/artifact file under runs/<run_id>/, so filtering
        # on cwd alone discarded every record they produced and left REVIEW:
        # swarms with an empty run log — no rows in review_tier_bias,
        # review_scans, review_findings or model_quality_events, ever. It is not
        # a widening of trust: the path must be inside *this* run's directory,
        # which is keyed by the same pointer the cwd lookup just resolved.
        run_dir = _run_dir_for(run_id)
        targets = [
            t
            for t in targets
            if _is_within_root(t, cwd) or (run_dir and _is_within_root(t, run_dir))
        ]
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


def _hook_response(result: dict[str, Any]) -> dict[str, Any]:
    """Return a neutral PostToolUse response accepted by host hook parsers."""
    del result
    return {
        "continue": True,
        "suppressOutput": True,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
        },
    }


def _emit(result: dict[str, Any], *, hook_response: bool = False) -> int:
    if hook_response:
        result = _hook_response(result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0  # PostToolUse must never block.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Threnody learning capture hook bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="Capture one PostToolUse event")
    capture.add_argument("--stdin", action="store_true", help="Read hook JSON from stdin")
    capture.add_argument("--json", default="", help="Inline hook JSON payload")
    capture.add_argument(
        "--hook-response",
        action="store_true",
        help="Emit a neutral host hook response instead of the raw capture result",
    )

    args = parser.parse_args(argv)
    if args.command != "capture":
        return 0

    # Bound the read: hook payloads embed tool I/O and could be large.
    _MAX_PAYLOAD = 8 * 1024 * 1024  # 8 MB
    raw_text = sys.stdin.read(_MAX_PAYLOAD) if args.stdin else args.json
    if not raw_text or not raw_text.strip():
        return _emit(
            {"captured": False, "reason": "empty payload"},
            hook_response=args.hook_response,
        )
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return _emit(
            {"captured": False, "reason": f"invalid JSON: {exc}"},
            hook_response=args.hook_response,
        )
    if not isinstance(payload, dict):
        return _emit(
            {"captured": False, "reason": "payload must be an object"},
            hook_response=args.hook_response,
        )

    # Accept file-edit events across host CLIs: Claude (Edit/Write/MultiEdit/
    # NotebookEdit), Codex (apply_patch), Copilot (edit/write/create), Cursor
    # (afterFileEdit event, no tool_name). Fall through on anything that still
    # yields an editable file path; reject the rest (e.g. Bash/Read).
    name = str(
        payload.get("tool_name") or payload.get("toolName")
        or payload.get("hook_event_name") or ""
    ).lower()
    _EDIT_TOKENS = (
        "edit",
        "write",
        "create",
        "apply_patch",
        "patch",
        "afterfileedit",
        "notebook",
        "replace_file_content",
        "write_to_file",
        "write_file",
    )
    try:
        fields = parse_hook_payload(payload)
        if not any(tok in name for tok in _EDIT_TOKENS) and not fields.get("target_files"):
            return _emit(
                {"captured": False, "reason": f"ignored tool {name}"},
                hook_response=args.hook_response,
            )
        result = capture_edit(fields)
    except Exception as exc:  # never break the tool
        log.debug("learning hook capture failed", exc_info=True)
        result = {"captured": False, "reason": f"{type(exc).__name__}: {exc}"}
    return _emit(result, hook_response=args.hook_response)


if __name__ == "__main__":
    raise SystemExit(main())
