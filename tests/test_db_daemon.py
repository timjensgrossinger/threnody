#!/usr/bin/env python3
"""Tests for the single-writer DB daemon (shared/db_daemon.py + db_client.py).

Proves the daemon proxies named methods and conn() transactions correctly, that
open_database falls back to a direct DB when disabled, and — the whole point —
that many concurrent processes through the daemon incur zero SIGBUS / corruption.
"""
from __future__ import annotations

import glob
import multiprocessing
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shared.db_client as dc  # noqa: E402
from shared.config import DbDaemonConfig, TGsConfig  # noqa: E402
from shared.db import Database, PlanCacheLookup  # noqa: E402
from shared.db_client import RemoteDatabase, open_database  # noqa: E402
from shared.db_ipc import decode, encode  # noqa: E402

_ROOT = str(Path(__file__).resolve().parent.parent)


def _remote(db_path: Path, *, idle: float = 3.0) -> RemoteDatabase:
    cfg = TGsConfig()
    cfg.db_daemon = DbDaemonConfig(
        enabled=True, socket_path=str(db_path) + ".sock",
        idle_timeout_s=idle, connect_timeout_s=15.0,
    )
    return RemoteDatabase(db_path, config=cfg)


# --- codec ------------------------------------------------------------------

def test_ipc_codec_round_trip() -> None:
    for v in [None, 1, "x", 3.5, True, [1, "a"], {"k": [1, 2]}, ("a", "b"),
              Path("/tmp/x"), b"\x00\x01", {"n": ("t", 1, Path("/p"))}]:
        assert decode(encode(v)) == v


def test_ipc_codec_dataclass_round_trip() -> None:
    # Regression: a registered dataclass (e.g. PlanCacheLookup) must decode back
    # into its own type, not the generic "stringify unknown types" fallback — a
    # caller doing ``lookup.status`` on a stringified lookup raises AttributeError.
    lookup = PlanCacheLookup(status="hit", plan={"subtasks": []}, plan_schema_version=2)
    decoded = decode(encode(lookup))
    assert decoded == lookup
    assert isinstance(decoded, PlanCacheLookup)
    assert decoded.status == "hit"

    # An unregistered dataclass still crosses (as its field dict), never stringified.
    from dataclasses import dataclass

    @dataclass
    class _Unregistered:
        x: int

    decoded_unknown = decode(encode(_Unregistered(x=1)))
    assert decoded_unknown == {"x": 1}


def test_ipc_codec_non_string_and_colliding_dict_keys() -> None:
    # F2: non-string dict keys must survive (JSON objects only allow string keys,
    # so the codec falls back to a tagged association list).
    assert decode(encode({1: "a", 2: "b"})) == {1: "a", 2: "b"}
    assert decode(encode({"outer": {5: "x"}})) == {"outer": {5: "x"}}
    # F3: a user dict whose key collides with the internal type tag must NOT be
    # mis-read as a tagged envelope (Path); it round-trips as a plain dict.
    collide = {"__tgs_t__": "path", "v": "/etc/passwd"}
    assert decode(encode(collide)) == collide


# --- _rpc retry safety (F1) -------------------------------------------------

def test_rpc_does_not_resend_after_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    # A frame that was already delivered must NOT be re-sent when the response is
    # lost — a blind resend would double-apply a write (duplicate INSERT).
    rdb = RemoteDatabase("/nonexistent/cache.db", config=None)
    monkeypatch.setattr(rdb, "_sock", lambda: object())
    monkeypatch.setattr(rdb, "_drop_sock", lambda: None)
    sends: list = []
    monkeypatch.setattr(dc, "send_frame", lambda sock, obj: sends.append(obj))

    def _lost_recv(sock):
        raise ConnectionError("response lost after the daemon applied the write")

    monkeypatch.setattr(dc, "recv_frame", _lost_recv)
    with pytest.raises(ConnectionError):
        rdb._rpc("conn_execute", session="s1", sql="INSERT INTO t VALUES (1)", params=[])
    assert len(sends) == 1  # delivered exactly once, never resent


