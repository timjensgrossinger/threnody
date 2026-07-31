"""Tests for shared/ladder.py — the graded task ladder.

Two properties make this a benchmark rather than theatre:
  1. Every shipped case's grader ACCEPTS a correct solution (otherwise every model
     would be reported as failing).
  2. Every grader REJECTS a wrong or empty solution (otherwise every model would be
     reported as passing).
Both are asserted below against real reference solutions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared import ladder
from shared.db import Database

# Reference solutions, kept inline so the assertions above are self-contained and
# the shipped cases can never silently drift away from being solvable.
REFERENCE: dict[str, str] = {
    "l0-return-constant": "def answer():\n    return 42\n",
    "l0-fix-off-by-one": "def last_index(items):\n    return len(items) - 1\n",
    "l1-word-frequency": (
        "_PUNCT = \".,!?;:'\\\"\"\n\n\n"
        "def word_counts(text):\n"
        "    counts = {}\n"
        "    for token in (text or '').split():\n"
        "        word = token.strip(_PUNCT).lower()\n"
        "        if not word:\n"
        "            continue\n"
        "        counts[word] = counts.get(word, 0) + 1\n"
        "    return counts\n"
    ),
    "l2-lru-cache": (
        "from collections import OrderedDict\n\n\n"
        "class LRUCache:\n"
        "    def __init__(self, capacity):\n"
        "        self.capacity = max(0, int(capacity))\n"
        "        self._data = OrderedDict()\n\n"
        "    def get(self, key):\n"
        "        if key not in self._data:\n"
        "            return None\n"
        "        self._data.move_to_end(key)\n"
        "        return self._data[key]\n\n"
        "    def put(self, key, value):\n"
        "        if self.capacity == 0:\n"
        "            return\n"
        "        if key in self._data:\n"
        "            self._data.move_to_end(key)\n"
        "        self._data[key] = value\n"
        "        while len(self._data) > self.capacity:\n"
        "            self._data.popitem(last=False)\n"
    ),
    "l5-refactor-validator": (
        "_REQUIRED_FIELDS = ('name', 'email', 'age')\n\n\n"
        "def _is_empty(value):\n"
        "    return value is None or value == ''\n\n\n"
        "def validate(record):\n"
        "    errors = []\n"
        "    for field in _REQUIRED_FIELDS:\n"
        "        if field not in record:\n"
        "            errors.append(f'missing:{field}')\n"
        "        elif _is_empty(record[field]):\n"
        "            errors.append(f'empty:{field}')\n"
        "    return errors\n"
    ),
    "l6-event-store": (
        "class ConcurrencyError(RuntimeError):\n"
        "    \"\"\"Raised on an expected_version mismatch.\"\"\"\n\n\n"
        "class EventStore:\n"
        "    def __init__(self):\n"
        "        self._events = {}\n\n"
        "    def append(self, event):\n"
        "        current = self.version(event.aggregate_id)\n"
        "        if event.expected_version != current:\n"
        "            raise ConcurrencyError('version mismatch')\n"
        "        self._events.setdefault(event.aggregate_id, []).append(event)\n\n"
        "    def replay(self, aggregate_id):\n"
        "        return list(self._events.get(aggregate_id, []))\n\n"
        "    def version(self, aggregate_id):\n"
        "        return len(self._events.get(aggregate_id, []))\n"
    ),
}


def reference_executor(case: ladder.LadderCase, tier: str) -> ladder.ExecutionOutput:
    content = REFERENCE.get(case.case_id)
    if content is None:
        return ladder.ExecutionOutput(error=f"no reference for {case.case_id}")
    return ladder.ExecutionOutput(content=content, model="reference", provider="stub")


def broken_executor(case: ladder.LadderCase, tier: str) -> ladder.ExecutionOutput:
    return ladder.ExecutionOutput(content="# does nothing\n", model="broken", provider="stub")


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "cache.db")


def covered_cases() -> list[ladder.LadderCase]:
    """Shipped cases that have an inline reference solution here."""
    return [c for c in ladder.load_cases() if c.case_id in REFERENCE]


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------


class TestLoadCases:
    def test_ships_cases_across_levels(self):
        cases = ladder.load_cases()
        assert cases, "no ladder cases are shipped"
        levels = {c.level for c in cases}
        assert 0 in levels
        assert max(levels) >= 5, "ladder should reach the harder rungs"

    def test_case_ids_are_unique(self):
        ids = [c.case_id for c in ladder.load_cases()]
        assert len(ids) == len(set(ids))

    def test_cases_are_level_ordered(self):
        levels = [c.level for c in ladder.load_cases()]
        assert levels == sorted(levels)

    def test_every_case_has_a_target_and_grader(self):
        for case in ladder.load_cases():
            assert case.target_file
            assert case.grader
            assert 0 <= case.level <= 6

    def test_every_case_ships_a_hidden_test(self):
        # The grader has to have something to run, or a pass would be meaningless.
        for case in ladder.load_cases():
            assert any(
                Path(rel).name.startswith("test_") for rel in case.seed
            ), f"{case.case_id} ships no test file"

    def test_filter_by_level(self):
        assert all(c.level == 0 for c in ladder.load_cases(levels=[0]))

    def test_filter_by_case_id(self):
        got = ladder.load_cases(case_ids=["l0-return-constant"])
        assert [c.case_id for c in got] == ["l0-return-constant"]

    def test_missing_root_returns_empty(self, tmp_path: Path):
        assert ladder.load_cases(tmp_path / "nope") == []

    def test_malformed_manifest_raises(self, tmp_path: Path):
        case_dir = tmp_path / "L0" / "bad"
        case_dir.mkdir(parents=True)
        (case_dir / "case.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(RuntimeError):
            ladder.load_case(case_dir)

    def test_missing_required_field_raises(self, tmp_path: Path):
        case_dir = tmp_path / "L0" / "bad"
        case_dir.mkdir(parents=True)
        (case_dir / "case.json").write_text(
            json.dumps({"id": "x", "level": 0, "prompt": "p"}), encoding="utf-8"
        )
        with pytest.raises(RuntimeError, match="target_file"):
            ladder.load_case(case_dir)

    def test_out_of_range_level_raises(self, tmp_path: Path):
        case_dir = tmp_path / "L9" / "bad"
        case_dir.mkdir(parents=True)
        (case_dir / "case.json").write_text(
            json.dumps({"id": "x", "level": 9, "prompt": "p", "target_file": "s.py"}),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="0-6"):
            ladder.load_case(case_dir)


# ---------------------------------------------------------------------------
# Grader correctness — the load-bearing pair
# ---------------------------------------------------------------------------


class TestGraderAcceptsCorrectSolutions:
    @pytest.mark.parametrize("case_id", sorted(REFERENCE))
    def test_reference_solution_passes(self, case_id: str):
        cases = ladder.load_cases(case_ids=[case_id])
        assert cases, f"case {case_id} is not shipped"
        result = ladder.run_case(cases[0], "high", executor=reference_executor)
        assert result.passed, (
            f"grader for {case_id} rejects a correct solution:\n{result.grader_output}"
        )


class TestGraderRejectsWrongSolutions:
    @pytest.mark.parametrize("case_id", sorted(REFERENCE))
    def test_broken_solution_fails(self, case_id: str):
        cases = ladder.load_cases(case_ids=[case_id])
        result = ladder.run_case(cases[0], "low", executor=broken_executor)
        assert not result.passed, f"grader for {case_id} accepts broken code"

    def test_empty_output_fails_with_an_error(self):
        cases = ladder.load_cases(case_ids=["l0-return-constant"])
        result = ladder.run_case(
            cases[0], "low", executor=lambda c, t: ladder.ExecutionOutput(content="  ")
        )
        assert result.passed is False
        assert result.error == "empty output"

    def test_executor_error_is_recorded_not_raised(self):
        cases = ladder.load_cases(case_ids=["l0-return-constant"])
        result = ladder.run_case(
            cases[0], "low", executor=lambda c, t: ladder.ExecutionOutput(error="provider down")
        )
        assert result.passed is False
        assert result.error == "provider down"


# ---------------------------------------------------------------------------
# Sandbox behavior
# ---------------------------------------------------------------------------


class TestSandbox:
    def test_materialize_writes_seed_files(self, tmp_path: Path):
        case = ladder.load_cases(case_ids=["l0-fix-off-by-one"])[0]
        sandbox = ladder.materialize(case, tmp_path / "box")
        for rel in case.seed:
            assert (sandbox / rel).is_file()

    def test_repo_is_not_touched(self, tmp_path: Path):
        # The runner must never write into the case directory itself.
        case = ladder.load_cases(case_ids=["l0-fix-off-by-one"])[0]
        before = sorted(p.name for p in Path(case.source).rglob("*"))
        ladder.run_case(case, "high", executor=reference_executor)
        after = sorted(p.name for p in Path(case.source).rglob("*"))
        assert before == after

    def test_code_fence_is_stripped(self, tmp_path: Path):
        case = ladder.load_cases(case_ids=["l0-return-constant"])[0]
        fenced = "```python\ndef answer():\n    return 42\n```\n"
        result = ladder.run_case(
            case, "high", executor=lambda c, t: ladder.ExecutionOutput(content=fenced)
        )
        assert result.passed, "a fenced response should still grade as correct"

    def test_grader_timeout_is_a_failure(self, tmp_path: Path):
        case_dir = tmp_path / "L0" / "slow"
        (case_dir / "seed").mkdir(parents=True)
        (case_dir / "seed" / "test_x.py").write_text("def test_x():\n    pass\n")
        (case_dir / "case.json").write_text(json.dumps({
            "id": "slow", "level": 0, "prompt": "p", "target_file": "s.py",
            "grader": "python3 -c \"import time; time.sleep(5)\"",
            "grader_timeout_seconds": 1,
        }), encoding="utf-8")
        case = ladder.load_case(case_dir)
        result = ladder.run_case(
            case, "low", executor=lambda c, t: ladder.ExecutionOutput(content="x = 1\n")
        )
        assert result.passed is False
        assert "timed out" in result.grader_output

    def test_unparseable_grader_is_a_failure(self, tmp_path: Path):
        case_dir = tmp_path / "L0" / "badcmd"
        (case_dir / "seed").mkdir(parents=True)
        (case_dir / "case.json").write_text(json.dumps({
            "id": "badcmd", "level": 0, "prompt": "p", "target_file": "s.py",
            "grader": '"unterminated',
        }), encoding="utf-8")
        passed, output = ladder.grade(ladder.load_case(case_dir), tmp_path)
        assert passed is False
        assert "unparseable" in output


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


class TestBuildCasePrompt:
    def test_includes_task_and_output_contract(self):
        case = ladder.load_cases(case_ids=["l0-return-constant"])[0]
        prompt = ladder.build_case_prompt(case)
        assert case.prompt.strip() in prompt
        assert case.target_file in prompt
        assert "no markdown fences" in prompt

    def test_hidden_tests_are_not_leaked(self):
        # Showing the grader's tests would make the benchmark measure nothing.
        case = ladder.load_cases(case_ids=["l0-fix-off-by-one"])[0]
        prompt = ladder.build_case_prompt(case)
        assert "test_solution.py" not in prompt
        assert "def test_" not in prompt

    def test_visible_seed_files_are_included(self):
        case = ladder.load_cases(case_ids=["l0-fix-off-by-one"])[0]
        prompt = ladder.build_case_prompt(case)
        assert "def last_index" in prompt


# ---------------------------------------------------------------------------
# Aggregation + ledger
# ---------------------------------------------------------------------------


class TestMinPassingTier:
    def _r(self, level, tier, case_id, passed):
        return ladder.LadderResult(
            case_id=case_id, level=level, tier=tier, passed=passed, model="m"
        )

    def test_cheapest_passing_tier_wins(self):
        results = [
            self._r(0, "low", "a", True),
            self._r(0, "high", "a", True),
        ]
        assert ladder.min_passing_tier_by_level(results) == {0: "low"}

    def test_escalates_when_cheap_tier_fails(self):
        results = [
            self._r(2, "low", "a", False),
            self._r(2, "medium", "a", True),
        ]
        assert ladder.min_passing_tier_by_level(results) == {2: "medium"}

    def test_level_absent_when_nothing_passes(self):
        results = [self._r(3, "low", "a", False), self._r(3, "high", "a", False)]
        assert ladder.min_passing_tier_by_level(results) == {}

    def test_partial_sweep_does_not_count(self):
        # One of two cases passing is not evidence the tier handles the level.
        results = [
            self._r(1, "low", "a", True),
            self._r(1, "low", "b", False),
            self._r(1, "high", "a", True),
            self._r(1, "high", "b", True),
        ]
        assert ladder.min_passing_tier_by_level(results) == {1: "high"}

    def test_empty_results(self):
        assert ladder.min_passing_tier_by_level([]) == {}


class TestSummarize:
    def test_pass_rate_per_tier(self):
        results = [
            ladder.LadderResult("a", 0, "low", True),
            ladder.LadderResult("b", 1, "low", False),
            ladder.LadderResult("a", 0, "high", True),
        ]
        summary = ladder.summarize(results)
        assert summary["total_runs"] == 3
        assert summary["by_tier"]["low"]["pass_rate"] == 0.5
        assert summary["by_tier"]["high"]["pass_rate"] == 1.0

    def test_render_handles_no_sweep(self):
        summary = ladder.summarize([ladder.LadderResult("a", 0, "low", False)])
        text = ladder.render_summary(summary)
        assert "No level was swept cleanly" in text
        assert "Failures:" in text

    def test_render_lists_min_tiers(self):
        summary = ladder.summarize([ladder.LadderResult("a", 0, "low", True)])
        assert "L0: low" in ladder.render_summary(summary)


class TestLedgerRecording:
    def test_run_ladder_records_events(self, db: Database):
        cases = ladder.load_cases(case_ids=["l0-return-constant"])
        ladder.run_ladder(
            cases=cases, tiers=("low",), executor=reference_executor, db=db
        )
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT source, sub_dimension, score_0_10 FROM model_quality_events"
            ).fetchall()
        assert rows == [("ladder", "L0", 10.0)]

    def test_failure_records_zero(self, db: Database):
        cases = ladder.load_cases(case_ids=["l0-return-constant"])
        ladder.run_ladder(cases=cases, tiers=("low",), executor=broken_executor, db=db)
        with db.conn() as conn:
            score = conn.execute("SELECT score_0_10 FROM model_quality_events").fetchone()[0]
        assert score == 0.0

    def test_min_passing_tier_map_from_ledger(self, db: Database):
        cases = ladder.load_cases(case_ids=["l0-return-constant", "l2-lru-cache"])

        def tiered(case, tier):
            if tier == "high" or case.level == 0:
                return reference_executor(case, tier)
            return ladder.ExecutionOutput(content="# nope\n", model=f"model-{tier}")

        # Attribute distinct models per tier so the map is per-model.
        def tiered_named(case, tier):
            out = tiered(case, tier)
            return ladder.ExecutionOutput(
                content=out.content, error=out.error, model=f"model-{tier}", provider="stub"
            )

        ladder.run_ladder(
            cases=cases, tiers=("low", "high"), executor=tiered_named, db=db
        )
        from shared.model_quality import build_min_passing_tier_map

        mapping = build_min_passing_tier_map(db)
        assert mapping["model-low"] == {"L0": "low"}
        assert mapping["model-high"]["L2"] == "high"

    def test_no_db_still_runs(self):
        cases = ladder.load_cases(case_ids=["l0-return-constant"])
        results = ladder.run_ladder(cases=cases, tiers=("low",), executor=reference_executor)
        assert len(results) == 1 and results[0].passed

    def test_unknown_tiers_are_ignored(self):
        cases = ladder.load_cases(case_ids=["l0-return-constant"])
        results = ladder.run_ladder(
            cases=cases, tiers=("bogus",), executor=reference_executor
        )
        assert results == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_list_prints_cases(self, capsys):
        assert ladder.main(["list"]) == 0
        out = capsys.readouterr().out
        assert "l0-return-constant" in out

    def test_list_respects_level_filter(self, capsys):
        assert ladder.main(["list", "--level", "0"]) == 0
        out = capsys.readouterr().out
        assert "l0-return-constant" in out
        assert "l6-event-store" not in out

    def test_default_command_is_list(self, capsys):
        assert ladder.main([]) == 0
        assert "l0-return-constant" in capsys.readouterr().out

    def test_run_rejects_unknown_tier(self, capsys):
        assert ladder.main(["run", "--tier", "turbo"]) == 1
        assert "unknown tier" in capsys.readouterr().out

    def test_run_rejects_empty_case_selection(self, capsys):
        assert ladder.main(["run", "--case", "does-not-exist"]) == 1
        assert "no matching ladder cases" in capsys.readouterr().out
