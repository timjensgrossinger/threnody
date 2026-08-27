"""
MCP tool handlers for memory operations (get, set, list, search, delete).
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from ..memory import (
    MemoryNotFoundError,
    MemoryRequestError,
    memory_delete,
    memory_get,
    memory_list,
    memory_search,
    memory_set,
)


def memory_error_response(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, MemoryNotFoundError):
        return {"error": "not_found", "details": str(exc)}
    return {"error": "invalid_request", "details": str(exc)}


def optional_memory_string_arg(args: Mapping[str, Any], key: str) -> str | None:
    raw_value = args.get(key)
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise MemoryRequestError(f"{key} must be a string")
    normalized = raw_value.strip()
    return normalized or None


def required_memory_string_arg(args: Mapping[str, Any], key: str) -> str:
    raw_value = args.get(key)
    if not isinstance(raw_value, str):
        raise MemoryRequestError(f"{key} must be a string")
    return raw_value


def normalize_memory_request(
    args: Mapping[str, Any],
    *,
    require_key: bool = False,
    normalize_project_id: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    raw_scope = args.get("scope")
    if not isinstance(raw_scope, str):
        raise MemoryRequestError("scope must be a string")

    scope = raw_scope.strip().lower()
    project_id = optional_memory_string_arg(args, "project_id")
    if project_id is not None and normalize_project_id is not None:
        try:
            project_id = normalize_project_id(project_id)
        except ValueError as exc:
            raise MemoryRequestError(str(exc)) from exc
    task_id = optional_memory_string_arg(args, "task_id")

    normalized: dict[str, Any] = {
        "scope": scope,
        "project_id": project_id,
        "task_id": task_id,
    }
    if require_key:
        normalized["key"] = required_memory_string_arg(args, "key")
    return normalized


def handle_memory_list_impl(
    args: dict[str, Any],
    *,
    get_db: Callable[[], Any],
    normalize_project_id: Callable[[str], str] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    try:
        db = get_db()
    except Exception:
        return {"error": "database unavailable — route_task still works", "code": "DB_UNAVAILABLE"}
    try:
        normalized = normalize_memory_request(args, normalize_project_id=normalize_project_id)
        raw_limit = args.get("limit")
        limit = int(raw_limit) if raw_limit is not None else None
        raw_offset = args.get("offset")
        offset = int(raw_offset) if raw_offset is not None else None
        return memory_list(
            normalized.get("scope", ""),
            project_id=normalized.get("project_id"),
            task_id=normalized.get("task_id"),
            limit=limit,
            offset=offset,
            db=db,
        )
    except (MemoryNotFoundError, MemoryRequestError) as exc:
        return memory_error_response(exc)


def handle_memory_get_impl(
    args: dict[str, Any],
    *,
    get_db: Callable[[], Any],
    normalize_project_id: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    try:
        db = get_db()
    except Exception:
        return {"error": "database unavailable — route_task still works", "code": "DB_UNAVAILABLE"}
    try:
        normalized = normalize_memory_request(
            args, require_key=True, normalize_project_id=normalize_project_id
        )
        return memory_get(
            normalized.get("scope", ""),
            normalized.get("key", ""),
            project_id=normalized.get("project_id"),
            task_id=normalized.get("task_id"),
            db=db,
        )
    except (MemoryNotFoundError, MemoryRequestError) as exc:
        return memory_error_response(exc)


def handle_memory_set_impl(
    args: dict[str, Any],
    *,
    get_db: Callable[[], Any],
    normalize_project_id: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    try:
        if "value" not in args:
            raise MemoryRequestError("value is required")
        db = get_db()
    except MemoryRequestError as exc:
        return memory_error_response(exc)
    except Exception:
        return {"error": "database unavailable — route_task still works", "code": "DB_UNAVAILABLE"}
    try:
        normalized = normalize_memory_request(
            args, require_key=True, normalize_project_id=normalize_project_id
        )
        return memory_set(
            normalized.get("scope", ""),
            normalized.get("key", ""),
            args.get("value"),
            project_id=normalized.get("project_id"),
            task_id=normalized.get("task_id"),
            db=db,
        )
    except (MemoryNotFoundError, MemoryRequestError) as exc:
        return memory_error_response(exc)


def handle_memory_delete_impl(
    args: dict[str, Any],
    *,
    get_db: Callable[[], Any],
    normalize_project_id: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    try:
        db = get_db()
    except Exception:
        return {"error": "database unavailable — route_task still works", "code": "DB_UNAVAILABLE"}
    try:
        normalized = normalize_memory_request(
            args, require_key=True, normalize_project_id=normalize_project_id
        )
        return memory_delete(
            normalized.get("scope", ""),
            normalized.get("key", ""),
            project_id=normalized.get("project_id"),
            task_id=normalized.get("task_id"),
            db=db,
        )
    except (MemoryNotFoundError, MemoryRequestError) as exc:
        return memory_error_response(exc)


def handle_memory_search_impl(
    args: dict[str, Any],
    *,
    get_db: Callable[[], Any],
) -> dict[str, Any]:
    try:
        db = get_db()
    except Exception:
        return {"error": "database unavailable — route_task still works", "code": "DB_UNAVAILABLE"}
    try:
        query = required_memory_string_arg(args, "query")
        scope = optional_memory_string_arg(args, "scope")
        project_id = optional_memory_string_arg(args, "project_id")
        limit_raw = args.get("limit", 10)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 10
        hits = memory_search(
            query,
            scope=scope,
            project_id=project_id,
            limit=limit,
            db=db,
        )
        return {"query": query, "count": len(hits), "hits": hits}
    except MemoryRequestError as exc:
        return memory_error_response(exc)