def test_rpc_retries_when_not_yet_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pre-delivery failure (stale cached socket) IS safe to retry transparently.
    rdb = RemoteDatabase("/nonexistent/cache.db", config=None)
    monkeypatch.setattr(rdb, "_sock", lambda: object())
    monkeypatch.setattr(rdb, "_drop_sock", lambda: None)
    attempts = {"send": 0}

    def _flaky_send(sock, obj):
        attempts["send"] += 1
        if attempts["send"] == 1:
            raise ConnectionError("stale socket, nothing sent")

    monkeypatch.setattr(dc, "send_frame", _flaky_send)
    monkeypatch.setattr(dc, "recv_frame", lambda sock: {"ok": True, "pong": True})
    assert rdb._rpc("ping").get("pong") is True
    assert attempts["send"] == 2  # retried once after the pre-send failure


# --- named-method parity ----------------------------------------------------

def test_named_method_parity() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cache.db"
        rdb = _remote(p)
        assert rdb.ping() is True

        rdb.cache_put("task-a", "result-a", "model-x")
        assert rdb.cache_get("task-a") == ("result-a", "model-x")   # tuple survives
        assert rdb.cache_get("missing") is None
        assert isinstance(rdb.cache_stats(), dict)

        rdb.persist_swarm_run({
            "swarm_id": "sw1", "status": "planned", "topology": "star",
            "requested_agents": 3, "effective_agents": 3, "task": "t",
            "created_ts": time.time(),
        })
        summary = rdb.get_swarm_summary("sw1")
        assert summary is not None and summary.get("swarm_id") == "sw1"
        assert rdb.last_integrity_ok is True

        # Regression: plan_lookup() returns a PlanCacheLookup dataclass. Before the
        # db_ipc codec learned to tag dataclasses, this decoded to a plain string on
        # the RemoteDatabase side and `lookup.status` raised AttributeError — the
        # crash any real REVIEW: swarm hit under a claude-code caller with the db
        # daemon enabled (the live default).
        miss = rdb.plan_lookup("some task")
        assert isinstance(miss, PlanCacheLookup)
        assert miss.status == "miss"
        rdb.plan_put("some task", {"subtasks": [{"id": "1"}]}, "model-x")
        hit = rdb.plan_lookup("some task")
        assert isinstance(hit, PlanCacheLookup)
        assert hit.status == "hit"
        assert hit.plan and "subtasks" in hit.plan

        rdb.close()


def test_remote_error_reconstructed_as_sqlite() -> None:
    import sqlite3
    with tempfile.TemporaryDirectory() as d:
        rdb = _remote(Path(d) / "cache.db")
        try:
            with rdb.conn() as c:
                c.execute("INSERT INTO nonexistent_table(x) VALUES (1)")
            assert False, "expected error"
        except sqlite3.OperationalError as exc:
            assert "no such table" in str(exc).lower()
        finally:
            rdb.close()


# --- conn() proxy -----------------------------------------------------------

def test_conn_proxy_commit_rollback_lastrowid_executemany() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cache.db"
        rdb = _remote(p)

        def count() -> int:
            with rdb.conn() as c:
                return c.execute("SELECT COUNT(*) FROM cache").fetchall()[0][0]

        # commit on clean exit
        with rdb.conn() as c:
            c.execute("INSERT INTO cache(key,task,result,model,ts) VALUES(?,?,?,?,?)",
                      ("k1", "t", "r", "m", 1.0))
            assert c.execute("SELECT COUNT(*) FROM cache").fetchall()[0][0] == 1
        assert count() == 1

        # rollback on exception
        try:
            with rdb.conn() as c:
                c.execute("INSERT INTO cache(key,task,result,model,ts) VALUES(?,?,?,?,?)",
                          ("k2", "t", "r", "m", 1.0))
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert count() == 1

        # executemany
        with rdb.conn() as c:
            c.executemany("INSERT INTO cache(key,task,result,model,ts) VALUES(?,?,?,?,?)",
                          [("k3", "t", "r", "m", 1.0), ("k4", "t", "r", "m", 1.0)])
        assert count() == 3

        # lastrowid via a rowid table
        with rdb.conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS _t(id INTEGER PRIMARY KEY, v TEXT)")
            cur = c.execute("INSERT INTO _t(v) VALUES(?)", ("a",))
            assert isinstance(cur.lastrowid, int) and cur.lastrowid >= 1
        rdb.close()


