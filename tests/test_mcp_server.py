#!/usr/bin/env python3
"""Tests for mcp_server write safety, routing metadata, and execution flow."""
from __future__ import annotations

import io
import json
import subprocess
import threading
from pathlib import Path
import tempfile
from types import MappingProxyType, SimpleNamespace

import mcp_server
import pytest
from shared.config import RoutingPreference, TGsConfig
from shared.db import Database


class StubRegistry:
    def select_provider_for_tier(
        self,
        tier: str,
        *,
        prefer_free: bool = True,
        caller: str | None = None,
        code_only: bool = False,
    ) -> dict[str, object]:
        is_free = tier == "low"
        return {
            "provider": "GitHub Copilot",
            "provider_id": "github-copilot",
            "model": "gpt-5-mini" if tier == "low" else "gpt-5.4",
            "tier": tier,
            "is_free": is_free,
            "billing_tier": "free" if is_free else "subscription",
            "provider_cost_hint": "free" if is_free else "included in subscription/quota",
            "cost_rank": 0 if is_free else 2,
            "billing_source": "user_override" if is_free else "provider_default",
            "excluded_providers": [],
        }

    def execute_cheapest(self, **_kwargs: object) -> dict[str, object]:
        return {
            "result": "def generated_preview():\n    return 'hello from preview'\n",
            "provider": "GitHub Copilot",
            "provider_id": "github-copilot",
            "model": "gpt-5-mini",
            "tier": "low",
            "is_free": True,
            "billing_tier": "free",
            "provider_cost_hint": "free",
            "cost_rank": 0,
            "billing_source": "user_override",
            "fallback_used": False,
        }

    def to_dict(self) -> dict[str, object]:
        return {"providers": []}


class WritingRegistry(StubRegistry):
    def __init__(self, target: Path, content: str) -> None:
        self.target = target
        self.content = content

    def execute_cheapest(self, **_kwargs: object) -> dict[str, object]:
        self.target.write_text(self.content, encoding="utf-8")
        return {
            "result": "provider wrote files directly",
            "provider": "Gemini CLI",
            "provider_id": "gemini",
            "model": "gemini-2.5-pro",
            "tier": "medium",
            "is_free": False,
            "billing_tier": "subscription",
            "provider_cost_hint": "included in subscription/quota",
            "cost_rank": 1,
            "billing_source": "provider_default",
            "fallback_used": False,
        }


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class _TTYInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_resolve_caller_maps_new_host_client_names(monkeypatch) -> None:
    monkeypatch.delenv("COPILOT_CLI", raising=False)
    monkeypatch.delenv("COPILOT_RUN_APP", raising=False)
    monkeypatch.delenv("CLAUDE_CODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION", raising=False)
    monkeypatch.delenv("OPENCODE_HOST", raising=False)
    monkeypatch.delenv("OPENCODE_SESSION", raising=False)
    monkeypatch.setattr(mcp_server, "_client_name", "Codex CLI")
    assert mcp_server._resolve_caller() == "codex"

    monkeypatch.setattr(mcp_server, "_client_name", "Cursor Agent")
    assert mcp_server._resolve_caller() == "cursor"

    monkeypatch.setattr(mcp_server, "_client_name", "JetBrains Junie")
    assert mcp_server._resolve_caller() == "junie"

    monkeypatch.setattr(mcp_server, "_client_name", "OpenCode")
    assert mcp_server._resolve_caller() == "opencode"


def test_resolve_caller_prefers_env_marker_over_client_name(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "_client_name", "Claude Code")
    monkeypatch.setenv("COPILOT_CLI", "1")

    assert mcp_server._resolve_caller() == "github-copilot"


def test_check_providers_does_not_require_full_init(monkeypatch) -> None:
    class CompactRegistry:
        def to_compact_dict(self) -> dict[str, object]:
            return {"providers": [{"id": "github-copilot"}], "total": 1}

    seen_config: list[dict[str, object] | None] = []

    def fake_get_registry(overrides=None, *_args, **_kwargs):
        seen_config.append(overrides)
        return CompactRegistry()

    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (_ for _ in ()).throw(AssertionError("_ensure_init should not be called")),
    )
    monkeypatch.setattr(mcp_server, "_config", TGsConfig(provider_cost_overrides={"stale": {}}))
    monkeypatch.setattr(mcp_server, "_registry_override_signature", None)
    monkeypatch.setattr(mcp_server, "_registry_override_cache", None)
    monkeypatch.setattr(mcp_server.TGsConfig, "from_yaml", lambda: TGsConfig(provider_cost_overrides={"fresh": {}}))
    monkeypatch.setattr(mcp_server, "get_registry", fake_get_registry)

    result = mcp_server.handle_check_providers({})

    assert result == {"providers": [{"id": "github-copilot"}], "total": 1}
    assert seen_config == [{"provider_cost_overrides": {"fresh": {}}}]


def test_check_providers_falls_back_to_cached_config_on_reload_error(monkeypatch) -> None:
    class CompactRegistry:
        def to_compact_dict(self) -> dict[str, object]:
            return {"providers": [{"id": "github-copilot"}], "total": 1}

    seen_config: list[dict[str, object] | None] = []
    reload_attempts = 0

    def fake_get_registry(overrides=None, *_args, **_kwargs):
        seen_config.append(overrides)
        return CompactRegistry()

    def broken_from_yaml():
        nonlocal reload_attempts
        reload_attempts += 1
        raise RuntimeError("broken config")

    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (_ for _ in ()).throw(AssertionError("_ensure_init should not be called")),
    )
    monkeypatch.setattr(mcp_server, "_config", TGsConfig(provider_cost_overrides={"cached": {}}))
    monkeypatch.setattr(mcp_server, "_registry_override_signature", None)
    monkeypatch.setattr(mcp_server, "_registry_override_cache", None)
    monkeypatch.setattr(mcp_server, "_config_file_signature", lambda: (True, 1, 1))
    monkeypatch.setattr(mcp_server.TGsConfig, "from_yaml", broken_from_yaml)
    monkeypatch.setattr(mcp_server, "get_registry", fake_get_registry)

    result = mcp_server.handle_check_providers({})
    second_result = mcp_server.handle_check_providers({})

    assert result == {"providers": [{"id": "github-copilot"}], "total": 1}
    assert second_result == result
    assert seen_config == [
        {"provider_cost_overrides": {"cached": {}}},
        {"provider_cost_overrides": {"cached": {}}},
    ]
    assert reload_attempts == 1


def test_check_providers_forwards_preferred_routing(monkeypatch) -> None:
    class CompactRegistry:
        def to_compact_dict(self) -> dict[str, object]:
            return {"providers": [{"id": "claude-code"}], "total": 1}

    seen_config: list[dict[str, object] | None] = []

    def fake_get_registry(overrides=None, *_args, **_kwargs):
        seen_config.append(overrides)
        return CompactRegistry()

    preferred_routing = {
        "low": [
            {"provider": "Claude Code", "model": "haiku"},
        ],
    }

    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (_ for _ in ()).throw(AssertionError("_ensure_init should not be called")),
    )
    monkeypatch.setattr(
        mcp_server,
        "_config",
        TGsConfig(preferred_routing={
            "low": [RoutingPreference(provider="Claude Code", model="haiku")],
        }),
    )
    monkeypatch.setattr(mcp_server, "_registry_override_signature", None)
    monkeypatch.setattr(mcp_server, "_registry_override_cache", None)
    monkeypatch.setattr(
        mcp_server.TGsConfig,
        "from_yaml",
        lambda: TGsConfig(preferred_routing={
            "low": [RoutingPreference(provider="Claude Code", model="haiku")],
        }),
    )
    monkeypatch.setattr(mcp_server, "get_registry", fake_get_registry)

    result = mcp_server.handle_check_providers({})

    assert result == {"providers": [{"id": "claude-code"}], "total": 1}
    assert seen_config == [{"preferred_routing": preferred_routing}]


def test_ensure_init_does_not_start_background_services(monkeypatch, tmp_path: Path) -> None:
    cfg = TGsConfig(db_path=tmp_path / "idle.db")
    started: list[str] = []

    monkeypatch.setattr(mcp_server, "_config", None)
    monkeypatch.setattr(mcp_server, "_db", None)
    monkeypatch.setattr(mcp_server, "_router", None)
    monkeypatch.setattr(mcp_server, "_planner", None)
    monkeypatch.setattr(mcp_server, "_orchestrator", None)
    monkeypatch.setattr(mcp_server, "_model_catalog", None)
    monkeypatch.setattr(mcp_server, "_bg_loop", None)
    monkeypatch.setattr(mcp_server, "_bg_loop_thread", None)
    monkeypatch.setattr(mcp_server, "_catalog_refresh_future", None)
    monkeypatch.setattr(mcp_server.TGsConfig, "from_yaml", lambda: cfg)
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(mcp_server, "_ensure_bg_loop", lambda: started.append("bg-loop"))
    monkeypatch.setattr(
        mcp_server,
        "_schedule_model_catalog_refresh",
        lambda: started.append("catalog-refresh"),
    )

    config, db, router, planner, orchestrator = mcp_server._ensure_init()

    assert config is cfg
    assert db is not None
    assert router is not None
    assert planner is not None
    assert orchestrator is not None
    assert orchestrator._project_root == str(tmp_path.resolve())
    assert started == []


def test_registry_access_schedules_model_catalog_refresh(monkeypatch) -> None:
    started: list[str] = []

    monkeypatch.setattr(mcp_server, "_model_catalog", object())
    monkeypatch.setattr(
        mcp_server,
        "_schedule_model_catalog_refresh",
        lambda: started.append("catalog-refresh"),
    )
    monkeypatch.setattr(mcp_server, "get_registry", lambda *_args, **_kwargs: {"ok": True})

    result = mcp_server._get_registry_with_config()

    assert result == {"ok": True}
    assert started == ["catalog-refresh"]


