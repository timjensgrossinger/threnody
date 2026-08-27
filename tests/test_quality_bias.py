#!/usr/bin/env python3
"""Tests for shared/quality_bias.py — objective quality ledger → tier bias."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.db import Database
from shared.quality_bias import (
    apply_quality_floor,
    load_kind_quality_bias,
    load_model_quality_bias,
)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "quality-bias.db")
    database._init_schema(database._get_connection())
    yield database
    database.close()


def _event(
    db: Database,
    *,
    model: str,
    dimension: str,
    score: float,
    source: str,
    sub_dimension: str | None = None,
    sample_meta: dict | None = None,
    run_id: str | None = "swarm-test",
    kind: str | None = None,
) -> None:
    """Insert one ledger row.

    ``run_id`` defaults to a non-empty value because ``load_model_quality_bias``
    only considers rows with provenance — a real observation always carries the
    run it came from, and requiring it is what stops a stray fixture row from
    moving a production tier decision. Pass ``run_id=None`` to exercise that
    guard.
    """
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO model_quality_events "
            "(model, effort, dimension, sub_dimension, score_0_10, source, "
            "sample_meta, task_hash, run_id, kind, ts) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                model,
                dimension,
                sub_dimension,
                score,
                source,
                json.dumps(sample_meta) if sample_meta else None,
                run_id,
                kind,
                time.time(),
            ),
        )


def test_empty_ledger_yields_no_bias(db: Database) -> None:
    assert load_model_quality_bias(db) == {}


def test_rows_without_provenance_never_move_tiers(db: Database) -> None:
    """A ledger row with no run_id cannot have come from a real run.

    This is the guard that stops leaked fixtures from steering routing. The
    reference install accumulated 467 rows of which only 31 carried a run_id —
    test data that reached the live DB through the learning journal and was fully
    eligible to change a production tier decision.
    """
    for run_id in (None, "", "   "):
        for _ in range(10):
            _event(db, model="m1", dimension="logic", score=0.0,
                   source="verify_gate", run_id=run_id)
        assert load_model_quality_bias(db) == {}, f"run_id={run_id!r} must be ignored"

    # Same rows, same scores, with provenance → the bias appears.
    for _ in range(10):
        _event(db, model="m1", dimension="logic", score=0.0,
               source="verify_gate", run_id="swarm-real")
    assert load_model_quality_bias(db) == {("m1", "logic"): 1}


def test_proxy_sources_never_move_tiers(db: Database) -> None:
    """findings/judge are inference, not ground truth — they must not route."""
    for _ in range(10):
        _event(db, model="m1", dimension="security", score=0.0, source="findings")
        _event(db, model="m1", dimension="security", score=0.0, source="judge")
    assert load_model_quality_bias(db) == {}


def test_below_min_samples_is_ignored(db: Database) -> None:
    for _ in range(3):
        _event(db, model="m1", dimension="logic", score=1.0, source="verify_gate")
    assert load_model_quality_bias(db, min_samples=4) == {}


def test_low_objective_score_escalates(db: Database) -> None:
    for _ in range(5):
        _event(db, model="m1", dimension="logic", score=2.0, source="verify_gate")
    assert load_model_quality_bias(db) == {("m1", "logic"): 1}


def test_high_objective_score_de_escalates(db: Database) -> None:
    for _ in range(5):
        _event(db, model="m1", dimension="security", score=10.0, source="static_recall")
    assert load_model_quality_bias(db) == {("m1", "security"): -1}


def test_midrange_score_yields_no_bias(db: Database) -> None:
    for _ in range(6):
        _event(db, model="m1", dimension="types", score=6.5, source="verify_gate")
    assert load_model_quality_bias(db) == {}


def test_sources_are_pooled_per_model_dimension(db: Database) -> None:
    """All objective sources grade the same thing — they aggregate together."""
    for _ in range(3):
        _event(db, model="m1", dimension="logic", score=1.0, source="verify_gate")
        _event(db, model="m1", dimension="logic", score=1.0, source="ladder")
    assert load_model_quality_bias(db) == {("m1", "logic"): 1}


def test_ladder_floor_blocks_de_escalation(db: Database) -> None:
    """A model with a graded floor above low must not be quietly cheapened."""
    for _ in range(5):
        _event(db, model="m1", dimension="logic", score=10.0, source="static_recall")
    bias = load_model_quality_bias(db)
    assert bias == {("m1", "logic"): -1}

    # Ladder: low FAILED the level, high passed it -> floor is above low.
    _event(
        db,
        model="m1",
        dimension="general",
        sub_dimension="L3",
        score=0.0,
        source="ladder",
        sample_meta={"tier": "low", "case_id": "c1"},
    )
    _event(
        db,
        model="m1",
        dimension="general",
        sub_dimension="L3",
        score=10.0,
        source="ladder",
        sample_meta={"tier": "high", "case_id": "c1"},
    )
    assert apply_quality_floor(db, bias) == {}


def test_ladder_floor_allows_de_escalation_when_the_model_sweeps_something_cheaply(
    db: Database,
) -> None:
    """A hard level a model cannot do cheaply must not veto every de-escalation.

    The veto used to fire on *any* floor above low, which is nearly every graded
    model — one `L6 -> high` result blanket-banned de-escalation for that model in
    every dimension, so graded evidence acted as a ban rather than as evidence.
    """
    for _ in range(5):
        _event(db, model="m1", dimension="logic", score=10.0, source="static_recall")
    assert load_model_quality_bias(db) == {("m1", "logic"): -1}

    # Hard level: only high sweeps it.
    _event(db, model="m1", dimension="general", sub_dimension="L6", score=10.0,
           source="ladder", sample_meta={"tier": "high", "case_id": "hard"})
    # Easy level: low sweeps it — so the cheap tier is demonstrably viable here.
    _event(db, model="m1", dimension="general", sub_dimension="L0", score=10.0,
           source="ladder", sample_meta={"tier": "low", "case_id": "easy"})

    bias = load_model_quality_bias(db)
    assert apply_quality_floor(db, bias) == {("m1", "logic"): -1}


def test_ladder_floor_without_any_graded_evidence_is_a_no_op(db: Database) -> None:
    """No ladder rows at all must not silently suppress a de-escalation."""
    for _ in range(5):
        _event(db, model="m1", dimension="logic", score=10.0, source="static_recall")
    bias = load_model_quality_bias(db)
    assert apply_quality_floor(db, bias) == {("m1", "logic"): -1}


def test_ladder_floor_never_filters_escalation(db: Database) -> None:
    for _ in range(5):
        _event(db, model="m1", dimension="logic", score=1.0, source="verify_gate")
    _event(
        db,
        model="m1",
        dimension="general",
        sub_dimension="L3",
        score=10.0,
        source="ladder",
        sample_meta={"tier": "high", "case_id": "c1"},
    )
    bias = load_model_quality_bias(db)
    assert apply_quality_floor(db, bias) == {("m1", "logic"): 1}


# ---------------------------------------------------------------------------
# task-kind axis
# ---------------------------------------------------------------------------

def test_kind_bias_empty_without_kind_rows(db: Database) -> None:
    """Rows with no `kind` belong to the dimension axis and must not appear here."""
    for _ in range(10):
        _event(db, model="m1", dimension="logic", score=10.0, source="verify_gate")
    assert load_kind_quality_bias(db) == {}


def test_kind_bias_reads_the_kind_column(db: Database) -> None:
    """Graded ladder evidence per task kind becomes a tier step.

    Ladder rows write `dimension='general'`, which no consumer matches — so before
    the `kind` column existed the one source with a real reference solution could
    never influence a routing decision.
    """
    # Swept every graded xss-fix case -> cheaper tier is worth trying.
    for i in range(5):
        _event(db, model="m1", dimension="general", sub_dimension="L2", score=10.0,
               source="ladder", kind="xss-fix", run_id=f"ladder-{i}")
    # Failed every graded refactor case -> escalate.
    for i in range(5):
        _event(db, model="m1", dimension="general", sub_dimension="L5", score=0.0,
               source="ladder", kind="refactor", run_id=f"ladder-{i}")
    assert load_kind_quality_bias(db) == {
        ("m1", "xss-fix"): -1,
        ("m1", "refactor"): 1,
    }


def test_kind_bias_respects_min_samples(db: Database) -> None:
    for i in range(2):
        _event(db, model="m1", dimension="general", score=10.0,
               source="ladder", kind="xss-fix", run_id=f"ladder-{i}")
    assert load_kind_quality_bias(db) == {}


def test_kind_bias_ignores_proxy_sources(db: Database) -> None:
    """findings/judge are inference; only ground truth may move a tier."""
    for i in range(10):
        _event(db, model="m1", dimension="security", score=10.0,
               source="findings", kind="xss-fix", run_id=f"r{i}")
    assert load_kind_quality_bias(db) == {}


def test_kind_bias_requires_provenance(db: Database) -> None:
    for _ in range(10):
        _event(db, model="m1", dimension="general", score=10.0,
               source="ladder", kind="xss-fix", run_id=None)
    assert load_kind_quality_bias(db) == {}
