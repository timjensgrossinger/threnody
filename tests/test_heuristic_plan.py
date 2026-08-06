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
from shared import heuristic_plan as heuristic_plan_module


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
    # The coupled group escalates to high, which the hybrid split then fronts with
    # a read-only diagnosis. The invariant under test is about the *implementers*:
    # all coupled files stay owned by exactly one writing agent.
    writers = [st for st in payload["subtasks"] if not st.get("read_only")]
    assert len(writers) == 1
    # Coupled source group escalates above the flat "low".
    assert writers[0]["tier"] in {"medium", "high"}
    assert len(writers[0].get("target_files", [])) == 4


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


# --- routing/planning defect fixes (risk floor, exemptions, ownership) -------

def test_risk_filename_floors_to_medium() -> None:
    """A security-sensitive basename is never routed to the cheapest tier (#4)."""
    # Top-level files so they fan out independently (no dir-coupling), isolating
    # the risk-floor behavior on the credential file.
    task = "Create setup_credentials.py and helpers.py and notes.py for the plugin"
    payload = build_heuristic_plan_payload(task, default_tier="medium")
    by_file = {st["target_file"]: st for st in payload["subtasks"]}
    assert by_file["setup_credentials.py"]["tier"] in {"medium", "high"}
    # A plain non-risk sibling still routes cheaply.
    assert by_file["helpers.py"]["tier"] == "low"


def test_test_file_inherits_code_under_test_tier() -> None:
    """A test file inherits the tier of the code under test, not doc-low (#4)."""
    task = (
        "Update credentials.py and test_credentials.py and notes.py at top level"
    )
    payload = build_heuristic_plan_payload(task, default_tier="medium")
    by_file = {st["target_file"]: st for st in payload["subtasks"]}
    # credentials.py is risk → medium; its test inherits medium (not doc-low).
    assert by_file["credentials.py"]["tier"] in {"medium", "high"}
    assert by_file["test_credentials.py"]["tier"] == by_file["credentials.py"]["tier"]


def test_markdown_exempt_files_fold_inline_no_agent() -> None:
    """Direct-edit exempt files (.md) get no agent; they go to inline_files (#5)."""
    task = "Update handler.py and CLAUDE.md and README.md at top level"
    payload = build_heuristic_plan_payload(task, default_tier="medium")
    targets = {st["target_file"] for st in payload["subtasks"]}
    assert "handler.py" in targets
    assert "CLAUDE.md" not in targets and "README.md" not in targets
    inline = set(payload.get("inline_files", []))
    assert "CLAUDE.md" in inline and "README.md" in inline


def test_every_subtask_has_target_files_and_ownership_line() -> None:
    """target_files is authoritative and prompt scope agrees with it (#3)."""
    payload = build_heuristic_plan_payload(CALCULATOR_TASK, default_tier="medium")
    for st in payload["subtasks"]:
        tfs = st.get("target_files")
        assert isinstance(tfs, list) and tfs, st
        assert tfs == [st["target_file"]]
        assert "You own exactly these files:" in st["description"]


def test_same_dir_source_files_couple_without_keyword() -> None:
    """Distinct-role source files sharing a dir couple into one agent (#6)."""
    task = "Create core/parser.py and core/lexer.py and core/emitter.py"
    payload = build_heuristic_plan_payload(
        task, default_tier="medium", coupled_strategy="single"
    )
    # One writing agent owns all three; a read-only hybrid diagnosis may front it.
    writers = [st for st in payload["subtasks"] if not st.get("read_only")]
    assert len(writers) == 1
    assert len(writers[0]["target_files"]) == 3


UNDER_BUDGET_TASK = (
    "Build a tool: (1) api/routes.py with endpoints, (2) api/schema.py with models, "
    "(3) cli/main.py entrypoint, (4) store/db.py persistence, (5) worker/queue.py jobs"
)


def test_packing_is_reported_in_coverage_and_analysis() -> None:
    """An agent budget below the file count is surfaced, not just logged."""
    payload = build_heuristic_plan_payload(
        UNDER_BUDGET_TASK, default_tier="medium", max_agents=2
    )
    writers = [st for st in payload["subtasks"] if not st.get("read_only")]
    assert len(writers) <= 2
    coverage = payload["coverage"]
    assert coverage["deferred"] == []
    packed = coverage["packed"]
    assert packed["cap"] == 2
    assert packed["trigger"] == "max_agents"
    assert packed["agents_after"] <= 2
    assert packed["subtasks_before"] > packed["agents_after"]
    assert "Agent budget 2" in payload["analysis"]
    # The transient build-time key never reaches the plan payload.
    assert "packing" not in payload


def test_uncapped_plan_reports_no_packing() -> None:
    payload = build_heuristic_plan_payload(UNDER_BUDGET_TASK, default_tier="medium")
    coverage = payload["coverage"]
    assert coverage["deferred"] == []
    assert "packed" not in coverage
    assert "Agent budget" not in payload["analysis"]