def test_execute_subtask_rejects_outside_workspace_target(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        repo_root = Path(td) / "repo"
        repo_root.mkdir()
        outside_path = Path(td) / "outside" / "generated.py"
        db_path = Path(td) / "preview.db"
        cfg = TGsConfig(db_path=db_path, write_safety_trusted_bases=[])
        db = Database(db_path=db_path)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write a one-line Python file",
            "target_file": str(outside_path),
        })

        assert result["error"] == "PathTraversalRejected"
        assert result["requested_path"] == str(outside_path.resolve())
        assert outside_path.exists() is False

        with db.conn() as conn:
            remaining_previews = conn.execute(
                "SELECT COUNT(*) FROM preview_records",
            ).fetchone()
            audit_rows = conn.execute(
                "SELECT outcome FROM write_audit ORDER BY id",
            ).fetchall()

        assert remaining_previews == (0,)
        assert [row[0] for row in audit_rows] == []


def test_apply_preview_hashes_stored_token_and_redacts_not_found(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "preview.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        requested_path = Path(td).resolve() / "generated.py"
        preview_token = "secret-preview-token"

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(
            mcp_server,
            "_active_workspace_root",
            lambda: Path(td).resolve(),
        )

        mcp_server._store_preview_record(
            db,
            preview_token=preview_token,
            requested_path=requested_path,
            content="print('preview')\n",
            caller="test",
        )

        with db.conn() as conn:
            stored = conn.execute(
                "SELECT preview_token FROM preview_records",
            ).fetchone()

        assert stored is not None
        assert stored[0] == mcp_server._write_preview_token_ref(preview_token)
        assert stored[0] != preview_token

        missing = mcp_server.apply_preview("missing-preview-token", approve=True)
        assert missing == {
            "error": "PreviewNotFound",
            "details": "No pending preview for the supplied preview_token",
        }

        approved = mcp_server.apply_preview(preview_token, approve=True)
        assert approved["approved"] is True

        with db.conn() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM preview_records",
            ).fetchone()
            audit_row = conn.execute(
                """
                SELECT preview_token, outcome
                FROM write_audit
                ORDER BY id DESC
                LIMIT 1
                """,
            ).fetchone()

        assert remaining == (0,)
        assert audit_row == (mcp_server._write_preview_token_ref(preview_token), "approved")


def test_ensure_init_thread_safe(monkeypatch, tmp_path: Path) -> None:
    cfg = TGsConfig(db_path=tmp_path / "thread-safe.db")
    call_count = 0
    barrier = threading.Barrier(8)
    results: list[tuple[object, object, object, object, object]] = []
    errors: list[BaseException] = []

    def fake_from_yaml():
        nonlocal call_count
        call_count += 1
        return cfg

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(mcp_server._ensure_init())
        except BaseException as exc:  # pragma: no cover - test captures failures explicitly
            errors.append(exc)

    monkeypatch.setattr(mcp_server, "_config", None)
    monkeypatch.setattr(mcp_server, "_db", None)
    monkeypatch.setattr(mcp_server, "_router", None)
    monkeypatch.setattr(mcp_server, "_planner", None)
    monkeypatch.setattr(mcp_server, "_orchestrator", None)
    monkeypatch.setattr(mcp_server, "_model_catalog", None)
    monkeypatch.setattr(mcp_server.TGsConfig, "from_yaml", fake_from_yaml)
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert len(results) == 8
    assert call_count == 1
    first = results[0]
    for result in results[1:]:
        assert result[0] is first[0]
        assert result[1] is first[1]
        assert result[2] is first[2]
        assert result[3] is first[3]
        assert result[4] is first[4]


def test_ensure_init_exception_resets_all_globals(monkeypatch, tmp_path: Path) -> None:
    cfg = TGsConfig(db_path=tmp_path / "init-failure.db")

    class ExplodingOrchestrator:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(mcp_server, "_config", None)
    monkeypatch.setattr(mcp_server, "_db", None)
    monkeypatch.setattr(mcp_server, "_router", None)
    monkeypatch.setattr(mcp_server, "_planner", None)
    monkeypatch.setattr(mcp_server, "_orchestrator", None)
    monkeypatch.setattr(mcp_server, "_model_catalog", None)
    monkeypatch.setattr(mcp_server.TGsConfig, "from_yaml", lambda: cfg)
    monkeypatch.setattr(mcp_server, "Orchestrator", ExplodingOrchestrator)
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())

    with pytest.raises(RuntimeError, match="boom"):
        mcp_server._ensure_init()

    assert mcp_server._config is None
    assert mcp_server._db is None
    assert mcp_server._router is None
    assert mcp_server._planner is None
    assert mcp_server._orchestrator is None
    assert mcp_server._model_catalog is None


def test_apply_preview_concurrent_race_only_one_write(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "preview-race.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        requested_path = Path(td).resolve() / "generated.py"
        preview_token = "race-preview-token"
        barrier = threading.Barrier(2)
        write_calls = 0
        write_lock = threading.Lock()
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def fake_write(
            _db: Database,
            *,
            requested_path: Path,
            content: str,
            caller: str | None,
            outcome: str,
            preview_token: str | None = None,
        ) -> dict[str, object]:
            nonlocal write_calls
            with write_lock:
                write_calls += 1
            requested_path.write_text(content, encoding="utf-8")
            return {
                "file_written": str(requested_path),
                "lines_written": content.count("\n") + 1,
            }

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(mcp_server.apply_preview(preview_token, approve=True))
            except BaseException as exc:  # pragma: no cover - test captures failures explicitly
                errors.append(exc)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(
            mcp_server,
            "_active_workspace_root",
            lambda: Path(td).resolve(),
        )
        monkeypatch.setattr(mcp_server, "_write_file_with_audit", fake_write)

        mcp_server._store_preview_record(
            db,
            preview_token=preview_token,
            requested_path=requested_path,
            content="print('preview')\n",
            caller="test",
        )

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert not errors
        assert len(results) == 2
        assert write_calls == 1
        assert requested_path.read_text(encoding="utf-8") == "print('preview')\n"
        approved = [result for result in results if result.get("approved") is True]
        rejected = [result for result in results if result.get("error") == "PreviewNotFound"]
        assert len(approved) == 1
        assert len(rejected) == 1

        with db.conn() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM preview_records WHERE preview_token = ?",
                (mcp_server._write_preview_token_ref(preview_token),),
            ).fetchone()

        assert remaining == (0,)


def test_execute_subtask_rejects_outside_workspace_before_provider_call(monkeypatch) -> None:
    class RecordingRegistry(StubRegistry):
        def __init__(self) -> None:
            self.execute_calls = 0

        def execute_cheapest(self, **_kwargs: object) -> dict[str, object]:
            self.execute_calls += 1
            return super().execute_cheapest(**_kwargs)

    with tempfile.TemporaryDirectory() as td:
        repo_root = Path(td) / "repo"
        repo_root.mkdir()
        outside_path = Path(td) / "outside" / "generated.py"
        db_path = Path(td) / "preview.db"
        cfg = TGsConfig(db_path=db_path, write_safety_trusted_bases=[])
        db = Database(db_path=db_path)
        registry = RecordingRegistry()

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write a one-line Python file",
            "target_file": str(outside_path),
        })

        assert result["error"] == "PathTraversalRejected"
        assert registry.execute_calls == 0


def test_execute_subtask_rejects_broad_workspace_override(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        outside_path = Path(td) / "outside" / "generated.py"
        db_path = Path(td) / "preview.db"
        cfg = TGsConfig(db_path=db_path, write_safety_trusted_bases=[])
        db = Database(db_path=db_path)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", "/")
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write a one-line Python file",
            "target_file": str(outside_path),
        })

        assert result["error"] == "PathTraversalRejected"
        assert outside_path.exists() is False


def test_execute_subtask_allows_workspace_target_with_workspace_override(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        repo_root = Path(td) / "repo"
        repo_root.mkdir()
        target_path = repo_root / "generated.py"
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write a one-line Python file",
            "target_file": str(target_path),
        })

        assert result["file_written"] == str(target_path.resolve())
        assert target_path.exists() is True


def test_execute_subtask_write_error_marks_failed(monkeypatch) -> None:
    def failing_write(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError("disk full")

    with tempfile.TemporaryDirectory() as td:
        repo_root = Path(td) / "repo"
        repo_root.mkdir()
        target_path = repo_root / "generated.py"
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(db_path=db_path, write_safety_trusted_bases=[])
        db = Database(db_path=db_path)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubRegistry())
        monkeypatch.setattr(mcp_server, "_write_file_with_audit", failing_write)

        with mcp_server._subtasks_lock:
            initial_history_len = len(mcp_server._subtask_history)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write a one-line Python file",
            "target_file": str(target_path),
        })

        assert result["error"] == "WriteError"
        with mcp_server._subtasks_lock:
            assert mcp_server._subtask_history[-1]["status"] == "failed"
            del mcp_server._subtask_history[initial_history_len:]


def test_execute_subtask_rejects_invalid_timeout(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "preview.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write a one-line Python file",
            "timeout": "slow",
        })

        assert result["error"] == "InvalidTimeout"


def test_execute_subtask_uses_snapshot_diff_for_non_native_agents(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        repo_root = Path(td) / "repo"
        repo_root.mkdir()
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        seed = repo_root / "seed.py"
        seed.write_text("print('seed')\n", encoding="utf-8")
        subprocess.run(["git", "add", "seed.py"], cwd=repo_root, check=True, capture_output=True, text=True)

        generated = repo_root / "generated.py"
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        registry = WritingRegistry(generated, "print('generated')\n")
        printed: list[dict[str, object]] = []

        monkeypatch.chdir(repo_root)
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: registry)
        monkeypatch.setattr(
            mcp_server,
            "_print_diff_to_terminal",
            lambda **kwargs: printed.append(kwargs),
        )

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write files directly",
            "tier": "medium",
        })

        assert result["change_type"] == "created"
        assert result["diff"]
        assert result["all_diffs"]
        assert printed and printed[0]["provider"] == "Gemini CLI"
        assert any(Path(item["path"]).name == "generated.py" for item in result["all_diffs"])


