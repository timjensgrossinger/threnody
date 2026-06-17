#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.heuristic_plan import (
    assess_task_complexity,
    build_heuristic_plan_payload,
    extract_task_file_entries,
)


CALCULATOR_TASK = (
    "Build a calculator app: (1) models.py with Operation dataclass, "
    "(2) ops.py with add/sub/mul/div, (3) main.py CLI entrypoint"
)


def test_extract_task_file_entries_numbered_calculator_files() -> None:
    entries = extract_task_file_entries(CALCULATOR_TASK)
    paths = [path for path, _ in entries]
    assert paths == ["models.py", "ops.py", "main.py"]
    assert entries[0][1].startswith("Create models.py:")
    assert "Operation dataclass" in entries[0][1]


def test_build_heuristic_plan_payload_calculator_three_file_case() -> None:
    payload = build_heuristic_plan_payload(CALCULATOR_TASK, default_tier="medium")
    subtasks = payload["subtasks"]
    assert len(subtasks) == 3
    assert [st["target_file"] for st in subtasks] == ["models.py", "ops.py", "main.py"]
    assert payload["strategy"] == "dag"
    assert payload["topology"] == "dag"


def test_build_heuristic_plan_payload_main_py_depends_on_foundation_files() -> None:
    payload = build_heuristic_plan_payload(CALCULATOR_TASK, default_tier="medium")
    by_file = {st["target_file"]: st for st in payload["subtasks"]}
    assert by_file["main.py"]["depends_on"] == [1, 2]
    assert by_file["models.py"]["depends_on"] == []
    assert by_file["ops.py"]["depends_on"] == []


def test_build_heuristic_plan_single_file_uses_low_tier() -> None:
    payload = build_heuristic_plan_payload(
        "Create greet.py in sandbox/demo-v4",
        default_tier="medium",
    )
    assert len(payload["subtasks"]) == 1
    assert payload["subtasks"][0]["tier"] == "low"


def test_extract_task_file_entries_expands_numbered_fanout() -> None:
    task = "Create 4 greet.py numbered in sandbox/swarm-demo-v5 that prints Hello, world!"
    entries = extract_task_file_entries(task)
    paths = [path for path, _ in entries]
    assert paths == [
        "sandbox/swarm-demo-v5/greet1.py",
        "sandbox/swarm-demo-v5/greet2.py",
        "sandbox/swarm-demo-v5/greet3.py",
        "sandbox/swarm-demo-v5/greet4.py",
    ]


def test_build_heuristic_plan_numbered_fanout_parallel_wave() -> None:
    task = "Create 4 greet.py numbered in sandbox/swarm-demo-v5 that prints Hello, world!"
    payload = build_heuristic_plan_payload(task, default_tier="medium")
    assert len(payload["subtasks"]) == 4
    assert payload["topology"] == "linear"
    assert payload["strategy"] == "parallel"


CLAUDE_NEWS_TASK = (
    "Build a small web app in sandbox/claude-news-app that checks for news about Claude. "
    "Python backend with HTML, CSS, and JavaScript frontend."
)


def test_webapp_intent_without_explicit_paths_fans_out() -> None:
    payload = build_heuristic_plan_payload(CLAUDE_NEWS_TASK, default_tier="medium")
    subtasks = payload["subtasks"]
    assert len(subtasks) == 4
    paths = [st["target_file"] for st in subtasks]
    assert paths == [
        "sandbox/claude-news-app/app.py",
        "sandbox/claude-news-app/templates/index.html",
        "sandbox/claude-news-app/static/css/style.css",
        "sandbox/claude-news-app/static/js/app.js",
    ]


def test_fullstack_intent_builds_contract_first_dag() -> None:
    task = (
        "Build a fullstack todo app in sandbox/todo under openapi contract "
        "with parallel frontend and backend"
    )
    payload = build_heuristic_plan_payload(task, default_tier="medium")
    subtasks = payload["subtasks"]
    assert len(subtasks) == 4
    assert payload["topology"] == "dag"
    by_file = {st["target_file"]: st for st in subtasks}
    assert by_file["sandbox/todo/openapi.yaml"]["depends_on"] == []
    assert by_file["sandbox/todo/app.py"]["depends_on"] == [1]
    assert by_file["sandbox/todo/templates/index.html"]["depends_on"] == [1]
    assert by_file["sandbox/todo/tests/integration.py"]["depends_on"] == [2, 3]


