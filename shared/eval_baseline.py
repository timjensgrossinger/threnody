"""
Deterministic baseline capture helper for Threnody eval suite.

Usage:
  python3 -m shared.eval_baseline [output_path]

Exports:
  capture_baseline(output_path: str = 'tests/eval/baseline.json') -> None

This module programmatically runs the in-repo routing evaluator in test mode
and writes a deterministic JSON baseline containing only stable, normalized
fields plus top-level metadata (config_hash, schema_version).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

from shared import routing_eval
from shared.config import load_eval_config


DEFAULT_OUTPUT = Path("tests/eval/baseline.json")


def _schema_version_from_file(schema_path: Path) -> str:
    try:
        with open(schema_path, encoding="utf-8") as f:
            d = json.load(f)
        # Prefer $id if present, else $schema, else fallback to "unknown"
        return d.get("$id") or d.get("$schema") or "unknown"
    except Exception:
        return "unknown"


def _compute_config_hash() -> str:
    cfg = load_eval_config()
    # Convert dataclass to plain mapping for deterministic serialization
    cfg_map = dataclasses.asdict(cfg)

    # Recursively convert non-JSON-serializable types (e.g., Path) to strings
    def _normalize(obj):
        if isinstance(obj, dict):
            return {k: _normalize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_normalize(v) for v in obj]
        try:
            from pathlib import Path as _Path
        except Exception:
            _Path = None
        if _Path is not None and isinstance(obj, _Path):
            return str(obj)
        return obj

    norm = _normalize(cfg_map)
    # Serialize deterministically
    text = json.dumps(norm, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _fixture_to_baseline_entry(fixture: dict[str, Any], router) -> dict[str, Any]:
    # Use router.classify to obtain a RoutingDecision
    decision = router.classify(fixture.get("prompt", ""))
    entry = {
        "id": fixture.get("id"),
        "category": fixture.get("category"),
        "tier": decision.tier,
        "score": decision.score,
        "urgency": bool(getattr(decision, "urgency_score", 0) > 0),
        "fanout": routing_eval._agents_to_fanout(getattr(decision, "agents", 1)),
    }
    return entry


def capture_baseline(output_path: str | Path = DEFAULT_OUTPUT) -> None:
    outp = Path(output_path)
    # Ensure test mode for any downstream code paths
    # Note: routing_eval.run_eval would set THRENODY_TEST_MODE, but we set again here
    import os

    os.environ.setdefault("THRENODY_TEST_MODE", "1")

    # Lazy-instantiate router same way as routing_eval.run_eval
    from shared.router import TaskRouter

    cfg = load_eval_config()
    router = TaskRouter(cfg, db=None)

    # Load fixtures deterministically
    fixtures = routing_eval.load_fixtures()

    fixtures_entries = []
    for f in fixtures:
        # strip internal helper keys like _source
        fixture_data = {k: v for k, v in f.items() if k != "_source"}
        fixtures_entries.append(_fixture_to_baseline_entry(fixture_data, router))

    # Top-level metadata
    schema_version = _schema_version_from_file(routing_eval.SCHEMA_PATH)
    config_hash = _compute_config_hash()

    payload = {
        "config_hash": config_hash,
        "schema_version": schema_version,
        "fixtures": fixtures_entries,
    }

    # Ensure parent dir exists
    outp.parent.mkdir(parents=True, exist_ok=True)
    # Write deterministic JSON
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True, separators=(",",":"), ensure_ascii=False)


if __name__ == "__main__":
    import sys

    p = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUTPUT
    capture_baseline(p)
