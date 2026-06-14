#!/usr/bin/env python3
"""Provider execution, routing, and DB integrity tests."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.discovery import BUILTIN_PROVIDERS, CLIProvider


def _builtin(name: str) -> CLIProvider:
    return next(p for p in BUILTIN_PROVIDERS if p.name == name)


def _fake_run_ok(stdout: str):
    """Return a factory for a subprocess.run mock that succeeds."""

    def _run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = stdout
        result.stderr = ""
        return result

    return _run


# ---------------------------------------------------------------------------
# 1. github-copilot: answer returned, sandbox env set
# ---------------------------------------------------------------------------
def test_github_copilot_execute_returns_answer(monkeypatch):
    provider = _builtin("github-copilot")
    captured = {}

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        r = MagicMock()
        r.returncode = 0
        r.stdout = "answer\n\n"
        r.stderr = ""
        return r

    monkeypatch.setattr("shared.discovery.subprocess.run", _run)
    with (
        patch("shared.discovery._copilot_supports_model_flag", return_value=True),
        patch("shared.discovery._copilot_supports_disable_builtin_mcps", return_value=True),
    ):
        result = provider.execute("reply with: answer", "gpt-5-mini", timeout=10)

    assert result == "answer"
    assert captured["kwargs"]["env"]["COPILOT_HOME"].endswith("copilot-sandbox")
    assert captured["kwargs"]["cwd"].endswith("copilot-sandbox")


# ---------------------------------------------------------------------------
# 2. github-copilot: stderr stats line does not corrupt output
# ---------------------------------------------------------------------------
def test_github_copilot_execute_strips_stderr_stats(monkeypatch):
    provider = _builtin("github-copilot")

    def _run(cmd, **kwargs):
        r = MagicMock()
        r.returncode = 0
        r.stdout = "answer\n\n"
        r.stderr = "\n\nChanges +0 -0\nRequests 1 Premium (5s)\nTokens ↑ 30k • ↓ 8\n"
        return r

    monkeypatch.setattr("shared.discovery.subprocess.run", _run)
    with (
        patch("shared.discovery._copilot_supports_model_flag", return_value=True),
        patch("shared.discovery._copilot_supports_disable_builtin_mcps", return_value=True),
    ):
        result = provider.execute("reply with: answer", "gpt-5-mini", timeout=10)

    assert result == "answer"


# ---------------------------------------------------------------------------
# 3. claude-code: answer returned, correct CLI args
# ---------------------------------------------------------------------------
def test_claude_code_execute_returns_answer(monkeypatch):
    provider = _builtin("claude-code")
    captured = {}

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        r = MagicMock()
        r.returncode = 0
        r.stdout = "answer\n"
        r.stderr = ""
        return r

    monkeypatch.setattr("shared.discovery.subprocess.run", _run)
    result = provider.execute("reply with: answer", "haiku", timeout=10)

    assert result == "answer"
    assert captured["cmd"][0] == "claude"
    assert "-p" in captured["cmd"]
    assert "--model" in captured["cmd"]


# ---------------------------------------------------------------------------
# 4. claude-code: known 60s --print hang → TimeoutExpired → returns None
# ---------------------------------------------------------------------------
def test_claude_code_execute_timeout_returns_none(monkeypatch):
    provider = _builtin("claude-code")

    def _run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

    monkeypatch.setattr("shared.discovery.subprocess.run", _run)
    assert provider.execute("x", "haiku", timeout=10) is None


# ---------------------------------------------------------------------------
# 5. mistral-vibe: answer returned, correct CLI args
# ---------------------------------------------------------------------------
def test_mistral_vibe_execute_returns_answer(monkeypatch):
    provider = _builtin("mistral-vibe")
    captured = {}

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        r = MagicMock()
        r.returncode = 0
        r.stdout = "answer\n"
        r.stderr = ""
        return r

    monkeypatch.setattr("shared.discovery.subprocess.run", _run)
    result = provider.execute("reply with: answer", "mistral-medium-3.5", timeout=10)

    assert result == "answer"
    assert captured["cmd"][0] == "vibe"
    assert "-p" in captured["cmd"]
    assert "--workdir" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--workdir") + 1] != "/tmp"


# ---------------------------------------------------------------------------
# 6. mistral-vibe: CLI freeze (TimeoutExpired) → returns None, does not raise
# ---------------------------------------------------------------------------
def test_mistral_vibe_execute_timeout_returns_none(monkeypatch):
    provider = _builtin("mistral-vibe")

    def _run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=300)

    monkeypatch.setattr("shared.discovery.subprocess.run", _run)
    assert provider.execute("x", "mistral-medium-3.5", timeout=300) is None


def test_mistral_safe_builder_preserves_private_workdir_on_hook_failure():
    from shared.discovery import _build_mistral_command_safe

    with patch("shared.discovery._get_mistral_hooks", side_effect=RuntimeError("boom")):
        cmd = _build_mistral_command_safe(None, "reply", "mistral-medium-3.5", "hello")

    assert cmd[0] == "vibe"
    assert "--workdir" in cmd
    assert cmd[cmd.index("--workdir") + 1] != "/tmp"


def test_mistral_workdir_is_cleaned_up_after_execute(monkeypatch):
    provider = _builtin("mistral-vibe")
    captured = {}

    def _run(cmd, **kwargs):
        workdir = cmd[cmd.index("--workdir") + 1]
        captured["workdir"] = workdir
        assert Path(workdir).exists()
        r = MagicMock()
        r.returncode = 0
        r.stdout = "answer\n"
        r.stderr = ""
        return r

    monkeypatch.setattr("shared.discovery.subprocess.run", _run)
    result = provider.execute("reply with: answer", "mistral-medium-3.5", timeout=10)

    assert result == "answer"
    assert "workdir" in captured
    assert not Path(captured["workdir"]).exists()


# ---------------------------------------------------------------------------
# 7. caller-scoped preference: Claude caller can explicitly route to Claude Code
# ---------------------------------------------------------------------------
def test_execute_cheapest_allows_claude_code_for_explicit_claude_preference(monkeypatch):
    from shared.discovery import ProviderRegistry, CLIProvider, DetectReason, ProviderReadiness

    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)
    monkeypatch.setattr("shared.discovery._LOCAL_ENDPOINT_CANDIDATES", ())

    def _mp(name, cost):
        p = MagicMock(spec=CLIProvider)
        p.name = name
        p.display_name = name
        p.binary = name
        p.tier_models = {"low": f"{name}-low", "medium": f"{name}-med", "high": f"{name}-high"}
        p.cost_rank = {"low": cost, "medium": cost, "high": cost}
        p.readiness = None
        p.detect.return_value = ProviderReadiness(routeable=True, reason=DetectReason.READY)
        p.execute.return_value = "ok"
        return p

    claude = _mp("claude-code", 1)
    copilot = _mp("github-copilot", 0)

    overrides = {
        "preferred_routing_by_caller": {
            "claude-code": {
                "low": [{"provider": "claude-code"}],
            },
        },
    }

    with patch("shared.discovery.BUILTIN_PROVIDERS", [claude, copilot]):
        registry = ProviderRegistry(config_overrides=overrides)

    result = registry.execute_cheapest(
        "reply with: ok", tier="low", caller="claude-code", timeout=10
    )

    # Caller preference routed execution to claude-code, not the cheaper copilot.
    assert result.get("provider_id") == "claude-code"
    claude.execute.assert_called_once()


# ---------------------------------------------------------------------------
# 8. caller-scoped routing order: Claude caller can explicitly prefer Claude, then Mistral
# ---------------------------------------------------------------------------
def test_ordered_candidates_prefers_claude_then_mistral_for_claude_caller(monkeypatch):
    from shared.discovery import ProviderRegistry, CLIProvider, DetectReason, ProviderReadiness
    from unittest.mock import MagicMock

    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)
    monkeypatch.setattr("shared.discovery._LOCAL_ENDPOINT_CANDIDATES", ())

    def _mp(name, cost):
        p = MagicMock(spec=CLIProvider)
        p.name = name
        p.display_name = name
        p.binary = name
        p.tier_models = {"low": f"{name}-low", "medium": f"{name}-med", "high": f"{name}-high"}
        p.cost_rank = {"low": cost, "medium": cost, "high": cost}
        p.readiness = None
        p.detect.return_value = ProviderReadiness(routeable=True, reason=DetectReason.READY)
        return p

    claude = _mp("claude-code", 1)
    mistral = _mp("mistral-vibe", 2)
    copilot = _mp("github-copilot", 0)

    overrides = {
        "preferred_routing_by_caller": {
            "claude-code": {
                "low": [{"provider": "claude-code"}, {"provider": "mistral-vibe"}],
            },
        },
    }
    with patch("shared.discovery.BUILTIN_PROVIDERS", [claude, mistral, copilot]):
        registry = ProviderRegistry(config_overrides=overrides)

    selected, _ = registry._ordered_execution_candidates("low", caller="claude-code")
    names = [p.name for p in selected]
    assert names[:2] == ["claude-code", "mistral-vibe"]


# ---------------------------------------------------------------------------
# 9. usage window: explicit Claude preference spills over to Mistral at threshold
# ---------------------------------------------------------------------------
def test_low_tier_routing_mistral_then_claude_when_claude_degraded(monkeypatch):
    from shared.discovery import ProviderRegistry, CLIProvider, DetectReason, ProviderReadiness
    from unittest.mock import MagicMock

    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)
    monkeypatch.setattr("shared.discovery._LOCAL_ENDPOINT_CANDIDATES", ())

    def _mp(name, cost):
        p = MagicMock(spec=CLIProvider)
        p.name = name
        p.display_name = name
        p.binary = name
        p.tier_models = {"low": f"{name}-low", "medium": f"{name}-med", "high": f"{name}-high"}
        p.cost_rank = {"low": cost, "medium": cost, "high": cost}
        p.readiness = None
        p.detect.return_value = ProviderReadiness(routeable=True, reason=DetectReason.READY)
        return p

    claude = _mp("claude-code", 1)
    mistral = _mp("mistral-vibe", 2)

    overrides = {
        "preferred_routing_by_caller": {
            "claude-code": {
                "low": [{"provider": "claude-code"}, {"provider": "mistral-vibe"}],
            },
        },
    }
    with patch("shared.discovery.BUILTIN_PROVIDERS", [claude, mistral]):
        registry = ProviderRegistry(config_overrides=overrides)

    registry._db = object()

    monkeypatch.setattr(
        registry,
        "_apply_usage_window_overrides",
        lambda candidates, tier, cfg, db: (
            [p for p in candidates if p.name != "claude-code"]
            + [p for p in candidates if p.name == "claude-code"],
            True,
        ),
    )

    selected, _ = registry._ordered_execution_candidates("low", caller="claude-code")
    names = [p.name for p in selected]
    assert names[0] == "mistral-vibe"
    assert "claude-code" in names
    assert names.index("mistral-vibe") < names.index("claude-code")


# ---------------------------------------------------------------------------
# 10. WAL mode: Database connection uses WAL journal mode after fix
# ---------------------------------------------------------------------------
def test_database_opens_with_wal_mode(tmp_path):
    from shared.db import Database

    db_path = tmp_path / "test.db"
    db = Database(db_path=db_path)
    with db.conn():
        pass
    conn = sqlite3.connect(str(db_path))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    db.close()
    assert mode == "wal", f"expected WAL mode, got {mode!r}"


# ---------------------------------------------------------------------------
# workspace_root threading tests (from test_provider_workspace_root.py)
# ---------------------------------------------------------------------------

import os as _os

_ROOT_CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT_CC / "claude-code"))

_os.environ.setdefault("THRENODY_TEST_MODE", "1")

import providers as claude_providers  # noqa: E402
from providers import ClaudeCodeProvider  # noqa: E402
from shared.planner import Subtask as _Subtask  # noqa: E402


def _make_cc_provider() -> ClaudeCodeProvider:
    p = ClaudeCodeProvider()
    p._claude_available = True  # skip shutil.which("claude")
    return p


def _mock_run_ok(stdout: str = "output text") -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = stdout
    return r


def _subtask_cc(workspace_root: str | None = None) -> _Subtask:
    return _Subtask(
        id=1,
        description="stub task",
        tier="low",
        model="claude-haiku-4-5",
        workspace_root=workspace_root,
    )


def test_workspace_root_passed_as_subprocess_cwd() -> None:
    provider = _make_cc_provider()

    with patch.object(claude_providers.subprocess, "run",
                      return_value=_mock_run_ok()) as mock_run:
        result = provider._execute_via_claude(_subtask_cc("/fake/workspace"), "claude-haiku-4-5")

    assert result == "output text"
    assert mock_run.call_count == 1
    _, kwargs = mock_run.call_args
    assert kwargs["cwd"] == "/fake/workspace"


def test_none_workspace_root_passes_none_cwd() -> None:
    provider = _make_cc_provider()

    with patch.object(claude_providers.subprocess, "run",
                      return_value=_mock_run_ok()) as mock_run:
        provider._execute_via_claude(_subtask_cc(None), "claude-haiku-4-5")

    _, kwargs = mock_run.call_args
    assert kwargs["cwd"] is None


def test_empty_string_workspace_root_treated_as_none() -> None:
    # `getattr(subtask, "workspace_root", None) or None` coerces "" → None
    provider = _make_cc_provider()

    with patch.object(claude_providers.subprocess, "run",
                      return_value=_mock_run_ok()) as mock_run:
        provider._execute_via_claude(_subtask_cc(""), "claude-haiku-4-5")

    _, kwargs = mock_run.call_args
    assert kwargs["cwd"] is None
