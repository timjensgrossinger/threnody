from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from .config import BASE_DIR
from .context import is_within_repo, normalize_target_path
from .db import Database

VALID_MEMORY_SCOPES = frozenset({"global", "project", "task"})
MAX_MEMORY_KEY_LENGTH = 256
MAX_MEMORY_TASK_ID_LENGTH = 256
MAX_MEMORY_VALUE_BYTES = 64 * 1024
SWARM_STATE_KEY_PREFIX = "swarm_state:"


class MemoryRequestError(ValueError):
    """Raised when a memory request is invalid."""


class MemoryNotFoundError(LookupError):
    """Raised when a requested memory entry does not exist."""


def canonical_project_id(raw: str, workspace_root: str | Path) -> str:
    """Resolve project_id to a stable absolute path for cross-CLI sharing."""
    normalized = str(raw or "").strip()
    if not normalized:
        raise MemoryRequestError("project_id is required")
    candidate = normalize_target_path(normalized, workspace_root)
    if not is_within_repo(candidate, workspace_root):
        raise MemoryRequestError("project_id must resolve inside the workspace")
    return str(candidate.resolve())


def _get_db(db: Database | None) -> Database:
    return db if db is not None else Database()


def _normalize_scope(scope: str) -> str:
    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope not in VALID_MEMORY_SCOPES:
        raise MemoryRequestError("scope must be one of: global, project, task")
    return normalized_scope


def _normalize_key(key: str) -> str:
    if not isinstance(key, str):
        raise MemoryRequestError("key must be a string")
    normalized_key = key.strip()
    if not normalized_key:
        raise MemoryRequestError("key is required")
    if len(normalized_key) > MAX_MEMORY_KEY_LENGTH:
        raise MemoryRequestError(
            f"key must be <= {MAX_MEMORY_KEY_LENGTH} characters"
        )
    return normalized_key


def _normalize_identifiers(
    scope: str,
    project_id: str | None,
    task_id: str | None,
) -> tuple[str, str]:
    normalized_project_id = str(project_id or "").strip()
    normalized_task_id = str(task_id or "").strip()

    if scope == "global":
        if normalized_project_id or normalized_task_id:
            raise MemoryRequestError(
                "global scope does not accept project_id or task_id"
            )
        return "", ""

    if scope == "project":
        if not normalized_project_id:
            raise MemoryRequestError("project_id is required for project scope")
        if normalized_task_id:
            raise MemoryRequestError("task_id is only valid for task scope")
        return normalized_project_id, ""

    if not normalized_project_id:
        raise MemoryRequestError("project_id is required for task scope")
    if not normalized_task_id:
        raise MemoryRequestError("task_id is required for task scope")
    if len(normalized_task_id) > MAX_MEMORY_TASK_ID_LENGTH:
        raise MemoryRequestError(
            f"task_id must be <= {MAX_MEMORY_TASK_ID_LENGTH} characters"
        )
    return normalized_project_id, normalized_task_id


def _value_type_for(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise MemoryRequestError(
        "value must be JSON-serializable as a string, number, bool, array, object, or null"
    )


def _serialize_value(value: Any) -> tuple[str, str, int]:
    if isinstance(value, float) and not math.isfinite(value):
        raise MemoryRequestError("number values must be finite")

    value_type = _value_type_for(value)
    try:
        value_json = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise MemoryRequestError("value must be JSON-serializable") from exc

    value_size = len(value_json.encode("utf-8"))
    if value_size > MAX_MEMORY_VALUE_BYTES:
        raise MemoryRequestError(
            f"value must be <= {MAX_MEMORY_VALUE_BYTES} bytes when JSON-encoded"
        )
    return value_type, value_json, value_size


def _deserialize_value(value_json: str) -> Any:
    try:
        return json.loads(value_json)
    except json.JSONDecodeError as exc:
        raise MemoryRequestError("stored memory value is corrupted") from exc


def _public_identifier(value: str) -> str | None:
    return value or None


def _swarm_state_key(swarm_id: str) -> str:
    normalized_swarm_id = str(swarm_id or "").strip()
    if not normalized_swarm_id:
        raise MemoryRequestError("swarm_id is required")
    return f"{SWARM_STATE_KEY_PREFIX}{normalized_swarm_id}"


def _default_swarm_project_id(project_id: str | None) -> str:
    normalized_project_id = str(project_id or "").strip()
    if normalized_project_id:
        return normalized_project_id
    return str(BASE_DIR.resolve())


def _value_to_search_text(value: Any, value_json: str) -> str:
    if isinstance(value, str):
        return value
    return value_json


def _sync_memory_fts(
    conn: Any,
    *,
    scope: str,
    project_id: str,
    task_id: str,
    key: str,
    value_text: str,
) -> None:
    conn.execute(
        """
        DELETE FROM memory_fts
        WHERE scope = ? AND project_id = ? AND task_id = ? AND key = ?
        """,
        (scope, project_id, task_id, key),
    )
    conn.execute(
        """
        INSERT INTO memory_fts(scope, project_id, task_id, key, value_text)
        VALUES (?, ?, ?, ?, ?)
        """,
        (scope, project_id, task_id, key, value_text),
    )


def _delete_memory_fts(
    conn: Any,
    *,
    scope: str,
    project_id: str,
    task_id: str,
    key: str,
) -> None:
    conn.execute(
        """
        DELETE FROM memory_fts
        WHERE scope = ? AND project_id = ? AND task_id = ? AND key = ?
        """,
        (scope, project_id, task_id, key),
    )


_SECRET_SNIPPET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*\S+"
)


