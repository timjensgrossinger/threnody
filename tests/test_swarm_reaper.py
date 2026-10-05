"""Stale swarm-run reaper: Database.reap_stale_swarm_runs + warm-path throttle + status."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from shared import eval as eval_mod
from shared import run_log
from shared.config import TGsConfig
from shared.db import Database
from shared.host_plan_expand import expand_host_plan
from shared.status import _load_swarm_run_summary

DAY = 86400.0


def _seed(db: Database, swarm_id: str, *, status: str, age_s: float, now: float) -> None:
    db.persist_swarm_run(
        {"swarm_id": swarm_id, "status": status, "created_ts": now - age_s}
    )


def _status(db: Database, swarm_id: str) -> tuple[str, str]:
    summary = db.get_swarm_summary(swarm_id)
    assert summary is not None
    return str(summary["status"]), str(summary["resume_status"])


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def test_old_active_runs_reaped(db: Database, tmp_path: Path) -> None:
    now = time.time()
    for sid, status in (
        ("swarm-await", "awaiting_host_execution"),
        ("swarm-run", "running"),
        ("swarm-plan", "planned"),
    ):
        _seed(db, sid, status=status, age_s=2 * DAY, now=now)

    reaped = db.reap_stale_swarm_runs(now=now, runs_root=tmp_path / "runs")

    assert sorted(reaped) == ["swarm-await", "swarm-plan", "swarm-run"]
    for sid in reaped:
        assert _status(db, sid) == ("abandoned", "abandoned")
    # Idempotent: nothing left to reap.
    assert db.reap_stale_swarm_runs(now=now, runs_root=tmp_path / "runs") == []


def test_recent_and_terminal_runs_untouched(db: Database, tmp_path: Path) -> None:
    now = time.time()
    _seed(db, "swarm-recent", status="awaiting_host_execution", age_s=3600, now=now)
    _seed(db, "swarm-done", status="completed", age_s=5 * DAY, now=now)
    _seed(db, "swarm-failed", status="failed", age_s=5 * DAY, now=now)

    assert db.reap_stale_swarm_runs(now=now, runs_root=tmp_path / "runs") == []
    assert _status(db, "swarm-recent")[0] == "awaiting_host_execution"
    assert _status(db, "swarm-done")[0] == "completed"
    assert _status(db, "swarm-failed")[0] == "failed"


def test_old_run_with_recent_run_dir_activity_untouched(db: Database, tmp_path: Path) -> None:
    now = time.time()
    root = tmp_path / "runs"
    _seed(db, "swarm-live", status="running", age_s=3 * DAY, now=now)
    _seed(db, "swarm-dead", status="running", age_s=3 * DAY, now=now)

    live_dir = root / "swarm-live" / "artifacts"
    live_dir.mkdir(parents=True)
    (live_dir / "a1.md").write_text("output", encoding="utf-8")
    dead_dir = root / "swarm-dead"
    dead_dir.mkdir(parents=True)
    meta = dead_dir / "meta.json"
    meta.write_text("{}", encoding="utf-8")
    old = now - 2 * DAY
    for path in (meta, dead_dir):
        os.utime(path, (old, old))
    # The recent file lives a level down; age everything above it.
    for path in (live_dir, root / "swarm-live"):
        os.utime(path, (old, old))

    reaped = db.reap_stale_swarm_runs(now=now, runs_root=root)

    assert reaped == ["swarm-dead"]
    assert _status(db, "swarm-live")[0] == "running"


def test_recent_active_pointer_keeps_run(db: Database, tmp_path: Path) -> None:
    now = time.time()
    root = tmp_path / "runs"
    root.mkdir()
    _seed(db, "plan-abc", status="awaiting_host_execution", age_s=2 * DAY, now=now)
    (root / "active-0123456789ab.json").write_text(
        json.dumps({"run_id": "plan-abc", "ts": now - 60}), encoding="utf-8"
    )

    assert db.reap_stale_swarm_runs(now=now, runs_root=root) == []
    assert (root / "active-0123456789ab.json").exists()


def test_hook_appends_do_not_keep_run_alive(db: Database, tmp_path: Path) -> None:
    """A stale pointer makes the hook append every edit to a dead run; those
    lines must not count as activity, and reaping must drop the pointer."""
    now = time.time()
    root = tmp_path / "runs"
    run_dir = root / "plan-dead"
    run_dir.mkdir(parents=True)
    with open(run_dir / "wave.jsonl", "w", encoding="utf-8") as fh:
        for _ in range(3):
            fh.write(json.dumps({"source": run_log.HOOK_SOURCE, "ts": now - 30}) + "\n")
    pointer = root / "active-aaaaaaaaaaaa.json"
    pointer.write_text(
        json.dumps({"run_id": "plan-dead", "ts": now - 4 * DAY}), encoding="utf-8"
    )
    _seed(db, "plan-dead", status="awaiting_host_execution", age_s=4 * DAY, now=now)

    assert db.reap_stale_swarm_runs(now=now, runs_root=root) == ["plan-dead"]
    assert not pointer.exists()


def test_non_hook_record_keeps_run_alive(db: Database, tmp_path: Path) -> None:
    now = time.time()
    root = tmp_path / "runs"
    run_dir = root / "swarm-model"
    run_dir.mkdir(parents=True)
    (run_dir / "wave.jsonl").write_text(
        json.dumps({"wave": 1, "spawn_id": "a1", "ts": now - 60}) + "\n", encoding="utf-8"
    )
    _seed(db, "swarm-model", status="running", age_s=2 * DAY, now=now)

    assert db.reap_stale_swarm_runs(now=now, runs_root=root) == []


def test_pointers_to_terminal_runs_removed(db: Database, tmp_path: Path) -> None:
    now = time.time()
    root = tmp_path / "runs"
    root.mkdir()
    _seed(db, "swarm-done", status="completed", age_s=60, now=now)
    _seed(db, "swarm-busy", status="running", age_s=60, now=now)
    for name, rid in (
        ("active-000000000001.json", "swarm-done"),
        ("active-000000000002.json", "swarm-busy"),
        ("active-000000000003.json", "swarm-unknown"),
    ):
        (root / name).write_text(json.dumps({"run_id": rid, "ts": now}), encoding="utf-8")

    assert db.reap_stale_swarm_runs(now=now, runs_root=root) == []
    assert sorted(p.name for p in root.glob("active*.json")) == [
        "active-000000000002.json",
        "active-000000000003.json",
    ]


def test_recent_swarm_event_keeps_run(db: Database, tmp_path: Path) -> None:
    now = time.time()
    _seed(db, "swarm-evt", status="running", age_s=2 * DAY, now=now)
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO swarm_events (swarm_id, event_type, payload, ts) VALUES (?, ?, ?, ?)",
            ("swarm-evt", "wave_progress", "{}", now - 60),
        )

    assert db.reap_stale_swarm_runs(now=now, runs_root=tmp_path / "runs") == []


def test_concurrent_completion_not_overwritten(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that completes between the candidate scan and the UPDATE stays completed."""
    now = time.time()
    root = tmp_path / "runs"
    root.mkdir()
    _seed(db, "swarm-race", status="running", age_s=2 * DAY, now=now)

    real_glob = Path.glob

    def _glob_then_complete(self: Path, pattern: str):
        # The pointer scan runs after the SELECT and before the UPDATE.
        if self == root and pattern == "active*.json":
            db.persist_swarm_run({"swarm_id": "swarm-race", "status": "completed"})
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", _glob_then_complete)

    assert db.reap_stale_swarm_runs(now=now, runs_root=root) == []
    assert _status(db, "swarm-race")[0] == "completed"


