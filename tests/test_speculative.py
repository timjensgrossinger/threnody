#!/usr/bin/env python3
"""Tests for speculative execution (Phase 6)."""
import sys, os, unittest, tempfile, threading
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.config import TGsConfig, ThresholdConfig, ParallelismConfig, SPECULATION_MARGIN
from shared.planner import Subtask
from shared.orchestrator import Provider
from shared.speculative import (
    is_borderline,
    check_output_quality,
    SpeculativeExecutor,
    SpeculativeResult,
    _speculation_worker_count,
)
from shared.db import Database


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------

class MockProvider(Provider):
    def __init__(self, tiers=None, models=None, outputs=None):
        self._tiers = tiers or ["low", "medium", "high"]
        self._models = models or {
            "low": "gpt-5-mini",
            "medium": "gpt-5.4",
            "high": "gpt-5.4",
        }
        self._outputs = outputs or {}
        self.call_log: list[tuple[str, str]] = []

    def resolve_model(self, tier: str) -> str:
        return self._models.get(tier, "unknown")

    def execute(self, subtask, model, timeout=120):
        self.call_log.append((subtask.tier, model))
        return self._outputs.get(subtask.tier, f"Output for tier {subtask.tier} with model {model}")

    def available_tiers(self):
        return list(self._tiers)


class BarrierHigherTierProvider(MockProvider):
    """Blocks higher-tier calls at a barrier to prove concurrent in-flight work."""

    def __init__(self, barrier: threading.Barrier, **kwargs):
        super().__init__(**kwargs)
        self._barrier = barrier
        self._lock = threading.Lock()
        self.concurrent_high = 0
        self.max_concurrent_high = 0

    def execute(self, subtask, model, timeout=120):
        if subtask.tier != "low":
            with self._lock:
                self.concurrent_high += 1
                self.max_concurrent_high = max(
                    self.max_concurrent_high,
                    self.concurrent_high,
                )
            try:
                self._barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            finally:
                with self._lock:
                    self.concurrent_high -= 1
        return super().execute(subtask, model, timeout=timeout)


# ---------------------------------------------------------------------------
# is_borderline tests
# ---------------------------------------------------------------------------

class TestIsBorderline(unittest.TestCase):
    def setUp(self):
        # ThresholdConfig.__post_init__ clamps to the hard bounds in
        # shared/config.py, so read the boundaries back and derive every probe
        # from them. Naming literal scores encoded one particular set of bounds:
        # when they were re-derived, every probe silently stopped sitting where
        # the test name claimed it did.
        self.thresholds = ThresholdConfig()
        self.low_max = self.thresholds.low_max
        self.medium_max = self.thresholds.medium_max
        # A score far enough from both boundaries to be comfortably interior.
        self.interior = (self.low_max + self.medium_max) / 2
        assert self.medium_max - self.low_max > 4 * SPECULATION_MARGIN, (
            "the bands must be wider than the speculation margin for an "
            "interior score to exist at all"
        )

    def test_well_below_low_max(self):
        result = is_borderline(max(0.0, self.low_max - 4 * SPECULATION_MARGIN),
                               self.thresholds)
        self.assertIsNone(result)

    def test_exactly_at_low_max(self):
        result = is_borderline(self.low_max, self.thresholds)
        self.assertIsNotNone(result)
        self.assertEqual(result, (True, "low", "medium"))

    def test_within_margin_above_low_max(self):
        result = is_borderline(self.low_max + SPECULATION_MARGIN * 0.5,
                               self.thresholds)
        self.assertIsNotNone(result)
        self.assertEqual(result[1:], ("low", "medium"))

    def test_within_margin_below_low_max(self):
        result = is_borderline(self.low_max - SPECULATION_MARGIN * 0.5,
                               self.thresholds)
        self.assertIsNotNone(result)
        self.assertEqual(result[1:], ("low", "medium"))

    def test_at_medium_max(self):
        result = is_borderline(self.medium_max, self.thresholds)
        self.assertIsNotNone(result)
        self.assertEqual(result, (True, "medium", "high"))

    def test_well_above_medium_max(self):
        result = is_borderline(min(1.0, self.medium_max + 4 * SPECULATION_MARGIN),
                               self.thresholds)
        self.assertIsNone(result)

    def test_between_boundaries_not_near_either(self):
        result = is_borderline(self.interior, self.thresholds)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# check_output_quality tests
