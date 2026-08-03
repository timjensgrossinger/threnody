#!/usr/bin/env python3
"""
Tests for Phase 3 schema additions in shared/db.py.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.db import Database


def _table_columns(conn, table_name: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def test_phase3_schema_tables_and_columns() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        with db.conn() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

            assert "agent_audit" in tables
            assert "fanout_telemetry" in tables

            agent_definition_columns = _table_columns(conn, "agent_definitions")
            project_routing_columns = _table_columns(conn, "project_routing")

            assert "promotion_state" in agent_definition_columns
            assert "learning_enabled" in project_routing_columns
        db.close()


def test_phase3_schema_writes_round_trip() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        with db.conn() as conn:
            agent_cursor = conn.execute(
                """
                INSERT INTO agent_audit
                    (agent_id, event_type, details_json, canonical_id, merged_from, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "agent-1",
                    "merged",
                    '{"reason":"near-duplicate"}',
                    "agent-1",
                    "agent-2",
                    "2026-04-10T00:00:00Z",
                ),
            )
            fanout_cursor = conn.execute(
                """
                INSERT INTO fanout_telemetry
                    (task_id, selected_routers, budget_accounting, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "task-1",
                    '["router-a","router-b"]',
                    '{"total_tokens":1234}',
                    "2026-04-10T00:00:01Z",
                ),
            )

            assert int(agent_cursor.lastrowid or 0) > 0
            assert int(fanout_cursor.lastrowid or 0) > 0

            agent_row = conn.execute(
                """
                SELECT agent_id, event_type, details_json, canonical_id, merged_from
                FROM agent_audit
                """,
            ).fetchone()
            fanout_row = conn.execute(
                """
                SELECT task_id, selected_routers, budget_accounting
                FROM fanout_telemetry
                """,
            ).fetchone()

            assert agent_row == (
                "agent-1",
                "merged",
                '{"reason":"near-duplicate"}',
                "agent-1",
                "agent-2",
            )
            assert fanout_row == (
                "task-1",
                '["router-a","router-b"]',
                '{"total_tokens":1234}',
            )
        db.close()


def test_project_settings_table_exists() -> None:
    """D-10 keeps operator defaults in SQLite; D-02 keeps them inspectable."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        with db.conn() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "project_settings" in tables
        db.close()


def test_approval_queue_table_exists() -> None:
    """D-10 stores the queue locally and D-02 keeps it operator-visible."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        with db.conn() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "approval_queue" in tables
        db.close()


def test_project_setting_helpers_round_trip() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        defaults = db.get_project_settings("project-a")

        updated = db.set_project_setting("project-a", "concurrency_limit", 5)
        assert updated["concurrency_limit"] == 5

        learning = db.set_project_setting("project-a", "learning_enabled", True)
        assert learning["learning_enabled"] is True

        reset_one = db.reset_project_setting("project-a", "concurrency_limit")
        assert reset_one["concurrency_limit"] == defaults["concurrency_limit"]

        reset_all = db.reset_project_setting("project-a")
        # Reset restores the configured default, exactly like concurrency_limit
        # below. It used to hard-write 0, which made "reset learning_enabled"
        # silently mean "disable learning" rather than "forget my choice".
        assert reset_all["learning_enabled"] == defaults["learning_enabled"]
        assert reset_all["concurrency_limit"] == defaults["concurrency_limit"]
        db.close()


def test_list_pending_approvals_returns_only_pending_rows() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO approval_queue
                    (project_path, draft_fingerprint, draft_name, draft_json,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    "project-a",
                    "draft-1",
                    "pending-agent",
                    '{"instructions":"## Context\\nPending."}',
                    "2026-04-10T00:00:00Z",
                    "2026-04-10T00:00:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO approval_queue
                    (project_path, draft_fingerprint, draft_name, draft_json,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'approved', ?, ?)
                """,
                (
                    "project-a",
                    "draft-2",
                    "approved-agent",
                    '{"instructions":"## Context\\nApproved."}',
                    "2026-04-10T00:00:01Z",
                    "2026-04-10T00:00:01Z",
                ),
            )

        pending = db.list_pending_approvals("project-a", limit=10)
        assert len(pending) == 1
        assert pending[0]["name"] == "pending-agent"
        db.close()


def test_phase11_plan_cache_columns_are_idempotent() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        with db.conn() as conn:
            db._init_schema(conn)
            db._init_schema(conn)
            plan_cache_columns = _table_columns(conn, "plan_cache")
            assert "topology" in plan_cache_columns
            assert "plan_schema_version" in plan_cache_columns
        db.close()


def test_phase11_plan_cache_defaults() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO plan_cache (key, task_hash, plan_json, model, ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "phase11",
                    "task-hash",
                    '{"subtasks":[{"id":1,"description":"test","tier":"low"}]}',
                    "gpt-5-mini",
                    1.0,
                ),
            )
            row = conn.execute(
                "SELECT topology, plan_schema_version FROM plan_cache WHERE key = ?",
                ("phase11",),
            ).fetchone()
            assert row == (None, 1)
        db.close()
