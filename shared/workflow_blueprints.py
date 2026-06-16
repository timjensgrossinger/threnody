"""Replayable host-native workflow blueprints."""
from __future__ import annotations

from copy import deepcopy
import re
import time
from typing import Any, Mapping

from .db import Database

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def slugify_blueprint_name(name: str) -> str:
    slug = _SLUG_RE.sub("-", str(name or "").strip().lower()).strip("-._")
    if not slug:
        raise ValueError("blueprint name is required")
    return slug[:80]


def export_blueprint_from_receipt(
    db: Database,
    *,
    run_id: str,
    name: str | None = None,
) -> dict[str, Any]:
    row = db.get_run_receipt(run_id)
    if row is None:
        raise KeyError(run_id)
    receipt = row.get("receipt") if isinstance(row.get("receipt"), dict) else {}
    blueprint_name = slugify_blueprint_name(name or str(receipt.get("source_tool") or run_id))
    plan = receipt.get("plan") if isinstance(receipt.get("plan"), Mapping) else {}
    waves = receipt.get("host_spawn_waves")
    if not isinstance(waves, list):
        waves = []
    blueprint = {
        "name": blueprint_name,
        "source_run_id": run_id,
        "created_ts": time.time(),
        "source_tool": receipt.get("source_tool"),
        "topology": receipt.get("topology"),
        "plan": plan,
        "host_spawn_waves": waves,
        "inputs_schema": {
            "task": "string optional; replaces {{task}} placeholders when present",
            "replacements": "object optional; literal string replacements",
        },
    }
    db.record_workflow_blueprint(blueprint_name, run_id=run_id, blueprint=blueprint)
    return blueprint


def run_workflow_blueprint(
    db: Database,
    *,
    name: str,
    inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    slug = slugify_blueprint_name(name)
    row = db.get_workflow_blueprint(slug)
    if row is None:
        raise KeyError(slug)
    blueprint = row.get("blueprint") if isinstance(row.get("blueprint"), dict) else {}
    inputs = inputs or {}
    replacements: dict[str, str] = {}
    task_value = inputs.get("task")
    if task_value is not None:
        replacements["{{task}}"] = str(task_value)
    raw_replacements = inputs.get("replacements")
    if isinstance(raw_replacements, Mapping):
        for key, value in raw_replacements.items():
            replacements[str(key)] = str(value)

    def replace_value(value: Any) -> Any:
        if isinstance(value, str):
            out = value
            for old, new in replacements.items():
                out = out.replace(old, new)
            return out
        if isinstance(value, list):
            return [replace_value(v) for v in value]
        if isinstance(value, dict):
            return {k: replace_value(v) for k, v in value.items()}
        return value

    return {
        "blueprint": slug,
        "source_run_id": blueprint.get("source_run_id"),
        "replayed": True,
        "planning_tokens_saved": True,
        "plan": replace_value(deepcopy(blueprint.get("plan") or {})),
        "host_spawn_waves": replace_value(deepcopy(blueprint.get("host_spawn_waves") or [])),
        "execution_note": "Replay these host_spawn_waves in the host; no planner call was needed.",
    }


__all__ = [
    "export_blueprint_from_receipt",
    "run_workflow_blueprint",
    "slugify_blueprint_name",
]
