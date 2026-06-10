#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server
from shared.db import (
    Database,
    ROUTING_GUARD_MODE_DIRECT,
    ROUTING_GUARD_MODE_EXECUTE_SUBTASK,
    ROUTING_GUARD_MODE_ROUTED_PLAN,
)
from shared.config import TGsConfig


class SelectionRegistry:
    def __init__(self, *, provider: str = "GitHub Copilot", model: str = "gpt-5-mini") -> None:
        self.provider = provider
        self.model = model

    def select_provider_for_tier(
        self,
        tier: str,
        *,
        prefer_free: bool = True,
        caller: str | None = None,
        code_only: bool = False,
        effort: str | None = None,
    ) -> dict[str, object]:
        return {
            "provider": self.provider,
            "provider_id": "github-copilot",
            "model": self.model,
            "tier": tier,
            "is_free": tier == "low",
            "billing_tier": "free" if tier == "low" else "subscription",
            "provider_cost_hint": "free" if tier == "low" else "included in subscription/quota",
            "cost_rank": 0 if tier == "low" else 2,
            "billing_source": "provider_default",
        }


class UnusedPlanner:
    def plan(self, _task: str) -> None:
        raise AssertionError("planner should not be used in this test")

    def plan_to_dict(self, _plan: object) -> dict[str, object]:
        raise AssertionError("planner should not be used in this test")


class UnusedOrchestrator:
    pass


def _stub_init(
    cfg: TGsConfig,
    db: Database,
    *,
    router: object = None,
    planner: object = None,
    orchestrator: object = None,
) -> tuple[object, object, object, object, object]:
    return (
        cfg,
        db,
        router,
        planner if planner is not None else UnusedPlanner(),
        orchestrator if orchestrator is not None else UnusedOrchestrator(),
    )


def _prepare_db(tmpdir: str) -> tuple[TGsConfig, Database]:
    db_path = Path(tmpdir) / "routing-guards.db"
    cfg = TGsConfig(db_path=db_path)
    db = Database(db_path=db_path)
    return cfg, db


class BytesPathLike(os.PathLike[bytes]):
    def __init__(self, value: str) -> None:
        self._value = value.encode()

    def __fspath__(self) -> bytes:
        return self._value


def test_routing_guard_store_round_trip() -> None:
    with tempfile.TemporaryDirectory() as td:
        _cfg, db = _prepare_db(td)
        guard = db.routing_guard_put(
            caller="claude-code",
            cwd=str(ROOT),
            mode=ROUTING_GUARD_MODE_DIRECT,
            source_tool="route_task",
            task_text="implement shared/db.py",
            tier="medium",
            provider="GitHub Copilot",
            model="gpt-5.4",
            file_hints=[str(ROOT / "shared" / "db.py")],
        )

        stored = db.routing_guard_get(caller="claude-code", cwd=str(ROOT))

        assert stored is not None
        assert stored["mode"] == ROUTING_GUARD_MODE_DIRECT
        assert stored["file_hints"] == [str(ROOT / "shared" / "db.py")]
        assert stored["provider"] == "GitHub Copilot"
        assert guard["expires_ts"] == stored["expires_ts"]


def test_path_normalization_helpers_decode_bytes_pathlike() -> None:
    with tempfile.TemporaryDirectory() as td:
        target = Path(td).resolve()
        pathlike = BytesPathLike(str(target))

        normalized_cwd = mcp_server._normalized_cwd_or_none(pathlike)
        normalized_path = mcp_server._normalize_path_input(pathlike)

        assert normalized_cwd == str(target)
        assert normalized_path == str(target)
        assert isinstance(normalized_cwd, str)
        assert isinstance(normalized_path, str)


def test_routing_guard_expires() -> None:
    with tempfile.TemporaryDirectory() as td:
        _cfg, db = _prepare_db(td)
        db.routing_guard_put(
            caller="claude-code",
            cwd=str(ROOT),
            mode=ROUTING_GUARD_MODE_DIRECT,
            source_tool="route_task",
            task_text="implement shared/db.py",
        )

        with db.conn() as conn:
            conn.execute(
                "UPDATE routing_guards SET expires_ts = ? WHERE guard_key = ?",
                (0, db._routing_guard_key("claude-code", str(ROOT))),
            )

        assert db.routing_guard_get(caller="claude-code", cwd=str(ROOT)) is None


