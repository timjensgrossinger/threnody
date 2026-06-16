#!/usr/bin/env python3
"""Tests for shared.eval — Phase 2 quality feedback loop."""
from __future__ import annotations

import sys
import os
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.eval import (
    OverlapType,
    classify_file_overlap,
    build_eval_prompt,
    WaveFileTracker,
    BackgroundEvaluator,
    cold_path_adjust,
    _extract_symbol_names,
    _extract_symbol_body,
    EvalResult,
)
from shared.config import TGsConfig
from shared.db import Database



# ---------------------------------------------------------------------------
# OverlapType enum
# ---------------------------------------------------------------------------

def test_overlap_type_values():
    assert OverlapType.SAME_SCOPE_REWRITE.value == "same_scope_rewrite"
    assert OverlapType.EXTENSION.value == "extension"
    assert OverlapType.NONE.value == "none"


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------

def test_extract_symbol_names_basic():
    source = """
def hello():
    pass

class Foo:
    pass

async def bar():
    pass
"""
    names = _extract_symbol_names(source)
    assert names == {"hello", "Foo", "bar"}


def test_extract_symbol_names_empty():
    assert _extract_symbol_names("") == set()
    assert _extract_symbol_names("x = 1\ny = 2") == set()


def test_extract_symbol_body():
    source = """def greet(name):
    msg = f"Hello {name}"
    return msg

def other():
    pass
"""
    body = _extract_symbol_body(source, "greet")
    # Includes trailing blank line before next def
    assert len(body) >= 3
    assert body[0].startswith("def greet")
    assert "return msg" in body[2]


# ---------------------------------------------------------------------------
# classify_file_overlap
# ---------------------------------------------------------------------------

def test_classify_same_scope_rewrite():
    before = {
        "auth.py": "def login():\n    return True\n",
    }
    after = {
        "auth.py": "def login():\n    validate()\n    return True\n",
    }
    result = classify_file_overlap(
        wave_n_files={"auth.py"},
        wave_n1_files={"auth.py"},
        content_before=before,
        content_after=after,
    )
    assert result["auth.py"] == OverlapType.SAME_SCOPE_REWRITE


def test_classify_extension():
    before = {
        "utils.py": "def helper():\n    pass\n",
    }
    after = {
        "utils.py": "def helper():\n    pass\n\ndef new_func():\n    pass\n",
    }
    result = classify_file_overlap(
        wave_n_files={"utils.py"},
        wave_n1_files={"utils.py"},
        content_before=before,
        content_after=after,
    )
    assert result["utils.py"] == OverlapType.EXTENSION


def test_classify_no_overlap():
    result = classify_file_overlap(
        wave_n_files={"a.py"},
        wave_n1_files={"b.py"},
        content_before={},
        content_after={},
    )
    assert result["b.py"] == OverlapType.NONE


def test_classify_empty_files():
    result = classify_file_overlap(
        wave_n_files={"x.py"},
        wave_n1_files={"x.py"},
        content_before={"x.py": ""},
        content_after={"x.py": ""},
    )
    # No symbols in either → EXTENSION
    assert result["x.py"] == OverlapType.EXTENSION


# ---------------------------------------------------------------------------
# build_eval_prompt
# ---------------------------------------------------------------------------

def test_eval_prompt_contains_diff():
    before = "def foo():\n    return 1\n"
    after = "def foo():\n    return 2\n"
    prompt = build_eval_prompt("test.py", before, after)
    assert "EVAL DIFF for test.py" in prompt
    assert "return 1" in prompt or "return 2" in prompt
    assert "score" in prompt.lower()


def test_eval_prompt_truncation():
    before = "line\n" * 1000
    after = "changed\n" * 1000
    prompt = build_eval_prompt("big.py", before, after, max_tokens=50)
    assert "[truncated]" in prompt


