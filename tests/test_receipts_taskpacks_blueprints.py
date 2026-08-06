from __future__ import annotations

from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
from shared.agent_optimizer import choose_agent_count
from shared.config import TGsConfig
from shared.db import Database
from shared.receipts import build_cost_receipt, load_run_receipt, record_run_receipt
from shared.spend import build_spend_snapshot
from shared.task_packs import list_task_packs, plan_task_pack
from shared.workflow_blueprints import export_blueprint_from_receipt, run_workflow_blueprint


def _payload() -> dict:
    return {
        "topology": "dag",
        "analysis": "Host-native plan",
        "subtasks": [
            {"id": 1, "description": "Implement {{task}}", "tier": "low", "target_file": "app.py"},
            {"id": 2, "description": "Test {{task}}", "tier": "low", "target_file": "test_app.py"},
        ],
        "waves": [[1], [2]],
        "host_spawn_waves": [
            {
                "wave": 1,
                "agents": [
                    {
                        "id": "agent-1",
                        "prompt": "Implement {{task}}",
                        "tier": "low",
                        "target_files": ["app.py"],
                    }
                ],
            }
        ],
    }


def test_cost_receipt_does_not_fabricate_savings_for_unpriced_selection() -> None:
    """Regression: every route_task receipt previously reported "100% savings,
    $0.0025" regardless of the actual model, because the counterfactual model
    id ("claude-opus-4.6", a typo for the catalog's "claude-opus-4-6") was
    never in the price table, so high_counterfactual silently priced to $0 and
    the old fallback (selected_cost + 0.0025 * agents) fired unconditionally.
    """
    cost = build_cost_receipt(
        source_tool="route_task",
        task="opus-vs-opus",
        tier="high",
        model="not-a-real-model-id",
        provider="claude-code",
        payload={"host_spawn": {"tool": "Task"}},
    )
    assert cost["selected"]["estimated_cost_usd"] is None
    assert cost["savings"]["estimated_usd"] is None
    assert cost["savings"]["pct"] is None
    assert cost["savings"]["basis"] == "unpriced"
    assert cost["estimate_basis"] == "tier_token_budget"


def test_cost_receipt_reports_not_comparable_for_opus_vs_opus() -> None:
    """Selecting the same model as the counterfactual (host-native opus vs the
    opus counterfactual) must not read as a real savings figure."""
    cost = build_cost_receipt(
        source_tool="execute_swarm",
        task="host-native opus run",
        tier="high",
        model="claude-opus-4-6",
        provider="claude-code",
        payload={"host_spawn_waves": [{"wave": 1, "agents": [{}, {}]}]},
        estimated_cost_usd=5.0,  # deliberately higher than the counterfactual estimate
    )
    assert cost["savings"]["estimated_usd"] is None
    assert cost["savings"]["basis"] == "not_comparable"


def test_cost_receipt_reports_real_savings_when_both_sides_priced() -> None:
    """The happy path still works: a cheap priced model vs. the priced opus
    counterfactual produces a real, positive savings figure."""
    cost = build_cost_receipt(
        source_tool="route_task",
        task="cheap task",
        tier="low",
        model="claude-haiku-4-5-20251001",
        provider="claude-code",
        payload={"host_spawn": {"tool": "Task"}},
    )
    assert cost["savings"]["basis"] == "priced"
    assert cost["savings"]["estimated_usd"] > 0
    assert cost["savings"]["pct"] > 0


def test_agent_count_prefers_host_spawn_waves_over_collapsed_subtasks() -> None:
    """Regression: agent_count previously came from the planner's internal
    subtasks list, which can diverge from what actually got spawned (e.g. a
    sanitization step or max_agents cap trimming host_spawn_waves separately).
    """
    payload = {
        "subtasks": [{"id": 1}, {"id": 2}, {"id": 3}],  # pre-divergence, larger
        "host_spawn_waves": [{"wave": 1, "agents": [{"id": "a"}]}],  # actually spawned
    }
    cost = build_cost_receipt(
        source_tool="execute_swarm",
        task="t",
        tier="low",
        model="gpt-5-mini",
        provider="github-copilot",
        payload=payload,
    )
    assert cost["agent_count"] == 1