def test_execute_subtask_uses_validated_target_for_snapshot_diff_path(monkeypatch) -> None:
    class MaliciousPathRegistry(StubRegistry):
        def execute_cheapest(self, **_kwargs: object) -> dict[str, object]:
            return {
                **super().execute_cheapest(**_kwargs),
                "file_written": "/tmp/evil.py",
            }

    def partial_write(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"lines_written": 1}

    with tempfile.TemporaryDirectory() as td:
        repo_root = Path(td) / "repo"
        repo_root.mkdir()
        target_path = repo_root / "generated.py"
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: MaliciousPathRegistry())
        monkeypatch.setattr(mcp_server, "_write_file_with_audit", partial_write)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write a one-line Python file",
            "target_file": str(target_path),
        })

        assert result["all_diffs"][0]["path"] == str(target_path.resolve())
        assert result["all_diffs"][0]["path"] != "/tmp/evil.py"


def test_print_diff_to_terminal_writes_colored_output(monkeypatch) -> None:
    stderr = _TTYBuffer()
    monkeypatch.setattr(mcp_server.sys, "stderr", stderr)

    mcp_server._print_diff_to_terminal(
        all_diffs=[{
            "path": "src/example.py",
            "change_type": "modified",
            "lines_added": 1,
            "lines_removed": 1,
            "diff": "--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1 @@\n-print('old')\n+\x1b]52;badprint('new')",
        }],
        agent_label="src/example.py",
        tier="medium",
        provider="GitHub Copilot",
    )

    output = stderr.getvalue()
    assert "src/example.py" in output
    assert "via GitHub Copilot" in output
    assert "+]52;badprint('new')" in output
    assert "-print('old')" in output
    assert mcp_server._ANSI_GREEN in output
    assert "\x1b]52;" not in output


def test_print_diff_to_terminal_strips_bidi_controls(monkeypatch) -> None:
    stderr = _TTYBuffer()
    monkeypatch.setattr(mcp_server.sys, "stderr", stderr)

    mcp_server._print_diff_to_terminal(
        all_diffs=[{
            "path": "src/example.py",
            "change_type": "modified",
            "lines_added": 1,
            "lines_removed": 0,
            "diff": "--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1 @@\n+\u202Eprint('new')",
        }],
        agent_label="src/\u202Eexample.py",
        tier="medium",
        provider="GitHub Copilot",
    )

    output = stderr.getvalue()
    assert "\u202E" not in output
    assert "src/example.py" in output


def test_approval_gate_rejects_no(monkeypatch) -> None:
    stdin = _TTYInput("n\n")
    stderr = _TTYBuffer()
    snapshot = mcp_server.FileSnapshot(str(Path.cwd()))
    monkeypatch.setattr(mcp_server.sys, "stdin", stdin)
    monkeypatch.setattr(mcp_server.sys, "stderr", stderr)
    monkeypatch.setattr(mcp_server.select, "select", lambda read, *_args: (read, [], []))

    approved = mcp_server._approval_gate(
        snapshot=snapshot,
        all_diffs=[{"change_type": "modified", "diff": "---\n+++\n"}],
        tier="medium",
        auto_approve_timeout=30,
    )

    assert approved is False
    assert "[Y/n]" in stderr.getvalue()


def test_approval_gate_auto_approves_on_timeout(monkeypatch) -> None:
    stdin = _TTYInput("")
    stderr = _TTYBuffer()
    snapshot = mcp_server.FileSnapshot(str(Path.cwd()))
    monkeypatch.setattr(mcp_server.sys, "stdin", stdin)
    monkeypatch.setattr(mcp_server.sys, "stderr", stderr)
    monkeypatch.setattr(mcp_server.select, "select", lambda *_args: ([], [], []))

    approved = mcp_server._approval_gate(
        snapshot=snapshot,
        all_diffs=[{"change_type": "modified", "diff": "---\n+++\n"}],
        tier="medium",
        auto_approve_timeout=3,
    )

    assert approved is True
    assert "auto-approved after 3s" in stderr.getvalue()


def test_approval_gate_manual_mode_uses_bounded_timeout(monkeypatch) -> None:
    stdin = _TTYInput("")
    stderr = _TTYBuffer()
    snapshot = mcp_server.FileSnapshot(str(Path.cwd()))
    seen_timeouts: list[int] = []

    def fake_select(_read, _write, _except, timeout):
        seen_timeouts.append(timeout)
        return ([], [], [])

    monkeypatch.setattr(mcp_server.sys, "stdin", stdin)
    monkeypatch.setattr(mcp_server.sys, "stderr", stderr)
    monkeypatch.setattr(mcp_server.select, "select", fake_select)

    approved = mcp_server._approval_gate(
        snapshot=snapshot,
        all_diffs=[{"change_type": "modified", "diff": "---\n+++\n"}],
        tier="medium",
        auto_approve_timeout=0,
    )

    assert approved is True
    assert seen_timeouts == [mcp_server._MANUAL_APPROVAL_TIMEOUT_SECONDS]
    assert f"auto-approved after {mcp_server._MANUAL_APPROVAL_TIMEOUT_SECONDS}s" in stderr.getvalue()


def test_execute_subtask_reverts_when_gate_denies(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        repo_root = Path(td) / "repo"
        repo_root.mkdir()
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(
            db_path=db_path,
            write_safety_trusted_bases=[repo_root],
            code_review=True,
            code_review_tier="all",
            auto_approve_timeout=0,
        )
        db = Database(db_path=db_path)
        target_path = repo_root / "generated.py"

        monkeypatch.chdir(repo_root)
        monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo_root))
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *_args, **_kwargs: StubRegistry())
        monkeypatch.setattr(mcp_server, "_print_diff_to_terminal", lambda **_kwargs: None)
        monkeypatch.setattr(mcp_server, "_approval_gate", lambda **_kwargs: False)
        monkeypatch.setattr(mcp_server.sys, "stdin", _TTYInput(""))
        monkeypatch.setattr(mcp_server.sys, "stderr", _TTYBuffer())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write a one-line Python file",
            "target_file": str(target_path),
        })

        assert result["status"] == "reverted"
        assert result["output"] == "Changes reverted by developer."
        assert not target_path.exists()


def test_handle_request_sanitizes_and_clears_progress_token(monkeypatch) -> None:
    captured_response: list[dict[str, object]] = []
    tool_name = "__test_progress_token__"
    original_handler = mcp_server.HANDLERS.get(tool_name)
    long_token = "x" * (mcp_server._MAX_PROGRESS_TOKEN_LENGTH + 1)

    def handler(_args: dict) -> dict[str, object]:
        return {"progress_token": getattr(mcp_server._request_context, "progress_token", None)}

    try:
        mcp_server.HANDLERS[tool_name] = handler
        monkeypatch.setattr(mcp_server, "send_response", lambda _req_id, payload: captured_response.append(payload))
        mcp_server.handle_request({
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {},
                "_meta": {"progressToken": long_token},
            },
        })
    finally:
        if original_handler is None:
            mcp_server.HANDLERS.pop(tool_name, None)
        else:
            mcp_server.HANDLERS[tool_name] = original_handler

    body = json.loads(captured_response[0]["content"][0]["text"])
    assert body["progress_token"] is None
    assert getattr(mcp_server._request_context, "progress_token", None) is None


def test_execute_subtask_rejects_empty_target_path(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write a one-line Python file",
            "target_file": "",
        })

        assert result["error"] == "InvalidTargetPath"


def test_handle_plan_task_cache_hit_includes_models(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "plan.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        db.cache_put(
            "plan this",
            json.dumps({
                "analysis": "cached",
                "subtasks": [
                    {"id": 1, "description": "one", "tier": "low", "depends_on": []},
                ],
                "waves": [[1]],
                "total_agents": 1,
                "strategy": "parallel",
            }),
            "planner",
        )
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubRegistry())
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

        result = mcp_server.handle_plan_task({"task": "plan this"})

        assert result["cache_hit"] is True
        assert "model" in result["subtasks"][0]
        assert result["subtasks"][0]["provider"] == "GitHub Copilot"
        assert result["subtasks"][0]["provider_id"] == "github-copilot"
        assert isinstance(result["subtasks"][0]["model"], str)
        assert len(result["subtasks"][0]["model"]) > 0
        assert result["subtasks"][0]["is_free"] is True
        assert result["subtasks"][0]["billing_tier"] == "free"
        assert result["subtasks"][0]["provider_cost_hint"] == "free"


def test_handle_plan_task_preserves_explicit_route_metadata(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "plan.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        plan = SimpleNamespace(total_agents=1, waves=[[1]])
        planner = SimpleNamespace(
            plan=lambda _task: plan,
            plan_to_dict=lambda _plan: {
                "analysis": "fresh",
                "subtasks": [{
                    "id": 1,
                    "description": "one",
                    "tier": "low",
                    "model": "claude-haiku-4.5",
                    "provider": "Claude Code",
                    "provider_id": "claude-code",
                    "depends_on": [],
                }],
                "waves": [[1]],
                "total_agents": 1,
                "strategy": "parallel",
            },
        )
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, planner, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubRegistry())
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

        result = mcp_server.handle_plan_task({"task": "plan this"})

        subtask = result["subtasks"][0]
        assert subtask["model"] == "claude-haiku-4.5"
        assert subtask["provider"] == "Claude Code"
        assert subtask["provider_id"] == "claude-code"


def test_handle_route_task_prefers_free_low_tier_metadata(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "route.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="low",
                score=0.21,
                reason="low-tier task",
                agents=2,
                override=False,
            )
        )
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, router, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubRegistry())
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

        result = mcp_server.handle_route_task({"task": "quick fix"})

        assert result["provider"] == "GitHub Copilot"
        assert result["model"] == "gpt-5-mini"
        assert result["is_free"] is True
        assert result["billing_tier"] == "free"
        assert result["provider_cost_hint"] == "free"
        assert result["cost_rank"] == 0
        assert result["billing_source"] == "user_override"


