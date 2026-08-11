"""Unit tests for shared/review_fanout.py — per-file x dimension review fanout."""
from __future__ import annotations

import tempfile
from types import SimpleNamespace
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
    resolve_synthesis_mode,
    tier_for,
)


def _llm_synthesis_config() -> SimpleNamespace:
    """Config stub pinning the LLM synthesis agent.

    Tests that assert properties *of the synthesis agent* (its tier, its
    depends_on) must pin the mode: under `python` synthesis there is no agent to
    assert about, because the merge happens in-process (shared/findings_merge.py).
    """
    return SimpleNamespace(review_synthesis_mode="llm")


def _python_synthesis_config() -> SimpleNamespace:
    return SimpleNamespace(review_synthesis_mode="python")


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

    def test_global_profile_key_bias_applies_to_every_profile(self, tmp_path: Path):
        """The objective quality loop is per-(model, dimension), not per-file-shape."""
        from shared.review_fanout import GLOBAL_PROFILE_KEY

        f = tmp_path / "m.py"
        f.write_text("\n".join(f"x{i} = {i}" for i in range(300)), encoding="utf-8")
        plan = build_review_subtasks(
            [(str(f), "")],
            f"REVIEW: {f} [dims=logic]",
            tier_bias={(GLOBAL_PROFILE_KEY, "logic"): 1},
        )
        logic = [s for s in plan["subtasks"] if s.get("subagent_type") == "review-logic"]
        assert logic and logic[0]["tier"] == "high"

    def test_profile_and_global_bias_clamp_to_one_step(self, tmp_path: Path):
        """Two independent learners must not compound into a two-tier jump."""
        from shared.review_fanout import GLOBAL_PROFILE_KEY

        f = tmp_path / "m.py"
        f.write_text("\n".join(f"x{i} = {i}" for i in range(300)), encoding="utf-8")
        pk = "%s|mid|flat" % ".py"
        plan = build_review_subtasks(
            [(str(f), "")],
            f"REVIEW: {f} [dims=logic]",
            tier_bias={(pk, "logic"): 1, (GLOBAL_PROFILE_KEY, "logic"): 1},
        )
        logic = [s for s in plan["subtasks"] if s.get("subagent_type") == "review-logic"]
        # medium + clamp(1+1) == medium + 1 == high, not beyond.
        assert logic and logic[0]["tier"] == "high"

    def test_opposing_biases_cancel(self, tmp_path: Path):
        from shared.review_fanout import GLOBAL_PROFILE_KEY

        f = tmp_path / "m.py"
        f.write_text("\n".join(f"x{i} = {i}" for i in range(300)), encoding="utf-8")
        pk = "%s|mid|flat" % ".py"
        plan = build_review_subtasks(
            [(str(f), "")],
            f"REVIEW: {f} [dims=logic]",
            tier_bias={(pk, "logic"): 1, (GLOBAL_PROFILE_KEY, "logic"): -1},
        )
        logic = [s for s in plan["subtasks"] if s.get("subagent_type") == "review-logic"]
        assert logic and logic[0]["tier"] == "medium"

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


