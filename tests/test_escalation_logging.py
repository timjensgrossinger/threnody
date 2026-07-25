from __future__ import annotations

"""Tests for enriched escalation logging: both triggers write a row with
model/effort/reason and a non-empty task_hash (orchestrator.py)."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.config import TGsConfig
from shared.db import Database
from shared.orchestrator import Orchestrator, Provider
from shared.planner import Planner, Subtask


class _PlaceholderProvider(Provider):
    """Always returns short placeholder output → fires the quality retry trigger."""

    def resolve_model(self, tier: str) -> str:
        return f"dummy-{tier}"

    def execute(self, subtask: Subtask, model: str, timeout: int = 120) -> str | None:
        return "def f():\n    pass"

    def available_tiers(self) -> list[str]:
        return ["low", "medium", "high"]


class _DummyPlanner(Planner):
    def __init__(self) -> None:
        self._backend = SimpleNamespace(call=lambda *a, **k: None)

    def plan(self, *args, **kwargs):
        raise NotImplementedError


def _escalation_rows(db: Database) -> list[dict]:
    with db.conn() as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(escalations)").fetchall()]
        rows = conn.execute("SELECT * FROM escalations").fetchall()
    return [dict(zip(cols, r)) for r in rows]


def test_escalations_table_has_new_columns(tmp_path: Path) -> None:
    db = Database(tmp_path / "e.db")
    with db.conn() as conn:
        cols = {c[1] for c in conn.execute("PRAGMA table_info(escalations)").fetchall()}
    assert {"from_model", "to_model", "effort", "reason"} <= cols


def test_log_escalation_persists_new_fields(tmp_path: Path) -> None:
    db = Database(tmp_path / "e.db")
    db.log_escalation(
        task_hash="abc", agent_id=1, from_tier="low", to_tier="medium",
        token_count=10, ceiling=5, from_model="haiku", to_model="sonnet",
        effort="medium", reason="token_ceiling",
    )
    row = _escalation_rows(db)[0]
    assert row["from_model"] == "haiku"
    assert row["to_model"] == "sonnet"
    assert row["effort"] == "medium"
    assert row["reason"] == "token_ceiling"


def test_quality_trigger_logs_escalation(tmp_path: Path) -> None:
    db = Database(tmp_path / "e.db")
    cfg = TGsConfig()
    cfg.output_quality_retry_enabled = True
    cfg.provider_effort_defaults = {"dummyprov": {"low": "high"}}
    orch = Orchestrator(cfg, _PlaceholderProvider(), _DummyPlanner(), db=db)

    subtask = Subtask(
        id=7, stable_id="phase-esc-01", description="write a helper",
        tier="low", model="low", provider_id="dummyprov", depends_on=[],
    )
    orch.execute_subtask(subtask, timeout=5)

    rows = _escalation_rows(db)
    assert rows, "expected an escalation row from the quality trigger"
    row = rows[0]
    # The previously-silent quality path now logs model/effort/reason + real hash.
    assert row["task_hash"], "task_hash must not be empty"
    assert row["reason"] == "placeholder_code"
    assert row["from_tier"] == "low" and row["to_tier"] == "medium"
    assert row["from_model"] == "dummy-low"
    assert row["to_model"] == "dummy-medium"
    assert row["effort"] == "high"


def test_effort_none_when_provider_id_unknown(tmp_path: Path) -> None:
    db = Database(tmp_path / "e.db")
    cfg = TGsConfig()
    cfg.output_quality_retry_enabled = True
    orch = Orchestrator(cfg, _PlaceholderProvider(), _DummyPlanner(), db=db)
    subtask = Subtask(
        id=8, stable_id="phase-esc-02", description="write a helper",
        tier="low", model="low", depends_on=[],
    )  # no provider_id -> effort must stay None, never a mis-resolved guess
    orch.execute_subtask(subtask, timeout=5)
    rows = _escalation_rows(db)
    assert rows and rows[0]["effort"] is None