def _redact_snippet(text: str) -> str:
    return _SECRET_SNIPPET_RE.sub(r"\1=<redacted>", text)


def _escape_fts_query(query: str) -> str:
    tokens = re.findall(r"[\w.-]+", query.strip())
    if not tokens:
        raise MemoryRequestError("query must contain at least one searchable token")
    escaped: list[str] = []
    for token in tokens:
        safe = token.replace('"', '""')
        escaped.append(f'"{safe}"')
    return " ".join(escaped)


def memory_search(
    query: str,
    *,
    scope: str | None = None,
    project_id: str | None = None,
    limit: int = 10,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """Search memory values via local FTS5 (no embeddings)."""
    if not isinstance(query, str) or not query.strip():
        raise MemoryRequestError("query is required")
    if limit < 1:
        raise MemoryRequestError("limit must be at least 1")
    if limit > 50:
        limit = 50

    normalized_scope: str | None = None
    normalized_project_id: str | None = None
    if scope is not None:
        normalized_scope = _normalize_scope(scope)
    if project_id is not None and str(project_id).strip():
        normalized_project_id = str(project_id).strip()
        if normalized_scope is None:
            normalized_scope = "project"

    fts_query = _escape_fts_query(query)
    database = _get_db(db)
    where = ["memory_fts MATCH ?"]
    params: list[Any] = [fts_query]
    if normalized_scope is not None:
        where.append("memory_fts.scope = ?")
        params.append(normalized_scope)
    if normalized_project_id is not None:
        where.append("memory_fts.project_id = ?")
        params.append(normalized_project_id)

    sql = f"""
        SELECT
            memory_fts.scope,
            memory_fts.project_id,
            memory_fts.task_id,
            memory_fts.key,
            snippet(memory_fts, 4, '[', ']', '...', 48) AS snippet,
            bm25(memory_fts) AS rank
        FROM memory_fts
        WHERE {' AND '.join(where)}
        ORDER BY rank
        LIMIT ?
    """
    params.append(limit)

    with database.conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    hits: list[dict[str, Any]] = []
    for row_scope, row_project_id, row_task_id, key, snippet, rank in rows:
        hits.append({
            "scope": str(row_scope),
            "project_id": _public_identifier(str(row_project_id or "")),
            "task_id": _public_identifier(str(row_task_id or "")),
            "key": str(key),
            "snippet": _redact_snippet(str(snippet or "")),
            "rank": float(rank) if rank is not None else 0.0,
        })
    return hits


def _memory_envelope(
    *,
    key: str,
    scope: str,
    project_id: str,
    task_id: str,
    value: Any,
    value_type: str,
    updated_at: float,
) -> dict[str, Any]:
    return {
        "key": key,
        "scope": scope,
        "project_id": _public_identifier(project_id),
        "task_id": _public_identifier(task_id),
        "value": value,
        "value_type": value_type,
        "updated_at": updated_at,
    }


def memory_set(
    scope: str,
    key: str,
    value: Any,
    project_id: str | None = None,
    task_id: str | None = None,
    *,
    db: Database | None = None,
) -> dict[str, Any]:
    """Store or overwrite one memory value in an explicit scope."""
    normalized_scope = _normalize_scope(scope)
    normalized_key = _normalize_key(key)
    normalized_project_id, normalized_task_id = _normalize_identifiers(
        normalized_scope,
        project_id,
        task_id,
    )
    value_type, value_json, value_size = _serialize_value(value)
    updated_at = time.time()
    database = _get_db(db)

    with database.conn() as conn:
        conn.execute(
            """
            INSERT INTO memory (
                scope,
                project_id,
                task_id,
                key,
                value_type,
                value_json,
                value_size,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, project_id, task_id, key) DO UPDATE SET
                value_type = excluded.value_type,
                value_json = excluded.value_json,
                value_size = excluded.value_size,
                updated_at = excluded.updated_at
            """,
            (
                normalized_scope,
                normalized_project_id,
                normalized_task_id,
                normalized_key,
                value_type,
                value_json,
                value_size,
                updated_at,
            ),
        )
        _sync_memory_fts(
            conn,
            scope=normalized_scope,
            project_id=normalized_project_id,
            task_id=normalized_task_id,
            key=normalized_key,
            value_text=_value_to_search_text(value, value_json),
        )

    return _memory_envelope(
        key=normalized_key,
        scope=normalized_scope,
        project_id=normalized_project_id,
        task_id=normalized_task_id,
        value=value,
        value_type=value_type,
        updated_at=updated_at,
    )


def memory_get(
    scope: str,
    key: str,
    project_id: str | None = None,
    task_id: str | None = None,
    *,
    db: Database | None = None,
) -> dict[str, Any]:
    """Fetch one memory envelope from an explicit scope."""
    normalized_scope = _normalize_scope(scope)
    normalized_key = _normalize_key(key)
    normalized_project_id, normalized_task_id = _normalize_identifiers(
        normalized_scope,
        project_id,
        task_id,
    )
    database = _get_db(db)

    with database.conn() as conn:
        row = conn.execute(
            """
            SELECT value_type, value_json, updated_at
            FROM memory
            WHERE scope = ? AND project_id = ? AND task_id = ? AND key = ?
            """,
            (
                normalized_scope,
                normalized_project_id,
                normalized_task_id,
                normalized_key,
            ),
        ).fetchone()

    if row is None:
        raise MemoryNotFoundError(
            f"memory key '{normalized_key}' was not found in {normalized_scope} scope"
        )

    value_type, value_json, updated_at = row
    return _memory_envelope(
        key=normalized_key,
        scope=normalized_scope,
        project_id=normalized_project_id,
        task_id=normalized_task_id,
        value=_deserialize_value(value_json),
        value_type=str(value_type),
        updated_at=float(updated_at),
    )


def memory_list(
    scope: str,
    project_id: str | None = None,
    task_id: str | None = None,
    *,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """List keys and compact metadata for one explicit scope."""
    normalized_scope = _normalize_scope(scope)
    normalized_project_id, normalized_task_id = _normalize_identifiers(
        normalized_scope,
        project_id,
        task_id,
    )
    database = _get_db(db)

    with database.conn() as conn:
        rows = conn.execute(
            """
            SELECT key, scope, value_type, value_size, updated_at
            FROM memory
            WHERE scope = ? AND project_id = ? AND task_id = ?
            ORDER BY key
            """,
            (normalized_scope, normalized_project_id, normalized_task_id),
        ).fetchall()

    return [
        {
            "key": str(key),
            "scope": str(row_scope),
            "updated_at": float(updated_at),
            "value_type": str(value_type),
            "value_size": int(value_size),
        }
        for key, row_scope, value_type, value_size, updated_at in rows
    ]


def memory_delete(
    scope: str,
    key: str,
    project_id: str | None = None,
    task_id: str | None = None,
    *,
    db: Database | None = None,
) -> dict[str, Any]:
    """Hard-delete one memory row from an explicit scope."""
    normalized_scope = _normalize_scope(scope)
    normalized_key = _normalize_key(key)
    normalized_project_id, normalized_task_id = _normalize_identifiers(
        normalized_scope,
        project_id,
        task_id,
    )
    database = _get_db(db)

    with database.conn() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM memory
            WHERE scope = ? AND project_id = ? AND task_id = ? AND key = ?
            """,
            (
                normalized_scope,
                normalized_project_id,
                normalized_task_id,
                normalized_key,
            ),
        ).fetchone()
        if row is None:
            raise MemoryNotFoundError(
                f"memory key '{normalized_key}' was not found in {normalized_scope} scope"
            )

        conn.execute(
            """
            DELETE FROM memory
            WHERE scope = ? AND project_id = ? AND task_id = ? AND key = ?
            """,
            (
                normalized_scope,
                normalized_project_id,
                normalized_task_id,
                normalized_key,
            ),
        )
        _delete_memory_fts(
            conn,
            scope=normalized_scope,
            project_id=normalized_project_id,
            task_id=normalized_task_id,
            key=normalized_key,
        )
    return {"deleted": True}


def memory_set_swarm_state(
    swarm_id: str,
    snapshot: dict[str, Any],
    project_id: str | None = None,
    *,
    db: Database | None = None,
) -> dict[str, Any]:
    """Store the compact swarm_state projection under a dedicated global key."""
    return memory_set(
        "project",
        _swarm_state_key(swarm_id),
        snapshot,
        project_id=_default_swarm_project_id(project_id),
        db=db,
    )


def memory_get_swarm_state(
    swarm_id: str,
    project_id: str | None = None,
    *,
    db: Database | None = None,
) -> dict[str, Any]:
    """Fetch the compact swarm_state projection for one swarm."""
    return memory_get(
        "project",
        _swarm_state_key(swarm_id),
        project_id=_default_swarm_project_id(project_id),
        db=db,
    )


def memory_refresh_swarm_state_from_db(
    swarm_id: str,
    project_id: str | None = None,
    *,
    db: Database | None = None,
) -> dict[str, Any]:
    """Rebuild the compact swarm_state projection from authoritative DB state."""
    database = _get_db(db)
    snapshot = database.rebuild_swarm_state_from_db(swarm_id)
    normalized_project_id = _default_swarm_project_id(project_id)
    return memory_set_swarm_state(
        swarm_id,
        snapshot,
        project_id=normalized_project_id,
        db=database,
    )


__all__ = [
    "MemoryNotFoundError",
    "MemoryRequestError",
    "canonical_project_id",
    "memory_delete",
    "memory_get",
    "memory_get_swarm_state",
    "memory_list",
    "memory_refresh_swarm_state_from_db",
    "memory_set",
    "memory_set_swarm_state",
    "memory_search",
]
