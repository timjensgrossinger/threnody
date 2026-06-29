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
    is_fast_review_intent,
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

    def test_fast_review_sentinel_is_review_intent(self):
        assert is_review_intent("FAST_REVIEW: src/a.py src/b.py") is True
        assert is_fast_review_intent("FAST_REVIEW: src/a.py") is True


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

    def test_security_complex_without_risk_is_medium(self):
        assert tier_for(self._dim("security"), "complex", False) == "medium"

    def test_security_with_ordinary_risk_is_medium(self):
        assert tier_for(self._dim("security"), "trivial", True) == "medium"

    def test_security_with_concrete_high_risk_is_high(self):
        assert tier_for(
            self._dim("security"), "trivial", True, concrete_high_risk=True
        ) == "high"

    def test_security_explicit_high_request_is_high(self):
        assert tier_for(self._dim("security"), "moderate", False, force_high=True) == "high"

    def test_logic_trivial_is_low(self):
        assert tier_for(self._dim("logic"), "trivial", False) == "low"

    def test_logic_moderate_is_medium(self):
        assert tier_for(self._dim("logic"), "moderate", False) == "medium"

    def test_performance_complex_no_risk_is_medium(self):
        # Legacy 2-band behavior preserved when loc is omitted.
        assert tier_for(self._dim("performance"), "complex", False) == "medium"

    # --- LOC-aware tiering (loc passed) ---

    def test_small_reasoning_light_is_low(self):
        assert tier_for(self._dim("types"), "complex", False, loc=100) == "low"

    def test_small_reasoning_heavy_is_low(self):
        assert tier_for(self._dim("performance"), "complex", False, loc=200) == "low"

    def test_mid_reasoning_heavy_is_medium(self):
        assert tier_for(self._dim("performance"), "complex", False, loc=400) == "medium"

    def test_large_reasoning_heavy_is_high(self):
        assert tier_for(self._dim("performance"), "complex", False, loc=700) == "high"
        assert tier_for(self._dim("logic"), "complex", False, loc=700) == "high"

    def test_large_reasoning_light_stays_medium(self):
        # edge/types never auto-escalate to high on size alone.
        assert tier_for(self._dim("types"), "complex", False, loc=900) == "medium"
        assert tier_for(self._dim("edge"), "complex", False, loc=900) == "medium"

    def test_security_with_ordinary_risk_is_medium_any_size(self):
        assert tier_for(self._dim("security"), "trivial", True, loc=50) == "medium"

    def test_boundary_230_is_medium(self):
        # _LOC_LOW boundary is exclusive: exactly 230 → not low.
        assert tier_for(self._dim("logic"), "complex", False, loc=230) == "medium"

    # --- learned tier bias ---

    def test_bias_up_escalates(self):
        # medium heuristic + learned +1 → high.
        assert tier_for(self._dim("logic"), "complex", False, loc=400, bias=1) == "high"

    def test_bias_down_deescalates(self):
        assert tier_for(self._dim("logic"), "complex", False, loc=400, bias=-1) == "low"

    def test_bias_clamps_at_bounds(self):
        # already low, bias -1 stays low; already high, bias +1 stays high.
        assert tier_for(self._dim("types"), "complex", False, loc=100, bias=-2) == "low"
        assert tier_for(
            self._dim("performance"), "complex", False, loc=700, density_score=0.3, bias=2
        ) == "high"

    def test_bias_can_deescalate_ordinary_security_risk(self):
        assert tier_for(self._dim("security"), "trivial", True, loc=100, bias=-2) == "low"

    def test_bias_never_overrides_concrete_security_high_risk(self):
        assert tier_for(
            self._dim("security"),
            "trivial",
            True,
            loc=100,
            concrete_high_risk=True,
            bias=-2,
        ) == "high"

    def test_bias_zero_is_noop(self):
        assert tier_for(self._dim("logic"), "complex", False, loc=400, bias=0) == "medium"

    # --- profile_key_for ---

    def test_profile_key_transferable(self):
        from shared.review_fanout import profile_key_for
        from shared.review_fanout import ReviewProfile
        prof = ReviewProfile("complex", False, 250, 0.6)
        # Same shape, different paths → same key (path-independent).
        k1 = profile_key_for(prof, "a/b/llm_client.py")
        k2 = profile_key_for(prof, "totally/other/thing.py")
        assert k1 == k2 == ".py|mid|dense"

    def test_build_review_subtasks_applies_bias(self, tmp_path: Path):
        # A flat mid-size .py logic review is medium; a learned +1 bias lifts it.
        f = tmp_path / "m.py"
        f.write_text("\n".join(f"x{i} = {i}" for i in range(300)), encoding="utf-8")
        pk = "%s|mid|flat" % ".py"
        plan = build_review_subtasks(
            [(str(f), "")],
            f"REVIEW: {f} [dims=logic]",
            tier_bias={(pk, "logic"): 1},
        )
        logic = [s for s in plan["subtasks"] if s.get("subagent_type") == "review-logic"]
        assert logic and logic[0]["tier"] == "high"
        # Without bias the same cell is medium.
        plan2 = build_review_subtasks([(str(f), "")], f"REVIEW: {f} [dims=logic]")
        logic2 = [s for s in plan2["subtasks"] if s.get("subagent_type") == "review-logic"]
        assert logic2 and logic2[0]["tier"] == "medium"

    # --- density-aware tiering (density_score passed) ---

    def test_dense_midsize_reasoning_heavy_escalates_to_high(self):
        # 250 LOC but dense + reasoning-heavy → high, where LOC alone gave medium.
        assert tier_for(
            self._dim("performance"), "complex", False, loc=250, density_score=0.6
        ) == "high"

    def test_flat_large_reasoning_heavy_held_at_medium(self):
        # 700 LOC but flat (config-ish) → medium instead of LOC-only high.
        assert tier_for(
            self._dim("performance"), "complex", False, loc=700, density_score=0.05
        ) == "medium"

    def test_large_reasoning_heavy_with_moderate_density_stays_high(self):
        # Real code (density above the flat floor) keeps the prior escalation.
        assert tier_for(
            self._dim("logic"), "complex", False, loc=700, density_score=0.3
        ) == "high"

    def test_dense_small_reasoning_heavy_climbs_to_medium(self):
        # Sub-230 but dense + reasoning-heavy → medium over low.
        assert tier_for(
            self._dim("logic"), "complex", False, loc=180, density_score=0.5
        ) == "medium"

    def test_dense_small_reasoning_light_stays_low(self):
        # Density only lifts reasoning-heavy dims; edge/types stay low when small.
        assert tier_for(
            self._dim("edge"), "complex", False, loc=180, density_score=0.9
        ) == "low"

    def test_density_omitted_preserves_loc_only_escalation(self):
        # density_score=None → exact legacy LOC-only behavior.
        assert tier_for(self._dim("performance"), "complex", False, loc=700) == "high"
        assert tier_for(self._dim("performance"), "complex", False, loc=250) == "medium"