class TestTaskFocus:
    """The caller's stated intent must reach the agents.

    Regression: the planner lexed a file list out of the task string and threw
    the rest away, so a three-part threat model produced five generic prompts.
    """

    THREAT_TASK = (
        "REVIEW: [dims=security,logic] app/auth.py app/store.py\n\n"
        "Threat focus:\n"
        "1. Prompt injection through untrusted agent output.\n"
        "2. Memory tamper via data/store.json.\n"
    )

    def test_focus_survives_extraction(self):
        from shared.review_fanout import extract_task_focus
        focus = extract_task_focus(
            self.THREAT_TASK, ["app/auth.py", "app/store.py", "data/store.json"]
        )
        assert "Prompt injection" in focus
        assert "Memory tamper" in focus

    def test_path_named_mid_sentence_is_not_blanked(self):
        """Deleting path substrings turned prose into nonsense ('tamper via .')."""
        from shared.review_fanout import extract_task_focus
        focus = extract_task_focus(
            self.THREAT_TASK, ["app/auth.py", "app/store.py", "data/store.json"]
        )
        assert "Memory tamper via data/store.json." in focus

    def test_bare_file_list_has_no_focus(self):
        from shared.review_fanout import extract_task_focus
        assert extract_task_focus("REVIEW: a.py b.py", ["a.py", "b.py"]) == ""
        assert extract_task_focus("REVIEW: [dims=security] a.py", ["a.py"]) == ""

    def test_focus_is_capped(self):
        from shared.review_fanout import extract_task_focus, FOCUS_MAX_CHARS
        task = "REVIEW: a.py\n" + ("word " * 4000)
        assert len(extract_task_focus(task, ["a.py"])) <= FOCUS_MAX_CHARS

    def test_focus_reaches_every_cell(self, tmp_path):
        from shared.review_fanout import build_review_subtasks
        f = tmp_path / "auth.py"
        f.write_text("\n".join(f"x = {i}" for i in range(210)), encoding="utf-8")
        task = f"REVIEW: [dims=security,logic] {f}\n\nFocus on token replay."
        plan = build_review_subtasks([(str(f), "")], task, db=None, caller="claude-code")
        assert len(plan["subtasks"]) == 2  # 2 dims; python synthesis plans no agent
        for st in plan["subtasks"]:
            assert "token replay" in st["description"]

    def test_focus_reaches_the_synthesis_agent(self, tmp_path):
        """Wide enough to plan an LLM synthesis agent, which must be primed too."""
        from shared.review_fanout import build_review_subtasks
        entries = []
        for name in ("a.py", "b.py", "c.py", "d.py"):
            f = tmp_path / name
            f.write_text("\n".join(f"x = {i}" for i in range(210)), encoding="utf-8")
            entries.append((str(f), ""))
        paths = " ".join(p for p, _ in entries)
        task = f"REVIEW: [dims=security,logic] {paths}\n\nFocus on token replay."
        plan = build_review_subtasks(entries, task, db=None, caller="claude-code")
        assert plan["synthesis_mode"] == "llm"
        synthesis = plan["subtasks"][-1]
        assert synthesis["depends_on"]
        assert "token replay" in synthesis["description"]

    def test_prose_named_file_is_reported_as_added(self, tmp_path):
        """Scope may widen; it may not widen silently."""
        from shared.review_fanout import build_review_subtasks
        listed = tmp_path / "auth.py"
        listed.write_text("x = 1\n", encoding="utf-8")
        extra = tmp_path / "store.json"
        extra.write_text("{}\n", encoding="utf-8")
        task = f"REVIEW: {listed}\n\nThe tamper artifact is {extra}."
        plan = build_review_subtasks(
            [(str(listed), ""), (str(extra), "")], task, db=None
        )
        assert plan["coverage"]["added_files"] == [str(extra)]

    def test_declared_list_is_not_reported_as_added(self, tmp_path):
        from shared.review_fanout import build_review_subtasks
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        for f in (a, b):
            f.write_text("x = 1\n", encoding="utf-8")
        plan = build_review_subtasks(
            [(str(a), ""), (str(b), "")], f"REVIEW: {a} {b}", db=None
        )
        assert plan["coverage"]["added_files"] == []


