"""Tests for the router's duration axis.

The axis is deliberately advisory: it must never change tier, score, or model. It
only sets expectations used for reasoning effort / token budget and for gating the
hybrid diagnose->implement hop.
"""
from __future__ import annotations

import pytest

from shared.config import TGsConfig, ThresholdConfig
from shared.router import (
    DURATION_LONG,
    DURATION_MEDIUM,
    DURATION_SHORT,
    TaskRouter,
    count_task_files,
    duration_bucket_for,
)


@pytest.fixture
def router() -> TaskRouter:
    return TaskRouter(TGsConfig.from_yaml(), db=None)


# ---------------------------------------------------------------------------
# File counting
# ---------------------------------------------------------------------------


class TestCountTaskFiles:
    def test_counts_distinct_source_files(self):
        assert count_task_files("Update core/parser.py and core/lexer.py") == 2

    def test_deduplicates_repeats(self):
        assert count_task_files("Fix a.py then re-check a.py again") == 1

    def test_ignores_docs_and_prose(self):
        assert count_task_files("Fix a typo in README.md") == 0
        assert count_task_files("Update CHANGELOG.md and notes.txt") == 0

    def test_counts_code_alongside_docs(self):
        assert count_task_files("Update svc.py and document it in README.md") == 1

    def test_no_files(self):
        assert count_task_files("Design a distributed rate limiter") == 0

    def test_nested_paths(self):
        assert count_task_files("Edit src/a/b/c/deep.ts") == 1

    def test_case_insensitive_dedupe(self):
        assert count_task_files("Touch App.py and app.py") == 1

    @pytest.mark.parametrize("bad", ["", None, 123, []])
    def test_non_string_input(self, bad):
        assert count_task_files(bad) == 0

    def test_bare_version_numbers_are_not_files(self):
        # "3.12" has no alpha extension, so it must not read as a file.
        assert count_task_files("Upgrade to Python 3.12") == 0


# ---------------------------------------------------------------------------
# Bucket classification
# ---------------------------------------------------------------------------


class TestDurationBucketFor:
    # ThresholdConfig clamps to hard bounds (low_max >= 0.50), so read the real
    # values back rather than assuming the requested ones took effect.
    TH = ThresholdConfig()
    LOW_MAX = TH.low_max
    MED_MAX = TH.medium_max

    def test_low_score_single_file_is_short(self):
        assert duration_bucket_for(0.05, file_count=1, thresholds=self.TH) == DURATION_SHORT

    def test_low_score_no_files_is_short(self):
        assert duration_bucket_for(0.0, file_count=0, thresholds=self.TH) == DURATION_SHORT

    def test_low_score_two_files_is_medium(self):
        # Two files is no longer a quick edit even at a low score.
        assert duration_bucket_for(0.05, file_count=2, thresholds=self.TH) == DURATION_MEDIUM

    def test_mid_score_is_medium(self):
        mid = (self.LOW_MAX + self.MED_MAX) / 2
        assert duration_bucket_for(mid, file_count=1, thresholds=self.TH) == DURATION_MEDIUM

    def test_high_score_is_long(self):
        assert duration_bucket_for(
            self.MED_MAX + 0.05, file_count=0, thresholds=self.TH
        ) == DURATION_LONG

    def test_many_files_is_long_regardless_of_score(self):
        assert duration_bucket_for(0.0, file_count=4, thresholds=self.TH) == DURATION_LONG

    def test_boundary_at_low_max_is_short(self):
        assert duration_bucket_for(
            self.LOW_MAX, file_count=1, thresholds=self.TH
        ) == DURATION_SHORT

    def test_boundary_just_above_low_max_is_medium(self):
        assert duration_bucket_for(
            self.LOW_MAX + 0.01, file_count=1, thresholds=self.TH
        ) == DURATION_MEDIUM

    def test_boundary_at_medium_max_is_medium(self):
        assert duration_bucket_for(
            self.MED_MAX, file_count=1, thresholds=self.TH
        ) == DURATION_MEDIUM


# ---------------------------------------------------------------------------
# Integration with classify()
# ---------------------------------------------------------------------------


