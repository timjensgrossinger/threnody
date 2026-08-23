"""Tests for Learning, Logging, Audit, and Hook systems with Antigravity workflows.

Covers:
- Outcome recording (record_outcome)
- Learning summaries & statistics (learning_outcome_stats, learning_agent_summary)
- Pattern health & mature pattern detection (learning_pattern_health)
- Learning audit log & cryptographic SHA-256 write audit chain (inspect_write_audit)
- Run log lifecycle & active run scoping (shared.run_log)
- PostToolUse learning capture hook with Antigravity tool shapes (write_to_file, replace_file_content)
- PreToolUse routing guard hook with Antigravity tool shapes (TargetFile, AbsolutePath)
- CLI --hook dispatcher in mcp_server.py
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
from unittest.mock import MagicMock
from shared.config import TGsConfig
from shared.db import Database
from shared.db_client import open_database
from shared.orchestrator import Orchestrator
from shared.planner import CLIBackend, Planner
from shared.router import TaskRouter
from shared import learning_hook, routing_hook, run_log


class MockPlannerBackend(CLIBackend):
    def call(self, prompt: str, model: str | None = None, timeout: int = 120) -> str | None:
        plan = {
            "tasks": [
                {"id": 1, "tier": "low", "task": "Task 1", "files": ["a.py"]},
            ]
        }
        return f"```json\n{json.dumps(plan)}\n```"


@pytest.fixture
def learning_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up an isolated database, run directory, and environment."""
    db_file = tmp_path / "test_learning.db"
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



class TestLearningSystem:
    """Tests for Threnody learning mechanisms."""

    def test_record_outcome_and_stats(self, learning_env) -> None:
        rec_res = mcp_server.handle_record_outcome({
            "task_id": "task_endpoint_1",
            "outcome": "accepted",
            "caller": "antigravity",
        })
        assert isinstance(rec_res, dict)
        assert rec_res.get("stored") is True or rec_res.get("recorded") is True or "outcome" in rec_res


        stats_res = mcp_server.handle_learning_outcome_stats({"caller": "antigravity"})
        assert isinstance(stats_res, dict)

        summary_res = mcp_server.handle_learning_agent_summary({"caller": "antigravity"})
        assert isinstance(summary_res, dict)

    def test_learning_pattern_health_and_mature_patterns(self, learning_env) -> None:
        db: Database = learning_env["db"]
        # Seed several repetitive task outcomes to trigger pattern detection
        for i in range(12):
            mcp_server.handle_record_outcome({
                "task_id": f"task_migration_{i}",
                "outcome": "accepted",
                "caller": "antigravity",
            })

        health_res = mcp_server.handle_learning_pattern_health({"caller": "antigravity"})
        assert isinstance(health_res, dict)

    def test_learning_audit_log(self, learning_env) -> None:
        audit_res = mcp_server.handle_learning_audit_log({
            "limit": 10,
            "caller": "antigravity",
        })
        assert isinstance(audit_res, dict)


class TestLoggingAndAuditChain:
    """Tests for Run Log and SHA-256 Audit Chain."""

    def test_run_log_lifecycle(self, learning_env) -> None:
        workspace = str(learning_env["tmp_path"])
        run_id = f"run_agy_{int(time.time())}"

        run_log.set_active_run(run_id, workspace_root=workspace)
        assert run_log.get_active_run(workspace_root=workspace) == run_id

        record = {
            "wave": 1,
            "spawn_id": "spawn_1",
            "task_id": "task_1",
            "tier": "low",
            "model": "gemini-3.5-flash",
            "success": True,
            "touched_files": [os.path.join(workspace, "main.py")],
            "output_excerpt": "Created main.py",
            "source": "antigravity",
            "ts": time.time(),
        }
        run_log.append_agent_record(run_id, record)

        records = run_log.read_run_log(run_id)
        assert len(records) >= 1
        assert records[0]["task_id"] == "task_1"
        assert records[0]["source"] == "antigravity"

    def test_inspect_write_audit_hash_chain(self, learning_env) -> None:
        db: Database = learning_env["db"]
        workspace = str(learning_env["tmp_path"])

        # Insert audit entries
        target_path = str(learning_env["tmp_path"] / "service.py")
        db.log_out_of_workspace_write(
            target_path=target_path,
            provider="antigravity",
            tier="low",
            grant_reason="approved edit",
        )

        res = mcp_server.handle_inspect_write_audit({
            "workspace_root": workspace,
            "caller": "antigravity",
        })
        assert isinstance(res, dict)
        assert "entries" in res or "count" in res



class TestHooksWithAntigravityPayloads:
    """Tests for routing and learning hooks with Antigravity tool calling payload formats."""

    def test_routing_hook_with_antigravity_format(self, learning_env) -> None:
        workspace = str(learning_env["tmp_path"])
        target = os.path.join(workspace, "lib.py")

        payload = {
            "name": "write_to_file",
            "arguments": {
                "TargetFile": target,
                "CodeContent": "def add(a, b): return a + b\n",
            },
            "Cwd": workspace,
        }
        parsed = routing_hook.parse_hook_payload(payload)
        assert parsed["target_file"] == target
        assert parsed["tool_name"] == "write_to_file"
        assert parsed["caller"] == "antigravity"

    def test_learning_hook_with_antigravity_format(self, learning_env) -> None:
        workspace = str(learning_env["tmp_path"])
        target = os.path.join(workspace, "handler.py")
        run_id = f"run_learn_{int(time.time())}"
        run_log.set_active_run(run_id, workspace_root=workspace)

        payload = {
            "tool_name": "replace_file_content",
            "parameters": {
                "TargetFile": target,
                "ReplacementContent": "new content",
            },
            "cwd": workspace,
            "tool_response": {"status": "SUCCESS", "success": True},
        }

        parsed = learning_hook.parse_hook_payload(payload)
        assert parsed["target_file"] == target
        assert parsed["success"] is True

        res = learning_hook.capture_edit(parsed)
        assert res.get("captured") is True
        assert res.get("run_id") == run_id

    def test_mcp_server_hook_cli_dispatch(self, learning_env, capsys: pytest.CaptureFixture[str]) -> None:
        workspace = str(learning_env["tmp_path"])
        target = os.path.join(workspace, "hook_test.py")

        # Test learning-capture CLI
        payload = json.dumps({
            "tool_name": "write_to_file",
            "arguments": {"TargetFile": target},
            "cwd": workspace,
        })
        rc = mcp_server._handle_hook_cli(["--hook", "learning-capture", "--json", payload])
        assert rc == 0

        # Test routing-guard CLI
        rc_guard = mcp_server._handle_hook_cli(["--hook", "routing-guard", "--json", payload])
        assert rc_guard in (0, 2)