class TestStructuralDensity:
    def test_flat_file_low_density(self):
        from shared.review_fanout import _structural_density
        flat = "\n".join(f"FIELD_{i} = {i}" for i in range(60))
        assert _structural_density(flat) < 0.18

    def test_nested_branchy_file_high_density(self):
        from shared.review_fanout import _structural_density
        nested = (
            "def f(x):\n"
            "    if x:\n"
            "        for i in x:\n"
            "            while i:\n"
            "                if i and x:\n"
            "                    try:\n"
            "                        return i\n"
            "                    except Exception:\n"
            "                        continue\n"
        ) * 5
        assert _structural_density(nested) >= 0.45

    def test_nested_outscores_flat(self, tmp_path: Path):
        from shared.review_fanout import _structural_density
        flat = "\n".join(f"x{i} = {i}" for i in range(80))
        nested = (
            "func handle(a) {\n"
            "  if (a) {\n"
            "    for (i) {\n"
            "      while (i) { if (i && a) { return i } }\n"
            "    }\n"
            "  }\n"
            "}\n"
        ) * 8
        assert _structural_density(nested) > _structural_density(flat)

    def test_empty_content_is_zero(self):
        from shared.review_fanout import _structural_density
        assert _structural_density("") == 0.0
        assert _structural_density("\n\n   \n") == 0.0

    def test_comment_only_lines_ignored(self):
        from shared.review_fanout import _effective_loc
        content = "# a\n# b\nx = 1\n// c\ny = 2\n"
        assert _effective_loc(content) == 2

    def test_profile_carries_density(self, tmp_path: Path):
        from shared.review_fanout import estimate_review_profile
        f = tmp_path / "nested.py"
        f.write_text(
            (
                "def g(x):\n"
                "    if x:\n"
                "        for i in x:\n"
                "            if i:\n"
                "                return i\n"
            ) * 10,
            encoding="utf-8",
        )
        prof = estimate_review_profile(str(f))
        assert prof.density_score > 0.0

    def test_profile_back_compat_three_positional(self):
        from shared.review_fanout import ReviewProfile
        prof = ReviewProfile("complex", True, 300)
        assert prof.density_score == 0.0


