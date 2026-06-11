"""Tests for operator spend snapshots (inspect_spend / build_spend_snapshot)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.db import Database
from shared.spend import build_spend_snapshot, parse_spend_window


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "spend.db")


def test_parse_spend_window_days():
    since_ts, label = parse_spend_window("7d")
    assert label == "7d"
    assert since_ts <= time.time()


def test_build_spend_snapshot_empty(db):
    snapshot = build_spend_snapshot(db, since="7d")
    assert snapshot["window"] == "7d"
    assert snapshot["totals"]["subtask_count"] == 0
    assert snapshot["totals"]["savings_usd"] == 0.0
    assert snapshot["usage_state"] == []
    assert "disclaimer" in snapshot


def test_build_spend_snapshot_usage_state(db):
    from shared.config import ProviderUsageWindowConfig, TGsConfig, UsageWindowEntry

    cfg = TGsConfig()
    cfg.provider_usage_windows = {
        "github-copilot": ProviderUsageWindowConfig(
            windows=[
                UsageWindowEntry(
                    hours=24,
                    budget_tokens=1000,
                    threshold=0.8,
                    action="prefer_alternatives",
                )
            ]
        )
    }
    db.log_agent_result(
        session_id="s1",
        task_hash="task-a",
        agent_id=1,
        tier="low",
        model="gpt-5-mini",
        provider_name="github-copilot",
        tokens_used=600,
    )
    snapshot = build_spend_snapshot(db, since="7d", config=cfg)
    assert len(snapshot["usage_state"]) == 1
    entry = snapshot["usage_state"][0]
    assert entry["provider"] == "github-copilot"
    assert entry["tokens_used"] == 600
    assert entry["limit"] == 1000


def test_build_spend_snapshot_aggregates(db):
    db.record_cost_telemetry(
        "t1", "low", "github-copilot", "gpt-5-mini",
        1000, 200, 0.0, counterfactual_cost_usd=0.01,
    )
    db.record_cost_telemetry(
        "t2", "high", "claude-code", "opus",
        5000, 1000, 0.015, counterfactual_cost_usd=0.015,
    )
    snapshot = build_spend_snapshot(db, since="7d")
    assert snapshot["totals"]["subtask_count"] == 2
    assert snapshot["totals"]["savings_usd"] > 0
    assert snapshot["totals"]["free_subtask_count"] == 1
    assert len(snapshot["by_provider"]) >= 1


def test_inspect_spend_mcp_handler(monkeypatch, tmp_path):
    import mcp_server
    from shared.config import TGsConfig

    db_path = tmp_path / "mcp-spend.db"
    cfg = TGsConfig(db_path=db_path)
    db = Database(db_path=db_path)
    db.record_cost_telemetry(
        "task-a", "low", "codex", "o4-mini",
        800, 100, 0.0001, counterfactual_cost_usd=0.005,
    )
    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (cfg, db, None, None, None),
    )
    result = mcp_server.inspect_spend("7d")
    assert result["totals"]["subtask_count"] == 1
    assert result["totals"]["savings_usd"] > 0


def test_inspect_spend_tool_registered() -> None:
    import mcp_server

    tool_names = {tool["name"] for tool in mcp_server.TOOLS}
    assert "inspect_spend" in tool_names
    assert "inspect_spend" in mcp_server.HANDLERS
