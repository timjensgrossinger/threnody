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
        "prefix": "Perform a security review first; prioritize concrete exploit paths and safe fixes.",
        "default_tier": "high",
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
    payload["task_pack"] = {"name": name, **deepcopy(meta)}
    payload["planner_host_execution_mode"] = "host_native"
    return payload


__all__ = ["TASK_PACKS", "list_task_packs", "plan_task_pack"]
