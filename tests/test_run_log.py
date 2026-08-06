#!/usr/bin/env python3
"""Tests for the append-only JSONL run log (shared/run_log.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import run_log


@pytest.fixture(autouse=True)
def isolated_runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect RUNS_ROOT (active pointer paths derive from it) into a temp dir."""
    root = tmp_path / "runs"
    monkeypatch.setattr(run_log, "RUNS_ROOT", root)
    return root


def test_append_and_read_roundtrip() -> None:
    run_log.append_agent_record("swarm-a", {"wave": 1, "spawn_id": "x", "success": True})
    run_log.append_agent_record("swarm-a", {"wave": 2, "spawn_id": "y", "success": False})
    records = run_log.read_run_log("swarm-a")
    assert len(records) == 2
    assert records[0]["spawn_id"] == "x"
    assert records[1]["wave"] == 2


def test_read_missing_run_is_empty() -> None:
    assert run_log.read_run_log("nope") == []


def test_read_tolerates_truncated_tail_line() -> None:
    run_log.append_agent_record("swarm-crash", {"wave": 1, "spawn_id": "ok"})
    # Simulate a crash mid-write: append a partial JSON line.
    path = run_log.run_log_path("swarm-crash")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"wave": 2, "spawn_id": "trunc"')  # no closing brace / newline
    records = run_log.read_run_log("swarm-crash")
    assert len(records) == 1
    assert records[0]["spawn_id"] == "ok"


def test_unsafe_run_id_is_sanitized() -> None:
    # Traversal characters get scrubbed to a single safe segment.
    run_log.append_agent_record("../../etc/passwd", {"wave": 1})
    d = run_log.run_log_dir("../../etc/passwd")
    # Collapses to a single safe segment directly under RUNS_ROOT — cannot escape.
    assert d.parent == run_log.RUNS_ROOT
    assert "/" not in d.name
    assert d.resolve().is_relative_to(run_log.RUNS_ROOT.resolve())


@pytest.mark.parametrize("bad", [".", "..", ""])
def test_rejects_degenerate_run_ids(bad: str) -> None:
    with pytest.raises(ValueError):
        run_log.run_log_dir(bad)


def test_meta_roundtrip_and_imported_flag() -> None:
    run_log.write_run_meta("swarm-m", {"topology": "star", "outcome": "accepted"})
    meta = run_log.read_run_meta("swarm-m")
    assert meta["topology"] == "star"
    assert "written_ts" in meta
    assert run_log.is_imported("swarm-m") is False
    run_log.mark_imported("swarm-m")
    assert run_log.is_imported("swarm-m") is True


def test_iter_pending_runs_excludes_imported() -> None:
    run_log.append_agent_record("swarm-pending", {"wave": 1})
    run_log.append_agent_record("swarm-done", {"wave": 1})
    run_log.mark_imported("swarm-done")
    pending = set(run_log.iter_pending_runs())
    assert "swarm-pending" in pending
    assert "swarm-done" not in pending


def test_active_run_pointer_lifecycle() -> None:
    assert run_log.get_active_run(workspace_root="/tmp/p") is None
    run_log.set_active_run("swarm-active", workspace_root="/tmp/p")
    assert run_log.get_active_run(workspace_root="/tmp/p") == "swarm-active"
    # Mismatched clear is a no-op.
    run_log.clear_active_run("other", workspace_root="/tmp/p")
    assert run_log.get_active_run(workspace_root="/tmp/p") == "swarm-active"
    run_log.clear_active_run("swarm-active", workspace_root="/tmp/p")
    assert run_log.get_active_run(workspace_root="/tmp/p") is None


def test_active_run_pointer_is_scoped_per_workspace() -> None:
    """Regression: a single global active.json meant two concurrent sessions in
    different repos shared one PostToolUse learning-hook target — one session's
    file edits were appended to the other's run log. Each workspace must get its
    own pointer.
    """
    run_log.set_active_run("swarm-a", workspace_root="/tmp/repo-a")
    run_log.set_active_run("swarm-b", workspace_root="/tmp/repo-b")
    assert run_log.get_active_run(workspace_root="/tmp/repo-a") == "swarm-a"
    assert run_log.get_active_run(workspace_root="/tmp/repo-b") == "swarm-b"
    # Clearing one workspace's run must not affect the other's pointer.
    run_log.clear_active_run("swarm-a", workspace_root="/tmp/repo-a")
    assert run_log.get_active_run(workspace_root="/tmp/repo-a") is None
    assert run_log.get_active_run(workspace_root="/tmp/repo-b") == "swarm-b"


def test_get_active_run_without_workspace_root_uses_legacy_global_pointer() -> None:
    """A caller with genuinely no workspace_root (rare) falls back to the
    pre-existing global pointer rather than always seeing None."""
    assert run_log.get_active_run() is None
    run_log.set_active_run("swarm-global")
    assert run_log.get_active_run() == "swarm-global"
    # A workspace-scoped lookup must not accidentally see the global pointer.
    assert run_log.get_active_run(workspace_root="/tmp/other") is None


def test_prune_keeps_most_recent() -> None:
    for i in range(5):
        run_log.append_agent_record(f"swarm-{i}", {"wave": 1})
    run_log.prune_runs(keep=2)
    remaining = [p.name for p in run_log.RUNS_ROOT.iterdir() if p.is_dir()]
    assert len(remaining) == 2