def test_execute_subtask_returns_billing_metadata(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: StubRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write one line of Python",
        })

        assert result["provider"] == "GitHub Copilot"
        assert result["provider_id"] == "github-copilot"
        assert result["model"] == "gpt-5-mini"
        assert result["is_free"] is True
        assert result["billing_tier"] == "free"
        assert result["provider_cost_hint"] == "free"
        assert result["cost_rank"] == 0
        assert result["billing_source"] == "user_override"


def test_handle_fleet_plan_agents_use_plan_route_metadata(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "fleet.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        plan = SimpleNamespace(waves=[[1]], total_agents=1)
        planner = SimpleNamespace(
            plan=lambda _task: plan,
            plan_to_dict=lambda _plan: {
                "analysis": "fresh",
                "subtasks": [{
                    "id": 1,
                    "description": "one",
                    "tier": "low",
                    "model": "claude-haiku-4.5",
                    "provider": "Claude Code",
                    "provider_id": "claude-code",
                    "depends_on": [],
                }],
                "waves": [[1]],
                "total_agents": 1,
                "strategy": "parallel",
            },
        )
        orchestrator = SimpleNamespace(
            to_fleet_waves=lambda _plan: [{
                "wave_number": 1,
                "parallel": False,
                "command": '/fleet "[low|gpt-5-mini] one"',
                "agents": [{
                    "tier": "low",
                    "model": "gpt-5-mini",
                    "prompt": "[low|gpt-5-mini] one",
                }],
            }],
        )
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, planner, orchestrator),
        )

        result = mcp_server.handle_fleet_plan({"task": "plan this"})

        agent = result["fleet_waves"][0]["agents"][0]
        assert agent["model"] == "claude-haiku-4.5"
        assert agent["provider"] == "Claude Code"
        assert agent["provider_id"] == "claude-code"
        assert agent["prompt"] == "[low|claude-haiku-4.5] one"
        assert "claude-haiku-4.5" in result["fleet_waves"][0]["command"]


def test_execute_subtask_returns_actual_execution_route(monkeypatch) -> None:
    class SwappingRegistry(StubRegistry):
        def select_provider_for_tier(self, *args, **kwargs) -> dict[str, object]:
            return {
                "provider": "Planned Provider",
                "provider_id": "planned-provider",
                "model": "planned-model",
                "tier": "low",
            }

        def execute_cheapest(self, **_kwargs: object) -> dict[str, object]:
            return {
                "result": "print('ok')\n",
                "provider": "Actual Provider",
                "provider_id": "actual-provider",
                "model": "actual-model",
                "tier": "low",
                "fallback_used": False,
            }

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: SwappingRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write one line of Python",
        })

        assert result["provider"] == "Actual Provider"
        assert result["provider_id"] == "actual-provider"
        assert result["model"] == "actual-model"


def test_execute_subtask_allows_partial_selection_when_execution_resolves_provider_id(monkeypatch) -> None:
    class PartialSelectionRegistry(StubRegistry):
        def select_provider_for_tier(self, *args, **kwargs) -> dict[str, object]:
            return {
                "provider": "GitHub Copilot",
                "model": "gpt-5-mini",
                "tier": "low",
            }

        def execute_cheapest(self, **_kwargs: object) -> dict[str, object]:
            return {
                "result": "print('ok')\n",
                "provider": "GitHub Copilot",
                "provider_id": "github-copilot",
                "model": "gpt-5-mini",
                "tier": "low",
                "fallback_used": False,
            }

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: PartialSelectionRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write one line of Python",
        })

        assert result["provider"] == "GitHub Copilot"
        assert result["provider_id"] == "github-copilot"
        assert result["model"] == "gpt-5-mini"


def test_execute_subtask_allows_provider_id_only_selection(monkeypatch) -> None:
    class ProviderIdOnlyRegistry(StubRegistry):
        def select_provider_for_tier(self, *args, **kwargs) -> dict[str, object]:
            return {
                "provider_id": "github-copilot",
                "model": "gpt-5-mini",
                "tier": "low",
            }

        def execute_cheapest(self, **_kwargs: object) -> dict[str, object]:
            return {
                "result": "print('ok')\n",
                "provider": "GitHub Copilot",
                "provider_id": "github-copilot",
                "model": "gpt-5-mini",
                "tier": "low",
                "fallback_used": False,
            }

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: ProviderIdOnlyRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write one line of Python",
        })

        assert result["provider"] == "GitHub Copilot"
        assert result["provider_id"] == "github-copilot"
        assert result["model"] == "gpt-5-mini"


def test_execute_subtask_returns_routing_error_when_unresolved(monkeypatch) -> None:
    class UnresolvableRegistry:
        def __init__(self) -> None:
            self.execute_calls = 0

        def select_provider_for_tier(self, *_args: object, **_kwargs: object) -> None:
            return None

        def execute_cheapest(self, **_kwargs: object) -> dict[str, object]:
            self.execute_calls += 1
            return {"result": "should not run"}

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        registry = UnresolvableRegistry()

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: registry)

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write one line of Python",
        })

        assert result["error"] == "RoutingUnavailable"
        assert "model and provider" in result["details"]
        assert registry.execute_calls == 0


def test_execute_subtask_provider_error_uses_compact_registry_details(monkeypatch) -> None:
    class FailingRegistry(StubRegistry):
        def execute_cheapest(self, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError("provider down")

        def to_compact_dict(self) -> dict[str, object]:
            return {"providers": [{"id": "github-copilot"}], "total": 1}

        def to_dict(self) -> dict[str, object]:
            return {"secret": "should-not-leak"}

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: FailingRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write one line of Python",
        })

        assert result["error"] == "ProviderError"
        assert result["details"] == "Provider execution failed. Check server logs for details."
        assert result["providers_checked"] == {"providers": [{"id": "github-copilot"}], "total": 1}
        assert "secret" not in json.dumps(result)


def test_execute_subtask_uses_detected_caller_for_routing(monkeypatch) -> None:
    class RecordingRegistry(StubRegistry):
        def __init__(self) -> None:
            self.selection_callers: list[object] = []
            self.execute_callers: list[object] = []

        def select_provider_for_tier(self, tier: str, **kwargs: object) -> dict[str, object]:
            self.selection_callers.append(kwargs.get("caller"))
            return super().select_provider_for_tier(tier, **kwargs)

        def execute_cheapest(self, **kwargs: object) -> dict[str, object]:
            self.execute_callers.append(kwargs.get("caller"))
            return super().execute_cheapest(**kwargs)

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "execute.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        registry = RecordingRegistry()

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write one line of Python",
            "provenance": {"caller_id": "spoofed-host", "depth": 1, "trace_id": "trace-1"},
        })

        assert result["provider"] == "GitHub Copilot"
        assert registry.selection_callers
        assert set(registry.selection_callers) == {"github-copilot"}
        assert registry.execute_callers == ["github-copilot"]


def test_handle_route_task_uses_code_only_hint_for_write_tasks(monkeypatch) -> None:
    class RecordingRegistry(StubRegistry):
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def select_provider_for_tier(
            self,
            tier: str,
            *,
            prefer_free: bool = True,
            caller: str | None = None,
            code_only: bool = False,
        ) -> dict[str, object]:
            self.calls.append({
                "tier": tier,
                "prefer_free": prefer_free,
                "caller": caller,
                "code_only": code_only,
            })
            return super().select_provider_for_tier(
                tier,
                prefer_free=prefer_free,
                caller=caller,
                code_only=code_only,
            )

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "route.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="low",
                score=0.21,
                reason="low-tier task",
                agents=2,
                override=False,
            )
        )
        registry = RecordingRegistry()
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, router, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

        result = mcp_server.handle_route_task({"task": "quick fix in parser.py"})

        assert result["provider"] == "GitHub Copilot"
        assert registry.calls == [{
            "tier": "low",
            "prefer_free": True,
            "caller": "github-copilot",
            "code_only": True,
        }]


def test_handle_route_task_avoids_code_only_for_plain_text_tasks(monkeypatch) -> None:
    class RecordingRegistry(StubRegistry):
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def select_provider_for_tier(
            self,
            tier: str,
            *,
            prefer_free: bool = True,
            caller: str | None = None,
            code_only: bool = False,
        ) -> dict[str, object]:
            self.calls.append({
                "tier": tier,
                "prefer_free": prefer_free,
                "caller": caller,
                "code_only": code_only,
            })
            return super().select_provider_for_tier(
                tier,
                prefer_free=prefer_free,
                caller=caller,
                code_only=code_only,
            )

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "route.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="low",
                score=0.21,
                reason="low-tier task",
                agents=2,
                override=False,
            )
        )
        registry = RecordingRegistry()
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, router, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: registry)
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

        mcp_server.handle_route_task({"task": "summarize parser.py routing heuristics"})

        assert registry.calls == [{
            "tier": "low",
            "prefer_free": True,
            "caller": "github-copilot",
            "code_only": False,
        }]


def test_handle_route_task_does_not_fabricate_provider_metadata(monkeypatch) -> None:
    class EmptyRegistry:
        def select_provider_for_tier(
            self,
            tier: str,
            *,
            prefer_free: bool = True,
            caller: str | None = None,
            code_only: bool = False,
        ) -> None:
            return None

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "route.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="low",
                score=0.21,
                reason="low-tier task",
                agents=2,
                override=False,
            )
        )
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, router, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: Path(td).resolve())
        monkeypatch.setattr(mcp_server, "get_registry", lambda: EmptyRegistry())
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

        result = mcp_server.handle_route_task({"task": "quick fix"})

        assert result["model"] == "gpt-5-mini"
        assert "provider" not in result
        assert "is_free" not in result


