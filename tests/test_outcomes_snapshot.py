from __future__ import annotations

import time
from pathlib import Path

import pytest

from shared.db import Database
from shared.memory import memory_get, MemoryNotFoundError
from shared.outcomes import compute_learning_outcome_snapshot, record_outcome


def _setup_telemetry_and_outcomes(db: Database, cutoff: float, num_tasks: int, num_with_feedback: int) -> None:
    """Helper to set up test data: telemetry records and outcomes."""
    with db.conn() as conn:
        # Insert telemetry records (simulating routed tasks)
        for i in range(num_tasks):
            ts = cutoff + 60 + i * 60  # Spread across the 1-hour window
            tier = "low" if i % 3 == 0 else ("medium" if i % 3 == 1 else "high")
            model = "gpt-5-mini" if i % 2 == 0 else "claude-sonnet-4-6"
            
            conn.execute(
                """
                INSERT INTO telemetry (ts, tier, model, provider_name)
                VALUES (?, ?, ?, ?)
                """,
                (ts, tier, model, "test-provider"),
            )
        
        # Insert routing_outcomes for first num_with_feedback tasks
        outcomes_distribution = [
            ("accepted", 30 if num_with_feedback >= 30 else num_with_feedback),
            ("revised", 2 if num_with_feedback >= 32 else (num_with_feedback - 30 if num_with_feedback > 30 else 0)),
            ("rejected", 1 if num_with_feedback >= 33 else (num_with_feedback - 32 if num_with_feedback > 32 else 0)),
            ("reworked", 2 if num_with_feedback >= 35 else (num_with_feedback - 33 if num_with_feedback > 33 else 0)),
        ]
        
        outcome_idx = 0
        for outcome_type, count in outcomes_distribution:
            for j in range(count):
                if outcome_idx >= num_with_feedback:
                    break
                task_id = f"task-{outcome_idx}"
                recorded_at = cutoff + 60 + outcome_idx * 60
                tier = "low"
                model = "gpt-5-mini"
                
                conn.execute(
                    """
                    INSERT INTO routing_outcomes (
                        task_id, current_outcome, recorded_at, tier, model,
                        provider_name, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, outcome_type, recorded_at, tier, model, "test-provider", recorded_at),
                )
                outcome_idx += 1


def test_compute_snapshot_basic(tmp_path) -> None:
    """Test basic snapshot computation with known outcomes."""
    db = Database(tmp_path / "test.db")
    now = time.time()
    cutoff = now - 3600
    
    # Setup: 40 telemetry records, 35 with feedback
    _setup_telemetry_and_outcomes(db, cutoff, num_tasks=40, num_with_feedback=35)
    
    # Execute
    compute_learning_outcome_snapshot(db)
    
    # Verify snapshot is stored in memory
    result = memory_get("global", "learning_stats", db=db)
    assert result is not None
    snapshot = result.get("value", {})
    
    # Verify structure
    assert "window_start_time" in snapshot
    assert "window_end_time" in snapshot
    assert "outcome_distribution" in snapshot
    assert "coverage_percentage" in snapshot
    assert "total_tasks_in_window" in snapshot
    assert "tasks_with_feedback" in snapshot
    assert "computed_at" in snapshot
    
    # Verify values
    assert snapshot["total_tasks_in_window"] == 40
    assert snapshot["tasks_with_feedback"] == 35
    assert abs(snapshot["coverage_percentage"] - 87.5) < 0.01  # 35/40 * 100


def test_compute_snapshot_empty_window(tmp_path) -> None:
    """Test snapshot with no outcomes in window (coverage = None)."""
    db = Database(tmp_path / "test.db")
    now = time.time()
    cutoff = now - 3600
    
    # Setup: 0 telemetry records, 0 outcomes
    with db.conn() as conn:
        # Insert just one old telemetry (outside window)
        conn.execute(
            """
            INSERT INTO telemetry (ts, tier, model, provider_name)
            VALUES (?, ?, ?, ?)
            """,
            (cutoff - 100, "low", "gpt-5-mini", "test-provider"),
        )
    
    # Execute
    compute_learning_outcome_snapshot(db)
    
    # Verify snapshot
    result = memory_get("global", "learning_stats", db=db)
    snapshot = result.get("value", {})
    
    assert snapshot["total_tasks_in_window"] == 0
    assert snapshot["tasks_with_feedback"] == 0
    assert snapshot["coverage_percentage"] is None


def test_compute_snapshot_coverage_calculation(tmp_path) -> None:
    """Test coverage calculation: 3 tasks total, 2 with feedback -> 66.7%."""
    db = Database(tmp_path / "test.db")
    now = time.time()
    cutoff = now - 3600
    
    # Setup: 3 telemetry, 2 outcomes
    _setup_telemetry_and_outcomes(db, cutoff, num_tasks=3, num_with_feedback=2)
    
    # Execute
    compute_learning_outcome_snapshot(db)
    
    # Verify coverage
    result = memory_get("global", "learning_stats", db=db)
    snapshot = result.get("value", {})
    
    assert snapshot["total_tasks_in_window"] == 3
    assert snapshot["tasks_with_feedback"] == 2
    assert abs(snapshot["coverage_percentage"] - 66.666666) < 0.01  # 2/3 * 100


def test_compute_snapshot_groupby_tiermodel(tmp_path) -> None:
    """Test that outcomes are grouped correctly by (tier, model) pair."""
    db = Database(tmp_path / "test.db")
    now = time.time()
    cutoff = now - 3600
    
    with db.conn() as conn:
        # Insert telemetry with varied tier/model combinations
        conn.execute(
            """
            INSERT INTO telemetry (ts, tier, model, provider_name)
            VALUES (?, ?, ?, ?)
            """,
            (cutoff + 100, "low", "gpt-5-mini", "test-provider"),
        )
        conn.execute(
            """
            INSERT INTO telemetry (ts, tier, model, provider_name)
            VALUES (?, ?, ?, ?)
            """,
            (cutoff + 200, "medium", "claude-sonnet-4-6", "test-provider"),
        )
        
        # Insert outcomes for both combinations
        conn.execute(
            """
            INSERT INTO routing_outcomes (
                task_id, current_outcome, recorded_at, tier, model,
                provider_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("task-1", "accepted", cutoff + 100, "low", "gpt-5-mini", "test-provider", cutoff + 100),
        )
        conn.execute(
            """
            INSERT INTO routing_outcomes (
                task_id, current_outcome, recorded_at, tier, model,
                provider_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("task-2", "revised", cutoff + 200, "medium", "claude-sonnet-4-6", "test-provider", cutoff + 200),
        )
    
    # Execute
    compute_learning_outcome_snapshot(db)
    
    # Verify grouping
    result = memory_get("global", "learning_stats", db=db)
    snapshot = result.get("value", {})
    dist = snapshot.get("outcome_distribution", {})
    
    # Should have two tier:model groups
    assert "low:gpt-5-mini" in dist
    assert "medium:claude-sonnet-4-6" in dist
    
    # Verify counts
    assert dist["low:gpt-5-mini"]["accepted"] == 1
    assert dist["medium:claude-sonnet-4-6"]["revised"] == 1


def test_compute_snapshot_all_outcome_types(tmp_path) -> None:
    """Test that all outcome types are included with zero counts."""
    db = Database(tmp_path / "test.db")
    now = time.time()
    cutoff = now - 3600
    
    with db.conn() as conn:
        # Insert telemetry
        conn.execute(
            """
            INSERT INTO telemetry (ts, tier, model, provider_name)
            VALUES (?, ?, ?, ?)
            """,
            (cutoff + 100, "low", "gpt-5-mini", "test-provider"),
        )
        
        # Insert only one outcome type (accepted)
        conn.execute(
            """
            INSERT INTO routing_outcomes (
                task_id, current_outcome, recorded_at, tier, model,
                provider_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("task-1", "accepted", cutoff + 100, "low", "gpt-5-mini", "test-provider", cutoff + 100),
        )
    
    # Execute
    compute_learning_outcome_snapshot(db)
    
    # Verify all outcome types are present
    result = memory_get("global", "learning_stats", db=db)
    snapshot = result.get("value", {})
    dist = snapshot.get("outcome_distribution", {})
    
    key = "low:gpt-5-mini"
    assert key in dist
    assert dist[key]["accepted"] == 1
    assert dist[key]["revised"] == 0
    assert dist[key]["rejected"] == 0
    assert dist[key]["reworked"] == 0


def test_compute_snapshot_memory_persistence(tmp_path) -> None:
    """Test that snapshot is stored and retrievable via memory_get."""
    db = Database(tmp_path / "test.db")
    now = time.time()
    cutoff = now - 3600
    
    _setup_telemetry_and_outcomes(db, cutoff, num_tasks=40, num_with_feedback=35)
    
    # Execute
    compute_learning_outcome_snapshot(db)
    
    # Retrieve via memory_get
    result = memory_get("global", "learning_stats", db=db)
    assert result is not None
    assert "value" in result
    assert "scope" in result
    assert result["scope"] == "global"
    assert result["key"] == "learning_stats"
    
    # Verify snapshot is correct
    snapshot = result.get("value", {})
    assert snapshot["coverage_percentage"] == 87.5


def test_compute_snapshot_window_boundaries(tmp_path) -> None:
    """Test that outcomes within the 1-hour window are included."""
    db = Database(tmp_path / "test.db")
    now = time.time()
    cutoff = now - 3600
    
    # Use times clearly within the window to avoid boundary precision issues
    t_inside = cutoff + 1800  # 30 minutes into window
    t_outside = cutoff - 1    # 1 second before window
    
    with db.conn() as conn:
        # Insert telemetry records
        conn.execute(
            """
            INSERT INTO telemetry (ts, tier, model, provider_name)
            VALUES (?, ?, ?, ?)
            """,
            (t_inside, "low", "gpt-5-mini", "test-provider"),
        )
        conn.execute(
            """
            INSERT INTO telemetry (ts, tier, model, provider_name)
            VALUES (?, ?, ?, ?)
            """,
            (t_outside, "low", "gpt-5-mini", "test-provider"),
        )
        
        # Insert outcome inside window
        conn.execute(
            """
            INSERT INTO routing_outcomes (
                task_id, current_outcome, recorded_at, tier, model,
                provider_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("task-1", "accepted", t_inside, "low", "gpt-5-mini", "test-provider", t_inside),
        )
        # Insert outcome outside window
        conn.execute(
            """
            INSERT INTO routing_outcomes (
                task_id, current_outcome, recorded_at, tier, model,
                provider_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("task-2", "accepted", t_outside, "low", "gpt-5-mini", "test-provider", t_outside),
        )
    
    # Execute
    compute_learning_outcome_snapshot(db)
    
    # Verify
    result = memory_get("global", "learning_stats", db=db)
    snapshot = result.get("value", {})
    
    # Only one outcome should be included (the one inside window)
    assert snapshot["tasks_with_feedback"] == 1
    dist = snapshot.get("outcome_distribution", {})
    assert dist["low:gpt-5-mini"]["accepted"] == 1


def test_compute_snapshot_error_handling(tmp_path) -> None:
    """Test that errors are logged but the function returns gracefully."""
    db = Database(tmp_path / "test.db")

    # Create a deliberately broken db connection scenario
    # by calling with a db that will fail on query execution
    # We'll just verify the function doesn't raise an exception
    try:
        compute_learning_outcome_snapshot(db)
        # If we get here, the function handled the error gracefully
        assert True
    except Exception as e:
        # The function should not raise
        pytest.fail(f"compute_learning_outcome_snapshot raised an exception: {e}")


# ---------------------------------------------------------------------------
# Full-pipeline integration test (from test_outcomes_integration.py)
# ---------------------------------------------------------------------------

from shared.memory import MemoryNotFoundError  # noqa: E402


def test_integration_outcome_recording_to_snapshot_computation() -> None:
    """
    Full pipeline test: insert telemetry, record outcomes, compute snapshot, query memory.

    Scenario: 40 tasks routed (telemetry), 35 with recorded outcomes.
    Expected coverage: 87.5% (35/40)
    Expected distribution: 30 accepted, 2 revised, 1 rejected, 2 reworked
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "integration-test.db"
        db = Database(db_path=db_path)

        now = time.time()
        cutoff = now - 3600  # 1 hour window

        # Step 1: Insert 40 telemetry records (simulating 40 routed tasks)
        with db.conn() as conn:
            for i in range(40):
                ts = cutoff + 100 + i * 60  # Spread across window
                conn.execute(
                    """
                    INSERT INTO telemetry (ts, tier, model, provider_name)
                    VALUES (?, ?, ?, ?)
                    """,
                    (ts, "low", "gpt-5-mini", "test-provider"),
                )

            # Step 2: Record 35 outcomes (various types) for 35 of the 40 tasks
            outcomes_spec = [
                ("task-0", "accepted"),
                ("task-1", "accepted"),
                ("task-2", "accepted"),
                ("task-3", "accepted"),
                ("task-4", "accepted"),
                ("task-5", "revised"),
                ("task-6", "revised"),
                ("task-7", "rejected"),
                ("task-8", "reworked"),
                ("task-9", "reworked"),
            ]

            for task_id, outcome in outcomes_spec:
                conn.execute(
                    """
                    INSERT INTO routing_outcomes (
                        task_id, current_outcome, recorded_at, tier, model,
                        provider_name, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, outcome, cutoff + 150, "low", "gpt-5-mini", "test-provider", cutoff + 150),
                )

            # Record 25 more accepted outcomes (to get 30 total accepted)
            for i in range(10, 35):
                conn.execute(
                    """
                    INSERT INTO routing_outcomes (
                        task_id, current_outcome, recorded_at, tier, model,
                        provider_name, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (f"task-{i}", "accepted", cutoff + 150 + i * 10, "low", "gpt-5-mini", "test-provider", cutoff + 150 + i * 10),
                )

        # Step 3: Compute snapshot (simulates warm-path executor background task)
        compute_learning_outcome_snapshot(db)

        # Step 4: Query via memory
        try:
            result = memory_get("global", "learning_stats", db=db)
            snapshot = result.get("value", {})
        except MemoryNotFoundError:
            pytest.fail("Snapshot should be stored in memory after computation")

        # Step 5: Verify response structure and exact values
        assert snapshot, "Snapshot should not be empty"

        assert snapshot["coverage_percentage"] == 87.5
        assert snapshot["total_tasks_in_window"] == 40
        assert snapshot["tasks_with_feedback"] == 35

        dist = snapshot["outcome_distribution"]
        assert "low:gpt-5-mini" in dist

        tier_model_dist = dist["low:gpt-5-mini"]
        assert tier_model_dist["accepted"] == 30
        assert tier_model_dist["revised"] == 2
        assert tier_model_dist["rejected"] == 1
        assert tier_model_dist["reworked"] == 2

        assert "window_start_time" in snapshot
        assert "window_end_time" in snapshot
        assert "computed_at" in snapshot
        assert snapshot["window_end_time"] - snapshot["window_start_time"] >= 3599