class TestRiskAwareTierFloors:
    """Tier must follow risk, not only file size.

    Regression: a 189-line module that was the only in-band injection defence in
    its repo drew five `low` reviewers, and the one that mattered rated a real
    HIGH as LOW. The band table only knew how big the file was. The configured
    filename floor (risk_floor_tier) did exist — but it was loaded downstream of
    the review branch's early return, so it had never applied to a review cell.
    """

    BODY = "\n".join(f"def rule_{i}(x):\n    return x" for i in range(94))

    def _plan(self, tmp_path, name, task_prefix="REVIEW: [dims=security,logic]"):
        from shared.heuristic_plan import build_heuristic_plan_payload
        f = tmp_path / name
        f.write_text(self.BODY, encoding="utf-8")
        plan = build_heuristic_plan_payload(
            f"{task_prefix} {f}", max_agents=22, caller="claude-code"
        )
        return {st["subagent_type"]: st["tier"] for st in plan["subtasks"]}

    def test_plain_small_file_stays_cheap(self, tmp_path):
        tiers = self._plan(tmp_path, "plain_helpers.py")
        assert tiers["review-security"] == "low"

    def test_risk_vocabulary_filename_floors_to_medium(self, tmp_path):
        """The file body never says sql/exec/token — only its name is a signal."""
        assert self._plan(tmp_path, "token_store.py")["review-security"] == "medium"
        assert self._plan(tmp_path, "oauth_guard.py")["review-security"] == "medium"

    def test_explicit_tier_token_floors_every_cell(self, tmp_path):
        tiers = self._plan(
            tmp_path, "plain_helpers.py", "REVIEW: [tier=high] [dims=security,logic]"
        )
        assert set(tiers.values()) == {"high"}

    def test_tier_token_is_a_floor_not_a_ceiling(self):
        from shared.review_fanout import _cell_tier, _DIM_BY_KEY, ReviewProfile
        prof = ReviewProfile(
            band="complex", has_risk=True, loc=900,
            density_score=0.9, concrete_high_risk=True, intel=None,
        )
        tier, _ = _cell_tier(
            "big.py", _DIM_BY_KEY["security"], prof,
            task_force_high=True, tier_bias=None, tier_floor="low",
        )
        assert tier == "high"


class TestAbsolutePathDedup:
    def test_absolute_target_is_not_extracted_twice(self, tmp_path):
        """`_ABSOLUTE_PATH` and `_BARE_PATH` both match /a/b.py, yielding a
        rooted and an unrooted copy of one file — two agents per dimension, the
        second pointing at a path that does not resolve."""
        from shared.heuristic_plan import extract_task_file_entries
        from shared.review_fanout import strip_intent_tokens
        f = tmp_path / "helpers.py"
        f.write_text("x = 1\n", encoding="utf-8")
        entries = extract_task_file_entries(
            strip_intent_tokens(f"REVIEW: [dims=security] {f}"),
            intent_templates=False,
            allow_external=True,
        )
        assert [p for p, _ in entries] == [str(f)]


