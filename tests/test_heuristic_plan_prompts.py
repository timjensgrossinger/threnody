"""Heuristic write-path planning: coherent prompts, resolved paths, evidence floors.

Regression for the plan_task prompt-mangling defects: a multi-item plan used to
decompose into per-file prompts cut out of the task text ("scoped verify signals
in and ;"), with bare names pointing at the repo root and a 12k-line module on the
cheapest tier.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import mcp_server
from shared.config import TGsConfig
from shared.heuristic_plan import (
    HEURISTIC_PLAN_VERSION,
    _attach_directory,
    _coupled_groups,
    _focus_clause,
    _write_role,
    build_heuristic_plan_payload,
)
from shared.host_spawn import is_mangled_prompt, sanitize_plan_for_host

REPRO_TASK = (
    "Implement plan /tmp/x.md: 1. scoped verify signals in shared/verify.py and "
    "shared/config.py, wire instructions.py. 2. outcome proxy source in "
    "shared/model_quality.py and shared/db.py. 3. touched-file capture in "
    "mcp_server.py, check the reviewer gate. 4. host detection in "
    "shared/discovery.py. 5. tests in tests/: test_verify_gate.py, test_model_quality.py"
)

REPO_FILES = [
    "shared/verify.py",
    "shared/config.py",
    "shared/instructions.py",
    "shared/model_quality.py",
    "shared/db.py",
    "shared/discovery.py",
    "tests/test_verify_gate.py",
    "tests/test_model_quality.py",
]


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    for rel in REPO_FILES:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VALUE = 1\n")
    # Well over 1500 non-blank lines.
    (tmp_path / "mcp_server.py").write_text(
        "".join(f"def handler_{i}():\n    return {i}\n" for i in range(900))
    )
    return tmp_path


def _plan(task: str, root: Path | None, **kwargs):
    kwargs.setdefault("default_tier", "low")
    return build_heuristic_plan_payload(
        task, workspace_root=str(root) if root else None, **kwargs
    )


def _writers(payload: dict) -> list[dict]:
    return [st for st in payload["subtasks"] if not st.get("read_only")]


def _targets(st: dict) -> list[str]:
    return list(st.get("target_files") or [st["target_file"]])


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def test_every_prompt_carries_the_full_task_verbatim(workspace: Path) -> None:
    payload = _plan(REPRO_TASK, workspace)
    writers = _writers(payload)
    assert writers
    for st in writers:
        assert st["description"].startswith(REPRO_TASK)
        assert "Your focus" in st["description"]
        assert "You own exactly these files:" in st["description"]
        assert not is_mangled_prompt(st["description"])


def test_sibling_paths_are_never_blanked_out_of_the_prompt(workspace: Path) -> None:
    payload = _plan(REPRO_TASK, workspace)
    for st in _writers(payload):
        assert "in and" not in st["description"]
        assert " ;" not in st["description"]


def test_focus_clause_is_the_sentence_that_names_the_file() -> None:
    clause = _focus_clause(REPRO_TASK, "shared/discovery.py")
    assert clause == "host detection in shared/discovery.py"
    # a basename-only mention still anchors a resolved path
    assert "wire instructions.py" in _focus_clause(REPRO_TASK, "shared/instructions.py")


def test_repro_targets_resolve_into_the_repo(workspace: Path) -> None:
    payload = _plan(REPRO_TASK, workspace)
    owned = {p for st in _writers(payload) for p in _targets(st)}
    assert "shared/instructions.py" in owned
    assert "instructions.py" not in owned
    assert "tests/test_verify_gate.py" in owned
    assert "tests/test_model_quality.py" in owned
    assert "test_verify_gate.py" not in owned
    assert "mcp_server.py" in owned
    assert payload["coverage"]["deferred"] == []


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def test_trailing_directory_attaches_to_following_bare_names() -> None:
    task = "5. tests in tests/: test_a.py, test_b.py"
    import re

    from shared.heuristic_plan import _BARE_FILENAME

    names = [_attach_directory(task, m) for m in _BARE_FILENAME.finditer(task)]
    assert names == ["tests/test_a.py", "tests/test_b.py"]


def test_directory_does_not_attach_across_unrelated_prose() -> None:
    task = "edit docs/ for the release, then fix helper.py"
    from shared.heuristic_plan import _BARE_FILENAME

    names = [_attach_directory(task, m) for m in _BARE_FILENAME.finditer(task)]
    assert names == ["helper.py"]


def test_bare_name_prefers_root_file_then_unique_repo_match(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "only_here.py").write_text("x = 1\n")
    (tmp_path / "root_one.py").write_text("x = 1\n")
    payload = _plan("update only_here.py and root_one.py and brand_new.py", tmp_path)
    owned = {p for st in _writers(payload) for p in _targets(st)}
    assert owned == {"pkg/only_here.py", "root_one.py", "brand_new.py"}


def test_ambiguous_bare_name_is_reported_and_owns_no_target(tmp_path: Path) -> None:
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "util.py").write_text("x = 1\n")
    (tmp_path / "main.py").write_text("x = 1\n")
    payload = _plan("rework util.py and main.py", tmp_path)
    owned = {p for st in _writers(payload) for p in _targets(st)}
    assert owned == {"main.py"}
    ambiguous = payload["coverage"]["ambiguous_files"]
    assert ambiguous == [{"name": "util.py", "candidates": ["a/util.py", "b/util.py"]}]
    assert "util.py" in payload["analysis"]


def test_resolution_is_skipped_without_a_workspace_root() -> None:
    payload = _plan("update instructions.py", None)
    assert _targets(_writers(payload)[0]) == ["instructions.py"]


# ---------------------------------------------------------------------------
# Fragment guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "scoped verify signals in and ;",
        "scoped verify signals in and , wire instructions.py",
        "tests in tests/ test_verify_gate.py",
        "wire it in",
        "Update foo",
    ],
)
def test_mangled_prompts_are_detected(text: str) -> None:
    assert is_mangled_prompt(text)


def test_coherent_prompts_are_not_flagged() -> None:
    assert not is_mangled_prompt(REPRO_TASK + "\n\nYour focus: shared/db.py")
    # the ownership sentence alone must not rescue a fragment
    assert is_mangled_prompt(
        "scoped verify signals in and ; You own exactly these files: a.py. "
        "Do not create or edit any other file."
    )


def test_sanitizer_collapses_a_mangled_plan_to_one_full_task_agent() -> None:
    mangled = (
        "scoped verify signals in and ; You own exactly these files: shared/a.py. "
        "Do not create or edit any other file."
    )
    plan = {
        "subtasks": [
            {"id": 1, "description": mangled, "tier": "medium", "target_file": "shared/a.py",
             "target_files": ["shared/a.py"], "depends_on": []},
            {"id": 2, "description": REPRO_TASK + " You own exactly these files: shared/b.py.",
             "tier": "high", "target_file": "shared/b.py", "depends_on": []},
        ],
        "waves": [[1, 2]],
    }
    with tempfile.TemporaryDirectory() as root:
        report = sanitize_plan_for_host(plan, workspace_root=root, task=REPRO_TASK)
    assert report["collapsed_to_single"] is True
    assert any("mangled" in reason for reason in report["reasons"])
    assert len(plan["subtasks"]) == 1
    assert plan["subtasks"][0]["description"] == REPRO_TASK
    assert plan["subtasks"][0]["tier"] == "high"
    assert plan["sanitization"] is report


def test_sanitizer_leaves_review_cells_alone() -> None:
    plan = {
        "subtasks": [
            {"id": 1, "description": "Review x.py for security", "tier": "low",
             "target_file": "x.py", "read_only": True, "review_dimension": "security",
             "depends_on": []},
        ],
        "waves": [[1]],
    }
    report = sanitize_plan_for_host(plan, workspace_root=None, task="REVIEW: x.py")
    assert report["collapsed_to_single"] is False
    assert len(plan["subtasks"]) == 1


# ---------------------------------------------------------------------------
# Role and tier
# ---------------------------------------------------------------------------

def test_write_role_is_judged_on_the_whole_task() -> None:
    task = "check the reviewer gate and verify signals in a.py"
    assert _write_role(task, ["a.py"]) == "Implementer"
    assert _write_role(task, ["tests/test_a.py"]) == "Tester"
    assert _write_role("fix the crash in a.py", ["a.py"]) == "Debugger"
    assert _write_role("review a.py", ["a.py"]) == "Implementer"


def test_repro_roles(workspace: Path) -> None:
    payload = _plan(REPRO_TASK, workspace)
    by_target = {tuple(_targets(st)): st for st in _writers(payload)}
    for targets, st in by_target.items():
        expected = "Tester" if all(t.startswith("tests/") for t in targets) else "Implementer"
        assert st["role"] == expected, targets
        assert st["role"] != "Reviewer"


def test_large_file_is_floored_to_medium_even_when_default_is_low(workspace: Path) -> None:
    payload = _plan(REPRO_TASK, workspace, default_tier="low")
    big = next(st for st in _writers(payload) if _targets(st) == ["mcp_server.py"])
    assert big["tier"] in {"medium", "high"}
    assert big["role"] == "Implementer"


def test_small_file_keeps_the_cheap_tier(tmp_path: Path) -> None:
    (tmp_path / "small.py").write_text("x = 1\n")
    payload = _plan("add a docstring to small.py", tmp_path)
    assert _writers(payload)[0]["tier"] == "low"


def test_floor_never_lowers_a_higher_tier(workspace: Path, monkeypatch) -> None:
    # Keep the hybrid diagnose->implement discount out of it: this asserts the floor.
    monkeypatch.setattr("shared.heuristic_plan._load_hybrid_config", lambda: None)
    payload = _plan("rewrite mcp_server.py", workspace, default_tier="high")
    assert _writers(payload)[0]["tier"] == "high"


# ---------------------------------------------------------------------------
# Coupling and complexity
# ---------------------------------------------------------------------------

def test_coupled_groups_are_per_directory(workspace: Path) -> None:
    entries = [(p, "") for p in REPO_FILES + ["mcp_server.py"]]
    groups = _coupled_groups(entries, "")
    assert len(groups) == 2
    dirs = {tuple(sorted({entries[i - 1][0].split("/")[0] for i in g})) for g in groups}
    assert dirs == {("shared",), ("tests",)}


def test_each_directory_gets_its_own_coupled_agent(workspace: Path) -> None:
    payload = _plan(REPRO_TASK, workspace)
    owners = [tuple(_targets(st)) for st in _writers(payload)]
    shared = next(o for o in owners if o[0].startswith("shared/"))
    tests = next(o for o in owners if o[0].startswith("tests/"))
    assert all(p.startswith("shared/") for p in shared)
    assert all(p.startswith("tests/") for p in tests)
    assert ("mcp_server.py",) in owners


def test_complex_wide_task_becomes_one_high_tier_agent(workspace: Path) -> None:
    payload = build_heuristic_plan_payload(
        REPRO_TASK, default_tier="low", workspace_root=str(workspace),
        single_agent_when_complex=True,
    )
    assert len(payload["subtasks"]) == 1
    st = payload["subtasks"][0]
    assert st["tier"] == "high"
    assert st["description"].startswith(REPRO_TASK)
    assert set(_targets(st)) == set(REPO_FILES) | {"mcp_server.py"}
    assert payload["coverage"]["deferred"] == []


def test_repro_fans_out_by_default(workspace: Path) -> None:
    payload = build_heuristic_plan_payload(
        REPRO_TASK, default_tier="low", workspace_root=str(workspace)
    )
    assert len(_writers(payload)) == 3


def test_config_default_is_off() -> None:
    assert TGsConfig.defaults().heuristic_single_agent_when_complex is False


def test_contract_strategy_is_not_collapsed(workspace: Path) -> None:
    payload = build_heuristic_plan_payload(
        REPRO_TASK, default_tier="low", workspace_root=str(workspace),
        coupled_strategy="contract", single_agent_when_complex=True,
    )
    assert len(payload["subtasks"]) > 1


def test_few_files_are_not_collapsed(tmp_path: Path) -> None:
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("x = 1\n")
    payload = build_heuristic_plan_payload(
        "refactor the schema in a.py and b.py", default_tier="low",
        workspace_root=str(tmp_path),
    )
    assert all(st["tier"] != "high" or st.get("target_files") != ["a.py", "b.py"]
               for st in payload["subtasks"])


# ---------------------------------------------------------------------------
# plan_task plumbing and cache
# ---------------------------------------------------------------------------

def test_stale_cached_heuristic_plans_are_not_served() -> None:
    stale = json.dumps({"planner_host_execution_mode": "host_native", "subtasks": []})
    fresh = json.dumps({
        "planner_host_execution_mode": "host_native",
        "heuristic_plan_version": HEURISTIC_PLAN_VERSION,
        "subtasks": [],
    })
    other = json.dumps({"analysis": "cached", "subtasks": []})
    assert mcp_server._plan_cache_entry_is_stale(stale) is True
    assert mcp_server._plan_cache_entry_is_stale(fresh) is False
    assert mcp_server._plan_cache_entry_is_stale(other) is False
    assert mcp_server._plan_cache_entry_is_stale("not json") is False


class _Captured(Exception):
    pass


def test_handle_plan_task_passes_workspace_root_to_the_planner(monkeypatch, tmp_path) -> None:
    cfg = TGsConfig(db_path=tmp_path / "plan.db")

    class _StaleCacheDb:
        """Serves a plan an older planner cached, which must not mask the call."""

        def cache_get(self, task):
            return json.dumps(
                {"planner_host_execution_mode": "host_native", "subtasks": []}
            ), "planner"

    db = _StaleCacheDb()
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: None)

    seen: dict = {}

    def fake(*args, **kwargs):
        seen.update(kwargs)
        raise _Captured

    monkeypatch.setattr(mcp_server, "_planner_plan_for_caller", fake)
    with pytest.raises(_Captured):
        mcp_server.handle_plan_task({"task": "plan me", "cwd": str(tmp_path)})
    assert seen["workspace_root"] == str(tmp_path)