def test_select_provider_metadata_fallback_preserves_metered_billing() -> None:
    provider = SimpleNamespace(
        name="aider",
        display_name="Aider",
        tier_models={"low": "aider-mini"},
        cost_rank={"low": 1},
        billing_model="metered",
    )

    class LegacyRegistry:
        def get_providers_for_tier(self, _tier: str) -> list[SimpleNamespace]:
            return [provider]

    result = mcp_server._select_provider_metadata(
        LegacyRegistry(),
        "low",
        caller=None,
    )

    assert result is not None
    assert result["provider"] == "Aider"
    assert result["billing_tier"] == "metered"
    assert result["provider_cost_hint"] == "metered / per-token"


def test_select_provider_metadata_fallback_replaces_none_model() -> None:
    provider = SimpleNamespace(
        name="github-copilot",
        display_name="GitHub Copilot",
        tier_models={"low": None},
        cost_rank={"low": 0},
        billing_model="free",
    )

    class LegacyRegistry:
        def get_providers_for_tier(self, _tier: str) -> list[SimpleNamespace]:
            return [provider]

    result = mcp_server._select_provider_metadata(
        LegacyRegistry(),
        "low",
        caller=None,
    )

    assert result is not None
    assert result["model"] == "gpt-5-mini"


def test_select_provider_metadata_fallback_tolerates_missing_mapping_fields() -> None:
    provider = SimpleNamespace(
        name="github-copilot",
        display_name="GitHub Copilot",
        tier_models=None,
        cost_rank=None,
        billing_model="free",
    )

    class LegacyRegistry:
        def get_providers_for_tier(self, _tier: str) -> list[SimpleNamespace]:
            return [provider]

    result = mcp_server._select_provider_metadata(
        LegacyRegistry(),
        "low",
        caller=None,
    )

    assert result is not None
    assert result["provider"] == "GitHub Copilot"
    assert result["provider_id"] == "github-copilot"
    assert result["model"] == "gpt-5-mini"
    assert result["cost_rank"] is None


def test_select_provider_metadata_fallback_tolerates_provider_name_accessor_errors() -> None:
    class FragileProvider:
        display_name = "GitHub Copilot"
        tier_models = {"low": "gpt-5-mini"}
        cost_rank = {"low": 0}
        billing_model = "free"

        @property
        def name(self) -> str:
            raise RuntimeError("boom")

    class LegacyRegistry:
        def get_providers_for_tier(self, _tier: str) -> list[object]:
            return [FragileProvider()]

    result = mcp_server._select_provider_metadata(
        LegacyRegistry(),
        "low",
        caller="github-copilot",
    )

    assert result is not None
    assert result["provider"] == "GitHub Copilot"
    assert result["model"] == "gpt-5-mini"


def test_select_provider_metadata_excludes_caller_by_display_name() -> None:
    current_provider = SimpleNamespace(
        name="copilot-cli",
        display_name="github-copilot",
        tier_models={"low": "gpt-5-mini"},
        cost_rank={"low": 0},
        billing_model="free",
    )
    other_provider = SimpleNamespace(
        name="claude-code",
        display_name="Claude Code",
        tier_models={"low": "claude-haiku-4.5"},
        cost_rank={"low": 1},
        billing_model="subscription",
    )

    class LegacyRegistry:
        def get_providers_for_tier(self, _tier: str) -> list[SimpleNamespace]:
            return [current_provider, other_provider]

    result = mcp_server._select_provider_metadata(
        LegacyRegistry(),
        "low",
        caller="github-copilot",
    )

    assert result is not None
    assert result["provider_id"] == "claude-code"
    assert result["model"] == "claude-haiku-4.5"


def test_select_provider_metadata_handles_registry_without_effort_kwarg() -> None:
    class NoEffortRegistry:
        def select_provider_for_tier(
            self,
            tier: str,
            *,
            prefer_free: bool = True,
            caller: str | None = None,
            code_only: bool = False,
        ) -> dict[str, object]:
            return {
                "provider": "GitHub Copilot",
                "provider_id": "github-copilot",
                "model": "gpt-5-mini",
                "tier": tier,
            }

    result = mcp_server._select_provider_metadata(
        NoEffortRegistry(),
        "low",
        caller=None,
        effort="high",
    )

    assert result is not None
    assert result["provider_id"] == "github-copilot"
    assert result["model"] == "gpt-5-mini"
    assert result["effort"] == "high"
    assert result["effort_source"] == "explicit"


def test_select_provider_metadata_accepts_mapping_result() -> None:
    class MappingRegistry:
        def select_provider_for_tier(
            self,
            tier: str,
            *,
            prefer_free: bool = True,
            caller: str | None = None,
            code_only: bool = False,
        ) -> MappingProxyType:
            return MappingProxyType({
                "provider": "GitHub Copilot",
                "provider_id": "github-copilot",
                "model": "gpt-5-mini",
                "tier": tier,
            })

    result = mcp_server._select_provider_metadata(
        MappingRegistry(),
        "low",
        caller=None,
    )

    assert result is not None
    assert result["provider_id"] == "github-copilot"
    assert result["model"] == "gpt-5-mini"


def test_select_provider_metadata_falls_back_when_native_selection_is_incomplete() -> None:
    provider = SimpleNamespace(
        name="github-copilot",
        display_name="GitHub Copilot",
        tier_models={"low": "gpt-5-mini"},
        cost_rank={"low": 0},
        billing_model="free",
    )

    class HybridRegistry:
        def select_provider_for_tier(self, _tier: str, **_kwargs: object) -> dict[str, object]:
            return {
                "provider": "GitHub Copilot",
                "model": "gpt-5-mini",
                "tier": "low",
            }

        def get_providers_for_tier(self, _tier: str) -> list[SimpleNamespace]:
            return [provider]

    result = mcp_server._select_provider_metadata(
        HybridRegistry(),
        "low",
        caller=None,
    )

    assert result is not None
    assert result["provider"] == "GitHub Copilot"
    assert result["provider_id"] == "github-copilot"
    assert result["model"] == "gpt-5-mini"


def test_select_provider_metadata_resolves_native_placeholder_model_via_matching_provider() -> None:
    provider = SimpleNamespace(
        name="claude-code",
        display_name="Claude Code",
        tier_models={"low": "claude-haiku-4.5"},
        cost_rank={"low": 1},
        billing_model="subscription",
    )

    class HybridRegistry:
        def select_provider_for_tier(self, _tier: str, **_kwargs: object) -> dict[str, object]:
            return {
                "provider": "Claude Code",
                "provider_id": "claude-code",
                "model": "low",
                "tier": "low",
            }

        def get_providers_for_tier(self, _tier: str) -> list[SimpleNamespace]:
            return [provider]

    result = mcp_server._select_provider_metadata(
        HybridRegistry(),
        "low",
        caller=None,
    )

    assert result is not None
    assert result["provider"] == "Claude Code"
    assert result["provider_id"] == "claude-code"
    assert result["model"] == "claude-haiku-4.5"


def test_selection_with_effort_metadata_normalizes_route_fields() -> None:
    result = mcp_server._selection_with_effort_metadata(
        {
            "model": None,
            "provider": " GitHub Copilot ",
            "provider_id": object(),
        },
        tier="low",
    )

    assert result == {
        "provider": "GitHub Copilot",
    }


def test_normalize_provenance_accepts_mapping_input() -> None:
    provenance = mcp_server._normalize_provenance(
        MappingProxyType({"trace_id": "trace-1", "depth": 2, "caller_id": "parent"}),
        "github-copilot",
    )

    assert provenance == {
        "trace_id": "trace-1",
        "depth": 3,
        "caller_id": "parent",
    }


def test_handle_route_task_ignores_invalid_model_from_selection(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "route.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        router = SimpleNamespace(
            classify=lambda _task: SimpleNamespace(
                tier="low",
                score=0.21,
                reason="low-tier task",
                agents=1,
                override=False,
            )
        )

        class WeirdRegistry:
            def select_provider_for_tier(self, _tier: str, **_kwargs: object) -> dict[str, object]:
                return {
                    "provider": "GitHub Copilot",
                    "model": object(),
                    "tier": "low",
                }

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, router, None, None),
        )
        monkeypatch.setattr(mcp_server, "get_registry", lambda: WeirdRegistry())
        monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

        result = mcp_server.handle_route_task({"task": "quick fix"})

        assert result["model"] == "gpt-5-mini"
        assert "provider" not in result
        assert "provider_id" not in result


def test_attach_models_to_subtasks_preserves_explicit_route_metadata(monkeypatch) -> None:
    class RecordingRegistry:
        def select_provider_for_tier(
            self,
            tier: str,
            *,
            prefer_free: bool = True,
            caller: str | None = None,
            code_only: bool = False,
        ) -> dict[str, object]:
            return {
                "provider": "GitHub Copilot",
                "provider_id": "github-copilot",
                "model": "gpt-5-mini",
                "tier": tier,
            }

    monkeypatch.setattr(mcp_server, "get_registry", lambda: RecordingRegistry())
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

    result = mcp_server._attach_models_to_subtasks({
        "subtasks": [{
            "tier": "low",
            "description": "summarize parser.py routing heuristics",
            "model": "claude-haiku-4.5",
            "provider": "Claude Code",
            "provider_id": "claude-code",
        }]
    })

    subtask = result["subtasks"][0]
    assert subtask["model"] == "claude-haiku-4.5"
    assert subtask["provider"] == "Claude Code"
    assert subtask["provider_id"] == "claude-code"
    assert "is_free" not in subtask
    assert "billing_tier" not in subtask


def test_attach_models_to_subtasks_reuses_selection_for_identical_inputs(monkeypatch) -> None:
    class RecordingRegistry:
        def __init__(self) -> None:
            self.calls = 0

        def select_provider_for_tier(
            self,
            tier: str,
            *,
            prefer_free: bool = True,
            caller: str | None = None,
            code_only: bool = False,
        ) -> dict[str, object]:
            self.calls += 1
            return {
                "provider": "GitHub Copilot",
                "provider_id": "github-copilot",
                "model": "gpt-5-mini",
                "tier": tier,
            }

    registry = RecordingRegistry()
    monkeypatch.setattr(mcp_server, "get_registry", lambda: registry)
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

    result = mcp_server._attach_models_to_subtasks({
        "subtasks": [
            {"tier": "low", "description": "summarize parser routing"},
            {"tier": "low", "description": "summarize parser routing again"},
        ],
    })

    assert registry.calls == 1
    assert [subtask["model"] for subtask in result["subtasks"]] == ["gpt-5-mini", "gpt-5-mini"]


