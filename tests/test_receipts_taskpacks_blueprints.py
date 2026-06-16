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