def test_intent_templates_disabled_keeps_single_subtask() -> None:
    payload = build_heuristic_plan_payload(
        CLAUDE_NEWS_TASK,
        default_tier="medium",
        intent_templates=False,
    )
    assert len(payload["subtasks"]) == 1


# --- coupled-group + description + complexity fixes -------------------------

COUPLED_TASK = (
    "Build a plugin with a shared event schema across "
    "lua/app/init.lua (setup and config), "
    "lua/app/panel.lua (render the collapsible panel), "
    "lua/app/sources/hooks.lua (RPC receiver and installer), and "
    "lua/app/sources/jsonl.lua (session watcher and parser)."
)


def test_description_hint_not_truncated_at_first_comma() -> None:
    # Full paths must inherit the basename-keyed clause, not a punctuation-truncated
    # fragment like "init.lua (setup".
    task = "Update lua/app/init.lua (setup, config, and teardown logic) carefully."
    entries = extract_task_file_entries(task)
    assert entries, "expected init.lua to be extracted"
    _path, hint = entries[0]
    assert "config" in hint and "teardown" in hint
    assert hint != "lua/app/init.lua (setup"


def test_coupled_group_single_strategy_collapses_to_one_subtask() -> None:
    payload = build_heuristic_plan_payload(
        COUPLED_TASK, default_tier="medium", coupled_strategy="single"
    )
    subtasks = payload["subtasks"]
    assert len(subtasks) == 1
    # Coupled source group escalates above the flat "low".
    assert subtasks[0]["tier"] in {"medium", "high"}
    assert len(subtasks[0].get("target_files", [])) == 4


def test_coupled_group_contract_strategy_builds_dag() -> None:
    payload = build_heuristic_plan_payload(
        COUPLED_TASK, default_tier="medium", coupled_strategy="contract"
    )
    subtasks = payload["subtasks"]
    assert len(subtasks) >= 2
    assert payload["strategy"] == "dag"
    assert subtasks[0]["depends_on"] == []
    assert all(st["depends_on"] == [1] for st in subtasks[1:])


def test_init_lua_recognized_as_integration_stem() -> None:
    # init.* is now an integration file; with a foundation file present it gains deps.
    task = "Wire app/init.lua and app/helper.lua together via a shared module interface."
    payload = build_heuristic_plan_payload(task, default_tier="medium", coupled_strategy="contract")
    # Coupled (shared dir app/ + 'shared'/'module'/'interface' keyword) → contract DAG.
    assert payload["strategy"] == "dag"


def test_assess_task_complexity_flags_coupled_and_spares_simple() -> None:
    assert assess_task_complexity(COUPLED_TASK)["complex"] is True
    assert assess_task_complexity("Create greet.py in sandbox/demo")["complex"] is False


def test_extract_rejects_absolute_and_home_paths() -> None:
    # Absolute home-dir path + plan-file path in prose must NOT become targets.
    task = (
        "Refactor the coordinator described in /Users/someuser/secret.py "
        "and the plan at /Users/someuser/.claude/plans/foo.md"
    )
    entries = extract_task_file_entries(task, intent_templates=False)
    paths = [path for path, _ in entries]
    assert not any(p.startswith("/") for p in paths)


def test_all_absolute_paths_collapse_to_single_subtask() -> None:
    # When the only "files" are out-of-root absolutes, fall back to one agent.
    task = "Edit /Users/someuser/a.py and /Users/someuser/b.py together."
    payload = build_heuristic_plan_payload(task, default_tier="medium", intent_templates=False)
    assert len(payload["subtasks"]) == 1
    assert payload["subtasks"][0]["description"] == task.strip()
    assert payload["topology"] == "linear"


def test_review_dims_token_not_parsed_as_file(tmp_path: Path) -> None:
    """[dims=...] intent token must not be extracted as a review target, and the
    fanout must run only the requested dimension (+ synthesis)."""
    f = tmp_path / "svc.py"
    f.write_text("\n".join(f"line {i}" for i in range(300)), encoding="utf-8")
    payload = build_heuristic_plan_payload(
        f"REVIEW: [dims=performance] {f}", max_agents=8
    )
    review = [s for s in payload["subtasks"] if not s.get("depends_on")]
    subagent_types = {s.get("subagent_type") for s in review}
    target_files = {str(s.get("target_file")) for s in review}
    # Only the performance dimension ran (no logic/edge/types collapse)
    assert subagent_types == {"review-performance"}
    # The bracket token never became a file target
    assert all("[dims" not in p and "=performance]" not in p for p in target_files)
    assert all(p.endswith("svc.py") for p in target_files)
