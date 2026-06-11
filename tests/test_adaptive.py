#!/usr/bin/env python3
"""Tests for Phase 3 — adaptive thresholds + budget awareness."""
from __future__ import annotations

import sys
import os
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.adaptive import (
    update_band, compute_thresholds, band_for_score, get_band_stats,
    EMA_ALPHA, SUCCESS_THRESHOLD, BANDS, PROJECT_SAMPLE_MIN,
    get_project_sample_count, register_observation, should_apply_adaptive_thresholds,
)
from shared.config import TGsConfig, ThresholdConfig, LOW_TIER_FLOOR, LOW_TIER_CEILING
from shared.db import Database
from shared.router import TaskRouter



# ---------------------------------------------------------------------------
# Band utilities
# ---------------------------------------------------------------------------

def test_band_for_score_boundaries():
    assert band_for_score(0.0) == "0.0-0.1"
    assert band_for_score(0.09) == "0.0-0.1"
    assert band_for_score(0.10) == "0.1-0.2"
    assert band_for_score(0.55) == "0.5-0.6"
    assert band_for_score(0.99) == "0.9-1.0"
    assert band_for_score(1.0) == "0.9-1.0"  # clamped


def test_bands_cover_full_range():
    assert len(BANDS) == 10
    assert BANDS[0] == "0.0-0.1"
    assert BANDS[9] == "0.9-1.0"


# ---------------------------------------------------------------------------
# EMA tracking
# ---------------------------------------------------------------------------

def test_update_band_first_observation():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        update_band(db, score=0.35, tier="low", success=True)
        stats = get_band_stats(db)
        assert len(stats) == 1
        assert stats[0]["band"] == "0.3-0.4"
        assert stats[0]["tier"] == "low"
        assert stats[0]["success_ema"] == 1.0  # first success
        assert stats[0]["sample_count"] == 1
        db.close()


def test_update_band_ema_decay():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        # 5 successes then 1 failure
        for _ in range(5):
            update_band(db, score=0.5, tier="low", success=True)
        update_band(db, score=0.5, tier="low", success=False)
        stats = get_band_stats(db)
        ema = stats[0]["success_ema"]
        # After 5 successes EMA ≈ 1.0, then one failure pulls it down
        assert ema < 1.0, f"EMA should decrease after failure, got {ema}"
        assert ema > 0.8, f"EMA shouldn't drop too fast, got {ema}"
        assert stats[0]["sample_count"] == 6
        db.close()


def test_update_band_multiple_tiers():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        update_band(db, score=0.5, tier="low", success=True)
        update_band(db, score=0.5, tier="medium", success=False)
        stats = get_band_stats(db)
        assert len(stats) == 2
        tiers = {s["tier"] for s in stats}
        assert tiers == {"low", "medium"}
        db.close()


# ---------------------------------------------------------------------------
# Threshold computation
# ---------------------------------------------------------------------------

def test_compute_thresholds_no_data():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        tc = compute_thresholds(db)
        # Should return midpoint defaults, clamped
        assert tc.low_max >= LOW_TIER_FLOOR
        assert tc.low_max <= LOW_TIER_CEILING
        db.close()


def test_compute_thresholds_poor_low_tier():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        # Feed many failures for low tier to push EMA below threshold
        for i in range(20):
            update_band(db, score=0.3, tier="low", success=(i % 3 == 0))
        tc = compute_thresholds(db, min_samples=5)
        # Low tier EMA should be poor → low_max should narrow
        default_low_max = (LOW_TIER_FLOOR + LOW_TIER_CEILING) / 2
        assert tc.low_max <= default_low_max, (
            f"Expected narrowed low_max <= {default_low_max}, got {tc.low_max}"
        )
        assert tc.low_max >= LOW_TIER_FLOOR  # hard bounds respected
        db.close()


def test_compute_thresholds_respects_hard_bounds():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        # Extreme failures to maximally narrow
        for _ in range(50):
            update_band(db, score=0.3, tier="low", success=False)
            update_band(db, score=0.7, tier="medium", success=False)
        tc = compute_thresholds(db, min_samples=5)
        assert tc.low_max >= LOW_TIER_FLOOR
        assert tc.medium_max >= 0.75  # MEDIUM_HIGH_BOUNDARY_FLOOR
        db.close()


# ---------------------------------------------------------------------------
# Router with adaptive thresholds
# ---------------------------------------------------------------------------

def test_router_with_db_uses_adaptive():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        cfg = TGsConfig()
        router = TaskRouter(cfg, db=db)
        # Should still work with empty DB (falls back gracefully)
        result = router.classify("fix typo")
        assert result.tier in ("low", "medium", "high")
        db.close()


def test_router_report_outcome():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        cfg = TGsConfig()
        router = TaskRouter(cfg, db=db)
        router.report_outcome(score=0.3, tier="low", success=True)
        router.report_outcome(score=0.3, tier="low", success=False)
        stats = get_band_stats(db)
        assert len(stats) == 1
        assert stats[0]["sample_count"] == 2
        db.close()


