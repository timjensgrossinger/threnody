"""Operator receipts for routing, planning, and host-native runs."""
from __future__ import annotations

import html
import json
import logging
import time
from hashlib import sha256
from typing import Any, Mapping

from .db import Database

log = logging.getLogger(__name__)

_TIER_TOKEN_BUDGETS = {"low": 2000, "medium": 8000, "high": 20000}


def _derived_label(which: str, task: str) -> str:
    """Role (shared/roles.py) or task kind (shared/task_kinds.py) of ``task``; "" on failure."""
    try:
        if which == "role":
            from .roles import derive_role_from_task

            return derive_role_from_task(task) or ""
        from .task_kinds import derive_kind_from_task

        return derive_kind_from_task(task) or ""
    except Exception:
        log.debug("receipt: %s derivation failed", which, exc_info=True)
        return ""


def _model_price_known(model: str | None) -> bool:
    """Whether *model* has a real entry in the bundled price table.

    Distinct from "cost estimates to $0" — an unrecognized model and a
    genuinely free one both produce a $0.0 estimate from ``_estimate_model_cost``
    otherwise, and a savings figure computed from two unpriced numbers is not an
    estimate, it is a coincidence.
    """
    if not model:
        return False
    try:
        from .model_catalog import _load_price_data

        return model.lower() in _load_price_data()
    except Exception:
        return False


def _estimate_model_cost(model: str | None, *, tier: str, agents: int = 1) -> float:
    if not model:
        return 0.0
    try:
        from .model_catalog import _load_price_data

        prices = _load_price_data()
        info = prices.get(model.lower(), {})
        input_rate = float(info.get("input_cost_per_token") or 0.0)
        output_rate = float(info.get("output_cost_per_token") or 0.0)
    except Exception:
        input_rate = 0.0
        output_rate = 0.0
    budget = _TIER_TOKEN_BUDGETS.get(tier, 5000)
    input_tokens = int(budget * 0.75)
    output_tokens = budget - input_tokens
    return round(max(agents, 1) * (input_tokens * input_rate + output_tokens * output_rate), 6)


def _agent_count_from_payload(payload: Mapping[str, Any] | None, fallback: int = 1) -> int:
    """Prefer the actually-emitted ``host_spawn_waves`` over the planner's
    internal ``subtasks`` list — the two can diverge (sanitization dropping
    entries, an inline_files bucket, a max_agents cap applied after subtasks
    were built), and host_spawn_waves is what the host actually spawns. Using
    the pre-divergence ``subtasks`` count is what made a collapsed review plan's
    receipt report an agent_count nobody actually ran.
    """
    if not isinstance(payload, Mapping):
        return max(1, fallback)
    waves = payload.get("host_spawn_waves")
    if isinstance(waves, list):
        count = 0
        for wave in waves:
            if isinstance(wave, Mapping) and isinstance(wave.get("agents"), list):
                count += len(wave["agents"])
        if count:
            return count
    subtasks = payload.get("subtasks")
    if isinstance(subtasks, list) and subtasks:
        return len(subtasks)
    return max(1, fallback)


_TIER_ALIASES = {"low": "haiku", "medium": "sonnet", "high": "opus"}


def resolve_receipt_model(model: str | None, tier: str) -> tuple[str, str]:
    """Return ``(concrete_model_id, alias)`` for pricing/attribution.

    Host specs carry bare Claude Code aliases ("opus"); the price table is keyed
    by concrete ids. A missing or placeholder model ("host-native") falls back to
    the tier's alias. ``alias`` is "" when *model* was already concrete.
    """
    raw = (model or "").strip()
    if not raw or raw.lower() == "host-native":
        raw = _TIER_ALIASES.get(tier, "")
    if not raw:
        return "", ""
    try:
        from .model_registry import CLAUDE_ALIAS_TABLE, resolve_model_alias

        concrete, _source = resolve_model_alias(None, raw)
    except Exception:
        log.debug("receipt: alias resolution failed for %s", raw, exc_info=True)
        return raw, ""
    concrete = concrete or raw
    alias = raw if raw.casefold() in CLAUDE_ALIAS_TABLE else ""
    return concrete, alias