def test_attach_models_to_subtasks_backfills_placeholder_model_for_explicit_provider(monkeypatch) -> None:
    class RecordingRegistry:
        def select_provider_for_tier(
            self,
            tier: str,
            *,
            prefer_free: bool = True,
            caller: str | None = None,
            code_only: bool = False,
        ) -> dict[str, object]:
            return {
                "provider": "Claude Code",
                "provider_id": "claude-code",
                "model": "claude-haiku-4.5",
                "tier": tier,
            }

    monkeypatch.setattr(mcp_server, "get_registry", lambda: RecordingRegistry())
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

    result = mcp_server._attach_models_to_subtasks({
        "subtasks": [{
            "tier": "low",
            "description": "summarize parser.py routing heuristics",
            "model": "low",
            "provider": "Claude Code",
        }]
    })

    subtask = result["subtasks"][0]
    assert subtask["model"] == "claude-haiku-4.5"
    assert subtask["provider"] == "Claude Code"
    assert subtask["provider_id"] == "claude-code"


def test_attach_models_to_subtasks_filters_non_primitive_selection_fields(monkeypatch) -> None:
    class RecordingRegistry:
        def select_provider_for_tier(
            self,
            tier: str,
            *,
            prefer_free: bool = True,
            caller: str | None = None,
            code_only: bool = False,
        ) -> dict[str, object]:
            return {
                "provider": "GitHub Copilot",
                "provider_id": "github-copilot",
                "model": "gpt-5-mini",
                "tier": tier,
                "billing_source": "provider_default",
                "extra": object(),
            }

    monkeypatch.setattr(mcp_server, "get_registry", lambda: RecordingRegistry())
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

    result = mcp_server._attach_models_to_subtasks({
        "subtasks": [{
            "tier": "low",
            "description": "summarize parser.py routing heuristics",
        }]
    })

    subtask = result["subtasks"][0]
    assert subtask["billing_source"] == "provider_default"
    assert "extra" not in subtask


def test_attach_models_to_subtasks_avoids_code_only_for_read_only_low_tier(monkeypatch) -> None:
    class RecordingRegistry(StubRegistry):
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def select_provider_for_tier(
            self,
            tier: str,
            *,
            prefer_free: bool = True,
            caller: str | None = None,
            code_only: bool = False,
        ) -> dict[str, object]:
            self.calls.append({
                "tier": tier,
                "prefer_free": prefer_free,
                "caller": caller,
                "code_only": code_only,
            })
            return super().select_provider_for_tier(
                tier,
                prefer_free=prefer_free,
                caller=caller,
                code_only=code_only,
            )

    registry = RecordingRegistry()
    monkeypatch.setattr(mcp_server, "get_registry", lambda: registry)
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "github-copilot")

    mcp_server._attach_models_to_subtasks({
        "subtasks": [{
            "tier": "low",
            "description": "summarize parser.py routing heuristics",
        }]
    })

    assert registry.calls == [{
        "tier": "low",
        "prefer_free": True,
        "caller": "github-copilot",
        "code_only": False,
    }]