def test_routing_guard_migration_backfills_unique_guard_keys() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "routing-guards-legacy.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE routing_guards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    caller TEXT NOT NULL,
                    cwd TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL,
                    tier TEXT,
                    provider TEXT,
                    model TEXT,
                    source_tool TEXT NOT NULL DEFAULT '',
                    task_text TEXT NOT NULL DEFAULT '',
                    file_hints_json TEXT NOT NULL DEFAULT '[]',
                    created_ts REAL NOT NULL,
                    expires_ts REAL NOT NULL
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO routing_guards (
                    caller, cwd, mode, source_tool, task_text, file_hints_json,
                    created_ts, expires_ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("claude-code", str(ROOT), ROUTING_GUARD_MODE_DIRECT, "route_task", "older", "[]", 10.0, 4102444800.0),
                    ("claude-code", str(ROOT), ROUTING_GUARD_MODE_DIRECT, "route_task", "newer", "[]", 11.0, 4102444801.0),
                ],
            )

        db = Database(db_path=db_path)
        try:
            stored = db.routing_guard_get(caller="claude-code", cwd=str(ROOT))
            assert stored is not None
            assert stored["task_text"] == "newer"

            with db.conn() as conn:
                rows = conn.execute(
                    "SELECT guard_key, COUNT(*) FROM routing_guards GROUP BY guard_key"
                ).fetchall()
                indexes = conn.execute(
                    "PRAGMA index_list(routing_guards)"
                ).fetchall()

            assert rows == [(db._routing_guard_key("claude-code", str(ROOT)), 1)]
            assert any(
                row[1] == "idx_routing_guards_guard_key" and row[2] == 1
                for row in indexes
            )
        finally:
            db.close()


def test_route_task_issues_low_tier_execute_subtask_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.chdir(ROOT)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="low",
                score=0.21,
                reason="low-tier task",
                agents=2,
                override=False,
            )
        )
        registry = SelectionRegistry()

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, router=router))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        result = mcp_server.handle_route_task({"task": "fix shared/db.py", "cwd": str(ROOT)})

        assert result["routing_guard"]["mode"] == ROUTING_GUARD_MODE_EXECUTE_SUBTASK
        stored = db.routing_guard_get(caller="claude-code", cwd=str(ROOT))
        assert stored is not None
        assert stored["mode"] == ROUTING_GUARD_MODE_EXECUTE_SUBTASK


def test_validate_routing_guard_denies_without_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db))
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        result = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "shared" / "db.py"),
            "tool_name": "Edit",
        })

        assert result["valid"] is False
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_validate_routing_guard_denies_low_tier_direct_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.chdir(ROOT)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="low",
                score=0.21,
                reason="low-tier task",
                agents=2,
                override=False,
            )
        )
        registry = SelectionRegistry()

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, router=router))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        mcp_server.handle_route_task({"task": "fix shared/db.py", "cwd": str(ROOT)})
        result = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "shared" / "db.py"),
            "tool_name": "Edit",
        })

        assert result["valid"] is False
        assert "execute_subtask" in result["reason"]


def test_route_task_skips_guard_for_exempt_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.chdir(ROOT)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="low",
                score=0.21,
                reason="low-tier task",
                agents=2,
                override=False,
            )
        )
        registry = SelectionRegistry()

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, router=router))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        result = mcp_server.handle_route_task({"task": "update README.md", "cwd": str(ROOT)})

        assert "routing_guard" not in result
        assert db.routing_guard_get(caller="claude-code", cwd=str(ROOT)) is None


def test_route_task_resolves_exempt_relative_hints_against_caller_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.chdir(td)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="low",
                score=0.21,
                reason="low-tier task",
                agents=2,
                override=False,
            )
        )
        registry = SelectionRegistry()
        caller_cwd = str(Path.home())

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, router=router))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        result = mcp_server.handle_route_task({
            "task": "update .claude/settings.json",
            "cwd": caller_cwd,
        })

        assert "routing_guard" not in result
        assert db.routing_guard_get(caller="claude-code", cwd=caller_cwd) is None


def test_validate_routing_guard_allows_exempt_markdown_without_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db))
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        result = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "README.md"),
            "tool_name": "Edit",
        })

        assert result["valid"] is True
        assert result["mode"] == "exempt"
        assert result["reason"] == "routing_exception_filetype"


