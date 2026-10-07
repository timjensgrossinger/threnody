"""Shared router status snapshot builder for MCP and CLI surfaces."""

from __future__ import annotations

import datetime
import json as _json
import logging
import time
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

    swarm_run_summary = _load_swarm_run_summary(db)
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
        "swarm_runs": swarm_run_summary,
        "reporting": _load_reporting_summary(db, superseded=swarm_run_summary.get("superseded")),
        "provider_health": _load_provider_health(db),
        "spend_summary": spend_snapshot,
        "quality_summary": quality_summary,
        "usage_state": usage_state,
        "plan_cache_summary": plan_cache_summary,
        "agent_spawns": _load_agent_spawn_summary(),
        "db_health": {
            "last_backup": (
                datetime.datetime.fromtimestamp(getattr(db, 'last_backup_ts', None)).isoformat()
                if getattr(db, 'last_backup_ts', None) is not None
                else None
            ),
            "last_integrity_ok": getattr(db, 'last_integrity_ok', None),
            **_load_db_health_snapshot(db),
            **_load_backup_health(db),
        },
        "explainability_link": "threnody inspect status --details",
        "spend_link": "threnody inspect spend --since 7d",
    }


def _load_agent_spawn_summary(*, window_s: float = 86400.0) -> dict:
    """Last 24 h of the Agent hook's spawn ledger (``logs/agent_spawns.jsonl``).

    Fail-soft and file-only: the ledger is the hook's, never the DB's. Shows
    which effort rule fired per spawn and where a requested effort did not land.
    """
    try:
        from shared.agent_ledger import summarize

        summary = summarize(since_ts=time.time() - window_s)
    except Exception:
        log.debug("agent spawn summary load failed", exc_info=True)
        return {"window_hours": int(window_s // 3600), "available": False}
    return {"window_hours": int(window_s // 3600), **summary}


def _load_db_health_snapshot(db: Database) -> dict:
    """Corruption-detection state — the single producer is Database.db_health_snapshot.

    Kept separate from ``_load_backup_health`` (which answers "is there a restore
    candidate on disk") — this answers "has this live process actually seen
    corruption", which only ``Database`` itself can know.
    """
    snapshot_fn = getattr(db, "db_health_snapshot", None)
    if not callable(snapshot_fn):
        return {}  # RemoteDatabase / stub without the method — nothing to report.
    try:
        snapshot = snapshot_fn()
    except Exception:
        log.debug("db health snapshot probe failed", exc_info=True)
        return {}
    if not isinstance(snapshot, dict):
        return {}
    return {
        "healthy": snapshot.get("healthy"),
        "corruption_detected_ts": snapshot.get("corruption_detected_ts"),
        "recovered_this_session": snapshot.get("recovered_this_session"),
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
    # Public accessor first: RemoteDatabase proxies it to the daemon, whereas the
    # private name resolves to nothing over the proxy and always reported None.
    age_fn = getattr(db, "newest_backup_age_s", None) or getattr(db, "_newest_backup_age_s", None)
    if not callable(age_fn):
        return result  # stub — nothing to report.
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


def _load_swarm_run_summary(db: Database, *, stale_after_s: float = 86400.0) -> dict:
    """Swarm run counts by status, plus active runs past the reaper's age cutoff.

    ``stale_active`` counts runs still in an active status that are older than
    *stale_after_s* — the backlog ``Database.reap_stale_swarm_runs`` has not
    marked ``abandoned`` yet (or skipped for recent run-dir activity).
    ``superseded`` counts handoffs retired by a newer handoff of the same
    workspace before any worker started (``Database.supersede_idle_swarm_runs``).
    """
    result: dict[str, object] = {
        "by_status": {}, "abandoned": 0, "superseded": 0, "stale_active": 0,
    }
    active = Database.ACTIVE_SWARM_STATUSES
    try:
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM swarm_runs GROUP BY status"
            ).fetchall()
            by_status = {str(row[0]): int(row[1]) for row in rows}
            stale_row = conn.execute(
                f"SELECT COUNT(*) FROM swarm_runs "
                f"WHERE status IN ({', '.join(['?'] * len(active))}) AND created_ts < ?",
                (*active, time.time() - stale_after_s),
            ).fetchone()
        result["by_status"] = by_status
        result["abandoned"] = by_status.get("abandoned", 0)
        result["superseded"] = by_status.get(Database.SWARM_STATUS_SUPERSEDED, 0)
        result["stale_active"] = int(stale_row[0]) if stale_row and stale_row[0] else 0
    except Exception:
        log.debug("swarm run summary load failed", exc_info=True)
    return result


_UNREPORTED_AFTER_S = 3600.0
_UNREPORTED_SAMPLE = 5


def _load_reporting_summary(
    db: Database, *, now: float | None = None, superseded: object = None
) -> dict:
    """Is reporting actually reaching Threnody? Fail-soft; each part degrades alone.

    * ``unreported_swarms`` — host handoffs still ``awaiting_host_execution`` an
      hour after they were issued: the host never sent a single wave report.
    * ``superseded`` — handoffs retired by a newer handoff of the same workspace
      (the ``swarm_runs`` summary's count when the caller passes it in).
    * ``outcomes_missing_model_24h`` — outcome rows from the last 24 h with no
      ``model_used``: nobody, not even route telemetry, said which model ran.
    * ``log_file`` — where warnings about lost reports are written.
    """
    current = time.time() if now is None else now
    result: dict[str, object] = {
        "unreported_swarms": {"count": 0, "newest": [], "older_than_s": _UNREPORTED_AFTER_S},
        "superseded": 0,
        "outcomes_missing_model_24h": None,
        "outcomes_24h": None,
        "log_file": None,
    }
    try:
        with db.conn() as conn:
            cutoff = current - _UNREPORTED_AFTER_S
            count_row = conn.execute(
                "SELECT COUNT(*) FROM swarm_runs "
                "WHERE status = 'awaiting_host_execution' AND created_ts < ?",
                (cutoff,),
            ).fetchone()
            rows = conn.execute(
                "SELECT swarm_id, created_ts FROM swarm_runs "
                "WHERE status = 'awaiting_host_execution' AND created_ts < ? "
                "ORDER BY created_ts DESC LIMIT ?",
                (cutoff, _UNREPORTED_SAMPLE),
            ).fetchall()
        result["unreported_swarms"] = {
            "count": int(count_row[0]) if count_row and count_row[0] else 0,
            "newest": [
                {"swarm_id": str(rid), "age_s": round(max(0.0, current - float(ts or 0.0)), 1)}
                for rid, ts in rows
            ],
            "older_than_s": _UNREPORTED_AFTER_S,
        }
    except Exception:
        log.warning("reporting summary: unreported swarm scan failed", exc_info=True)
    if isinstance(superseded, int):
        result["superseded"] = superseded
    else:
        try:
            with db.conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM swarm_runs WHERE status = ?",
                    (Database.SWARM_STATUS_SUPERSEDED,),
                ).fetchone()
            result["superseded"] = int(row[0]) if row and row[0] else 0
        except Exception:
            log.warning("reporting summary: superseded count failed", exc_info=True)
    try:
        with db.conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN model_used IS NULL OR model_used = '' THEN 1 ELSE 0 END) "
                "FROM routing_outcomes WHERE recorded_at >= ?",
                (current - 86400.0,),
            ).fetchone()
        result["outcomes_24h"] = int(row[0]) if row and row[0] else 0
        result["outcomes_missing_model_24h"] = int(row[1]) if row and row[1] else 0
    except Exception:
        log.warning("reporting summary: routing_outcomes scan failed", exc_info=True)
    try:
        from shared.logging_setup import log_file_path

        result["log_file"] = str(log_file_path())
    except Exception:
        log.warning("reporting summary: log path resolution failed", exc_info=True)
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