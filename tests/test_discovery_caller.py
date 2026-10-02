"""detect_caller: env markers first, then a cached parent-process walk."""

from __future__ import annotations

import pytest

import shared.discovery as disc

_WALK = disc._caller_from_process_tree
_MARKERS = (
    "OPENCODE_HOST", "OPENCODE_SESSION", "COPILOT_CLI", "COPILOT_RUN_APP",
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE", "CLAUDE_CODE_SESSION", "MCP_TRANSPORT",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in _MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)
    # conftest disables the process walk suite-wide; this module tests it.
    monkeypatch.setattr(disc, "_PROCESS_WALK_ENABLED", True)
    _WALK.cache_clear()
    yield
    _WALK.cache_clear()


@pytest.mark.parametrize(
    "name", ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"]
)
def test_claude_code_env_markers(monkeypatch, name):
    monkeypatch.setenv(name, "1")
    assert disc.detect_caller() == "claude-code"


def test_no_markers_and_no_process_match(monkeypatch):
    monkeypatch.setattr(disc, "_caller_from_process_tree", lambda ppid: None)
    assert disc.detect_caller() is None


def test_process_walk_finds_codex(monkeypatch):
    table = {
        10: "20 node /tmp/server.py",
        20: "30 /usr/bin/zsh",
        30: "40 /opt/bin/codex --foo",
    }
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        import subprocess

        pid = int(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout=table.get(pid, ""), stderr="")

    monkeypatch.setattr(disc.os, "getppid", lambda: 10)
    monkeypatch.setattr(disc.subprocess, "run", fake_run)
    assert disc.detect_caller() == "codex"
    n = len(calls)
    assert disc.detect_caller() == "codex"
    assert len(calls) == n  # cached


def test_env_marker_beats_process_walk(monkeypatch):
    monkeypatch.setattr(disc, "_caller_from_process_tree", lambda ppid: "codex")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert disc.detect_caller() == "claude-code"


def test_test_mode_skips_walk_without_transport(monkeypatch):
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")
    monkeypatch.setattr(disc, "_caller_from_process_tree", lambda ppid: "codex")
    assert disc.detect_caller() is None
