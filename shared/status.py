"""Shared router status snapshot builder for MCP and CLI surfaces."""

from __future__ import annotations

import datetime
import json as _json
import logging
from typing import TYPE_CHECKING

from shared.agents import DEFAULT_PENDING_APPROVAL_LIMIT, approval_queue_list
from shared.config import normalize_parallelism_limit
from shared.db import DEFAULT_PROJECT_FANOUT_CAP, Database
from shared.plan_cache import build_plan_cache_summary
from shared.spend import build_spend_snapshot, build_usage_state

if TYPE_CHECKING:
    from shared.config import TGsConfig

log = logging.getLogger(__name__)
_MAX_STATUS_NOTE_LEN = 400


def build_status_snapshot(
    config: "TGsConfig",
    db: Database,
    project_id: str,
) -> dict:
    """Return a point-in-time router status snapshot for one project.

    The caller must pass an already-normalized, workspace-validated project_id.
    Returns conservative defaults for missing or partially initialized data.
    """
    settings = db.get_project_settings(project_id)
    learning_enabled = bool(settings.get("learning_enabled", False))
    raw_concurrency_limit = settings.get(
        "concurrency_limit",
        config.parallelism.max_workers,
    )
    concurrency_limit = normalize_parallelism_limit(raw_concurrency_limit)
    budget_hard_cap_tokens = int(
        settings.get("budget_hard_cap_tokens", config.budgets.default_hard_cap_tokens)
    )
    raw_fanout_cap = settings.get("fanout_cap", DEFAULT_PROJECT_FANOUT_CAP)
    fanout_cap = normalize_parallelism_limit(
        raw_fanout_cap,
        zero_means_disabled=True,
    )
    pending_approval_limit = int(
        settings.get("pending_approval_limit", DEFAULT_PENDING_APPROVAL_LIMIT)
    )

    pending_items = _load_pending_approvals(project_id, db)

    enabled_features: list[str] = []
    if learning_enabled:
        enabled_features.append("learning")
    if pending_items:
        enabled_features.append("approval_queue")
    fanout_enabled = fanout_cap != 0
    if fanout_enabled:
        enabled_features.append("fanout")

    disabled_features: list[str] = []
    if not learning_enabled:
        disabled_features.append("learning")
    if not pending_items:
        disabled_features.append("approval_queue")
    if not fanout_enabled:
        disabled_features.append("fanout")

    limits = {
        "concurrency": concurrency_limit,
        "budget_hard_cap_tokens": budget_hard_cap_tokens,
        "fanout_cap": fanout_cap,
        "pending_approval_limit": pending_approval_limit,
    }

    spend_snapshot = _load_spend_summary(db, config)
    quality_summary = _load_quality_summary(db, config)
    usage_state = build_usage_state(db, config)
    plan_cache_summary = build_plan_cache_summary(db)

    return {
        "project_id": project_id,
        "readiness": {
            "enabled": enabled_features,
            "enabled_features": enabled_features,
            "disabled_features": disabled_features,
            "limits": limits,
            "summary": {
                "learning_enabled": learning_enabled,
                "pending_approval_count": len(pending_items),
                "conservative_defaults": not bool(project_id),
            },
        },
        "limits": limits,
        "pending_approvals": pending_items,
        "recent_summary": _load_recent_summary(db),
        "adaptive_thresholds": _load_adaptive_summary(db),
        "rework_summary": _load_rework_summary(db),
        "provider_health": _load_provider_health(db),
        "spend_summary": spend_snapshot,
        "quality_summary": quality_summary,
        "usage_state": usage_state,
        "plan_cache_summary": plan_cache_summary,
        "db_health": {
            "last_backup": (
                datetime.datetime.fromtimestamp(getattr(db, 'last_backup_ts', None)).isoformat()
                if getattr(db, 'last_backup_ts', None) is not None
                else None
            ),
            "last_integrity_ok": getattr(db, 'last_integrity_ok', None),
            **_load_backup_health(db),
        },
        "explainability_link": "threnody inspect status --details",
        "spend_link": "threnody inspect spend --since 7d",
    }


def _load_backup_health(db: Database) -> dict:
    """Report whether a restore candidate exists on disk, and how old it is.

    ``last_backup_ts`` only reflects a backup *this process* took, so it reads
    None on a healthy long-lived install. What actually decides whether a
    corruption costs every learning table is whether a ``.bak.*`` file exists at
    all — surface that, plus a warning when it does not.
    """
    result: dict[str, object] = {
        "backups_present": None,
        "newest_backup_age_hours": None,
    }
    age_fn = getattr(db, "_newest_backup_age_s", None)
    if not callable(age_fn):
        return result  # RemoteDatabase / stub — nothing to report.
    try:
        age_s = age_fn()
    except Exception:
        log.debug("backup health probe failed", exc_info=True)
        return result
    result["backups_present"] = age_s is not None
    if age_s is None:
        result["warning"] = (
            "no DB backup on disk — a corruption would quarantine cache.db and "
            "reset every learning table (run: threnody db backup)"
        )
    else:
        result["newest_backup_age_hours"] = round(age_s / 3600.0, 2)
    return result


def _load_pending_approvals(project_id: str, db: Database) -> list[dict]:
    """Return pending approvals or an empty list."""
    if not project_id:
        return []
    try:
        return approval_queue_list(project_id, db=db)
    except Exception:
        log.debug("pending approvals load failed", exc_info=True)
        return []


