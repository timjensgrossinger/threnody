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


def test_close_checkpoints_the_wal() -> None:
    """close() must merge committed frames, and must not be shadowed.

    A second `def close()` later in the Database class body once overrode the
    checkpointing one. Because it closed connections without a checkpoint, every
    writer left its commits sitting in the WAL, and whichever process raced to
    close last removed that WAL along with its peers' frames — one worker's rows
    vanished wholesale despite every commit having returned. Source inspection is
    the only way to catch a redefinition; a behavioural test alone would still
    pass whenever the race did not fire.
    """
    import inspect

    source = inspect.getsource(Database.close)
    assert "_drain_wal" in source, "Database.close() no longer drains the WAL"
    drain = inspect.getsource(Database._drain_wal)
    assert "wal_checkpoint" in drain
    # A checkpoint that reports busy transfers nothing; close() must not treat
    # that as done, or the frames it was meant to merge stay in the WAL.
    assert "busy" in drain, "_drain_wal ignores the checkpoint busy result"

    class_source = inspect.getsource(Database)
    assert class_source.count("\n    def close(self)") == 1, (
        "Database defines close() more than once — the later definition silently "
        "shadows the earlier one"
    )


def test_committed_rows_survive_close(tmp_path: Path) -> None:
    """Committed rows must be visible to a fresh process-level open after close."""
    db_path = tmp_path / "cache.db"
    db = Database(db_path)
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO cache(key, task, result, model, ts) VALUES (?,?,?,?,?)",
            ("survives", "t", "r", "m", time.time()),
        )
    db.close()

    reopened = Database(db_path)
    with reopened.conn() as conn:
        keys = {row[0] for row in conn.execute("SELECT key FROM cache")}
    reopened.close()
    assert "survives" in keys


def _contention_timeout_s(n_procs: int, base_s: float = 60.0) -> float:
    """Wall-clock budget for n_procs writers contending for one WAL.

    Scales with the writer count so a busy machine starves rather than fails.
    Overridable for CI via THRENODY_TEST_CONTENTION_TIMEOUT_S.
    """
    override = os.environ.get("THRENODY_TEST_CONTENTION_TIMEOUT_S")
    if override:
        try:
            return max(1.0, float(override))
        except ValueError:
            pass
    return base_s * max(1, n_procs) / 2.0


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
        # Each writer contends with every other for the same WAL, so the wall
        # time scales with the writer count — a fixed timeout turns ordinary
        # starvation on a loaded CI box into a failure that reads like
        # corruption. Both modes were observed; only one of them is a bug.
        timeout_s = _contention_timeout_s(n_procs)
        result_q = ctx.Queue()
        procs = [
            ctx.Process(target=_writer_worker, args=(repo_root, db_path, i, rows, result_q))
            for i in range(n_procs)
        ]
        for p in procs:
            p.start()
        results = {}
        for _ in range(n_procs):
            idx, status = result_q.get(timeout=timeout_s)
            results[idx] = status
        for p in procs:
            p.join(timeout=timeout_s)

        failures = {i: s for i, s in results.items() if s != "ok"}
        assert not failures, failures
        assert len(results) == n_procs
        # No process falsely quarantined the shared DB.
        assert glob.glob(db_path + ".corrupt.*") == []

        db = Database(Path(db_path))
        assert db.last_integrity_ok is True
        with db.conn() as conn:
            stored = {row[0] for row in conn.execute("SELECT key FROM cache")}
        db.close()

        # Assert the missing SET, not the count. Every worker above reported
        # "ok", which means each of its commits returned — so a shortfall here
        # is data loss after a successful commit, and naming the exact rows is
        # the difference between a diagnosable failure and "47 != 60".
        expected = {f"p{i}-r{r}" for i in range(n_procs) for r in range(rows)}
        missing = expected - stored
        assert not missing, (
            f"{len(missing)} committed row(s) lost: "
            f"{sorted(missing)[:10]}{' ...' if len(missing) > 10 else ''} "
            f"(by worker: { {i: sum(1 for k in missing if k.startswith(f'p{i}-')) for i in range(n_procs)} })"
        )
        assert stored == expected


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


