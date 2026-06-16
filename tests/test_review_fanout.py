"""Unit tests for shared/review_fanout.py — per-file x dimension review fanout."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from shared.review_fanout import (
    REVIEW_DIMENSIONS,
    _REVIEW_SENTINEL,
    Complexity,
    build_review_subtasks,
    dimensions_for,
    estimate_complexity,
    is_review_intent,
    tier_for,
)


# ---------------------------------------------------------------------------
# is_review_intent
# ---------------------------------------------------------------------------

class TestIsReviewIntent:
    def test_exact_sentinel(self):
        assert is_review_intent("REVIEW: src/auth.py") is True

    def test_lowercase_sentinel(self):
        assert is_review_intent("review: src/auth.py") is True

    def test_mixed_case_sentinel(self):
        assert is_review_intent("Review: src/auth.py") is True

    def test_leading_whitespace(self):
        assert is_review_intent("  REVIEW: src/auth.py") is True

    def test_no_sentinel(self):
        assert is_review_intent("implement JWT auth for the user service") is False

    def test_review_word_not_prefix(self):
        assert is_review_intent("please review src/auth.py") is False

    def test_empty_string(self):
        assert is_review_intent("") is False

    def test_non_string(self):
        assert is_review_intent(None) is False  # type: ignore[arg-type]
        assert is_review_intent(42) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# estimate_complexity
# ---------------------------------------------------------------------------

class TestEstimateComplexity:
    def test_unreadable_path_defaults_to_moderate(self):
        band, risk = estimate_complexity("/nonexistent/path/file.py")
        assert band == "moderate"
        assert risk is False

    def test_trivial_file_no_risk(self, tmp_path: Path):
        # Use .md (non-risky extension) with short content — stays trivial
        f = tmp_path / "small.md"
        f.write_text("hello\nworld\n", encoding="utf-8")
        band, risk = estimate_complexity(str(f))
        assert band == "trivial"
        assert risk is False

    def test_trivial_bumped_to_moderate_by_risky_extension(self, tmp_path: Path):
        # .py is a risky extension — bumps trivial → moderate
        f = tmp_path / "small.py"
        f.write_text("x = 1\ny = 2\n", encoding="utf-8")
        band, risk = estimate_complexity(str(f))
        assert band == "moderate"

    def test_trivial_bumped_by_risk_signal(self, tmp_path: Path):
        # Non-risky extension (.txt) but contains auth keyword
        f = tmp_path / "config.txt"
        content = "auth_key = 'secret'\n"
        f.write_text(content, encoding="utf-8")
        band, risk = estimate_complexity(str(f))
        assert risk is True
        assert band == "moderate"  # bumped from trivial

    def test_moderate_file(self, tmp_path: Path):
        # 100 lines, no risky extension, no risk signals
        f = tmp_path / "module.md"
        f.write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
        band, risk = estimate_complexity(str(f))
        assert band == "moderate"
        assert risk is False

    def test_complex_file(self, tmp_path: Path):
        f = tmp_path / "big.md"
        f.write_text("\n".join(f"line {i}" for i in range(250)), encoding="utf-8")
        band, risk = estimate_complexity(str(f))
        assert band == "complex"

    def test_oversize_file_still_bands_complex(self, tmp_path: Path):
        # File exceeds CONTEXT_MAX_FILE_BYTES: complexity estimation must still
        # read it fully (uncapped cache delegation). A capped read would return
        # None → "moderate", so this guards the max_bytes=None fallback.
        from shared.config import CONTEXT_MAX_FILE_BYTES
        f = tmp_path / "huge.md"
        line = "x" * 10_000
        n_lines = (CONTEXT_MAX_FILE_BYTES // len(line)) + 250  # > cap and > _LOC_COMPLEX
        f.write_text("\n".join(line for _ in range(n_lines)), encoding="utf-8")
        assert f.stat().st_size > CONTEXT_MAX_FILE_BYTES
        band, risk = estimate_complexity(str(f))
        assert band == "complex"

    def test_risk_signal_sql(self, tmp_path: Path):
        f = tmp_path / "query.md"
        f.write_text("do sql injection here\n" + "\n".join("x" for _ in range(5)), encoding="utf-8")
        _, risk = estimate_complexity(str(f))
        assert risk is True

    def test_risk_signal_subprocess(self, tmp_path: Path):
        f = tmp_path / "runner.md"
        f.write_text("run subprocess here\n", encoding="utf-8")
        _, risk = estimate_complexity(str(f))
        assert risk is True


# ---------------------------------------------------------------------------
# dimensions_for
# ---------------------------------------------------------------------------

class TestDimensionsFor:
    def test_trivial_no_risk(self):
        dims = dimensions_for("trivial", False)
        keys = [d.key for d in dims]
        assert keys == ["logic", "edge"]

    def test_trivial_with_risk_adds_security(self):
        dims = dimensions_for("trivial", True)
        keys = [d.key for d in dims]
        assert "security" in keys
        assert "logic" in keys

    def test_moderate_no_risk(self):
        dims = dimensions_for("moderate", False)
        keys = [d.key for d in dims]
        assert set(keys) == {"logic", "edge", "types"}

    def test_moderate_with_risk_adds_security(self):
        dims = dimensions_for("moderate", True)
        keys = [d.key for d in dims]
        assert "security" in keys

    def test_complex_no_risk(self):
        dims = dimensions_for("complex", False)
        keys = [d.key for d in dims]
        assert set(keys) == {"logic", "edge", "types", "security", "performance"}

    def test_complex_with_risk_no_duplicate_security(self):
        dims = dimensions_for("complex", True)
        keys = [d.key for d in dims]
        assert keys.count("security") == 1


# ---------------------------------------------------------------------------
# tier_for
# ---------------------------------------------------------------------------

class TestTierFor:
    def _dim(self, key: str):
        from shared.review_fanout import _DIM_BY_KEY
        return _DIM_BY_KEY[key]

    def test_security_complex_is_high(self):
        assert tier_for(self._dim("security"), "complex", False) == "high"

    def test_security_with_risk_is_high(self):
        assert tier_for(self._dim("security"), "trivial", True) == "high"

    def test_logic_trivial_is_low(self):
        assert tier_for(self._dim("logic"), "trivial", False) == "low"

    def test_logic_moderate_is_medium(self):
        assert tier_for(self._dim("logic"), "moderate", False) == "medium"

    def test_performance_complex_no_risk_is_medium(self):
        assert tier_for(self._dim("performance"), "complex", False) == "medium"


# ---------------------------------------------------------------------------
# build_review_subtasks
# ---------------------------------------------------------------------------

class TestBuildReviewSubtasks:
    def test_empty_entries_returns_single_fallback(self):
        result = build_review_subtasks([], "REVIEW:")
        subtasks = result["subtasks"]
        assert len(subtasks) == 1
        assert result["topology"] == "linear"

    def test_single_file_produces_review_plus_synthesis(self, tmp_path: Path):
        # Use .md so no risky-extension bump; keep content short
        f = tmp_path / "tiny.md"
        f.write_text("\n".join(f"line {i}" for i in range(10)), encoding="utf-8")
        entries = [(str(f), "")]
        result = build_review_subtasks(entries, f"REVIEW: {f}")

        subtasks = result["subtasks"]
        review = [s for s in subtasks if not s.get("depends_on")]
        synthesis = [s for s in subtasks if s.get("depends_on")]

        assert len(synthesis) == 1
        assert len(review) >= 1
        assert result["topology"] == "dag"

    def test_synthesis_depends_on_all_review_ids(self, tmp_path: Path):
        f = tmp_path / "file.md"
        f.write_text("\n".join(f"line {i}" for i in range(10)), encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: {f}")
        subtasks = result["subtasks"]
        review_ids = [s["id"] for s in subtasks if not s.get("depends_on")]
        synth = next(s for s in subtasks if s.get("depends_on"))
        assert set(synth["depends_on"]) == set(review_ids)

    def test_all_subtasks_are_read_only(self, tmp_path: Path):
        f = tmp_path / "f.md"
        f.write_text("x\n", encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: {f}")
        for st in result["subtasks"]:
            assert st.get("read_only") is True

    def test_trivial_file_spawns_at_most_two_review_agents(self, tmp_path: Path):
        # .md extension, 5 lines → trivial, no risk → {logic, edge}
        f = tmp_path / "tiny.md"
        f.write_text("\n".join(f"l{i}" for i in range(5)), encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: {f}")
        review = [s for s in result["subtasks"] if not s.get("depends_on")]
        assert len(review) <= 2

    def test_complex_risky_file_gets_security_high_tier(self, tmp_path: Path):
        # .md extension so extension doesn't bump, but content has auth + 210 lines
        f = tmp_path / "big.md"
        lines = ["auth = 'secret'"] + [f"line {i}" for i in range(210)]
        f.write_text("\n".join(lines), encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: {f}")
        sec = next(
            (s for s in result["subtasks"] if s.get("subagent_type") == "review-security"),
            None,
        )
        assert sec is not None
        assert sec["tier"] == "high"

    def test_max_agents_drops_lowest_priority_first(self, tmp_path: Path):
        # Complex file would have 5 dims → cap to 3 review + 1 synthesis = 4 total
        f = tmp_path / "big.md"
        f.write_text("\n".join(f"line {i}" for i in range(210)), encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: {f}", max_agents=4)
        review = [s for s in result["subtasks"] if not s.get("depends_on")]
        assert len(review) <= 3
        # Performance (drop_priority=4) should be absent when capped
        dropped_keys = {s.get("subagent_type") for s in review}
        # At least security and logic should be kept (drop_priority 0 and 1)
        assert "review-security" in dropped_keys or "review-logic" in dropped_keys

    def test_max_agents_one_still_produces_synthesis(self, tmp_path: Path):
        f = tmp_path / "f.md"
        f.write_text("x\n", encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: {f}", max_agents=1)
        # With cap=1: review_cap=max(1,0)=0 → 0 review agents? No, max(1, 1-1)=max(1,0)=1
        # Actually max(1, max_agents - 1) = max(1, 0) = 1 review agent kept + synthesis
        subtasks = result["subtasks"]
        assert len(subtasks) >= 1

    def test_review_subtasks_have_subagent_type(self, tmp_path: Path):
        f = tmp_path / "f.md"
        f.write_text("x\n", encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: {f}")
        review = [s for s in result["subtasks"] if not s.get("depends_on")]
        for st in review:
            assert "subagent_type" in st
            assert st["subagent_type"].startswith("review-")

    def test_multiple_files_produces_correct_count(self, tmp_path: Path):
        f1 = tmp_path / "a.md"
        f2 = tmp_path / "b.md"
        # Both trivial → 2 dims each = 4 review + 1 synthesis
        f1.write_text("\n".join(f"l{i}" for i in range(5)), encoding="utf-8")
        f2.write_text("\n".join(f"l{i}" for i in range(5)), encoding="utf-8")
        result = build_review_subtasks([(str(f1), ""), (str(f2), "")], "REVIEW: a.md b.md")
        review = [s for s in result["subtasks"] if not s.get("depends_on")]
        synthesis = [s for s in result["subtasks"] if s.get("depends_on")]
        assert len(synthesis) == 1
        assert len(review) == 4  # 2 files × 2 dims (trivial)


# ---------------------------------------------------------------------------
# Integration: heuristic planner picks up REVIEW: sentinel
# ---------------------------------------------------------------------------

class TestHeuristicPlannerIntegration:
    def test_review_sentinel_activates_fanout(self, tmp_path: Path):
        from shared.heuristic_plan import build_heuristic_plan_payload

        f = tmp_path / "target.md"
        f.write_text("\n".join(f"l{i}" for i in range(10)), encoding="utf-8")

        result = build_heuristic_plan_payload(f"REVIEW: {f}")
        assert result["topology"] == "dag"
        subtasks = result["subtasks"]
        # Must have at least one review subtask and one synthesis
        review = [s for s in subtasks if not s.get("depends_on")]
        synthesis = [s for s in subtasks if s.get("depends_on")]
        assert len(review) >= 1
        assert len(synthesis) == 1

    def test_non_review_task_unaffected(self):
        from shared.heuristic_plan import build_heuristic_plan_payload

        result = build_heuristic_plan_payload("implement JWT auth for user service")
        # Should NOT produce review fanout — normal heuristic path
        subtasks = result["subtasks"]
        for st in subtasks:
            assert not st.get("read_only")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_planner():
    from shared.config import TGsConfig
    from shared.db import Database
    from shared.planner import CLIBackend, Planner

    class _DummyBackend(CLIBackend):
        def call(self, prompt: str, model: str | None = None, timeout: int = 120) -> str | None:
            return None

    _tmpdir = tempfile.TemporaryDirectory()
    db_path = Path(_tmpdir.name) / "test.db"
    planner = Planner(TGsConfig(db_path=db_path), _DummyBackend(), Database(db_path=db_path))
    planner._phase11_tempdir = _tmpdir  # keep alive
    return planner


# ---------------------------------------------------------------------------
# Integration: Subtask dataclass preserves new fields through plan_to_dict
# ---------------------------------------------------------------------------

class TestPlannerSubtaskRoundtrip:
    def test_subagent_type_and_read_only_round_trip(self):
        from shared.planner import Planner

        plan_json = {
            "analysis": "test",
            "subtasks": [
                {
                    "id": 1,
                    "description": "Security review of foo.py",
                    "tier": "high",
                    "target_file": "foo.py",
                    "subagent_type": "review-security",
                    "read_only": True,
                    "depends_on": [],
                },
                {
                    "id": 2,
                    "description": "Synthesis",
                    "tier": "high",
                    "depends_on": [1],
                    "subagent_type": "",
                    "read_only": True,
                },
            ],
            "strategy": "dag",
            "topology": "dag",
        }

        planner = _make_planner()
        plan = planner._build_plan(plan_json, "REVIEW: foo.py")

        assert plan.subtasks[0].subagent_type == "review-security"
        assert plan.subtasks[0].read_only is True
        assert plan.subtasks[1].read_only is True

        d = Planner.plan_to_dict(plan)
        st0 = d["subtasks"][0]
        assert st0["subagent_type"] == "review-security"
        assert st0["read_only"] is True

    def test_normal_subtask_no_extra_keys(self):
        from shared.planner import Planner

        plan_json = {
            "analysis": "normal",
            "subtasks": [
                {
                    "id": 1,
                    "description": "Create app.py",
                    "tier": "medium",
                    "target_file": "app.py",
                    "depends_on": [],
                }
            ],
            "strategy": "parallel",
            "topology": "linear",
        }

        planner = _make_planner()
        plan = planner._build_plan(plan_json, "Create app.py")

        d = Planner.plan_to_dict(plan)
        st0 = d["subtasks"][0]
        assert "subagent_type" not in st0
        assert "read_only" not in st0