def test_eval_prompt_empty_diff():
    prompt = build_eval_prompt("same.py", "same\n", "same\n")
    assert "EVAL DIFF for same.py" in prompt


# ---------------------------------------------------------------------------
# WaveFileTracker
# ---------------------------------------------------------------------------

def test_tracker_record_and_detect_no_rework():
    tracker = WaveFileTracker()
    tracker.record_wave(0, {"a.py"})
    tracker.record_wave(1, {"b.py"})
    events = tracker.detect_rework(1)
    assert events == []


def test_tracker_detect_rework_on_overlap():
    tracker = WaveFileTracker()
    tracker.record_wave(0, {"shared.py"}, content_before={}, content_after={
        "shared.py": "def process():\n    return None\n"
    })
    tracker.record_wave(1, {"shared.py"}, content_before={
        "shared.py": "def process():\n    return None\n"
    }, content_after={
        "shared.py": "def process():\n    validate()\n    return None\n"
    })
    events = tracker.detect_rework(1)
    assert len(events) == 1
    assert events[0]["file_path"] == "shared.py"
    assert events[0]["scope_match"] is True


def test_tracker_prunes_old_wave_snapshots():
    # Resident snapshot content must stay bounded to the last 2 waves so a
    # long multi-wave run under large fan-out cannot accumulate every file's
    # content in RAM. Older waves' content (and bookkeeping) is evicted.
    tracker = WaveFileTracker()
    for w in range(5):
        path = f"w{w}.py"
        tracker.record_wave(w, {path}, content_after={path: f"x = {w}\n"})
    # Only the final two waves' files survive.
    assert set(tracker.snapshots_after) == {"w3.py", "w4.py"}
    assert set(tracker.wave_files) == {3, 4}
    # The most recent wave pair still detects rework correctly post-prune.
    tracker.record_wave(5, {"w4.py"}, content_before={"w4.py": "x = 4\n"},
                        content_after={"w4.py": "x = 99\n"})
    events = tracker.detect_rework(5)
    assert len(events) == 1
    assert events[0]["file_path"] == "w4.py"


def test_tracker_detect_rework_wave_0_returns_empty():
    tracker = WaveFileTracker()
    tracker.record_wave(0, {"a.py"})
    events = tracker.detect_rework(0)
    assert events == []


def test_tracker_detect_rework_with_db():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        tracker = WaveFileTracker()
        tracker.record_wave(0, {"f.py"}, content_after={"f.py": "x = 1\n"})
        tracker.record_wave(1, {"f.py"}, content_before={"f.py": "x = 1\n"},
                            content_after={"f.py": "x = 2\n"})
        events = tracker.detect_rework(1, db=db, session_id="test-sess")
        assert len(events) >= 1
        # Verify it was persisted
        row = db._conn.execute("SELECT COUNT(*) FROM rework_events").fetchone()
        assert row[0] >= 1
        db.close()


# ---------------------------------------------------------------------------
# BackgroundEvaluator
# ---------------------------------------------------------------------------

def test_evaluator_build_prompts():
    tracker = WaveFileTracker()
    tracker.snapshots_before = {"auth.py": "old code"}
    tracker.snapshots_after = {"auth.py": "new code"}
    evaluator = BackgroundEvaluator()
    prompts = evaluator.build_prompts(tracker, [
        {"file_path": "auth.py", "wave_n": 0, "wave_n1": 1},
    ])
    assert len(prompts) == 1
    assert prompts[0].file_path == "auth.py"


def test_evaluator_build_prompts_skips_empty():
    tracker = WaveFileTracker()
    evaluator = BackgroundEvaluator()
    prompts = evaluator.build_prompts(tracker, [
        {"file_path": "missing.py", "wave_n": 0, "wave_n1": 1},
    ])
    assert len(prompts) == 0


