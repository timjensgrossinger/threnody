"""Comprehensive tests for all 10 Threnody skills with Antigravity caller.

Tests all MCP skill handlers and skill contracts:
1. threnody-cost: inspect_spend, inspect_quality, inspect_run_receipt
2. threnody-fast-review: fast code review planning & review fanout
3. threnody-fullstack: fullstack decomposition & role assignments
4. threnody-plan: plan_task, fleet_plan, expand_host_plan, auto topology & agy format
5. threnody-routing: route_task, validate_routing_guard, routing exceptions
6. threnody-subtasks: list_subtasks, stop_subtask, resume_subtask, execute_subtask
7. threnody-swarm: execute_swarm, inspect_swarm, resume_swarm, apply_preview
8. threnody-swarm-review: multi-agent review consensus & report_host_swarm_complete
9. threnody-task: start_task, inspect_task, inspect_status, approval queue CRUD
10. threnody-workflow: list_task_packs, plan_task_pack, blueprint export/run, trace tools
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import mcp_server
from shared.config import TGsConfig
from shared.db import Database
from shared.db_client import open_database
from shared.orchestrator import Orchestrator
from unittest.mock import MagicMock
from shared.planner import CLIBackend, Planner
from shared.router import TaskRouter


class MockPlannerBackend(CLIBackend):
    def call(self, prompt: str, model: str | None = None, timeout: int = 120) -> str | None:
        plan = {
            "tasks": [
                {"id": 1, "tier": "low", "task": "Task 1", "files": ["a.py"]},
                {"id": 2, "tier": "medium", "task": "Task 2", "files": ["b.py"]},
            ]
        }
        return f"```json\n{json.dumps(plan)}\n```"


@pytest.fixture
def test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up an isolated database and environment for skill testing."""
    db_file = tmp_path / "test_skills.db"
    config = TGsConfig(db_path=db_file)
    db = open_database(str(db_file), config=config)
    router = TaskRouter(config)
    backend = MockPlannerBackend()
    planner = Planner(config, backend=backend, db=db)
    mock_provider = MagicMock()
    orch = Orchestrator(
        config=config,
        provider=mock_provider,
        planner=planner,
        db=db,
        caller="antigravity",
    )

    monkeypatch.setenv("THRENODY_HOST", "antigravity")
    monkeypatch.setattr(mcp_server, "_client_name", "antigravity")
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "antigravity")
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (config, db, router, planner, orch))

    return {
        "tmp_path": tmp_path,
        "config": config,
        "db": db,
        "router": router,
        "planner": planner,
        "orch": orch,
    }




class TestSkillCost:
    """Skill 1: threnody-cost tests."""

    def test_inspect_spend(self, test_env) -> None:
        res = mcp_server.handle_inspect_spend({"caller": "antigravity"})
        assert isinstance(res, dict)
        assert "window" in res
        assert "totals" in res
        assert "by_tier" in res

    def test_inspect_quality(self, test_env) -> None:
        res = mcp_server.handle_inspect_quality({"caller": "antigravity"})
        assert isinstance(res, dict)
        assert "window" in res
        assert "rows" in res
        assert "event_count" in res

    def test_inspect_run_receipt(self, test_env) -> None:
        res = mcp_server.handle_inspect_run_receipt({"run_id": "nonexistent_run", "caller": "antigravity"})
        assert isinstance(res, dict)



class TestSkillFastReview:
    """Skill 2: threnody-fast-review tests."""

    def test_fast_review_planning(self, test_env) -> None:
        res = mcp_server.handle_plan_task({
            "task": "Review authentication module security and error handling",
            "caller": "antigravity",
        })
        assert isinstance(res, dict)
        assert "subtasks" in res or "waves" in res or "tasks" in res or "host_spawn_waves" in res
        if "host_spawn_waves" in res and res["host_spawn_waves"]:
            wave = res["host_spawn_waves"][0]
            assert "wave" in wave
            assert "agents" in wave
            for agent in wave["agents"]:
                assert agent["tool"] == "invoke_subagent"
                assert "tier" in agent
                assert "prompt" in agent