def _agent_specs_from_payload(payload: Mapping[str, Any] | None) -> list[tuple[str | None, str | None]]:
    """``(tier, model)`` for every agent in ``host_spawn_waves`` (empty if none)."""
    specs: list[tuple[str | None, str | None]] = []
    waves = payload.get("host_spawn_waves") if isinstance(payload, Mapping) else None
    if isinstance(waves, list):
        for wave in waves:
            agents = wave.get("agents") if isinstance(wave, Mapping) else None
            if isinstance(agents, list):
                for agent in agents:
                    if isinstance(agent, Mapping):
                        specs.append((agent.get("tier"), agent.get("model")))
                    else:
                        specs.append((None, None))
    return specs


def build_cost_receipt(
    *,
    source_tool: str,
    task: str,
    tier: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    payload: Mapping[str, Any] | None = None,
    estimated_cost_usd: float | None = None,
    rationale: str | None = None,
    skipped_calls: list[str] | None = None,
) -> dict[str, Any]:
    """Build a compact, response-safe savings receipt.

    Both sides are priced in USD by the same estimator (tier token budget x
    concrete model price, per agent), so they are always comparable. The
    counterfactual is the concrete model the HIGH tier resolves to on this host.

    ``savings.basis``: ``priced`` (real difference), ``same_model`` (selected is
    the counterfactual model: 0), ``no_savings`` (selected costs at least as much)
    or ``unpriced`` (a model is missing from the price table).

    ``estimated_cost_usd`` is accepted for compatibility but ignored: caller
    supplied figures (credits heuristics, 0.0 for host-native) were not comparable
    with the counterfactual. Host-native runs still consume model tokens on the
    user's subscription, flagged ``billing: "host_entitlement"`` rather than $0.
    """
    del estimated_cost_usd
    agent_count = _agent_count_from_payload(payload)
    resolved_tier = tier or "medium"

    specs = _agent_specs_from_payload(payload)
    if not specs:
        specs = [(resolved_tier, model)] * agent_count
    agents: list[tuple[str, str, str]] = []  # (tier, concrete model, alias)
    for spec_tier, spec_model in specs:
        agent_tier = str(spec_tier or resolved_tier)
        # A spec without its own model inherits the route-level one only when it
        # is the same tier; otherwise the tier's own alias applies.
        inherited = model if agent_tier == resolved_tier else None
        concrete, alias = resolve_receipt_model(str(spec_model or inherited or ""), agent_tier)
        agents.append((agent_tier, concrete, alias))

    selected_priced = all(_model_price_known(m) for _t, m, _a in agents)
    selected_cost: float | None = (
        round(sum(_estimate_model_cost(m, tier=t) for t, m, _a in agents), 6) if selected_priced else None
    )

    counterfactual_model, counterfactual_alias = resolve_receipt_model("opus", "high")
    counterfactual_priced = _model_price_known(counterfactual_model)
    high_counterfactual = (
        _estimate_model_cost(counterfactual_model, tier="high", agents=len(agents))
        if counterfactual_priced
        else None
    )

    savings_usd: float | None = None
    savings_pct: float | None = None
    if not (selected_priced and counterfactual_priced):
        savings_basis = "unpriced"
    elif all(m == counterfactual_model for _t, m, _a in agents):
        savings_usd, savings_pct, savings_basis = 0.0, 0.0, "same_model"
    elif selected_cost is not None and high_counterfactual and high_counterfactual > selected_cost:
        savings_usd = round(high_counterfactual - selected_cost, 6)
        savings_pct = round((savings_usd / high_counterfactual) * 100.0, 1)
        savings_basis = "priced"
    else:
        savings_usd, savings_pct, savings_basis = 0.0, 0.0, "no_savings"

    host_native = bool(
        (payload or {}).get("host_spawn")
        or (payload or {}).get("host_spawn_waves")
        or (payload or {}).get("host_execution_mode") == "host_native"
    )
    skipped = list(skipped_calls or [])
    if host_native:
        skipped.extend(["same-host subprocess delegation", "extra coordinator fanout process"])
    model_counts: dict[str, int] = {}
    for _t, m, _a in agents:
        model_counts[m] = model_counts.get(m, 0) + 1
    selected_model = next(iter(model_counts)) if len(model_counts) == 1 else "mixed"
    selected_alias = agents[0][2] if len({a for _t, _m, a in agents}) == 1 else ""
    # The tier is the agents' own, not the caller's argument: execute_swarm passes a
    # placeholder tier, and a nine-opus swarm labelled "medium" misreports the run.
    tier_counts: dict[str, int] = {}
    for t, _m, _a in agents:
        tier_counts[t] = tier_counts.get(t, 0) + 1
    selected_tier = next(iter(tier_counts)) if len(tier_counts) == 1 else "mixed"
    selected: dict[str, Any] = {
        "tier": selected_tier,
        "model": selected_model,
        "provider": provider,
        "estimated_cost_usd": selected_cost,
        "host_native": host_native,
        "billing": "host_entitlement" if host_native else "metered",
    }
    if selected_alias:
        selected["model_alias"] = selected_alias
    if len(model_counts) > 1:
        selected["models"] = model_counts
    if len(tier_counts) > 1:
        selected["tiers"] = tier_counts
    counterfactual: dict[str, Any] = {
        "tier": "high",
        "model": counterfactual_model,
        "estimated_cost_usd": high_counterfactual,
    }
    if counterfactual_alias:
        counterfactual["model_alias"] = counterfactual_alias
    return {
        "receipt_version": 2,
        "source_tool": source_tool,
        "task_hash": sha256(task.encode("utf-8")).hexdigest()[:16],
        "agent_count": agent_count,
        # These are token-budget-per-tier estimates (_TIER_TOKEN_BUDGETS), not
        # measured spend — labeled so the figure is never read as billed usage.
        "estimate_basis": "tier_token_budget",
        "currency": "USD",
        "selected": selected,
        "counterfactual": counterfactual,
        "savings": {
            "estimated_usd": savings_usd,
            "pct": savings_pct,
            "basis": savings_basis,
        },
        "skipped_calls": sorted(set(s for s in skipped if s)),
        "rationale": rationale or "Selected the cheapest host-native path that matched the task tier.",
        "disclaimer": "Estimate only; provider invoices and subscription quotas remain source of truth.",
    }


