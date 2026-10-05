#!/usr/bin/env python3
"""Lock-safety tests for the SQLite layer, the db daemon and its client.

The failure these guard against was observed live: ``_restrict_db_permissions``
opened and closed ``cache.db`` / ``-wal`` / ``-shm`` on every new connection.
POSIX drops all of a process's fcntl locks on an inode when *any* descriptor
for it closes, so the daemon silently lost the locks SQLite held; a direct
opener then got EXCLUSIVE, checkpointed, and deleted the sidecars underneath
it, and every later daemon connection failed with ``disk I/O error``.
"""
from __future__ import annotations

import ast
import os
import shutil
import signal
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
from shared.db import READONLY_METHODS, RECOVERY_DECLINED_IN_USE, Database  # noqa: E402
from shared.db_client import (  # noqa: E402
    TRANSACTION_ABORTED,
    DaemonRestarted,
    RemoteConnection,
    RemoteDatabase,
)
from shared.resilience import ErrorCategory, classify_sqlite_error  # noqa: E402


def _python(script: str, *args: str, **kwargs) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)}, **kwargs,
    )


# --- 1. a new connection must not drop a peer's locks -----------------------

_HOLDER = """
import sys
from pathlib import Path
from shared.db import Database
db = Database(Path(sys.argv[1]))
reader = db._connect()
reader.execute("SELECT COUNT(*) FROM cache").fetchone()
# The trigger: opening another connection used to run _restrict_db_permissions,
# whose os.open/os.close on the db and sidecars released this process's locks.
extra = db._connect()
extra.execute("SELECT 1").fetchone()
print("ready", flush=True)
sys.stdin.readline()
try:
    n = reader.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    fresh = db._connect()
    m = fresh.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    print(f"ok {n} {m}", flush=True)
except Exception as exc:
    print(f"error {exc}", flush=True)
sys.stdin.readline()
"""

_WRITER = """
import sys
from pathlib import Path
from shared.db import Database
db = Database(Path(sys.argv[1]))
with db.conn() as c:
    c.execute("INSERT INTO cache(key, task, result, model, ts) VALUES ('k','t','r','m',1.0)")
db.close()
"""


