#!/usr/bin/env python3
"""Focused tests for Phase 37 execute_swarm budget preview and confirmation."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
from shared.config import TGsConfig
from shared.db import Database
from shared.planner import ExecutionPlan


def _stub_init(monkeypatch, tmp_path: Path) -> tuple[TGsConfig, Database]:
    db_path = tmp_path / "execute-swarm-budget.db"
    cfg = TGsConfig(db_path=db_path)
    db = Database(db_path=db_path)
    db._init_schema(db._get_connection())
    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (cfg, db, None, None, None),
    )
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "test-budget")
    monkeypatch.setenv("PREVIEW_TOKEN_SECRET", f"test-preview-{tmp_path.name}")
    mcp_server._execute_swarm_rate_limit.clear()
    return cfg, db


def test_preview_returned_when_over_budget(monkeypatch, tmp_path: Path) -> None:
    _cfg, db = _stub_init(monkeypatch, tmp_path)

    result = mcp_server.handle_execute_swarm(
        {"task": {"id": "over-budget"}, "max_agents": 4, "budget_limit": 0.1}
    )

    assert result["started"] is False
    payload = result["result"]
    assert payload["preview"] is True
    assert isinstance(payload["preview_token"], str)
    assert payload["preview_token"]
    assert payload["budget_limit"] == 0.1
    assert payload["estimated_cost"] > payload["budget_limit"]

    with db.conn() as conn:
        status_row = conn.execute(
            "SELECT status FROM swarm_runs WHERE swarm_id = ?",
            (payload["swarm_id"],),
        ).fetchone()
        confirmed_row = conn.execute(
            "SELECT COUNT(*) FROM swarm_events WHERE swarm_id = ? AND event_type = ?",
            (payload["swarm_id"], "preview_confirmed"),
        ).fetchone()

    assert status_row == ("planned",)
    assert confirmed_row == (0,)


def test_preview_token_persisted(monkeypatch, tmp_path: Path) -> None:
    _cfg, db = _stub_init(monkeypatch, tmp_path)

    result = mcp_server.handle_execute_swarm(
        {"task": {"id": "persist-preview"}, "max_agents": 3, "budget_limit": 0.1}
    )

    preview_token = result["result"]["preview_token"]
    token_hmac = mcp_server._execute_swarm_preview_token_hmac(preview_token)
    swarm_id = db.lookup_preview_token_swarm_id(token_hmac)
    preview_payload = db.get_latest_swarm_event_payload(
        result["result"]["swarm_id"],
        "preview_required",
    )

    assert swarm_id == result["result"]["swarm_id"]
    assert preview_payload is not None
    assert "request" not in preview_payload
    assert "task_text" not in preview_payload


def test_host_native_swarm_response_declares_no_external_delegation(monkeypatch, tmp_path: Path) -> None:
    cfg, db = _stub_init(monkeypatch, tmp_path)
    plan = ExecutionPlan(
        analysis="host-native boundary test",
        subtasks=[],
        waves=[],
        total_agents=0,
        topology="dag",
        strategy="dag",
    )
    planner = type(
        "PlannerStub",
        (),
        {"plan_to_dict": staticmethod(lambda execution_plan: mcp_server.Planner.plan_to_dict(execution_plan))},
    )()
    monkeypatch.setattr(
        mcp_server,
        "_planner_plan_for_caller",
        lambda *args, **kwargs: (plan, True),
    )
    monkeypatch.setattr(
        mcp_server,
        "_attach_host_spawn_metadata",
        lambda plan_dict, **kwargs: plan_dict.update({"host_spawn_waves": []}),
    )
    monkeypatch.setattr(
        mcp_server,
        "_issue_host_handoff_routing_guard",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        mcp_server,
        "build_learning_report_contract",
        lambda *args, **kwargs: {"report_mode": "batch"},
    )

    result = mcp_server._execute_swarm_host_native_response(
        config=cfg,
        db=db,
        planner=planner,
        router=None,
        swarm_id="swarm-boundary",
        task_text="REVIEW: shared/discovery.py",
        caller="codex",
        request_meta={"workspace_root": str(tmp_path), "topology": "dag"},
        estimated_cost=0.0,
    )

    payload = result["result"]
    assert payload["host_execution_mode"] == "host_native"
    assert payload["awaiting_host_execution"] is True
    assert payload["execution_boundary"] == {
        "mode": "host_native_only",
        "data_export": "host_agent_prompts_only",
        "external_provider_delegation": False,
        "delegation_utilities_enabled": False,
        "provider": "codex",
    }


def test_confirm_preview_starts_execution(monkeypatch, tmp_path: Path) -> None:
    _cfg, db = _stub_init(monkeypatch, tmp_path)
    spawned: list[tuple[str, object]] = []
    monkeypatch.setattr(
        mcp_server,
        "_spawn_execute_swarm_runtime_handoff",
        lambda db_arg, swarm_id, execution_context: spawned.append((swarm_id, execution_context)),
    )

    preview = mcp_server.handle_execute_swarm(
        {"task": {"id": "confirm-preview"}, "max_agents": 2, "budget_limit": 0.1}
    )
    preview_payload = preview["result"]

    confirmed = mcp_server.confirm_preview_and_start(preview_payload["preview_token"])

    assert confirmed["started"] is True
    assert confirmed["result"]["confirmed"] is True
    assert confirmed["result"]["swarm_id"] == preview_payload["swarm_id"]
    assert (
        confirmed["result"]["cost_estimate"]["estimated"]
        == preview_payload["cost_estimate"]["estimated"]
    )
    assert spawned == [(
        preview_payload["swarm_id"],
        {
            "task_text": "{\"id\":\"confirm-preview\"}",
            "topology": "dag",
            "max_agents": 2,
        },
    )]

    token_hmac = mcp_server._execute_swarm_preview_token_hmac(
        preview_payload["preview_token"]
    )
    assert db.lookup_preview_token_swarm_id(token_hmac) is None

    preview_confirmed = db.get_latest_swarm_event_payload(
        preview_payload["swarm_id"],
        "preview_confirmed",
    )
    assert preview_confirmed is not None
    assert (
        preview_confirmed["estimated_cost"]
        == preview_payload["cost_estimate"]["estimated"]
    )


def test_confirm_preview_keeps_token_when_task_context_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _cfg, db = _stub_init(monkeypatch, tmp_path)
    spawned: list[tuple[str, object]] = []
    monkeypatch.setattr(
        mcp_server,
        "_spawn_execute_swarm_runtime_handoff",
        lambda db_arg, swarm_id, execution_context: spawned.append((swarm_id, execution_context)),
    )

    preview = mcp_server.handle_execute_swarm(
        {"task": {"id": "confirm-preview"}, "max_agents": 2, "budget_limit": 0.1}
    )
    preview_payload = preview["result"]
    swarm_id = preview_payload["swarm_id"]
    preview_event = db.get_latest_swarm_event_payload(swarm_id, "preview_required")
    assert preview_event is not None
    preview_event.pop("task_text", None)

    with db.conn() as conn:
        conn.execute(
            """
            UPDATE swarm_events
            SET payload = ?
            WHERE id = (
                SELECT id FROM swarm_events
                WHERE swarm_id = ? AND event_type = ?
                ORDER BY id DESC LIMIT 1
            )
            """,
            (json.dumps(preview_event), swarm_id, "preview_required"),
        )
        conn.execute(
            "DELETE FROM swarm_events WHERE swarm_id = ? AND event_type = ?",
            (swarm_id, "execute_swarm_requested"),
        )

    first_attempt = mcp_server.confirm_preview_and_start(preview_payload["preview_token"])

    assert first_attempt == {
        "error": "execution_error",
        "details": "preview metadata is missing original task context",
    }
    token_hmac = mcp_server._execute_swarm_preview_token_hmac(
        preview_payload["preview_token"]
    )
    assert db.lookup_preview_token_swarm_id(token_hmac) == swarm_id

    db.log_swarm_event(swarm_id, "execute_swarm_requested", {"task_text": "late-context"})

    second_attempt = mcp_server.confirm_preview_and_start(preview_payload["preview_token"])

    assert second_attempt["started"] is True
    assert spawned == [(
        swarm_id,
        {
            "task_text": "late-context",
            "topology": "dag",
            "max_agents": 2,
        },
    )]
    assert db.lookup_preview_token_swarm_id(token_hmac) is None


def test_confirm_preview_rejects_non_string_token(monkeypatch, tmp_path: Path) -> None:
    _stub_init(monkeypatch, tmp_path)

    result = mcp_server.confirm_preview_and_start(123)  # type: ignore[arg-type]

    assert result == {
        "error": "invalid_request",
        "details": "preview_token must be a string",
    }


def test_preview_token_hmac_rejects_non_string_token() -> None:
    try:
        mcp_server._execute_swarm_preview_token_hmac(123)  # type: ignore[arg-type]
    except ValueError as exc:
        assert str(exc) == "preview_token must be a string"
    else:
        raise AssertionError("expected ValueError for non-string preview token")


def test_confirm_preview_rejects_oversized_token(monkeypatch, tmp_path: Path) -> None:
    _stub_init(monkeypatch, tmp_path)

    result = mcp_server.confirm_preview_and_start(
        "x" * (mcp_server._EXECUTE_SWARM_MAX_PREVIEW_TOKEN_CHARS + 1)
    )

    assert result == {
        "error": "invalid_request",
        "details": (
            "preview_token must be <= "
            f"{mcp_server._EXECUTE_SWARM_MAX_PREVIEW_TOKEN_CHARS} characters"
        ),
    }


def test_preview_flow_uses_ephemeral_db(monkeypatch, tmp_path: Path) -> None:
    cfg, db = _stub_init(monkeypatch, tmp_path)

    result = mcp_server.handle_execute_swarm(
        {"task": {"id": "ephemeral-db"}, "max_agents": 2, "budget_limit": 0.1}
    )

    assert cfg.db_path == tmp_path / "execute-swarm-budget.db"
    assert db._db_path == cfg.db_path
    assert db._db_path.parent == tmp_path

    with db.conn() as conn:
        preview_count = conn.execute(
            "SELECT COUNT(*) FROM preview_tokens",
        ).fetchone()

    assert result["started"] is False
    assert preview_count == (1,)


def test_preview_metadata_failure_cleans_up_token(monkeypatch, tmp_path: Path) -> None:
    _cfg, db = _stub_init(monkeypatch, tmp_path)
    monkeypatch.setattr(
        db,
        "persist_preview_token_with_event",
        lambda *args, **kwargs: False,
    )

    result = mcp_server.handle_execute_swarm(
        {"task": {"id": "metadata-failure"}, "max_agents": 2, "budget_limit": 0.1}
    )

    assert result == {
        "error": "execution_error",
        "details": "failed to persist preview token",
    }
    with db.conn() as conn:
        pending_tokens = conn.execute(
            "SELECT COUNT(*) FROM preview_tokens WHERE used = 0",
        ).fetchone()

    assert pending_tokens == (0,)
