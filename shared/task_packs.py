"""Curated task packs: small reusable planning presets instead of giant agent catalogs."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .heuristic_plan import build_heuristic_plan_payload

TASK_PACKS: dict[str, dict[str, Any]] = {
    "test-gap": {
        "description": "Find missing tests, then add focused regression coverage.",
        "prefix": "Audit test gaps, identify risky uncovered behavior, and implement focused tests.",
        "default_tier": "medium",
        "verification": ["run affected tests", "run full hermetic suite when shared code changes"],
    },
    "security-review": {
        "description": "Review inputs, auth, filesystem, subprocess, and secret handling.",
        "prefix": (
            "Perform a security review first; prioritize concrete exploit paths "
            "and safe fixes. Use high tier only for explicit deep/security-critical "
            "review or concrete exploit primitives."
        ),
        "default_tier": "medium",
        "read_only_first": True,
    },
    "docs-sync": {
        "description": "Synchronize README, docs, examples, and tool descriptions.",
        "prefix": "Synchronize documentation with implemented behavior and keep claims evidence-backed.",
        "default_tier": "low",
    },
    "release-check": {
        "description": "Run release hygiene checks, archive checks, and compatibility smoke tests.",
        "prefix": "Perform release readiness checks, update checklist/docs, and record verification.",
        "default_tier": "medium",
    },
    "frontend-smoke": {
        "description": "Verify UI rendering, layout, and interaction smoke paths.",
        "prefix": "Run frontend smoke verification across desktop and mobile viewports.",
        "default_tier": "medium",
    },
    "migration-plan": {
        "description": "Plan and stage a migration with compatibility and rollback notes.",
        "prefix": "Create a migration plan, identify compatibility risks, then implement the safest slice.",
        "default_tier": "high",
    },
}


def _build_waves(subtasks: list[dict[str, Any]]) -> list[list[Any]]:
    known_ids = [st.get("id") for st in subtasks if st.get("id") is not None]
    known = set(known_ids)
    remaining: dict[Any, set[Any]] = {}
    for st in subtasks:
        sid = st.get("id")
        if sid is None:
            continue
        deps = st.get("depends_on") or []
        remaining[sid] = {dep for dep in deps if dep in known} if isinstance(deps, list) else set()
    completed: set[Any] = set()
    waves: list[list[Any]] = []
    while remaining:
        ready = [sid for sid, deps in remaining.items() if deps.issubset(completed)]
        if not ready:
            waves.append(list(remaining))
            break
        waves.append(ready)
        for sid in ready:
            del remaining[sid]
            completed.add(sid)
    return waves or [known_ids]


def list_task_packs() -> list[dict[str, Any]]:
    return [
        {"name": name, **deepcopy(meta)}
        for name, meta in sorted(TASK_PACKS.items())
    ]


def plan_task_pack(pack: str, task: str, *, max_agents: int | None = None) -> dict[str, Any]:
    name = str(pack or "").strip().lower()
    if name not in TASK_PACKS:
        raise ValueError(f"unknown task pack: {pack}")
    meta = TASK_PACKS[name]
    packed_task = f"{meta['prefix']}\n\nUser task: {task.strip()}"
    payload = build_heuristic_plan_payload(
        packed_task,
        default_tier=str(meta.get("default_tier") or "medium"),
        max_agents=max_agents,
    )
    subtasks = payload.get("subtasks")
    if isinstance(subtasks, list) and "waves" not in payload:
        payload["waves"] = _build_waves([st for st in subtasks if isinstance(st, dict)])
    payload["task_pack"] = {"name": name, **deepcopy(meta)}
    payload["planner_host_execution_mode"] = "host_native"
    return payload


__all__ = ["TASK_PACKS", "list_task_packs", "plan_task_pack"]