def _crashed_writer(db_path: str, rows: int) -> None:
    """Subprocess entry: fill the WAL, then die without checkpointing it."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache("
        "key TEXT PRIMARY KEY, task TEXT, result TEXT, model TEXT, ts REAL)"
    )
    for i in range(rows):
        conn.execute(
            "INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
            (f"wal{i}", "t", "x" * 200, "m", 1.0),
        )
    conn.commit()
    os._exit(9)  # crash: the -wal survives uncheckpointed


def _leave_hot_wal(db_path: Path, rows: int = 3000) -> int:
    """Leave a genuine uncheckpointed WAL next to ``db_path``. Returns its size."""
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_crashed_writer, args=(str(db_path), rows))
    proc.start()
    proc.join(timeout=60)
    wal = db_path.with_name(db_path.name + "-wal")
    assert wal.exists() and wal.stat().st_size > 0, "expected a hot WAL on disk"
    return wal.stat().st_size


def test_quarantine_discards_wal_so_fresh_db_is_clean() -> None:
    """Quarantine must take the sidecars with it.

    Characterization + hygiene: SQLite happens to reset (not replay) a WAL whose
    main file is empty, so the recreated DB was already safe here. The assertion
    pins that behaviour and checks the sidecars follow the DB they belong to.
    """
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "cache.db"
        Database(db_path).close()
        _leave_hot_wal(db_path)

        # Quarantine the main file only, exactly as the old recovery path did.
        os.replace(db_path, db_path.with_name(db_path.name + ".corrupt.1"))

        db = Database(db_path)  # previously: replayed the foreign WAL
        with db.conn() as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            # The orphaned WAL's rows must NOT resurface in the fresh DB.
            assert conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0
        db.close()
        # Sidecars moved next to the quarantined DB, not left on the live name.
        assert Path(str(db_path) + ".corrupt.1-wal").exists() or not Path(
            str(db_path) + "-wal"
        ).exists()


def test_recovery_restores_backup_and_keeps_it() -> None:
    """Restore must survive a hot WAL and must not consume the backup."""
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "cache.db"
        db = Database(db_path)
        with db.conn() as conn:
            conn.execute(
                "INSERT INTO cache(key, task, result, model, ts) VALUES (?,?,?,?,?)",
                ("good", "t", "r", "m", 1.0),
            )
        backup = db.backup_db()
        assert backup is not None
        db.close()

        _leave_hot_wal(db_path)  # 3000 rows sitting in an uncheckpointed WAL

        Database(db_path)._recover_db()

        # Copied, not moved: a consumed backup leaves nothing to retry with.
        assert Path(backup).exists()

        db2 = Database(db_path)
        with db2.conn() as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            rows = conn.execute("SELECT key FROM cache").fetchall()
        db2.close()
        # Exactly the backup's contents — not the WAL's 3000 rows.
        assert rows == [("good",)], rows


def test_recovery_declines_while_another_process_holds_db() -> None:
    """Recovery must not mutate the DB file while a peer connection is attached.

    The process lock only serializes processes that take it; a peer merely holding
    a connection does not. Replacing the file or deleting its WAL underneath that
    reader invalidates its shm mapping (``disk I/O error``).
    """
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "cache.db"
        db = Database(db_path)
        with db.conn() as conn:
            conn.execute(
                "INSERT INTO cache(key, task, result, model, ts) VALUES (?,?,?,?,?)",
                ("k", "t", "r", "m", 1.0),
            )
        assert db.backup_db() is not None
        db.close()

        holder = sqlite3.connect(str(db_path))
        holder.execute("BEGIN EXCLUSIVE")
        try:
            before = db_path.stat().st_mtime_ns
            Database(db_path)._recover_db()
            assert db_path.stat().st_mtime_ns == before, "recovery mutated a shared DB"
            assert glob.glob(str(db_path) + ".corrupt.*") == []
        finally:
            holder.rollback()
            holder.close()


def test_orphaned_wal_discarded_when_main_db_missing() -> None:
    """A WAL beside a missing/empty main DB cannot belong to it → discard it."""
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "cache.db"
        Database(db_path).close()
        _leave_hot_wal(db_path)
        db_path.unlink()  # DB gone out of band; sidecars stranded

        db = Database(db_path)
        wal = db_path.with_name(db_path.name + "-wal")
        # The stranded frames were dropped rather than replayed into the new DB.
        with db.conn() as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0
        db.close()
        assert not wal.exists() or wal.stat().st_size == 0


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