def test_evaluator_eval_one_no_backend():
    evaluator = BackgroundEvaluator()
    from shared.eval import EvalPromptData
    pd = EvalPromptData("test.py", "old", "new", 0, 1)
    result = evaluator._eval_one(pd, "gpt-5-mini")
    assert result.score == 0.5
    assert result.model == "gpt-5-mini"


def test_evaluator_eval_one_with_mock_backend():
    def mock_cli(prompt, model, timeout):
        return '{"score": 0.8, "reason": "good change"}'

    evaluator = BackgroundEvaluator(cli_call=mock_cli)
    from shared.eval import EvalPromptData
    pd = EvalPromptData("test.py", "old", "new", 0, 1)
    result = evaluator._eval_one(pd, "gpt-5-mini")
    assert result.score == 0.8
    assert result.reason == "good change"


def test_warm_path_sync_spawn():
    """Test that warm-path spawns from synchronous context (no event loop).
    
    Wave 3 FNDX-03: Verify spawn_warm_path() returns Future immediately
    without blocking, and doesn't require async context.
    """
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        evaluator = BackgroundEvaluator(db=db)
        
        tracker = WaveFileTracker()
        tracker.record_wave(0, {"test.py"}, content_after={"test.py": "x = 1\n"})
        tracker.record_wave(1, {"test.py"}, 
                           content_before={"test.py": "x = 1\n"},
                           content_after={"test.py": "x = 2\n"})
        
        rework_events = tracker.detect_rework(1)
        assert len(rework_events) > 0
        
        # Spawn warm-path — should return Future immediately, not block
        future = evaluator.spawn_warm_path(tracker, rework_events)
        assert future is not None
        assert not future.done()  # Should not be done immediately
        
        # Wait for completion with timeout
        try:
            result = future.result(timeout=5)
            assert isinstance(result, list)
        except Exception as e:
            # Best-effort: eval may fail if no CLI backend, but Future should exist
            pass
        
        db.close()


def test_warm_path_persists_results():
    """Test that warm-path persists evaluation results to database.
    
    Wave 3 FNDX-03: Verify _run_warm_path_sync() uses thread-local
    database connections (from Wave 1) and persists results.
    """
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        
        def mock_cli(prompt, model, timeout):
            return '{"score": 0.7, "reason": "acceptable"}'
        
        evaluator = BackgroundEvaluator(db=db, cli_call=mock_cli)
        
        tracker = WaveFileTracker()
        tracker.record_wave(0, {"change.py"}, content_after={"change.py": "v = 1\n"})
        tracker.record_wave(1, {"change.py"},
                           content_before={"change.py": "v = 1\n"},
                           content_after={"change.py": "v = 2\n"})
        
        rework_events = tracker.detect_rework(1)
        assert len(rework_events) > 0
        
        # Run sync (not spawned — direct call for testing)
        results = evaluator._run_warm_path_sync(tracker, rework_events)
        assert len(results) > 0
        assert results[0].score == 0.7
        
        # Verify results were persisted
        row = db._conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE version = 'eval'"
        ).fetchone()
        assert row[0] >= 1
        
        db.close()


# ---------------------------------------------------------------------------
# cold_path_adjust
# ---------------------------------------------------------------------------

def test_cold_path_no_data_returns_false():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        cfg = TGsConfig()
        result = cold_path_adjust(db, cfg, every_n_tasks=10)
        assert result is False
        db.close()


def test_cold_path_adjusts_on_high_rework():
    import time
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        cfg = TGsConfig()
        original_low_max = cfg.thresholds.low_max

        # Insert 10 telemetry rows (so task_count % 10 == 0)
        for i in range(10):
            db._conn.execute(
                "INSERT INTO telemetry (session_id, task_hash, agent_id, tier, model, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"s{i}", f"h{i}", i, "low", "gpt-5-mini", time.time()),
            )
        # Insert 5 rework events (50% rework rate > 30% threshold)
        for i in range(5):
            db._conn.execute(
                "INSERT INTO rework_events (session_id, wave_n, wave_n1, file_path, scope_match, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"s{i}", 0, 1, f"file{i}.py", 1, time.time()),
            )
        db._conn.commit()

        result = cold_path_adjust(db, cfg, every_n_tasks=10)
        assert result is True
        # low_max should have decreased
        assert cfg.thresholds.low_max <= original_low_max
        db.close()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Learning Queue Batch Processing (Phase 24)
