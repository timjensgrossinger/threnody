"""Append-only ledger of host subagent spawns (``logs/agent_spawns.jsonl``).

Written by the Claude Code Agent hook (``shared/agent_hook.py``) — one JSON line
per hook event: ``pre`` (the decision on the Agent call), ``post`` (what actually
ran, with the concrete ``resolvedModel``), ``subagent-start`` / ``subagent-stop``.

Deliberately a flat file and never sqlite: the hook runs on every spawn of every
session, and a hook that opened ``cache.db`` would contend with the MCP server's
own writers (and has, for other hooks — see the orphan-WAL incident). Lines are
appended with one ``write`` each and the file rotates at ~5 MB, keeping three
files in total (``agent_spawns.jsonl`` plus ``.1`` and ``.2``).

The prompt body is never recorded — only a truncated ``description`` — because
the ledger outlives the session and prompts carry whatever the user was working on.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

LEDGER_NAME = "agent_spawns.jsonl"
SESSIONS_NAME = "agent_sessions.json"
VARIANTS_NAME = "agent_variants.json"

#: Rotate once the live file passes this size.
MAX_BYTES = 5 * 1024 * 1024
#: Files kept in total, the live one included.
KEEP_FILES = 3
#: Sessions remembered in ``agent_sessions.json``.
MAX_SESSIONS = 200
#: ``description`` is cut to this many characters.
DESCRIPTION_MAX = 120

_LOG_DIR_ENV = "THRENODY_LOG_DIR"

#: Keys every row of an event kind carries (``null`` when unknown), so a reader
#: never has to guess whether a missing key means "not set" or "not recorded".
#: Other keys (``planned``, ``model_source``, ``error`` …) appear only when set.
EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "pre": (
        "session_id", "tool_use_id", "cwd", "caller", "requested_type", "resolved_type",
        "base_type", "requested_model", "resolved_model_alias", "tier", "requested_effort",
        "applied_effort", "effort_source", "effort_unapplied_reason", "variant_created",
        "decision", "run_id", "spawn_id", "description",
    ),
    "post": (
        "session_id", "tool_use_id", "agent_id", "agent_type", "resolved_model", "status",
        "duration_ms",
    ),
    "subagent-start": ("session_id", "agent_id", "agent_type"),
    "subagent-stop": ("session_id", "agent_id", "agent_type"),
}


def log_dir() -> Path:
    """``$THRENODY_LOG_DIR`` if set, else ``<install>/logs``. Resolved per call."""
    override = os.environ.get(_LOG_DIR_ENV)
    if override and override.strip():
        return Path(override.strip()).expanduser()
    install = os.environ.get("THRENODY_INSTALL_DIR") or "~/.local/lib/threnody"
    return Path(install).expanduser() / "logs"


def ledger_path(directory: Path | None = None) -> Path:
    return (directory or log_dir()) / LEDGER_NAME


def truncate_description(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if len(text) > DESCRIPTION_MAX:
        text = text[: DESCRIPTION_MAX - 1] + "…"
    return text or None


def _rotated(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _rotate_if_needed(path: Path, max_bytes: int) -> None:
    """Shift ``.jsonl`` → ``.1`` → ``.2`` once *path* reaches *max_bytes*.

    Several hooks of one fan-out may race here. A non-blocking ``flock`` makes
    one of them rotate; the others skip and append to whichever file is live.
    """
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    lock_path = path.with_name(path.name + ".lock")
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        fcntl = None  # type: ignore[assignment]
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return
        try:
            if path.stat().st_size < max_bytes:
                return  # somebody else rotated while we waited
        except OSError:
            return
        for index in range(KEEP_FILES - 1, 0, -1):
            src = path if index == 1 else _rotated(path, index - 1)
            if src.exists():
                os.replace(src, _rotated(path, index))
    except OSError:
        return
    finally:
        if fd is not None:
            os.close(fd)


def append_event(
    event: dict[str, Any],
    *,
    directory: Path | None = None,
    max_bytes: int = MAX_BYTES,
) -> bool:
    """Append one event line; ``False`` on any failure (never raises)."""
    try:
        schema = EVENT_FIELDS.get(str(event.get("event")), ())
        record: dict[str, Any] = {"ts": round(time.time(), 3), "event": event.get("event")}
        record.update({key: event.get(key) for key in schema})
        record.update(
            {k: v for k, v in event.items() if k not in record and v is not None}
        )
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        path = ledger_path(directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path, max_bytes)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:  # noqa: BLE001 - the ledger must never break a spawn
        return False


def _iter_file(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def read_events(
    *,
    since_ts: float | None = None,
    directory: Path | None = None,
) -> list[dict[str, Any]]:
    """Every event, oldest first, across the rotated files; *since_ts* filters."""
    path = ledger_path(directory)
    files = [_rotated(path, i) for i in range(KEEP_FILES - 1, 0, -1)] + [path]
    out: list[dict[str, Any]] = []
    for file in files:
        for record in _iter_file(file):
            ts = record.get("ts")
            if since_ts is not None and not (isinstance(ts, (int, float)) and ts >= since_ts):
                continue
            out.append(record)
    return out


def tail(n: int = 20, *, directory: Path | None = None) -> list[dict[str, Any]]:
    return read_events(directory=directory)[-max(0, int(n)):] if n > 0 else []


def join_spawns(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per spawn: the ``pre`` decision joined with what ran.

    ``pre`` ↔ ``post`` share ``tool_use_id``; ``post`` names the ``agent_id``
    that ``subagent-start`` / ``subagent-stop`` carry. A ``post`` with no ``pre``
    (hook installed mid-session, or ``pre`` failed) still yields a record.
    """
    spawns: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    by_agent: dict[str, dict[str, Any]] = {}
    pending_agent: dict[str, dict[str, Any]] = {}
    for record in events:
        kind = record.get("event")
        if kind in ("pre", "post"):
            key = str(record.get("tool_use_id") or f"_anon{len(order)}")
            spawn = spawns.get(key)
            if spawn is None:
                spawn = {"tool_use_id": record.get("tool_use_id")}
                spawns[key] = spawn
                order.append(key)
            if kind == "pre":
                spawn["pre"] = record
            else:
                spawn["post"] = record
                agent_id = record.get("agent_id")
                if agent_id:
                    by_agent[str(agent_id)] = spawn
                    early = pending_agent.pop(str(agent_id), None)
                    if early:
                        spawn.update(early)
        elif kind in ("subagent-start", "subagent-stop"):
            agent_id = str(record.get("agent_id") or "")
            if not agent_id:
                continue
            slot = "start" if kind == "subagent-start" else "stop"
            target = by_agent.get(agent_id)
            if target is not None:
                target[slot] = record
            else:
                pending_agent.setdefault(agent_id, {})[slot] = record
    return [spawns[k] for k in order]