def test_unlimited_sentinel_is_not_a_cap_of_one() -> None:
    """config.swarm_max_agents uses -1 for unlimited; it must not pack to 1 agent."""
    unlimited = build_heuristic_plan_payload(
        UNDER_BUDGET_TASK, default_tier="medium", max_agents=-1
    )
    baseline = build_heuristic_plan_payload(UNDER_BUDGET_TASK, default_tier="medium")
    assert len(unlimited["subtasks"]) == len(baseline["subtasks"])
    assert "packed" not in unlimited["coverage"]


def test_fullstack_plan_accounts_for_template_files() -> None:
    """Intent-template fanout gets the same file accounting as a listed fanout."""
    task = "Build a fullstack webapp with a React frontend, FastAPI backend, and REST API"
    payload = build_heuristic_plan_payload(task, default_tier="medium", max_agents=2)
    coverage = payload["coverage"]
    assert coverage["files_total"] >= coverage["files_assigned"] > 0
    assert coverage["deferred"] == []
    assert coverage["packed"]["cap"] == 2
    assert "Agent budget 2" in payload["analysis"]


# ---------------------------------------------------------------------------
# _load_role_quality_bias — write-path sibling of _load_quality_tier_bias
# ---------------------------------------------------------------------------

def test_load_role_quality_bias_disabled_by_default() -> None:
    """routing_bias_enabled defaults to False -> always {} regardless of ledger."""
    assert heuristic_plan_module._load_role_quality_bias() == {}


def test_load_role_quality_bias_agreement_across_models(monkeypatch) -> None:
    from shared.config import ModelQualityConfig, TGsConfig

    cfg = TGsConfig()
    cfg.model_quality = ModelQualityConfig(enabled=True, routing_bias_enabled=True)
    monkeypatch.setattr(TGsConfig, "from_yaml", staticmethod(lambda *a, **k: cfg))
    monkeypatch.setattr(
        "shared.quality_bias.load_model_quality_bias",
        lambda db, **k: {("opus", "implementer"): 1, ("sonnet", "implementer"): 1},
    )
    monkeypatch.setattr("shared.quality_bias.apply_quality_floor", lambda db, raw: raw)
    monkeypatch.setattr("shared.agents._get_agent_db", lambda: object())

    assert heuristic_plan_module._load_role_quality_bias() == {"implementer": 1}


def test_load_role_quality_bias_disagreement_yields_nothing(monkeypatch) -> None:
    from shared.config import ModelQualityConfig, TGsConfig

    cfg = TGsConfig()
    cfg.model_quality = ModelQualityConfig(enabled=True, routing_bias_enabled=True)
    monkeypatch.setattr(TGsConfig, "from_yaml", staticmethod(lambda *a, **k: cfg))
    monkeypatch.setattr(
        "shared.quality_bias.load_model_quality_bias",
        lambda db, **k: {("opus", "implementer"): 1, ("sonnet", "implementer"): -1},
    )
    monkeypatch.setattr("shared.quality_bias.apply_quality_floor", lambda db, raw: raw)
    monkeypatch.setattr("shared.agents._get_agent_db", lambda: object())

    assert heuristic_plan_module._load_role_quality_bias() == {}


# ---------------------------------------------------------------------------
# _finalize_subtasks — role-bias tier nudge on the write path
# ---------------------------------------------------------------------------

def test_finalize_subtasks_applies_role_tier_bias(monkeypatch) -> None:
    monkeypatch.setattr(
        heuristic_plan_module, "_load_role_quality_bias", lambda: {"implementer": 1}
    )
    subtasks = [
        {
            "description": "add a new export helper",
            "target_files": ["shared/exporter.py"],
            "tier": "low",
        }
    ]
    out = heuristic_plan_module._finalize_subtasks(subtasks)
    assert out[0]["role"] == "Implementer"
    assert out[0]["tier"] == "medium"


def test_finalize_subtasks_no_bias_leaves_tier_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(heuristic_plan_module, "_load_role_quality_bias", lambda: {})
    subtasks = [
        {
            "description": "add a new export helper",
            "target_files": ["shared/exporter.py"],
            "tier": "low",
        }
    ]
    out = heuristic_plan_module._finalize_subtasks(subtasks)
    assert out[0]["role"] == "Implementer"
    assert out[0]["tier"] == "low"


def test_finalize_subtasks_bias_clamps_at_high(monkeypatch) -> None:
    monkeypatch.setattr(
        heuristic_plan_module, "_load_role_quality_bias", lambda: {"implementer": 1}
    )
    subtasks = [
        {
            "description": "add a new export helper",
            "target_files": ["shared/exporter.py"],
            "tier": "high",
        }
    ]
    out = heuristic_plan_module._finalize_subtasks(subtasks)
    assert out[0]["tier"] == "high"