# --- factory / fallback -----------------------------------------------------

def test_open_database_disabled_returns_direct() -> None:
    with tempfile.TemporaryDirectory() as d:
        cfg = TGsConfig()
        cfg.db_daemon = DbDaemonConfig(enabled=False)
        db = open_database(Path(d) / "cache.db", config=cfg)
        assert isinstance(db, Database)
        db.close()


def test_open_database_enabled_returns_remote() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cache.db"
        cfg = TGsConfig()
        cfg.db_daemon = DbDaemonConfig(
            enabled=True, socket_path=str(p) + ".sock",
            idle_timeout_s=3.0, connect_timeout_s=15.0,
        )
        db = open_database(p, config=cfg)
        assert isinstance(db, RemoteDatabase)
        db.cache_put("t", "r", "m")
        assert db.cache_get("t") == ("r", "m")
        db.close()


def test_direct_fallback_when_daemon_unreachable() -> None:
    # enabled but pointed at a socket in a dir we will not spawn into; fallback on.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cache.db"
        rdb = _remote(p)
        rdb._connect_timeout_s = 0.2  # fail fast
        rdb._socket_path = str(Path(d) / "nonexistent" / "x.sock")  # unspawnable dir
        rdb._fallback_ok = True
        # A named call must transparently fall back to a direct Database.
        rdb.cache_put("t", "r", "m")
        assert rdb.cache_get("t") == ("r", "m")
        rdb.close()


# --- concurrency (the payoff) ----------------------------------------------

def _conc_worker(root: str, db_path: str, sock: str, idx: int, writes: int) -> None:
    sys.path.insert(0, root)
    from shared.config import DbDaemonConfig as _Cfg, TGsConfig as _T
    from shared.db_client import RemoteDatabase as _R
    cfg = _T()
    cfg.db_daemon = _Cfg(enabled=True, socket_path=sock, idle_timeout_s=30.0, connect_timeout_s=30.0)
    rdb = _R(db_path, config=cfg)
    for r in range(writes):
        rdb.persist_swarm_run({
            "swarm_id": f"s-{idx}-{r}", "status": "planned", "topology": "star",
            "requested_agents": 3, "effective_agents": 3, "task": "probe",
            "created_ts": time.time(),
        })
    rdb.close()


def test_many_processes_through_daemon_no_sigbus() -> None:
    n_procs, writes = 16, 8
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "cache.db")
        sock = p + ".sock"
        ctx = multiprocessing.get_context("spawn")
        procs = [
            ctx.Process(target=_conc_worker, args=(_ROOT, p, sock, i, writes))
            for i in range(n_procs)
        ]
        for pr in procs:
            pr.start()
        for pr in procs:
            pr.join(timeout=90)
        codes = [pr.exitcode for pr in procs]
        for pr in procs:
            if pr.is_alive():
                pr.terminate()

        assert all(c == 0 for c in codes), codes  # zero SIGBUS / crashes
        assert glob.glob(p + ".corrupt.*") == []
        db = Database(Path(p))
        with db.conn() as c:
            total = c.execute("SELECT COUNT(*) FROM swarm_runs").fetchone()[0]
        db.close()
        assert total == n_procs * writes