def _load_recent_summary(db: Database) -> dict:
    """Return recent telemetry aggregates or zero-initialized defaults."""
    result: dict[str, object] = {
        "artifact_publish_count": 0,
        "artifact_consume_count": 0,
        "coordinator_amendment_count": 0,
        "max_urgency_score": None,
        "latest_notable_event": None,
    }
    try:
        with db.conn() as conn:
            row = conn.execute(
                "SELECT SUM(artifact_publish_count), SUM(artifact_consume_count), "
                "SUM(coordinator_amendment_count), MAX(urgency_score) "
                "FROM telemetry"
            ).fetchone()
            if row:
                result["artifact_publish_count"] = int(row[0]) if row[0] is not None else 0
                result["artifact_consume_count"] = int(row[1]) if row[1] is not None else 0
                result["coordinator_amendment_count"] = int(row[2]) if row[2] is not None else 0
                result["max_urgency_score"] = float(row[3]) if row[3] is not None else None

            note_row = conn.execute(
                "SELECT parse_diagnostics, reason FROM telemetry "
                "WHERE (parse_diagnostics IS NOT NULL AND parse_diagnostics != '') "
                "OR (reason IS NOT NULL AND reason != '') "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if note_row:
                parse_diag, reason = note_row
                latest_note: str | None = None
                if isinstance(parse_diag, str) and parse_diag:
                    try:
                        parsed = _json.loads(parse_diag)
                        if isinstance(parsed, dict):
                            latest_note = str(
                                parsed.get("note")
                                or parsed.get("message")
                                or str(parsed)
                            )[:_MAX_STATUS_NOTE_LEN]
                        else:
                            latest_note = str(parsed)[:_MAX_STATUS_NOTE_LEN]
                    except _json.JSONDecodeError:
                        latest_note = str(parse_diag)[:_MAX_STATUS_NOTE_LEN]
                elif reason:
                    latest_note = str(reason)[:_MAX_STATUS_NOTE_LEN]
                result["latest_notable_event"] = latest_note
    except Exception:
        log.debug("recent summary load failed", exc_info=True)
    return result


def _load_adaptive_summary(db: Database) -> dict:
    """Return adaptive threshold stats or empty sentinel."""
    try:
        from shared.adaptive import get_band_stats

        bands = get_band_stats(db)
        if not bands:
            return {"initialized": False, "bands": []}
        return {
            "initialized": True,
            "band_count": len(bands),
            "total_samples": sum(int(b.get("sample_count") or 0) for b in bands),
            "bands": bands,
        }
    except Exception:
        log.debug("adaptive threshold load failed", exc_info=True)
        return {"initialized": False, "bands": []}


def _load_provider_health(db: Database) -> dict:
    """Return provider health snapshot for status surfaces."""
    try:
        rows = db.iter_provider_health()
        quarantined = [r for r in rows if r.get("state") == "QUARANTINED"]
        degraded = [r for r in rows if r.get("state") == "DEGRADED"]
        return {
            "providers": rows,
            "quarantined_count": len(quarantined),
            "degraded_count": len(degraded),
            "any_unhealthy": bool(quarantined or degraded),
        }
    except Exception:
        log.debug("provider health load failed", exc_info=True)
        return {"providers": [], "quarantined_count": 0, "degraded_count": 0, "any_unhealthy": False}


def _load_spend_summary(db: Database, config: "TGsConfig") -> dict:
    """Return compact spend totals for status surfaces."""
    try:
        snapshot = build_spend_snapshot(db, since="7d", config=config)
        totals = snapshot.get("totals") if isinstance(snapshot.get("totals"), dict) else {}
        return {
            "window": snapshot.get("window", "7d"),
            "subtask_count": int(totals.get("subtask_count") or 0),
            "est_cost_usd": totals.get("est_cost_usd", 0.0),
            "savings_usd": totals.get("savings_usd", 0.0),
            "free_subtask_pct": totals.get("free_subtask_pct", 0.0),
            "disclaimer": snapshot.get("disclaimer"),
            "cli_hint": snapshot.get("cli_hint"),
        }
    except Exception:
        log.debug("spend summary load failed", exc_info=True)
        return {
            "window": "7d",
            "subtask_count": 0,
            "est_cost_usd": 0.0,
            "savings_usd": 0.0,
            "free_subtask_pct": 0.0,
        }


def _load_quality_summary(db: Database, config: "TGsConfig") -> dict:
    """Return a compact model-quality ledger summary for status surfaces."""
    try:
        from shared.model_quality import build_quality_snapshot

        snapshot = build_quality_snapshot(db, since="7d", config=config)
        rows = snapshot.get("rows") or []
        top = sorted(rows, key=lambda r: r.get("n", 0), reverse=True)[:5]
        return {
            "window": snapshot.get("window", "7d"),
            "event_count": int(snapshot.get("event_count") or 0),
            "tracked_keys": len(rows),
            "top": [
                {
                    "model": r.get("model"),
                    "effort": r.get("effort"),
                    "dimension": (
                        f"{r.get('dimension')}/{r.get('sub_dimension')}"
                        if r.get("sub_dimension")
                        else r.get("dimension")
                    ),
                    "avg_score": r.get("avg_score"),
                    "n": r.get("n"),
                }
                for r in top
            ],
            "cli_hint": snapshot.get("cli_hint"),
        }
    except Exception:
        log.debug("quality summary load failed", exc_info=True)
        return {"window": "7d", "event_count": 0, "tracked_keys": 0, "top": []}


def _load_rework_summary(db: Database) -> dict:
    """Return global rework count or zero-initialized sentinel."""
    try:
        with db.conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM rework_events").fetchone()
            count = int(row[0]) if row and row[0] is not None else 0
        if count == 0:
            return {"initialized": False, "scope": "global", "recent_rework_count": 0}
        return {"initialized": True, "scope": "global", "recent_rework_count": count}
    except Exception:
        log.debug("rework summary load failed", exc_info=True)
        return {"initialized": False, "scope": "global", "recent_rework_count": 0}


__all__ = ["build_status_snapshot"]