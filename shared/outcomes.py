from __future__ import annotations

import hashlib
import itertools
import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .db import Database
from .memory import memory_set

log = logging.getLogger(__name__)

OUTCOME_VALUES = ("accepted", "revised", "rejected", "reworked")
OUTCOME_ALLOWLIST = frozenset(OUTCOME_VALUES)
OUTCOME_MEMORY_KEY = "routing_outcomes"
OUTCOME_MEMORY_PROJECT_ID = str(Path(__file__).resolve().parent.parent)
OUTCOME_READONLY_WINDOW_SECONDS = 7 * 24 * 60 * 60
ANONYMOUS_OPERATOR_ID = "anonymous"


class OutcomeReadonlyWindowError(RuntimeError):
    """Raised when an outcome can no longer be corrected."""


def _normalize_required_string(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _normalize_optional_string(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string when provided")
    normalized = value.strip()
    return normalized or None


def _normalize_recorded_operator_id(value: str | None) -> str:
    normalized = _normalize_optional_string(value, "operator_id")
    return normalized if normalized is not None else ANONYMOUS_OPERATOR_ID


def _coerce_created_at(value: object, *, fallback: float) -> float:
    if value is None:
        return fallback
    if isinstance(value, str) and not value.strip():
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("existing outcome row has invalid created_at") from exc


def _normalize_outcome(outcome: str) -> str:
    normalized = _normalize_required_string(outcome, "outcome").lower()
    if normalized not in OUTCOME_ALLOWLIST:
        allowed = ", ".join(OUTCOME_VALUES)
        raise ValueError(f"outcome must be one of: {allowed}")
    return normalized


def _normalize_non_negative_int(value: object, field_name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if normalized < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return normalized


def _normalize_swarm_outcome_note(
    note: str | Mapping[str, object] | None,
) -> str | None:
    def _compact_note_text(value: str) -> str | None:
        normalized = " ".join(value.split())
        if not normalized:
            return None
        return normalized[:160]

    if note is None:
        return None
    if isinstance(note, str):
        normalized_note = _normalize_optional_string(note, "note")
        if normalized_note is None:
            return None
        return _compact_note_text(normalized_note)
    if not isinstance(note, Mapping):
        raise ValueError("note must be a string or mapping when provided")
    compact: dict[str, object] = {}
    for key, value in note.items():
        if isinstance(value, str):
            compact_value = _compact_note_text(value)
            if compact_value is not None:
                compact[str(key)] = compact_value
        elif isinstance(value, (int, float, bool)) or value is None:
            compact[str(key)] = value
    if not compact:
        return None
    return json.dumps(compact, sort_keys=True, separators=(",", ":"))


def route_task_id(task: str) -> str:
    """Return a stable task_id for route_task → record_outcome correlation."""
    digest = hashlib.sha256(task.encode()).hexdigest()[:16]
    return f"route-{digest}"


def _latest_telemetry_context(db: Database, task_id: str) -> dict[str, Any]:
    with db.conn() as conn:
        row = conn.execute(
            """
            SELECT id, tier, model, provider_name, complexity_score
            FROM telemetry
            WHERE task_hash = ?
            ORDER BY ts DESC, id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()

    if row is None:
        return {
            "telemetry_id": None,
            "tier": None,
            "model": None,
            "provider": None,
            "complexity_score": None,
        }

    telemetry_id, tier, model, provider_name, complexity_score = row
    return {
        "telemetry_id": telemetry_id,
        "tier": tier,
        "model": model,
        "provider": provider_name,
        "complexity_score": complexity_score,
    }


def _default_complexity_score_for_tier(tier: str) -> float:
    """Generate a default complexity score based on tier."""
    tier_scores = {
        "low": 0.25,
        "medium": 0.50,
        "high": 0.75,
    }
    return tier_scores.get(tier, 0.50)


def persist_route_telemetry(
    db: Database,
    *,
    task_id: str,
    tier: str,
    complexity_score: float,
    model: str | None = None,
    provider: str | None = None,
    caller: str | None = None,
) -> int | None:
    """Persist a routing decision so record_outcome can correlate scores."""
    try:
        return db.log_agent_result(
            session_id=caller or "mcp",
            task_hash=task_id,
            agent_id=0,
            tier=tier,
            model=model or "",
            provider_name=provider or "mcp",
            complexity_score=float(complexity_score),
            reason="route_task",
            version="route",
        )
    except Exception:
        log.debug(
            "Failed to persist route telemetry for task %s",
            task_id,
            exc_info=True,
        )
        return None


def enqueue_learning_update(
    db: Database,
    task_id: str,
    outcome: str,
    project_id: str | None = None,
) -> None:
    """
    Enqueue a learning update for an outcome (non-blocking).
    
    Called by record_outcome() after the outcome row is stored.
    Performs checks:
    1. learning_enabled gate check via get_project_settings() (if project_id provided)
    2. Telemetry lookup to get tier
    3. routing_outcomes lookup to get complexity_score (uses tier-based default if missing)
    4. If missing tier, log debug and skip (non-breaking)
    5. Otherwise, INSERT into learning_queue with status='pending'
    
    This function is synchronous but the actual update_band() processing
    happens asynchronously in eval.py process_learning_queue().
    """
    # Step 1: Map outcome to success signal (positive/negative)
    success = outcome in ("accepted", "revised")  # True for positive
    
    # Step 2: Check learning_enabled gate only if project_id is provided
    # (Without project context, we can't properly gate, so we proceed)
    if project_id is not None:
        settings = db.get_project_settings(project_id) or {}
        learning_enabled = settings.get("learning_enabled", False)
        if not learning_enabled:
            log.debug(
                "Learning disabled for project %s; skipping enqueue for task %s",
                project_id,
                task_id,
            )
            return
    
    # Step 3: Look up telemetry to get tier
    with db.conn() as conn:
        telemetry_row = conn.execute(
            """
            SELECT tier FROM telemetry
            WHERE task_hash = ?
            ORDER BY ts DESC, id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        
        # Also look up routing_outcomes to get complexity_score
        outcomes_row = conn.execute(
            """
            SELECT complexity_score FROM routing_outcomes
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
    
    if telemetry_row is None:
        log.debug(
            "Outcome %s has no telemetry row; skipping adaptive update",
            task_id,
        )
        return
    
    tier = telemetry_row[0]
    if not tier:
        log.debug(
            "Outcome %s has telemetry row without tier; skipping adaptive update",
            task_id,
        )
        return
    
    # Get complexity_score from routing_outcomes, or use tier-based default
    complexity_score = (
        outcomes_row[0]
        if outcomes_row and outcomes_row[0] is not None
        else _default_complexity_score_for_tier(tier)
    )
    try:
        complexity_score = float(complexity_score)
    except (TypeError, ValueError):
        complexity_score = _default_complexity_score_for_tier(str(tier))
    
    # Step 4: Insert into learning_queue (non-blocking)
    now = time.time()
    try:
        with db.conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO learning_queue
                (task_id, tier, complexity_score, success, status, enqueued_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (task_id, tier, complexity_score, success, now),
            )
        if project_id:
            try:
                from .adaptive import register_observation

                register_observation(
                    db,
                    project_id,
                    {
                        "rework_count": 0,
                        "token_cost": 0,
                        "success": success,
                        "timestamp": now,
                    },
                )
            except Exception:
                log.debug(
                    "Failed to register project observation for %s",
                    project_id,
                    exc_info=True,
                )
        log.debug(
            "Enqueued learning update for task %s: tier=%s score=%s success=%s",
            task_id,
            tier,
            complexity_score,
            success,
        )
    except Exception as e:
        log.error("Failed to enqueue learning update for task %s: %s", task_id, e)


def record_outcome(
    db: Database,
    task_id: str,
    outcome: str,
    operator_id: str | None = None,
    note: str | None = None,
    project_id: str | None = None,
    gate_verdict: str | None = None,
) -> dict[str, Any]:
    normalized_task_id = _normalize_required_string(task_id, "task_id")
    normalized_outcome = _normalize_outcome(outcome)
    normalized_operator_id = _normalize_recorded_operator_id(operator_id)
    normalized_note = _normalize_optional_string(note, "note")
    normalized_gate_verdict = gate_verdict if gate_verdict in ("pass", "warn", "block", "rejected") else None
    recorded_at = time.time()

    try:
        with db.conn() as conn:
            prior = conn.execute(
                """
                SELECT current_outcome, created_at
                FROM routing_outcomes
                WHERE task_id = ?
                """,
                (normalized_task_id,),
            ).fetchone()

            previous_outcome = prior[0] if prior else None
            created_at = (
                _coerce_created_at(prior[1], fallback=recorded_at) if prior else recorded_at
            )

            if prior and (recorded_at - created_at) > OUTCOME_READONLY_WINDOW_SECONDS:
                raise OutcomeReadonlyWindowError(
                    f"task '{normalized_task_id}' is outside the 7-day correction window"
                )

            if prior:
                conn.execute(
                    """
                    UPDATE routing_outcomes
                    SET previous_outcome = ?,
                        current_outcome = ?,
                        recorded_at = ?,
                        tier = NULL,
                        model = NULL,
                        provider_name = NULL,
                        complexity_score = NULL,
                        telemetry_id = NULL,
                        last_modified_by = ?,
                        gate_verdict = ?
                    WHERE task_id = ?
                    """,
                    (
                        previous_outcome,
                        normalized_outcome,
                        recorded_at,
                        normalized_operator_id,
                        normalized_gate_verdict,
                        normalized_task_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO routing_outcomes (
                        task_id,
                        current_outcome,
                        previous_outcome,
                        recorded_at,
                        tier,
                        model,
                        provider_name,
                        complexity_score,
                        telemetry_id,
                        last_modified_by,
                        created_at,
                        gate_verdict
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_task_id,
                        normalized_outcome,
                        None,
                        recorded_at,
                        None,
                        None,
                        None,
                        None,
                        None,
                        normalized_operator_id,
                        created_at,
                        normalized_gate_verdict,
                    ),
                )

            conn.execute(
                """
                INSERT INTO routing_outcome_audit (
                    task_id,
                    outcome,
                    operator_id,
                    note,
                    recorded_at,
                    previous_outcome
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_task_id,
                    normalized_outcome,
                    normalized_operator_id,
                    normalized_note,
                    recorded_at,
                    previous_outcome,
                ),
            )

        telemetry_context = _latest_telemetry_context(db, normalized_task_id)
        with db.conn() as conn:
            conn.execute(
                """
                UPDATE routing_outcomes
                SET tier = ?,
                    model = ?,
                    provider_name = ?,
                    complexity_score = ?,
                    telemetry_id = ?
                WHERE task_id = ?
                """,
                (
                    telemetry_context["tier"],
                    telemetry_context["model"],
                    telemetry_context["provider"],
                    telemetry_context["complexity_score"],
                    telemetry_context["telemetry_id"],
                    normalized_task_id,
                ),
            )

        snapshot = {
            "current_outcome": normalized_outcome,
            "recorded_at": recorded_at,
            "previous_outcome": previous_outcome,
            "tier": telemetry_context["tier"],
            "model": telemetry_context["model"],
            "provider": telemetry_context["provider"],
            "complexity_score": telemetry_context["complexity_score"],
        }
        try:
            memory_set(
                "task",
                OUTCOME_MEMORY_KEY,
                snapshot,
                project_id=OUTCOME_MEMORY_PROJECT_ID,
                task_id=normalized_task_id,
                db=db,
            )
        except Exception:
            log.warning(
                "Failed to update outcome memory snapshot for %s",
                normalized_task_id,
                exc_info=True,
            )

        # Enqueue learning signal (non-blocking, reuses warm-path executor)
        try:
            enqueue_learning_update(
                db,
                normalized_task_id,
                normalized_outcome,
                project_id=project_id,
            )
        except Exception as e:
            log.warning("Failed to enqueue learning update: %s", e)
            # Do not raise; outcome recording is primary concern

        return {"stored": True, "task_id": normalized_task_id}

    except OutcomeReadonlyWindowError:
        raise
    except Exception as e:
        log.debug("Backend failure in record_outcome", exc_info=True)
        raise ValueError(f"Failed to record outcome: {e}") from e


def record_swarm_outcome(
    db: Database,
    swarm_id: str,
    outcome: str,
    *,
    selected_topology: str = "star",
    coordinator_round_count: int = 0,
    artifact_consume_count: int = 0,
    coordinator_amendment_count: int = 0,
    operator_id: str | None = None,
    note: str | Mapping[str, object] | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    normalized_swarm_id = _normalize_required_string(swarm_id, "swarm_id")
    normalized_outcome = _normalize_outcome(outcome)
    normalized_topology = _normalize_required_string(
        selected_topology,
        "selected_topology",
    )
    normalized_note = _normalize_swarm_outcome_note(note)
    telemetry_id: int | None = None
    try:
        telemetry_id = db.log_agent_result(
            session_id=normalized_swarm_id,
            task_hash=normalized_swarm_id,
            agent_id=0,
            tier="coordinator",
            model=f"{normalized_topology}-coordinator",
            success=normalized_outcome in {"accepted", "revised"},
            rework=normalized_outcome == "reworked",
            provider_name="swarm",
            reason=normalized_note or f"swarm:{normalized_topology}:{normalized_outcome}",
            selected_topology=normalized_topology,
            artifact_consume_count=_normalize_non_negative_int(
                artifact_consume_count,
                "artifact_consume_count",
            ),
            coordinator_round_count=_normalize_non_negative_int(
                coordinator_round_count,
                "coordinator_round_count",
            ),
            coordinator_amendment_count=_normalize_non_negative_int(
                coordinator_amendment_count,
                "coordinator_amendment_count",
            ),
        )
    except Exception:
        log.warning(
            "Failed to record swarm telemetry for %s",
            normalized_swarm_id,
            exc_info=True,
        )
    try:
        return record_outcome(
            db,
            normalized_swarm_id,
            normalized_outcome,
            operator_id=operator_id,
            note=normalized_note,
            project_id=project_id,
        )
    except Exception:
        if telemetry_id is not None:
            try:
                with db.conn() as conn:
                    conn.execute("DELETE FROM telemetry WHERE id = ?", (telemetry_id,))
            except Exception:
                log.warning(
                    "Failed to roll back swarm telemetry for %s",
                    normalized_swarm_id,
                    exc_info=True,
                )
        raise


def compute_learning_outcome_snapshot(db: Database) -> None:
    """
    Compute a 1-hour outcome distribution snapshot and store it in memory.
    
    Aggregates outcomes by (tier, model) pairs over the last 1 hour (3600 seconds).
    Calculates coverage as percentage of tasks with feedback.
    Stores result in memory using memory_set("project", "learning_stats", ...).
    
    Called by warm-path executor as a background task (fire-and-forget).
    Logs errors but returns gracefully (never raises).
    """
    cutoff = time.time() - 3600  # 1 hour ago
    
    try:
        with db.conn() as conn:
            # Query outcomes in 1-hour window, ordered by (tier, model)
            outcomes = conn.execute(
                """
                SELECT current_outcome, tier, model 
                FROM routing_outcomes 
                WHERE recorded_at >= ?
                ORDER BY tier, model
                """,
                (cutoff,),
            ).fetchall()
            
            # Aggregate outcomes by (tier, model) pair using itertools.groupby
            outcome_distribution = {}
            if outcomes:
                for (tier, model), group in itertools.groupby(
                    outcomes, key=lambda r: (r[1], r[2])  # group by (tier, model)
                ):
                    group_list = list(group)
                    tier_model_key = f"{tier}:{model}" if tier and model else "unknown:unknown"
                    
                    # Count outcomes by type
                    counts = {
                        "accepted": sum(1 for o in group_list if o[0] == "accepted"),
                        "revised": sum(1 for o in group_list if o[0] == "revised"),
                        "rejected": sum(1 for o in group_list if o[0] == "rejected"),
                        "reworked": sum(1 for o in group_list if o[0] == "reworked"),
                    }
                    outcome_distribution[tier_model_key] = counts
            
            # Query total task count in window
            total_tasks_row = conn.execute(
                """
                SELECT COUNT(*) FROM telemetry WHERE ts >= ?
                """,
                (cutoff,),
            ).fetchone()
            total_tasks = total_tasks_row[0] if total_tasks_row else 0
            
            # Query task count with feedback in window
            tasks_with_feedback_row = conn.execute(
                """
                SELECT COUNT(*) FROM routing_outcomes WHERE recorded_at >= ?
                """,
                (cutoff,),
            ).fetchone()
            tasks_with_feedback = tasks_with_feedback_row[0] if tasks_with_feedback_row else 0
            
            # Calculate coverage percentage
            coverage_percentage = None
            if total_tasks > 0:
                coverage_percentage = (tasks_with_feedback / total_tasks) * 100
        
        # Build snapshot JSON payload
        now = time.time()
        snapshot = {
            "window_start_time": cutoff,
            "window_end_time": now,
            "outcome_distribution": outcome_distribution,
            "coverage_percentage": coverage_percentage,
            "total_tasks_in_window": total_tasks,
            "tasks_with_feedback": tasks_with_feedback,
            "computed_at": now,
        }
        
        # Store snapshot in memory (global scope with learning_stats key)
        memory_set("global", "learning_stats", snapshot, db=db)
        
        log.info(
            "Computed outcome snapshot: %d tasks, %.1f%% coverage",
            total_tasks,
            coverage_percentage if coverage_percentage is not None else 0,
        )
    
    except Exception as e:
        log.error("Outcome snapshot computation failed: %s", e, exc_info=True)
        # Return gracefully (no raise)


__all__ = [
    "OUTCOME_ALLOWLIST",
    "ANONYMOUS_OPERATOR_ID",
    "OUTCOME_MEMORY_KEY",
    "OUTCOME_MEMORY_PROJECT_ID",
    "OUTCOME_READONLY_WINDOW_SECONDS",
    "OUTCOME_VALUES",
    "OutcomeReadonlyWindowError",
    "persist_route_telemetry",
    "route_task_id",
    "record_outcome",
    "record_swarm_outcome",
    "enqueue_learning_update",
    "compute_learning_outcome_snapshot",
]