# ---------------------------------------------------------------------------

class TestCheckOutputQuality(unittest.TestCase):
    def test_none_input(self):
        self.assertFalse(check_output_quality(None))

    def test_empty_string(self):
        self.assertFalse(check_output_quality(""))

    def test_too_short(self):
        self.assertFalse(check_output_quality("hi"))

    def test_error_pattern(self):
        self.assertFalse(check_output_quality("x" * 100 + " error occurred here"))

    def test_traceback_pattern(self):
        self.assertFalse(check_output_quality("x" * 100 + " Traceback (most recent call last)"))

    def test_syntax_error_pattern(self):
        self.assertFalse(check_output_quality("x" * 100 + " syntax error on line 5"))

    def test_clean_output(self):
        self.assertTrue(check_output_quality("def hello():\n    return 'world'\n" + "x" * 50))

    def test_long_clean_output(self):
        self.assertTrue(check_output_quality("a" * 500))


# ---------------------------------------------------------------------------
# SpeculativeExecutor tests
# ---------------------------------------------------------------------------

class TestSpeculativeExecutor(unittest.TestCase):
    def setUp(self):
        self.config = TGsConfig()
        # Derived, not literal — see TestIsBorderline.setUp. BORDERLINE sits on
        # the low/medium boundary and INTERIOR comfortably inside a band, so
        # these stay meaningful across a bounds change.
        self.config.thresholds = ThresholdConfig()
        self.BORDERLINE = self.config.thresholds.low_max
        self.INTERIOR = (
            self.config.thresholds.low_max + self.config.thresholds.medium_max
        ) / 2

    def test_can_speculate_true_with_mini(self):
        provider = MockProvider()
        with SpeculativeExecutor(provider, self.config) as ex:
            self.assertTrue(ex.can_speculate(self.BORDERLINE, "low"))

    def test_can_speculate_false_without_mini(self):
        provider = MockProvider(models={
            "low": "claude-haiku-4.5",
            "medium": "claude-sonnet-4.6",
            "high": "claude-opus-4.6",
        })
        with SpeculativeExecutor(provider, self.config) as ex:
            self.assertFalse(ex.can_speculate(self.BORDERLINE, "low"))

    def test_can_speculate_false_tier_not_available(self):
        provider = MockProvider(tiers=["medium", "high"])
        with SpeculativeExecutor(provider, self.config) as ex:
            self.assertFalse(ex.can_speculate(self.BORDERLINE, "low"))

    def test_not_borderline_returns_none(self):
        provider = MockProvider()
        subtask = Subtask(id=1, description="test", tier="low")
        with SpeculativeExecutor(provider, self.config) as ex:
            result = ex.execute_speculative(subtask, self.INTERIOR)
        self.assertIsNone(result)

    def test_borderline_returns_result(self):
        provider = MockProvider(outputs={
            "low": "Good output " * 20,
            "medium": "Better output " * 20,
        })
        subtask = Subtask(id=1, description="test", tier="medium")
        with SpeculativeExecutor(provider, self.config) as ex:
            result = ex.execute_speculative(subtask, self.BORDERLINE)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, SpeculativeResult)
        self.assertTrue(result.speculated)

    def test_lower_tier_passes_quality(self):
        provider = MockProvider(outputs={
            "low": "Clean output with sufficient length " * 5,
            "medium": "Higher tier output " * 5,
        })
        subtask = Subtask(id=1, description="test", tier="medium")
        with SpeculativeExecutor(provider, self.config) as ex:
            result = ex.execute_speculative(subtask, self.BORDERLINE)
        self.assertTrue(result.lower_tier_passed)
        self.assertEqual(result.tier_used, "low")

    def test_lower_tier_fails_quality_short(self):
        provider = MockProvider(outputs={
            "low": "too short",
            "medium": "Higher tier output with enough length " * 5,
        })
        subtask = Subtask(id=1, description="test", tier="medium")
        with SpeculativeExecutor(provider, self.config) as ex:
            result = ex.execute_speculative(subtask, self.BORDERLINE)
        self.assertFalse(result.lower_tier_passed)
        self.assertEqual(result.tier_used, "medium")

    def test_lower_tier_fails_quality_error(self):
        provider = MockProvider(outputs={
            "low": "This produced an error in the function " * 3,
            "medium": "Higher tier clean output " * 5,
        })
        subtask = Subtask(id=1, description="test", tier="medium")
        with SpeculativeExecutor(provider, self.config) as ex:
            result = ex.execute_speculative(subtask, self.BORDERLINE)
        self.assertFalse(result.lower_tier_passed)
        self.assertEqual(result.tier_used, "medium")

    def test_with_db_logs_speculation(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            db = Database(Path(db_path))
            provider = MockProvider(outputs={
                "low": "Good output " * 20,
                "medium": "Better output " * 20,
            })
            subtask = Subtask(id=1, description="test speculation logging", tier="medium")
            with SpeculativeExecutor(provider, self.config, db=db) as ex:
                result = ex.execute_speculative(subtask, self.BORDERLINE)
            self.assertIsNotNone(result)
            row = db._conn.execute("SELECT COUNT(*) FROM speculation_log").fetchone()
            self.assertGreaterEqual(row[0], 1)
        finally:
            os.unlink(db_path)

    def test_without_db_no_crash(self):
        provider = MockProvider(outputs={
            "low": "Good output " * 20,
            "medium": "Better output " * 20,
        })
        subtask = Subtask(id=1, description="test", tier="medium")
        with SpeculativeExecutor(provider, self.config, db=None) as ex:
            result = ex.execute_speculative(subtask, self.BORDERLINE)
        self.assertIsNotNone(result)

    def test_speculation_worker_count_defaults_and_caps(self):
        config = TGsConfig()
        self.assertEqual(_speculation_worker_count(config), 1)

        config.parallelism = ParallelismConfig(speculation_workers=3)
        self.assertEqual(_speculation_worker_count(config), 3)

        config.parallelism = ParallelismConfig(speculation_workers=99)
        self.assertEqual(_speculation_worker_count(config), 8)

    def test_speculation_workers_allows_concurrent_higher_tier(self):
        worker_count = 3
        self.config.parallelism = ParallelismConfig(speculation_workers=worker_count)
        barrier = threading.Barrier(worker_count)
        provider = BarrierHigherTierProvider(
            barrier,
            outputs={
                "low": "Clean lower tier output with enough length " * 5,
                "medium": "Higher tier output with enough length " * 5,
            },
        )
        subtasks = [
            Subtask(id=i, description=f"borderline task {i}", tier="medium")
            for i in range(worker_count)
        ]
        results: list[SpeculativeResult | None] = []

        with SpeculativeExecutor(provider, self.config) as executor:
            threads = [
                threading.Thread(
                    target=lambda st=subtask: results.append(
                        executor.execute_speculative(st, self.BORDERLINE)
                    ),
                    daemon=True,
                )
                for subtask in subtasks
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

        self.assertEqual(len(results), worker_count)
        self.assertEqual(provider.max_concurrent_high, worker_count)


class TestSpeculativeResult(unittest.TestCase):
    def test_fields(self):
        r = SpeculativeResult(
            output="hello",
            tier_used="low",
            model_used="gpt-5-mini",
            speculated=True,
            lower_tier_passed=True,
            token_estimate=42,
        )
        self.assertEqual(r.output, "hello")
        self.assertEqual(r.tier_used, "low")
        self.assertEqual(r.model_used, "gpt-5-mini")
        self.assertTrue(r.speculated)
        self.assertTrue(r.lower_tier_passed)
        self.assertEqual(r.token_estimate, 42)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
