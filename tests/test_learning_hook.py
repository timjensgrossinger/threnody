#!/usr/bin/env python3
"""Tests for the standalone PostToolUse learning-capture hook."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import learning_hook, run_log


@pytest.fixture(autouse=True)
def isolated_runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "runs"
    monkeypatch.setattr(run_log, "RUNS_ROOT", root)
    monkeypatch.setattr(run_log, "_ACTIVE_POINTER", root / "active.json")
    return root


def test_parse_hook_payload_extracts_fields() -> None:
    fields = learning_hook.parse_hook_payload(
        {
            "tool_name": "Write",
            "cwd": "/tmp/p",
            "tool_input": {"file_path": "/tmp/p/a.py"},
            "tool_response": {"success": True},
        }
    )
    assert fields["tool_name"] == "Write"
    assert fields["target_file"] == "/tmp/p/a.py"
    assert fields["success"] is True


def test_capture_uses_active_run_pointer() -> None:
    run_log.set_active_run("swarm-hook")
    out = learning_hook.capture_edit(
        {"target_file": "/tmp/p/a.py", "success": True, "run_id": None}
    )
    assert out["captured"] is True
    records = run_log.read_run_log("swarm-hook")
    assert records[0]["touched_files"] == ["/tmp/p/a.py"]
    assert records[0]["source"] == "post_tool_use_hook"


def test_capture_noop_without_active_run() -> None:
    out = learning_hook.capture_edit({"target_file": "/tmp/p/a.py", "success": True})
    assert out["captured"] is False
    assert "no active run" in out["reason"]


def test_capture_noop_without_target_file() -> None:
    run_log.set_active_run("swarm-hook2")
    out = learning_hook.capture_edit({"target_file": None, "success": True})
    assert out["captured"] is False


def test_main_ignores_non_edit_tools(capsys: pytest.CaptureFixture) -> None:
    run_log.set_active_run("swarm-hook3")
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    rc = learning_hook.main(["capture", "--json", payload])
    assert rc == 0  # never blocks
    out = json.loads(capsys.readouterr().out)
    assert out["captured"] is False
    assert run_log.read_run_log("swarm-hook3") == []


def test_main_captures_edit(capsys: pytest.CaptureFixture) -> None:
    run_log.set_active_run("swarm-hook4")
    payload = json.dumps(
        {
            "tool_name": "Edit",
            "cwd": "/tmp/p",
            "tool_input": {"file_path": "/tmp/p/b.py"},
            "tool_response": {"success": True},
        }
    )
    rc = learning_hook.main(["capture", "--json", payload])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["captured"] is True
    assert run_log.read_run_log("swarm-hook4")[0]["touched_files"] == ["/tmp/p/b.py"]


def test_main_tolerates_garbage(capsys: pytest.CaptureFixture) -> None:
    rc = learning_hook.main(["capture", "--json", "not json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["captured"] is False