def _effort_unapplied(pre: dict[str, Any]) -> bool:
    return bool(pre.get("requested_effort")) and not pre.get("applied_effort")


def summarize(
    *,
    since_ts: float | None = None,
    directory: Path | None = None,
    recent_unapplied: int = 5,
) -> dict[str, Any]:
    """Counts for ``inspect_status``: spawns, effort sources, models, misses."""
    events = read_events(since_ts=since_ts, directory=directory)
    spawns = join_spawns(events)
    by_source: dict[str, int] = {}
    by_model: dict[str, int] = {}
    by_decision: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    unapplied: list[dict[str, Any]] = []
    for spawn in spawns:
        pre = spawn.get("pre") or {}
        post = spawn.get("post") or {}
        if pre:
            source = str(pre.get("effort_source") or "none")
            by_source[source] = by_source.get(source, 0) + 1
            decision = str(pre.get("decision") or "pass")
            by_decision[decision] = by_decision.get(decision, 0) + 1
            if _effort_unapplied(pre):
                reason = str(pre.get("effort_unapplied_reason") or "unknown")
                by_reason[reason] = by_reason.get(reason, 0) + 1
                unapplied.append(
                    {
                        "ts": pre.get("ts"),
                        "requested_type": pre.get("requested_type"),
                        "resolved_type": pre.get("resolved_type"),
                        "requested_effort": pre.get("requested_effort"),
                        "effort_source": pre.get("effort_source"),
                        "reason": pre.get("effort_unapplied_reason"),
                    }
                )
        model = post.get("resolved_model")
        if model:
            by_model[str(model)] = by_model.get(str(model), 0) + 1
    return {
        "total": sum(1 for s in spawns if s.get("pre") or s.get("post")),
        "by_effort_source": dict(sorted(by_source.items())),
        "by_decision": dict(sorted(by_decision.items())),
        "effort_unapplied": len(unapplied),
        "effort_unapplied_by_reason": dict(sorted(by_reason.items())),
        "by_resolved_model": dict(sorted(by_model.items())),
        "recent_unapplied": unapplied[-max(0, recent_unapplied):] if recent_unapplied else [],
    }


def unapplied_spawns(
    *, since_ts: float | None = None, directory: Path | None = None
) -> list[dict[str, Any]]:
    """``pre`` events whose requested effort was not pinned on the spawn."""
    return [
        e for e in read_events(since_ts=since_ts, directory=directory)
        if e.get("event") == "pre" and _effort_unapplied(e)
    ]


# ---------------------------------------------------------------------------
# Small JSON state files beside the ledger
# ---------------------------------------------------------------------------

def read_json_state(name: str, *, directory: Path | None = None) -> dict[str, Any]:
    try:
        raw = json.loads(((directory or log_dir()) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_json_state(name: str, data: dict[str, Any], *, directory: Path | None = None) -> bool:
    """Atomic replace (temp file + ``os.replace``); ``False`` on failure."""
    target = (directory or log_dir()) / name
    tmp = target.with_name(f".{name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def record_session_start(
    session_id: str, ts: float | None = None, *, directory: Path | None = None
) -> bool:
    """Remember when *session_id*'s process loaded its agent definitions."""
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    sessions = read_json_state(SESSIONS_NAME, directory=directory)
    sessions = {
        k: v for k, v in sessions.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    sessions[session_id.strip()] = float(ts if ts is not None else time.time())
    if len(sessions) > MAX_SESSIONS:
        newest = sorted(sessions.items(), key=lambda kv: kv[1], reverse=True)[:MAX_SESSIONS]
        sessions = dict(newest)
    return write_json_state(SESSIONS_NAME, sessions, directory=directory)


def session_start_for(session_id: Any, *, directory: Path | None = None) -> float | None:
    if not isinstance(session_id, str) or not session_id:
        return None
    value = read_json_state(SESSIONS_NAME, directory=directory).get(session_id)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None
