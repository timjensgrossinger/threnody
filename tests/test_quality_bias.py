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
from shared.quality_bias import apply_quality_floor, load_model_quality_bias


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
) -> None:
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO model_quality_events "
            "(model, effort, dimension, sub_dimension, score_0_10, source, "
            "sample_meta, task_hash, run_id, ts) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                model,
                dimension,
                sub_dimension,
                score,
                source,
                json.dumps(sample_meta) if sample_meta else None,
                time.time(),
            ),
        )


def test_empty_ledger_yields_no_bias(db: Database) -> None:
    assert load_model_quality_bias(db) == {}


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