class TestClassifyPopulatesDuration:
    def test_trivial_task_is_short(self, router: TaskRouter):
        d = router.classify("Add a docstring to utils/strings.py")
        assert d.expected_duration_bucket == DURATION_SHORT
        assert d.expected_file_count == 1

    def test_wide_multi_file_task_is_long(self, router: TaskRouter):
        d = router.classify(
            "Refactor auth/mw.py auth/tokens.py auth/store.py auth/policy.py to share a session"
        )
        assert d.expected_duration_bucket == DURATION_LONG
        assert d.expected_file_count == 4

    def test_bucket_is_always_valid(self, router: TaskRouter):
        for task in (
            "x",
            "Design an event-sourced ledger",
            "rename a var",
            "Fix README.md",
            "parallelize the test suite across 8 workers",
        ):
            assert router.classify(task).expected_duration_bucket in (
                DURATION_SHORT, DURATION_MEDIUM, DURATION_LONG
            )

    def test_high_keyword_override_is_never_short(self, router: TaskRouter):
        # The override path returns early; it must still populate the axis, and a
        # hard high override is complex by definition.
        for kw in router._overrides.get("high", [])[:3]:
            d = router.classify(f"{kw} the session store")
            assert d.override is True
            assert d.expected_duration_bucket != DURATION_SHORT

    def test_duration_does_not_change_tier_or_score(self, router: TaskRouter):
        # Same prompt with more named files changes duration but must not be the
        # thing that moves the tier: assert the axis is reported, not applied.
        one = router.classify("Update a.py")
        many = router.classify("Update a.py b.py c.py d.py")
        assert one.expected_duration_bucket == DURATION_SHORT
        assert many.expected_duration_bucket == DURATION_LONG
        # Tier comes from the score, which the duration axis never feeds.
        assert one.tier == router._tier_from_score(one.score)
        assert many.tier == router._tier_from_score(many.score)

    def test_reasoning_effort_and_thinking_budget_populated(self, router: TaskRouter):
        short_task = router.classify("Add a docstring to utils/strings.py")
        assert short_task.reasoning_effort == "low"
        assert short_task.thinking_budget == 0

        long_task = router.classify("Refactor auth/mw.py auth/tokens.py auth/store.py auth/policy.py")
        assert long_task.reasoning_effort == "high"
        assert long_task.thinking_budget == 8192


# ---------------------------------------------------------------------------
# Gating the hybrid split
# ---------------------------------------------------------------------------


class TestDurationGatesHybridSplit:
    def test_short_work_skips_the_diagnosis_hop(self):
        from shared.heuristic_plan import build_heuristic_plan_payload

        payload = build_heuristic_plan_payload(
            "Add a docstring to utils/strings.py", default_tier="high"
        )
        assert "hybrid_split" not in payload

    def test_complex_work_gets_the_diagnosis_hop(self):
        from shared.heuristic_plan import build_heuristic_plan_payload

        payload = build_heuristic_plan_payload(
            "Refactor the token rotation architecture in auth/rotate.py", default_tier="high"
        )
        assert "hybrid_split" in payload

    def test_explicit_bucket_overrides_derivation(self):
        from shared.heuristic_plan import build_heuristic_plan_payload

        payload = build_heuristic_plan_payload(
            "Add a docstring to utils/strings.py",
            default_tier="high",
            duration_bucket="long",
        )
        assert "hybrid_split" in payload

    def test_derivation_uses_full_classification_not_raw_score(self):
        from shared.heuristic_plan import _derive_routing_hints

        # A task whose complexity comes from a high-tier override/reasoning bump
        # rather than plain keyword scoring must not be labelled 'short'.
        _, bucket = _derive_routing_hints(
            "Refactor the token rotation architecture in auth/rotate.py"
        )
        assert bucket != DURATION_SHORT

    def test_derivation_is_fail_safe(self):
        from shared.heuristic_plan import _derive_routing_hints

        assert _derive_routing_hints("") == (0.0, DURATION_MEDIUM)
        assert _derive_routing_hints(None) == (0.0, DURATION_MEDIUM)