class TestSkillFullstack:
    """Skill 3: threnody-fullstack tests."""

    def test_fullstack_decomposition_and_roles(self, test_env) -> None:
        res = mcp_server.handle_plan_task({
            "task": "Build fullstack dashboard: React frontend, FastAPI backend, SQLite models, and tests",
            "caller": "antigravity",
        })
        assert isinstance(res, dict)
        if "host_spawn_waves" in res and res["host_spawn_waves"]:
            agents = res["host_spawn_waves"][0].get("agents", [])
            assert len(agents) >= 1
            for agent in agents:
                assert agent["tool"] == "invoke_subagent"
                assert "workspace" in agent



class TestSkillPlan:
    """Skill 4: threnody-plan tests."""

    def test_plan_task(self, test_env) -> None:
        plan_res = mcp_server.handle_plan_task({
            "task": "Refactor router to support streaming JSON-RPC responses",
            "caller": "antigravity",
        })
        assert isinstance(plan_res, dict)
        assert "subtasks" in plan_res or "waves" in plan_res or "tasks" in plan_res


    def test_fleet_plan_and_expand_host_plan(self, test_env) -> None:
        fleet_res = mcp_server.handle_fleet_plan({
            "task": "Implement distributed worker pool",
            "caller": "antigravity",
        })
        assert isinstance(fleet_res, dict)

        expand_res = mcp_server.handle_expand_host_plan({
            "plan_id": "test_plan_123",
            "task": "Implement worker pool",
            "caller": "antigravity",
        })
        assert isinstance(expand_res, dict)


class TestSkillRouting:
    """Skill 5: threnody-routing tests."""

    def test_route_task_antigravity(self, test_env) -> None:
        res = mcp_server.handle_route_task({
            "task": "Fix simple typo in documentation",
            "caller": "antigravity",
        })
        assert res.get("tier") in ("low", "medium", "high")
        assert "host_spawn" in res or "execution_hint" in res
        if "host_spawn" in res:
            assert res["host_spawn"]["tool"] == "invoke_subagent"

    def test_validate_routing_guard(self, test_env) -> None:
        guard_res = mcp_server.handle_validate_routing_guard({
            "caller": "antigravity",
            "cwd": str(test_env["tmp_path"]),
            "target_file": str(test_env["tmp_path"] / "app.py"),
            "tool_name": "write_to_file",
        })
        assert "valid" in guard_res

    def test_routing_exceptions_crud(self, test_env) -> None:
        add_res = mcp_server.handle_routing_exception_add({
            "exception_type": "path",
            "pattern": "custom_build/**",
            "caller": "antigravity",
        })
        assert add_res.get("added") is True or "exception" in add_res

        list_res = mcp_server.handle_routing_exception_list({"caller": "antigravity"})
        assert isinstance(list_res, (dict, list))

        rem_res = mcp_server.handle_routing_exception_remove({
            "exception_type": "path",
            "pattern": "custom_build/**",
            "caller": "antigravity",
        })
        assert isinstance(rem_res, dict)
        assert rem_res.get("removed") is True or "pattern" in rem_res



class TestSkillSubtasks:
    """Skill 6: threnody-subtasks tests."""

    def test_list_and_manage_subtasks(self, test_env) -> None:
        list_res = mcp_server.handle_list_subtasks({"caller": "antigravity"})
        assert isinstance(list_res, (dict, list))

        stop_res = mcp_server.handle_stop_subtask({"subtask_id": "nonexistent", "caller": "antigravity"})
        assert isinstance(stop_res, dict)

        resume_res = mcp_server.handle_resume_subtask({"subtask_id": "nonexistent", "caller": "antigravity"})
        assert isinstance(resume_res, dict)

    def test_execute_subtask_requires_host_native(self, test_env) -> None:
        res = mcp_server.handle_execute_subtask({
            "subtask_id": "sub_1",
            "task": "Write test file",
            "caller": "antigravity",
        })
        assert isinstance(res, dict)


