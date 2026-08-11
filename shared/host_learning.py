"""Host-native execution learning ingest — closes the feedback loop for swarms/plans."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from .agents import check_draft_ready, derive_learning_quality, pattern_hash, structured_pattern_example
from .config import TGsConfig
from .roles import derive_role_from_task
from .consensus import (
    build_judge_prompt,
    consensus_tally,
    parse_judge_decision,
    persona_id_from_spawn_id,
)
from .context import is_within_repo, normalize_target_path, read_source_cached
from .db import Database
from .eval import BackgroundEvaluator, WaveFileTracker, cold_path_adjust
from .host_spawn import build_judge_spawn
from .memory import memory_refresh_swarm_state_from_db
from .outcomes import record_swarm_outcome
from .router import TaskRouter
from .style import DecompositionPrefs, StyleLearner

log = logging.getLogger(__name__)

_HOST_RUN_META: dict[str, dict[str, Any]] = {}
_HOST_WAVE_TRACKERS: dict[str, WaveFileTracker] = {}

_FILE_PATH_RE = re.compile(r"(?:^|\s)((?:\./|/)?[\w./-]+\.\w{1,6})")
_HOST_HANDOFF_EVENT = "host_handoff_registered"


def host_task_id(run_id: str, spawn_id: str) -> str:
    return f"{run_id}:{spawn_id}"


def plan_run_id(task_text: str) -> str:
    digest = hashlib.sha256(task_text.encode()).hexdigest()[:16]
    return f"plan-{digest}"


def _extract_file_paths(text: str) -> set[str]:
    paths: set[str] = set()
    for match in _FILE_PATH_RE.finditer(text):
        candidate = match.group(1)
        if len(candidate) > 3:
            paths.add(candidate)
    return paths


def _normalize_outcome(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value not in {"accepted", "revised", "reworked", "rejected"}:
        raise ValueError("outcome must be one of: accepted, revised, reworked, rejected")
    return value


def _looks_like_path(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if stripped.startswith(("/", "~")):
        return True
    return ":" in stripped[:3]


def _effective_workspace_root(
    workspace_root: str | None,
    meta: Mapping[str, Any],
) -> str | None:
    if isinstance(workspace_root, str) and workspace_root.strip():
        return workspace_root.strip()
    stored = meta.get("workspace_root")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    project_id = meta.get("project_id")
    if isinstance(project_id, str) and project_id.strip() and _looks_like_path(project_id):
        return project_id.strip()
    return None


def _resolve_touched_path(workspace_root: str | None, path: str) -> Path | None:
    if not isinstance(path, str) or not path.strip():
        return None
    raw = path.strip()
    try:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
            return resolved if resolved.is_file() else None
        if workspace_root:
            resolved = normalize_target_path(raw, workspace_root)
            return resolved if resolved.is_file() else None
    except (OSError, ValueError):
        log.debug("could not resolve touched path %s", raw, exc_info=True)
    return None


def _normalize_touched_file_key(workspace_root: str | None, path: str) -> str:
    resolved = _resolve_touched_path(workspace_root, path)
    if resolved is None:
        return path.strip()
    if workspace_root:
        try:
            root = Path(workspace_root).expanduser().resolve(strict=False)
            if is_within_repo(resolved, root):
                return resolved.relative_to(root).as_posix()
        except (OSError, ValueError):
            log.debug("could not relativize touched path %s", path, exc_info=True)
    return resolved.as_posix()


def _auto_output_excerpt(
    paths: list[str],
    workspace_root: str | None,
    *,
    max_chars: int = 400,
) -> str:
    if not paths:
        return ""
    per_file = max(80, max_chars // max(1, len(paths)))
    parts: list[str] = []
    for path in paths:
        if not isinstance(path, str) or not path.strip():
            continue
        resolved = _resolve_touched_path(workspace_root, path)
        if resolved is None:
            continue
        key = _normalize_touched_file_key(workspace_root, path)
        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log.debug("could not read %s for auto excerpt", resolved, exc_info=True)
            continue
        size_kb = max(1, len(content.encode("utf-8")) // 1024)
        snippet = " ".join(content.split())
        if len(snippet) > per_file:
            snippet = snippet[: per_file - 3].rstrip() + "..."
        parts.append(f"wrote {key} ({size_kb}KB): {snippet}")
    return "; ".join(parts)


def effective_learning_capture(config: TGsConfig | None, caller: str | None) -> str:
    """Resolve the capture mode for *caller*.

    ``hook`` capture only works where install.sh actually registered a learning
    hook (``LEARNING_HOOK_CAPABLE_SHELLS`` — claude-code, codex, cursor,
    github-copilot-cli). Other host CLIs (Junie, OpenCode, …) fall back to
    ``model`` capture: the host passes per-agent results in the single terminal
    report. Same fidelity, one call, no per-wave round-trip — so no CLI loses
    learning when it lacks a wired hook.
    """
    from .config import (
        LEARNING_HOOK_CAPABLE_SHELLS,
        normalize_routing_policy_shell_id,
    )

    cap = getattr(config.host_native, "learning_capture", "hook") if config else "hook"
    if cap != "hook":
        return cap
    shell_id = normalize_routing_policy_shell_id(caller)
    if shell_id in LEARNING_HOOK_CAPABLE_SHELLS:
        return "hook"
    return "model"


def build_learning_report_contract(
    workspace_root: str | None,
    *,
    run_id: str | None = None,
    config: TGsConfig | None = None,
    caller: str | None = None,
) -> dict[str, Any]:
    """Host-facing contract for report learning fields.

    Advertises the active ``report_mode`` so the host knows whether to report
    learning per wave (``inline``) or accumulate it and report once at terminal
    (``batch`` — the default; worker waves need no ``report_host_wave`` call,
    capture happens via the PostToolUse hook or the host's own appends).
    """
    report_mode = "batch"
    if config is not None:
        report_mode = getattr(config.host_native, "report_mode", "batch")
    # Resolve per-caller: only hook-capable shells get `hook`; others → `model`.
    learning_capture = effective_learning_capture(config, caller)

    contract: dict[str, Any] = {
        "workspace_root": workspace_root,
        "report_mode": report_mode,
        "learning_capture": learning_capture,
        "per_agent": [
            "task_id",
            "spawn_id",
            "success",
            "touched_files",
            "output_excerpt",
        ],
        "output_excerpt_hint": (
            "1-2 sentence agent summary or first ~400 chars of written file"
        ),
        "terminal": {"outcome": "accepted|revised|reworked|rejected"},
    }
    if report_mode == "batch":
        contract["batch"] = {
            "worker_waves": (
                "Do NOT call report_host_wave for plain worker waves. Spawn the "
                "wave natively; per-agent learning is captured automatically "
                "(PostToolUse hook) or, when learning_capture=model, by passing "
                "agents to a single terminal report."
            ),
            "round_trips": (
                "report_host_wave is only needed for consensus waves and "
                "expand_host_plan. Report once at terminal via "
                "report_host_swarm_complete(outcome=...)."
            ),
        }
        if run_id:
            try:
                from . import run_log

                contract["batch"]["run_log_path"] = str(run_log.run_log_path(run_id))
            except Exception:
                log.debug("run_log path for contract failed", exc_info=True)
    return contract


def _wave_tracker(run_id: str) -> WaveFileTracker:
    tracker = _HOST_WAVE_TRACKERS.get(run_id)
    if tracker is None:
        tracker = WaveFileTracker()
        _HOST_WAVE_TRACKERS[run_id] = tracker
    return tracker


def _persist_host_run_meta(db: Database, run_id: str, meta: Mapping[str, Any]) -> None:
    """Persist host run metadata for MCP process restarts."""
    payload = dict(meta)
    try:
        db.log_swarm_event(run_id, _HOST_HANDOFF_EVENT, payload)
    except Exception:
        log.debug("host handoff meta event failed for %s", run_id, exc_info=True)
    try:
        summary = db.get_swarm_summary(run_id)
        counters: dict[str, Any] = {}
        if summary and isinstance(summary.get("progress_counters"), dict):
            counters = dict(summary["progress_counters"])
        counters["host_run_meta"] = payload
        db.persist_swarm_run(
            {
                "swarm_id": run_id,
                "progress_counters": counters,
            }
        )
    except Exception:
        log.debug("host handoff meta counters failed for %s", run_id, exc_info=True)


def _load_host_run_meta_from_db(db: Database, run_id: str) -> dict[str, Any]:
    """Load persisted host run metadata when in-memory state is missing."""
    try:
        events = db.get_swarm_events(run_id, event_type=_HOST_HANDOFF_EVENT, limit=1)
        if events:
            payload = events[0].get("payload")
            if isinstance(payload, dict):
                return dict(payload)
    except Exception:
        log.debug("host handoff event load failed for %s", run_id, exc_info=True)
    try:
        summary = db.get_swarm_summary(run_id)
        if summary and isinstance(summary.get("progress_counters"), dict):
            stored = summary["progress_counters"].get("host_run_meta")
            if isinstance(stored, dict):
                return dict(stored)
    except Exception:
        log.debug("host handoff counters load failed for %s", run_id, exc_info=True)
    return {}


def _reconcile_completed_waves(db: Database, run_id: str, planned_waves: set[int]) -> None:
    """Fold waves that wrote nothing into ``completed_waves`` at finalize.

    ``ingest_host_wave`` only ever runs for waves with captured run-log records
    — a read-only wave (a review synthesis agent, or any agent that touches no
    file) never triggers it, so ``completed_waves`` silently omitted it even
    though the handoff planned it and the host already reported it complete via
    this very call (``import_run_log`` only runs at terminal/outcome time, so
    every planned wave has, by definition, already executed). Records
    ``waves_planned`` alongside ``waves_captured`` so a real mismatch — a crash
    mid-run, not this — stays visible instead of both collapsing to one list.
    """
    if not planned_waves:
        return
    try:
        meta = _ensure_host_run_meta(db, run_id)
        captured = {int(w) for w in (meta.get("completed_waves") or [])}
        meta["waves_planned"] = sorted(planned_waves)
        meta["waves_captured"] = sorted(captured)
        if planned_waves - captured:
            meta["completed_waves"] = sorted(captured | planned_waves)
        _HOST_RUN_META[run_id] = meta
        _persist_host_run_meta(db, run_id, meta)
    except Exception:
        log.debug("completed_waves reconciliation failed for %s", run_id, exc_info=True)


def _ensure_host_run_meta(db: Database, run_id: str) -> dict[str, Any]:
    meta = _HOST_RUN_META.get(run_id)
    if meta:
        return meta
    loaded = _load_host_run_meta_from_db(db, run_id)
    if loaded:
        _HOST_RUN_META[run_id] = loaded
        return loaded
    return _HOST_RUN_META.setdefault(run_id, {})


def _normalize_path_key(path: object, base: str | None = None) -> str:
    """Canonical key for matching a touched file to a planned target_file.

    Handoff snapshots may hold workspace-relative paths while hook-captured
    records hold absolute ones, so both sides are resolved against the run's
    workspace root before comparison.
    """
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        if base and not os.path.isabs(raw):
            raw = os.path.join(base, raw)
        return os.path.realpath(raw)
    except Exception:  # pragma: no cover - defensive
        return raw


def _index_handoff_snapshots(
    run_id: str,
    snapshots: list[dict[str, object]],
    workspace_root: str | None = None,
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
    dict[tuple[int, int], dict[str, object]],
    dict[str, dict[str, object]],
]:
    by_task_id: dict[str, dict[str, object]] = {}
    by_spawn_id: dict[str, dict[str, object]] = {}
    by_wave_agent: dict[tuple[int, int], dict[str, object]] = {}
    by_target_file: dict[str, dict[str, object]] = {}
    for snap in snapshots:
        task_id = snap.get("task_id")
        if isinstance(task_id, str) and task_id.strip():
            by_task_id[task_id.strip()] = snap
        spawn_id = snap.get("spawn_id")
        if isinstance(spawn_id, str) and spawn_id.strip():
            by_spawn_id[spawn_id.strip()] = snap
        wave_raw = snap.get("wave")
        # Prefer the per-wave agent_index: worker_index is a run-global counter,
        # so on wave 2+ it never equals the caller's per-wave agent position.
        worker_raw = snap.get("agent_index")
        if worker_raw is None:
            worker_raw = snap.get("worker_index")
        try:
            wave_num = int(wave_raw) if wave_raw is not None else 0
            worker_num = int(worker_raw) if worker_raw is not None else 0
        except (TypeError, ValueError):
            continue
        if wave_num > 0:
            by_wave_agent[(wave_num, worker_num)] = snap
        # Path index: the only key a PostToolUse-captured edit can be matched on,
        # since the hook has no spawn_id/task_id to report. On collision keep the
        # earliest wave — the agent that wrote the file, not a later fixer.
        targets = snap.get("target_files")
        if isinstance(targets, list):
            for target in targets:
                key = _normalize_path_key(target, workspace_root)
                if not key:
                    continue
                prior = by_target_file.get(key)
                if prior is None:
                    by_target_file[key] = snap
                    continue
                try:
                    prior_wave = int(prior.get("wave") or 0)
                except (TypeError, ValueError):
                    prior_wave = 0
                if 0 < wave_num < prior_wave or prior_wave == 0:
                    by_target_file[key] = snap
        # Review agents are read-only w.r.t. their target file — their only
        # Write hits their own findings artifact. Index that path too, or the
        # PostToolUse hook (which only ever sees the artifact write) can never
        # resolve a review agent's hook record back to this snapshot.
        subagent_type = str(snap.get("subagent_type") or "")
        if subagent_type in _REVIEW_SUBAGENT_TO_DIM and isinstance(spawn_id, str) and spawn_id.strip():
            try:
                from .findings_merge import findings_path

                art_key = _normalize_path_key(
                    str(findings_path(run_id, spawn_id.strip())), workspace_root
                )
            except Exception:
                art_key = ""
            if art_key:
                by_target_file.setdefault(art_key, snap)
    return by_task_id, by_spawn_id, by_wave_agent, by_target_file


def _agent_touched_files(agent: Mapping[str, Any]) -> list[str]:
    """Files an agent reported writing, across report and hook payload shapes."""
    out: list[str] = []
    for key in ("touched_files", "target_files"):
        raw = agent.get(key)
        if isinstance(raw, list):
            out.extend(str(p) for p in raw if str(p).strip())
        elif isinstance(raw, str) and raw.strip():
            out.append(raw.strip())
    single = agent.get("target_file")
    if isinstance(single, str) and single.strip():
        out.append(single.strip())
    return out


def _enrich_agent_from_handoff(
    agent: Mapping[str, Any],
    *,
    snapshots_by_task_id: Mapping[str, Mapping[str, object]],
    snapshots_by_spawn_id: Mapping[str, Mapping[str, object]],
    snapshots_by_wave_agent: Mapping[tuple[int, int], Mapping[str, object]],
    wave_index: int,
    agent_index: int,
    snapshots_by_target_file: Mapping[str, Mapping[str, object]] | None = None,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    """Merge handoff snapshot fields into a wave report agent payload."""
    merged: dict[str, Any] = dict(agent)
    snap: Mapping[str, object] | None = None
    task_id_raw = agent.get("task_id")
    if isinstance(task_id_raw, str) and task_id_raw.strip():
        snap = snapshots_by_task_id.get(task_id_raw.strip())
    if snap is None:
        spawn_raw = agent.get("spawn_id") or agent.get("id")
        if isinstance(spawn_raw, str) and spawn_raw.strip():
            snap = snapshots_by_spawn_id.get(spawn_raw.strip())
    if snap is None and snapshots_by_target_file:
        # Path match comes BEFORE the positional fallback: a hook-captured record
        # carries no spawn_id/task_id and lands on a synthetic wave/index, so
        # (wave_index, agent_index) would match some unrelated planned agent.
        for touched in _agent_touched_files(agent):
            candidate = snapshots_by_target_file.get(
                _normalize_path_key(touched, workspace_root)
            )
            if candidate is not None:
                snap = candidate
                break
    if snap is None:
        snap = snapshots_by_wave_agent.get((wave_index, agent_index))
    if snap is None:
        return merged
    for key in ("prompt", "tier", "model", "task_id", "spawn_id", "subagent_type", "role"):
        if not merged.get(key) and snap.get(key):
            merged[key] = snap[key]
    if not merged.get("description") and snap.get("prompt"):
        merged["description"] = snap["prompt"]
    target_files = merged.get("target_files")
    snap_targets = snap.get("target_files")
    if not target_files and isinstance(snap_targets, list):
        merged["target_files"] = list(snap_targets)
        target_files = snap_targets
    if not merged.get("target_file") and isinstance(target_files, list) and target_files:
        merged["target_file"] = target_files[0]
    return merged


def register_host_run_handoff(
    db: Database,
    *,
    run_id: str,
    host_spawn_waves: list[dict[str, Any]],
    planned_subtasks: int,
    workspace_root: str | None = None,
    project_id: str | None = None,
    topology: str | None = None,
    task_hint: str | None = None,
    hybrid_split: Mapping[str, Any] | None = None,
) -> None:
    """Persist handoff metadata and per-agent telemetry stubs.

    ``hybrid_split`` is the diagnose->implement descriptor from
    ``heuristic_plan.apply_hybrid_split``; carrying it here is what lets finalize
    attribute the run's outcome back to the tier discount that was applied.
    """
    handoff_caller: str | None = None
    for wave in host_spawn_waves:
        if not isinstance(wave, dict):
            continue
        agents = wave.get("agents")
        if not isinstance(agents, list):
            continue
        for agent in agents:
            if isinstance(agent, dict) and isinstance(agent.get("caller"), str) and agent["caller"].strip():
                handoff_caller = agent["caller"].strip()
                break
        if handoff_caller:
            break

    existing = _HOST_RUN_META.get(run_id) or {}
    meta = {
        "planned_subtasks": max(0, int(planned_subtasks)),
        "workspace_root": workspace_root or existing.get("workspace_root"),
        "project_id": project_id or workspace_root or existing.get("project_id") or "default-project",
        "topology": topology or existing.get("topology") or "linear",
        "reported_agents": int(existing.get("reported_agents") or 0),
        "host_waves_completed": int(existing.get("host_waves_completed") or 0),
        "completed_waves": list(existing.get("completed_waves") or []),
        "assigned_files": list(existing.get("assigned_files") or []),
        "registered_ts": existing.get("registered_ts") or time.time(),
        "caller": handoff_caller or existing.get("caller"),
        "plan_revision": existing.get("plan_revision"),
        "next_subtask_id": existing.get("next_subtask_id"),
        "task_hint": task_hint or existing.get("task_hint"),
        "hybrid_split": (
            dict(hybrid_split) if isinstance(hybrid_split, Mapping)
            else existing.get("hybrid_split")
        ),
    }
    if int(planned_subtasks) > int(existing.get("planned_subtasks") or 0):
        meta["planned_subtasks"] = max(0, int(planned_subtasks))
    _HOST_RUN_META[run_id] = meta
    _persist_host_run_meta(db, run_id, meta)
    _wave_tracker(run_id)

    global_worker_index = 0
    try:
        snapshots = db.get_handoff_agent_snapshots(run_id)
        global_worker_index = len(snapshots)
    except Exception:
        log.debug("handoff snapshot count failed for %s", run_id, exc_info=True)
    for wave_idx, wave in enumerate(host_spawn_waves, start=1):
        if not isinstance(wave, dict):
            continue
        agents = wave.get("agents")
        if not isinstance(agents, list):
            continue
        for agent_index, agent in enumerate(agents):
            if not isinstance(agent, dict):
                continue
            spawn_id = str(agent.get("id") or agent_index)
            task_id = host_task_id(run_id, spawn_id)
            agent["task_id"] = task_id
            tier = str(agent.get("tier") or "medium")
            model = str(agent.get("model") or "host-native")
            target_files_raw = agent.get("target_files")
            target_files: list[str] = []
            if isinstance(target_files_raw, list):
                target_files = [str(p).strip() for p in target_files_raw if str(p).strip()]
            try:
                db.log_agent_result(
                    session_id=run_id,
                    task_hash=task_id,
                    agent_id=int(spawn_id) if str(spawn_id).isdigit() else agent_index,
                    tier=tier,
                    model=model,
                    success=True,
                    provider_name=str(agent.get("caller") or "host-native"),
                    reason="host_handoff_stub",
                    version="host_native",
                )
                snapshot = {
                    "spawn_id": spawn_id,
                    "task_id": task_id,
                    "tier": tier,
                    "model": model,
                    "prompt": agent.get("prompt"),
                    "target_files": target_files,
                    "subagent_type": str(agent.get("subagent_type") or "") or None,
                    "role": str(agent.get("role") or "") or None,
                    "wave": wave_idx,
                    "agent_index": agent_index,
                }
                # Journal before the DB write. This snapshot is the join key the
                # entire learning path depends on: a hook-captured run-log line
                # carries no model/tier/role/dimension of its own, and import
                # recovers all four by matching touched files against these. If
                # the DB is the thing that got corrupted, every surviving
                # wave.jsonl becomes unreplayable without this copy.
                from .learning_journal import KIND_HANDOFF_AGENT, append

                append(
                    KIND_HANDOFF_AGENT,
                    {
                        "run_id": run_id,
                        "spawn_id": spawn_id,
                        "task_id": task_id,
                        "agent_index": global_worker_index,
                        "snapshot": snapshot,
                    },
                )
                db.persist_worker_snapshot(
                    run_id,
                    worker_index=global_worker_index,
                    snapshot_json=snapshot,
                )
                global_worker_index += 1
            except Exception:
                log.debug("host handoff stub failed for %s", task_id, exc_info=True)
        # `spawn_batch` no longer exists (it duplicated `agents` verbatim and was
        # half the wire payload); mutations above land on `agents` directly, so
        # there is nothing left to re-sync. Older payloads may still carry the
        # key — drop it rather than leave a stale copy the host might spawn from.
        wave.pop("spawn_batch", None)


def record_consensus_handoff(
    db: Database,
    run_id: str,
    *,
    wave_index: int,
    personas: list[str],
    queen_tier: str,
) -> None:
    """Record host-native consensus-wave metadata so ingest can recognise it."""
    meta = _ensure_host_run_meta(db, run_id)
    meta["consensus_wave_index"] = int(wave_index)
    meta["consensus_personas"] = [str(p) for p in personas if p]
    meta["consensus_queen_tier"] = str(queen_tier or "low")
    _persist_host_run_meta(db, run_id, meta)


def _consensus_proposals_from_agents(
    agents: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Parse each consensus-queen agent's reported output into a proposal dict."""
    from .planner import _extract_json

    proposals: list[dict[str, Any]] = []
    for agent in agents:
        if not isinstance(agent, Mapping):
            continue
        spawn_id = str(agent.get("spawn_id") or agent.get("id") or "")
        persona = agent.get("persona") or persona_id_from_spawn_id(spawn_id)
        raw = str(agent.get("output_excerpt") or "").strip()
        decision: dict[str, Any] = {}
        if raw:
            try:
                parsed = _extract_json(raw)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                decision = dict(parsed)
        decision["persona"] = persona
        proposals.append(decision)
    return proposals


def _consensus_block(tally, *, judge_used: bool, resolved: bool) -> dict[str, Any]:
    winner = tally.winner or {}
    return {
        "resolved": resolved,
        "queens": tally.queens,
        "valid": tally.valid_count,
        "personas": list(tally.personas),
        "quorum": tally.quorum,
        "agreement": tally.agreement,
        "judge_used": judge_used,
        "winner_persona": tally.winner_persona,
        "verdict": winner.get("verdict"),
        "dominant_verdict": tally.dominant_verdict,
        "degraded": tally.degraded,
    }


def _process_consensus_report(
    db: Database,
    *,
    run_id: str,
    wave_index: int,
    agents: list[Mapping[str, Any]],
    meta: dict[str, Any],
    config: TGsConfig | None,
    terminal: bool,
) -> dict[str, Any] | None:
    """Handle a reported consensus or judge wave; mutate meta with the winner.

    Returns a response fragment with a ``consensus`` block, or a
    ``consensus_followup`` fragment requesting the host spawn the judge wave.
    Returns ``None`` when this wave is not a consensus wave.
    """
    consensus_wave = meta.get("consensus_wave_index")
    judge_wave = meta.get("consensus_judge_wave")

    # --- Judge round: resolve the pending proposals with the judge's pick. ---
    if judge_wave is not None and wave_index == int(judge_wave):
        pending = meta.get("consensus_pending")
        pending = list(pending) if isinstance(pending, list) else []
        judge_raw = ""
        for agent in agents:
            if isinstance(agent, Mapping) and str(agent.get("output_excerpt") or "").strip():
                judge_raw = str(agent.get("output_excerpt")).strip()
                break
        idx, judge_used = parse_judge_decision(judge_raw, pending)
        winner = pending[idx] if 0 <= idx < len(pending) else (pending[0] if pending else {})
        meta["consensus_winner_persona"] = winner.get("persona")
        meta["consensus_resolved"] = True
        meta["consensus_judge_used"] = judge_used
        meta["consensus_verdict"] = winner.get("verdict")
        meta.pop("consensus_pending", None)
        _persist_host_run_meta(db, run_id, meta)
        try:
            db.log_swarm_event(
                run_id,
                "consensus_vote",
                {
                    "queens": len(pending),
                    "valid": len(pending),
                    "judge_used": judge_used,
                    "selected_persona": winner.get("persona"),
                    "wave": wave_index,
                },
            )
        except Exception:
            log.debug("consensus judge vote log failed for %s", run_id, exc_info=True)
        return {
            "consensus": {
                "resolved": True,
                "judge_used": judge_used,
                "winner_persona": winner.get("persona"),
                "verdict": winner.get("verdict"),
                "personas": [str(p.get("persona")) for p in pending if p.get("persona")],
            }
        }

    # --- Queen round: tally the persona proposals. ---
    if consensus_wave is None or wave_index != int(consensus_wave):
        return None

    quorum = getattr(config, "consensus_quorum", 2) if config is not None else 2
    judge_enabled = getattr(config, "consensus_judge_enabled", True) if config is not None else True
    proposals = _consensus_proposals_from_agents(agents)
    tally = consensus_tally(proposals, quorum=quorum, queens=len(proposals))

    try:
        db.log_swarm_event(run_id, "consensus_vote", tally.event_payload(round=wave_index))
    except Exception:
        log.debug("consensus vote log failed for %s", run_id, exc_info=True)

    if tally.judge_needed and judge_enabled and not terminal:
        meta["consensus_judge_wave"] = wave_index + 1
        meta["consensus_pending"] = list(tally.valid)
        _persist_host_run_meta(db, run_id, meta)
        judge_prompt = build_judge_prompt(tally.valid)
        caller = str(meta.get("caller") or "claude-code")
        judge_spec = build_judge_spawn(
            config=config,
            caller=caller,
            task_text=str(meta.get("task_hint") or ""),
            judge_prompt=judge_prompt,
            wave_index=wave_index,
        ) if config is not None else None
        if judge_spec is not None:
            return {
                "consensus_followup": {
                    "reason": "no_quorum",
                    "expects_wave": wave_index + 1,
                    "host_spawn": judge_spec,
                    "execution_note": (
                        "No quorum among consensus queens. Spawn this single read-only "
                        "judge agent, then call report_host_wave again with wave="
                        f"{wave_index + 1} and the judge's JSON output as output_excerpt."
                    ),
                }
            }

    # Quorum / single / degraded / (judge needed but terminal or disabled) → resolve now.
    judge_used = False
    winner = tally.winner
    if winner is None and tally.valid:
        # judge needed but cannot run (terminal/disabled): deterministic fallback.
        complete = [p for p in tally.valid if p.get("verdict") == "complete"]
        winner = complete[0] if complete else tally.valid[0]
    winner = winner or {}
    meta["consensus_winner_persona"] = winner.get("persona") or tally.winner_persona
    meta["consensus_resolved"] = True
    meta["consensus_judge_used"] = judge_used
    meta["consensus_verdict"] = winner.get("verdict")
    _persist_host_run_meta(db, run_id, meta)
    return {"consensus": _consensus_block(tally, judge_used=judge_used, resolved=True)}


def record_consensus_learning(
    db: Database,
    run_id: str,
    *,
    outcome: str,
    meta: Mapping[str, Any],
    project_id: str | None,
    router: TaskRouter | None,
) -> None:
    """Feed the consensus winner into the existing bandit/outcome learning infra.

    Reuses the shadow-mode contextual bandit (``shared/bandit.py``): the winning
    persona is rewarded by the terminal outcome under a dedicated ``:persona:``
    arm namespace so it never pollutes the ``tier:provider`` routing arms.
    Approval-gated on ``router.is_learning_enabled``. Best-effort.
    """
    winner_persona = meta.get("consensus_winner_persona")
    if not winner_persona:
        return
    queen_tier = str(meta.get("consensus_queen_tier") or "low")
    personas = [str(p) for p in (meta.get("consensus_personas") or []) if p]
    success = outcome in {"accepted", "revised"}

    try:
        db.log_swarm_event(
            run_id,
            "consensus_outcome",
            {
                "winner_persona": winner_persona,
                "personas": personas,
                "judge_used": bool(meta.get("consensus_judge_used")),
                "outcome": outcome,
                "success": success,
            },
        )
    except Exception:
        log.debug("consensus_outcome log failed for %s", run_id, exc_info=True)

    if router is None or not project_id or not router.is_learning_enabled(project_id):
        return
    try:
        from .bandit import extract_task_features, get_bandit_policy

        features = extract_task_features(str(meta.get("task_hint") or ""), project_id)
        reward = 1.0 if success else 0.0
        arm_id = f"{queen_tier}:persona:{winner_persona}"
        get_bandit_policy(db).update(arm_id, features, reward)
    except Exception:
        log.debug("consensus bandit update failed for %s", run_id, exc_info=True)


_REVIEW_SUBAGENT_TO_DIM = {
    "review-security": "security",
    "review-logic": "logic",
    "review-edge-cases": "edge",
    "review-types": "types",
    "review-performance": "performance",
}


def _build_review_outcome(
    agent_spec: Mapping[str, Any], result: Mapping[str, Any], tier: str
) -> dict[str, Any] | None:
    """Extract a review-tier learning record from a read-only review agent.

    Returns None for non-review agents or when the host did not report findings,
    so the loop simply skips them. Pure — no DB access.
    """
    review_meta = result.get("review_meta")
    if not isinstance(review_meta, Mapping):
        return None
    dim = _REVIEW_SUBAGENT_TO_DIM.get(str(agent_spec.get("subagent_type") or ""))
    target_file = str(agent_spec.get("target_file") or "").strip()
    if not dim or not target_file:
        return None
    try:
        findings_total = int(review_meta.get("findings_total") or 0)
        findings_high = int(review_meta.get("findings_high") or 0)
    except (TypeError, ValueError):
        return None
    # Optional per-category (sub-dimension) breakdown for the granular quality
    # ledger. The host may report review_meta["categories"] as
    # {slug: {findings_total, findings_high, kept}}; absent → top-level dim only.
    categories: dict[str, dict[str, Any]] = {}
    raw_categories = review_meta.get("categories")
    if isinstance(raw_categories, Mapping):
        for slug, cat in raw_categories.items():
            if not isinstance(cat, Mapping):
                continue
            key = str(slug or "").strip().lower()
            if not key:
                continue
            try:
                categories[key] = {
                    "findings_total": int(cat.get("findings_total") or 0),
                    "findings_high": int(cat.get("findings_high") or 0),
                    "kept": bool(cat.get("kept", True)),
                }
            except (TypeError, ValueError):
                continue
    return {
        "target_file": target_file,
        "dimension": dim,
        "tier": tier,
        # Carried so the quality ledger can key a row to the exact agent that
        # produced it, not just to the run.
        "spawn_id": str(agent_spec.get("spawn_id") or ""),
        "model": str(agent_spec.get("model") or ""),
        "effort": (str(agent_spec.get("effort")).strip() or None) if agent_spec.get("effort") else None,
        "findings_total": findings_total,
        "findings_high": findings_high,
        "kept_by_synthesis": bool(review_meta.get("kept_by_synthesis", True)),
        "categories": categories,
        # Optional per-finding detail for prior-review memory. Absent → only counts
        # are persisted, which still enables the unchanged-revision skip but leaves
        # finding lifecycle state untouched (see review_memory.record_review_scan).
        "findings": review_meta.get("findings"),
    }


def _backfill_review_meta(
    run_id: str,
    spawn_id: str,
    agent_spec: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Derive ``review_meta`` from this agent's findings file when the host omitted it.

    Under the findings-file protocol a review agent replies with counts only, so the
    host has nothing to build ``review_meta`` from — without this, every review
    learning signal (``review_tier_bias``, prior-review memory, the quality ledger)
    would silently stop. Parsing the file here also yields the per-category
    breakdown, which is *more* than hosts usually report: the static-recall scorer
    skips scoring entirely when categories are absent.

    Returns *result* unchanged when the host already reported ``review_meta``, when
    this is not a review agent, or when no findings file exists.
    """
    if isinstance(result.get("review_meta"), Mapping):
        return result
    if not run_id or not spawn_id:
        return result
    if not str(agent_spec.get("subagent_type") or "") in _REVIEW_SUBAGENT_TO_DIM:
        return result
    try:
        from .findings_merge import findings_path, parse_findings_text, review_meta_for

        path = findings_path(run_id, spawn_id)
        if not path.is_file():
            return result
        findings = parse_findings_text(
            path.read_text(encoding="utf-8", errors="replace"), source=spawn_id
        )
        merged = dict(result)
        merged["review_meta"] = review_meta_for(findings)
        return merged
    except Exception:
        log.debug(
            "host_learning: review_meta backfill failed for %s/%s",
            run_id,
            spawn_id,
            exc_info=True,
        )
        return result


def _record_review_memory(
    db: Database,
    outcome: Mapping[str, Any],
    prof: Any,
) -> None:
    """Persist that this (file revision x dimension) was reviewed, plus findings.

    The content digest comes from the finalize-time scan, so a file edited between
    plan and finalize is recorded against the revision that was actually reviewed.
    Without a digest there is nothing safe to key a future skip on, so the write is
    dropped rather than guessed.
    """
    intel = getattr(prof, "intel", None)
    sha = getattr(intel, "content_sha", "") if intel is not None else ""
    if not sha:
        return
    try:
        from .review_memory import record_review_scan

        record_review_scan(
            db,
            path=str(outcome["target_file"]),
            content_sha=sha,
            dimension=str(outcome["dimension"]),
            tier=str(outcome["tier"]),
            findings_total=int(outcome["findings_total"]),
            findings_high=int(outcome["findings_high"]),
            findings=outcome.get("findings"),
            model=outcome.get("model"),
        )
    except Exception:  # pragma: no cover - best-effort persistence
        log.debug("review-memory capture failed", exc_info=True)


def _record_static_recall(
    db: Database,
    outcome: Mapping[str, Any],
    prof: Any,
) -> None:
    """Score this reviewer against the deterministic static pre-scan.

    The expectation is recomputed from the file at finalize time (``prof.intel``
    already carries the scan), so it never depends on the host echoing plan
    metadata back. Scoring is skipped unless the host reported a per-category
    breakdown or reported nothing at all: with findings but no categories there is
    no way to tell whether the static hits were among them, and guessing would
    punish a correct reviewer.
    """
    intel = getattr(prof, "intel", None)
    if intel is None:
        return
    try:
        from . import model_quality
        from .code_intel import expected_findings

        dimension = str(outcome["dimension"])
        expected = sorted({
            s.rule_id for s in expected_findings(intel.smells, dimension)
        })
        if not expected:
            return
        categories = outcome.get("categories")
        reported = list(categories) if isinstance(categories, Mapping) else []
        findings_total = int(outcome["findings_total"])
        if not reported and findings_total > 0:
            log.debug(
                "static-recall skipped for %s/%s: findings reported without categories",
                outcome.get("target_file"),
                dimension,
            )
            return
        model_quality.record_static_recall_score(
            db,
            model=outcome.get("model"),
            effort=outcome.get("effort"),
            dimension=dimension,
            expected_rules=expected,
            reported_categories=reported,
            findings_total=findings_total,
            task_hash=outcome.get("task_hash"),
            run_id=outcome.get("run_id"),
            # static_recall is the objective review source, so it is the one the
            # routing bias actually reads — it must carry the join axes or the
            # bias can only ever be keyed on the model as a whole.
            tier=str(outcome.get("tier") or "") or None,
            profile_key=_profile_key_for_outcome(outcome, prof),
            spawn_id=str(outcome.get("spawn_id") or "") or None,
        )
    except Exception:  # pragma: no cover - best-effort learning
        log.debug("model-quality static-recall capture failed", exc_info=True)


def _profile_key_for_outcome(outcome: Mapping[str, Any], prof: Any) -> str | None:
    """``ext|loc_bucket|density_bucket`` for this reviewed file, or None."""
    target = str(outcome.get("target_file") or "").strip()
    if not target or prof is None:
        return None
    try:
        from .review_fanout import profile_key_for

        return profile_key_for(prof, target)
    except Exception:  # pragma: no cover - best-effort
        log.debug("profile key resolution failed for %s", target, exc_info=True)
        return None


def _record_review_outcome(
    db: Database,
    outcome: Mapping[str, Any],
    config: TGsConfig | None = None,
) -> None:
    """EMA-update review_tier_bias AND record the granular quality ledger.

    The tier-bias EMA is unchanged. Additionally — when the model-quality ledger
    is enabled — one findings-based ledger event per reviewed dimension (and one
    per reported sub-dimension/category) attributes review precision to the model
    that ran, plus one objective static-recall event graded against the
    deterministic code_intel pre-scan. All are best-effort and never raise into
    finalize.
    """
    prof = None
    try:
        from .review_fanout import estimate_review_profile, profile_key_for
        from .review_learning import record_review_tier_outcome

        prof = estimate_review_profile(outcome["target_file"], db=db)
        profile_key = profile_key_for(prof, outcome["target_file"])
        record_review_tier_outcome(
            db,
            profile_key=profile_key,
            dimension=str(outcome["dimension"]),
            tier=str(outcome["tier"]),
            findings_high=int(outcome["findings_high"]),
            findings_total=int(outcome["findings_total"]),
            kept_by_synthesis=bool(outcome["kept_by_synthesis"]),
        )
    except Exception:  # pragma: no cover - best-effort learning
        log.debug("review-tier outcome capture failed", exc_info=True)

    if config is None or getattr(config, "review_memory_enabled", True):
        _record_review_memory(db, outcome, prof)

    mq_cfg = getattr(config, "model_quality", None) if config is not None else None
    ledger_on = mq_cfg is not None and getattr(mq_cfg, "enabled", True)
    if ledger_on and getattr(mq_cfg, "static_recall_enabled", True):
        _record_static_recall(db, outcome, prof)

    # Granular model-quality ledger (source='findings'). Gated + best-effort.
    if not (ledger_on and getattr(mq_cfg, "findings_enabled", True)):
        return
    try:
        from . import model_quality

        dimension = str(outcome["dimension"])
        model = outcome.get("model")
        effort = outcome.get("effort")
        task_hash = outcome.get("task_hash")
        run_id = outcome.get("run_id")
        # Top-level dimension score.
        model_quality.record_findings_score(
            db,
            model=model,
            effort=effort,
            dimension=dimension,
            findings_high=int(outcome["findings_high"]),
            findings_total=int(outcome["findings_total"]),
            kept_by_synthesis=bool(outcome["kept_by_synthesis"]),
            task_hash=task_hash,
            run_id=run_id,
            # The join axes. Without them the ledger knows which model scored
            # what, but not at which tier or on what shape of file — and
            # review_tier_bias holds the profile with no model, so the two
            # ledgers could never be joined to answer the actual question.
            tier=str(outcome.get("tier") or "") or None,
            profile_key=profile_key,
            spawn_id=str(outcome.get("spawn_id") or "") or None,
        )
        # Per-category sub-dimension scores (e.g. security -> sql-injection).
        categories = outcome.get("categories")
        if isinstance(categories, Mapping):
            for slug, cat in categories.items():
                if not isinstance(cat, Mapping):
                    continue
                model_quality.record_findings_score(
                    db,
                    model=model,
                    effort=effort,
                    dimension=dimension,
                    sub_dimension=str(slug),
                    findings_high=int(cat.get("findings_high") or 0),
                    findings_total=int(cat.get("findings_total") or 0),
                    kept_by_synthesis=bool(cat.get("kept", True)),
                    task_hash=task_hash,
                    run_id=run_id,
                    tier=str(outcome.get("tier") or "") or None,
                    profile_key=profile_key,
                    spawn_id=str(outcome.get("spawn_id") or "") or None,
                )
    except Exception:  # pragma: no cover - best-effort learning
        log.debug("model-quality findings capture failed", exc_info=True)


def build_host_agent_record(
    db: Database,
    *,
    run_id: str,
    agent_spec: Mapping[str, Any],
    result: Mapping[str, Any],
    project_id: str | None = None,
) -> dict[str, Any]:
    """Pure compute for one host agent completion — performs no DB writes.

    Derives the task id, pattern hash, eval quality, touched files, and the
    ready-to-write ``pattern_payload`` / ``telemetry_payload`` kwargs. Used by
    :func:`record_host_agent_result` (single, immediate writes) and by the
    buffered :func:`ingest_host_wave` loop (one batched flush per wave).
    """
    spawn_id = str(agent_spec.get("spawn_id") or agent_spec.get("id") or "")
    task_id = str(agent_spec.get("task_id") or host_task_id(run_id, spawn_id))
    description = str(
        agent_spec.get("description")
        or agent_spec.get("prompt")
        or f"host agent {spawn_id}"
    )
    tier = str(agent_spec.get("tier") or "medium")
    model = str(agent_spec.get("model") or "host-native")
    success = bool(result.get("success", True))
    output_excerpt = str(result.get("output_excerpt") or "")
    touched_files_raw = result.get("touched_files")
    touched_files: list[str] = []
    if isinstance(touched_files_raw, list):
        touched_files = [str(path).strip() for path in touched_files_raw if str(path).strip()]
    if not touched_files and output_excerpt:
        touched_files = sorted(_extract_file_paths(output_excerpt))

    rework_hint = bool(result.get("rework_detected", False))
    eval_quality = derive_learning_quality(
        success=success,
        escalated=False,
        rework_count=1 if rework_hint else 0,
        used_fallback=False,
        used_speculation=False,
        output=output_excerpt,
    )
    if success and output_excerpt.strip():
        outcome_summary = "completed"
    elif success:
        outcome_summary = "completed with no captured output"
    else:
        outcome_summary = "failed"

    example = structured_pattern_example(
        task=description,
        tier=tier,
        model=model,
        provider="host-native",
        touched_files=touched_files,
        outcome_summary=outcome_summary,
        quality_score=eval_quality,
    )
    # Prefer the builder's prompt-independent key when the spawn carried one. Prompt
    # wording varies with prompt-economy settings, and hashing it would mint a new
    # pattern for the same kind of work, orphaning accumulated rows.
    carried_hash = str(agent_spec.get("pattern_hash") or "").strip()
    ph = carried_hash or pattern_hash(description)
    resolved_project = project_id or _HOST_RUN_META.get(run_id, {}).get("project_id") or "default-project"

    result = _backfill_review_meta(run_id, spawn_id, agent_spec, result)
    review_outcome = _build_review_outcome(agent_spec, result, tier)
    if review_outcome is not None:
        review_outcome["run_id"] = run_id
        review_outcome["task_hash"] = ph

    role = str(agent_spec.get("role") or "").strip() or derive_role_from_task(description)

    return {
        "task_id": task_id,
        "pattern_hash": ph,
        "eval_quality": eval_quality,
        "touched_files": touched_files,
        "resolved_project": resolved_project,
        "review_outcome": review_outcome,
        "pattern_payload": {
            "pattern_hash": ph,
            "pattern_desc": description,
            "tier": tier,
            "example": example,
            "quality_score": eval_quality,
            "rework_detected": rework_hint,
        },
        "telemetry_payload": {
            "session_id": run_id,
            "task_hash": task_id,
            "agent_id": int(spawn_id) if spawn_id.isdigit() else 0,
            "tier": tier,
            "model": model,
            "success": success,
            "rework": rework_hint,
            "provider_name": "host-native",
            "reason": "host_agent_complete",
            "version": "host_native",
            "role": role,
            "timing_ms": int(result.get("duration_ms") or 0) if result.get("duration_ms") else None,
        },
    }


def record_host_agent_result(
    db: Database,
    *,
    run_id: str,
    agent_spec: Mapping[str, Any],
    result: Mapping[str, Any],
    project_id: str | None = None,
) -> dict[str, Any]:
    """Record one host agent completion into pattern tracking and telemetry."""
    rec = build_host_agent_record(
        db,
        run_id=run_id,
        agent_spec=agent_spec,
        result=result,
        project_id=project_id,
    )
    task_id = rec["task_id"]

    pattern_warning: str | None = None
    try:
        db.track_pattern(**rec["pattern_payload"])
        check_draft_ready(db, rec["resolved_project"], rec["pattern_hash"])
    except Exception as exc:
        pattern_warning = f"pattern_tracking:{exc}"
        log.warning("host pattern tracking failed for %s", task_id, exc_info=True)

    telemetry_warning: str | None = None
    try:
        db.log_agent_result(**rec["telemetry_payload"])
    except Exception as exc:
        telemetry_warning = f"telemetry:{exc}"
        log.debug("host agent telemetry update failed for %s", task_id, exc_info=True)

    meta = _HOST_RUN_META.setdefault(run_id, {})
    meta["reported_agents"] = int(meta.get("reported_agents") or 0) + 1

    result_payload: dict[str, Any] = {
        "task_id": task_id,
        "pattern_hash": rec["pattern_hash"],
        "eval_quality": rec["eval_quality"],
        "touched_files": rec["touched_files"],
    }
    warnings = [w for w in (pattern_warning, telemetry_warning) if w]
    if warnings:
        result_payload["warnings"] = warnings
    return result_payload


def ingest_host_wave(
    db: Database,
    *,
    run_id: str,
    wave_index: int,
    agents: list[Mapping[str, Any]],
    workspace_root: str | None = None,
    terminal: bool = False,
    outcome: str | None = None,
    config: TGsConfig | None = None,
    router: TaskRouter | None = None,
    expand_plan: bool = False,
    discovered_files: list[str] | None = None,
    defer_draft_ready: bool = False,
) -> dict[str, Any]:
    """Ingest one host-reported wave and optionally finalize the run.

    When *defer_draft_ready* is true the per-pattern ``check_draft_ready`` LLM
    calls are NOT run here; the ``(project, pattern_hash)`` pairs are accumulated
    into ``meta["pending_draft_hashes"]`` and drained off the hot path in
    ``finalize_host_swarm`` / the warm-path daemon. This keeps the only LLM cost
    out of any reporting call.
    """
    if wave_index < 1:
        raise ValueError("wave must be >= 1")
    meta = _ensure_host_run_meta(db, run_id)
    effective_root = _effective_workspace_root(workspace_root, meta)
    if workspace_root:
        meta["workspace_root"] = workspace_root
    elif effective_root:
        meta["workspace_root"] = effective_root
    project_id = str(meta.get("project_id") or effective_root or "default-project")
    handoff_caller = str(meta.get("caller") or "mcp")
    handoff_cwd = effective_root

    db.persist_swarm_run(
        {
            "swarm_id": run_id,
            "status": "running",
            "resume_status": "running",
        }
    )

    snapshots = db.get_handoff_agent_snapshots(run_id)
    by_task_id, by_spawn_id, by_wave_agent, by_target_file = _index_handoff_snapshots(
        run_id, snapshots, effective_root
    )

    tracker = _wave_tracker(run_id)
    wave_files: set[str] = set()
    content_before: dict[str, str] = {}
    content_after: dict[str, str] = {}
    agent_results: list[dict[str, Any]] = []
    wave_warnings: list[str] = []
    auto_excerpt_count = 0
    files_read = 0

    # Per-agent DB writes are buffered and flushed once per wave (one
    # transaction instead of 3×N auto-commits). See db.flush_host_wave_records.
    pattern_buffer: list[dict[str, Any]] = []
    telemetry_buffer: list[dict[str, Any]] = []
    routing_guard_buffer: list[dict[str, Any]] = []
    draft_projects_by_hash: dict[str, str] = {}
    processed_agents = 0

    for agent_index, agent in enumerate(agents):
        if not isinstance(agent, Mapping):
            continue
        enriched = _enrich_agent_from_handoff(
            agent,
            snapshots_by_task_id=by_task_id,
            snapshots_by_spawn_id=by_spawn_id,
            snapshots_by_wave_agent=by_wave_agent,
            wave_index=wave_index,
            agent_index=agent_index,
            snapshots_by_target_file=by_target_file,
            workspace_root=effective_root,
        )
        spawn_id = str(enriched.get("spawn_id") or enriched.get("id") or "")
        spec = {
            "spawn_id": spawn_id,
            "task_id": enriched.get("task_id") or host_task_id(run_id, spawn_id),
            "tier": enriched.get("tier"),
            "model": enriched.get("model"),
            "prompt": enriched.get("prompt"),
            "description": enriched.get("description") or enriched.get("prompt"),
            "subagent_type": enriched.get("subagent_type"),
            "target_file": enriched.get("target_file"),
            "role": enriched.get("role"),
        }
        touched_files_raw = enriched.get("touched_files")
        touched_files: list[str] = []
        if isinstance(touched_files_raw, list):
            touched_files = [str(path).strip() for path in touched_files_raw if str(path).strip()]
        output_excerpt = str(enriched.get("output_excerpt") or "").strip()
        success = bool(enriched.get("success", True))
        if not output_excerpt and success and touched_files:
            auto_excerpt = _auto_output_excerpt(touched_files, effective_root)
            if auto_excerpt:
                output_excerpt = auto_excerpt
                auto_excerpt_count += 1
        result_payload = {
            "success": success,
            "touched_files": touched_files,
            "output_excerpt": output_excerpt,
            "rework_detected": enriched.get("rework_detected", False),
            "duration_ms": enriched.get("duration_ms"),
            "review_meta": enriched.get("review_meta"),
        }
        rec = build_host_agent_record(
            db,
            run_id=run_id,
            agent_spec=spec,
            result=result_payload,
            project_id=project_id,
        )
        # Buffer the per-agent writes; they are flushed once after the loop.
        pattern_buffer.append(rec["pattern_payload"])
        telemetry_buffer.append(rec["telemetry_payload"])
        # Profile-keyed review-tier learning (read-only review agents). Best-effort.
        if rec.get("review_outcome"):
            _record_review_outcome(db, rec["review_outcome"], config)
        draft_projects_by_hash.setdefault(rec["pattern_hash"], rec["resolved_project"])
        processed_agents += 1
        recorded = {
            "task_id": rec["task_id"],
            "pattern_hash": rec["pattern_hash"],
            "eval_quality": rec["eval_quality"],
            "touched_files": rec["touched_files"],
        }
        agent_results.append(recorded)
        task_id = str(spec.get("task_id") or "")
        for path in recorded.get("touched_files") or []:
            if not isinstance(path, str) or not path.strip():
                continue
            routing_guard_buffer.append(
                {
                    "caller": handoff_caller,
                    "cwd": handoff_cwd,
                    "task_id": task_id,
                    "file_written": path.strip(),
                }
            )
        for path in recorded.get("touched_files") or []:
            if not isinstance(path, str) or not path.strip():
                continue
            norm_key = _normalize_touched_file_key(effective_root, path)
            wave_files.add(norm_key)
            resolved = _resolve_touched_path(effective_root, path)
            if resolved is None:
                continue
            # Use the mtime-keyed cache and the 2 MiB byte cap: under large
            # fan-out this avoids re-reading the same source per agent and
            # never pulls an oversized/generated file fully into RAM. Oversized
            # or unreadable files return None and are skipped (rework
            # classification degrades to EXTENSION for them — telemetry only).
            text = read_source_cached(resolved)
            if text is None:
                continue
            content_after[norm_key] = text
            files_read += 1

    # Flush all buffered per-agent writes for this wave in one transaction,
    # then run draft-readiness once per unique pattern hash (kept strictly
    # after the flush — check_draft_ready opens its own connection and reads
    # the now-committed counts).
    if pattern_buffer or telemetry_buffer or routing_guard_buffer:
        try:
            db.flush_host_wave_records(
                patterns=pattern_buffer,
                telemetry=telemetry_buffer,
                routing_guards=routing_guard_buffer,
            )
        except Exception as exc:
            wave_warnings.append(f"wave_flush:{exc}")
            log.warning("host wave flush failed for run %s wave %d", run_id, wave_index, exc_info=True)
    if defer_draft_ready:
        # Off-hot-path: stash pairs in meta; drained in finalize / warm-path.
        pending = meta.get("pending_draft_hashes")
        if not isinstance(pending, dict):
            pending = {}
        pending.update(draft_projects_by_hash)
        meta["pending_draft_hashes"] = pending
    else:
        for ph, proj in draft_projects_by_hash.items():
            try:
                check_draft_ready(db, proj, ph)
            except Exception as exc:
                wave_warnings.append(f"pattern_tracking:{exc}")
                log.warning("host draft-readiness check failed for %s", ph, exc_info=True)
    if processed_agents:
        meta["reported_agents"] = int(meta.get("reported_agents") or 0) + processed_agents

    if wave_index > 1:
        prev_files = tracker.wave_files.get(wave_index - 1, set())
        for path in wave_files & prev_files:
            before = tracker.snapshots_after.get(path, tracker.snapshots_before.get(path, ""))
            if before:
                content_before[path] = before

    tracker.record_wave(
        wave_index,
        wave_files,
        content_before=content_before or None,
        content_after=content_after or None,
    )
    rework_events: list[dict[str, Any]] = []
    if wave_index > 1:
        rework_events = tracker.detect_rework(wave_index, db=db, session_id=run_id)

    if effective_root:
        for path, after in content_after.items():
            before = content_before.get(path) or tracker.snapshots_before.get(path, "")
            if before and before != after:
                observe_host_style_edits(
                    db,
                    project_path=effective_root,
                    file_path=path,
                    original=before,
                    edited=after,
                )

    meta["host_waves_completed"] = wave_index
    completed = meta.get("completed_waves")
    if not isinstance(completed, list):
        completed = []
    if wave_index not in completed:
        completed.append(wave_index)
    meta["completed_waves"] = sorted(completed)
    assigned = meta.get("assigned_files")
    if not isinstance(assigned, list):
        assigned = []
    for path in wave_files:
        if path not in assigned:
            assigned.append(path)
    meta["assigned_files"] = assigned
    _persist_host_run_meta(db, run_id, meta)

    consensus_fragment: dict[str, Any] | None = None
    try:
        consensus_fragment = _process_consensus_report(
            db,
            run_id=run_id,
            wave_index=wave_index,
            agents=list(agents),
            meta=meta,
            config=config,
            terminal=terminal,
        )
    except Exception:
        log.debug("consensus processing failed for %s", run_id, exc_info=True)

    db.log_swarm_event(
        run_id,
        "wave_progress",
        {
            "wave": wave_index,
            "agent_count": len(agent_results),
            "rework_events": len(rework_events),
        },
    )
    db.log_swarm_event(
        run_id,
        "host_agent_complete",
        {"wave": wave_index, "agents": agent_results},
    )

    try:
        memory_refresh_swarm_state_from_db(run_id, db=db)
    except Exception:
        log.debug("swarm memory refresh failed for %s", run_id, exc_info=True)

    db.persist_swarm_run(
        {
            "swarm_id": run_id,
            "status": "running",
            "progress_counters": {
                "host_waves_completed": wave_index,
                "host_agents_reported": len(agent_results),
                "host_run_meta": dict(meta),
            },
            "resume_status": "running",
        }
    )

    response: dict[str, Any] = {
        "run_id": run_id,
        "wave": wave_index,
        "agents_recorded": len(agent_results),
        "rework_events": rework_events,
        "terminal": terminal,
    }
    if effective_root or auto_excerpt_count or files_read:
        response["learning_enrichment"] = {
            "workspace_root": effective_root,
            "auto_excerpt_count": auto_excerpt_count,
            "files_read": files_read,
        }
    if wave_warnings:
        response["warnings"] = wave_warnings
    if consensus_fragment:
        response.update(consensus_fragment)

    expansion_files = discovered_files
    if expand_plan and not expansion_files:
        expansion_files = sorted(wave_files)
    if expand_plan and expansion_files and config is not None and not terminal:
        from .host_plan_expand import expand_host_plan

        try:
            expansion = expand_host_plan(
                db,
                run_id=run_id,
                discovered_files=expansion_files,
                workspace_root=effective_root,
                config=config,
                reason="report_host_wave expand_plan",
            )
            response["plan_expansion"] = expansion
        except Exception as exc:
            response.setdefault("warnings", []).append(f"plan_expansion:{exc}")
            log.warning("plan expansion failed for %s", run_id, exc_info=True)

    awaiting_judge = bool(consensus_fragment and "consensus_followup" in consensus_fragment)
    if terminal and not awaiting_judge:
        if outcome is None:
            raise ValueError("outcome is required when terminal=true")
        response["finalize"] = finalize_host_swarm(
            db,
            run_id,
            outcome,
            config=config,
            router=router,
            workspace_root=effective_root,
            rework_events=rework_events,
        )
        _promote_verify_keys(response)
    return response


def _promote_verify_keys(response: dict[str, Any]) -> None:
    """Lift verify_report / verify_followup out of ``finalize`` to the top level.

    ``verify_followup`` is an instruction to the host to spawn one more agent, so
    it has to be visible where the host looks for actions rather than buried in the
    finalize sub-dict alongside bookkeeping.
    """
    finalize = response.get("finalize")
    if not isinstance(finalize, Mapping):
        return
    for key in ("verify_report", "verify_followup"):
        if key in finalize and key not in response:
            response[key] = finalize[key]


def import_run_log(
    db: Database,
    run_id: str,
    *,
    outcome: str,
    config: TGsConfig | None = None,
    router: TaskRouter | None = None,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    """Batch-import a run's JSONL worker-wave log into the DB, once, at terminal.

    Replays each captured wave through ``ingest_host_wave`` (with
    ``defer_draft_ready=True``) in wave order so cross-wave rework detection and
    the single batched ``flush_host_wave_records`` still happen — just off the
    per-wave hot path. The terminal wave triggers ``finalize_host_swarm``, which
    drains the deferred draft-readiness checks.

    Consensus waves are NOT in the log (they are processed live during the run),
    so they are never double-counted here. Idempotent: a run already marked
    imported is skipped, so the warm-path daemon can safely retry crashed
    terminals.
    """
    from . import run_log

    if run_log.is_imported(run_id):
        return {"already_imported": True, "run_id": run_id}

    records = run_log.read_run_log(run_id)

    # Hook-captured records carry no wave (the PostToolUse hook has no wave
    # context), so recover it from the planned agent that owns the touched file.
    # Without this every hook record collapses onto wave 1 and cross-wave rework
    # detection sees a single flat wave.
    by_target_file: dict[str, Mapping[str, object]] = {}
    planned_waves: set[int] = set()
    try:
        _, _, by_wave_agent, by_target_file = _index_handoff_snapshots(
            run_id, db.get_handoff_agent_snapshots(run_id), workspace_root
        )
        planned_waves = {wave for wave, _agent_idx in by_wave_agent}
    except Exception:
        log.debug("handoff path index unavailable for %s", run_id, exc_info=True)

    def _resolve_wave(rec: Mapping[str, Any]) -> int:
        try:
            w = int(rec.get("wave", 1))
        except (TypeError, ValueError):
            w = 1
        if w > 0:
            return w
        for touched in _agent_touched_files(rec):
            snap = by_target_file.get(_normalize_path_key(touched, workspace_root))
            if snap is None:
                continue
            try:
                snap_wave = int(snap.get("wave") or 0)
            except (TypeError, ValueError):
                continue
            if snap_wave > 0:
                return snap_wave
        return 1

    waves: dict[int, list[dict[str, Any]]] = {}
    for rec in records:
        if not isinstance(rec, Mapping):
            continue
        waves.setdefault(_resolve_wave(rec), []).append(dict(rec))

    ordered = sorted(waves)
    result: dict[str, Any] = {"run_id": run_id, "imported_waves": len(ordered)}

    if not ordered:
        # No captured worker records (e.g. capture=off, or all read-only):
        # still terminalize so the run is finalized exactly once.
        result["finalize"] = finalize_host_swarm(
            db,
            run_id,
            outcome,
            config=config,
            router=router,
            workspace_root=workspace_root,
        )
        _promote_verify_keys(result)
        _reconcile_completed_waves(db, run_id, planned_waves)
        run_log.mark_imported(run_id)
        return result

    last = ordered[-1]
    for w in ordered:
        wave_result = ingest_host_wave(
            db,
            run_id=run_id,
            wave_index=w,
            agents=waves[w],
            workspace_root=workspace_root,
            config=config,
            router=router,
            defer_draft_ready=True,
            terminal=(w == last),
            outcome=outcome if w == last else None,
        )
        if w == last:
            result["finalize"] = wave_result.get("finalize")
            result["rework_events"] = wave_result.get("rework_events", [])
            _promote_verify_keys(result)

    _reconcile_completed_waves(db, run_id, planned_waves)
    run_log.mark_imported(run_id)
    try:
        keep = config.host_native.runs_keep if config is not None else 20
        run_log.prune_runs(keep=keep)
    except Exception:
        log.debug("run_log prune failed", exc_info=True)
    return result


def _run_host_verify_gate(
    run_id: str,
    meta: Mapping[str, Any],
    *,
    config: TGsConfig | None,
    workspace_root: str | None,
    success: bool,
) -> dict[str, Any] | None:
    """Run the verify gate for a host-native run, in-process and token-free.

    No agent is spawned on the happy path: lint/type/test commands are ordinary
    subprocesses, and failures that were already present at the merge base are
    discarded. Only when *new* failures survive that diff does the caller get back
    a report carrying ``followup``, which the MCP layer turns into a single
    low-tier fix agent — the same lazy shape as ``consensus_followup``.

    Returns None when the gate is disabled, nothing was written, or the run
    already failed (a failed run has its own error path; piling a gate rejection
    on top adds no information).
    """
    gate_cfg = getattr(config, "verify_gate", None) if config is not None else None
    if gate_cfg is None or not getattr(gate_cfg, "enabled", False):
        return None
    if not workspace_root or not success:
        return None
    assigned = meta.get("assigned_files")
    if not assigned:
        return None
    try:
        from .verify import run_verify_gate

        report = run_verify_gate(
            gate_cfg,
            project_root=workspace_root,
            run_id=run_id,
        )
    except Exception:  # pragma: no cover - gate is best-effort
        log.debug("host verify gate failed for %s", run_id, exc_info=True)
        return None
    payload = report.to_dict()
    if report.new_failures:
        payload["followup"] = {
            "tier": "low",
            "read_only": False,
            "wave_kind": "verify_fix",
            "description": (
                "The verify gate found failures introduced by this run. Fix ONLY "
                "these, and do not touch anything unrelated:\n"
                + "\n".join(f"- {f}" for f in report.new_failures[:25])
                + "\n\nFailures that already existed on the merge base are excluded "
                "and must be left alone."
            ),
        }
        log.info(
            "verify gate: %d new failure(s) in run %s (%d pre-existing ignored)",
            len(report.new_failures),
            run_id,
            len(report.preexisting_failures),
        )
    return payload


def _record_verify_quality(
    db: Database,
    run_id: str,
    verify_report: Mapping[str, Any],
    *,
    config: TGsConfig | None,
    workspace_root: str | None = None,
) -> None:
    """Record the gate result as an objective quality event per model that wrote.

    This is ground truth, not a proxy: the code either compiled, type-checked, and
    passed its tests, or it did not. Score is 10 for a clean run and degrades with
    the number of newly introduced failures. Attributed to each model that actually
    wrote files in this run, read back from the run log.

    Raw run-log records are hook-captured under the default ``learning_capture:
    hook`` mode and so carry no model/tier/role of their own (the same gap fixed
    for review agents earlier) — each is enriched against the handoff snapshot by
    target file before being counted as a writer, exactly like ``import_run_log``.
    """
    mq_cfg = getattr(config, "model_quality", None) if config is not None else None
    if mq_cfg is None or not getattr(mq_cfg, "enabled", True):
        return
    new_failures = list(verify_report.get("new_failures") or [])
    # Without a baseline the pass/fail is not trustworthy enough to be called
    # ground truth, so it is left out of the objective ledger entirely.
    if not verify_report.get("baseline_used") and new_failures:
        return
    score = 10.0 if not new_failures else max(0.0, 10.0 - 2.5 * len(new_failures))
    try:
        from . import model_quality, run_log

        by_target_file: dict[str, Mapping[str, object]] = {}
        try:
            _, _, _, by_target_file = _index_handoff_snapshots(
                run_id, db.get_handoff_agent_snapshots(run_id), workspace_root
            )
        except Exception:
            log.debug("handoff path index unavailable for %s", run_id, exc_info=True)

        writers: dict[str, tuple[str | None, str | None]] = {}
        for rec in run_log.read_run_log(run_id):
            if not isinstance(rec, Mapping) or rec.get("read_only"):
                continue
            touched_files = _agent_touched_files(rec)
            if not touched_files:
                continue
            enriched = rec
            if not str(rec.get("model") or "").strip():
                snap: Mapping[str, object] | None = None
                for touched in touched_files:
                    snap = by_target_file.get(_normalize_path_key(touched, workspace_root))
                    if snap is not None:
                        break
                if snap is not None:
                    enriched = {**rec, **{k: v for k, v in snap.items() if not rec.get(k) and v}}
            model = str(enriched.get("model") or "").strip()
            if model:
                effort = (str(enriched.get("effort")).strip() or None) if enriched.get("effort") else None
                role = str(enriched.get("role") or "").strip() or None
                writers[model] = (effort, role)
        if not writers:
            writers = {model_quality.MODEL_UNRESOLVED: (None, None)}
        for model, (effort, role) in writers.items():
            model_quality.record_verify_gate_score(
                db,
                model=model,
                effort=effort,
                role=role,
                score_0_10=score,
                new_failure_count=len(new_failures),
                preexisting_count=len(verify_report.get("preexisting_failures") or []),
                run_id=run_id,
            )
    except Exception:  # pragma: no cover - best-effort learning
        log.debug("verify-gate quality capture failed", exc_info=True)


def _record_run_belief(
    db: Database,
    meta: Mapping[str, Any],
    *,
    project_id: str,
    success: bool,
    rework_events: list[dict[str, Any]] | None,
    verify_report: Mapping[str, Any] | None,
    config: TGsConfig | None,
) -> None:
    """Derive one repo-scoped belief from how this run went. Free, no LLM.

    A clean run records a ``pattern``; a failed, reworked, or verify-dirty run
    records a ``constraint``. The summary is built from the run's own task hint and
    touched files — deliberately not model-generated, so this costs nothing and
    cannot hallucinate a lesson that did not happen.
    """
    cfg = getattr(config, "beliefs", None) if config is not None else None
    if cfg is not None and not (
        getattr(cfg, "enabled", True) and getattr(cfg, "capture_enabled", True)
    ):
        return
    task_hint = " ".join(str(meta.get("task_hint") or "").split())
    if not task_hint or not project_id:
        return
    # finalize_host_swarm falls back to "default-project" when no workspace root was
    # resolved. Writing beliefs there would pool lessons from unrelated repos into
    # one bucket and then inject them everywhere, so skip rather than cross-contaminate.
    if project_id == "default-project":
        log.debug("belief capture skipped: no resolved project for run")
        return
    files = [str(p) for p in (meta.get("assigned_files") or [])][:5]
    file_note = f" (files: {', '.join(files)})" if files else ""

    new_failures = []
    if isinstance(verify_report, Mapping):
        new_failures = list(verify_report.get("new_failures") or [])
    reworked = bool(rework_events)

    if success and not reworked and not new_failures:
        kind = "pattern"
        summary = f"{task_hint}{file_note} — completed cleanly on the first pass."
    else:
        kind = "constraint"
        if new_failures:
            reason = f"left {len(new_failures)} new verify failure(s)"
        elif reworked:
            reason = f"needed {len(rework_events or [])} rework pass(es)"
        else:
            reason = "did not complete successfully"
        summary = f"{task_hint}{file_note} — {reason}; plan for more care here."
    try:
        from .beliefs import record_belief

        record_belief(
            kind=kind, summary=summary, project_id=project_id, paths=files, db=db
        )
    except Exception:  # pragma: no cover - best-effort learning
        log.debug("belief capture failed", exc_info=True)


def _record_hybrid_split_outcome(
    db: Database,
    meta: Mapping[str, Any],
    *,
    success: bool,
    rework_events: list[dict[str, Any]] | None,
    config: TGsConfig | None,
) -> None:
    """Feed one diagnose->implement run back into the learned tier discount.

    A run counts as clean only when it succeeded AND produced no rework: those are
    the two ways a too-aggressive discount shows up. ``verify_report`` is consulted
    when present so a run that left new lint/type/test failures is never counted as
    a success for the discount. No-op unless the plan actually applied a split.
    """
    split = meta.get("hybrid_split")
    if not isinstance(split, Mapping):
        return
    hybrid_cfg = getattr(config, "hybrid", None) if config is not None else None
    if hybrid_cfg is not None and not getattr(hybrid_cfg, "learning_enabled", True):
        return
    profile_key = str(split.get("profile_key") or "")
    if not profile_key:
        return
    verify = meta.get("verify_report")
    verify_clean = True
    if isinstance(verify, Mapping):
        verify_clean = not verify.get("new_failures")
    clean = bool(success) and not rework_events and verify_clean
    try:
        from .hybrid_learning import record_hybrid_outcome

        record_hybrid_outcome(
            db,
            profile_key=profile_key,
            delta=int(split.get("delta") or -1),
            clean=clean,
        )
    except Exception:  # pragma: no cover - learning is best-effort
        log.debug("hybrid split outcome capture failed", exc_info=True)


def finalize_host_swarm(
    db: Database,
    run_id: str,
    outcome: str,
    *,
    config: TGsConfig | None = None,
    router: TaskRouter | None = None,
    workspace_root: str | None = None,
    note: str | Mapping[str, object] | None = None,
    rework_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Terminalize a host-native run and fan out learning side-effects."""
    normalized_outcome = _normalize_outcome(outcome)
    meta = _ensure_host_run_meta(db, run_id)
    effective_root = _effective_workspace_root(workspace_root, meta)
    project_id = str(meta.get("project_id") or effective_root or "default-project")
    planned = int(meta.get("planned_subtasks") or 0)
    reported = int(meta.get("reported_agents") or 0)
    topology = str(meta.get("topology") or "linear")
    success = normalized_outcome in {"accepted", "revised"}
    finalize_warnings: list[str] = []

    # Drain deferred draft-readiness checks accumulated across waves (the only
    # LLM calls in the learning path). This runs at terminal — off the per-wave
    # hot path — and is backgrounded by the MCP terminal handler.
    pending_drafts = meta.get("pending_draft_hashes")
    if isinstance(pending_drafts, dict) and pending_drafts:
        for ph, proj in list(pending_drafts.items()):
            try:
                check_draft_ready(db, proj, ph)
            except Exception as exc:
                finalize_warnings.append(f"draft_ready:{exc}")
                log.debug("deferred draft-readiness failed for %s", ph, exc_info=True)
        meta["pending_draft_hashes"] = {}

    status = "completed" if success else "failed"
    db.persist_swarm_run(
        {
            "swarm_id": run_id,
            "status": status,
            "resume_status": status,
            "progress_counters": {
                "host_waves_completed": meta.get("host_waves_completed"),
                "host_agents_reported": reported,
                "host_run_meta": dict(meta),
            },
        }
    )
    db.log_swarm_event(
        run_id,
        "host_swarm_complete",
        {"outcome": normalized_outcome, "reported_agents": reported},
    )

    swarm_outcome: dict[str, Any] | None = None
    swarm_outcome_error: str | None = None
    try:
        swarm_outcome = record_swarm_outcome(
            db,
            run_id,
            normalized_outcome,
            selected_topology=topology,
            operator_id="host-native",
            note=note,
            project_id=project_id,
        )
    except Exception as exc:
        swarm_outcome_error = str(exc)
        finalize_warnings.append(f"swarm_outcome:{exc}")
        log.warning("record_swarm_outcome failed for %s", run_id, exc_info=True)

    if meta.get("consensus_winner_persona"):
        try:
            record_consensus_learning(
                db,
                run_id,
                outcome=normalized_outcome,
                meta=meta,
                project_id=project_id,
                router=router,
            )
        except Exception:
            log.debug("consensus learning failed for %s", run_id, exc_info=True)

    routing_learning_warning: str | None = None
    if router is not None and project_id and router.is_learning_enabled(project_id):
        try:
            was_correct = normalized_outcome in {"accepted", "revised"}
            tier = "medium"
            with db.conn() as conn:
                row = conn.execute(
                    "SELECT tier FROM telemetry WHERE session_id = ? ORDER BY ts DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
            if row and row[0]:
                tier = str(row[0])
            router.learn_project_routing(project_id, tier, was_correct=was_correct)
            hour = time.localtime().tm_hour
            router.learn_time_pattern(hour, was_quality_focused=was_correct)
        except Exception as exc:
            routing_learning_warning = f"routing_bias:{exc}"
            log.debug("routing bias learning failed for %s", run_id, exc_info=True)

    # Close the bandit loop. routing_decisions rows are keyed on
    # outcomes.route_task_id(task), so the run's task_hint must be resolved
    # through the same helper — passing run_id (as this did) matched nothing,
    # leaving outcome_score NULL on every row and the routing arms untrained.
    try:
        task_hint = str(meta.get("task_hint") or "").strip()
        if task_hint:
            from .outcomes import route_task_id

            db.update_routing_decision_outcome(
                route_task_id(task_hint),
                outcome_score=1.0 if success else 0.0,
                regret=0.0 if success else 1.0,
            )
    except Exception:
        log.debug("bandit outcome update skipped for %s", run_id, exc_info=True)

    # Cold-path training: replay newly scored decisions into the arm models.
    try:
        from .bandit import train_from_decisions

        train_from_decisions(db)
    except Exception:
        log.debug("bandit training skipped for %s", run_id, exc_info=True)

    if config is not None:
        try:
            cold_path_adjust(db, config)
        except Exception:
            log.debug("cold_path_adjust failed", exc_info=True)

    if effective_root and reported > 0:
        try:
            DecompositionPrefs(db).record_plan_interaction(
                effective_root,
                planned_count=max(planned, reported),
                actual_count=reported,
            )
        except Exception:
            log.debug("decomposition prefs record failed", exc_info=True)

    # Verify gate runs locally in Python — no agent, no tokens. Must precede the
    # hybrid outcome record so the discount is judged against the gate result.
    verify_report = _run_host_verify_gate(
        run_id, meta, config=config, workspace_root=effective_root, success=success
    )
    if verify_report is not None:
        meta["verify_report"] = verify_report
        _record_verify_quality(db, run_id, verify_report, config=config, workspace_root=effective_root)

    _record_hybrid_split_outcome(
        db, meta, success=success, rework_events=rework_events, config=config
    )
    _record_run_belief(
        db,
        meta,
        project_id=project_id,
        success=success,
        rework_events=rework_events,
        verify_report=verify_report,
        config=config,
    )

    if config is not None and rework_events:
        try:
            tracker = _HOST_WAVE_TRACKERS.get(run_id)
            if tracker is not None:
                evaluator = BackgroundEvaluator(db=db, config=config)
                evaluator.spawn_warm_path(tracker, rework_events)
        except Exception as exc:
            finalize_warnings.append(f"warm_path:{exc}")
            log.debug("warm path spawn failed for %s", run_id, exc_info=True)

    try:
        memory_refresh_swarm_state_from_db(run_id, db=db)
    except Exception:
        log.debug("final swarm memory refresh failed", exc_info=True)

    _HOST_WAVE_TRACKERS.pop(run_id, None)
    _HOST_RUN_META.pop(run_id, None)

    result: dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "outcome": normalized_outcome,
        "swarm_outcome": swarm_outcome,
        "reported_agents": reported,
    }
    if swarm_outcome_error:
        result["swarm_outcome_error"] = swarm_outcome_error
    review_report = _build_python_review_report(run_id)
    if review_report is not None:
        # A review run that used the findings-file protocol has no synthesis agent;
        # the ranked report is produced here instead, in the same shape the synthesis
        # prompt specified so consumers cannot tell the difference except by cost.
        result["review_report"] = review_report
    if verify_report is not None:
        result["verify_report"] = verify_report
        followup = verify_report.get("followup")
        if followup:
            # Lazy single-agent fix round, mirroring consensus_followup: the host
            # spawns it only because real new failures survived the baseline diff.
            result["verify_followup"] = followup
    all_warnings = list(finalize_warnings)
    if routing_learning_warning:
        all_warnings.append(routing_learning_warning)
    if all_warnings:
        result["warnings"] = all_warnings
    return result


def _build_python_review_report(run_id: str) -> dict[str, Any] | None:
    """Merge this run's findings files into one ranked report, in-process.

    Returns ``None`` when the run wrote no findings files — i.e. it was not a review
    run, or it ran with ``synthesis_mode=llm`` and a synthesis agent produced the
    report instead. Never raises: a merge failure must not fail the terminal report,
    and the individual findings files remain on disk either way.
    """
    if not run_id:
        return None
    try:
        from .findings_merge import merge, read_run_findings, render_report

        per_agent = read_run_findings(run_id)
        if not per_agent:
            return None
        flat = [f for findings in per_agent.values() for f in findings]
        merged = merge(flat)
        reviewed_files = sorted({f.path for f in flat})
        return {
            "source": "python",
            "agents": len(per_agent),
            "findings_total": merged.total,
            "duplicates_collapsed": len(merged.duplicates),
            "counts_by_severity": merged.counts_by_severity,
            "report": render_report(merged, reviewed_files=reviewed_files),
        }
    except Exception:
        log.debug("host_learning: python review merge failed for %s", run_id, exc_info=True)
        return None


def inspect_host_swarm(db: Database, run_id: str) -> dict[str, Any] | None:
    """Return swarm summary plus host-run metadata when present."""
    summary = db.get_swarm_summary(run_id)
    if summary is None:
        return None
    payload = dict(summary)
    meta = _HOST_RUN_META.get(run_id) or _load_host_run_meta_from_db(db, run_id)
    if meta:
        payload["host_run_meta"] = dict(meta)
        consensus = _consensus_inspect_section(db, meta)
        if consensus:
            payload["consensus"] = consensus
    return payload


def _consensus_inspect_section(
    db: Database,
    meta: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Assemble the consensus view for inspect from meta + learned persona stats."""
    if meta.get("consensus_wave_index") is None and not meta.get("consensus_personas"):
        return None
    section: dict[str, Any] = {
        "enabled": True,
        "personas": list(meta.get("consensus_personas") or []),
        "wave_index": meta.get("consensus_wave_index"),
        "resolved": bool(meta.get("consensus_resolved")),
        "winner_persona": meta.get("consensus_winner_persona"),
        "judge_used": bool(meta.get("consensus_judge_used")),
        "verdict": meta.get("consensus_verdict"),
    }
    try:
        from .bandit import get_bandit_policy

        learned = [
            arm
            for arm in get_bandit_policy(db).arm_stats()
            if ":persona:" in str(arm.get("arm_id", ""))
        ]
        if learned:
            section["learned_persona_stats"] = learned
    except Exception:
        log.debug("consensus persona stats unavailable", exc_info=True)
    return section


def observe_host_style_edits(
    db: Database,
    *,
    project_path: str,
    file_path: str,
    original: str,
    edited: str,
) -> None:
    """Best-effort style learning when before/after content is available."""
    if not original.strip() or not edited.strip() or original == edited:
        return
    try:
        StyleLearner(db).observe(project_path, original, edited)
    except Exception:
        log.debug("StyleLearner.observe failed for %s", file_path, exc_info=True)


__all__ = [
    "build_learning_report_contract",
    "finalize_host_swarm",
    "host_task_id",
    "ingest_host_wave",
    "inspect_host_swarm",
    "observe_host_style_edits",
    "plan_run_id",
    "record_host_agent_result",
    "register_host_run_handoff",
]