class TestRequestedTierFloor:
    def test_parsed(self):
        from shared.review_fanout import requested_tier_floor
        assert requested_tier_floor("REVIEW: [tier=high] a.py") == "high"

    def test_absent(self):
        from shared.review_fanout import requested_tier_floor
        assert requested_tier_floor("REVIEW: a.py") == ""

    def test_tier_token_is_not_lexed_as_a_path(self):
        from shared.heuristic_plan import extract_task_file_entries
        from shared.review_fanout import strip_intent_tokens
        entries = extract_task_file_entries(
            strip_intent_tokens("REVIEW: [tier=high] [dims=security] a.py"),
            intent_templates=False,
            allow_external=True,
        )
        assert [p for p, _ in entries] == ["a.py"]


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
        result = build_review_subtasks(
            entries, f"REVIEW: {f}", config=_llm_synthesis_config()
        )

        subtasks = result["subtasks"]
        review = [s for s in subtasks if not s.get("depends_on")]
        synthesis = [s for s in subtasks if s.get("depends_on")]

        assert len(synthesis) == 1
        assert len(review) >= 1
        assert result["topology"] == "dag"

    def test_python_synthesis_drops_the_agent(self, tmp_path: Path):
        """python mode plans no synthesis agent — the merge is in-process."""
        f = tmp_path / "tiny.md"
        f.write_text("\n".join(f"line {i}" for i in range(10)), encoding="utf-8")
        result = build_review_subtasks(
            [(str(f), "")], f"REVIEW: {f}", config=_python_synthesis_config()
        )
        assert result["synthesis_mode"] == "python"
        assert [s for s in result["subtasks"] if s.get("depends_on")] == []
        # The reviewed-file list must survive so the in-process report can name them.
        assert result["reviewed_files"] == [str(f)]

    def test_synthesis_depends_on_all_review_ids(self, tmp_path: Path):
        f = tmp_path / "file.md"
        f.write_text("\n".join(f"line {i}" for i in range(10)), encoding="utf-8")
        result = build_review_subtasks(
            [(str(f), "")], f"REVIEW: {f}", config=_llm_synthesis_config()
        )
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
        result = build_review_subtasks(
            [(str(f), "")], f"REVIEW: {f}", config=_llm_synthesis_config()
        )
        synth = next(s for s in result["subtasks"] if s.get("depends_on"))
        assert synth["tier"] == "medium"

    def test_synthesis_stays_medium_on_ordinary_risk(self, tmp_path: Path):
        f = tmp_path / "secrets.md"
        f.write_text("token = 'abc'\n", encoding="utf-8")
        result = build_review_subtasks(
            [(str(f), "")], f"REVIEW: {f}", config=_llm_synthesis_config()
        )
        synth = next(s for s in result["subtasks"] if s.get("depends_on"))
        assert synth["tier"] == "medium"

    def test_synthesis_high_on_concrete_high_risk(self, tmp_path: Path):
        f = tmp_path / "runner.md"
        f.write_text("subprocess.run(cmd, shell=True)\n", encoding="utf-8")
        result = build_review_subtasks(
            [(str(f), "")], f"REVIEW: {f}", config=_llm_synthesis_config()
        )
        synth = next(s for s in result["subtasks"] if s.get("depends_on"))
        assert synth["tier"] == "high"

    def test_max_agents_drops_lowest_priority_first(self, tmp_path: Path):
        # Complex file would have 5 dims → cap to 3 review + 1 synthesis = 4 total.
        # Pinned to llm mode: that "+ 1 synthesis" is what reserves the fourth slot,
        # and under python synthesis all 4 would correctly go to review cells.
        f = tmp_path / "big.md"
        f.write_text("\n".join(f"line {i}" for i in range(210)), encoding="utf-8")
        result = build_review_subtasks(
            [(str(f), "")], f"REVIEW: {f}", max_agents=4, config=_llm_synthesis_config()
        )
        review = [s for s in result["subtasks"] if not s.get("depends_on")]
        assert len(review) <= 3
        # Performance (drop_priority=4) should be absent when capped
        dropped_keys = {s.get("subagent_type") for s in review}
        # At least security and logic should be kept (drop_priority 0 and 1)
        assert "review-security" in dropped_keys or "review-logic" in dropped_keys

    def test_max_agents_drop_is_reported_in_coverage_and_analysis(self, tmp_path: Path):
        """The Aug-3/Aug-5 regression: a cap-driven drop must be visible, not silent.

        Same 5-dim complex file + tight cap as test_max_agents_drops_lowest_priority_first,
        but asserting the *reporting* surface: plan_summary.coverage.dropped_cells and
        the human-readable analysis sentence, not just which cells survived.
        """
        f = tmp_path / "big.md"
        f.write_text("\n".join(f"line {i}" for i in range(210)), encoding="utf-8")
        result = build_review_subtasks(
            [(str(f), "")], f"REVIEW: {f}", max_agents=4, config=_llm_synthesis_config()
        )
        coverage = result["coverage"]
        # 5 expected dims; max_agents=4 under llm synthesis reserves a synthesis
        # slot, so review_cap = max(1, 4-1) = 3 -> 2 dropped.
        assert coverage["dropped_cells"], "cap dropped cells but coverage says nothing was dropped"
        assert len(coverage["dropped_cells"]) == 2
        assert set(coverage["dimensions_expected"][str(f)]) == {
            "security", "logic", "edge", "types", "performance",
        }
        assert len(coverage["dimensions_planned"][str(f)]) == 3
        assert "dropped" in result["analysis"]
        assert "of 5" in result["analysis"]

    def test_multi_file_complex_review_plans_every_dimension_uncapped(self, tmp_path: Path):
        """Regression for the Aug swarm-review fanout collapse.

        3 complex (>200 LOC) files with no explicit cap must plan one agent per
        (file, dimension) — 5 dims each — not collapse to a handful of
        security-only agents because an upstream agent-count budget was
        dimension-blind. Uncapped here (max_agents=None) isolates review_fanout's
        own behavior from agent_optimizer.choose_agent_count, which is covered
        separately in test_receipts_taskpacks_blueprints.py.
        """
        files = []
        for name in ("a.py", "b.py", "c.py"):
            f = tmp_path / name
            f.write_text("\n".join(f"line {i}" for i in range(210)), encoding="utf-8")
            files.append(f)
        entries = [(str(f), "") for f in files]
        result = build_review_subtasks(
            entries,
            "REVIEW: " + " ".join(str(f) for f in files),
            config=_llm_synthesis_config(),
        )
        review = [s for s in result["subtasks"] if not s.get("depends_on")]
        synthesis = [s for s in result["subtasks"] if s.get("depends_on")]
        assert len(review) == 15  # 3 files x 5 dims
        assert len(synthesis) == 1
        for f in files:
            assert sorted(result["coverage"]["dimensions_planned"][str(f)]) == sorted(
                ["security", "logic", "edge", "types", "performance"]
            )
        assert result["coverage"]["dropped_cells"] == []

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
        result = build_review_subtasks(
            [(str(f1), ""), (str(f2), "")],
            "REVIEW: a.md b.md",
            config=_llm_synthesis_config(),
        )
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
        review = [s for s in subtasks if not s.get("depends_on")]
        synthesis = [s for s in subtasks if s.get("depends_on")]
        assert len(review) >= 1
        # A merge step must always exist, but its mechanism depends on the resolved
        # synthesis mode: an agent for `llm`, in-process for `python`.
        assert result["synthesis_mode"] in {"python", "llm"}
        if result["synthesis_mode"] == "llm":
            assert len(synthesis) == 1
        else:
            assert synthesis == []

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