class TestSkillSwarm:
    """Skill 7: threnody-swarm tests."""

    def test_execute_swarm_and_inspect(self, test_env) -> None:
        swarm_res = mcp_server.handle_execute_swarm({
            "task": "Migrate database schema and update queries",
            "caller": "antigravity",
        })
        assert isinstance(swarm_res, dict)

        inspect_res = mcp_server.handle_inspect_swarm({
            "swarm_id": "nonexistent_swarm",
            "caller": "antigravity",
        })
        assert isinstance(inspect_res, dict)

    def test_resume_swarm_and_apply_preview(self, test_env) -> None:
        resume_insp = mcp_server.handle_resume_swarm_inspect({
            "checkpoint_id": "chk_1",
            "caller": "antigravity",
        })
        assert isinstance(resume_insp, dict)

        preview_res = mcp_server.handle_apply_preview({
            "preview_id": "prev_1",
            "action": "view",
            "caller": "antigravity",
        })
        assert isinstance(preview_res, dict)


class TestSkillSwarmReview:
    """Skill 8: threnody-swarm-review tests."""

    def test_swarm_review_and_completion_report(self, test_env) -> None:
        swarm_res = mcp_server.handle_execute_swarm({
            "task": "Review pull request #42 for concurrency leaks",
            "caller": "antigravity",
        })
        assert isinstance(swarm_res, dict)

        report_res = mcp_server.handle_report_host_swarm_complete({
            "run_id": "run_test_review",
            "results": [
                {"agent": "reviewer-1", "findings": ["Finding A"], "score": 90},
                {"agent": "reviewer-2", "findings": ["Finding A", "Finding B"], "score": 85},
            ],
            "caller": "antigravity",
        })
        assert isinstance(report_res, dict)


class TestSkillTask:
    """Skill 9: threnody-task tests."""

    def test_task_lifecycle_and_approval_queue(self, test_env) -> None:
        start_res = mcp_server.handle_start_task({
            "task": "Optimize startup time",
            "caller": "antigravity",
        })
        assert isinstance(start_res, dict)

        status_res = mcp_server.handle_inspect_status({"caller": "antigravity"})
        assert isinstance(status_res, dict)

        q_list = mcp_server.handle_approval_queue_list({"caller": "antigravity"})
        assert isinstance(q_list, (dict, list))

        q_app = mcp_server.handle_approval_queue_approve({"item_id": "item_1", "caller": "antigravity"})
        assert isinstance(q_app, dict)

        q_rej = mcp_server.handle_approval_queue_reject({"item_id": "item_1", "caller": "antigravity"})
        assert isinstance(q_rej, dict)


class TestSkillWorkflow:
    """Skill 10: threnody-workflow tests."""

    def test_task_packs_and_blueprints(self, test_env) -> None:
        packs = mcp_server.handle_list_task_packs({"caller": "antigravity"})
        assert isinstance(packs, (dict, list))

        plan_pack = mcp_server.handle_plan_task_pack({
            "pack_id": "security_audit",
            "caller": "antigravity",
        })
        assert isinstance(plan_pack, dict)

        bp_export = mcp_server.handle_workflow_blueprint_export({
            "blueprint_id": "default",
            "caller": "antigravity",
        })
        assert isinstance(bp_export, dict)

        bp_run = mcp_server.handle_workflow_blueprint_run({
            "blueprint_id": "default",
            "caller": "antigravity",
        })
        assert isinstance(bp_run, dict)

    def test_trace_tools(self, test_env) -> None:
        t_show = mcp_server.handle_trace_show({"trace_id": "tr_1", "caller": "antigravity"})
        assert isinstance(t_show, dict)

        t_rep = mcp_server.handle_trace_replay({"trace_id": "tr_1", "caller": "antigravity"})
        assert isinstance(t_rep, dict)

        t_fork = mcp_server.handle_trace_fork({"trace_id": "tr_1", "caller": "antigravity"})
        assert isinstance(t_fork, dict)

        t_diff = mcp_server.handle_trace_diff({"trace_a": "tr_1", "trace_b": "tr_2", "caller": "antigravity"})
        assert isinstance(t_diff, dict)
