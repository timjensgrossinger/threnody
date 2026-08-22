"""
MCP tool handlers for governance, approval queues, routing exceptions, and telemetry inspection.
"""
from __future__ import annotations

import os
from typing import Any, Callable

_VALID_EXCEPTION_TYPES_MCP = frozenset({
    "skill", "filetype", "project", "command", "caller", "path",
})


def handle_approval_queue_list_impl(
    args: dict[str, Any],
    *,
    normalize_project_id: Callable[[str], str],
    approval_queue_list: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    try:
        limit_raw = args.get("limit", 25)
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = 25
    try:
        project_id = normalize_project_id(args.get("project_id", ""))
        items = approval_queue_list(project_id, limit=limit)
    except (TypeError, ValueError) as exc:
        return {"error": "InvalidProjectPath", "details": str(exc)}
    return {
        "project_id": project_id,
        "items": items,
        "count": len(items),
    }


def handle_routing_exception_add_impl(
    args: dict[str, Any],
    *,
    get_db: Callable[[], Any],
    default_filetypes: frozenset[str],
    default_paths: frozenset[str],
) -> dict[str, Any]:
    db = get_db()
    exc_type = (args.get("exception_type") or "").strip().lower()
    pattern = (args.get("pattern") or "").strip()
    note = args.get("note")
    if exc_type not in _VALID_EXCEPTION_TYPES_MCP:
        return {
            "error": "InvalidExceptionType",
            "details": (
                f"exception_type must be one of: {', '.join(sorted(_VALID_EXCEPTION_TYPES_MCP))}"
            ),
        }
    if not pattern:
        return {"error": "MissingPattern", "details": "pattern must not be empty"}
    if exc_type == "filetype":
        normalized_ft = pattern.lower().strip()
        if normalized_ft in default_filetypes:
            return {
                "already_in_builtins": True,
                "tip": (
                    f"'{pattern}' is already a built-in exempt filetype — "
                    "write to it directly without calling routing_exception_add."
                ),
            }
    elif exc_type == "path":
        path_basename = os.path.basename(pattern.strip())
        if path_basename in default_paths or pattern.strip() in default_paths:
            return {
                "already_in_builtins": True,
                "tip": (
                    f"'{pattern}' is already a built-in exempt path — "
                    "write to it directly without calling routing_exception_add."
                ),
            }
    try:
        row = db.routing_exception_add(exc_type, pattern, note)
    except ValueError as exc:
        return {"error": "InvalidInput", "details": str(exc)}
    return {"added": True, "exception": row}


def handle_routing_exception_remove_impl(
    args: dict[str, Any],
    *,
    get_db: Callable[[], Any],
) -> dict[str, Any]:
    db = get_db()
    exc_type = (args.get("exception_type") or "").strip().lower()
    pattern = (args.get("pattern") or "").strip()
    if not exc_type or not pattern:
        return {"error": "MissingInput", "details": "exception_type and pattern are required"}
    removed = db.routing_exception_remove(exc_type, pattern)
    return {"removed": removed, "exception_type": exc_type, "pattern": pattern}


def handle_routing_exception_list_impl(
    args: dict[str, Any],
    *,
    get_db: Callable[[], Any],
) -> dict[str, Any]:
    db = get_db()
    rows = db.routing_exception_list()
    raw_limit = args.get("limit")
    raw_offset = args.get("offset")
    if raw_limit is not None or raw_offset is not None:
        try:
            offset = max(0, int(raw_offset)) if raw_offset is not None else 0
            limit = max(1, int(raw_limit)) if raw_limit is not None else len(rows)
            sliced = rows[offset : offset + limit]
            return {"exceptions": sliced, "count": len(sliced), "total": len(rows)}
        except (TypeError, ValueError):
            pass
    return {"exceptions": rows, "count": len(rows)}


def handle_inspect_write_audit_impl(
    args: dict[str, Any],
    *,
    get_db: Callable[[], Any],
) -> dict[str, Any]:
    """Return recent out-of-workspace write audit entries."""
    db = get_db()
    raw_limit = args.get("limit") or 50
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))
    entries = db.get_write_audit(limit=limit)
    return {"entries": entries, "count": len(entries), "limit": limit}