def test_run_receipt_persistence_and_formats(tmp_path: Path) -> None:
    db = Database(tmp_path / "receipts.db")
    payload = _payload()
    cost = build_cost_receipt(
        source_tool="plan_task",
        task="implement feature",
        tier="low",
        model="gpt-5-mini",
        provider="github-copilot",
        payload=payload,
    )
    receipt = record_run_receipt(
        db,
        run_id="run-1",
        source_tool="plan_task",
        task="implement feature",
        payload=payload,
        cost_receipt=cost,
        workspace_root=str(tmp_path),
    )

    assert receipt["run_id"] == "run-1"
    assert load_run_receipt(db, "run-1")["receipt"]["cost_receipt"]["source_tool"] == "plan_task"
    assert "# Threnody Run Receipt" in load_run_receipt(db, "run-1", format="markdown")["content"]
    assert "<!doctype html>" in load_run_receipt(db, "run-1", format="html")["content"]


def test_spend_snapshot_includes_recent_receipts(tmp_path: Path) -> None:
    db = Database(tmp_path / "spend-receipts.db")
    cost = build_cost_receipt(
        source_tool="route_task",
        task="simple edit",
        tier="low",
        model="gpt-5-mini",
        provider="github-copilot",
        payload={"host_spawn": {"tool": "Task"}},
        estimated_cost_usd=0.0,
    )
    record_run_receipt(
        db,
        run_id="route-1",
        source_tool="route_task",
        task="simple edit",
        payload={"host_spawn": {"tool": "Task"}},
        cost_receipt=cost,
    )

    snapshot = build_spend_snapshot(db, since="7d")
    assert snapshot["receipts"]["count"] == 1
    assert snapshot["receipts"]["estimated_savings_usd"] > 0


def test_task_packs_plan_injects_pack_metadata() -> None:
    packs = {pack["name"] for pack in list_task_packs()}
    assert {"test-gap", "security-review", "release-check"} <= packs

    plan = plan_task_pack("security-review", "Review auth.py and db.py")
    assert plan["task_pack"]["name"] == "security-review"
    assert plan["subtasks"]
    assert "security review" in plan["subtasks"][0]["description"].lower()


def test_task_pack_handler_returns_batch_spawn_handoff_without_planner(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = TGsConfig(db_path=tmp_path / "taskpack.db")
    db = Database(db_path=cfg.db_path)

    class FailPlanner:
        def plan(self, *_args, **_kwargs):
            raise AssertionError("task packs must not call the LLM planner")

    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")
    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (cfg, db, None, FailPlanner(), None),
    )

    result = mcp_server.handle_plan_task_pack(
        {
            "pack": "docs-sync",
            "task": "Update README.md and docs/usage.md",
            "cwd": str(tmp_path),
        }
    )

    assert result["planner_host_execution_mode"] == "host_native"
    assert result["host_spawn_waves"]
    first_wave = result["host_spawn_waves"][0]
    assert first_wave["parallel_start_required"] is True
    assert first_wave["spawn_batch"] == first_wave["agents"]


def test_workflow_blueprint_export_and_run(tmp_path: Path) -> None:
    db = Database(tmp_path / "blueprints.db")
    cost = build_cost_receipt(
        source_tool="plan_task",
        task="Implement {{task}}",
        tier="low",
        model="gpt-5-mini",
        provider="github-copilot",
        payload=_payload(),
    )
    record_run_receipt(
        db,
        run_id="run-blue",
        source_tool="plan_task",
        task="Implement {{task}}",
        payload=_payload(),
        cost_receipt=cost,
    )

    exported = export_blueprint_from_receipt(db, run_id="run-blue", name="Feature Flow")
    assert exported["name"] == "feature-flow"

    replay = run_workflow_blueprint(
        db,
        name="feature-flow",
        inputs={"task": "billing export"},
    )
    assert replay["planning_tokens_saved"] is True
    assert "billing export" in replay["host_spawn_waves"][0]["agents"][0]["prompt"]


def test_agent_count_optimizer_defaults_to_single_for_simple_task(tmp_path: Path) -> None:
    decision = choose_agent_count("Create greet.py", hard_cap=12)
    assert decision["recommended_agents"] == 1
    assert decision["strategy"] == "single_agent"

    db = Database(tmp_path / "swarm-opt.db")
    prepared = mcp_server.prepare_swarm_execution_request(
        {"task": "Create greet.py"},
        config=TGsConfig.defaults(),
        db=db,
        swarm_id="swarm-opt",
    )
    assert prepared["effective_agents"] == 1
    assert prepared["agent_count_optimizer"]["strategy"] == "single_agent"