def test_inspect_status_readiness_summary(monkeypatch) -> None:
    """D-01/D-03/D-16: one CLI-first status surface should summarize readiness."""
    with tempfile.TemporaryDirectory() as td:
        project_path = Path(td) / "repo"
        project_path.mkdir()
        project_id = str(project_path.resolve())
        db_path = Path(td) / "status.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO project_routing (project_path, overrides_json, learning_enabled, ts)
                VALUES (?, ?, 1, 0)
                """,
                (project_id, "{}",),
            )
            conn.execute(
                """
                INSERT INTO project_settings
                    (project_path, concurrency_limit, budget_hard_cap_tokens,
                     fanout_cap, pending_approval_limit, ts)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (project_id, 4, 2500, 2, 5),
            )
            conn.execute(
                """
                INSERT INTO approval_queue
                    (project_path, draft_fingerprint, draft_name, draft_json,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    project_id,
                    "draft-1",
                    "status-agent",
                    json.dumps({"instructions": "## Context\nReady."}),
                    "2026-04-10T00:00:00Z",
                    "2026-04-10T00:00:00Z",
                ),
            )

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: Path(td).resolve())

        inspection = mcp_server.inspect_status(project_id)

        assert inspection["project_id"] == project_id
        assert set(inspection) >= {"readiness", "limits", "pending_approvals"}
        assert inspection["readiness"]["enabled_features"] == ["learning", "approval_queue", "fanout"]
        assert inspection["readiness"]["enabled"] == ["learning", "approval_queue", "fanout"]
        assert inspection["readiness"]["summary"]["learning_enabled"] is True
        assert inspection["readiness"]["summary"]["pending_approval_count"] == 1
        assert inspection["limits"] == {
            "concurrency": 4,
            "budget_hard_cap_tokens": 2500,
            "fanout_cap": 2,
            "pending_approval_limit": 5,
        }
        assert inspection["explainability_link"] == "threnody inspect status --details"
        assert len(inspection["pending_approvals"]) == 1
        assert "draft" not in inspection["pending_approvals"][0]
        assert inspection["pending_approvals"][0]["instructions_preview"].startswith("## Context")


def test_inspect_status_rejects_paths_outside_workspace(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as outside_dir:
        workspace_path = Path(workspace_dir).resolve()
        outside_path = Path(outside_dir).resolve()
        db_path = workspace_path / "status.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: workspace_path)

        inspection = mcp_server.inspect_status(str(outside_path))

        assert inspection == {
            "error": "InvalidProjectPath",
            "details": "project_path must resolve inside the active workspace",
        }


def test_tune_set_show_and_reset_round_trip(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        project_path = Path(td) / "repo"
        project_path.mkdir()
        project_id = str(project_path.resolve())
        db_path = Path(td) / "tune.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: Path(td).resolve())

        baseline = mcp_server.tune_show(project_id)
        updated = mcp_server.tune_set(project_id, "concurrency_limit", "5")
        inspection = mcp_server.inspect_status(project_id)
        reset = mcp_server.tune_reset(project_id, "concurrency_limit")

        assert baseline["settings"]["concurrency_limit"] != 5
        assert updated["updated"] is True
        assert updated["value"] == 5
        assert inspection["limits"]["concurrency"] == 5
        assert reset["reset"] is True
        assert (
            reset["settings"]["concurrency_limit"]
            == baseline["settings"]["concurrency_limit"]
        )


def test_tune_set_allows_unbounded_concurrency_and_large_fanout_values(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        project_path = Path(td) / "repo"
        project_path.mkdir()
        project_id = str(project_path.resolve())
        db_path = Path(td) / "tune.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: Path(td).resolve())

        fanout = mcp_server.tune_set(project_id, "fanout_cap", "99")
        concurrency = mcp_server.tune_set(project_id, "concurrency_limit", "-1")
        inspection = mcp_server.inspect_status(project_id)

        assert fanout["updated"] is True
        assert fanout["value"] == 99
        assert fanout["warning"] is None
        assert concurrency["updated"] is True
        assert concurrency["value"] == -1
        assert inspection["limits"]["concurrency"] is None
        assert inspection["limits"]["fanout_cap"] == 99


def test_tune_set_allows_zero_for_disable_style_limits(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        project_path = Path(td) / "repo"
        project_path.mkdir()
        project_id = str(project_path.resolve())
        db_path = Path(td) / "tune-zero.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: Path(td).resolve())

        zeroed = mcp_server.tune_set(project_id, "fanout_cap", "0")
        numeric_zeroed = mcp_server.tune_set(project_id, "pending_approval_limit", 0)
        inspection = mcp_server.inspect_status(project_id)

        assert zeroed["updated"] is True
        assert zeroed["value"] == 0
        assert numeric_zeroed["updated"] is True
        assert numeric_zeroed["value"] == 0
        assert "fanout" in inspection["readiness"]["disabled_features"]


def test_approval_queue_wrappers_require_operator_and_update_state(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        project_path = Path(td) / "repo"
        project_path.mkdir()
        project_id = str(project_path.resolve())
        db_path = Path(td) / "approvals.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO approval_queue
                    (project_path, draft_fingerprint, draft_name, draft_json,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    project_id,
                    "draft-approve",
                    "wrapper-agent",
                    json.dumps({"name": "wrapper-agent", "instructions": "## Context\nWrap."}),
                    "2026-04-10T00:00:00Z",
                    "2026-04-10T00:00:00Z",
                ),
            )

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: Path(td).resolve())

        queued = mcp_server.approval_queue_list(project_id, limit=10)
        missing_operator = mcp_server.approval_queue_approve(project_id, queued[0]["id"], "")
        rejected = mcp_server.approval_queue_reject(
            project_id,
            queued[0]["id"],
            "operator-4",
            reason="needs-more-evidence",
        )

        assert len(queued) == 1
        assert missing_operator["error"] == "ApprovalActionError"
        assert rejected["rejected"] is True
        assert rejected["operator_id"] == "operator-4"

        with db.conn() as conn:
            queue_row = conn.execute(
                "SELECT status, review_note FROM approval_queue WHERE id = ?",
                (queued[0]["id"],),
            ).fetchone()
        assert queue_row == ("rejected", "needs-more-evidence")


def test_approval_queue_approve_handler_with_operator_audit(monkeypatch) -> None:
    """Wave 2b: Test approval handler updates queue state and operator tracking."""
    with tempfile.TemporaryDirectory() as td:
        project_path = Path(td) / "repo"
        project_path.mkdir()
        project_id = str(project_path.resolve())
        db_path = Path(td) / "approvals-wave2b.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        # Create a draft agent in the approval_queue
        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO approval_queue
                    (project_path, draft_fingerprint, draft_name, draft_json,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    project_id,
                    "draft-wave2b",
                    "test-agent",
                    json.dumps({"name": "test-agent", "instructions": "## Context\nTest."}),
                    "2026-04-13T00:00:00Z",
                    "2026-04-13T00:00:00Z",
                ),
            )

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: Path(td).resolve())

        # Get the queued draft
        queued = mcp_server.approval_queue_list(project_id, limit=10)
        assert len(queued) == 1
        queue_id = queued[0]["id"]
        assert queued[0]["status"] == "pending"

        # Approve the draft with operator identifier
        operator_id = "test-operator-42"
        result = mcp_server.approval_queue_approve(project_id, queue_id, operator_id)

        # Verify approval succeeded
        assert result["approved"] is True

        # Verify queue status updated to "approved" in database
        with db.conn() as conn:
            queue_row = conn.execute(
                "SELECT status FROM approval_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()
        assert queue_row[0] == "approved"

        # Verify that we can still retrieve it with a different status filter
        approved_items = mcp_server.approval_queue_list(
            project_id,
            limit=10
        ) if hasattr(mcp_server, 'approval_queue_list_by_status') else []
        
        # More direct check: verify via database that the status changed
        with db.conn() as conn:
            final_row = conn.execute(
                "SELECT status, updated_at FROM approval_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()
        assert final_row is not None
        assert final_row[0] == "approved"


def test_approval_queue_reject_handler_logs_reason_with_operator(monkeypatch) -> None:
    """Wave 2b: Test reject handler records reason and operator ID."""
    with tempfile.TemporaryDirectory() as td:
        project_path = Path(td) / "repo"
        project_path.mkdir()
        project_id = str(project_path.resolve())
        db_path = Path(td) / "rejections-wave2b.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        # Create a draft agent in the approval_queue
        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO approval_queue
                    (project_path, draft_fingerprint, draft_name, draft_json,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    project_id,
                    "draft-reject",
                    "test-agent-reject",
                    json.dumps({"name": "test-agent-reject", "instructions": "## Context\nReject."}),
                    "2026-04-13T00:00:00Z",
                    "2026-04-13T00:00:00Z",
                ),
            )

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: Path(td).resolve())

        queued = mcp_server.approval_queue_list(project_id, limit=10)
        assert len(queued) == 1
        queue_id = queued[0]["id"]
        assert queued[0]["status"] == "pending"

        operator_id = "test-operator-43"
        rejection_reason = "insufficient-evidence-for-reuse"
        result = mcp_server.approval_queue_reject(
            project_id,
            queue_id,
            operator_id,
            reason=rejection_reason,
        )

        assert result["rejected"] is True
        assert result["operator_id"] == operator_id

        with db.conn() as conn:
            queue_row = conn.execute(
                "SELECT status, review_note FROM approval_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()
        assert queue_row is not None
        assert queue_row[0] == "rejected"
        assert queue_row[1] == rejection_reason


def test_agent_queue_aliases_require_operator_id(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        project_path = Path(td) / "repo"
        project_path.mkdir()
        project_id = str(project_path.resolve())
        db_path = Path(td) / "agent-queue-aliases.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO approval_queue
                    (project_path, draft_fingerprint, draft_name, draft_json,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    project_id,
                    "draft-alias",
                    "test-agent-alias",
                    json.dumps({"name": "test-agent-alias", "instructions": "## Context\nAlias."}),
                    "2026-04-17T00:00:00Z",
                    "2026-04-17T00:00:00Z",
                ),
            )

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: Path(td).resolve())

        queued = mcp_server.agent_queue_list(project_id, limit=10)
        queue_id = queued[0]["id"]

        missing_approve = mcp_server.agent_queue_approve(project_id, queue_id, "")
        missing_reject = mcp_server.agent_queue_reject(project_id, queue_id, "", reason="needs-review")
        missing_merge = mcp_server.agent_queue_merge(project_id, queue_id, "canon", "", reason="near-duplicate")

        assert missing_approve == {"error": "ApprovalActionError", "details": "operator_id is required"}
        assert missing_reject == {"error": "ApprovalActionError", "details": "operator_id is required"}
        assert missing_merge == {"error": "ApprovalActionError", "details": "operator_id is required"}


def test_agent_queue_approve_alias_preserves_approval_flow(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        project_path = Path(td) / "repo"
        project_path.mkdir()
        project_id = str(project_path.resolve())
        db_path = Path(td) / "agent-queue-approve.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO approval_queue
                    (project_path, draft_fingerprint, draft_name, draft_json,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    project_id,
                    "draft-agent-approve",
                    "agent-queue-approve",
                    json.dumps({"name": "agent-queue-approve", "instructions": "## Context\nApprove."}),
                    "2026-04-17T00:00:00Z",
                    "2026-04-17T00:00:00Z",
                ),
            )

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: Path(td).resolve())

        queued = mcp_server.agent_queue_list(project_id, limit=10)
        result = mcp_server.agent_queue_approve(project_id, queued[0]["id"], "operator-alias")

        assert result["approved"] is True
        assert result["operator_id"] == "operator-alias"

        with db.conn() as conn:
            queue_row = conn.execute(
                "SELECT status FROM approval_queue WHERE id = ?",
                (queued[0]["id"],),
            ).fetchone()
        assert queue_row == ("approved",)

def test_memory_handlers_round_trip_and_not_found(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace_path = Path(td).resolve()
        project_path = workspace_path / "repo"
        project_path.mkdir()
        project_id = str(project_path.resolve())
        db_path = workspace_path / "memory.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: workspace_path)

        global_set = mcp_server.handle_memory_set({
            "scope": "global",
            "key": "banner",
            "value": "hello",
        })
        project_set = mcp_server.handle_memory_set({
            "scope": "project",
            "key": "banner",
            "value": {"theme": "dark"},
            "project_id": project_id,
        })
        task_set = mcp_server.handle_memory_set({
            "scope": "task",
            "key": "banner",
            "value": ["todo"],
            "project_id": project_id,
            "task_id": "task-18",
        })

        assert global_set["value_type"] == "string"
        assert project_set["value_type"] == "object"
        assert task_set["value_type"] == "array"

        fetched_task = mcp_server.handle_memory_get({
            "scope": "task",
            "key": "banner",
            "project_id": project_id,
            "task_id": "task-18",
        })
        assert fetched_task["value"] == ["todo"]
        assert fetched_task["project_id"] == project_id
        assert fetched_task["task_id"] == "task-18"

        listed_project = mcp_server.handle_memory_list({
            "scope": "project",
            "project_id": project_id,
        })
        assert listed_project == [{
            "key": "banner",
            "scope": "project",
            "updated_at": project_set["updated_at"],
            "value_type": "object",
            "value_size": len('{"theme":"dark"}'.encode("utf-8")),
        }]

        deleted = mcp_server.handle_memory_delete({
            "scope": "task",
            "key": "banner",
            "project_id": project_id,
            "task_id": "task-18",
        })
        assert deleted == {"deleted": True}

        missing = mcp_server.handle_memory_get({
            "scope": "task",
            "key": "banner",
            "project_id": project_id,
            "task_id": "task-18",
        })
        assert missing == {
            "error": "not_found",
            "details": "memory key 'banner' was not found in task scope",
        }


def test_memory_handlers_validate_scope_and_identifiers(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace_path = Path(td).resolve()
        project_path = workspace_path / "repo"
        project_path.mkdir()
        project_id = str(project_path.resolve())
        db_path = workspace_path / "memory-invalid.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: workspace_path)

        invalid_scope = mcp_server.handle_memory_set({
            "scope": "workspace",
            "key": "banner",
            "value": "hello",
        })
        missing_project = mcp_server.handle_memory_get({
            "scope": "project",
            "key": "banner",
        })
        missing_task = mcp_server.handle_memory_get({
            "scope": "task",
            "key": "banner",
            "project_id": project_id,
        })
        oversized_task = mcp_server.handle_memory_set({
            "scope": "task",
            "key": "banner",
            "value": "hello",
            "project_id": project_id,
            "task_id": "t" * 257,
        })
        extra_global_identifier = mcp_server.handle_memory_list({
            "scope": "global",
            "project_id": project_id,
        })

        assert invalid_scope == {
            "error": "invalid_request",
            "details": "scope must be one of: global, project, task",
        }
        assert missing_project == {
            "error": "invalid_request",
            "details": "project_id is required for project scope",
        }
        assert missing_task == {
            "error": "invalid_request",
            "details": "task_id is required for task scope",
        }
        assert oversized_task == {
            "error": "invalid_request",
            "details": "task_id must be <= 256 characters",
        }
        assert extra_global_identifier == {
            "error": "invalid_request",
            "details": "global scope does not accept project_id or task_id",
        }


def test_memory_get_handler_rejects_corrupted_payload(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace_path = Path(td).resolve()
        project_path = workspace_path / "repo"
        project_path.mkdir()
        db_path = workspace_path / "memory-corrupt.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO memory (
                    scope, project_id, task_id, key, value_type, value_json, value_size, updated_at
                )
                VALUES ('global', '', '', 'broken', 'object', '{', 1, 0)
                """
            )

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: workspace_path)

        result = mcp_server.handle_memory_get({
            "scope": "global",
            "key": "broken",
        })

        assert result == {
            "error": "invalid_request",
            "details": "stored memory value is corrupted",
        }

def test_memory_tools_are_registered() -> None:
    tool_names = {tool["name"] for tool in mcp_server.TOOLS}
    assert {"memory_list", "memory_get", "memory_set", "memory_delete"} <= tool_names
    assert mcp_server.HANDLERS["memory_list"] is mcp_server.handle_memory_list
    assert mcp_server.HANDLERS["memory_get"] is mcp_server.handle_memory_get
    assert mcp_server.HANDLERS["memory_set"] is mcp_server.handle_memory_set
    assert mcp_server.HANDLERS["memory_delete"] is mcp_server.handle_memory_delete


