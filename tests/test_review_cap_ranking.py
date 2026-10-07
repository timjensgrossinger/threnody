"""Agent-cap ranking, loud drops, the literal findings format and review effort.

An 11-file review at ``max_agents=9`` once kept the smallest, last-listed files and
dropped the largest and riskiest, with every security cell gone — and the prompts
pointed at "the format given above" that no longer existed. These pin the repairs.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_server
from shared.config import TGsConfig
from shared.db import Database
from shared.findings_merge import (
    FINDINGS_LINE_FORMAT,
    findings_line_example,
    findings_line_format,
    parse_findings_text,
)
from shared.review_fanout import (
    REVIEW_DIMENSIONS,
    _review_effort,
    build_review_subtasks,
    estimate_review_profile,
    select_review_cells,
)


@pytest.fixture(autouse=True)
def _default_config_not_the_install(monkeypatch, tmp_path):
    import shared.config as _config

    monkeypatch.setattr(_config, "CONFIG_YAML", tmp_path / "no-config.yaml")


_PY = SimpleNamespace(review_synthesis_mode="python")
_CLEAN_LINE = "def f{i}(x):\n    return x + {i}\n"


def _clean(path: Path, lines: int) -> str:
    path.write_text("".join(_CLEAN_LINE.format(i=i) for i in range(lines // 2)), encoding="utf-8")
    return str(path)


def _risky(path: Path, lines: int, *, concrete: bool) -> str:
    head = "subprocess.run(cmd, shell=True)\n" if concrete else "password = load_password()\n"
    body = "".join(_CLEAN_LINE.format(i=i) for i in range(lines // 2))
    path.write_text(head + body, encoding="utf-8")
    return str(path)


def _observed_scenario(tmp_path: Path) -> list[str]:
    """11 files listed small-first: the big and risky ones are at the END."""
    paths = [_clean(tmp_path / f"small_{i}.py", 20) for i in range(5)]
    paths += [_clean(tmp_path / f"mid_{i}.py", 150) for i in range(3)]
    paths.append(_clean(tmp_path / "big.py", 700))
    paths.append(_risky(tmp_path / "exploit.py", 60, concrete=True))
    paths.append(_risky(tmp_path / "authish.py", 80, concrete=False))
    return paths


def _plan(paths: list[str], *, cap: int, prefix: str = "REVIEW:") -> dict:
    return build_review_subtasks(
        [(p, "") for p in paths], f"{prefix} " + " ".join(paths), max_agents=cap, config=_PY
    )


def _cells(plan: dict) -> list[tuple[str, str]]:
    return [
        (Path(s["target_file"]).name, s["review_dimension"])
        for s in plan["subtasks"]
        if s.get("review_dimension")
    ]


class TestCapRanking:
    def test_cap_keeps_the_large_and_risky_files(self, tmp_path: Path):
        plan = _plan(_observed_scenario(tmp_path), cap=9)
        cells = _cells(plan)
        assert len(cells) == 9
        kept_files = {name for name, _ in cells}
        # The files the old order threw away because they were listed last.
        assert {"exploit.py", "authish.py", "big.py"} <= kept_files
        # The files that lose everything are small clean ones from the front of the list.
        assert plan["coverage"]["dropped_cells"]
        assert {name for name in (Path(p).name for p in _observed_scenario(tmp_path))} - kept_files <= {
            f"small_{i}.py" for i in range(5)
        } | {f"mid_{i}.py" for i in range(3)}

    def test_every_file_gets_a_cell_before_any_gets_a_second(self, tmp_path: Path):
        paths = _observed_scenario(tmp_path)
        plan = _plan(paths, cap=len(paths))  # exactly one slot per file, 20+ cells expected
        cells = _cells(plan)
        assert len(cells) == len(paths)
        assert {name for name, _ in cells} == {Path(p).name for p in paths}

    def test_slots_beyond_the_file_count_deepen_the_big_files(self, tmp_path: Path):
        paths = _observed_scenario(tmp_path)
        cells = _cells(_plan(paths, cap=len(paths) + 3))
        per_file: dict[str, int] = {}
        for name, _ in cells:
            per_file[name] = per_file.get(name, 0) + 1
        assert set(per_file) == {Path(p).name for p in paths}
        # The extra three go to the highest-scored files, not to the first listed.
        assert per_file["exploit.py"] >= 2
        assert per_file["small_0.py"] == 1

    def test_security_cells_are_reserved_for_the_risky_files(self, tmp_path: Path):
        cells = _cells(_plan(_observed_scenario(tmp_path), cap=9))
        security = {name for name, dim in cells if dim == "security"}
        assert len(security) >= 2  # max(1, 9 // 4)
        assert {"exploit.py", "authish.py"} <= security

    def test_an_explicit_dims_list_without_security_stands_the_reserve_down(self, tmp_path: Path):
        paths = _observed_scenario(tmp_path)
        plan = _plan(paths, cap=9, prefix="REVIEW: [dims=logic]")
        dims = {dim for _, dim in _cells(plan)}
        assert "security" not in dims

    def test_ranking_is_deterministic(self, tmp_path: Path):
        paths = _observed_scenario(tmp_path)
        first = _plan(paths, cap=9)
        second = _plan(list(paths), cap=9)
        assert first["coverage"]["dropped_cells"] == second["coverage"]["dropped_cells"]
        assert _cells(first) == _cells(second)

    def test_equal_files_fall_back_to_listed_order(self, tmp_path: Path):
        paths = [_clean(tmp_path / f"same_{i}.py", 40) for i in range(6)]
        names = [Path(p).name for p in paths]
        kept = {name for name, _ in _cells(_plan(paths, cap=3))}
        assert kept == set(names[:3])

    def test_select_review_cells_returns_kept_and_dropped_in_cell_order(self, tmp_path: Path):
        dim = REVIEW_DIMENSIONS[1]
        prof = estimate_review_profile(_clean(tmp_path / "a.py", 40))
        cells = [("a.py", dim, prof), ("b.py", dim, prof), ("c.py", dim, prof)]
        kept, dropped = select_review_cells(cells, 2, set())
        assert kept == [0, 1] and dropped == [2]


class TestFastReviewCap:
    def test_fast_review_truncation_uses_the_same_ranking(self, tmp_path: Path):
        paths = [
            _clean(tmp_path / "tiny_0.py", 10),
            _clean(tmp_path / "tiny_1.py", 10),
            _clean(tmp_path / "huge.py", 800),
            _risky(tmp_path / "exploit.py", 40, concrete=True),
            _clean(tmp_path / "tiny_2.py", 10),
        ]
        plan = _plan(paths, cap=3, prefix="FAST_REVIEW:")  # one slot is the synthesis agent
        reviewed = [Path(s["target_file"]).name for s in plan["subtasks"] if s.get("target_file")]
        assert reviewed == ["huge.py", "exploit.py"]  # kept files stay in listed order
        assert plan["coverage"]["dropped_cells"] == [
            f"{paths[0]}:all", f"{paths[1]}:all", f"{paths[4]}:all"
        ]

    def test_fast_review_effort_follows_file_size_and_risk(self, tmp_path: Path):
        paths = [_clean(tmp_path / "small.py", 20), _risky(tmp_path / "exploit.py", 40, concrete=True)]
        plan = _plan(paths, cap=10, prefix="FAST_REVIEW:")
        by_name = {Path(s["target_file"]).name: s["reasoning_effort"] for s in plan["subtasks"] if s.get("target_file")}
        assert by_name == {"small.py": "medium", "exploit.py": "high"}


class TestReviewEffort:
    def test_security_on_a_large_risky_file_is_high(self, tmp_path: Path):
        plan = _plan([_risky(tmp_path / "exploit.py", 60, concrete=True)], cap=0)
        sec = next(s for s in plan["subtasks"] if s.get("review_dimension") == "security")
        assert sec["reasoning_effort"] == "high"

    def test_a_types_cell_on_a_small_file_is_medium(self, tmp_path: Path):
        f = _clean(tmp_path / "small.py", 20)
        plan = build_review_subtasks([(f, "")], f"REVIEW: [dims=types] {f}", config=_PY)
        cell = next(s for s in plan["subtasks"] if s.get("review_dimension") == "types")
        assert cell["reasoning_effort"] == "medium"

    def test_effort_never_exceeds_high_and_bumps_one_level(self, tmp_path: Path):
        small = estimate_review_profile(_clean(tmp_path / "s.py", 20))
        risky = estimate_review_profile(_risky(tmp_path / "r.py", 40, concrete=True))
        by_key = {d.key: d for d in REVIEW_DIMENSIONS}
        assert _review_effort(by_key["security"], "high", risky) == "high"
        assert _review_effort(by_key["security"], "medium", small) == "medium"
        assert _review_effort(by_key["security"], "medium", risky) == "high"
        assert _review_effort(by_key["types"], "low", small) == "medium"
        assert _review_effort(by_key["types"], "high", risky) == "high"


class TestFindingsFormat:
    def test_every_dimension_report_spells_the_shared_format(self):
        for dim in REVIEW_DIMENSIONS:
            assert findings_line_format(dim.key) in dim.report, dim.key
            assert "Output nothing" not in dim.report

    def test_the_example_line_parses(self):
        for dim in [*(d.key for d in REVIEW_DIMENSIONS), "all", ""]:
            found = parse_findings_text(findings_line_example(dim))
            assert len(found) == 1, dim
            assert found[0].severity == "high" and found[0].line == 42

    def test_format_constant_matches_the_parsers_rendering(self):
        found = parse_findings_text(findings_line_example("security"))[0]
        rendered = found.format_line()
        head, _, _ = FINDINGS_LINE_FORMAT.partition("<dimension>")
        assert rendered.startswith(head.replace("SEVERITY", "HIGH"))

    def test_protocol_block_states_the_format_literally(self):
        from shared.host_spawn import _findings_protocol_block

        block = _findings_protocol_block("run-x", "7", "security")
        assert "given above" not in block
        assert findings_line_format("security") in block
        example = next(line for line in block.splitlines() if line.startswith("⚠️ [HIGH]"))
        assert parse_findings_text(example)
        assert "dim=security total=" in block


def _run_review(tmp_path: Path, *, effective_agents: int, task_prefix: str, paths: list[str]):
    db_path = tmp_path / "cap.db"
    cfg = TGsConfig(db_path=db_path)
    db = Database(db_path=db_path)
    db._init_schema(db._get_connection())
    from shared.planner import CLIBackend, Planner

    class _NoBackend(CLIBackend):
        def call(self, prompt, model=None, timeout=120):  # pragma: no cover
            raise AssertionError("review heuristic must not call the LLM backend")

    out = mcp_server._execute_swarm_host_native_response(
        config=cfg,
        db=db,
        planner=Planner(cfg, _NoBackend()),
        router=None,
        swarm_id="swarm-cap",
        task_text=f"{task_prefix} " + " ".join(paths),
        caller="claude-code",
        request_meta={
            "topology": "dag",
            "workspace_root": str(tmp_path),
            "effective_agents": effective_agents,
        },
        estimated_cost=0.0,
    )
    db.close()
    return out["result"]


class TestLoudDrops:
    def test_coverage_warning_lists_every_dropped_cell(self, tmp_path: Path):
        paths = _observed_scenario(tmp_path)
        result = _run_review(tmp_path, effective_agents=6, task_prefix="REVIEW:", paths=paths)
        dropped = [d["cell"] for d in result["plan_summary"]["contract"]["dropped"]]
        assert len(dropped) > 8, "the old warning truncated at 8"
        for cell in dropped:
            assert cell in result["coverage_warning"]
        assert "..." not in result["coverage_warning"]

    def test_heavy_drops_require_confirmation(self, tmp_path: Path):
        paths = _observed_scenario(tmp_path)
        result = _run_review(tmp_path, effective_agents=6, task_prefix="REVIEW:", paths=paths)
        assert result["requires_confirmation"] is True
        assert "max_agents" in result["confirmation_reason"]

    def test_a_full_review_does_not_require_confirmation(self, tmp_path: Path):
        paths = [_clean(tmp_path / "only.py", 20)]
        result = _run_review(tmp_path, effective_agents=25, task_prefix="REVIEW:", paths=paths)
        assert "requires_confirmation" not in result
        assert "coverage_warning" not in result

    def test_confirmation_thresholds(self):
        confirm = mcp_server._review_drop_confirmation
        expected = {"dimensions_expected": {"f.py": ["logic"] * 10}}
        not_security = [f"f{i}.py:logic" for i in range(3)]
        assert confirm(not_security, 11, 8, expected) == ""  # 30 % exactly: not above it
        assert "40%" in confirm(not_security + ["f9.py:edge"], 11, 7, expected)
        one_security = ["f0.py:security"]
        assert "security" in confirm(one_security, 11, 10, expected)


class TestDefinitionFallback:
    def _paths(self, tmp_path: Path) -> list[str]:
        return [_risky(tmp_path / "exploit.py", 60, concrete=True)]

    def _security_agent(self, result: dict) -> dict:
        agents = [a for w in result["host_spawn_waves"] for a in w["agents"]]
        return next(a for a in agents if a.get("base_subagent_type") == "threnody-review-security"
                    or a.get("subagent_type") == "threnody-review-security"
                    or "Security review" in a["prompt"])

    def test_missing_definition_keeps_the_stable_block_inline(self, tmp_path: Path):
        result = _run_review(tmp_path, effective_agents=10, task_prefix="REVIEW: [dims=security]",
                             paths=self._paths(tmp_path))
        agent = self._security_agent(result)
        assert agent["effort_source"] == "unknown_base"
        assert "Check for injection" in agent["prompt"]
        assert findings_line_format("security") in agent["prompt"]
        assert agent["requested_effort"] == "high"

    def test_installed_definition_keeps_the_prompt_short(self, tmp_path: Path):
        from shared import host_spawn

        agents_dir = host_spawn.claude_agents_dir()
        agents_dir.mkdir(parents=True, exist_ok=True)
        for dim in REVIEW_DIMENSIONS:
            (agents_dir / f"{dim.subagent_type}.md").write_text(
                f"---\nname: {dim.subagent_type}\n---\n{dim.stable_block}\n", encoding="utf-8"
            )
        result = _run_review(tmp_path, effective_agents=10, task_prefix="REVIEW: [dims=security]",
                             paths=self._paths(tmp_path))
        agent = self._security_agent(result)
        assert agent["subagent_type"] == "threnody-review-security"
        assert "Check for injection" not in agent["prompt"]
        assert json.dumps(agent)  # still a plain manifest entry
