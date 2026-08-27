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

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from .config import BASE_DIR

log = logging.getLogger(__name__)

RUNS_ROOT = BASE_DIR / "runs"

# Env override for the runs root, honoured at *call* time — the sibling of
# ``learning_journal.journal_root()`` and the same defect.
#
# ``BASE_DIR`` is fixed when ``config`` is imported, so nothing downstream can
# redirect this, and the test suite had no isolation for it at all. The result on
# the reference install: ``runs/active.json`` — the pointer the PostToolUse
# learning hook and the terminal ``import_run_log`` follow — was left pointing at
# a deleted pytest temp dir by a test run. That silently breaks learning capture
# for real runs, which is the likely reason a real 14-agent review swarm left
# handoff snapshots but not one ``model_quality_events`` row.
_RUNS_ROOT_ENV = "THRENODY_RUNS_ROOT"
_DEFAULT_RUNS_ROOT = RUNS_ROOT


def runs_root() -> Path:
    """Resolve the runs root now, not at import time.

    Precedence matches ``learning_journal.journal_root()``: an explicitly
    reassigned ``RUNS_ROOT`` module attribute wins over the ambient
    ``THRENODY_RUNS_ROOT`` env var, which in turn wins over the import-time
    default. Never cached.
    """
    if RUNS_ROOT != _DEFAULT_RUNS_ROOT:
        return RUNS_ROOT
    override = os.environ.get(_RUNS_ROOT_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return RUNS_ROOT
_LOG_NAME = "wave.jsonl"
_META_NAME = "meta.json"
# Where an agent leaves output that its dependents read. Without this, a plan can
# only *name* a dependency, so the dependent agent either re-derives the upstream
# analysis or the host re-pastes it into every dependent prompt.
_ARTIFACTS_NAME = "artifacts"
# Pointer(s) to the run a PostToolUse learning hook should append to — one file
# per workspace (see _active_pointer_path) plus a legacy global fallback. The
# MCP execute_swarm/plan response sets it; the terminal report clears it. The
# hook stays dependency-light (run_log only) by reading this rather than the DB.

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
    """Return ``runs_root() / <run_id>``; optionally create it."""
    d = runs_root() / _safe_run_id(run_id)
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def run_log_path(run_id: str) -> Path:
    return run_log_dir(run_id) / _LOG_NAME


def artifacts_dir(run_id: str, *, create: bool = False) -> Path:
    """``<run_dir>/artifacts`` — where an agent leaves output for its dependents."""
    d = run_log_dir(run_id, create=create) / _ARTIFACTS_NAME
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def artifact_path(run_id: str, spawn_id: str) -> Path:
    """Per-agent artifact file. *spawn_id* is reduced to one safe path segment."""
    safe = _SAFE_ID.sub("_", str(spawn_id or "agent").strip()) or "agent"
    if safe in {".", ".."}:
        safe = "agent"
    return artifacts_dir(run_id) / f"{safe}.md"


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
    root = runs_root()
    if not root.exists():
        return []
    pending: list[str] = []
    try:
        for child in root.iterdir():
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
    root = runs_root()
    if not root.exists() or keep < 0:
        return
    try:
        dirs = [c for c in root.iterdir() if c.is_dir()]
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


def _normalize_workspace_root(workspace_root: str) -> str:
    return os.path.normpath(str(workspace_root))


def _active_pointer_path(workspace_root: str | None) -> Path:
    """One pointer file per workspace, not one global file for every session.

    A single global ``active.json`` meant two concurrent Claude Code sessions in
    different repos shared one PostToolUse learning-hook target: session B's file
    edits were appended to session A's run log, and vice versa — the source of
    both the foreign ``assigned_files`` entries and the wrong ``reported_agents``
    count seen in production. ``None`` keeps the pre-existing global file as a
    fallback for the rare caller that genuinely has no workspace to scope by.
    """
    if not workspace_root:
        return runs_root() / "active.json"
    digest = hashlib.sha256(
        _normalize_workspace_root(workspace_root).encode("utf-8")
    ).hexdigest()[:12]
    return runs_root() / f"active-{digest}.json"


def set_active_run(run_id: str, *, workspace_root: str | None = None) -> None:
    """Mark *run_id* as the run the PostToolUse learning hook should append to."""
    try:
        runs_root().mkdir(parents=True, exist_ok=True)
        payload = {"run_id": _safe_run_id(run_id), "ts": time.time()}
        if workspace_root:
            payload["workspace_root"] = _normalize_workspace_root(workspace_root)
        _active_pointer_path(workspace_root).write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8"
        )
    except Exception:
        log.debug("run_log: set_active_run failed for %s", run_id, exc_info=True)


def get_active_run(workspace_root: str | None = None) -> str | None:
    """Return the active run for *workspace_root*, or the legacy global pointer.

    When *workspace_root* is given but the resolved pointer's own recorded root
    disagrees (defensive — pointer files are keyed by hash, so this only matters
    if two roots ever collided), the mismatch is treated as "no active run"
    rather than risk attributing a hook capture to the wrong session.
    """
    path = _active_pointer_path(workspace_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if workspace_root:
        stored_root = data.get("workspace_root")
        if stored_root and stored_root != _normalize_workspace_root(workspace_root):
            return None
    rid = data.get("run_id")
    return str(rid) if rid else None


def clear_active_run(run_id: str | None = None, *, workspace_root: str | None = None) -> None:
    """Clear the active-run pointer (optionally only if it matches *run_id*)."""
    try:
        path = _active_pointer_path(workspace_root)
        if run_id is not None and get_active_run(workspace_root=workspace_root) not in (
            None,
            _safe_run_id(run_id),
        ):
            return
        path.unlink(missing_ok=True)
    except Exception:
        log.debug("run_log: clear_active_run failed", exc_info=True)
