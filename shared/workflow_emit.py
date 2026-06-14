"""Render an ExecutionPlan into a Claude Code Dynamic Workflow JS script.

Claude Code's Workflow tool runs a JavaScript script that orchestrates subagents
deterministically. By default *every* agent in a workflow uses the session model;
this renderer populates the per-agent ``model`` option from Threnody's per-subtask
tier routing, so a Threnody-emitted workflow is tier-aware (low→haiku, medium→sonnet,
high→opus, or whatever the caller's profile maps).

Scope: claude-code only, opt-in. The emitter is additive — it never replaces the
``host_spawn_waves`` contract; callers choose one or the other (see mcp_server).

Faithfulness to host_spawn_waves semantics:

* Waves are barriers. Each wave is emitted as a ``parallel([...])`` (or a single
  ``await agent`` when the wave has one subtask), awaited before the next wave —
  identical ordering to ``build_host_spawn_waves``.
* A subtask with ``depends_on`` has its dependency results injected into its prompt
  at runtime (``JSON.stringify`` of the upstream result variables). This is required
  because workflow intermediate results live in *script variables*, not in the
  spawned agent's context window — unlike the host_spawn path where the synthesis
  agent reads prior ``output_excerpt`` from its own context.
* Each ``agent()`` call carries a ``schema`` so it returns a structured per-agent
  result; the final ``return`` collects them into an array consumed by the Phase 3
  telemetry bridge (``report_workflow_result``).

Determinism constraints the Workflow runtime enforces are respected: the rendered
script never emits ``Date.now()`` / ``Math.random()`` / argless ``new Date()``, and
``meta`` is a pure literal.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

from .config import TGsConfig
from .consensus import (
    build_queen_prompt,
    consensus_review_instruction,
    select_personas,
)
from .host_spawn import host_native_model_for_tier

log = logging.getLogger(__name__)

# Workflow runtime caps (mirrored from the Workflow tool contract so the renderer
# can warn rather than emit a script the runtime will reject).
MAX_CONCURRENT_AGENTS = 16
MAX_TOTAL_AGENTS = 1000

_REVIEW_SENTINEL = "REVIEW:"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _js_str(value: Any) -> str:
    """Encode a Python value as a valid JS string literal.

    ``json.dumps`` produces a double-quoted, fully-escaped literal that is valid
    JavaScript for any string — including newlines and quotes in agent prompts.
    """
    return json.dumps("" if value is None else str(value))


def _js_literal(value: Any) -> str:
    """Encode a JSON-serialisable Python value as a JS literal (objects/arrays)."""
    return json.dumps(value, ensure_ascii=False)


def workflow_slug(task_text: str | None, *, fallback: str = "threnody-workflow") -> str:
    """Derive a stable, lowercase-kebab workflow name from the task text.

    Deterministic (no randomness) — required because the runtime forbids
    ``Math.random()`` and resume journaling keys off the script.
    """
    base = (task_text or "").strip()
    if base.upper().startswith(_REVIEW_SENTINEL):
        base = base[len(_REVIEW_SENTINEL):].strip()
        prefix = "review-"
    else:
        prefix = ""
    slug = _SLUG_RE.sub("-", base.lower()).strip("-")
    slug = "-".join(slug.split("-")[:6])  # cap length; keep readable
    if not slug:
        return fallback
    return f"{prefix}{slug}"[:64].strip("-") or fallback


def _resolve_model(
    config: TGsConfig,
    caller: str | None,
    subtask: Mapping[str, Any],
    tier: str,
    registry: Any | None,
) -> str | None:
    """Resolve the tier→model for a subtask.

    Host callers always route through ``host_native_model_for_tier`` (the workflow
    runs inside the host shell). An explicit subtask ``model`` only wins for the
    non-host/utility case, mirroring ``build_host_spawn_waves``.
    """
    host_model = host_native_model_for_tier(config, caller, tier, registry=registry)
    if host_model:
        return host_model
    raw_model = subtask.get("model")
    if isinstance(raw_model, str) and raw_model.strip():
        return raw_model.strip()
    return None


def _agent_opts(
    *,
    label: str,
    phase: str,
    model: str | None,
    subagent_type: str | None,
    read_only: bool,
) -> str:
    """Build the JS options object literal for an ``agent()`` call."""
    parts = [
        f"label: {_js_str(label)}",
        f"phase: {_js_str(phase)}",
        "schema: RESULT_SCHEMA",
    ]
    if model:
        parts.append(f"model: {_js_str(model)}")
    # Named review subagent types are claude-code first-class agent types.
    if read_only and subagent_type and subagent_type.strip():
        parts.append(f"agentType: {_js_str(subagent_type.strip())}")
    return "{" + ", ".join(parts) + "}"


def _agent_prompt_expr(
    subtask: Mapping[str, Any],
    *,
    read_only: bool,
    dep_vars: list[str],
) -> str:
    """Build the JS prompt expression for an ``agent()`` call.

    A read-only subtask gets an explicit read-only instruction prepended. A subtask
    with dependencies gets the upstream results appended at runtime — necessary
    because workflow agents do not inherit prior agents' context.
    """
    prompt = str(subtask.get("description") or "").strip()
    if read_only:
        prompt = (
            "READ-ONLY: do not write or edit any file. Investigate and report only.\n\n"
            + prompt
        )
    expr = _js_str(prompt)
    if dep_vars:
        arr = "[" + ", ".join(dep_vars) + "]"
        expr = (
            f"{expr} + \"\\n\\nStructured results from prior stages:\\n\" + "
            f"JSON.stringify({arr}, null, 2)"
        )
    return expr


def render_workflow_script(
    plan_dict: Mapping[str, Any],
    *,
    config: TGsConfig,
    caller: str | None,
    registry: Any | None = None,
    task_text: str | None = None,
    name: str | None = None,
    include_consensus: bool = False,
) -> str:
    """Render ``plan_dict`` (subtasks + waves) into a Workflow JS script string.

    ``plan_dict`` is the same shape consumed by ``build_host_spawn_waves``:
    ``{"subtasks": [...], "waves": [[ids], ...], "analysis": str, "topology": str}``.

    When ``include_consensus`` is true, a final read-only persona-diverse queen phase is
    appended (the consensus_in_workflow opt-in). The queens return coordinator-style
    verdicts collected into ``__consensus``; the *decision* (quorum/judge) is tallied
    Python-side in ``report_workflow_result`` using ``shared/consensus.py`` — never in JS.

    Raises ``ValueError`` when the plan lacks the ``subtasks``/``waves`` structure.
    """
    subtasks = plan_dict.get("subtasks")
    waves = plan_dict.get("waves")
    if not isinstance(subtasks, list) or not isinstance(waves, list):
        raise ValueError("plan_dict must contain 'subtasks' and 'waves' lists")

    subtask_by_id: dict[Any, Mapping[str, Any]] = {}
    for raw in subtasks:
        if not isinstance(raw, Mapping):
            continue
        raw_id = raw.get("id")
        if raw_id is not None:
            subtask_by_id[raw_id] = raw

    analysis = str(plan_dict.get("analysis") or "Threnody-emitted workflow.").strip()
    topology = str(plan_dict.get("topology") or "dag")
    wf_name = name or workflow_slug(task_text)

    # ----- meta block (pure literal) -----------------------------------------
    phase_titles: list[str] = []
    for wave_idx, wave_ids in enumerate(waves, start=1):
        if not isinstance(wave_ids, list) or not wave_ids:
            continue
        # A lone synthesis subtask (has deps, others depend on nothing) reads nicer
        # as "Synthesis"; otherwise label by wave index.
        title = f"Wave {wave_idx}"
        if len(wave_ids) == 1:
            only = subtask_by_id.get(wave_ids[0])
            if isinstance(only, Mapping) and only.get("depends_on"):
                title = "Synthesis"
        phase_titles.append(title)

    # Consensus phase is rendered after the worker waves (opt-in). Build personas now
    # so the phase appears in meta and the cap math.
    personas = select_personas(getattr(config, "consensus_queens", 2), config) if include_consensus else []
    if include_consensus and personas:
        phase_titles.append("Consensus")

    meta_phases = [{"title": t} for t in phase_titles]
    meta_obj = {
        "name": wf_name,
        "description": (analysis[:240] or "Threnody-emitted workflow"),
        "phases": meta_phases,
    }

    total_agents = sum(
        len(w) for w in waves if isinstance(w, list)
    )
    over_total = total_agents > MAX_TOTAL_AGENTS
    widest = max((len(w) for w in waves if isinstance(w, list)), default=0)

    lines: list[str] = []
    lines.append("// Generated by Threnody (shared/workflow_emit.py) — do not hand-edit.")
    lines.append(
        f"// Tier-aware: each agent() routes to its tier model "
        f"(topology={topology}, agents={total_agents})."
    )
    if over_total:
        lines.append(
            f"// WARNING: {total_agents} agents exceeds the runtime cap "
            f"({MAX_TOTAL_AGENTS}); the runtime will reject this run."
        )
    if widest > MAX_CONCURRENT_AGENTS:
        lines.append(
            f"// NOTE: widest wave has {widest} agents; the runtime caps concurrency "
            f"at {MAX_CONCURRENT_AGENTS} — excess queues automatically."
        )
    lines.append("")
    lines.append(f"export const meta = {_js_literal(meta_obj)}")
    lines.append("")
    lines.append("// Structured per-agent result — forces each agent to return data we can")
    lines.append("// feed back to Threnody's learning loop (report_workflow_result).")
    lines.append(
        "const RESULT_SCHEMA = "
        + _js_literal(
            {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "findings": {"type": "array", "items": {"type": "string"}},
                    "success": {"type": "boolean"},
                },
                "required": ["summary", "success"],
                "additionalProperties": True,
            }
        )
    )
    lines.append("")
    lines.append("const __agents = []  // telemetry: one entry per spawned agent")
    lines.append("")

    # ----- wave-by-wave emission ---------------------------------------------
    phase_cursor = 0
    for wave_idx, wave_ids in enumerate(waves, start=1):
        if not isinstance(wave_ids, list) or not wave_ids:
            continue
        valid_ids = [sid for sid in wave_ids if sid in subtask_by_id]
        if not valid_ids:
            continue
        phase_title = phase_titles[phase_cursor] if phase_cursor < len(phase_titles) else f"Wave {wave_idx}"
        phase_cursor += 1
        lines.append(f"phase({_js_str(phase_title)})")

        for sid in valid_ids:
            subtask = subtask_by_id[sid]
            tier = str(subtask.get("tier") or "medium")
            model = _resolve_model(config, caller, subtask, tier, registry)
            read_only = bool(subtask.get("read_only", False))
            subagent_type = subtask.get("subagent_type")
            label = str(subtask.get("stable_id") or f"st-{sid}")
            dep_ids = subtask.get("depends_on") or []
            dep_vars = [f"r_{d}" for d in dep_ids if d in subtask_by_id]
            prompt_expr = _agent_prompt_expr(
                subtask, read_only=read_only, dep_vars=dep_vars
            )
            opts = _agent_opts(
                label=label,
                phase=phase_title,
                model=model,
                subagent_type=subagent_type if isinstance(subagent_type, str) else None,
                read_only=read_only,
            )
            meta_entry = _js_literal(
                {"id": str(sid), "label": label, "tier": tier, "model": model}
            )
            if len(valid_ids) == 1:
                # Single-agent wave — await directly.
                lines.append(f"const r_{sid} = await agent({prompt_expr}, {opts})")
                lines.append(f"__agents.push(Object.assign({meta_entry}, {{result: r_{sid}}}))")
            else:
                # Multi-agent wave — declare placeholder, fill via parallel below.
                lines.append(f"let r_{sid}")

        if len(valid_ids) > 1:
            thunks = []
            for sid in valid_ids:
                subtask = subtask_by_id[sid]
                tier = str(subtask.get("tier") or "medium")
                model = _resolve_model(config, caller, subtask, tier, registry)
                read_only = bool(subtask.get("read_only", False))
                subagent_type = subtask.get("subagent_type")
                label = str(subtask.get("stable_id") or f"st-{sid}")
                dep_ids = subtask.get("depends_on") or []
                dep_vars = [f"r_{d}" for d in dep_ids if d in subtask_by_id]
                prompt_expr = _agent_prompt_expr(
                    subtask, read_only=read_only, dep_vars=dep_vars
                )
                opts = _agent_opts(
                    label=label,
                    phase=phase_title,
                    model=model,
                    subagent_type=subagent_type if isinstance(subagent_type, str) else None,
                    read_only=read_only,
                )
                meta_entry = _js_literal(
                    {"id": str(sid), "label": label, "tier": tier, "model": model}
                )
                thunks.append(
                    f"  async () => {{ r_{sid} = await agent({prompt_expr}, {opts}); "
                    f"__agents.push(Object.assign({meta_entry}, {{result: r_{sid}}})); "
                    f"return r_{sid} }}"
                )
            lines.append("await parallel([")
            lines.append(",\n".join(thunks))
            lines.append("])")
        lines.append("")

    # ----- consensus phase (opt-in) ------------------------------------------
    emit_consensus = bool(include_consensus and personas)
    if emit_consensus:
        queen_tier = str(getattr(config, "consensus_queen_tier", "low") or "low")
        queen_model = host_native_model_for_tier(config, caller, queen_tier, registry=registry)
        review_instruction = consensus_review_instruction(task_text or "")
        lines.append("// Consensus phase — persona-diverse read-only review queens. Their")
        lines.append("// verdicts are tallied by report_workflow_result (shared/consensus.py),")
        lines.append("// never in JS. Diversity is by persona, all on the host model.")
        lines.append(
            "const CONSENSUS_SCHEMA = "
            + _js_literal(
                {
                    "type": "object",
                    "properties": {
                        "verdict": {"type": "string", "enum": ["complete", "another-pass"]},
                        "amendment": {"type": ["string", "null"]},
                        "next_work": {"type": ["object", "null"]},
                        "synthesis": {"type": "object"},
                    },
                    "required": ["verdict"],
                    "additionalProperties": True,
                }
            )
        )
        lines.append("const __consensus = []")
        lines.append('phase("Consensus")')
        queen_thunks: list[str] = []
        for persona in personas:
            pid = str(persona.get("id") or "queen")
            qprompt = build_queen_prompt(review_instruction, persona)
            qopts = _agent_opts(
                label=f"queen-{pid}",
                phase="Consensus",
                model=queen_model,
                subagent_type=None,
                read_only=True,
            )
            # Read-only instruction prepended; queens review, never write.
            qprompt_expr = (
                _js_str("READ-ONLY consensus review. Do not write or edit any file.\n\n" + qprompt)
            )
            qmeta = _js_literal(
                {"persona": pid, "wave_kind": "consensus", "tier": queen_tier, "model": queen_model}
            )
            queen_thunks.append(
                f"  async () => {{ const q = await agent({qprompt_expr}, {qopts}); "
                f"__consensus.push(Object.assign({qmeta}, {{result: q}})); return q }}"
            )
        lines.append("await parallel([")
        lines.append(",\n".join(queen_thunks))
        lines.append("])")
        lines.append("")

    lines.append("// Final return — Threnody ingests __agents via report_workflow_result.")
    if emit_consensus:
        lines.append("return { workflow: meta.name, agents: __agents, consensus: __consensus }")
    else:
        lines.append("return { workflow: meta.name, agents: __agents }")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "render_workflow_script",
    "workflow_slug",
    "MAX_CONCURRENT_AGENTS",
    "MAX_TOTAL_AGENTS",
]