def test_expand_host_plan_rejects_abandoned(db: Database, tmp_path: Path) -> None:
    now = time.time()
    _seed(db, "swarm-gone", status="awaiting_host_execution", age_s=2 * DAY, now=now)
    db.reap_stale_swarm_runs(now=now, runs_root=tmp_path / "runs")

    with pytest.raises(ValueError, match="not expandable"):
        expand_host_plan(
            db,
            run_id="swarm-gone",
            discovered_files=["a.py"],
            workspace_root=str(tmp_path),
            config=TGsConfig(),
        )


def test_status_snapshot_counts(db: Database, tmp_path: Path) -> None:
    now = time.time()
    _seed(db, "s-old-1", status="awaiting_host_execution", age_s=2 * DAY, now=now)
    _seed(db, "s-old-2", status="running", age_s=2 * DAY, now=now)
    _seed(db, "s-new", status="running", age_s=60, now=now)
    _seed(db, "s-done", status="completed", age_s=2 * DAY, now=now)

    before = _load_swarm_run_summary(db)
    assert before["stale_active"] == 2
    assert before["abandoned"] == 0
    assert before["by_status"] == {"awaiting_host_execution": 1, "running": 2, "completed": 1}

    db.reap_stale_swarm_runs(now=now, runs_root=tmp_path / "runs")

    after = _load_swarm_run_summary(db)
    assert after["stale_active"] == 0
    assert after["abandoned"] == 2
    assert after["by_status"] == {"abandoned": 2, "running": 1, "completed": 1}


