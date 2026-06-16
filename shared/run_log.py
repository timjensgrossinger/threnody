"""
Append-only JSONL run log for host-native wave execution.

Host-native swarms / orchestration / workflow runs no longer report learning to
the MCP server after every wave (that per-wave round-trip + DB write was the
dominant local cost — see ``host_learning.import_run_log``). Instead each agent
result is captured as one JSON line in a per-run log under

    ~/.local/lib/threnody/runs/<run_id>/wave.jsonl

written either by the PostToolUse learning hook (zero model tokens) or by the
host itself, and imported into the database exactly once at terminal /
warm-path time.

The log is the durable record for a run: ``read_run_log`` tolerates a trailing
partial line so an import after a mid-run crash is safe, and imports are
idempotent (see ``host_learning.import_run_log``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

from .config import BASE_DIR

log = logging.getLogger(__name__)

RUNS_ROOT = BASE_DIR / "runs"
_LOG_NAME = "wave.jsonl"
_META_NAME = "meta.json"
# Pointer to the run a PostToolUse learning hook should append to. The MCP
# execute_swarm/plan response sets it; the terminal report clears it. The hook
# stays dependency-light (run_log only) by reading this rather than the DB.
_ACTIVE_POINTER = RUNS_ROOT / "active.json"

# A run id is a generated ``swarm-<hex>`` token, but callers may pass a
# user-supplied id. Constrain it to a single safe path segment so it can never
# escape RUNS_ROOT.
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def _safe_run_id(run_id: str) -> str:
    if not run_id or not str(run_id).strip():
        raise ValueError("run_id must be a non-empty string")
    cleaned = _SAFE_ID.sub("_", str(run_id).strip())
    # Defuse "." / ".." after substitution.
    if cleaned in {".", ".."} or not cleaned:
        raise ValueError(f"unsafe run_id: {run_id!r}")
    return cleaned


def run_log_dir(run_id: str, *, create: bool = False) -> Path:
    """Return ``RUNS_ROOT / <run_id>``; optionally create it."""
    d = RUNS_ROOT / _safe_run_id(run_id)
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def run_log_path(run_id: str) -> Path:
    return run_log_dir(run_id) / _LOG_NAME


def run_meta_path(run_id: str) -> Path:
    return run_log_dir(run_id) / _META_NAME


def append_agent_record(run_id: str, record: dict) -> None:
    """Append one agent result as a JSON line. Best-effort, no fsync.

    A single ``O_APPEND`` write of a sub-page payload is atomic on local
    filesystems, so concurrent appends from parallel wave agents do not
    interleave. Failures are logged and swallowed — learning capture must never
    break a run.
    """
    try:
        path = run_log_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        log.debug("run_log: append failed for %s", run_id, exc_info=True)


def read_run_log(run_id: str) -> list[dict]:
    """Read all agent records. Tolerates a trailing partial/corrupt line."""
    path = run_log_path(run_id)
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    # Crash-truncated tail line — stop, keep what parsed.
                    log.debug("run_log: skipping unparsable line in %s", run_id)
                    continue
    except OSError:
        log.debug("run_log: read failed for %s", run_id, exc_info=True)
    return records


def write_run_meta(run_id: str, meta: dict) -> None:
    """Write the run metadata snapshot (topology, waves, report_mode, ...)."""
    try:
        path = run_meta_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(meta)
        payload.setdefault("written_ts", time.time())
        path.write_text(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        log.debug("run_log: meta write failed for %s", run_id, exc_info=True)


def read_run_meta(run_id: str) -> dict:
    path = run_meta_path(run_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.debug("run_log: meta read failed for %s", run_id, exc_info=True)
        return {}


def mark_imported(run_id: str) -> None:
    """Record that a run's log has been imported into the DB (idempotency)."""
    meta = read_run_meta(run_id)
    meta["imported_ts"] = time.time()
    write_run_meta(run_id, meta)


def is_imported(run_id: str) -> bool:
    return bool(read_run_meta(run_id).get("imported_ts"))


def iter_pending_runs() -> list[str]:
    """Run ids with a log present but not yet imported — for the warm-path daemon."""
    if not RUNS_ROOT.exists():
        return []
    pending: list[str] = []
    try:
        for child in RUNS_ROOT.iterdir():
            if not child.is_dir():
                continue
            if not (child / _LOG_NAME).exists():
                continue
            if is_imported(child.name):
                continue
            pending.append(child.name)
    except OSError:
        log.debug("run_log: iter_pending_runs failed", exc_info=True)
    return pending


def prune_runs(keep: int = 20) -> None:
    """Keep the *keep* most-recently-modified run dirs; drop older ones.

    Mirrors the backup-rotation policy in ``db`` (``cache.backup_keep``).
    """
    if not RUNS_ROOT.exists() or keep < 0:
        return
    try:
        dirs = [c for c in RUNS_ROOT.iterdir() if c.is_dir()]
    except OSError:
        return
    if len(dirs) <= keep:
        return
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    import shutil

    for stale in dirs[keep:]:
        try:
            shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            log.debug("run_log: prune failed for %s", stale, exc_info=True)


def set_active_run(run_id: str, *, workspace_root: str | None = None) -> None:
    """Mark *run_id* as the run the PostToolUse learning hook should append to."""
    try:
        RUNS_ROOT.mkdir(parents=True, exist_ok=True)
        payload = {"run_id": _safe_run_id(run_id), "ts": time.time()}
        if workspace_root:
            payload["workspace_root"] = workspace_root
        _ACTIVE_POINTER.write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8"
        )
    except Exception:
        log.debug("run_log: set_active_run failed for %s", run_id, exc_info=True)


def get_active_run() -> str | None:
    if not _ACTIVE_POINTER.exists():
        return None
    try:
        data = json.loads(_ACTIVE_POINTER.read_text(encoding="utf-8"))
        rid = data.get("run_id")
        return str(rid) if rid else None
    except (OSError, json.JSONDecodeError):
        return None


def clear_active_run(run_id: str | None = None) -> None:
    """Clear the active-run pointer (optionally only if it matches *run_id*)."""
    try:
        if run_id is not None and get_active_run() not in (None, _safe_run_id(run_id)):
            return
        _ACTIVE_POINTER.unlink(missing_ok=True)
    except Exception:
        log.debug("run_log: clear_active_run failed", exc_info=True)