def test_validate_routing_guard_allows_exempt_cursor_rule_without_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db))
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        mdc_result = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / ".cursor" / "rules" / "router.mdc"),
            "tool_name": "Edit",
        })
        legacy_result = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / ".cursorrules"),
            "tool_name": "Edit",
        })
        code_result = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / ".cursor" / "rules" / "evil.py"),
            "tool_name": "Edit",
        })

        assert mdc_result["valid"] is True
        assert mdc_result["reason"] == "routing_exception_filetype"
        assert legacy_result["valid"] is True
        assert legacy_result["reason"] == "routing_exception_path"
        assert code_result["valid"] is False
        assert "Call route_task or decompose_task first" in code_result["reason"]


def test_validate_routing_guard_denies_unknown_filetype_without_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db))
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        result = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "example.unlisted-language"),
            "tool_name": "Edit",
        })

        assert result["valid"] is False
        assert "Call route_task or decompose_task first" in result["reason"]


def test_validate_routing_guard_allows_medium_direct_edit_in_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.chdir(ROOT)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="medium",
                score=0.61,
                reason="medium-tier task",
                agents=2,
                override=False,
            )
        )
        registry = SelectionRegistry(provider="Claude Code", model="claude-sonnet-4.6")

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, router=router))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        mcp_server.handle_route_task({"task": "implement changes in shared/db.py", "cwd": str(ROOT)})

        allowed = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "shared" / "db.py"),
            "tool_name": "Edit",
        })
        denied = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "mcp_server.py"),
            "tool_name": "Edit",
        })

        assert allowed["valid"] is True
        assert denied["valid"] is False
        assert "outside the latest routed file scope" in denied["reason"]


def test_validate_routing_guard_allows_system_managed_markdown_without_guard(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg, db = _prepare_db(td)
            monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db))

            result = mcp_server.handle_validate_routing_guard({
                "cwd": str(ROOT),
                "target_file": str(Path("~").expanduser() / ".claude" / "settings.md"),
                "tool_name": "Write",
            })

            assert result["valid"] is True
            assert result["mode"] == "exempt"
            assert result["reason"] == "exempt_system_path"


def test_validate_routing_guard_system_path_requires_segment_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db))

        result = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(Path("~").expanduser() / ".claudemanager" / "settings.py"),
            "tool_name": "Write",
        })

        assert result["valid"] is False
        assert "Call route_task or decompose_task first" in result["reason"]


def test_validate_routing_guard_resolves_relative_target_against_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db))

        result = mcp_server.handle_validate_routing_guard({
            "cwd": str(Path("~").expanduser()),
            "target_file": ".claude/settings.py",
            "tool_name": "Write",
        })

        assert result["valid"] is True
        assert result["reason"] == "exempt_system_path"


def test_validate_routing_guard_uses_runtime_home_for_system_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        home = Path(td) / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db))

        result = mcp_server.handle_validate_routing_guard({
            "cwd": str(home),
            "target_file": ".claude/settings.py",
            "tool_name": "Write",
        })

        assert result["valid"] is True
        assert result["reason"] == "exempt_system_path"


def test_plan_task_issues_routed_plan_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.chdir(ROOT)
        registry = SelectionRegistry(provider="Claude Code", model="claude-sonnet-4.6")
        fake_plan = SimpleNamespace(total_agents=2, waves=[[1]])
        planner = SimpleNamespace(
            plan=lambda _task: fake_plan,
            plan_to_dict=lambda _plan: {
                "analysis": "test",
                "subtasks": [{"id": 1, "description": "Update shared/db.py", "tier": "medium", "model": "claude-sonnet-4.6", "depends_on": []}],
                "waves": [[1]],
                "strategy": "parallel",
                "total_agents": 2,
            },
        )

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, planner=planner))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        result = mcp_server.handle_plan_task({
            "task": "implement changes across shared/db.py and mcp_server.py",
            "cwd": str(ROOT),
        })
        validated = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "shared" / "db.py"),
            "tool_name": "Edit",
        })

        assert result["routing_guard"]["mode"] == ROUTING_GUARD_MODE_ROUTED_PLAN
        assert validated["valid"] is False
        assert "multi-file routed plan" in validated["reason"]