def build_run_receipt_payload(
    *,
    run_id: str,
    source_tool: str,
    task: str,
    payload: Mapping[str, Any],
    cost_receipt: Mapping[str, Any] | None = None,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    plan = payload.get("plan") if isinstance(payload.get("plan"), Mapping) else payload
    waves = payload.get("host_spawn_waves")
    if not isinstance(waves, list) and isinstance(plan, Mapping):
        waves = plan.get("host_spawn_waves")
    return {
        "receipt_version": 1,
        "run_id": run_id,
        "source_tool": source_tool,
        "created_ts": time.time(),
        "workspace_root": workspace_root,
        "task_hash": sha256(task.encode("utf-8")).hexdigest()[:16],
        # Derived labels only (never the prose): direct_edit_quality needs the
        # role/kind of a route task long after its routing guard was replaced.
        "task_role": _derived_label("role", task),
        "task_kind": _derived_label("kind", task),
        "status": payload.get("status") or payload.get("host_execution_mode") or "planned",
        "topology": payload.get("topology") or (plan.get("topology") if isinstance(plan, Mapping) else None),
        "plan": {
            "analysis": plan.get("analysis") if isinstance(plan, Mapping) else None,
            "strategy": plan.get("strategy") if isinstance(plan, Mapping) else None,
            "subtasks": plan.get("subtasks") if isinstance(plan, Mapping) else [],
            "waves": plan.get("waves") if isinstance(plan, Mapping) else [],
        },
        "host_spawn_waves": waves or [],
        "learning_report_contract": payload.get("learning_report_contract"),
        "cost_receipt": dict(cost_receipt or {}),
        # Concrete model id (aliases resolved) — telemetry columns still hold the alias.
        "model_id": (
            (cost_receipt.get("selected") or {}).get("model")
            if isinstance(cost_receipt, Mapping) and isinstance(cost_receipt.get("selected"), Mapping)
            else None
        ),
        "approvals": [],
        "policy_decisions": [
            "host-native execution" if payload.get("host_execution_mode") == "host_native" or waves else "direct route",
        ],
        "verification_commands": [],
        "outcome": payload.get("outcome"),
    }


def receipt_to_markdown(receipt: Mapping[str, Any]) -> str:
    cost = receipt.get("cost_receipt") if isinstance(receipt.get("cost_receipt"), Mapping) else {}
    plan = receipt.get("plan") if isinstance(receipt.get("plan"), Mapping) else {}
    subtasks = plan.get("subtasks") if isinstance(plan.get("subtasks"), list) else []
    waves = plan.get("waves") if isinstance(plan.get("waves"), list) else []
    lines = [
        f"# Threnody Run Receipt: {receipt.get('run_id')}",
        "",
        f"- Source: `{receipt.get('source_tool')}`",
        f"- Status: `{receipt.get('status')}`",
        f"- Topology: `{receipt.get('topology') or 'n/a'}`",
        f"- Subtasks: {len(subtasks)}",
        f"- Waves: {len(waves)}",
    ]
    if cost:
        selected = cost.get("selected") if isinstance(cost.get("selected"), Mapping) else {}
        savings = cost.get("savings") if isinstance(cost.get("savings"), Mapping) else {}
        selected_cost_display = (
            f"${float(selected.get('estimated_cost_usd')):.6f}"
            if isinstance(selected.get("estimated_cost_usd"), (int, float))
            else "n/a (unpriced model)"
        )
        savings_display = (
            f"${float(savings.get('estimated_usd')):.6f}"
            if isinstance(savings.get("estimated_usd"), (int, float))
            else f"n/a ({savings.get('basis') or 'unavailable'})"
        )
        lines.extend([
            "",
            "## Cost Receipt",
            f"- Selected: `{selected.get('tier')}` / `{selected.get('model')}`",
            f"- Estimated cost: `{selected_cost_display}`",
            f"- Estimated savings vs high-tier counterfactual: `{savings_display}`",
        ])
    if subtasks:
        lines.extend(["", "## Subtasks"])
        for st in subtasks:
            if isinstance(st, Mapping):
                lines.append(f"- `{st.get('id')}` {st.get('description')} ({st.get('tier')})")
    return "\n".join(lines).rstrip() + "\n"


def receipt_to_html(receipt: Mapping[str, Any]) -> str:
    markdown = receipt_to_markdown(receipt)
    rows = "".join(
        f"<p>{html.escape(line)}</p>" if line else "<br>"
        for line in markdown.splitlines()
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Threnody Run Receipt</title>"
        "<style>body{font:14px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "max-width:960px;margin:32px auto;padding:0 20px;color:#1f2933}"
        "p{margin:6px 0}code{background:#eef2f7;padding:2px 4px;border-radius:4px}</style>"
        "</head><body>"
        f"{rows}"
        "</body></html>"
    )


def record_run_receipt(
    db: Database,
    *,
    run_id: str,
    source_tool: str,
    task: str,
    payload: Mapping[str, Any],
    cost_receipt: Mapping[str, Any] | None = None,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    receipt = build_run_receipt_payload(
        run_id=run_id,
        source_tool=source_tool,
        task=task,
        payload=payload,
        cost_receipt=cost_receipt,
        workspace_root=workspace_root,
    )
    db.record_run_receipt(
        run_id=run_id,
        source_tool=source_tool,
        task_hash=str(receipt["task_hash"]),
        receipt=receipt,
        markdown=receipt_to_markdown(receipt),
    )
    return receipt


def load_run_receipt(db: Database, run_id: str, *, format: str = "json") -> dict[str, Any]:
    row = db.get_run_receipt(run_id)
    if row is None:
        raise KeyError(run_id)
    receipt = row.get("receipt") if isinstance(row.get("receipt"), dict) else {}
    if format == "markdown":
        return {"run_id": run_id, "format": "markdown", "content": row.get("markdown") or receipt_to_markdown(receipt)}
    if format == "html":
        return {"run_id": run_id, "format": "html", "content": receipt_to_html(receipt)}
    return {"run_id": run_id, "format": "json", "receipt": receipt}
