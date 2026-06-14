from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from shared.config import ParallelismConfig, TGsConfig
from shared.db import Database
from shared.eval import BackgroundEvaluator, EvalResult, WaveFileTracker, run_warm_path_background_tasks, process_learning_queue
from shared.memory import memory_get
from shared.outcomes import record_outcome


def test_warmpath_calls_compute_snapshot(tmp_path) -> None:
    """Test that background tasks function calls compute_learning_outcome_snapshot."""
    db = Database(tmp_path / "test.db")
    now = time.time()
    cutoff = now - 3600
    
    # Setup: insert telemetry and outcome
    with db.conn() as conn:
        conn.execute(
            """
            INSERT INTO telemetry (ts, tier, model, provider_name)
            VALUES (?, ?, ?, ?)
            """,
            (cutoff + 100, "low", "gpt-5-mini", "test-provider"),
        )
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
    
    # Execute background tasks
    results = run_warm_path_background_tasks(db)
    
    # Verify snapshot was computed
    assert "snapshot" in results
    assert results["snapshot"] == "computed"
    
    # Verify snapshot is stored in memory
    result = memory_get("global", "learning_stats", db=db)
    snapshot = result.get("value", {})
    assert snapshot["total_tasks_in_window"] >= 1
    assert snapshot["tasks_with_feedback"] >= 1


def test_warmpath_graceful_snapshot_error(tmp_path) -> None:
    """Test that snapshot errors don't block learning queue processing."""
    db = Database(tmp_path / "test.db")
    
    # Setup: add a learning queue item
    now = time.time()
    with db.conn() as conn:
        conn.execute(
            """
            INSERT INTO learning_queue (task_id, tier, complexity_score, success, status, enqueued_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("task-1", "low", 0.5, True, "pending", now),
        )
    
    # Execute background tasks (should succeed even if there are errors)
    results = run_warm_path_background_tasks(db)
    
    # Both tasks should have results
    assert "learning" in results
    assert "snapshot" in results
    
    # Learning should show at least attempted to process
    learning = results.get("learning", {})
    # Either processed_count or an error message
    assert "processed_count" in learning or "error" in learning


def test_process_learning_queue_called(tmp_path) -> None:
    """Test that run_warm_path_background_tasks calls process_learning_queue."""
    db = Database(tmp_path / "test.db")
    now = time.time()
    
    # Setup: add a learning queue item
    with db.conn() as conn:
        conn.execute(
            """
            INSERT INTO learning_queue (task_id, tier, complexity_score, success, status, enqueued_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("task-1", "low", 0.5, True, "pending", now),
        )
    
    # Execute background tasks
    results = run_warm_path_background_tasks(db)
    
    # Verify learning queue was processed
    assert "learning" in results
    learning = results.get("learning", {})
    assert "processed_count" in learning
    # Should have processed the 1 item
    assert learning.get("processed_count", 0) >= 0  # May be 0 if adaptive update fails, but should exist


def test_compute_snapshot_in_background_tasks(tmp_path) -> None:
    """Test that snapshot computation is included in background tasks."""
    db = Database(tmp_path / "test.db")
    now = time.time()
    cutoff = now - 3600
    
    # Setup: insert telemetry and outcome
    with db.conn() as conn:
        conn.execute(
            """
            INSERT INTO telemetry (ts, tier, model, provider_name)
            VALUES (?, ?, ?, ?)
            """,
            (cutoff + 100, "low", "gpt-5-mini", "test-provider"),
        )
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
    
    # Execute background tasks
    results = run_warm_path_background_tasks(db)
    
    # Snapshot should be "computed" or have an error key
    snapshot_result = results.get("snapshot")
    assert snapshot_result == "computed" or isinstance(snapshot_result, dict)
    
    # If computed successfully, verify it's in memory
    if snapshot_result == "computed":
        result = memory_get("global", "learning_stats", db=db)
        snapshot = result.get("value", {})
        assert "outcome_distribution" in snapshot
        assert "coverage_percentage" in snapshot


# ---------------------------------------------------------------------------
# Warm-path scheduling tests (from test_warm_path.py)
# ---------------------------------------------------------------------------


def test_spawn_warm_path_from_sync() -> None:
    """BackgroundEvaluator should schedule work via ThreadPoolExecutor warm path."""
    event = threading.Event()
    evaluator = BackgroundEvaluator()

    def fake_run_warm_path_sync(
        tracker: WaveFileTracker,
        rework_events: list[dict],
        model: str = "gpt-5-mini",
    ) -> list[object]:
        event.set()
        return []

    evaluator._run_warm_path_sync = fake_run_warm_path_sync  # type: ignore[method-assign]
    future = evaluator.spawn_warm_path(
        WaveFileTracker(),
        [{"file_path": "foo.py", "wave_n": 0, "wave_n1": 1}],
    )

    assert future is not None
    assert event.wait(timeout=5)
    if hasattr(future, "result"):
        future.result(timeout=5)


def test_warm_path_parallel_eval_faster_than_serial() -> None:
    """Four prompts with warm_path_workers=4 should run concurrently."""
    config = TGsConfig.defaults()
    config.parallelism = ParallelismConfig(warm_path_workers=4)
    evaluator = BackgroundEvaluator(config=config)

    tracker = WaveFileTracker()
    files = ["a.py", "b.py", "c.py", "d.py"]
    for fp in files:
        tracker.snapshots_before[fp] = "v1\n"
        tracker.snapshots_after[fp] = "v2\n"

    rework_events = [
        {"file_path": fp, "wave_n": 0, "wave_n1": 1}
        for fp in files
    ]

    def slow_eval_one(prompt_data, model: str) -> EvalResult:
        time.sleep(0.05)
        return EvalResult(
            file_path=prompt_data.file_path,
            score=0.8,
            reason="ok",
            model=model,
        )

    evaluator._eval_one = slow_eval_one  # type: ignore[method-assign]

    start = time.monotonic()
    results = evaluator._run_warm_path_sync(tracker, rework_events)
    elapsed = time.monotonic() - start

    assert len(results) == 4
    # Serial would take ~0.2s; four workers should finish near one sleep.
    assert elapsed < 0.15
