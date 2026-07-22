#!/usr/bin/env python3
"""Concurrency + self-heal tests for the shared-WAL SQLite layer.

Covers the multi-MCP-server contention scenario: several processes open ONE
cache.db, plus the lock-vs-corruption distinction and quarantine-on-corruption.
"""
from __future__ import annotations

import glob
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.db import Database  # noqa: E402


def _writer_worker(repo_root: str, db_path: str, idx: int, rows: int, result_q) -> None:
    """Subprocess entry: open the shared DB and write rows; report outcome on queue."""
    sys.path.insert(0, repo_root)
    try:
        from shared.db import Database as _Database

        db = _Database(Path(db_path))
        for r in range(rows):
            with db.conn() as conn:
                conn.execute(
                    "INSERT INTO cache(key, task, result, model, ts) VALUES (?,?,?,?,?)",
                    (f"p{idx}-r{r}", "t", "res", "m", time.time()),
                )
        db.close()
        result_q.put((idx, "ok"))
    except Exception as exc:  # pragma: no cover - reported to parent
        import traceback

        result_q.put((idx, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"))


def test_concurrent_processes_no_corruption() -> None:
    """4 processes writing to one shared DB → no corruption, no false recovery."""
    repo_root = str(Path(__file__).resolve().parent.parent)
    with tempfile.TemporaryDirectory() as d:
        db_path = str(Path(d) / "cache.db")
        # Initialize once so the schema exists before the race.
        Database(Path(db_path)).close()

        ctx = multiprocessing.get_context("spawn")
        n_procs, rows = 4, 15
        result_q = ctx.Queue()
        procs = [
            ctx.Process(target=_writer_worker, args=(repo_root, db_path, i, rows, result_q))
            for i in range(n_procs)
        ]
        for p in procs:
            p.start()
        results = {}
        for _ in range(n_procs):
            idx, status = result_q.get(timeout=60)
            results[idx] = status
        for p in procs:
            p.join(timeout=60)

        failures = {i: s for i, s in results.items() if s != "ok"}
        assert not failures, failures
        assert len(results) == n_procs
        # No process falsely quarantined the shared DB.
        assert glob.glob(db_path + ".corrupt.*") == []

        db = Database(Path(db_path))
        assert db.last_integrity_ok is True
        with db.conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        db.close()
        assert count == n_procs * rows


def test_lock_not_misclassified_as_corruption() -> None:
    """A held write lock during init must NOT trigger destructive recovery."""
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "cache.db"
        seed = Database(db_path)
        with seed.conn() as conn:
            conn.execute(
                "INSERT INTO cache(key, task, result, model, ts) VALUES (?,?,?,?,?)",
                ("keep", "t", "r", "m", 1.0),
            )
        seed.close()

        # Hold an exclusive write transaction on a separate raw connection.
        holder = sqlite3.connect(str(db_path))
        holder.execute("PRAGMA busy_timeout=0")
        holder.execute("BEGIN EXCLUSIVE")
        try:
            db = Database(db_path)  # runs the integrity probe under contention
            # Never recovered/deleted: integrity is ok/inconclusive, data intact.
            assert db.last_integrity_ok is not False
            assert glob.glob(str(db_path) + ".corrupt.*") == []
        finally:
            holder.rollback()
            holder.close()
        with db.conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 1
        db.close()


def test_corruption_quarantined_not_deleted() -> None:
    """Genuine corruption with no backup → renamed to .corrupt.<ts>, DB recreated."""
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "cache.db"
        db = Database(db_path)
        with db.conn() as conn:
            conn.execute(
                "INSERT INTO cache(key, task, result, model, ts) VALUES (?,?,?,?,?)",
                ("k", "t", "r", "m", 1.0),
            )
        db.close()

        # Trash the file header so integrity_check reports corruption.
        with open(db_path, "r+b") as f:
            f.seek(0)
            f.write(b"\xde\xad\xbe\xef" * 400)

        db2 = Database(db_path)  # __init__ integrity check quarantines
        quarantines = glob.glob(str(db_path) + ".corrupt.*")
        assert len(quarantines) == 1, quarantines
        # Fresh DB is usable.
        with db2.conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0
        db2.close()


def test_drop_thread_local_conn_forces_reopen() -> None:
    """_drop_thread_local_conn (auto-reconnect) clears the cached conn; next op reopens."""
    with tempfile.TemporaryDirectory() as d:
        db = Database(Path(d) / "cache.db")
        with db.conn() as conn:  # prime the thread-local connection
            conn.execute("SELECT 1")
        assert hasattr(db._thread_local, "conn")
        first = db._get_connection()

        db._drop_thread_local_conn()
        assert not hasattr(db._thread_local, "conn")

        # Next call reopens a fresh, usable connection (a different object).
        with db.conn() as conn:
            assert conn is not first
            conn.execute(
                "INSERT INTO cache(key, task, result, model, ts) VALUES (?,?,?,?,?)",
                ("k", "t", "r", "m", 1.0),
            )
        with db.conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 1
        db.close()


def test_conn_reconnects_after_locked_commit(monkeypatch) -> None:
    """A DB_LOCKED commit failure in conn() drops the cached connection."""
    with tempfile.TemporaryDirectory() as d:
        db = Database(Path(d) / "cache.db")
        with db.conn() as conn:
            conn.execute("SELECT 1")
        assert hasattr(db._thread_local, "conn")

        # Force commit to always fail as locked (Connection.commit is read-only, so
        # patch the retrying primitive to raise for this call).
        import shared.db as db_mod

        def _always_locked(fn, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(db_mod, "run_with_retry", _always_locked)
        try:
            with db.conn() as conn:
                conn.execute(
                    "INSERT INTO cache(key, task, result, model, ts) VALUES (?,?,?,?,?)",
                    ("k", "t", "r", "m", 1.0),
                )
        except sqlite3.OperationalError:
            pass
        monkeypatch.undo()
        # Auto-reconnect fired: stale conn dropped from thread-local storage.
        assert not hasattr(db._thread_local, "conn")
        db.close()