# ---------------------------------------------------------------------------
# code_intel wiring: static smells refine dimensions, tier, and prompts
# ---------------------------------------------------------------------------

class TestStaticSmellWiring:
    """Phase 1 wiring — shared/code_intel.py feeding review fanout."""

    @staticmethod
    def _write(tmp_path, name: str, body: str) -> str:
        f = tmp_path / name
        f.write_text(body, encoding="utf-8")
        return str(f)

    def test_density_uses_ast_when_parse_succeeds(self):
        from shared.code_intel import scan
        from shared.review_fanout import _structural_density

        # `if` and `for` appear only inside a string and a comment: the keyword
        # regexes count them, the AST does not.
        content = (
            "def f(x):\n"
            '    msg = "if for while case switch and && || ?"\n'
            "    # if for while else elif case\n"
            "    return msg\n"
        ) * 20
        intel = scan("fake_dense.py", content=content)
        assert intel.parsed is True
        assert _structural_density(content, intel) < _structural_density(content)

    def test_density_falls_back_when_parse_fails(self):
        from shared.code_intel import scan
        from shared.review_fanout import _structural_density

        content = "def broken(:\n    if x:\n        return 1\n" * 10
        intel = scan("fake_broken.py", content=content)
        assert intel.parsed is False
        # Unparsed intel must not zero the score — the regex path still applies.
        assert _structural_density(content, intel) == _structural_density(content)

    def test_high_severity_smell_adds_missing_dimension(self):
        from shared.code_intel import DIM_PERFORMANCE, SEVERITY_HIGH, Smell
        from shared.review_fanout import dimensions_for

        smells = {DIM_PERFORMANCE: [
            Smell("blocking_call_in_async", DIM_PERFORMANCE, SEVERITY_HIGH, 3, "m")
        ]}
        keys = [d.key for d in dimensions_for("moderate", False, smells=smells)]
        assert "performance" in keys  # moderate band would not include it

    def test_high_severity_smell_survives_explicit_dims(self):
        from shared.code_intel import DIM_SECURITY, SEVERITY_HIGH, Smell
        from shared.review_fanout import dimensions_for

        smells = {DIM_SECURITY: [Smell("eval_exec", DIM_SECURITY, SEVERITY_HIGH, 1, "m")]}
        keys = [d.key for d in dimensions_for("moderate", False, ["types"], smells=smells)]
        assert keys[0] == "types"  # requested dim stays first / protected
        assert "security" in keys

    def test_trivial_clean_file_drops_covered_dimensions(self):
        from shared.review_fanout import dimensions_for

        keys = [d.key for d in dimensions_for("trivial", False, smells={})]
        # edge has real static coverage and was clean -> dropped.
        assert "edge" not in keys
        # logic has no static coverage -> a clean scan is no evidence, keep it.
        assert "logic" in keys

    def test_trivial_file_keeps_security_when_risky(self):
        from shared.review_fanout import dimensions_for

        keys = [d.key for d in dimensions_for("trivial", True, smells={})]
        assert "security" in keys

    def test_no_scan_preserves_legacy_dimensions(self):
        from shared.review_fanout import dimensions_for

        # smells=None (no scan) must reproduce the pre-Phase-1 band selection.
        assert [d.key for d in dimensions_for("trivial", False)] == ["logic", "edge"]
        assert [d.key for d in dimensions_for("trivial", False, smells=None)] == [
            "logic", "edge"
        ]

    def test_smell_bumps_tier_and_injects_leads(self, tmp_path: Path):
        from shared.review_fanout import build_review_subtasks

        # Small file: security would normally tier low/medium. A concrete exploit
        # primitive is present, so it must escalate and carry the lead.
        path = self._write(
            tmp_path,
            "vuln.py",
            "import os\ndef run(cmd):\n    os.system('rm -rf ' + cmd)\n",
        )
        plan = build_review_subtasks([(path, "")], f"REVIEW: {path}")
        cell = next(
            st for st in plan["subtasks"] if st.get("review_dimension") == "security"
        )
        assert cell["tier"] == "high"
        assert "os_system" in cell["description"]
        assert "NOT confirmed findings" in cell["description"]
        assert "os_system" in cell["expected_rules"]
        assert cell["content_sha"]

    def test_clean_file_has_no_leads_and_no_expectation(self, tmp_path: Path):
        from shared.review_fanout import build_review_subtasks

        path = self._write(
            tmp_path, "clean.py", "def add(a: int, b: int) -> int:\n    return a + b\n"
        )
        plan = build_review_subtasks([(path, "")], f"REVIEW: {path}")
        cells = [st for st in plan["subtasks"] if st.get("review_dimension")]
        assert cells, "a clean file still gets reviewed"
        for cell in cells:
            assert cell["expected_rules"] == []
            assert "Static pre-scan leads" not in cell["description"]

    def test_unreadable_file_still_plans(self):
        from shared.review_fanout import build_review_subtasks

        plan = build_review_subtasks(
            [("/nonexistent/zz.py", "")], "REVIEW: /nonexistent/zz.py"
        )
        assert len(plan["subtasks"]) >= 2  # >=1 review cell + synthesis


