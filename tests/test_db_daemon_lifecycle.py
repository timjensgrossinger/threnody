#!/usr/bin/env python3
"""Daemon lifetime, admin RPC, protocol hello, constrained fallback, install helpers.

Daemon subprocesses run on short ``/tmp`` paths (AF_UNIX caps socket paths at
104 bytes on macOS) and are always killed in teardown.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import shared.db_client as dc  # noqa: E402
from shared import db_locks  # noqa: E402
from shared.config import DbDaemonConfig, TGsConfig  # noqa: E402
from shared.db import RECOVERY_DECLINED_IN_USE  # noqa: E402
from shared.db_client import (  # noqa: E402
    DaemonNotRunning,
    RemoteDatabase,
    RemoteDBError,
    db_background,
    open_database,
    probe_daemon,
)
from shared.db_daemon import DBDaemon, _ClientState  # noqa: E402
from shared.db_ipc import recv_frame, send_frame  # noqa: E402

_ENV = {**os.environ, "PYTHONPATH": str(ROOT)}


# --- helpers ----------------------------------------------------------------

@pytest.fixture
def short_dir():
    d = tempfile.mkdtemp(dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _cfg(db_path: Path, **daemon) -> TGsConfig:
    cfg = TGsConfig()
    cfg.db_path = db_path
    cfg.db_daemon = DbDaemonConfig(
        enabled=True, socket_path=str(db_path) + ".sock",
        idle_timeout_s=60.0, connect_timeout_s=10.0, **daemon,
    )
    return cfg


def _start_daemon(db_path: Path, *extra: str) -> subprocess.Popen:
    sock = str(db_path) + ".sock"
    proc = subprocess.Popen(
        [sys.executable, "-m", "shared.db_daemon", str(db_path), "--socket", sock,
         "--idle-timeout", "60", "--watch-interval", "0.1", *extra],
        cwd=str(ROOT), env=_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not os.path.exists(sock):
        if proc.poll() is not None:
            raise AssertionError(f"daemon exited early ({proc.returncode})")
        time.sleep(0.05)
    assert os.path.exists(sock), "daemon never bound its socket"
    return proc


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def _terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return
        except ChildProcessError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        time.sleep(0.05)


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FakeConn:
    closed = False

    def close(self) -> None:
        self.closed = True

    def rollback(self) -> None:
        pass


class _FakeDB:
    def _connect(self):
        return _FakeConn()


# --- 1. idle: background frames never count; open sessions block -------------

def test_idle_ignores_background_frames_and_honours_open_sessions(tmp_path: Path) -> None:
    clock = _Clock()
    daemon = DBDaemon(str(tmp_path / "c.db"), socket_path=str(tmp_path / "s"),
                      idle_timeout_s=10.0, max_lifetime_s=0, clock=clock)
    daemon._db = _FakeDB()
    state = _ClientState()
    daemon._serve_one({"kind": "hello", "protocol": 2}, state)

    clock.now += 5
    daemon._serve_one({"kind": "ping", "bg": True}, state)  # background: no refresh
    assert daemon._idle_due(clock.now + 6), "a bg frame kept the daemon alive"

    clock.now += 3
    daemon._serve_one({"kind": "ping"}, state)  # foreground refresh
    assert not daemon._idle_due(clock.now + 9)
    assert daemon._idle_due(clock.now + 11)

    # A connected-but-silent client no longer blocks idle; an open session does.
    assert daemon._clients == 0  # (this unit never went through _handle_client)
    daemon._serve_one({"kind": "conn_open"}, state)
    assert daemon._open_sessions == 1
    assert not daemon._idle_due(clock.now + 1000)
    sid = next(iter(state.sessions))
    daemon._serve_one({"kind": "conn_close", "session": sid}, state)
    assert daemon._open_sessions == 0
    assert daemon._idle_due(clock.now + 11)
    assert daemon.status()["requests"] == {"fg": 3, "bg": 1}


def test_max_lifetime_waits_for_open_sessions(tmp_path: Path) -> None:
    clock = _Clock()
    daemon = DBDaemon(str(tmp_path / "c.db"), socket_path=str(tmp_path / "s"),
                      idle_timeout_s=0, max_lifetime_s=100.0, clock=clock)
    daemon._db = _FakeDB()
    state = _ClientState()
    assert not daemon._recycle_due(clock.now + 99)
    daemon._serve_one({"kind": "conn_open"}, state)
    assert not daemon._recycle_due(clock.now + 500), "recycled under an open session"
    daemon._serve_one({"kind": "conn_close", "session": next(iter(state.sessions))}, state)
    assert daemon._recycle_due(clock.now + 500)
    assert not daemon._idle_due(clock.now + 10**6), "idle_timeout 0 means never"


def test_max_lifetime_recycles_and_client_moves_to_successor(short_dir: Path) -> None:
    db_path = short_dir / "c.db"
    proc = _start_daemon(db_path, "--max-lifetime", "1")
    spawned: list[int] = []
    rdb = RemoteDatabase(db_path, config=_cfg(db_path))
    try:
        assert rdb._rpc("ping")["pid"] == proc.pid
        rdb.cache_put("k", "r", "m")
        assert proc.wait(timeout=10) == 0, "recycle must be a clean exit"
        assert not os.path.exists(str(db_path) + ".sock"), "handover left the socket"
        assert db_locks.access_lock(db_path).probe() == "unused", "access lock not released"
        rdb.cache_put("k2", "r", "m")  # transparently reaches a fresh daemon
        pid = rdb._rpc("ping")["pid"]
        spawned.append(pid)
        assert pid != proc.pid
        assert rdb.cache_get("k") == ("r", "m")
    finally:
        rdb.close()
        _stop(proc)
        for pid in spawned:
            _terminate_pid(pid)


# --- 2. admin RPC --------------------------------------------------------------

def test_admin_status_checkpoint_repair_and_shutdown(short_dir: Path) -> None:
    db_path = short_dir / "c.db"
    proc = _start_daemon(db_path)
    rdb = RemoteDatabase(db_path, config=_cfg(db_path))
    try:
        rdb.cache_put("k", "r", "m")
        assert rdb.backup_db() is not None

        st = rdb.daemon_status()
        assert st["pid"] == proc.pid and st["protocol"] == 2
        assert st["legacy_clients"] == 0
        for key in ("version", "uptime_s", "files", "last_error", "requests",
                    "sessions_open", "access_lock", "last_checkpoint"):
            assert key in st, key
        assert all(v["recorded"] == v["current"] for v in st["files"].values())
        ckpt = rdb.admin("checkpoint", mode="PASSIVE")
        assert ckpt["mode"] == "PASSIVE"

        # Another process holds the database: repair must decline, touch nothing,
        # and the daemon must keep serving (no self-inflicted restart).
        peer = db_locks.access_lock(db_path).acquire_shared(0.0)
        before = db_path.stat().st_ino
        try:
            assert rdb.repair(timeout_s=0.2) == RECOVERY_DECLINED_IN_USE
        finally:
            peer.release()
        assert db_path.stat().st_ino == before
        time.sleep(0.4)  # several watchdog ticks
        assert proc.poll() is None
        assert rdb.cache_get("k") == ("r", "m")

        # Nobody else holds it now: the daemon quiesces itself and restores.
        assert rdb.repair(timeout_s=1.0) == "restored"
        assert db_path.stat().st_ino != before
        time.sleep(0.4)
        assert proc.poll() is None, "the daemon's own repair tripped its watchdog"
        assert rdb.cache_get("k") == ("r", "m")
        assert rdb.daemon_status()["paused"] is False

        # `threnody db status` against the running daemon.
        out = subprocess.run(
            [sys.executable, "-m", "shared.db_cli", "status", "--db", str(db_path), "--json"],
            cwd=str(ROOT), env=_ENV, capture_output=True, text=True, timeout=60,
        )
        info = json.loads(out.stdout)
        assert info["running"] is True and info["status"]["pid"] == proc.pid

        assert rdb.admin("shutdown")["stopping"] is True
        assert proc.wait(timeout=10) == 0
    finally:
        rdb.close()
        _stop(proc)


def test_destructive_admin_refused_for_a_legacy_connection(tmp_path: Path) -> None:
    daemon = DBDaemon(str(tmp_path / "c.db"), socket_path=str(tmp_path / "s"))
    daemon._db = _FakeDB()
    state = _ClientState()  # never says hello
    resp = daemon._serve_one({"kind": "admin", "op": "shutdown"}, state)
    assert resp["ok"] is False and "hello" in resp["error"]["message"]
    assert not daemon._stop.is_set()
    assert daemon._legacy_clients == 1
    ok = daemon._serve_one({"kind": "admin", "op": "status"}, state)
    assert ok["ok"] is True and ok["result"]["legacy_clients"] == 1


# --- 3. protocol hello -----------------------------------------------------------

def _scripted_peer(replies: list[dict]) -> tuple[socket.socket, threading.Thread, list]:
    client, server = socket.socketpair()
    seen: list[dict] = []

    def run() -> None:
        try:
            for reply in replies:
                seen.append(recv_frame(server))
                send_frame(server, reply)
        except (ConnectionError, OSError):
            pass
        finally:
            server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return client, thread, seen


def test_client_falls_back_to_legacy_mode_for_an_old_daemon(monkeypatch) -> None:
    old_daemon_hello = {"ok": False, "error": {"type": "ProtocolError",
                                               "message": "unknown request kind: 'hello'"}}
    client, thread, seen = _scripted_peer([old_daemon_hello, {"ok": True, "pong": True}])
    rdb = RemoteDatabase("/tmp/nonexistent-threnody/c.db", config=None)
    monkeypatch.setattr(rdb, "_connect", lambda: client)
    assert rdb.ping() is True
    assert rdb.peer()["legacy"] is True
    assert seen[0]["kind"] == "hello" and seen[0]["protocol"] == 2
    with pytest.raises(RemoteDBError, match="legacy"):
        rdb.admin("shutdown")
    assert [f["kind"] for f in seen] == ["hello", "ping"], "refused op must not be sent"
    rdb.close()


def test_client_refuses_a_daemon_serving_another_database(monkeypatch) -> None:
    hello = {"ok": True, "protocol": 2, "version": "x", "pid": 1, "db_path": "/elsewhere/c.db"}
    client, _thread, _seen = _scripted_peer([hello])
    rdb = RemoteDatabase("/tmp/nonexistent-threnody/c.db", config=None)
    monkeypatch.setattr(rdb, "_connect", lambda: client)
    with pytest.raises(dc.DaemonUnreachable, match="serves"):
        rdb._sock()


# --- 4. constrained fallback -------------------------------------------------------

def _unspawnable(base: Path, mode: str) -> RemoteDatabase:
    """A short socket path in a directory that does not exist: spawns cannot bind."""
    db_path = base / "c.db"
    cfg = _cfg(db_path, fallback_mode=mode)
    cfg.db_daemon.socket_path = str(base / "missing-dir" / "x.sock")
    rdb = RemoteDatabase(db_path, config=cfg)
    rdb._connect_timeout_s = 0.3
    return rdb


def test_direct_fallback_is_constrained_and_holds_the_access_lock(short_dir: Path) -> None:
    rdb = _unspawnable(short_dir, "direct")
    try:
        rdb.cache_put("k", "r", "m")
        assert rdb.in_fallback
        direct = rdb._direct
        assert direct._maintenance is False and direct._persistent_connections is False
        assert direct._open_conn_count() == 0, "fallback kept a connection between ops"
        assert db_locks.access_lock(short_dir / "c.db").probe() == "shared"
        assert rdb.cache_get("k") == ("r", "m")
        with rdb.conn() as c:
            assert c.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 1
    finally:
        rdb.close()


def test_readonly_fallback_refuses_writes(short_dir: Path) -> None:
    from shared.db import Database

    seed = Database(short_dir / "c.db")
    seed.cache_stats()  # creates the schema
    seed.close()
    rdb = _unspawnable(short_dir, "readonly")
    try:
        assert rdb.cache_stats()["total_cached"] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            rdb.cache_put("k", "r", "m")
    finally:
        rdb.close()


def test_fallback_off_raises(short_dir: Path) -> None:
    rdb = _unspawnable(short_dir, "off")
    with pytest.raises(ConnectionError):
        rdb.cache_put("k", "r", "m")
    assert not rdb.in_fallback


def test_open_database_returns_remote_on_fallback_and_switches_back(
    short_dir: Path, monkeypatch
) -> None:
    db_path = short_dir / "c.db"
    cfg = _cfg(db_path)
    cfg.db_daemon.socket_path = str(short_dir / "missing-dir" / "x.sock")
    cfg.db_daemon.connect_timeout_s = 0.3
    db = open_database(db_path, config=cfg)
    assert isinstance(db, RemoteDatabase) and db.in_fallback
    db.cache_put("k", "r", "m")
    # Within the re-probe window the fallback is used without touching the socket.
    monkeypatch.setattr(db, "_daemon_is_live", lambda: (_ for _ in ()).throw(AssertionError))
    db.cache_get("k")
    # Window elapsed and the daemon is back: leave the fallback.
    monkeypatch.setattr(db, "_daemon_is_live", lambda: True)
    db._next_probe = 0.0
    assert db._active_fallback() is None and not db.in_fallback
    db.close()


def test_fallback_off_makes_open_database_raise(short_dir: Path) -> None:
    db_path = short_dir / "c.db"
    cfg = _cfg(db_path, fallback_mode="off")
    cfg.db_daemon.socket_path = str(short_dir / "missing-dir" / "x.sock")
    cfg.db_daemon.connect_timeout_s = 0.3
    with pytest.raises(ConnectionError):
        open_database(db_path, config=cfg)


def test_background_requests_never_spawn_or_open_a_fallback(short_dir: Path, monkeypatch) -> None:
    rdb = _unspawnable(short_dir, "direct")
    monkeypatch.setattr(rdb, "_spawn_daemon", lambda: pytest.fail("bg request spawned a daemon"))
    with db_background():
        with pytest.raises(DaemonNotRunning):
            rdb.cache_stats()
        assert dc.background_db_available(rdb) is False
    assert not rdb.in_fallback


def test_respawn_is_bounded(short_dir: Path, monkeypatch) -> None:
    rdb = _unspawnable(short_dir, "off")
    rdb._connect_timeout_s = 4.0
    spawns: list[float] = []
    monkeypatch.setattr(rdb, "_spawn_daemon", lambda: spawns.append(time.monotonic()))
    with pytest.raises(dc.DaemonUnreachable):
        rdb._connect()
    assert len(spawns) == 3
    gaps = [b - a for a, b in zip(spawns, spawns[1:])]
    assert gaps[0] >= 0.45 and gaps[1] >= 0.95, gaps  # 0.5 s, then 1 s backoff


# --- 5. `threnody db status` with no daemon -----------------------------------------

def test_db_status_without_daemon_reports_election_lock(short_dir: Path) -> None:
    db_path = short_dir / "c.db"
    info = probe_daemon(db_path)
    assert info["running"] is False and info["election_lock"] == "free"
    held = db_locks.acquire(db_locks.election_lock_path(db_path), db_locks.EXCLUSIVE, 0.0)
    try:
        out = subprocess.run(
            [sys.executable, "-m", "shared.db_cli", "status", "--db", str(db_path)],
            cwd=str(ROOT), env=_ENV, capture_output=True, text=True, timeout=60,
        )
        assert "daemon: not running" in out.stdout
        assert "election_lock: held" in out.stdout
    finally:
        held.release()


# --- 6. repair reporting ------------------------------------------------------------

def test_cli_and_doctor_report_a_declined_repair(capsys) -> None:
    from shared import db_cli, doctor

    assert db_cli._print_repair(RECOVERY_DECLINED_IN_USE) == 1
    out = capsys.readouterr().out
    assert f"result: {RECOVERY_DECLINED_IN_USE}" in out and "result: ok" not in out
    assert db_cli._print_repair("restored") == 0

    class _Corrupt:
        _db_path = None
        last_backup_ts = time.time()

        def check(self):
            return {"integrity_ok": False}

        def repair(self, timeout_s=None):
            return RECOVERY_DECLINED_IN_USE

        def backup_db(self):
            return None

    doctor.run_self_repair(_Corrupt())
    assert RECOVERY_DECLINED_IN_USE in capsys.readouterr().out


def test_swarm_init_maps_ioerr_to_its_own_error() -> None:
    import mcp_server

    resp = mcp_server._swarm_init_error_response(TGsConfig(), sqlite3.OperationalError("disk I/O error"))
    assert resp["error"] == "db_io_error" and resp["retryable"] is True


# --- 7. install.sh helpers ----------------------------------------------------------

_INSTALL = (ROOT / "install.sh").read_text()


def _heredoc(opening: str, terminator: str) -> str:
    start = _INSTALL.index(opening)
    body_start = _INSTALL.index("\n", start) + 1
    end = _INSTALL.index(f"\n{terminator}\n", body_start)
    return _INSTALL[body_start:end] + "\n"


def test_portable_copy_keeps_every_runtime_file(tmp_path: Path) -> None:
    script = _heredoc('python3 - "$SOURCE_DIR" "$INSTALL_DIR" <<\'PY\'', "PY")
    source, target = tmp_path / "src", tmp_path / "dst"
    (source / "shared").mkdir(parents=True)
    (source / "shared" / "x.py").write_text("x = 1\n")
    keep = [
        "cache.db", "cache.db-wal", "cache.db-shm", "cache.db.bak.1", "cache.db.lock",
        "cache.db.daemon.lock", "cache.db.access.lock", "cache.db.sock", "config.yaml",
        "audit_secret",
    ]
    keep_dirs = ["journal", "runs", "logs", "worktrees", ".runtime", "backup"]
    target.mkdir()
    for name in keep:
        (target / name).write_text("data")
    for name in keep_dirs:
        (target / name).mkdir()
        (target / name / "f").write_text("data")
    (target / "stale.py").write_text("old")
    subprocess.run([sys.executable, "-c", script, str(source), str(target)], check=True)
    for name in keep + keep_dirs:
        assert (target / name).exists(), f"portable copy deleted {name}"
    assert not (target / "stale.py").exists()
    assert (target / "shared" / "x.py").exists()

    # rsync path: the same runtime state must be excluded from --delete.
    for name in ("journal/", "runs/", "logs/", "worktrees/", ".runtime/", "audit_secret", "cache.db*"):
        assert f"--exclude='{name}'" in _INSTALL, name


def test_preinstall_stops_the_daemon_then_backs_up(short_dir: Path) -> None:
    script = _heredoc('python3 - "$SOURCE_DIR" "$INSTALL_DIR" <<\'PYEOF\'', "PYEOF")
    base = short_dir / "i"
    base.mkdir()
    db_path = base / "cache.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE marker (v TEXT)")
        conn.execute("INSERT INTO marker VALUES ('kept')")
    proc = _start_daemon(db_path)
    try:
        out = subprocess.run(
            [sys.executable, "-c", script, str(ROOT), str(base)],
            env={**_ENV, "HOME": str(short_dir)}, capture_output=True, text=True, timeout=60,
        )
        assert "admin shutdown — stopped" in out.stdout, out.stdout + out.stderr
        assert proc.wait(timeout=10) == 0
        backups = list(base.glob("cache.db.bak.*"))
        assert len(backups) == 1, out.stdout
        with sqlite3.connect(backups[0]) as conn:
            assert conn.execute("SELECT v FROM marker").fetchall() == [("kept",)]
        assert backups[0].stat().st_mode & 0o777 == 0o600
    finally:
        _stop(proc)


def test_install_prints_restart_hint() -> None:
    assert re.search(r"Restart Claude Code / MCP sessions", _INSTALL)