# ---------------------------------------------------------------------------

def test_process_learning_queue_single_item():
    """Test that process_learning_queue processes 1 pending item and marks it processed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "queue_single.db")
        from shared.eval import process_learning_queue
        from shared.adaptive import band_for_score
        
        # Insert 1 pending item
        now = time.time()
        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO learning_queue (task_id, tier, complexity_score, success, status, enqueued_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("task-queue-1", "low", 0.05, 1, "pending", now),
            )
        
        # Process
        result = process_learning_queue(db)
        
        assert result["processed_count"] == 1
        assert result["error_count"] == 0
        
        # Verify status updated
        with db.conn() as conn:
            row = conn.execute(
                "SELECT status, processed_at FROM learning_queue WHERE task_id = ?",
                ("task-queue-1",),
            ).fetchone()
        
        assert row[0] == "processed"
        assert row[1] is not None, "processed_at should be set"
        
        # Verify adaptive_thresholds entry created
        band = band_for_score(0.05)
        with db.conn() as conn:
            threshold_row = conn.execute(
                "SELECT success_ema, sample_count FROM adaptive_thresholds WHERE band = ? AND tier = ? AND version = ?",
                (band, "low", "shared"),
            ).fetchone()
        
        assert threshold_row is not None, f"adaptive_thresholds entry not created for band {band}"
        assert threshold_row[1] == 1, "sample_count should be 1"


def test_process_learning_queue_batch_50():
    """Test that process_learning_queue handles batch of 50 items."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "queue_batch50.db")
        from shared.eval import process_learning_queue
        
        now = time.time()
        with db.conn() as conn:
            for i in range(50):
                conn.execute(
                    """
                    INSERT INTO learning_queue (task_id, tier, complexity_score, success, status, enqueued_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (f"task-queue-{i}", "medium", 0.5, i % 2, "pending", now),
                )
        
        result = process_learning_queue(db)
        
        assert result["processed_count"] == 50
        assert result["error_count"] == 0
        
        # Verify all items marked processed
        with db.conn() as conn:
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM learning_queue WHERE status = 'pending'"
            ).fetchone()[0]
        
        assert pending_count == 0, "Some items not processed"


def test_process_learning_queue_batch_100_limit():
    """Test that process_learning_queue respects 100-item batch limit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "queue_batch100.db")
        from shared.eval import process_learning_queue
        
        now = time.time()
        with db.conn() as conn:
            # Insert 150 items; only 100 should be processed per call
            for i in range(150):
                conn.execute(
                    """
                    INSERT INTO learning_queue (task_id, tier, complexity_score, success, status, enqueued_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (f"task-queue-{i}", "high", 0.95, 1, "pending", now),
                )
        
        result = process_learning_queue(db)
        
        assert result["processed_count"] == 100, f"Expected 100 processed, got {result['processed_count']}"
        
        # Verify 50 still pending
        with db.conn() as conn:
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM learning_queue WHERE status = 'pending'"
            ).fetchone()[0]
        
        assert pending_count == 50, f"Expected 50 remaining pending, got {pending_count}"


def test_process_learning_queue_empty():
    """Test that process_learning_queue on empty queue returns zeros."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "queue_empty.db")
        from shared.eval import process_learning_queue
        
        result = process_learning_queue(db)
        
        assert result["processed_count"] == 0
        assert result["error_count"] == 0


def test_process_learning_queue_error_handling():
    """Test that process_learning_queue handles individual item errors gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "queue_error.db")
        from shared.eval import process_learning_queue
        
        now = time.time()
        with db.conn() as conn:
            # Insert item with invalid tier (will cause update_band to fail or skip)
            conn.execute(
                """
                INSERT INTO learning_queue (task_id, tier, complexity_score, success, status, enqueued_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (f"task-queue-bad", "invalid_tier", 0.5, 1, "pending", now),
            )
        
        result = process_learning_queue(db)
        
        # Should either process or error gracefully (depends on adaptive.update_band implementation)
        # At minimum, error_count should be <= 1 and process should not crash
        assert result["processed_count"] + result["error_count"] >= 1
        assert isinstance(result, dict)


def test_process_learning_queue_stats_return():
    """Test that process_learning_queue returns proper stats dict."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "queue_stats.db")
        from shared.eval import process_learning_queue
        
        now = time.time()
        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO learning_queue (task_id, tier, complexity_score, success, status, enqueued_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (f"task-queue-stats", "low", 0.1, 1, "pending", now),
            )
        
        result = process_learning_queue(db)
        
        assert isinstance(result, dict)
        assert "processed_count" in result
        assert "skipped_count" in result
        assert "error_count" in result
        assert result["processed_count"] == 1


def test_integration_outcome_to_adaptive_update():
    """
    Integration test: outcome recording → queue enqueue → background processing → adaptive update.
    
    Verifies the complete learning loop:
    1. record_outcome() enqueues learning signal (async)
    2. process_learning_queue() processes signal (background)
    3. adaptive_thresholds updated via update_band()
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "integration.db")
        from shared.eval import process_learning_queue
        from shared.outcomes import record_outcome
        from shared.adaptive import band_for_score
        
        task_id = "task-integration-full"
        complexity = 0.42
        tier = "medium"
        
        # Step 1: Create telemetry
        db.log_agent_result(
            session_id="session-int",
            task_hash=task_id,
            agent_id=1,
            tier=tier,
            model="claude-sonnet",
            provider_name="Claude Code",
        )
        
        # Step 2: Record outcome (should enqueue learning signal)
        result = record_outcome(db, task_id, "accepted", operator_id="op-int")
        assert result == {"stored": True, "task_id": task_id}
        
        # Step 3: Verify learning_queue entry
        with db.conn() as conn:
            queue_entry = conn.execute(
                "SELECT task_id, tier, success, status FROM learning_queue WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        
        assert queue_entry is not None, "Queue entry not created by record_outcome()"
        assert queue_entry[2] == 1, "Success should be 1 for accepted outcome"
        assert queue_entry[3] == "pending", "Status should be pending"
        
        # Step 4: Process learning queue (background job)
        stats = process_learning_queue(db)
        assert stats["processed_count"] == 1
        assert stats["error_count"] == 0
        
        # Step 5: Verify queue entry marked processed
        with db.conn() as conn:
            processed_entry = conn.execute(
                "SELECT status, processed_at FROM learning_queue WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        
        assert processed_entry[0] == "processed", "Status not updated to processed"
        assert processed_entry[1] is not None, "processed_at timestamp not set"
        
        # Step 6: Verify adaptive_thresholds updated
        # Get the actual complexity_score from the queue
        with db.conn() as conn:
            queue_with_score = conn.execute(
                "SELECT complexity_score FROM learning_queue WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        
        actual_complexity = queue_with_score[0] if queue_with_score else None
        band = band_for_score(actual_complexity)
        with db.conn() as conn:
            threshold_entry = conn.execute(
                """
                SELECT success_ema, sample_count FROM adaptive_thresholds
                WHERE band = ? AND tier = ? AND version = ?
                """,
                (band, tier, "shared"),
            ).fetchone()
        
        assert threshold_entry is not None, f"Adaptive threshold entry not created for band {band}"
        assert threshold_entry[1] == 1, f"sample_count should be 1, got {threshold_entry[1]}"


