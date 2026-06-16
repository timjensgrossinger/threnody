"""Operator receipts for routing, planning, and host-native runs."""
from __future__ import annotations

import html
import json
import time
from hashlib import sha256
from typing import Any, Mapping

from .db import Database

_TIER_TOKEN_BUDGETS = {"low": 2000, "medium": 8000, "high": 20000}


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
    if not isinstance(payload, Mapping):
        return max(1, fallback)
    subtasks = payload.get("subtasks")
    if isinstance(subtasks, list) and subtasks:
        return len(subtasks)
    waves = payload.get("host_spawn_waves")
    if isinstance(waves, list):
        count = 0
        for wave in waves:
            if isinstance(wave, Mapping) and isinstance(wave.get("agents"), list):
                count += len(wave["agents"])
        if count:
            return count
    return max(1, fallback)


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
    """Build a compact, response-safe savings receipt."""
    agent_count = _agent_count_from_payload(payload)
    resolved_tier = tier or "medium"
    selected_cost = (
        round(float(estimated_cost_usd), 6)
        if isinstance(estimated_cost_usd, (int, float))
        else _estimate_model_cost(model, tier=resolved_tier, agents=agent_count)
    )
    high_counterfactual = _estimate_model_cost(
        "claude-opus-4.6",
        tier="high",
        agents=agent_count,
    )
    if high_counterfactual <= selected_cost:
        high_counterfactual = round(selected_cost + (0.0025 * agent_count), 6)
    savings = round(high_counterfactual - selected_cost, 6)
    host_native = bool(
        (payload or {}).get("host_spawn")
        or (payload or {}).get("host_spawn_waves")
        or (payload or {}).get("host_execution_mode") == "host_native"
    )
    skipped = list(skipped_calls or [])
    if host_native:
        skipped.extend(["same-host subprocess delegation", "extra coordinator fanout process"])
    return {
        "receipt_version": 1,
        "source_tool": source_tool,
        "task_hash": sha256(task.encode("utf-8")).hexdigest()[:16],
        "agent_count": agent_count,
        "selected": {
            "tier": resolved_tier,
            "model": model,
            "provider": provider,
            "estimated_cost_usd": selected_cost,
            "host_native": host_native,
        },
        "counterfactual": {
            "tier": "high",
            "model": "claude-opus-4.6",
            "estimated_cost_usd": round(high_counterfactual, 6),
        },
        "savings": {
            "estimated_usd": savings,
            "pct": round((savings / high_counterfactual) * 100.0, 1) if high_counterfactual else 0.0,
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
        lines.extend([
            "",
            "## Cost Receipt",
            f"- Selected: `{selected.get('tier')}` / `{selected.get('model')}`",
            f"- Estimated cost: `${float(selected.get('estimated_cost_usd') or 0.0):.6f}`",
            f"- Estimated savings vs high-tier counterfactual: `${float(savings.get('estimated_usd') or 0.0):.6f}`",
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