def test_route_task_uses_explicit_cwd_for_guard_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        monkeypatch.chdir(td)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="medium",
                score=0.61,
                reason="medium-tier task",
                agents=2,
                override=False,
            )
        )
        registry = SelectionRegistry(provider="Claude Code", model="claude-sonnet-4.6")

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, router=router))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        mcp_server.handle_route_task({"task": "implement changes in shared/db.py", "cwd": str(ROOT)})
        stored = db.routing_guard_get(caller="claude-code", cwd=str(ROOT))
        validated = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "shared" / "db.py"),
            "tool_name": "Edit",
        })

        assert stored is not None
        assert stored["cwd"] == str(ROOT)
        assert validated["valid"] is True


def test_validate_routing_guard_denies_target_outside_workspace_without_file_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="medium",
                score=0.61,
                reason="medium-tier task",
                agents=2,
                override=False,
            )
        )
        registry = SelectionRegistry(provider="Claude Code", model="claude-sonnet-4.6")

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, router=router))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")
        monkeypatch.setattr(
            db,
            "routing_guard_purge_expired",
            lambda: (_ for _ in ()).throw(AssertionError("validate should not purge on the hot path")),
        )

        mcp_server.handle_route_task({"task": "implement routing changes", "cwd": str(ROOT)})

        allowed = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "shared" / "db.py"),
            "tool_name": "Edit",
        })
        denied = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT.parent / "outside.txt"),
            "tool_name": "Edit",
        })

        assert allowed["valid"] is True
        assert denied["valid"] is False
        assert "outside the latest routed workspace" in denied["reason"]


def test_validate_routing_guard_denies_missing_target_file(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="medium",
                score=0.61,
                reason="medium-tier task",
                agents=2,
                override=False,
            )
        )
        registry = SelectionRegistry(provider="Claude Code", model="claude-sonnet-4.6")

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, router=router))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        mcp_server.handle_route_task({"task": "implement routing changes", "cwd": str(ROOT)})
        denied = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "tool_name": "Edit",
        })

        assert denied["valid"] is False
        assert "target_file is required" in denied["reason"]


def test_route_task_extracts_extensionless_and_dotfile_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="medium",
                score=0.61,
                reason="medium-tier task",
                agents=2,
                override=False,
            )
        )
        registry = SelectionRegistry(provider="Claude Code", model="claude-sonnet-4.6")

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, router=router))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        dockerfile_result = mcp_server.handle_route_task({"task": "update Dockerfile", "cwd": str(ROOT)})
        dotfile_result = mcp_server.handle_route_task({"task": "update .gitignore", "cwd": str(ROOT)})

        assert dockerfile_result["routing_guard"]["file_hints"] == [str(ROOT / "Dockerfile")]
        assert dotfile_result["routing_guard"]["file_hints"] == [str(ROOT / ".gitignore")]


def test_route_task_tolerates_guard_store_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="medium",
                score=0.61,
                reason="medium-tier task",
                agents=2,
                override=False,
            )
        )
        registry = SelectionRegistry(provider="Claude Code", model="claude-sonnet-4.6")

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, router=router))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")
        db.routing_guard_put(
            caller="claude-code",
            cwd=str(ROOT),
            mode=ROUTING_GUARD_MODE_DIRECT,
            source_tool="route_task",
            task_text="stale guard",
        )
        monkeypatch.setattr(
            db,
            "routing_guard_put",
            lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("db locked")),
        )

        result = mcp_server.handle_route_task({"task": "implement routing changes", "cwd": str(ROOT)})

        assert "routing_guard" not in result
        assert db.routing_guard_get(caller="claude-code", cwd=str(ROOT)) is None


def test_validate_routing_guard_denies_invalid_guard_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db))
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")
        monkeypatch.setattr(db, "routing_guard_get", lambda **_kwargs: "broken")

        denied = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "shared" / "db.py"),
            "tool_name": "Edit",
        })

        assert denied["valid"] is False
        assert "Routing guard state is invalid" in denied["reason"]


def test_validate_routing_guard_denies_lookup_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db))
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")
        monkeypatch.setattr(
            db,
            "routing_guard_get",
            lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("db locked")),
        )

        denied = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "shared" / "db.py"),
            "tool_name": "Edit",
        })

        assert denied["valid"] is False
        assert "Routing guard lookup failed" in denied["reason"]


