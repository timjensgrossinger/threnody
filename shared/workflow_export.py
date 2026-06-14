"""Learn and export permanent Claude Code workflows.

Phase 3 of the Dynamic Workflow integration. Threnody emits a workflow script per
fan-out run (see ``shared/workflow_emit.py``). When the *same orchestration shape*
recurs and runs succeed, that shape is worth keeping as a permanent ``/command``.

This module owns:

* ``workflow_shape_fingerprint`` — a stable hash of the *orchestration shape*
  (topology + per-wave agent counts / tiers / subagent types), NOT the prompt text.
  Two review-fanouts over different files share a fingerprint; that recurrence is
  what proves the shape is reusable.
* ``build_workflow_draft`` — package a script + shape into an approval-queue draft
  (``kind="workflow"``). Reuses the existing ``approval_queue`` table and gate —
  learning stays approval-gated, never auto-activated.
* ``export_workflow`` — write an *approved* draft's script to
  ``.claude/workflows/<slug>.js`` (project) or ``~/.claude/workflows/<slug>.js``
  (user), where Claude Code picks it up as ``/<slug>``.

The recurrence counter and draft enqueue are driven from ``report_workflow_result``
in ``mcp_server.py``; this module is pure persistence/packaging.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Mapping

# Reuse the hardened path/slug primitives from agent_export so workflow files get
# the same symlink-refusing, fsync'd write treatment.
from .agent_export import _safe_write, _slugify

log = logging.getLogger(__name__)

WORKFLOW_DRAFT_KIND = "workflow"
# Default recurrence before a shape is offered for approval ("spawn some, learn").
DEFAULT_WORKFLOW_PROMOTE_THRESHOLD = 2

_PROJECT_SUBDIR = ".claude/workflows"
_GLOBAL_DIR = Path.home() / ".claude" / "workflows"


def workflow_shape_fingerprint(plan_dict: Mapping[str, Any]) -> str:
    """Hash the orchestration *shape* of a plan, independent of prompt text.

    Captures: topology, and for each wave (in order) the agent count plus the
    sorted multiset of (tier, subagent_type). Prompts, file paths, and analysis
    text are deliberately excluded so the same shape over different inputs collides.
    """
    subtasks = plan_dict.get("subtasks")
    waves = plan_dict.get("waves")
    if not isinstance(subtasks, list) or not isinstance(waves, list):
        raise ValueError("plan_dict must contain 'subtasks' and 'waves' lists")

    by_id: dict[Any, Mapping[str, Any]] = {}
    for raw in subtasks:
        if isinstance(raw, Mapping):
            rid = raw.get("id")
            if rid is not None:
                by_id[rid] = raw

    wave_sigs: list[list[list[str]]] = []
    for wave_ids in waves:
        if not isinstance(wave_ids, list):
            continue
        cells: list[list[str]] = []
        for sid in wave_ids:
            st = by_id.get(sid)
            if not isinstance(st, Mapping):
                continue
            tier = str(st.get("tier") or "medium")
            sub = str(st.get("subagent_type") or "")
            read_only = bool(st.get("read_only", False))
            cells.append([tier, sub, "ro" if read_only else "rw"])
        wave_sigs.append(sorted(cells))

    shape = {
        "topology": str(plan_dict.get("topology") or "dag"),
        "waves": wave_sigs,
    }
    encoded = json.dumps(shape, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def build_workflow_draft(
    *,
    name: str,
    script: str,
    fingerprint: str,
    plan_dict: Mapping[str, Any],
    run_count: int,
    tier_models: Mapping[str, str] | None = None,
    personas: list[str] | None = None,
) -> dict[str, Any]:
    """Build an approval-queue draft dict for a learned workflow.

    The returned dict satisfies ``approval_queue_enqueue`` (requires ``name`` and
    ``fingerprint``) and carries the script under ``kind="workflow"`` so the export
    step can distinguish it from learned-agent drafts. ``tier_models`` (the baked
    tier→model map) and ``personas`` are stored so the export step can re-tune from
    learning and document the run without re-deriving them.
    """
    slug = _slugify(name)
    waves = plan_dict.get("waves") if isinstance(plan_dict.get("waves"), list) else []
    return {
        "kind": WORKFLOW_DRAFT_KIND,
        "name": slug,
        "fingerprint": f"workflow:{fingerprint}",
        "script": script,
        "shape": {
            "topology": str(plan_dict.get("topology") or "dag"),
            "wave_count": len(waves),
            "agent_count": sum(len(w) for w in waves if isinstance(w, list)),
        },
        "tier_models": dict(tier_models or {}),
        "personas": list(personas or []),
        "run_count": int(run_count),
        "summary": str(plan_dict.get("analysis") or "Learned Threnody workflow")[:240],
    }


def tune_models_from_learning(
    db: Any,
    tier_models: Mapping[str, str],
    *,
    min_samples: int = 3,
) -> dict[str, str]:
    """Return tier→model overrides where recorded outcomes favor a different model.

    Reads the global ``learning_stats`` snapshot (written by
    ``outcomes.compute_learning_outcome_snapshot``): a per-``"tier:model"`` outcome
    distribution. For each tier in ``tier_models`` it picks the model with the best
    accepted-ratio (>= ``min_samples`` graded runs). Returns only tiers whose best
    learned model differs from the baked one. Empty when no usable data — the baked
    script then ships unchanged.
    """
    try:
        from .memory import memory_get

        env = memory_get("global", "learning_stats", db=db)
        snapshot = env.get("value") if isinstance(env, dict) else None
    except Exception:
        log.debug("tune_models_from_learning: no learning_stats snapshot", exc_info=True)
        return {}
    if not isinstance(snapshot, dict):
        return {}
    dist = snapshot.get("outcome_distribution")
    if not isinstance(dist, dict):
        return {}

    # Group accepted-ratio by tier → {model: ratio}.
    by_tier: dict[str, dict[str, float]] = {}
    for key, counts in dist.items():
        if not isinstance(key, str) or ":" not in key or not isinstance(counts, dict):
            continue
        tier, _, model = key.partition(":")
        accepted = int(counts.get("accepted") or 0)
        total = sum(int(counts.get(k) or 0) for k in ("accepted", "revised", "rejected", "reworked"))
        if total < min_samples:
            continue
        by_tier.setdefault(tier, {})[model] = accepted / total if total else 0.0

    overrides: dict[str, str] = {}
    for tier, baked in tier_models.items():
        ranked = by_tier.get(tier)
        if not ranked:
            continue
        best_model = max(ranked, key=lambda m: ranked[m])
        if best_model and best_model != baked:
            overrides[tier] = best_model
    return overrides


def _apply_tuning_to_script(
    script: str,
    baseline_tier_models: Mapping[str, str],
    overrides: Mapping[str, str],
) -> str:
    """Swap baked per-tier model literals in the JS for learning-preferred ones.

    Per-tier model is uniform across that tier's agents (resolved once via
    ``host_native_model_for_tier``), so a literal swap of ``"model": "<baked>"`` →
    ``"model": "<tuned>"`` is exact. Only changed tiers are touched.
    """
    out = script
    for tier, new_model in overrides.items():
        baked = baseline_tier_models.get(tier)
        if not baked or baked == new_model:
            continue
        out = out.replace(f'"model": {_json_str(baked)}', f'"model": {_json_str(new_model)}')
    return out


def _json_str(value: str) -> str:
    return json.dumps(value)


def build_workflow_doc_header(
    draft: Mapping[str, Any],
    *,
    tuning: Mapping[str, str] | None = None,
    learned_agents: list[str] | None = None,
) -> str:
    """Build a rich JS comment header documenting the saved workflow.

    Documents intent, per-tier→model map (+ any learning re-tune), the consensus
    persona roster with each persona's role, run count, and related learned agents —
    so a coworker can read the file and run it with zero config.
    """
    from .consensus import QUEEN_PERSONAS

    name = str(draft.get("name") or "threnody-workflow")
    shape = draft.get("shape") if isinstance(draft.get("shape"), dict) else {}
    tier_models = draft.get("tier_models") if isinstance(draft.get("tier_models"), dict) else {}
    personas = draft.get("personas") if isinstance(draft.get("personas"), list) else []
    persona_roles = {p.get("id"): p.get("label") for p in QUEEN_PERSONAS}
    tuning = tuning or {}

    lines: list[str] = []
    lines.append(f"// ============================================================")
    lines.append(f"// Threnody learned workflow: /{name}")
    lines.append(f"// {str(draft.get('summary') or '').strip()[:200]}")
    lines.append(f"// Proven over {int(draft.get('run_count') or 0)} successful run(s). "
                 f"Topology={shape.get('topology', 'dag')}, agents={shape.get('agent_count', '?')}.")
    lines.append("// Zero-config: per-agent models are baked below. Just run /" + name + ".")
    lines.append("//")
    if tier_models:
        lines.append("// Per-tier model map (what model runs each task tier):")
        for tier in sorted(tier_models):
            baked = tier_models[tier]
            if tier in tuning and tuning[tier] != baked:
                lines.append(f"//   {tier:<6} → {tuning[tier]}  (re-tuned from learning; was {baked})")
            else:
                lines.append(f"//   {tier:<6} → {baked}")
    if personas:
        lines.append("//")
        lines.append("// Consensus personas (review stances — diversity by persona, not model):")
        for pid in personas:
            role = persona_roles.get(pid, pid)
            lines.append(f"//   {pid} — {role}")
    if learned_agents:
        lines.append("//")
        lines.append("// Related learned agents (approved specialists available in this project):")
        for agent_name in learned_agents[:10]:
            lines.append(f"//   {agent_name}")
    lines.append(f"// ============================================================")
    lines.append("")
    return "\n".join(lines)


def _resolve_workflow_path(root: Path, slug: str) -> Path:
    """Resolve and contain the workflow script path under ``root/.claude/workflows``."""
    target_dir = root.joinpath(_PROJECT_SUBDIR).resolve()
    try:
        target_dir.relative_to(root.resolve())
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"Export path escaped root: {target_dir}") from exc
    return target_dir / f"{slug}.js"


def export_workflow(
    draft: Mapping[str, Any],
    *,
    project_path: str | None = None,
    global_scope: bool = False,
    dry_run: bool = False,
    db: Any | None = None,
    tune: bool = True,
) -> dict[str, Any]:
    """Write an approved workflow draft's script to a saved-workflow file.

    When ``tune`` is true and ``db`` is provided, per-tier models are re-tuned from
    recorded learning outcomes and a documented header (tier→model map, persona roster,
    related learned agents) is prepended — producing a zero-config, pre-tuned, shareable
    ``/<slug>`` command. Returns ``{"written": [...], "skipped": [...], "errors": [...]}``.
    Raises ``ValueError`` when the draft is not a workflow draft or has no script.
    """
    if str(draft.get("kind")) != WORKFLOW_DRAFT_KIND:
        raise ValueError("not a workflow draft")
    script = draft.get("script")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("workflow draft has no script")
    slug = _slugify(str(draft.get("name") or "threnody-workflow"))

    tuning: dict[str, str] = {}
    if tune:
        tier_models = draft.get("tier_models") if isinstance(draft.get("tier_models"), dict) else {}
        if db is not None and tier_models:
            try:
                tuning = tune_models_from_learning(db, tier_models)
                if tuning:
                    script = _apply_tuning_to_script(script, tier_models, tuning)
            except Exception:
                log.warning("workflow export: tuning failed; shipping baked models", exc_info=True)
                tuning = {}
        learned_agents: list[str] = []
        if db is not None:
            try:
                rows = db.get_active_agents() or []
                learned_agents = [
                    str(r.get("name") or r.get("agent_id"))
                    for r in rows
                    if isinstance(r, dict) and (r.get("name") or r.get("agent_id"))
                ]
            except Exception:
                log.debug("workflow export: active agent lookup failed", exc_info=True)
        header = build_workflow_doc_header(draft, tuning=tuning, learned_agents=learned_agents)
        script = header + script

    if global_scope:
        target = (_GLOBAL_DIR / f"{slug}.js").resolve()
    else:
        root = Path(project_path or ".").resolve()
        target = _resolve_workflow_path(root, slug)

    result: dict[str, Any] = {"written": [], "skipped": [], "errors": [], "slug": slug}
    if dry_run:
        result["skipped"].append(str(target))
        return result
    try:
        _safe_write(target, script if script.endswith("\n") else script + "\n")
        result["written"].append(str(target))
    except OSError as exc:
        log.warning("workflow export failed for %s: %s", target, exc, exc_info=True)
        result["errors"].append(f"{target}: {exc}")
    return result


__all__ = [
    "WORKFLOW_DRAFT_KIND",
    "DEFAULT_WORKFLOW_PROMOTE_THRESHOLD",
    "workflow_shape_fingerprint",
    "build_workflow_draft",
    "export_workflow",
]
