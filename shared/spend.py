"""Operator-facing spend and savings snapshots from local cost telemetry."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shared.config import TGsConfig
    from shared.db import Database

log = logging.getLogger(__name__)

_DEFAULT_WINDOW = "7d"


def parse_spend_window(since: str) -> tuple[float, str]:
    """Parse window strings like ``7d``, ``30d``, ``24h`` into ``(since_ts, label)``."""
    normalized = (since or _DEFAULT_WINDOW).strip().lower()
    now = time.time()
    if normalized.endswith("d"):
        try:
            days = float(normalized[:-1])
        except ValueError:
            days = 7.0
            normalized = _DEFAULT_WINDOW
        return now - days * 86400, normalized
    if normalized.endswith("h"):
        try:
            hours = float(normalized[:-1])
        except ValueError:
            hours = 24.0
            normalized = "24h"
        return now - hours * 3600, normalized
    if normalized in {"all", "all-time", "alltime"}:
        return 0.0, "all"
    return now - 7 * 86400, _DEFAULT_WINDOW


def _aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_est = sum(float(row.get("est_cost_usd") or 0.0) for row in rows)
    total_cf = sum(float(row.get("counterfactual_cost_usd") or 0.0) for row in rows)
    total_input = sum(int(row.get("input_tokens") or 0) for row in rows)
    total_output = sum(int(row.get("output_tokens") or 0) for row in rows)
    subtasks = sum(int(row.get("subtask_count") or 0) for row in rows)
    return {
        "subtask_count": subtasks,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "est_cost_usd": round(total_est, 6),
        "counterfactual_cost_usd": round(total_cf, 6),
        "savings_usd": round(total_cf - total_est, 6),
    }


def _free_subtask_stats(db: Database, since_ts: float) -> dict[str, Any]:
    try:
        with db.conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN est_cost_usd <= 0 THEN 1 ELSE 0 END)"
                " FROM cost_telemetry WHERE ts >= ?",
                (since_ts,),
            ).fetchone()
        total = int(row[0] or 0) if row else 0
        free_count = int(row[1] or 0) if row else 0
    except Exception:
        log.debug("free subtask stats query failed", exc_info=True)
        total = 0
        free_count = 0
    pct = round((free_count / total) * 100.0, 1) if total else 0.0
    return {
        "free_subtask_count": free_count,
        "free_subtask_pct": pct,
    }


def _routing_telemetry_summary(db: Database, since_ts: float) -> dict[str, Any]:
    """Aggregate routed execution telemetry (tokens by tier)."""
    try:
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT tier, COUNT(*), COALESCE(SUM(tokens_used), 0),"
                " COALESCE(SUM(actual_tokens), 0), COALESCE(SUM(estimated_tokens), 0)"
                " FROM telemetry WHERE ts >= ? GROUP BY tier",
                (since_ts,),
            ).fetchall()
    except Exception:
        log.debug("routing telemetry summary failed", exc_info=True)
        return {"initialized": False, "by_tier": [], "execution_count": 0}

    by_tier: list[dict[str, Any]] = []
    execution_count = 0
    for tier, count, tokens_used, actual_tokens, estimated_tokens in rows:
        execution_count += int(count or 0)
        by_tier.append({
            "tier": tier,
            "execution_count": int(count or 0),
            "tokens_used": int(tokens_used or 0),
            "actual_tokens": int(actual_tokens or 0),
            "estimated_tokens": int(estimated_tokens or 0),
        })
    return {
        "initialized": bool(by_tier),
        "execution_count": execution_count,
        "by_tier": by_tier,
    }


def _serialize_usage_windows(config: TGsConfig | None) -> dict[str, Any]:
    if config is None:
        return {}
    windows = getattr(config, "provider_usage_windows", None) or {}
    serialized: dict[str, Any] = {}
    for provider_id, window_cfg in windows.items():
        if hasattr(window_cfg, "to_dict"):
            serialized[str(provider_id)] = window_cfg.to_dict()
        elif isinstance(window_cfg, dict):
            serialized[str(provider_id)] = window_cfg
    return serialized


def build_usage_state(db: Database, config: TGsConfig | None = None) -> list[dict[str, Any]]:
    """Return per-provider usage window headroom for operator surfaces."""
    if config is None:
        return []

    from shared.discovery import ProviderUsageChecker

    checker = ProviderUsageChecker()
    usage_windows = getattr(config, "provider_usage_windows", None) or {}
    now = time.time()
    states: list[dict[str, Any]] = []

    for provider_id, window_cfg in usage_windows.items():
        entries = getattr(window_cfg, "windows", None) or []
        for entry in entries:
            hours = getattr(entry, "hours", None)
            budget_tokens = getattr(entry, "budget_tokens", None)
            threshold = getattr(entry, "threshold", None)
            action = getattr(entry, "action", None)
            if isinstance(entry, dict):
                hours = entry.get("hours")
                budget_tokens = entry.get("budget_tokens")
                threshold = entry.get("threshold")
                action = entry.get("action")
            if not isinstance(hours, (int, float)) or not isinstance(threshold, (int, float)):
                continue
            decision = checker.query_window_decision(
                str(provider_id).strip().lower(),
                float(hours),
                budget_tokens if isinstance(budget_tokens, int) else None,
                float(threshold),
                str(action or "prefer_alternatives"),
                db,
            )
            ratio = decision.get("ratio")
            since_ts = now - float(hours) * 3600.0
            tokens_used: int | None = None
            if isinstance(budget_tokens, int) and budget_tokens > 0:
                try:
                    tokens_used = db.get_provider_token_usage(
                        str(provider_id).strip().lower(),
                        since_ts,
                    )
                except Exception:
                    log.debug("usage_state token query failed", exc_info=True)
                    tokens_used = None
            pct = round(float(ratio) * 100.0, 1) if isinstance(ratio, (int, float)) else None
            states.append({
                "provider": str(provider_id).strip().lower(),
                "window": f"{float(hours):g}h",
                "tokens_used": tokens_used,
                "limit": budget_tokens if isinstance(budget_tokens, int) else None,
                "pct": pct,
                "threshold_pct": round(float(threshold) * 100.0, 1),
                "action": str(action or decision.get("action") or "prefer_alternatives"),
                "triggered": bool(decision.get("triggered")),
                "source": decision.get("source"),
            })
    return states


def build_spend_snapshot(
    db: Database,
    *,
    since: str = _DEFAULT_WINDOW,
    config: TGsConfig | None = None,
) -> dict[str, Any]:
    """Return aggregated spend/savings for operator and MCP inspect surfaces."""
    since_ts, window_label = parse_spend_window(since)
    by_tier = db.get_cost_summary(since_ts=since_ts, group_by="tier")
    by_provider = db.get_cost_summary(since_ts=since_ts, group_by="provider_id")
    totals = _aggregate_rows(by_tier)
    totals.update(_free_subtask_stats(db, since_ts))
    routing = _routing_telemetry_summary(db, since_ts)
    usage_state = build_usage_state(db, config)
    try:
        with db.conn() as conn:
            receipt_rows = conn.execute(
                """
                SELECT run_id, source_tool, receipt_json, created_ts
                FROM run_receipts
                WHERE created_ts >= ?
                ORDER BY created_ts DESC
                LIMIT 25
                """,
                (since_ts,),
            ).fetchall()
    except Exception:
        log.debug("run receipt summary failed", exc_info=True)
        receipt_rows = []
    receipts: list[dict[str, Any]] = []
    receipt_savings = 0.0
    for run_id, source_tool, receipt_json, created_ts in receipt_rows:
        try:
            import json

            receipt = json.loads(receipt_json)
        except Exception:
            receipt = {}
        cost_receipt = receipt.get("cost_receipt") if isinstance(receipt, dict) else {}
        savings = cost_receipt.get("savings") if isinstance(cost_receipt, dict) else {}
        selected = cost_receipt.get("selected") if isinstance(cost_receipt, dict) else {}
        savings_usd = float(savings.get("estimated_usd") or 0.0) if isinstance(savings, dict) else 0.0
        receipt_savings += savings_usd
        receipts.append({
            "run_id": run_id,
            "source_tool": source_tool,
            "created_ts": float(created_ts),
            "selected_tier": selected.get("tier") if isinstance(selected, dict) else None,
            "selected_model": selected.get("model") if isinstance(selected, dict) else None,
            "estimated_savings_usd": round(savings_usd, 6),
        })
    return {
        "window": window_label,
        "since_ts": since_ts,
        "totals": totals,
        "by_tier": by_tier,
        "by_provider": by_provider,
        "routing_telemetry": routing,
        "receipts": {
            "count": len(receipts),
            "estimated_savings_usd": round(receipt_savings, 6),
            "recent": receipts,
        },
        "usage_windows": _serialize_usage_windows(config),
        "usage_state": usage_state,
        "disclaimer": (
            "est_cost_usd uses bundled model price hints and token estimates; "
            "not a provider invoice."
        ),
        "cli_hint": f"threnody gain --since {window_label}",
    }


__all__ = ["build_spend_snapshot", "build_usage_state", "parse_spend_window"]
