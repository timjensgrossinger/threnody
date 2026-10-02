"""Quality-ledger writes for route_task work done by direct host edits.

A host that calls ``route_task`` and then edits files itself (no swarm) used to
leave no trace in ``model_quality_events``: touched files were stored nowhere and
the host's verdict only fed the router. :func:`finalize_route_task` closes that
loop off the hot path:

* the host's verdict (``record_outcome``) becomes a PROXY ``outcome`` row, and
* the files edited under the route guard are verified (lint/type/test, scoped to
  those files, graded against the merge base) into an OBJECTIVE ``verify_gate`` row.

Everything is best-effort: a failure here must never affect routing or the edit.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROUTE_ID_PREFIX = "route-"


def _routed_context(db: Any, task_id: str) -> dict[str, Any]:
    from . import outcomes as shared_outcomes

    try:
        return shared_outcomes._latest_telemetry_context(db, task_id)
    except Exception:
        log.debug("telemetry context lookup failed for %s", task_id, exc_info=True)
        return {}


def _receipt_context(db: Any, task_id: str) -> dict[str, Any]:
    """Return ``workspace_root`` and the derived ``task_role``/``task_kind`` labels.

    The receipt deliberately stores a task hash, not the prose; the role and kind
    are derived at route time (shared/receipts.py) so they survive the routing
    guard being replaced before the host reports its verdict.
    """
    try:
        row = db.get_run_receipt(task_id)
    except Exception:
        log.debug("receipt lookup failed for %s", task_id, exc_info=True)
        return {}
    receipt = row.get("receipt") if isinstance(row, dict) else None
    return dict(receipt) if isinstance(receipt, dict) else {}


def _guard_task_text(db: Any, task_id: str, caller: str | None, cwd: str | None) -> str:
    """Recover the task text from the live route guard when it matches ``task_id``."""
    if not cwd:
        return ""
    from . import outcomes as shared_outcomes

    for who in dict.fromkeys([caller or "", "claude-code", "mcp"]):
        if not who:
            continue
        try:
            guard = db.routing_guard_get(caller=who, cwd=cwd)
        except Exception:
            log.debug("guard lookup failed for %s", task_id, exc_info=True)
            continue
        text = str((guard or {}).get("task_text") or "")
        if text and shared_outcomes.route_task_id(text) == task_id:
            return text
    return ""


def _verify_row_exists(db: Any, task_id: str) -> bool:
    with db.conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM model_quality_events "
            "WHERE source = 'verify_gate' AND run_id = ? LIMIT 1",
            (task_id,),
        ).fetchone()
    return row is not None


def finalize_route_task(
    db: Any,
    task_id: str,
    *,
    config: Any,
    outcome: str | None = None,
    actual_model: str | None = None,
    actual_tier: str | None = None,
    caller: str | None = None,
    task_text: str | None = None,
) -> dict[str, Any]:
    """Write ledger rows for a route_task id. Returns a summary; never raises."""
    summary: dict[str, Any] = {
        "task_id": task_id,
        "outcome_recorded": False,
        "verify_recorded": False,
        "attribution": None,
        "skipped": None,
    }
    try:
        if not isinstance(task_id, str) or not task_id.startswith(ROUTE_ID_PREFIX):
            summary["skipped"] = "not_a_route_task"
            return summary
        if not getattr(config.model_quality, "enabled", True):
            summary["skipped"] = "model_quality_disabled"
            return summary

        from .host_spawn import host_native_model_for_tier
        from .model_quality import record_outcome_score, record_verify_gate_score
        from .roles import derive_role_from_task
        from .task_kinds import derive_kind_from_task

        ctx = _routed_context(db, task_id)
        routed_tier = ctx.get("tier") or None
        routed_model = ctx.get("model") or None
        receipt = _receipt_context(db, task_id)
        workspace_root = str(receipt["workspace_root"]) if receipt.get("workspace_root") else None
        role = receipt.get("task_role") or None
        kind = receipt.get("task_kind") or None
        if not (role and kind):
            # Receipts written before the labels existed: fall back to the task
            # text (explicit argument, or the live guard if it is still ours).
            text = task_text or _guard_task_text(db, task_id, caller, workspace_root)
            if text:
                role = role or derive_role_from_task(text) or None
                kind = kind or derive_kind_from_task(text) or None

        if actual_model:
            model, attribution = actual_model, "reported"
            tier = actual_tier or routed_tier
        elif actual_tier:
            model = host_native_model_for_tier(config, caller or "claude-code", actual_tier)
            attribution, tier = "override", actual_tier
        else:
            model, attribution, tier = routed_model, "routed", routed_tier
        summary["attribution"] = attribution
        summary["model"] = model
        summary["tier"] = tier

        if outcome:
            summary["outcome_recorded"] = bool(
                record_outcome_score(
                    db,
                    model=model,
                    outcome=outcome,
                    role=role,
                    kind=kind,
                    tier=tier,
                    task_hash=task_id,
                    run_id=task_id,
                    attribution=attribution,
                )
            )

        _finalize_verify(
            db,
            task_id,
            config=config,
            workspace_root=workspace_root,
            model=model,
            tier=tier,
            role=role,
            kind=kind,
            summary=summary,
            record_verify_gate_score=record_verify_gate_score,
        )
    except Exception:
        log.debug("finalize_route_task failed for %s", task_id, exc_info=True)
        summary["error"] = True
    return summary


def _finalize_verify(
    db: Any,
    task_id: str,
    *,
    config: Any,
    workspace_root: str | None,
    model: str | None,
    tier: str | None,
    role: str | None,
    kind: str | None,
    summary: dict[str, Any],
    record_verify_gate_score: Any,
) -> None:
    if not getattr(config.verify_gate, "enabled", False):
        summary["verify_skipped"] = "verify_disabled"
        return
    if not workspace_root:
        summary["verify_skipped"] = "no_workspace"
        return
    touched = db.direct_edit_touches(task_id)
    root = Path(workspace_root)
    files: list[str] = []
    for raw in touched:
        try:
            p = Path(raw)
            if p.is_file() and (p.resolve() == root.resolve() or root.resolve() in p.resolve().parents):
                files.append(str(p))
        except OSError:
            log.debug("touched file check failed for %s", raw, exc_info=True)
    if not files:
        summary["verify_skipped"] = "no_touched_files"
        return
    if _verify_row_exists(db, task_id):
        summary["verify_skipped"] = "already_recorded"
        return

    from .verify import run_verify_gate, scoped_resolver, verify_report_score

    # Always scoped on this path (regardless of ``scope``) and run_id=None, which
    # avoids creating a runs/ directory per route task.
    report = run_verify_gate(
        config.verify_gate,
        project_root=str(root),
        run_id=None,
        command_resolver=scoped_resolver(files),
    )
    rd = report.to_dict()
    score = verify_report_score(rd)
    if score is None:
        summary["verify_skipped"] = "unscorable"
        return
    record_verify_gate_score(
        db,
        model=model,
        effort=None,
        score_0_10=score,
        new_failure_count=len(rd.get("new_failures") or []),
        preexisting_count=len(rd.get("preexisting_failures") or []),
        role=role,
        kind=kind,
        tier=tier,
        task_hash=task_id,
        run_id=task_id,
        ran_signals=rd.get("ran_signals"),
    )
    summary["verify_recorded"] = True
    summary["verify_score"] = score


def schedule_finalize(db: Any, task_id: str, *, config: Any, **kw: Any) -> None:
    """Run :func:`finalize_route_task` on the warm-path executor. Never raises."""
    try:
        from .eval import _get_warm_path_executor, _warm_path_worker_count

        executor = _get_warm_path_executor(_warm_path_worker_count(config))
        executor.submit(finalize_route_task, db, task_id, config=config, **kw)
    except Exception:
        log.debug("schedule_finalize failed for %s", task_id, exc_info=True)
