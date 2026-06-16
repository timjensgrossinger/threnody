"""Cheap agent-count optimizer for host-native planning."""
from __future__ import annotations

from typing import Any

from .heuristic_plan import assess_task_complexity, extract_task_file_entries


def choose_agent_count(task: str, *, requested: int | None = None, hard_cap: int = 12) -> dict[str, Any]:
    """Recommend the smallest agent count likely to help.

    Explicit user requests remain the ceiling input; the optimizer explains rather
    than silently expanding. This is intentionally conservative: Threnody should
    prove when extra agents are useful, not assume it.
    """
    try:
        raw_cap = int(hard_cap)
    except (TypeError, ValueError):
        raw_cap = 12
    cap = raw_cap if raw_cap > 0 else None

    def _cap(value: int) -> int:
        return min(value, cap) if cap is not None else value

    if requested is not None:
        try:
            requested_int = max(1, int(requested))
        except (TypeError, ValueError):
            requested_int = cap or 1
        return {
            "recommended_agents": _cap(requested_int),
            "strategy": "user_requested",
            "rationale": "Honoring explicit max_agents subject to configured cap.",
        }
    complexity = assess_task_complexity(task)
    entries = extract_task_file_entries(task, intent_templates=True) if isinstance(task, str) else []
    file_count = len(entries)
    task_lower = task.lower() if isinstance(task, str) else ""
    if file_count <= 1 and not complexity.get("complex"):
        recommended = 1
        strategy = "single_agent"
        rationale = "Single-file or low-coupling task; extra agents add coordination cost."
    elif "review" in task_lower or "security" in task_lower:
        if file_count >= 4:
            recommended = _cap(file_count + 1)
            strategy = "review_file_sweep"
            rationale = (
                "Large review detected; scale toward one file-level reviewer per file "
                "plus synthesis, bounded by swarm.max_agents only when a cap is configured."
            )
        else:
            recommended = _cap(max(2, file_count + 1))
            strategy = "two_agent_pair"
            rationale = "Small review/security work benefits from a checker plus synthesis."
    elif file_count >= 4 or complexity.get("complex"):
        recommended = _cap(max(2, min(file_count, 4)))
        strategy = "bounded_swarm"
        rationale = "Multi-file or coupled task; bounded fanout preserves parallelism without a large swarm."
    else:
        recommended = _cap(2)
        strategy = "two_agent_pair"
        rationale = "Moderate task; pair fanout is the cheapest useful parallel shape."
    return {
        "recommended_agents": recommended,
        "strategy": strategy,
        "rationale": rationale,
        "signals": {
            "file_count": file_count,
            **complexity,
        },
    }


__all__ = ["choose_agent_count"]