# ---------------------------------------------------------------------------
# estimate_review_profile + requested dimensions
# ---------------------------------------------------------------------------

class TestEstimateReviewProfile:
    def test_returns_loc(self, tmp_path: Path):
        from shared.review_fanout import estimate_review_profile
        f = tmp_path / "f.md"
        f.write_text("\n".join(f"line {i}" for i in range(42)), encoding="utf-8")
        prof = estimate_review_profile(str(f))
        assert prof.loc == 42
        assert prof.has_risk is False

    def test_unreadable_defaults_mid(self):
        from shared.review_fanout import estimate_review_profile, _LOC_COMPLEX
        prof = estimate_review_profile("/nonexistent/path/zzz.py")
        assert prof.loc == _LOC_COMPLEX
        assert prof.band == "moderate"


class TestRequestedDimensions:
    def test_bracket_single(self):
        from shared.review_fanout import _requested_dimensions
        assert _requested_dimensions("REVIEW: [dims=performance] a.py") == ["performance"]

    def test_bracket_multi(self):
        from shared.review_fanout import _requested_dimensions
        assert _requested_dimensions("REVIEW: [dims=performance,security] a.py") == [
            "performance",
            "security",
        ]

    def test_alias_perf(self):
        from shared.review_fanout import _requested_dimensions
        assert _requested_dimensions("REVIEW: [dims=perf] a.py") == ["performance"]

    def test_unknown_keys_dropped(self):
        from shared.review_fanout import _requested_dimensions
        assert _requested_dimensions("REVIEW: [dims=foo,performance] a.py") == ["performance"]

    def test_bare_word_fallback(self):
        from shared.review_fanout import _requested_dimensions
        assert _requested_dimensions("REVIEW: performance review of a.py") == ["performance"]

    def test_no_intent_returns_empty(self):
        from shared.review_fanout import _requested_dimensions
        assert _requested_dimensions("REVIEW: a.py b.py") == []

    def test_strip_dims_token(self):
        from shared.review_fanout import strip_dims_token
        out = strip_dims_token("REVIEW: [dims=performance] a.py b.py")
        assert "[dims=" not in out
        assert "a.py" in out and "b.py" in out