def test_new_connection_does_not_drop_peer_locks(tmp_path: Path) -> None:
    """A peer that writes and closes must not delete the WAL a live reader uses.

    Fails on the pre-fix code: process B's close finds no lock held on the file
    (A's were dropped by the stray close), takes EXCLUSIVE and unlinks -wal and
    -shm while A still has them mapped. Verified by running this scenario
    against a scratch copy of the previous shared/ tree.
    """
    db_path = tmp_path / "cache.db"
    Database(db_path).close()
    holder = _python(_HOLDER, str(db_path))
    try:
        assert holder.stdout.readline().strip() == "ready"
        writer = _python(_WRITER, str(db_path))
        writer.communicate(timeout=60)
        assert writer.returncode == 0

        assert Path(f"{db_path}-wal").exists(), "peer deleted the WAL under a live reader"
        assert Path(f"{db_path}-shm").exists(), "peer deleted the shm under a live reader"

        holder.stdin.write("check\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "ok 1 1"
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


def test_connect_never_opens_the_live_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic in-process complement to the subprocess test above."""
    import shared.db as db_mod

    db = Database(tmp_path / "cache.db")
    live = {str(db._db_path), *(str(p) for p in db._sidecar_paths())}
    opened: list[str] = []
    real_open = os.open

    def recording_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(db_mod.os, "open", recording_open)
    try:
        conn = db._connect()
        conn.execute("SELECT 1")
        with db.conn() as c:
            c.execute("SELECT 1")
        conn.close()
    finally:
        monkeypatch.undo()
        db.close()
    assert not live.intersection(opened), opened


# --- 2. static guard: file-swapping ops only under EXCLUSIVE -----------------

# Functions in shared/db.py allowed to replace / delete / copy over the live
# database or its sidecars. Each must call self._require_exclusive(...), which
# refuses unless the access lock is held EXCLUSIVE.
EXCLUSIVE_ALLOWLIST = frozenset({
    "_discard_wal_sidecars",
    "_recover_db_exclusive",
    "_salvage_db",
})

_FD_FUNCS = {
    ("os", "open"), ("os", "replace"), ("os", "rename"), ("os", "unlink"),
    ("os", "remove"), ("shutil", "copy"), ("shutil", "copyfile"),
    ("shutil", "copy2"), ("shutil", "move"),
}
_PATH_METHODS = {"unlink", "rename", "replace", "open", "write_bytes", "write_text", "touch"}


def _is_live_path(node: ast.AST, tainted: set[str]) -> bool:
    """Does *node* evaluate to the live db, -wal or -shm path?"""
    if isinstance(node, ast.Name):
        return node.id in tainted
    if isinstance(node, ast.Attribute):
        return (
            node.attr == "_db_path"
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in {"str", "Path"} and node.args:
            return _is_live_path(node.args[0], tainted)
        if isinstance(func, ast.Attribute) and func.attr == "fspath" and node.args:
            return _is_live_path(node.args[0], tainted)
        if isinstance(func, ast.Attribute) and func.attr == "_sidecar_paths":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "with_name":
            consts = [
                n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            return _is_live_path(func.value, tainted) and any(
                c.endswith(("-wal", "-shm")) for c in consts
            )
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(
            _is_live_path(e.value if isinstance(e, ast.Starred) else e, tainted)
            for e in node.elts
        )
    if isinstance(node, ast.Subscript):
        return _is_live_path(node.value, tainted)
    return False


def _names(target: ast.AST) -> list[str]:
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]


def _violations(source: str, allowlist: frozenset[str]) -> list[str]:
    tree = ast.parse(source)
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tainted: set[str] = set()
        for _ in range(3):  # propagate through simple assignments / loops
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and _is_live_path(node.value, tainted):
                    for target in node.targets:
                        tainted.update(_names(target))
                elif isinstance(node, ast.For) and _is_live_path(node.iter, tainted):
                    tainted.update(_names(node.target))
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            hit = False
            if isinstance(func, ast.Name) and func.id == "open":
                hit = bool(node.args) and _is_live_path(node.args[0], tainted)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and (
                (func.value.id, func.attr) in _FD_FUNCS
            ):
                hit = any(_is_live_path(arg, tainted) for arg in node.args)
            elif isinstance(func, ast.Attribute) and func.attr in _PATH_METHODS:
                hit = _is_live_path(func.value, tainted)
            if hit and fn.name not in allowlist:
                found.append(f"{fn.name}:{node.lineno}")
    return found


def test_no_fd_ops_on_live_files_outside_exclusive_allowlist() -> None:
    source = (ROOT / "shared" / "db.py").read_text()
    assert _violations(source, EXCLUSIVE_ALLOWLIST) == []

    tree = ast.parse(source)
    functions = {
        n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
    }
    for name in EXCLUSIVE_ALLOWLIST:
        assert name in functions, f"allowlisted {name} no longer exists"
        calls = {
            c.func.attr for c in ast.walk(functions[name])
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
        }
        assert "_require_exclusive" in calls, f"{name} swaps files without the EXCLUSIVE gate"


def test_static_guard_is_not_vacuous() -> None:
    """The analyzer must flag the exact shape of the original defect."""
    offender = '''
class Database:
    def _restrict_db_permissions(self):
        for candidate in (self._db_path, self._db_path.with_name(f"{self._db_path.name}-wal")):
            fd = os.open(candidate, os.O_RDONLY)
            os.close(fd)

    def _wipe(self):
        wal, shm = self._sidecar_paths()
        wal.unlink()
        shutil.copyfile("x", str(self._db_path))

    def fine(self):
        backup = self._db_path.with_name(self._db_path.name + ".bak.1")
        os.unlink(backup)
'''
    assert _violations(offender, frozenset()) == [
        "_restrict_db_permissions:5", "_wipe:10", "_wipe:11",
    ]


# --- 3. daemon restarts when its files are swapped underneath it ------------

def _remote(db_path: Path, sock: str) -> RemoteDatabase:
    cfg = TGsConfig()
    cfg.db_daemon = DbDaemonConfig(
        enabled=True, socket_path=sock, idle_timeout_s=30.0, connect_timeout_s=20.0,
    )
    return RemoteDatabase(db_path, config=cfg)


def _terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            # The client Popen'd it, so it is our child: reap it, or it lingers
            # as a zombie that kill(pid, 0) still reports as alive.
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return
        except ChildProcessError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_daemon_exits_75_on_inode_swap_and_client_respawns() -> None:
    # AF_UNIX paths are capped at 104 bytes on macOS; the default tempdir is too deep.
    d = tempfile.mkdtemp(dir="/tmp")
    db_path = Path(d) / "c.db"
    sock = str(db_path) + ".sock"
    proc = subprocess.Popen(
        [sys.executable, "-m", "shared.db_daemon", str(db_path),
         "--socket", sock, "--idle-timeout", "60", "--watch-interval", "0.1"],
        cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    spawned: list[int] = []
    rdb = None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not os.path.exists(sock):
            time.sleep(0.05)
        assert os.path.exists(sock), "daemon never bound its socket"

        rdb = _remote(db_path, sock)
        assert rdb._rpc("ping")["pid"] == proc.pid
        rdb.cache_put("before", "r", "m")

        copy = Path(d) / "c.copy"
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(copy))
        src.backup(dst)
        dst.close()
        src.close()
        os.replace(copy, db_path)

        assert proc.wait(timeout=2) == 75

        rdb.cache_put("after", "r", "m")  # respawns a daemon transparently
        new_pid = rdb._rpc("ping")["pid"]
        spawned.append(new_pid)
        assert new_pid != proc.pid
        assert rdb.cache_get("before") == ("r", "m")
        assert rdb.cache_get("after") == ("r", "m")
    finally:
        if rdb is not None:
            rdb.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        for pid in spawned:
            _terminate_pid(pid)
        shutil.rmtree(d, ignore_errors=True)


# --- 4. classification -------------------------------------------------------

@pytest.mark.parametrize(
    "code, expected",
    [
        (10, ErrorCategory.DB_IOERR),           # SQLITE_IOERR
        (10 | (15 << 8), ErrorCategory.DB_IOERR),  # SQLITE_IOERR_SHMOPEN (extended)
        (14, ErrorCategory.DB_IOERR),           # SQLITE_CANTOPEN
        (1032, ErrorCategory.DB_IOERR),         # SQLITE_READONLY_DBMOVED
        (11, ErrorCategory.DB_CORRUPT),         # SQLITE_CORRUPT
        (26, ErrorCategory.DB_CORRUPT),         # SQLITE_NOTADB
        (5, ErrorCategory.DB_LOCKED),           # SQLITE_BUSY
        (6, ErrorCategory.DB_LOCKED),           # SQLITE_LOCKED
    ],
)
def test_classify_uses_sqlite_errorcode(code: int, expected: ErrorCategory) -> None:
    exc = sqlite3.OperationalError("some message that matches nothing")
    exc.sqlite_errorcode = code
    assert classify_sqlite_error(exc) == expected


@pytest.mark.parametrize(
    "message, expected",
    [
        ("disk I/O error", ErrorCategory.DB_IOERR),
        ("attempt to write a readonly database", ErrorCategory.DB_IOERR),
        ("unable to open database file", ErrorCategory.DB_IOERR),
        ("database disk image is malformed", ErrorCategory.DB_CORRUPT),
        ("file is not a database", ErrorCategory.DB_CORRUPT),
        ("database is locked", ErrorCategory.DB_LOCKED),
        ("no such table: x", ErrorCategory.UNKNOWN),
    ],
)
def test_classify_falls_back_to_message(message: str, expected: ErrorCategory) -> None:
    exc = sqlite3.OperationalError(message)
    if hasattr(exc, "sqlite_errorcode"):
        del exc.sqlite_errorcode
    assert classify_sqlite_error(exc) == expected


def test_ioerr_is_not_retried() -> None:
    from shared.resilience import RetryPolicy, run_with_retry

    calls = {"n": 0}

    def _fail():
        calls["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError):
        run_with_retry(
            _fail, classify_exc=classify_sqlite_error,
            policy=RetryPolicy(attempts=5, base_delay_s=0.0, max_delay_s=0.0),
        )
    assert calls["n"] == 1


# --- 5. access lock ----------------------------------------------------------

def test_flock_descriptions_conflict_within_one_process(tmp_path: Path) -> None:
    lock = db_locks.access_lock(tmp_path / "cache.db")
    shared_a = lock.acquire_shared(0.0)
    shared_b = lock.acquire_shared(0.0)
    assert shared_a is not None and shared_b is not None, "SHARED must be compatible"
    assert lock.try_exclusive(0.0) is None
    assert lock.probe() == "shared"
    shared_a.release()
    shared_b.release()
    exclusive = lock.try_exclusive(0.0)
    assert exclusive is not None
    assert lock.acquire_shared(0.0) is None
    assert lock.probe() == "exclusive"
    exclusive.release()
    assert lock.probe() == "unused"


def test_lock_handle_detects_replaced_lock_file(tmp_path: Path) -> None:
    lock = db_locks.access_lock(tmp_path / "cache.db")
    handle = lock.acquire_shared(0.0)
    assert handle is not None and handle.still_current()
    os.unlink(lock.path)
    lock.path.touch()
    assert not handle.still_current()
    handle.release()


def test_recovery_declines_while_access_lock_is_shared(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    db = Database(db_path)
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO cache(key, task, result, model, ts) VALUES (?,?,?,?,?)",
            ("k", "t", "r", "m", 1.0),
        )
    assert db.backup_db() is not None

    # A separate open file description: exactly what a peer process holds.
    peer = db_locks.access_lock(db_path).acquire_shared(0.0)
    assert peer is not None
    before = db_path.stat().st_ino
    assert db._recover_db(timeout_s=0.1) == RECOVERY_DECLINED_IN_USE
    assert db.last_recovery_result == RECOVERY_DECLINED_IN_USE
    assert db_path.stat().st_ino == before, "recovery swapped the file under a holder"
    # It gave back its own SHARED hold and keeps working.
    with db.conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 1

    peer.release()
    assert db._recover_db(timeout_s=1.0) == "restored"
    assert db_path.stat().st_ino != before
    with db.conn() as conn:
        assert conn.execute("SELECT key FROM cache").fetchall() == [("k",)]
    db.close()
    assert db_locks.access_lock(db_path).probe() == "unused", "close() must release SHARED"


def test_close_all_connections_reaches_other_threads(tmp_path: Path) -> None:
    db = Database(tmp_path / "cache.db")
    opened = threading.Event()
    proceed = threading.Event()
    seen: dict[str, object] = {}

    def worker() -> None:
        with db.conn() as conn:
            conn.execute("SELECT 1")
        seen["first"] = db._get_connection()
        opened.set()
        proceed.wait(10)
        with db.conn() as conn:  # must transparently reopen
            seen["count"] = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        seen["second"] = db._get_connection()

    thread = threading.Thread(target=worker)
    thread.start()
    assert opened.wait(10)
    assert db._close_all_connections() is True
    assert seen["first"].closed
    proceed.set()
    thread.join(10)
    assert seen["count"] == 0
    assert seen["second"] is not seen["first"]
    db.close()


# --- 6. client retry semantics (fake transport) ------------------------------

class _FakeTransport:
    """Scripted send/recv: each script entry is a response dict or an exception."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, rdb: RemoteDatabase, script: list) -> None:
        self.sent: list[dict] = []
        self._script = list(script)
        monkeypatch.setattr(rdb, "_sock", lambda: object())
        monkeypatch.setattr(rdb, "_drop_sock", lambda: None)
        monkeypatch.setattr(dc, "send_frame", lambda sock, obj: self.sent.append(obj))
        monkeypatch.setattr(dc, "recv_frame", self._recv)

    def _recv(self, sock):
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _restarting(applied: bool) -> dict:
    return {"ok": False, "error": {"type": "DaemonRestarting", "message": "x", "applied": applied}}


_OK = {"ok": True, "result": None}
_WRITE = "cache_put"
_READ = "cache_stats"


def _client(monkeypatch: pytest.MonkeyPatch) -> RemoteDatabase:
    rdb = RemoteDatabase("/nonexistent/cache.db", config=None)

    def _no_direct():
        raise AssertionError("must not fall back to a direct DB after a possible apply")

    monkeypatch.setattr(rdb, "_direct_db", _no_direct)
    return rdb


def test_readonly_methods_are_conservative() -> None:
    assert _READ in READONLY_METHODS
    for writer in ("cache_put", "cache_get", "plan_lookup", "get_project_settings",
                   "get_workflow_blueprint", "increment_agent_match_count"):
        assert writer not in READONLY_METHODS, writer


def test_readonly_call_is_resent_after_lost_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    rdb = _client(monkeypatch)
    t = _FakeTransport(monkeypatch, rdb, [ConnectionError("reply lost"), _OK])
    rdb._call(_READ, (), {})
    assert len(t.sent) == 2
    assert t.sent[0]["request_id"] == t.sent[1]["request_id"]


def test_write_call_is_not_resent_after_lost_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    rdb = _client(monkeypatch)
    t = _FakeTransport(monkeypatch, rdb, [ConnectionError("reply lost"), _OK])
    with pytest.raises(sqlite3.OperationalError, match="daemon restarted"):
        rdb._call(_WRITE, ("t", "r", "m"), {})
    assert len(t.sent) == 1


def test_write_call_resent_to_the_same_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    rdb = _client(monkeypatch)
    rdb._local.daemon_id = "d1"
    t = _FakeTransport(monkeypatch, rdb, [ConnectionError("reply lost"), _OK])
    monkeypatch.setattr(rdb, "_reattach", lambda daemon_id: daemon_id == "d1")
    rdb._call(_WRITE, ("t", "r", "m"), {})
    assert len(t.sent) == 2
    assert t.sent[0]["request_id"] == t.sent[1]["request_id"], "dedup needs the same id"


def test_write_call_retried_when_daemon_says_not_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    rdb = _client(monkeypatch)
    t = _FakeTransport(monkeypatch, rdb, [_restarting(applied=False), _OK])
    rdb._call(_WRITE, ("t", "r", "m"), {})
    assert len(t.sent) == 2


def test_write_call_not_retried_when_maybe_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    rdb = _client(monkeypatch)
    t = _FakeTransport(monkeypatch, rdb, [_restarting(applied=True), _OK])
    with pytest.raises(sqlite3.OperationalError, match="daemon restarted"):
        rdb._call(_WRITE, ("t", "r", "m"), {})
    assert len(t.sent) == 1


def test_readonly_call_retried_even_when_maybe_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    rdb = _client(monkeypatch)
    t = _FakeTransport(monkeypatch, rdb, [_restarting(applied=True), _OK])
    rdb._call(_READ, (), {})
    assert len(t.sent) == 2


def test_retry_happens_at_most_once(monkeypatch: pytest.MonkeyPatch) -> None:
    rdb = _client(monkeypatch)
    t = _FakeTransport(monkeypatch, rdb, [_restarting(False), _restarting(False), _OK])
    with pytest.raises(DaemonRestarted):
        rdb._rpc("call", method=_WRITE, args=[], kwargs={})
    assert len(t.sent) == 2


def test_missing_wire_fields_default_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old daemon: no daemon_id, no `applied` (treated as possibly applied)."""
    rdb = _client(monkeypatch)
    old_style = {"ok": False, "error": {"type": "DaemonRestarting", "message": "x"}}
    t = _FakeTransport(monkeypatch, rdb, [old_style])
    with pytest.raises(sqlite3.OperationalError):
        rdb._call(_WRITE, ("t", "r", "m"), {})
    assert len(t.sent) == 1


def test_session_that_wrote_reports_transaction_aborted(monkeypatch: pytest.MonkeyPatch) -> None:
    rdb = _client(monkeypatch)
    t = _FakeTransport(monkeypatch, rdb, [
        {"ok": True, "rows": [], "lastrowid": 1, "rowcount": 1},
        _restarting(applied=False),
    ])
    conn = RemoteConnection(rdb, "s1")
    conn.execute("INSERT INTO cache VALUES (?)", (1,))
    with pytest.raises(sqlite3.OperationalError, match=TRANSACTION_ABORTED):
        conn.execute("SELECT 1")
    assert len(t.sent) == 2


def test_session_without_writes_reopens_transparently(monkeypatch: pytest.MonkeyPatch) -> None:
    rdb = _client(monkeypatch)
    t = _FakeTransport(monkeypatch, rdb, [
        ConnectionError("daemon gone"),               # SELECT on the old session
        {"ok": True, "session": "s9"},                # conn_open on the new daemon
        {"ok": True, "rows": [[1]], "lastrowid": None, "rowcount": -1},
        {"ok": True},                                 # commit
    ])
    conn = RemoteConnection(rdb, "s1")
    assert conn.execute("SELECT 1").fetchall() == [[1]]
    conn.commit()
    kinds = [f["kind"] for f in t.sent]
    assert kinds == ["conn_execute", "conn_open", "conn_execute", "conn_commit"]
    assert t.sent[2]["session"] == "s9"


# --- 7. daemon-side request handling -----------------------------------------

class _CountingDB:
    def __init__(self) -> None:
        self.calls = 0

    def bump(self) -> int:
        self.calls += 1
        return self.calls

    def explode(self) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    def _recover_db(self, *a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("IOERR must never trigger recovery")


def _daemon(tmp_path: Path):
    from shared.db_daemon import DBDaemon

    daemon = DBDaemon(str(tmp_path / "cache.db"), socket_path=str(tmp_path / "s"))
    daemon._db = _CountingDB()
    return daemon


def test_daemon_memo_applies_a_resent_write_once(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    req = {"kind": "call", "method": "bump", "args": [], "kwargs": {}, "request_id": "r1"}
    first = daemon._execute(dict(req))
    again = daemon._execute(dict(req))
    assert daemon._db.calls == 1
    assert first == again and first["result"] == 1
    # Without a request_id (an old client) every frame executes.
    legacy = {k: v for k, v in req.items() if k != "request_id"}
    daemon._execute(dict(legacy))
    daemon._execute(dict(legacy))
    assert daemon._db.calls == 3


def test_daemon_ioerr_is_fatal_not_recovery(tmp_path: Path) -> None:
    from shared.db_daemon import DAEMON_RESTARTING

    daemon = _daemon(tmp_path)
    resp = daemon._execute(
        {"kind": "call", "method": "explode", "args": [], "kwargs": {}, "request_id": "r2"}
    )
    assert resp["ok"] is False
    assert resp["error"]["type"] == DAEMON_RESTARTING
    assert resp["error"]["applied"] is True  # a write-capable method failed mid-run
    assert daemon._fatal.is_set() and daemon._stop.is_set()
    pre = daemon._restarting_response(applied=False)
    assert pre["error"]["applied"] is False


def test_daemon_ignores_unknown_fields(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    resp = daemon._execute({"kind": "ping", "request_id": "r3", "from_the_future": True})
    assert resp["ok"] is True and resp["pid"] == os.getpid()


def test_overlong_socket_path_falls_back_without_waiting(tmp_path: Path) -> None:
    """AF_UNIX rejects the path permanently; polling it cost ~10s per open_database."""
    from shared.db_client import open_database

    deep = tmp_path / ("d" * 120)
    deep.mkdir()
    cfg = TGsConfig()
    cfg.db_daemon = DbDaemonConfig(enabled=True, connect_timeout_s=5.0, fallback_to_direct=True)
    started = time.monotonic()
    db = open_database(deep / "cache.db", config=cfg)
    try:
        assert isinstance(db, Database)
        assert time.monotonic() - started < 2.0
    finally:
        db.close()