def test_status_snapshot_includes_swarm_runs(db: Database) -> None:
    from shared.status import build_status_snapshot

    snapshot = build_status_snapshot(TGsConfig(), db, "proj")
    assert snapshot["swarm_runs"] == {"by_status": {}, "abandoned": 0, "stale_active": 0}


def test_warm_path_reap_throttled(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []

    def _fake_reap(**kwargs: object) -> list[str]:
        calls.append(float(kwargs["now"]))  # type: ignore[arg-type]
        return ["swarm-x"]

    monkeypatch.setattr(db, "reap_stale_swarm_runs", _fake_reap)
    monkeypatch.setattr(eval_mod, "_LAST_SWARM_REAP_TS", None)
    clock = [1_000_000.0]
    monkeypatch.setattr(eval_mod.time, "time", lambda: clock[0])

    first = eval_mod.run_warm_path_background_tasks(db)
    assert first["swarm_reap"] == 1
    clock[0] += 3600  # within the 6 h window
    assert "swarm_reap" not in eval_mod.run_warm_path_background_tasks(db)
    clock[0] += 6 * 3600
    assert eval_mod.run_warm_path_background_tasks(db)["swarm_reap"] == 1
    assert len(calls) == 2


def test_warm_path_reap_failure_retries_next_tick(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = {"n": 0}

    def _boom(**_kwargs: object) -> list[str]:
        attempts["n"] += 1
        raise RuntimeError("daemon has no such method")

    monkeypatch.setattr(db, "reap_stale_swarm_runs", _boom)
    monkeypatch.setattr(eval_mod, "_LAST_SWARM_REAP_TS", None)

    result = eval_mod.run_warm_path_background_tasks(db)
    assert result["swarm_reap"] == {"error": "daemon has no such method"}
    eval_mod.run_warm_path_background_tasks(db)
    assert attempts["n"] == 2


def test_warm_tick_prunes_stale_pointers(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_mod, "_LAST_SWARM_REAP_TS", None)
    run_log.set_active_run("swarm-gone", workspace_root=str(tmp_path / "deleted-ws"))
    live = tmp_path / "live"
    live.mkdir()
    run_log.set_active_run("swarm-live", workspace_root=str(live))

    results = eval_mod.run_warm_path_background_tasks(db)

    assert results["pointer_prune"] == 1
    assert set(run_log.active_pointer_runs()) == {"swarm-live"}