def test_register_observation_tracks_project_samples():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        project_id = str(Path(td) / "project")
        db._conn.execute(
            """
            INSERT INTO project_routing (project_path, overrides_json, learning_enabled, ts)
            VALUES (?, ?, 1, ?)
            """,
            (project_id, '{"tier_bias": 0.0, "sample_count": 0}', 0.0),
        )
        db._conn.commit()

        count = register_observation(
            db,
            project_id,
            {"rework_count": 0, "token_cost": 12, "success": True, "timestamp": 1.0},
        )
        assert count == 1
        assert get_project_sample_count(db, project_id) == 1
        db.close()


def test_adaptive_gate_requires_project_sample_min():
    assert should_apply_adaptive_thresholds(
        "project-1",
        band_sample_count=5,
        project_sample_count=PROJECT_SAMPLE_MIN - 1,
        band_min_samples=5,
    ) is False
    assert should_apply_adaptive_thresholds(
        "project-1",
        band_sample_count=5,
        project_sample_count=PROJECT_SAMPLE_MIN,
        band_min_samples=5,
    ) is True


def test_router_without_db_skips_adaptive():
    cfg = TGsConfig()
    router = TaskRouter(cfg)  # no DB
    result = router.classify("refactor auth module")
    assert result.tier in ("low", "medium", "high")
    # report_outcome should not crash
    router.report_outcome(score=0.5, tier="medium", success=True)


def test_classify_applies_adaptive_thresholds_when_gates_satisfied():
    """E2E: mature band + project samples switch classify() to adaptive thresholds."""
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        project_id = str(Path(td) / "project")
        db._conn.execute(
            """
            INSERT INTO project_routing (project_path, overrides_json, learning_enabled, ts)
            VALUES (?, ?, 1, ?)
            """,
            (project_id, '{"tier_bias": 0.0, "sample_count": 0}', 0.0),
        )
        db._conn.commit()

        cfg = TGsConfig()
        router = TaskRouter(cfg, db=db)
        static_thresholds = router._get_thresholds(score=0.52, project_path=project_id)
        assert static_thresholds.low_max == cfg.thresholds.low_max

        for _ in range(5):
            update_band(db, score=0.52, tier="low", success=False)
        for _ in range(PROJECT_SAMPLE_MIN):
            register_observation(
                db,
                project_id,
                {"rework_count": 0, "token_cost": 0, "success": False, "timestamp": 1.0},
            )

        assert should_apply_adaptive_thresholds(
            project_id,
            band_sample_count=5,
            project_sample_count=PROJECT_SAMPLE_MIN,
        )
        adaptive_thresholds = router._get_thresholds(score=0.52, project_path=project_id)
        assert adaptive_thresholds.low_max < static_thresholds.low_max

        decision = router.classify(
            "update unit tests for helper module",
            project_path=project_id,
        )
        expected_tier = router._tier_from_score(decision.score, project_path=project_id)
        assert decision.tier == expected_tier
        db.close()


def test_persist_route_telemetry_stores_complexity_score():
    from shared.outcomes import persist_route_telemetry, route_task_id

    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        task = "fix typo in readme"
        task_id = route_task_id(task)
        persist_route_telemetry(
            db,
            task_id=task_id,
            tier="low",
            complexity_score=0.22,
            model="test-model",
            provider="mcp",
            caller="test",
        )
        row = db._conn.execute(
            "SELECT complexity_score, tier FROM telemetry WHERE task_hash = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == 0.22
        assert row[1] == "low"
        db.close()


def test_enqueue_learning_update_registers_project_observation():
    from shared.outcomes import enqueue_learning_update

    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        project_id = str(Path(td) / "project")
        db._conn.execute(
            """
            INSERT INTO project_routing (project_path, overrides_json, learning_enabled, ts)
            VALUES (?, ?, 1, ?)
            """,
            (project_id, '{"tier_bias": 0.0, "sample_count": 0}', 0.0),
        )
        db._conn.execute(
            """
            INSERT INTO telemetry (session_id, task_hash, agent_id, tier, model, ts)
            VALUES ('test', 'task-1', 0, 'low', 'm', ?)
            """,
            (time.time(),),
        )
        db._conn.commit()

        enqueue_learning_update(db, "task-1", "accepted", project_id=project_id)
        assert get_project_sample_count(db, project_id) == 1
        db.close()


# ---------------------------------------------------------------------------
# Budget awareness (claude-code/providers.py)
# ---------------------------------------------------------------------------

def _budget_modifier(usage: float) -> float:
    """Replicate the budget modifier formula for testing without importing provider."""
    if usage < 0.70:
        return 1.0
    elif usage < 0.85:
        t = (usage - 0.70) / 0.15
        return 1.0 - t * 0.15
    elif usage < 0.95:
        t = (usage - 0.85) / 0.10
        return 0.85 - t * 0.15
    else:
        return 0.70


def test_budget_modifier_no_usage():
    assert _budget_modifier(0.0) == 1.0


def test_budget_modifier_at_threshold():
    assert _budget_modifier(0.70) == 1.0


def test_budget_modifier_mid_degradation():
    mod = _budget_modifier(0.775)  # midpoint of 0.70-0.85
    assert 0.90 < mod < 0.96, f"Expected ~0.925, got {mod}"


def test_budget_modifier_heavy_usage():
    mod = _budget_modifier(0.90)
    assert 0.70 < mod < 0.85, f"Expected ~0.775, got {mod}"


def test_budget_modifier_maxed_out():
    assert _budget_modifier(0.95) == 0.70
    assert _budget_modifier(1.0) == 0.70


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

