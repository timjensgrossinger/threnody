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
#
# A pointer whose run never got its terminal report (session closed, abandoned
# plan) used to live forever: the hook kept appending every edit in that
# workspace to a dead run that would never be imported, and those appends kept
# the run looking active to the stale-run reaper. A pointer older than this TTL
# with no non-hook activity in its run dir is treated as absent and removed.
# 6 h is far past any observed host handoff (``_caller_has_active_host_handoff``
# considers 1 h); artifact/findings/meta writes extend a genuinely long run.
ACTIVE_POINTER_TTL_S = 6 * 3600.0
# ``source`` of records the PostToolUse hook appends. Hook appends are not
# evidence a run is alive — they are exactly what a stale pointer produces.
HOOK_SOURCE = "post_tool_use_hook"

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
        # Stamp host-reported records too, so run_activity_ts can date them.
        if "ts" not in record:
            record = {**record, "ts": time.time()}
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


def run_activity_ts(run_id: str, *, root: Path | None = None) -> float:
    """Newest *non-hook* activity in a run dir as an epoch timestamp, ``0.0`` if none.

    Counts the mtime of every file under the run dir except ``wave.jsonl``
    (meta, synthesis, ``artifacts/``, ``findings/``) and the ``ts`` of the newest
    ``wave.jsonl`` record whose ``source`` is not the PostToolUse hook. Hook
    lines are excluded because a stale active pointer produces them for as long
    as anyone edits files in that workspace.
    """
    try:
        run_dir = (root if root is not None else runs_root()) / _safe_run_id(run_id)
    except ValueError:
        return 0.0
    newest = 0.0
    try:
        if not run_dir.is_dir():
            return 0.0
        for child in run_dir.iterdir():
            if child.is_dir():
                for grandchild in child.iterdir():
                    if grandchild.is_file():
                        newest = max(newest, grandchild.stat().st_mtime)
            elif child.name != _LOG_NAME:
                newest = max(newest, child.stat().st_mtime)
        log_path = run_dir / _LOG_NAME
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict) or rec.get("source") == HOOK_SOURCE:
                        continue
                    ts = rec.get("ts")
                    if isinstance(ts, (int, float)):
                        newest = max(newest, float(ts))
    except OSError:
        log.debug("run_log: activity scan failed for %s", run_id, exc_info=True)
    return newest


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
    if not isinstance(data, dict):
        return None
    if workspace_root:
        stored_root = data.get("workspace_root")
        if stored_root and stored_root != _normalize_workspace_root(workspace_root):
            return None
    rid = data.get("run_id")
    if not rid or not _pointer_is_fresh(path, data):
        return None
    return str(rid)


def _pointer_ts(path: Path, data: dict) -> float:
    ts = data.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _pointer_is_fresh(
    path: Path,
    data: dict,
    *,
    now: float | None = None,
    ttl_s: float = ACTIVE_POINTER_TTL_S,
) -> bool:
    """True if the pointer is within its TTL; an expired one is refreshed or removed.

    Past the TTL, non-hook activity in the run dir (``run_activity_ts``) keeps
    the pointer alive and is written back as its ``ts`` so the next lookup is
    cheap again; otherwise the pointer file is unlinked. Filesystem only — the
    hook calling this must stay DB-free.
    """
    current = time.time() if now is None else now
    if current - _pointer_ts(path, data) <= ttl_s:
        return True
    rid = str(data.get("run_id") or "")
    activity = run_activity_ts(rid, root=path.parent) if rid else 0.0
    try:
        if activity and current - activity <= ttl_s:
            path.write_text(
                json.dumps({**data, "ts": activity}, separators=(",", ":")),
                encoding="utf-8",
            )
            return True
        path.unlink(missing_ok=True)
    except OSError:
        log.debug("run_log: expired pointer update failed for %s", path, exc_info=True)
    return False


def _iter_active_pointers(root: Path) -> list[tuple[Path, dict]]:
    pointers: list[tuple[Path, dict]] = []
    try:
        candidates = sorted(root.glob("active*.json"))
    except OSError:
        log.debug("run_log: pointer scan failed under %s", root, exc_info=True)
        return pointers
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.debug("run_log: unreadable active pointer %s", path, exc_info=True)
            data = {}
        pointers.append((path, data if isinstance(data, dict) else {}))
    return pointers


def active_pointer_runs(*, root: Path | None = None) -> dict[str, float]:
    """``{run_id: newest pointer ts}`` for every readable active pointer (read-only)."""
    resolved = root if root is not None else runs_root()
    runs: dict[str, float] = {}
    for path, data in _iter_active_pointers(resolved):
        rid = str(data.get("run_id") or "")
        if rid:
            runs[rid] = max(runs.get(rid, 0.0), _pointer_ts(path, data))
    return runs


def remove_active_pointers_for(run_id: str, *, root: Path | None = None) -> int:
    """Unlink every active pointer (any workspace) naming *run_id*. Returns the count."""
    try:
        target = _safe_run_id(run_id)
    except ValueError:
        return 0
    resolved = root if root is not None else runs_root()
    removed = 0
    for path, data in _iter_active_pointers(resolved):
        if str(data.get("run_id") or "") != target:
            continue
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            log.debug("run_log: pointer unlink failed for %s", path, exc_info=True)
    return removed


def prune_active_pointers(
    *,
    now: float | None = None,
    ttl_s: float = ACTIVE_POINTER_TTL_S,
    root: Path | None = None,
) -> list[str]:
    """Remove pointers that are unreadable, expired, or whose workspace is gone.

    The workspace check lives here rather than in ``get_active_run``: the hook
    looks a pointer up by its own (existing) cwd, so only a sweep can find the
    pointers pytest temp workspaces left behind. Returns removed file names.
    """
    resolved = root if root is not None else runs_root()
    removed: list[str] = []
    for path, data in _iter_active_pointers(resolved):
        workspace = data.get("workspace_root")
        stale = (
            not data.get("run_id")
            or (isinstance(workspace, str) and workspace and not os.path.isdir(workspace))
        )
        if not stale and _pointer_is_fresh(path, data, now=now, ttl_s=ttl_s):
            continue
        try:
            path.unlink(missing_ok=True)
            removed.append(path.name)
        except OSError:
            log.debug("run_log: pointer unlink failed for %s", path, exc_info=True)
    if removed:
        log.info("run_log: removed %d stale active-run pointer(s)", len(removed))
    return removed


def clear_active_run(run_id: str | None = None, *, workspace_root: str | None = None) -> None:
    """Clear the active-run pointer (optionally only if it matches *run_id*)."""
    try:
        path = _active_pointer_path(workspace_root)
        if run_id is not None and workspace_root is None:
            # The terminal report clears without a workspace root, which only
            # ever reached the legacy global file and left the per-workspace
            # pointer aimed at a finished run. Clear every pointer naming it.
            remove_active_pointers_for(run_id)
        if run_id is not None and get_active_run(workspace_root=workspace_root) not in (
            None,
            _safe_run_id(run_id),
        ):
            return
        path.unlink(missing_ok=True)
    except Exception:
        log.debug("run_log: clear_active_run failed", exc_info=True)
