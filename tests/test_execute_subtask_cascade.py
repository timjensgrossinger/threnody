"""Tests for execute_subtask auto-cascade on FileTooLarge and auto path exception."""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import mcp_server
from shared.config import TGsConfig
from shared.db import Database


ROOT = Path(__file__).parent.parent


class _StubRegistry:
    """Registry stub that echoes back without calling a real CLI."""

    def select_provider_for_tier(self, tier: str, **_kw):
        return {
            "provider": "GitHub Copilot",
            "provider_id": "github-copilot",
            "model": "gpt-5-mini",
            "tier": tier,
            "is_free": True,
            "billing_tier": "free",
            "provider_cost_hint": "free",
            "cost_rank": 0,
            "billing_source": "provider_default",
            "excluded_providers": [],
        }

    def execute_cheapest(self, **_kw):
        return {
            "result": "# stub output",
            "provider": "GitHub Copilot",
            "provider_id": "github-copilot",
            "model": "gpt-5-mini",
            "tier": "low",
            "is_free": True,
            "billing_tier": "free",
            "provider_cost_hint": "free",
            "cost_rank": 0,
            "billing_source": "provider_default",
            "fallback_used": False,
        }


def _setup(tmpdir: str) -> tuple[TGsConfig, Database]:
    db_path = Path(tmpdir) / "cascade.db"
    cfg = TGsConfig(db_path=db_path, delegation_utilities_enabled=True)
    db = Database(db_path=db_path)
    return cfg, db


def _big_file(path: Path, size_bytes: int) -> None:
    """Write a Python source file of at least size_bytes."""
    line = "# " + "x" * 78 + "\n"
    reps = (size_bytes // len(line)) + 1
    path.write_text(line * reps, encoding="utf-8")


# ---------------------------------------------------------------------------
# auto_cascade_mode tests
# ---------------------------------------------------------------------------

def test_rewrite_cascade_to_blocks_suppresses_file_too_large(monkeypatch, tmp_path):
    """File > 32 KiB + rewrite mode + auto_cascade_mode=True → no FileTooLarge."""
    target = tmp_path / "big.py"
    _big_file(target, 34_000)  # > 32 KiB limit

    with tempfile.TemporaryDirectory() as td:
        cfg, db = _setup(td)
        cfg.auto_cascade_mode = True

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "get_registry", lambda: _StubRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Add a docstring to the module.",
            "tier": "low",
            "mode": "rewrite",
            "target_file": str(target),
        })

    assert result.get("error") != "FileTooLarge", (
        f"Expected cascade, got FileTooLarge: {result.get('details')}"
    )


def test_rewrite_returns_file_too_large_when_cascade_disabled(monkeypatch, tmp_path):
    """File > 32 KiB + rewrite mode + auto_cascade_mode=False → FileTooLarge."""
    target = tmp_path / "big.py"
    _big_file(target, 34_000)

    with tempfile.TemporaryDirectory() as td:
        cfg, db = _setup(td)
        cfg.auto_cascade_mode = False

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "get_registry", lambda: _StubRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Add a docstring to the module.",
            "tier": "low",
            "mode": "rewrite",
            "target_file": str(target),
        })

    assert result.get("error") == "FileTooLarge"
    assert "rewrite" in result.get("details", "").lower()


def test_blocks_cascade_to_patch_suppresses_file_too_large(monkeypatch, tmp_path):
    """File > 128 KiB + blocks mode + auto_cascade_mode=True → no FileTooLarge."""
    target = tmp_path / "huge.py"
    _big_file(target, 135_000)  # > 128 KiB limit

    with tempfile.TemporaryDirectory() as td:
        cfg, db = _setup(td)
        cfg.auto_cascade_mode = True

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "get_registry", lambda: _StubRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Fix a typo in line 5.",
            "tier": "low",
            "mode": "blocks",
            "target_file": str(target),
        })

    assert result.get("error") != "FileTooLarge", (
        f"Expected cascade, got FileTooLarge: {result.get('details')}"
    )


def test_blocks_returns_file_too_large_when_cascade_disabled(monkeypatch, tmp_path):
    """File > 128 KiB + blocks mode + auto_cascade_mode=False → FileTooLarge."""
    target = tmp_path / "huge.py"
    _big_file(target, 135_000)

    with tempfile.TemporaryDirectory() as td:
        cfg, db = _setup(td)
        cfg.auto_cascade_mode = False

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "get_registry", lambda: _StubRegistry())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Fix a typo in line 5.",
            "tier": "low",
            "mode": "blocks",
            "target_file": str(target),
        })

    assert result.get("error") == "FileTooLarge"
    assert "blocks" in result.get("details", "").lower()


# ---------------------------------------------------------------------------
# auto path exception registration
# ---------------------------------------------------------------------------

def test_execute_subtask_auto_registers_path_exception(monkeypatch, tmp_path):
    """After execute_subtask writes a file, its path is added to routing_exceptions."""
    target = tmp_path / "out.py"

    with tempfile.TemporaryDirectory() as td:
        cfg, db = _setup(td)

        monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
        monkeypatch.setattr(mcp_server, "get_registry", lambda: _StubRegistry())
        monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())

        result = mcp_server.handle_execute_subtask({
            "prompt": "Write a hello-world script.",
            "tier": "low",
            "mode": "write",
            "target_file": str(target),
        })

        # The subtask should have written the file.
        assert result.get("error") is None, f"Unexpected error: {result}"

        # Path exception should now exist in the DB.
        exceptions = db.routing_exception_list()
        paths = [e["pattern"] for e in exceptions if e["exception_type"] == "path"]
        assert str(target) in paths, (
            f"Expected {target} in path exceptions, got: {paths}"
        )

        # Validate guard should allow a direct Edit on that file now.
        with patch.object(mcp_server, "_ensure_init", return_value=(cfg, db, None, None, None)):
            guard_result = mcp_server.handle_validate_routing_guard({
                "target_file": str(target),
                "cwd": str(tmp_path),
                "tool_name": "Edit",
            })
        assert guard_result["valid"] is True
        assert "exempt" in guard_result.get("mode", "")
