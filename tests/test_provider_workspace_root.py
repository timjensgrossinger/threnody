#!/usr/bin/env python3
"""Verify workspace_root is threaded to subprocess cwd in the claude-code provider.

Covers claude-code/providers.py:145 — the line that passes workspace_root as cwd
to subprocess.run. This test was missing after the fix was merged.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "claude-code"))

os.environ.setdefault("THRENODY_TEST_MODE", "1")

import providers as claude_providers  # noqa: E402
from providers import ClaudeCodeProvider  # noqa: E402
from shared.planner import Subtask  # noqa: E402


def _make_provider() -> ClaudeCodeProvider:
    p = ClaudeCodeProvider()
    p._claude_available = True  # skip shutil.which("claude")
    return p


def _mock_run_ok(stdout: str = "output text") -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = stdout
    return r


def _subtask(workspace_root: str | None = None) -> Subtask:
    return Subtask(id=1, description="stub task", tier="low",
                   model="claude-haiku-4-5", workspace_root=workspace_root)


def test_workspace_root_passed_as_subprocess_cwd() -> None:
    provider = _make_provider()

    with patch.object(claude_providers.subprocess, "run",
                      return_value=_mock_run_ok()) as mock_run:
        result = provider._execute_via_claude(_subtask("/fake/workspace"), "claude-haiku-4-5")

    assert result == "output text"
    assert mock_run.call_count == 1
    _, kwargs = mock_run.call_args
    assert kwargs["cwd"] == "/fake/workspace"


def test_none_workspace_root_passes_none_cwd() -> None:
    provider = _make_provider()

    with patch.object(claude_providers.subprocess, "run",
                      return_value=_mock_run_ok()) as mock_run:
        provider._execute_via_claude(_subtask(None), "claude-haiku-4-5")

    _, kwargs = mock_run.call_args
    assert kwargs["cwd"] is None


def test_empty_string_workspace_root_treated_as_none() -> None:
    # `getattr(subtask, "workspace_root", None) or None` coerces "" → None
    provider = _make_provider()

    with patch.object(claude_providers.subprocess, "run",
                      return_value=_mock_run_ok()) as mock_run:
        provider._execute_via_claude(_subtask(""), "claude-haiku-4-5")

    _, kwargs = mock_run.call_args
    assert kwargs["cwd"] is None