def test_agent_count_optimizer_scales_large_reviews_to_cap() -> None:
    files = " ".join(f"src/file{i}.py" for i in range(35))
    decision = choose_agent_count(f"Review these files: {files}", hard_cap=12)
    assert decision["recommended_agents"] == 12
    assert decision["strategy"] == "review_file_sweep"


def test_agent_count_optimizer_sizes_review_sentinel_by_dimension_not_file_count(
    tmp_path: Path, monkeypatch,
) -> None:
    """Regression: a REVIEW: sentinel task must be sized from the actual
    (file x dimension) fanout, not a dimension-blind file_count + 1 guess that
    silently caps a 3-file/5-dim review down to ~4 agents before review_fanout
    ever gets a say (the Aug swarm-review fanout collapse).

    Uses relative filenames (chdir'd into tmp_path) rather than the absolute
    tmp_path — extract_task_file_entries(allow_external=True) has a separate,
    pre-existing dual-match bug on absolute paths that is not part of this fix.
    """
    names = ("a.py", "b.py", "c.py")
    for name in names:
        (tmp_path / name).write_text(
            "\n".join(f"line {i}" for i in range(210)), encoding="utf-8"
        )
    monkeypatch.chdir(tmp_path)
    task = "REVIEW: " + " ".join(names)

    decision = choose_agent_count(task, hard_cap=100)
    assert decision["strategy"] == "review_dimension_fanout"
    assert decision["recommended_agents"] == 16  # 3 files x 5 dims + 1 synthesis

    # A configured hard_cap still bounds it — the fix removes the dimension-blind
    # guess, not the operator's ability to cap.
    capped = choose_agent_count(task, hard_cap=4)
    assert capped["recommended_agents"] == 4


def test_inspect_run_receipt_distinguishes_pending_failed_and_absent(
    monkeypatch, tmp_path: Path
) -> None:
    """Regression: a live-DB read that reported RunReceiptNotFound for a receipt
    that genuinely existed (19 rows in the table) collapsed three different
    facts — not written yet, write failed, or never registered — into one
    error, giving an operator no way to tell whether to retry.
    """
    db = Database(tmp_path / "receipt-states.db")
    db._init_schema(db._get_connection())
    monkeypatch.setattr(
        mcp_server, "_ensure_init", lambda: (TGsConfig.defaults(), db, None, None, None)
    )
    monkeypatch.setattr(mcp_server, "_pending_receipts", {})
    monkeypatch.setattr(mcp_server, "_failed_receipts", {})

    # Genuinely never registered.
    out = mcp_server.handle_inspect_run_receipt({"run_id": "swarm-none"})
    assert out == {"error": "RunReceiptNotFound", "run_id": "swarm-none"}

    # Background persist thread hasn't completed yet.
    mcp_server._pending_receipts["swarm-pending"] = 0.0
    out = mcp_server.handle_inspect_run_receipt({"run_id": "swarm-pending"})
    assert out["error"] == "RunReceiptPending"
    assert out["run_id"] == "swarm-pending"

    # Background persist thread raised.
    mcp_server._failed_receipts["swarm-failed"] = "boom"
    out = mcp_server.handle_inspect_run_receipt({"run_id": "swarm-failed"})
    assert out == {
        "error": "RunReceiptWriteFailed",
        "run_id": "swarm-failed",
        "details": "boom",
    }

    # Actually written and readable.
    record_run_receipt(db, run_id="swarm-ok", source_tool="test", task="t", payload={})
    out = mcp_server.handle_inspect_run_receipt({"run_id": "swarm-ok"})
    assert out["receipt"]["run_id"] == "swarm-ok"
    db.close()


def test_new_mcp_tools_registered() -> None:
    tool_names = {tool["name"] for tool in mcp_server.TOOLS}
    for name in {
        "inspect_run_receipt",
        "list_task_packs",
        "plan_task_pack",
        "workflow_blueprint_export",
        "workflow_blueprint_run",
    }:
        assert name in tool_names
        assert name in mcp_server.HANDLERS
