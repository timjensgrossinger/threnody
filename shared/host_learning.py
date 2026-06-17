"""Host-native execution learning ingest — closes the feedback loop for swarms/plans."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Mapping

from .agents import check_draft_ready, derive_learning_quality, pattern_hash, structured_pattern_example
from .config import TGsConfig
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


def _ensure_host_run_meta(db: Database, run_id: str) -> dict[str, Any]:
    meta = _HOST_RUN_META.get(run_id)
    if meta:
        return meta
    loaded = _load_host_run_meta_from_db(db, run_id)
    if loaded:
        _HOST_RUN_META[run_id] = loaded
        return loaded
    return _HOST_RUN_META.setdefault(run_id, {})


def _index_handoff_snapshots(
    snapshots: list[dict[str, object]],
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]], dict[tuple[int, int], dict[str, object]]]:
    by_task_id: dict[str, dict[str, object]] = {}
    by_spawn_id: dict[str, dict[str, object]] = {}
    by_wave_agent: dict[tuple[int, int], dict[str, object]] = {}
    for snap in snapshots:
        task_id = snap.get("task_id")
        if isinstance(task_id, str) and task_id.strip():
            by_task_id[task_id.strip()] = snap
        spawn_id = snap.get("spawn_id")
        if isinstance(spawn_id, str) and spawn_id.strip():
            by_spawn_id[spawn_id.strip()] = snap
        wave_raw = snap.get("wave")
        worker_raw = snap.get("worker_index")
        try:
            wave_num = int(wave_raw) if wave_raw is not None else 0
            worker_num = int(worker_raw) if worker_raw is not None else int(snap.get("worker_index") or 0)
        except (TypeError, ValueError):
            continue
        if wave_num > 0:
            by_wave_agent[(wave_num, worker_num)] = snap
    return by_task_id, by_spawn_id, by_wave_agent


def _enrich_agent_from_handoff(
    agent: Mapping[str, Any],
    *,
    snapshots_by_task_id: Mapping[str, Mapping[str, object]],
    snapshots_by_spawn_id: Mapping[str, Mapping[str, object]],
    snapshots_by_wave_agent: Mapping[tuple[int, int], Mapping[str, object]],
    wave_index: int,
    agent_index: int,
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
    if snap is None:
        snap = snapshots_by_wave_agent.get((wave_index, agent_index))
    if snap is None:
        return merged
    for key in ("prompt", "tier", "model", "task_id"):
        if not merged.get(key) and snap.get(key):
            merged[key] = snap[key]
    if not merged.get("description") and snap.get("prompt"):
        merged["description"] = snap["prompt"]
    target_files = merged.get("target_files")
    snap_targets = snap.get("target_files")
    if not target_files and isinstance(snap_targets, list):
        merged["target_files"] = list(snap_targets)
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
) -> None:
    """Persist handoff metadata and per-agent telemetry stubs."""
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
                    "wave": wave_idx,
                    "agent_index": agent_index,
                }
                db.persist_worker_snapshot(
                    run_id,
                    worker_index=global_worker_index,
                    snapshot_json=snapshot,
                )
                global_worker_index += 1
            except Exception:
                log.debug("host handoff stub failed for %s", task_id, exc_info=True)
        if wave.get("spawn_batch") is not None:
            wave["spawn_batch"] = [
                dict(agent) if isinstance(agent, dict) else agent
                for agent in agents
            ]


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
    return {
        "target_file": target_file,
        "dimension": dim,
        "tier": tier,
        "findings_total": findings_total,
        "findings_high": findings_high,
        "kept_by_synthesis": bool(review_meta.get("kept_by_synthesis", True)),
    }


def _record_review_outcome(db: Database, outcome: Mapping[str, Any]) -> None:
    """Resolve the profile key and EMA-update review_tier_bias. Best-effort."""
    try:
        from .review_fanout import estimate_review_profile, profile_key_for
        from .review_learning import record_review_tier_outcome

        prof = estimate_review_profile(outcome["target_file"])
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
    ph = pattern_hash(description)
    resolved_project = project_id or _HOST_RUN_META.get(run_id, {}).get("project_id") or "default-project"

    review_outcome = _build_review_outcome(agent_spec, result, tier)

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
    by_task_id, by_spawn_id, by_wave_agent = _index_handoff_snapshots(snapshots)

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
        )
        spawn_id = str(enriched.get("spawn_id") or enriched.get("id") or "")
        spec = {
            "spawn_id": spawn_id,
            "task_id": enriched.get("task_id") or host_task_id(run_id, spawn_id),
            "tier": enriched.get("tier"),
            "model": enriched.get("model"),
            "prompt": enriched.get("prompt"),
            "description": enriched.get("description") or enriched.get("prompt"),
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
            _record_review_outcome(db, rec["review_outcome"])
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
    return response


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
    waves: dict[int, list[dict[str, Any]]] = {}
    for rec in records:
        if not isinstance(rec, Mapping):
            continue
        try:
            w = int(rec.get("wave", 1))
        except (TypeError, ValueError):
            w = 1
        waves.setdefault(max(1, w), []).append(dict(rec))

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

    run_log.mark_imported(run_id)
    try:
        keep = config.host_native.runs_keep if config is not None else 20
        run_log.prune_runs(keep=keep)
    except Exception:
        log.debug("run_log prune failed", exc_info=True)
    return result


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

    try:
        db.update_routing_decision_outcome(
            run_id,
            outcome_score=1.0 if success else 0.0,
            regret=0.0 if success else 1.0,
        )
    except Exception:
        log.debug("bandit outcome update skipped for %s", run_id, exc_info=True)

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
    all_warnings = list(finalize_warnings)
    if routing_learning_warning:
        all_warnings.append(routing_learning_warning)
    if all_warnings:
        result["warnings"] = all_warnings
    return result


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
