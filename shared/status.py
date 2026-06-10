"""Shared router status snapshot builder for MCP and CLI surfaces."""

from __future__ import annotations

import datetime
import json as _json
import logging
from typing import TYPE_CHECKING

from shared.agents import DEFAULT_PENDING_APPROVAL_LIMIT, approval_queue_list
from shared.config import normalize_parallelism_limit
from shared.db import DEFAULT_PROJECT_FANOUT_CAP, Database

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
        "db_health": {
            "last_backup": (
                datetime.datetime.fromtimestamp(getattr(db, 'last_backup_ts', None)).isoformat()
                if getattr(db, 'last_backup_ts', None) is not None
                else None
            ),
            "last_integrity_ok": getattr(db, 'last_integrity_ok', None),
        },
        "explainability_link": "threnody inspect status --details",
    }


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