def test_validate_routing_guard_denies_invalid_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db))
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")
        monkeypatch.setattr(
            db,
            "routing_guard_get",
            lambda **_kwargs: {"mode": "unexpected", "file_hints": [], "cwd": str(ROOT)},
        )

        denied = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "shared" / "db.py"),
            "tool_name": "Edit",
        })

        assert denied["valid"] is False
        assert "Routing guard mode is invalid" in denied["reason"]


def test_install_writes_claude_routing_hook() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        bin_dir = Path(td) / "bin"
        home.mkdir()
        bin_dir.mkdir()

        for name in ("gh", "claude"):
            script = bin_dir / name
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)

        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        install_dir = home / ".local" / "lib" / "threnody"
        install_dir.mkdir(parents=True)
        (install_dir / "config.yaml").write_text("routing_policy:\n  mode: default\n", encoding="utf-8")

        result = subprocess.run(
            ["bash", "install.sh"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stderr

        settings_path = home / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        pre_tool_use = settings["hooks"]["PreToolUse"]
        managed = [
            item for item in pre_tool_use
            if item.get("matcher") == "Edit|Write"
        ]

        assert managed
        hook = managed[0]["hooks"][0]
        assert hook["type"] == "mcp_tool"
        assert hook["server"] == "Threnody"
        assert hook["tool"] == "validate_routing_guard"


def test_install_keeps_claude_routing_hook_on_policy_parse_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        bin_dir = Path(td) / "bin"
        home.mkdir()
        bin_dir.mkdir()

        for name in ("gh", "claude"):
            script = bin_dir / name
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)

        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        install_dir = home / ".local" / "lib" / "threnody"
        install_dir.mkdir(parents=True)
        (install_dir / "config.yaml").write_text("routing_policy: [\n", encoding="utf-8")

        result = subprocess.run(
            ["bash", "install.sh"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stderr

        settings_path = home / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        managed = [
            item for item in settings["hooks"]["PreToolUse"]
            if item.get("matcher") == "Edit|Write"
        ]
        assert managed


def test_install_removes_claude_routing_hook_when_policy_disables_it() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        bin_dir = Path(td) / "bin"
        settings_dir = home / ".claude"
        install_dir = home / ".local" / "lib" / "threnody"
        home.mkdir()
        bin_dir.mkdir()
        settings_dir.mkdir(parents=True)
        install_dir.mkdir(parents=True)

        for name in ("gh", "claude"):
            script = bin_dir / name
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)

        (install_dir / "config.yaml").write_text("routing_policy:\n  mode: advisory\n", encoding="utf-8")
        settings_path = settings_dir / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Edit|Write",
                                "hooks": [
                                    {
                                        "type": "mcp_tool",
                                        "server": "Threnody",
                                        "tool": "validate_routing_guard",
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"

        result = subprocess.run(
            ["bash", "install.sh"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stderr

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "hooks" not in settings


def test_soft_hint_when_not_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    """execute_subtask_guard_strict=False returns valid:True with hint instead of hard deny."""
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        cfg.execute_subtask_guard_strict = False
        monkeypatch.chdir(ROOT)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="low",
                score=0.21,
                reason="low-tier task",
                agents=2,
                override=False,
            )
        )
        registry = SelectionRegistry()

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, router=router))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        mcp_server.handle_route_task({"task": "fix shared/db.py", "cwd": str(ROOT)})
        result = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "shared" / "db.py"),
            "tool_name": "Edit",
        })

        assert result["valid"] is True
        assert result["reason"] == "execute_subtask_hint"
        assert "hint" in result


def test_hard_deny_unchanged_when_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default execute_subtask_guard_strict=True still hard-denies Edit/Write."""
    with tempfile.TemporaryDirectory() as td:
        cfg, db = _prepare_db(td)
        # default: execute_subtask_guard_strict = True
        assert cfg.execute_subtask_guard_strict is True
        monkeypatch.chdir(ROOT)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="low",
                score=0.21,
                reason="low-tier task",
                agents=2,
                override=False,
            )
        )
        registry = SelectionRegistry()

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: _stub_init(cfg, db, router=router))
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

        mcp_server.handle_route_task({"task": "fix shared/db.py", "cwd": str(ROOT)})
        result = mcp_server.handle_validate_routing_guard({
            "cwd": str(ROOT),
            "target_file": str(ROOT / "shared" / "db.py"),
            "tool_name": "Edit",
        })

        assert result["valid"] is False
        assert "execute_subtask" in result["reason"]