def test_mcp_record_outcome(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "record-outcome.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        calls: list[tuple[object, ...]] = []

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )

        def stub_record_outcome(db_arg, task_id, outcome, operator_id=None, note=None, project_id=None):
            calls.append((db_arg, task_id, outcome, operator_id, note))
            return {"stored": True, "task_id": task_id}

        monkeypatch.setattr(
            mcp_server.shared_outcomes,
            "record_outcome",
            stub_record_outcome,
        )

        result = mcp_server.handle_record_outcome({
            "task_id": "t-123",
            "outcome": "accepted",
            "note": "done",
        })

        assert result == {"stored": True, "task_id": "t-123"}
        assert calls == [
            (db, "t-123", "accepted", mcp_server.shared_outcomes.ANONYMOUS_OPERATOR_ID, "done")
        ]


def test_mcp_record_outcome_records_anonymous_when_operator_missing(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "record-outcome-anonymous.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        calls: list[tuple[object, ...]] = []

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )

        def stub_record_outcome(db_arg, task_id, outcome, operator_id=None, note=None, project_id=None):
            calls.append((db_arg, task_id, outcome, operator_id, note))
            return {"stored": True, "task_id": task_id}

        monkeypatch.setattr(
            mcp_server.shared_outcomes,
            "record_outcome",
            stub_record_outcome,
        )

        result = mcp_server.handle_record_outcome({
            "task_id": "t-123",
            "outcome": "accepted",
        })

        assert result == {"stored": True, "task_id": "t-123"}
        assert calls == [
            (db, "t-123", "accepted", mcp_server.shared_outcomes.ANONYMOUS_OPERATOR_ID, None)
        ]


def test_mcp_record_outcome_rejects_spoofed_operator_id(monkeypatch) -> None:
    result = mcp_server.handle_record_outcome({
        "task_id": "t-123",
        "outcome": "accepted",
        "operator_id": "spoofed-user",
    })

    assert result == {
        "error": "invalid_request",
        "details": "operator_id cannot be asserted by this tool; omit it to record anonymous feedback",
    }


def test_mcp_record_outcome_rejects_asserted_operator_without_authenticated_caller(monkeypatch) -> None:
    result = mcp_server.handle_record_outcome({
        "task_id": "t-123",
        "outcome": "accepted",
        "operator_id": "spoofed-user",
    })

    assert result == {
        "error": "invalid_request",
        "details": "operator_id cannot be asserted by this tool; omit it to record anonymous feedback",
    }


def test_mcp_record_outcome_rejects_invalid_enum() -> None:
    result = mcp_server.handle_record_outcome({
        "task_id": "t-123",
        "outcome": "bad",
    })

    assert result == {
        "error": "invalid_outcome",
        "allowed": ["accepted", "revised", "rejected", "reworked"],
    }


def test_mcp_record_outcome_surfaces_readonly_window(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "record-outcome-readonly.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)

        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )

        def stub_record_outcome(*_args, **_kwargs):
            raise mcp_server.shared_outcomes.OutcomeReadonlyWindowError("window expired")

        monkeypatch.setattr(
            mcp_server.shared_outcomes,
            "record_outcome",
            stub_record_outcome,
        )

        result = mcp_server.handle_record_outcome({
            "task_id": "t-123",
            "outcome": "accepted",
        })

        assert result == {
            "error": "readonly_window_expired",
            "details": "window expired",
        }


def test_record_outcome_tool_is_registered() -> None:
    tool_names = {tool["name"] for tool in mcp_server.TOOLS}

    assert "record_outcome" in tool_names
    assert mcp_server.HANDLERS["record_outcome"] is mcp_server.handle_record_outcome


def test_learning_outcome_stats_success(monkeypatch) -> None:
    """Test learning_outcome_stats returns snapshot data when available."""
    import time
    from shared.memory import memory_set
    from shared.outcomes import compute_learning_outcome_snapshot
    
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "outcome-stats.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        
        # Setup: insert telemetry and outcome
        now = time.time()
        cutoff = now - 3600
        
        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO telemetry (ts, tier, model, provider_name)
                VALUES (?, ?, ?, ?)
                """,
                (cutoff + 100, "low", "gpt-5-mini", "test-provider"),
            )
            conn.execute(
                """
                INSERT INTO routing_outcomes (
                    task_id, current_outcome, recorded_at, tier, model,
                    provider_name, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("task-1", "accepted", cutoff + 100, "low", "gpt-5-mini", "test-provider", cutoff + 100),
            )
        
        # Compute snapshot
        compute_learning_outcome_snapshot(db)
        
        # Call handler
        result = mcp_server.handle_learning_outcome_stats({})
        
        # Verify response
        assert result["success"] == True
        assert "window_start_time" in result
        assert "window_end_time" in result
        assert "outcome_distribution" in result
        assert "coverage_percentage" in result
        assert "total_tasks_in_window" in result
        assert "tasks_with_feedback" in result
        assert "computed_at" in result
        assert "low:gpt-5-mini" in result["outcome_distribution"]


def test_learning_outcome_stats_not_available(monkeypatch) -> None:
    """Test learning_outcome_stats returns error when snapshot not available."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "outcome-stats-empty.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        
        # Call handler with no snapshot
        result = mcp_server.handle_learning_outcome_stats({})
        
        # Verify error response
        assert result["success"] == False
        assert "error" in result
        assert "not yet available" in result["error"]


def test_learning_outcome_stats_response_schema(monkeypatch) -> None:
    """Test learning_outcome_stats returns complete response schema."""
    import time
    from shared.outcomes import compute_learning_outcome_snapshot
    
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "outcome-stats-schema.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        
        # Setup data
        now = time.time()
        cutoff = now - 3600
        
        with db.conn() as conn:
            # Insert 3 telemetry records
            for i in range(3):
                conn.execute(
                    """
                    INSERT INTO telemetry (ts, tier, model, provider_name)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cutoff + 100 + i*60, "low", "gpt-5-mini", "test-provider"),
                )
            
            # Insert 2 outcomes (66.7% coverage)
            for i in range(2):
                conn.execute(
                    """
                    INSERT INTO routing_outcomes (
                        task_id, current_outcome, recorded_at, tier, model,
                        provider_name, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (f"task-{i}", "accepted", cutoff + 100 + i*60, "low", "gpt-5-mini", "test-provider", cutoff + 100 + i*60),
                )
        
        # Compute snapshot
        compute_learning_outcome_snapshot(db)
        
        # Call handler
        result = mcp_server.handle_learning_outcome_stats({})
        
        # Verify all required fields are present
        required_fields = [
            "success", "window_start_time", "window_end_time",
            "outcome_distribution", "coverage_percentage",
            "total_tasks_in_window", "tasks_with_feedback", "computed_at"
        ]
        for field in required_fields:
            assert field in result, f"Missing field: {field}"
        
        # Verify types
        assert isinstance(result["success"], bool)
        assert isinstance(result["window_start_time"], float)
        assert isinstance(result["window_end_time"], float)
        assert isinstance(result["outcome_distribution"], dict)
        assert isinstance(result["total_tasks_in_window"], int)
        assert isinstance(result["tasks_with_feedback"], int)
        assert isinstance(result["computed_at"], float)


def test_learning_outcome_stats_tool_is_registered() -> None:
    """Test that learning_outcome_stats tool is registered in TOOLS and HANDLERS."""
    tool_names = {tool["name"] for tool in mcp_server.TOOLS}
    
    assert "learning_outcome_stats" in tool_names
    assert mcp_server.HANDLERS["learning_outcome_stats"] is mcp_server.handle_learning_outcome_stats


def test_every_published_tool_has_a_callable_handler() -> None:
    tool_names = [tool["name"] for tool in mcp_server.TOOLS]

    assert len(tool_names) == len(set(tool_names))
    assert set(tool_names) <= set(mcp_server.HANDLERS)
    assert all(callable(mcp_server.HANDLERS[name]) for name in tool_names)


def test_learning_pattern_health_includes_coverage(monkeypatch) -> None:
    """Test that learning_pattern_health includes outcome_coverage_percentage."""
    import time
    from shared.outcomes import compute_learning_outcome_snapshot
    
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "pattern-health-coverage.db"
        cfg = TGsConfig(db_path=db_path)
        db = Database(db_path=db_path)
        
        monkeypatch.setattr(
            mcp_server,
            "_ensure_init",
            lambda: (cfg, db, None, None, None),
        )
        
        # Setup: insert telemetry and outcomes
        now = time.time()
        cutoff = now - 3600
        
        with db.conn() as conn:
            # Insert 40 telemetry records
            for i in range(40):
                conn.execute(
                    """
                    INSERT INTO telemetry (ts, tier, model, provider_name)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cutoff + 100 + i*60, "low", "gpt-5-mini", "test-provider"),
                )
            
            # Insert 35 outcomes (87.5% coverage)
            for i in range(35):
                conn.execute(
                    """
                    INSERT INTO routing_outcomes (
                        task_id, current_outcome, recorded_at, tier, model,
                        provider_name, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (f"task-{i}", "accepted", cutoff + 100 + i*60, "low", "gpt-5-mini", "test-provider", cutoff + 100 + i*60),
                )
        
        # Compute snapshot
        compute_learning_outcome_snapshot(db)
        
        # Call handler
        result = mcp_server.handle_learning_pattern_health({})
        
        # Verify coverage fields are present
        assert result["success"] == True
        assert "outcome_coverage_percentage" in result
        assert "outcome_window_hours" in result
        assert "feedback_scope" in result
        
        # Verify values
        assert result["outcome_coverage_percentage"] == 87.5
        assert result["outcome_window_hours"] == 1
        assert result["feedback_scope"] == "global"
        
        # Verify existing fields are still present
        assert "patterns_tracked" in result
        assert "mature_patterns" in result
        assert "pending_proof" in result
        assert "draft_proposals" in result
        assert "active_agents" in result