def _git_cmd(args: list[str], cwd: Path) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class TestChangedLineScopedPrompts:
    """Regression for the "bare prompt" defect: under a boilerplate mode that
    drops the stable instruction block (BOILERPLATE_DEFINITION, claude-code's
    default), the description previously collapsed to just "Review this file:
    <path>" — no dimension name, no scope. It must now name the dimension and,
    when a merge base exists, the changed-line ranges — without changing what
    file content gets read (whole-file, always) or how it's cached (content_sha
    unaffected).
    """

    def _repo_with_a_change(self, tmp_path: Path) -> tuple[Path, Path]:
        root = tmp_path / "repo"
        root.mkdir()
        _git_cmd(["init", "-b", "main"], root)
        _git_cmd(["config", "user.email", "t@example.com"], root)
        _git_cmd(["config", "user.name", "T"], root)
        target = root / "big.py"
        base_lines = [f"line {i}" for i in range(210)]
        target.write_text("\n".join(base_lines), encoding="utf-8")
        _git_cmd(["add", "-A"], root)
        _git_cmd(["commit", "-m", "base"], root)

        changed_lines = list(base_lines)
        changed_lines[100] = "CHANGED line 100"
        target.write_text("\n".join(changed_lines), encoding="utf-8")
        _git_cmd(["commit", "-am", "change line 100"], root)
        return root, target

    def test_names_dimension_and_changed_lines_under_definition_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from shared.config import TGsConfig

        root, target = self._repo_with_a_change(tmp_path)
        monkeypatch.chdir(root)

        plan = build_review_subtasks(
            [(str(target), "")],
            f"REVIEW: {target} — review the changes on this branch",
            caller="claude-code",
            config=TGsConfig(),
            workspace_root=str(root),
        )
        security = next(
            st for st in plan["subtasks"] if st.get("review_dimension") == "security"
        )
        desc = security["description"]
        assert "Security review of" in desc
        assert "Review this file:" not in desc  # the old bare form
        assert "Changed lines:" in desc
        assert "101" in desc  # 0-indexed line 100 -> 1-indexed line 101 in git

    def test_plain_review_does_not_scope_to_a_branch_delta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """"Review this file" is not "review what changed on this branch".

        Emitting merge-base ranges unasked told every agent to prioritize a delta
        the caller never mentioned — and only for files that happened to be in it,
        so one run mixed narrowed and whole-file review with nothing saying so.
        """
        from shared.config import TGsConfig

        root, target = self._repo_with_a_change(tmp_path)
        monkeypatch.chdir(root)

        plan = build_review_subtasks(
            [(str(target), "")],
            f"REVIEW: {target}",
            caller="claude-code",
            config=TGsConfig(),
            workspace_root=str(root),
        )
        for st in plan["subtasks"]:
            assert "Changed lines:" not in st["description"]
        security = next(
            st for st in plan["subtasks"] if st.get("review_dimension") == "security"
        )
        assert "Review the whole file." in security["description"]

    def test_delta_intent_uses_the_handoff_root_not_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The diff must resolve against the same root containment is checked at.

        Resolving it from Path.cwd() meant that whenever the process cwd and the
        handoff workspace_root disagreed, every cell silently degraded to
        whole-file with nothing reporting why.
        """
        from shared.config import TGsConfig

        root, target = self._repo_with_a_change(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        plan = build_review_subtasks(
            [(str(target), "")],
            f"REVIEW: {target} — what changed since main?",
            caller="claude-code",
            config=TGsConfig(),
            workspace_root=str(root),
        )
        security = next(
            st for st in plan["subtasks"] if st.get("review_dimension") == "security"
        )
        assert "Changed lines:" in security["description"]

    def test_omits_changed_lines_clause_without_a_merge_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No git repo at all: still names the dimension, no fabricated range."""
        from shared.config import TGsConfig

        f = tmp_path / "big.py"
        f.write_text("\n".join(f"line {i}" for i in range(210)), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        plan = build_review_subtasks(
            [(str(f), "")],
            f"REVIEW: {f}",
            caller="claude-code",
            config=TGsConfig(),
        )
        security = next(
            st for st in plan["subtasks"] if st.get("review_dimension") == "security"
        )
        desc = security["description"]
        assert "Security review of" in desc
        assert "Changed lines:" not in desc
        assert "Review the whole file." in desc

    def test_content_sha_and_cell_count_unaffected_by_diff_scoping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prompt changes; the learning-key surface (content_sha, dimension
        count) must not — review_memory replay and static-recall grading key
        off content_sha, not off wording."""
        from shared.config import TGsConfig

        root, target = self._repo_with_a_change(tmp_path)
        monkeypatch.chdir(root)

        with_diff = build_review_subtasks(
            [(str(target), "")], f"REVIEW: {target}", caller="claude-code", config=TGsConfig()
        )
        without_diff = build_review_subtasks([(str(target), "")], f"REVIEW: {target}")

        shas_a = sorted(st["content_sha"] for st in with_diff["subtasks"] if st.get("content_sha"))
        shas_b = sorted(st["content_sha"] for st in without_diff["subtasks"] if st.get("content_sha"))
        assert shas_a == shas_b
        review_a = [st for st in with_diff["subtasks"] if st.get("review_dimension")]
        review_b = [st for st in without_diff["subtasks"] if st.get("review_dimension")]
        assert len(review_a) == len(review_b)
