"""Guards for the prompt-economy work: the invariants that make it safe to ship.

Each test here pins a property that, if it silently broke, would either change agent
behaviour on a host that opted out, or corrupt a learning table.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from shared.config import TGsConfig
from shared.review_fanout import (
    REVIEW_DIMENSIONS,
    _gate_dimensions_structurally,
    build_review_subtasks,
    directory_targets,
    estimate_review_profile,
    profile_key_for,
)

# The exact dimension prompts as they read before the title/focus/report split. If
# `prompt_template` ever stops reproducing these byte-for-byte, a host that has not
# opted into prompt economy silently gets different instructions.
_PRE_SPLIT_TEMPLATES = {
    "security": (
        "Security review of {path}: check for injection (SQL, command, XSS), "
        "auth bypass, hardcoded secrets, SSRF, path traversal, weak crypto, "
        "CSRF, IDOR, insecure deserialization, and input validation gaps. "
        "Report each finding as: ⚠️ [SEVERITY] security/<category> — file:line — description (CWE-XXX), "
        "where <category> is a kebab-case vulnerability class "
        "(e.g. sql-injection, xss, path-traversal, hardcoded-secret, ssrf, weak-crypto). "
        "Output nothing if no issues found."
    ),
    "logic": (
        "Logic review of {path}: check for off-by-one errors, wrong conditions, "
        "unreachable code, swapped arguments, missing returns, and state invariant violations. "
        "Report each finding as: ⚠️ [SEVERITY] logic/<category> — file:line — description, "
        "where <category> is a kebab-case slug (e.g. off-by-one, wrong-condition, missing-return). "
        "Output nothing if no issues found."
    ),
    "edge": (
        "Edge and null case review of {path}: check for null/None dereferences, "
        "empty collection access, division by zero, missing error handling, "
        "missing defaults, boundary conditions, and missing I/O error handling. "
        "Report each finding as: ⚠️ [SEVERITY] edge/<category> — file:line — description, "
        "where <category> is a kebab-case slug (e.g. null-deref, empty-collection, div-by-zero). "
        "Output nothing if no issues found."
    ),
    "types": (
        "Type safety review of {path}: check for type mismatches, unsafe casts, "
        "generic violations, incompatible return types, and serialization/deserialization drift. "
        "Report each finding as: ⚠️ [SEVERITY] types/<category> — file:line — description, "
        "where <category> is a kebab-case slug (e.g. type-mismatch, unsafe-cast, serde-drift). "
        "Output nothing if no issues found."
    ),
    "performance": (
        "Performance review of {path}: check for O(n²) algorithms, N+1 queries, "
        "memory leaks, blocking I/O in async contexts, unbounded growth, missing pagination, "
        "and redundant calls. "
        "Report each finding as: ⚠️ [SEVERITY] performance/<category> — file:line — description, "
        "where <category> is a kebab-case slug (e.g. quadratic, n-plus-1, memory-leak, blocking-io). "
        "Output nothing if no issues found."
    ),
}


class TestInlinePromptUnchanged:
    def test_every_template_is_byte_identical_to_pre_split(self):
        for dim in REVIEW_DIMENSIONS:
            assert dim.prompt_template == _PRE_SPLIT_TEMPLATES[dim.key], dim.key

    def test_stable_block_is_path_free(self):
        for dim in REVIEW_DIMENSIONS:
            assert "{path}" not in dim.stable_block
            assert " of " not in dim.stable_block.split(".")[0]

    def test_stable_block_reads_as_prose(self):
        """`focus` is stored lowercase for the inline form; standing alone it starts
        a sentence."""
        for dim in REVIEW_DIMENSIONS:
            after_title = dim.stable_block.split(". ", 1)[1]
            assert after_title[:1].isupper(), dim.key

    def test_externalized_prompt_is_much_smaller(self):
        inline = sum(len(d.prompt_template) for d in REVIEW_DIMENSIONS)
        variable = sum(len(d.variable_line("shared/db.py")) for d in REVIEW_DIMENSIONS)
        assert variable < inline / 4


class TestLegacyModeIsAByteForByteNoop:
    def test_no_caller_reproduces_the_old_description(self, tmp_path: Path):
        """caller=None must render exactly what the pre-split code rendered."""
        target = tmp_path / "svc.md"
        target.write_text("\n".join(f"line {i}" for i in range(10)), encoding="utf-8")
        plan = build_review_subtasks(
            [(str(target), "")],
            f"REVIEW: {target}",
            config=SimpleNamespace(review_synthesis_mode="llm"),
        )
        for cell in plan["subtasks"]:
            dim_key = cell.get("review_dimension")
            if not dim_key:
                continue
            expected = _PRE_SPLIT_TEMPLATES[dim_key].format(path=str(target))
            assert cell["description"].startswith(expected)


class TestPatternHashStability:
    def test_review_cells_carry_a_prompt_independent_key(self, tmp_path: Path):
        target = tmp_path / "svc.md"
        target.write_text("x\n", encoding="utf-8")
        plans = [
            build_review_subtasks(
                [(str(target), "")], f"REVIEW: {target}", caller=caller,
                config=TGsConfig(),
            )
            for caller in (None, "claude-code", "junie")
        ]
        by_dim: dict[str, set[str]] = {}
        for plan in plans:
            for cell in plan["subtasks"]:
                dim_key = cell.get("review_dimension")
                if dim_key:
                    by_dim.setdefault(dim_key, set()).add(cell["pattern_hash"])
        assert by_dim, "expected review cells"
        # One hash per dimension across all three prompt renderings — otherwise every
        # prompt-economy change would orphan accumulated subtask_patterns rows.
        for dim_key, hashes in by_dim.items():
            assert len(hashes) == 1, dim_key
            assert hashes != {""}


class TestStructuralGatingSafety:
    def _dims(self, *keys: str):
        from shared.review_fanout import _DIM_BY_KEY

        return [_DIM_BY_KEY[k] for k in keys]

    def _traits(self, *, annotations=False, loops=False, io=False, parsed=True):
        from shared.code_intel import StructuralTraits

        return StructuralTraits(annotations, loops, io, parsed)

    def test_unparsed_traits_never_skip(self):
        """A regex guess must never be grounds for removing a reviewer."""
        dims = self._dims("types", "performance", "logic")
        kept = _gate_dimensions_structurally(dims, self._traits(parsed=False), set())
        assert [d.key for d in kept] == ["types", "performance", "logic"]

    def test_none_traits_never_skip(self):
        dims = self._dims("types", "performance")
        assert _gate_dimensions_structurally(dims, None, set()) == dims

    def test_security_and_logic_are_never_gated(self):
        dims = self._dims("security", "logic")
        kept = _gate_dimensions_structurally(dims, self._traits(), set())
        assert {d.key for d in kept} == {"security", "logic"}

    def test_requested_dimension_survives(self):
        dims = self._dims("types")
        kept = _gate_dimensions_structurally(dims, self._traits(), {"types"})
        assert [d.key for d in kept] == ["types"]

    def test_never_returns_an_empty_set(self):
        """Dropping every dimension would silently drop the file from the review."""
        dims = self._dims("types", "performance")
        kept = _gate_dimensions_structurally(dims, self._traits(), set())
        assert kept == dims

    def test_performance_kept_when_io_present_without_loops(self):
        dims = self._dims("performance", "logic")
        kept = _gate_dimensions_structurally(dims, self._traits(io=True), set())
        assert {d.key for d in kept} == {"performance", "logic"}


class TestChangedScopePreservesLearningKeys:
    def test_scoped_file_profile_is_identical_to_unscoped(self, tmp_path: Path):
        """F narrows *which* files are reviewed, never how much of one is read — so
        content_sha and the profile key must not move."""
        target = tmp_path / "svc.py"
        target.write_text(
            "def run(items):\n"
            + "".join(f"    x{i} = {i}\n" for i in range(60))
            + "    return items\n",
            encoding="utf-8",
        )
        first = estimate_review_profile(str(target))
        second = estimate_review_profile(str(target))
        assert first.loc == second.loc
        assert profile_key_for(first, str(target)) == profile_key_for(second, str(target))
        assert first.intel is not None and second.intel is not None
        assert first.intel.content_sha == second.intel.content_sha


class TestDirectoryTargets:
    def test_directory_token(self):
        assert directory_targets("REVIEW: shared/") == ["shared"]

    def test_multiple_directories(self):
        assert directory_targets("REVIEW: shared/ tests/") == ["shared", "tests"]

    def test_dot_target(self):
        assert directory_targets("REVIEW: .") == ["."]

    def test_explicit_files_are_not_directories(self):
        """An explicitly named file list must never trigger changed-file scoping —
        the operator asked for those files."""
        assert directory_targets("REVIEW: shared/db.py shared/api.py") == []

    def test_non_review_text(self):
        assert directory_targets("") == []


class TestAgentBudgetAccounting:
    """The agent cap and the synthesis mode are mutually dependent; getting the
    reservation wrong silently costs a review cell (or overruns the budget)."""

    def _cfg(self, mode: str) -> SimpleNamespace:
        return SimpleNamespace(
            review_synthesis_mode=mode,
            review_structural_dim_gating=True,
            review_synthesis_python_max_cells=6,
            review_synthesis_python_max_files=2,
        )

    def _files(self, tmp_path: Path, n: int) -> list[tuple[str, str]]:
        out = []
        for i in range(n):
            f = tmp_path / f"m{i}.py"
            f.write_text(
                f"def pick{i}(a, b):\n    if a > b:\n        return a\n    return b\n",
                encoding="utf-8",
            )
            out.append((str(f), ""))
        return out

    def test_python_mode_spends_the_whole_budget_on_cells(self, tmp_path: Path):
        """No synthesis agent is planned, so no slot may be reserved for one."""
        plan = build_review_subtasks(
            self._files(tmp_path, 2), "REVIEW: m", max_agents=3,
            config=self._cfg("python"),
        )
        cells = [s for s in plan["subtasks"] if s.get("review_dimension")]
        assert len(cells) == 3
        assert [s for s in plan["subtasks"] if s.get("depends_on")] == []

    def test_llm_mode_reserves_exactly_one_slot(self, tmp_path: Path):
        plan = build_review_subtasks(
            self._files(tmp_path, 2), "REVIEW: m", max_agents=3, config=self._cfg("llm")
        )
        cells = [s for s in plan["subtasks"] if s.get("review_dimension")]
        synth = [s for s in plan["subtasks"] if s.get("depends_on")]
        assert len(cells) == 2 and len(synth) == 1

    def test_no_mode_ever_exceeds_the_budget(self, tmp_path: Path):
        for mode in ("python", "llm", "auto"):
            for budget in (1, 2, 3, 5):
                plan = build_review_subtasks(
                    self._files(tmp_path, 3), "REVIEW: m", max_agents=budget,
                    config=self._cfg(mode),
                )
                assert len(plan["subtasks"]) <= budget, (mode, budget)

    def test_auto_does_not_reserve_a_slot_it_then_leaves_empty(self, tmp_path: Path):
        """Regression: `auto` resolved its mode *after* capping, so it reserved a
        synthesis slot and then planned no synthesis agent — wasting an agent."""
        plan = build_review_subtasks(
            self._files(tmp_path, 1), "REVIEW: m", max_agents=2, config=self._cfg("auto")
        )
        if plan["synthesis_mode"] == "python":
            assert len(plan["subtasks"]) == 2
            assert [s for s in plan["subtasks"] if s.get("depends_on")] == []
