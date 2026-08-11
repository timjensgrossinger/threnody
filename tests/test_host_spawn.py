"""Tests for shared.host_spawn meta-harness v2 helpers."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shared.config import TGsConfig
from shared.host_spawn import (
    HOST_SPAWN_ERROR,
    HOST_EXECUTION_CONTRACT,
    build_host_native_required_response,
    build_host_spawn,
    build_host_spawn_waves,
    enrich_host_spawn_waves,
    effective_swarm_host_execution_mode,
    host_tool_for_caller,
    sanitize_plan_for_host,
    would_self_delegate,
)


def test_host_tool_for_caller() -> None:
    assert host_tool_for_caller("claude-code") == "Agent"
    assert host_tool_for_caller("Claude") == "Agent"
    assert host_tool_for_caller("cursor") == "Task"
    assert host_tool_for_caller("github-copilot") == "Task"


def test_build_host_spawn_waves_from_plan() -> None:
    cfg = TGsConfig.defaults()
    plan = {
        "subtasks": [
            {"id": 1, "description": "edit auth", "tier": "medium", "model": "sonnet"},
            {"id": 2, "description": "add tests", "tier": "low", "target_file": "tests/test_auth.py"},
        ],
        "waves": [[1], [2]],
    }
    waves = build_host_spawn_waves(plan, config=cfg, caller="claude-code")
    assert len(waves) == 2
    assert waves[0]["agents"][0]["tool"] == "Agent"
    assert waves[0]["agents"][0]["subagent_type"] == "threnody-medium"
    assert waves[1]["agents"][0]["method"] == "host_task"
    assert waves[1]["agents"][0]["spawn_required"] is True
    assert waves[1]["execution_contract"] == "spawn_subagents"
    assert waves[1]["parallel_start_required"] is True
    assert "spawn_batch" not in waves[1]  # duplicated agents; half the payload
    assert waves[1]["agents"][0]["target_files"] == ["tests/test_auth.py"]


def test_build_host_spawn_waves_exposes_batch_metadata_for_same_wave_agents() -> None:
    cfg = TGsConfig.defaults()
    plan = {
        "subtasks": [
            {"id": "a", "description": "edit auth", "tier": "medium"},
            {
                "id": "b",
                "description": "add tests",
                "tier": "low",
                "target_file": "tests/test_auth.py",
            },
        ],
        "waves": [["a", "b"]],
    }

    waves = build_host_spawn_waves(plan, config=cfg, caller="claude-code")

    assert len(waves) == 1
    wave = waves[0]
    assert wave["parallel"] is True
    assert wave["execution_contract"] == HOST_EXECUTION_CONTRACT
    assert wave["parallel_start_required"] is True
    assert "spawn_batch" not in wave
    assert [agent["id"] for agent in wave["agents"]] == ["a", "b"]
    for agent in wave["agents"]:
        assert agent["method"] == "host_task"
        assert agent["spawn_required"] is True


def test_would_self_delegate_blocks_same_host_without_provider_id() -> None:
    provider = SimpleNamespace(name="claude-code", display_name="Claude Code")
    registry = MagicMock()
    registry._ordered_execution_candidates.return_value = ([provider], [])
    registry._caller_matches_provider.return_value = True
    assert would_self_delegate(registry, caller="claude-code", tier="medium") is True


def test_would_self_delegate_allows_cross_backend_provider_id() -> None:
    copilot = SimpleNamespace(name="github-copilot", display_name="GitHub Copilot")
    registry = MagicMock()
    registry._ordered_execution_candidates.return_value = ([copilot], [])
    registry._caller_identifiers.return_value = {"claude-code", "claude"}
    registry._provider_identifiers.return_value = {"github-copilot", "copilot"}
    assert (
        would_self_delegate(
            registry,
            caller="claude-code",
            tier="low",
            provider_id="github-copilot",
        )
        is False
    )


def test_build_host_native_required_response_shape() -> None:
    cfg = TGsConfig.defaults()
    payload = build_host_native_required_response(
        config=cfg,
        caller="cursor",
        tier="medium",
        prompt="refactor module",
        delegation_targets=["opencode"],
    )
    assert payload["error"] == HOST_SPAWN_ERROR
    assert payload["host_spawn"]["tool"] == "Task"
    assert payload["delegation_targets"] == ["opencode"]


def test_effective_swarm_host_execution_mode_defaults_host_native_for_hosts() -> None:
    cfg = TGsConfig.defaults()
    assert effective_swarm_host_execution_mode(cfg, "claude-code") == "host_native"
    assert effective_swarm_host_execution_mode(cfg, "external-caller") == "delegate"


def test_effective_swarm_host_execution_mode_per_caller_override() -> None:
    cfg = TGsConfig.defaults()
    cfg.swarm_host_execution_mode = "host_native"
    cfg.swarm_host_execution_mode_by_caller = {"claude-code": "delegate"}
    assert effective_swarm_host_execution_mode(cfg, "claude-code") == "delegate"


def test_enrich_host_spawn_waves_forces_host_task_contract() -> None:
    waves = enrich_host_spawn_waves(
        [
            {
                "wave": 1,
                "parallel": True,
                "agents": [
                    {"id": "1", "method": "direct_edit", "tier": "low"},
                    {"id": "2", "method": "direct_edit", "tier": "low"},
                ],
            }
        ]
    )
    assert waves[0]["execution_contract"] == HOST_EXECUTION_CONTRACT
    assert waves[0]["parallel_start_required"] is True
    assert "spawn_batch" not in waves[0]
    assert [agent["id"] for agent in waves[0]["agents"]] == ["1", "2"]
    for agent in waves[0]["agents"]:
        assert agent["method"] == "host_task"
        assert agent["spawn_required"] is True


# ---- sanitize_plan_for_host: workspace-containment + fragment safety gate ----


def test_sanitize_strips_out_of_root_target(tmp_path) -> None:
    root = str(tmp_path)
    plan = {
        "subtasks": [
            {
                "id": 1,
                "description": "Update the home file as described in the task.",
                "tier": "medium",
                "target_file": "/Users/someuser/secret.py",
            }
        ],
        "waves": [[1]],
    }
    report = sanitize_plan_for_host(plan, workspace_root=root, task="do work")
    # Target escapes root -> stripped, but coherent prompt keeps the subtask.
    assert plan["subtasks"][0].get("target_file") is None
    assert any(d["id"] == 1 for d in report["dropped_targets"])
    assert plan["waves"] == [[1]]


def test_sanitize_drops_fragment_prompt_subtask(tmp_path) -> None:
    plan = {
        "subtasks": [
            {"id": 1, "description": "someuser/", "tier": "low",
             "target_file": "/Users/someuser"},
            {"id": 2, "description": "Implement the parser module fully.",
             "tier": "medium", "target_file": "src/parser.py"},
        ],
        "waves": [[1, 2]],
    }
    sanitize_plan_for_host(plan, workspace_root=str(tmp_path), task="build parser")
    ids = [st["id"] for st in plan["subtasks"]]
    assert ids == [2]
    assert plan["waves"] == [[2]]


def test_sanitize_collapses_to_single_agent_when_all_unsafe(tmp_path) -> None:
    plan = {
        "subtasks": [
            {"id": 1, "description": "someuser/", "tier": "low",
             "target_file": "/Users/someuser"},
            {"id": 2, "description": "plans/", "tier": "low",
             "target_file": "/Users/someuser/.claude/plans/x.md"},
        ],
        "waves": [[1, 2]],
        "topology": "dag",
    }
    task = "Refactor the tightly-coupled coordinator and queen modules together."
    report = sanitize_plan_for_host(plan, workspace_root=str(tmp_path), task=task)
    assert report["collapsed_to_single"] is True
    assert len(plan["subtasks"]) == 1
    assert plan["subtasks"][0]["description"] == task
    assert plan["waves"] == [[1]]
    assert plan["topology"] == "linear"


def test_sanitize_leaves_clean_fanout_untouched(tmp_path) -> None:
    plan = {
        "subtasks": [
            {"id": 1, "description": "Build module a fully.", "tier": "low",
             "target_file": "a.py"},
            {"id": 2, "description": "Build module b fully.", "tier": "low",
             "target_file": "b.py"},
            {"id": 3, "description": "Build module c fully.", "tier": "low",
             "target_file": "c.py"},
        ],
        "waves": [[1, 2, 3]],
    }
    report = sanitize_plan_for_host(plan, workspace_root=str(tmp_path), task="build")
    assert report["collapsed_to_single"] is False
    assert not report["dropped_targets"]
    assert not report["dropped_subtasks"]
    assert [st["target_file"] for st in plan["subtasks"]] == ["a.py", "b.py", "c.py"]
    assert plan["waves"] == [[1, 2, 3]]


def test_sanitize_keeps_read_only_external_target(tmp_path) -> None:
    # Read-only review subtasks may legitimately target absolute, out-of-root files.
    plan = {
        "subtasks": [
            {
                "id": 1,
                "description": "Security review of the auth module.",
                "tier": "high",
                "read_only": True,
                "target_file": "/Users/someuser/repo/auth.py",
            }
        ],
        "waves": [[1]],
    }
    report = sanitize_plan_for_host(plan, workspace_root=str(tmp_path), task="review")
    assert plan["subtasks"][0]["target_file"] == "/Users/someuser/repo/auth.py"
    assert not report["dropped_targets"]


def test_sanitize_prunes_dropped_id_from_depends_on(tmp_path) -> None:
    plan = {
        "subtasks": [
            {"id": 1, "description": "x/", "tier": "low",
             "target_file": "/etc/passwd"},
            {"id": 2, "description": "Wire the integration layer together.",
             "tier": "medium", "target_file": "main.py", "depends_on": [1]},
        ],
        "waves": [[1], [2]],
    }
    sanitize_plan_for_host(plan, workspace_root=str(tmp_path), task="integrate")
    survivors = {st["id"]: st for st in plan["subtasks"]}
    assert set(survivors) == {2}
    assert survivors[2]["depends_on"] == []
    assert plan["waves"] == [[2]]


def test_sanitize_dedupes_overlapping_target_ownership() -> None:
    """Two subtasks claiming the same file → each file owned once (#2)."""
    plan = {
        "subtasks": [
            {"id": 1, "description": "Own the module", "tier": "medium",
             "target_file": "app/core.py", "target_files": ["app/core.py", "app/util.py"]},
            {"id": 2, "description": "Also edit util", "tier": "low",
             "target_file": "app/util.py"},
            {"id": 3, "description": "Edit view", "tier": "low",
             "target_file": "app/view.py"},
        ],
        "waves": [[1, 2, 3]],
    }
    report = sanitize_plan_for_host(plan, workspace_root=None, task="build app")
    owners: dict[str, list] = {}
    for st in plan["subtasks"]:
        for f in st.get("target_files", [st.get("target_file")]):
            owners.setdefault(f, []).append(st["id"])
    # No file is owned by more than one surviving subtask.
    assert all(len(ids) == 1 for ids in owners.values()), owners
    assert report.get("dedup"), "expected a dedup report entry"


def test_sanitize_keeps_every_read_only_cell_for_one_file() -> None:
    """Regression: ownership dedup silently collapsed review fanout.

    Every (file x dimension) review cell carries the same ``target_file``. The
    disjoint-ownership rule exists to stop two agents *editing* one file, but it
    had no read_only exemption, so a 5-dimension review of one file emitted a
    single security agent and recorded the other four only in
    ``sanitization.dedup`` — a field the coverage contract never read. A
    2-dimension review of 11 files reported 12 planned agents and 3 drops.
    """
    dims = ["security", "logic", "edge", "types", "performance"]
    plan = {
        "subtasks": [
            {
                "id": i,
                "description": f"{d.title()} review of app/core.py.",
                "tier": "medium",
                "target_file": "app/core.py",
                "subagent_type": f"review-{d}",
                "review_dimension": d,
                "read_only": True,
                "depends_on": [],
            }
            for i, d in enumerate(dims, start=1)
        ],
        "waves": [list(range(1, len(dims) + 1))],
    }
    report = sanitize_plan_for_host(plan, workspace_root=None, task="REVIEW: app/core.py")
    assert len(plan["subtasks"]) == len(dims)
    assert [st["review_dimension"] for st in plan["subtasks"]] == dims
    assert not report.get("dedup")


def test_read_only_cell_does_not_claim_ownership_from_a_writer() -> None:
    """A reviewer must not evict the writer that follows it.

    Exempting read_only subtasks from the dedup is only half correct: they must
    also not populate ``claimed``, or a review cell listed ahead of a write
    subtask for the same file would take ownership and the writer would be
    dropped as a duplicate.
    """
    plan = {
        "subtasks": [
            {"id": 1, "description": "Security review of app/core.py.", "tier": "low",
             "target_file": "app/core.py", "subagent_type": "review-security",
             "review_dimension": "security", "read_only": True},
            {"id": 2, "description": "Implement the retry policy in app/core.py.",
             "tier": "medium", "target_file": "app/core.py"},
        ],
        "waves": [[1, 2]],
    }
    report = sanitize_plan_for_host(plan, workspace_root=None, task="review then fix")
    assert {st["id"] for st in plan["subtasks"]} == {1, 2}
    assert not report.get("dedup")


def test_dedup_records_the_review_cell_it_removed() -> None:
    """Removal records must name the cell, not just a subtask id.

    The contract reconciles removals against ``dimensions_expected`` by label.
    An id alone is useless downstream because the subtask it names is gone by
    the time the contract is built.
    """
    plan = {
        "subtasks": [
            {"id": 1, "description": "Own the module", "tier": "medium",
             "target_file": "app/core.py"},
            {"id": 2, "description": "Security review of app/core.py.", "tier": "low",
             "target_file": "app/core.py", "review_dimension": "security"},
        ],
        "waves": [[1, 2]],
    }
    report = sanitize_plan_for_host(plan, workspace_root=None, task="build app")
    dropped = [e for e in report.get("dedup", []) if e.get("dropped")]
    assert dropped and dropped[0]["cell"] == "app/core.py:security"


def test_subtask_target_files_reads_plural_list() -> None:
    """_subtask_target_files honors the plural list, not just the scalar (#2)."""
    from shared.host_spawn import _subtask_target_files
    st = {"target_file": "a.py", "target_files": ["a.py", "b.py", "c.py"]}
    assert _subtask_target_files(st) == ["a.py", "b.py", "c.py"]


# ---------------------------------------------------------------------------
# Prompt economy: capability gating, findings protocol, upstream forwarding
# ---------------------------------------------------------------------------

def _review_plan() -> dict:
    return {
        "subtasks": [
            {
                "id": 1,
                "description": "Security review of shared/db.py",
                "tier": "medium",
                "read_only": True,
                "review_dimension": "security",
                "subagent_type": "review-security",
                "target_file": "shared/db.py",
                "depends_on": [],
            }
        ],
        "waves": [[1]],
    }


def test_named_subagent_type_requires_the_capability() -> None:
    """A shell with no exported definition must fall back to the tier-derived type,
    or its spawn names an agent that does not exist."""
    from shared.host_spawn import named_subagent_types_supported

    config = TGsConfig()
    assert named_subagent_types_supported(config, "claude-code") is True
    assert named_subagent_types_supported(config, "junie") is False
    # An unknown shell must not inherit the capability via the advisory fallback.
    assert named_subagent_types_supported(config, "not-a-real-shell") is False
    assert named_subagent_types_supported(config, None) is False


def test_unknown_shell_spawn_matches_tier_derived_type() -> None:
    config = TGsConfig()
    spec = build_host_spawn(
        config=config,
        caller="not-a-real-shell",
        tier="medium",
        prompt="p",
        subagent_type="review-security",
        read_only=True,
    )
    assert spec.subagent_type == "threnody-medium"


def test_findings_protocol_under_python_synthesis() -> None:
    config = TGsConfig()
    plan = _review_plan()
    plan["synthesis_mode"] = "python"
    waves = build_host_spawn_waves(
        plan, config=config, caller="claude-code", run_id="swarm-fp-test"
    )
    prompt = waves[0]["agents"][0]["prompt"]
    assert "Write your findings to" in prompt
    assert "dim=security" in prompt


def test_findings_protocol_also_applies_under_llm_synthesis() -> None:
    """Both modes, because the categories are a learning input, not a merge detail.

    Gating this on python mode meant parsed per-category findings existed only
    for narrow reviews (python is chosen for <=6 cells / <=2 files), so
    `record_static_recall_score` — the only objective signal a read-only review
    run can produce — was unavailable for exactly the broad reviews where it
    matters. The synthesis agent is not starved: it depends on those cells, so it
    receives their findings paths and reads them.
    """
    config = TGsConfig()
    plan = _review_plan()
    plan["synthesis_mode"] = "llm"
    waves = build_host_spawn_waves(
        plan, config=config, caller="claude-code", run_id="swarm-fp-test"
    )
    assert "Write your findings to" in waves[0]["agents"][0]["prompt"]


def test_review_cell_has_exactly_one_output_file() -> None:
    """A review cell must not be asked for the same content twice.

    Its artifact path IS its findings path, so the merge and the synthesis agent
    read the same file rather than two files in two formats.
    """
    config = TGsConfig()
    plan = _review_plan()
    plan["synthesis_mode"] = "llm"
    # A synthesis agent depending on the cell is what gives the cell an artifact.
    plan["subtasks"].append({
        "id": 2,
        "description": "Synthesize the review findings.",
        "tier": "medium",
        "read_only": True,
        "subagent_type": "",
        "depends_on": [1],
    })
    plan["waves"] = [[1], [2]]
    waves = build_host_spawn_waves(
        plan, config=config, caller="claude-code", run_id="swarm-fp-test"
    )
    cell = waves[0]["agents"][0]
    assert "/findings/" in str(cell["artifact_path"])
    assert "Write your output to" not in cell["prompt"]
    synthesis = waves[-1]["agents"][-1]
    upstream = synthesis.get("upstream") or []
    assert upstream, "synthesis must be handed its upstream findings files"
    assert all("/findings/" in str(u["artifact_path"]) for u in upstream)


def test_findings_protocol_absent_without_run_id() -> None:
    """The run id names the file; without it there is nothing to write to."""
    config = TGsConfig()
    plan = _review_plan()
    plan["synthesis_mode"] = "python"
    waves = build_host_spawn_waves(plan, config=config, caller="claude-code")
    assert "Write your findings to" not in waves[0]["agents"][0]["prompt"]


def _dag_plan() -> dict:
    return {
        "subtasks": [
            {"id": 1, "description": "Diagnose the flow.", "tier": "high",
             "read_only": True, "depends_on": []},
            {"id": 2, "description": "Implement a.py", "tier": "medium",
             "target_file": "a.py", "depends_on": [1]},
            {"id": 3, "description": "Implement b.py", "tier": "medium",
             "target_file": "b.py", "depends_on": [1]},
        ],
        "waves": [[1], [2, 3]],
    }


def test_upstream_artifact_forwarding() -> None:
    config = TGsConfig()
    waves = build_host_spawn_waves(
        _dag_plan(), config=config, caller="claude-code", run_id="swarm-up-test"
    )
    producer = waves[0]["agents"][0]
    consumers = waves[1]["agents"]
    # The depended-upon agent is told where to leave its output...
    assert producer["artifact_path"]
    assert "depend on your output" in producer["prompt"]
    # ...and both dependents are pointed at that one file, rather than the host
    # re-pasting the diagnosis into each prompt.
    for agent in consumers:
        assert [u["id"] for u in agent["upstream"]] == ["1"]
        assert agent["upstream"][0]["artifact_path"] == producer["artifact_path"]
        assert "Read these upstream results" in agent["prompt"]
    assert "artifact_path" not in consumers[0]


def test_upstream_forwarding_off_by_config() -> None:
    config = TGsConfig()
    config.host_native.forward_upstream_results = False
    waves = build_host_spawn_waves(
        _dag_plan(), config=config, caller="claude-code", run_id="swarm-up-test"
    )
    assert "artifact_path" not in waves[0]["agents"][0]
    assert "upstream" not in waves[1]["agents"][0]


def test_upstream_forwarding_absent_without_dependencies() -> None:
    config = TGsConfig()
    plan = {
        "subtasks": [
            {"id": 1, "description": "Edit a.py", "tier": "low", "target_file": "a.py",
             "depends_on": []},
        ],
        "waves": [[1]],
    }
    waves = build_host_spawn_waves(
        plan, config=config, caller="claude-code", run_id="swarm-up-test"
    )
    agent = waves[0]["agents"][0]
    assert "artifact_path" not in agent and "upstream" not in agent


def test_pattern_hash_is_carried_into_the_spawn_payload() -> None:
    """Learning keys on the kind of work, not the rendered prompt."""
    config = TGsConfig()
    plan = _review_plan()
    plan["subtasks"][0]["pattern_hash"] = "abc123def456"
    waves = build_host_spawn_waves(plan, config=config, caller="claude-code")
    assert waves[0]["agents"][0]["pattern_hash"] == "abc123def456"


def test_instruction_tax_report_is_per_host_and_thresholded(tmp_path) -> None:
    config = TGsConfig()
    from shared.host_spawn import instruction_tax_report

    (tmp_path / "CLAUDE.md").write_text("x" * 40_000, encoding="utf-8")
    big = instruction_tax_report(
        config, workspace_root=str(tmp_path), agent_count=15, caller="claude-code"
    )
    assert big is not None and big["per_agent_bytes"] == 40_000
    assert big["total_bytes"] == 600_000
    # Codex does not read CLAUDE.md, so it must not be billed for it.
    assert instruction_tax_report(
        config, workspace_root=str(tmp_path), agent_count=15, caller="codex"
    ) is None
    # Under the threshold, and unknown hosts, stay quiet.
    assert instruction_tax_report(
        config, workspace_root=str(tmp_path), agent_count=1, caller="claude-code"
    ) is None
    assert instruction_tax_report(
        config, workspace_root=str(tmp_path), agent_count=15, caller="mystery-shell"
    ) is None
