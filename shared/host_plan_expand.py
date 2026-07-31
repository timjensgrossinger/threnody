"""Mid-run host-native plan expansion for discovered files."""
from __future__ import annotations

import logging
from typing import Any

from .config import TGsConfig
from .db import Database
from .heuristic_plan import (
    file_entries_from_paths,
    _is_integration_file,
    _pack_subtasks_to_cap,
    _tier_for_subtask,
)
from .host_learning import _HOST_RUN_META, _ensure_host_run_meta, register_host_run_handoff
from .host_spawn import build_host_spawn_waves, sanitize_plan_for_host
from .planner import build_waves, Subtask

log = logging.getLogger(__name__)


def _normalize_paths(paths: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = str(raw).strip().replace("\\", "/")
        if not path or path.lower() in seen:
            continue
        seen.add(path.lower())
        result.append(path)
    return result


def _assigned_files(meta: dict[str, Any], snapshots: list[dict[str, object]]) -> set[str]:
    assigned: set[str] = set()
    raw_assigned = meta.get("assigned_files")
    if isinstance(raw_assigned, list):
        for path in raw_assigned:
            if isinstance(path, str) and path.strip():
                assigned.add(path.strip().replace("\\", "/").lower())
    for snap in snapshots:
        targets = snap.get("target_files")
        if isinstance(targets, list):
            for path in targets:
                if isinstance(path, str) and path.strip():
                    assigned.add(path.strip().replace("\\", "/").lower())
    return assigned


def expand_host_plan(
    db: Database,
    *,
    run_id: str,
    discovered_files: list[str],
    workspace_root: str | None = None,
    config: TGsConfig,
    caller: str | None = None,
    reason: str = "host_plan_expand",
    descriptions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Append file-scoped subtasks and return pending host_spawn_waves."""
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        raise ValueError("run_id is required")

    summary = db.get_swarm_summary(normalized_run_id)
    if summary is None:
        raise ValueError(f"run_id {normalized_run_id!r} was not found")

    status = str(summary.get("status") or "")
    resume_status = str(summary.get("resume_status") or "")
    if status not in {"awaiting_host_execution", "running"} and resume_status not in {
        "awaiting_host_execution",
        "running",
    }:
        raise ValueError(
            f"run {normalized_run_id} is not expandable (status={status}, resume={resume_status})"
        )

    meta = _ensure_host_run_meta(db, normalized_run_id)
    if workspace_root:
        meta["workspace_root"] = workspace_root
    resolved_workspace = str(meta.get("workspace_root") or workspace_root or "")
    if not resolved_workspace:
        log.warning(
            "expansion of %s has no workspace root (neither argument nor run meta); "
            "discovered target paths cannot be containment-checked",
            normalized_run_id,
        )
    snapshots = db.get_handoff_agent_snapshots(normalized_run_id)
    assigned = _assigned_files(meta, snapshots)

    normalized_discovered = _normalize_paths(discovered_files)
    new_paths = [p for p in normalized_discovered if p.lower() not in assigned]
    if not new_paths:
        return {
            "expanded": False,
            "run_id": normalized_run_id,
            "reason": "no_new_files",
            "host_spawn_waves": [],
        }

    task_hint = str(meta.get("task_hint") or reason)
    entries = file_entries_from_paths(new_paths, task_hint=task_hint)
    if descriptions:
        entries = [
            (path, descriptions.get(path, hint) if descriptions.get(path) else hint)
            for path, hint in entries
        ]

    start_id = int(meta.get("next_subtask_id") or 0)
    if start_id < 1:
        max_spawn = 0
        for snap in snapshots:
            spawn_raw = snap.get("spawn_id")
            if isinstance(spawn_raw, str) and spawn_raw.isdigit():
                max_spawn = max(max_spawn, int(spawn_raw))
            elif isinstance(spawn_raw, int):
                max_spawn = max(max_spawn, spawn_raw)
        start_id = max_spawn + 1 if max_spawn > 0 else 1

    default_tier = "medium"
    subtasks: list[dict[str, object]] = []
    integration_ids: list[int] = []
    foundation_ids: list[int] = []
    for offset, (path, hint) in enumerate(entries):
        subtask_id = start_id + offset
        tier = _tier_for_subtask(file_count=len(entries), default_tier=default_tier)
        subtask: dict[str, object] = {
            "id": subtask_id,
            "description": hint,
            "tier": tier,
            "target_file": path,
            "single_file_insertion": False,
            "depends_on": [],
        }
        subtasks.append(subtask)
        if _is_integration_file(path):
            integration_ids.append(subtask_id)
        else:
            foundation_ids.append(subtask_id)

    if integration_ids and foundation_ids:
        foundation_set = set(foundation_ids)
        for subtask in subtasks:
            if int(subtask["id"]) in integration_ids:
                subtask["depends_on"] = sorted(foundation_set)

    # Bound one expansion the same way the initial plan is bounded — a host that
    # reports 60 discovered files must not turn into a 60-agent wave. Packing
    # merges instead of dropping, so every discovered file keeps an owner.
    plan_dict: dict[str, object] = {"subtasks": subtasks}
    cap = getattr(config, "swarm_max_agents", None)
    _pack_subtasks_to_cap(plan_dict, cap)
    packing = plan_dict.pop("packing", None)
    subtasks = list(plan_dict.get("subtasks") or [])
    if isinstance(packing, dict):
        # Packing renumbers from 1 and clears edges; re-offset onto this run's id
        # space so the appended subtasks never collide with the ones already run.
        for offset, st in enumerate(subtasks):
            st["id"] = start_id + offset
            st["depends_on"] = []
        log.info(
            "host plan expansion for %s packed %d file(s) into %d agent(s) (cap=%s)",
            normalized_run_id, len(entries), len(subtasks), cap,
        )

    # Same containment gate as the initial handoff: discovered paths come from a
    # model, so strip targets that escape the workspace root before they reach a
    # spawn manifest. Read-only expansion agents may still look outside it.
    sanitization = sanitize_plan_for_host(
        plan_dict,
        workspace_root=resolved_workspace or None,
        task=task_hint,
        default_tier=default_tier,
        # An expansion has nothing to collapse to: one agent over the original
        # task hint would re-do the run, so report no_safe_files instead.
        collapse_unsafe_to_single=False,
    )
    # Every expansion subtask exists to own a discovered file. One whose targets
    # were all stripped has nothing left to do, so drop it rather than spawn an
    # agent with no write scope.
    subtasks = [
        st
        for st in (plan_dict.get("subtasks") or [])
        if isinstance(st, dict) and _owned_paths([st])
    ]
    plan_dict["subtasks"] = subtasks
    if not subtasks:
        return {
            "expanded": False,
            "run_id": normalized_run_id,
            "reason": "no_safe_files",
            "discovered_files": new_paths,
            "deferred_files": new_paths,
            "sanitization": sanitization,
            "host_spawn_waves": [],
        }

    subtask_objs = [
        Subtask(
            id=int(st["id"]),
            description=str(st["description"]),
            tier=str(st.get("tier") or default_tier),
            model="",
            depends_on=list(st.get("depends_on") or []),
            target_file=str(st.get("target_file") or "") or None,
        )
        for st in subtasks
    ]
    wave_ids = build_waves(subtask_objs)
    plan_dict["waves"] = wave_ids
    plan_dict["topology"] = str(meta.get("topology") or "dag")
    host_waves = build_host_spawn_waves(
        plan_dict,
        config=config,
        caller=caller or str(meta.get("caller") or "mcp"),
    )
    start_wave = int(meta.get("host_waves_completed") or 0) + 1
    for wave in host_waves:
        if isinstance(wave, dict):
            wave["wave"] = start_wave
            start_wave += 1

    revision_number = int(meta.get("plan_revision") or 0) + 1
    diff_blob = {
        "discovered_files": new_paths,
        "subtasks": subtasks,
        "waves": wave_ids,
        "reason": reason,
    }
    try:
        db.insert_plan_revision(
            normalized_run_id,
            revision_number,
            diff_blob,
            proposer_id=str(caller or meta.get("caller") or "host-native"),
            reason=reason,
        )
    except Exception:
        log.debug("plan revision persist failed for %s", normalized_run_id, exc_info=True)

    meta["plan_revision"] = revision_number
    meta["next_subtask_id"] = start_id + len(subtasks)
    # Only the paths an agent actually owns count as assigned — a target the
    # containment gate stripped must stay claimable by a later expansion.
    owned_paths = _owned_paths(subtasks)
    deferred_paths = [p for p in new_paths if p.lower() not in {o.lower() for o in owned_paths}]
    for path in owned_paths:
        if path not in meta.get("assigned_files", []):
            assigned_list = list(meta.get("assigned_files") or [])
            assigned_list.append(path)
            meta["assigned_files"] = assigned_list
    meta["planned_subtasks"] = int(meta.get("planned_subtasks") or 0) + len(subtasks)
    _HOST_RUN_META[normalized_run_id] = meta

    register_host_run_handoff(
        db,
        run_id=normalized_run_id,
        host_spawn_waves=host_waves,
        planned_subtasks=int(meta.get("planned_subtasks") or len(subtasks)),
        workspace_root=resolved_workspace or None,
        project_id=str(meta.get("project_id") or resolved_workspace or "default-project"),
        topology=str(meta.get("topology") or "dag"),
        task_hint=task_hint,
    )

    result: dict[str, Any] = {
        "expanded": True,
        "run_id": normalized_run_id,
        "new_files": owned_paths,
        "discovered_files": new_paths,
        "host_spawn_waves": host_waves,
        "start_wave": int(meta.get("host_waves_completed") or 0) + 1,
        "plan_revision": revision_number,
        "execution_contract": "spawn_subagents",
    }
    if deferred_paths:
        result["deferred_files"] = deferred_paths
    if isinstance(packing, dict):
        result["packing"] = packing
    if any(sanitization.get(key) for key in ("dropped_targets", "dropped_subtasks", "dedup")):
        result["sanitization"] = sanitization
    return result


def _owned_paths(subtasks: list[dict[str, Any]]) -> list[str]:
    """Write targets that survived packing and containment, order-preserving."""
    paths: list[str] = []
    seen: set[str] = set()
    for st in subtasks:
        candidates: list[object] = []
        raw_multi = st.get("target_files")
        if isinstance(raw_multi, list):
            candidates.extend(raw_multi)
        candidates.append(st.get("target_file"))
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            path = candidate.strip().replace("\\", "/")
            if path.lower() in seen:
                continue
            seen.add(path.lower())
            paths.append(path)
    return paths


__all__ = ["expand_host_plan"]