class TestDimensionsForRequested:
    def test_requested_only_runs_named(self):
        dims = dimensions_for("complex", False, requested=["performance"])
        assert [d.key for d in dims] == ["performance"]

    def test_requested_adds_security_on_risk(self):
        dims = dimensions_for("complex", True, requested=["performance"])
        keys = [d.key for d in dims]
        assert "performance" in keys and "security" in keys

    def test_requested_does_not_add_security_without_risk(self):
        dims = dimensions_for("complex", False, requested=["performance"])
        assert "security" not in [d.key for d in dims]

    def test_empty_requested_falls_back_to_band(self):
        dims = dimensions_for("moderate", False, requested=[])
        assert {d.key for d in dims} == {"logic", "edge", "types"}


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

    def test_complex_ordinary_risky_file_gets_security_medium_tier(self, tmp_path: Path):
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
        assert sec["tier"] == "medium"

    def test_concrete_high_risk_file_gets_security_high_tier(self, tmp_path: Path):
        f = tmp_path / "big.md"
        lines = ["subprocess.run(cmd, shell=True)"] + [f"line {i}" for i in range(210)]
        f.write_text("\n".join(lines), encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: {f}")
        sec = next(
            (s for s in result["subtasks"] if s.get("subagent_type") == "review-security"),
            None,
        )
        assert sec is not None
        assert sec["tier"] == "high"

    def test_ordinary_security_review_worker_is_medium_tier(self, tmp_path: Path):
        # Mid-sized file (230–600 LOC), no risk signals → security worker = medium.
        # (Files < 230 LOC now tier to low; > 600 reasoning-heavy → high.)
        f = tmp_path / "ordinary.md"
        f.write_text("\n".join(f"line {i}" for i in range(300)), encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: security review {f}")
        sec = next(s for s in result["subtasks"] if s.get("subagent_type") == "review-security")
        assert sec["tier"] == "medium"

    def test_explicit_deep_security_review_escalates_security_worker(self, tmp_path: Path):
        f = tmp_path / "ordinary.md"
        f.write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: deep security review {f}")
        sec = next(s for s in result["subtasks"] if s.get("subagent_type") == "review-security")
        assert sec["tier"] == "high"

    def test_synthesis_defaults_to_medium(self, tmp_path: Path):
        f = tmp_path / "tiny.md"
        f.write_text("\n".join(f"line {i}" for i in range(10)), encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: {f}")
        synth = next(s for s in result["subtasks"] if s.get("depends_on"))
        assert synth["tier"] == "medium"

    def test_synthesis_stays_medium_on_ordinary_risk(self, tmp_path: Path):
        f = tmp_path / "secrets.md"
        f.write_text("token = 'abc'\n", encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: {f}")
        synth = next(s for s in result["subtasks"] if s.get("depends_on"))
        assert synth["tier"] == "medium"

    def test_synthesis_high_on_concrete_high_risk(self, tmp_path: Path):
        f = tmp_path / "runner.md"
        f.write_text("subprocess.run(cmd, shell=True)\n", encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: {f}")
        synth = next(s for s in result["subtasks"] if s.get("depends_on"))
        assert synth["tier"] == "high"

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

    def test_requested_dim_survives_cap_over_security(self, tmp_path: Path):
        # Defect-3 regression: a risky file + explicit [dims=performance] under a
        # tight cap must KEEP performance — it is drop-protected — even though the
        # file also triggers security (which is only ADDED, never evicting).
        f = tmp_path / "risky.py"
        lines = ["password = 'x'"] + [f"line {i}" for i in range(210)]
        f.write_text("\n".join(lines), encoding="utf-8")
        result = build_review_subtasks(
            [(str(f), "")], f"REVIEW: [dims=performance] {f}", max_agents=2
        )
        review = [s for s in result["subtasks"] if not s.get("depends_on")]
        kept = {s.get("subagent_type") for s in review}
        assert "review-performance" in kept

    def test_performance_intent_does_not_collapse_to_security(self, tmp_path: Path):
        # Even when the file has risk signals, a performance request keeps a
        # performance agent (it is not silently replaced by security-only).
        f = tmp_path / "svc.py"
        lines = ["token = get_secret()"] + [f"line {i}" for i in range(300)]
        f.write_text("\n".join(lines), encoding="utf-8")
        result = build_review_subtasks([(str(f), "")], f"REVIEW: [dims=performance] {f}")
        review_types = {
            s.get("subagent_type")
            for s in result["subtasks"]
            if not s.get("depends_on")
        }
        assert "review-performance" in review_types

    def test_synthesis_scales_high_on_many_agents(self, tmp_path: Path):
        # >=12 review cells → high-tier synthesis even with no risk.
        entries = []
        for i in range(6):
            f = tmp_path / f"f{i}.md"
            # 210 LOC .md → complex band → 5 dims each (no risk) = 30 cells
            f.write_text("\n".join(f"line {j}" for j in range(210)), encoding="utf-8")
            entries.append((str(f), ""))
        task = "REVIEW: " + " ".join(p for p, _ in entries)
        result = build_review_subtasks(entries, task)
        synth = next(s for s in result["subtasks"] if s.get("depends_on"))
        assert synth["tier"] == "high"

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

    def test_fast_review_one_agent_per_file_plus_synthesis(self, tmp_path: Path):
        files = []
        for name in ("a.py", "b.py", "c.py"):
            f = tmp_path / name
            f.write_text("x = 1\n", encoding="utf-8")
            files.append(f)
        result = build_review_subtasks(
            [(str(f), "") for f in files],
            "FAST_REVIEW: " + " ".join(str(f) for f in files),
            max_agents=4,
        )
        review = [s for s in result["subtasks"] if not s.get("depends_on")]
        synthesis = [s for s in result["subtasks"] if s.get("depends_on")]
        assert result["review_mode"] == "fast_file"
        assert len(review) == 3
        assert len(synthesis) == 1
        assert all(s["subagent_type"] == "review-fast-file" for s in review)
        assert all(s["tier"] == "medium" for s in review)
        assert synthesis[0]["tier"] == "medium"

    def test_fast_review_ordinary_risk_stays_medium(self, tmp_path: Path):
        risky = tmp_path / "auth.py"
        risky.write_text("token = request.headers['Authorization']\n", encoding="utf-8")
        ordinary = tmp_path / "plain.py"
        ordinary.write_text("x = 1\n", encoding="utf-8")
        result = build_review_subtasks(
            [(str(risky), ""), (str(ordinary), "")],
            f"FAST_REVIEW: {risky} {ordinary}",
        )
        review = [s for s in result["subtasks"] if not s.get("depends_on")]
        synthesis = next(s for s in result["subtasks"] if s.get("depends_on"))
        tiers = {Path(s["target_file"]).name: s["tier"] for s in review}
        assert tiers == {"auth.py": "medium", "plain.py": "medium"}
        assert synthesis["tier"] == "medium"

    def test_fast_review_high_tier_on_concrete_high_risk(self, tmp_path: Path):
        risky = tmp_path / "runner.py"
        risky.write_text("subprocess.run(cmd, shell=True)\n", encoding="utf-8")
        ordinary = tmp_path / "plain.py"
        ordinary.write_text("x = 1\n", encoding="utf-8")
        result = build_review_subtasks(
            [(str(risky), ""), (str(ordinary), "")],
            f"FAST_REVIEW: {risky} {ordinary}",
        )
        review = [s for s in result["subtasks"] if not s.get("depends_on")]
        synthesis = next(s for s in result["subtasks"] if s.get("depends_on"))
        tiers = {Path(s["target_file"]).name: s["tier"] for s in review}
        assert tiers == {"runner.py": "high", "plain.py": "medium"}
        assert synthesis["tier"] == "high"

    def test_fast_review_respects_max_agents_cap(self, tmp_path: Path):
        files = []
        for i in range(5):
            f = tmp_path / f"f{i}.py"
            f.write_text("x = 1\n", encoding="utf-8")
            files.append(f)
        result = build_review_subtasks(
            [(str(f), "") for f in files],
            "FAST_REVIEW: " + " ".join(str(f) for f in files),
            max_agents=3,
        )
        review = [s for s in result["subtasks"] if not s.get("depends_on")]
        assert len(review) == 2
        assert result["dropped_file_count"] == 3


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
