#!/usr/bin/env python3
"""Client side of the single-writer DB daemon.

``RemoteDatabase`` is a drop-in for ``shared.db.Database``: every public method
is proxied to the daemon over a per-thread Unix socket, and ``conn()`` yields a
``RemoteConnection`` that forwards execute/fetch/commit round-trips. Call sites
using either the 114 named methods or ``with db.conn() as c: c.execute(...)`` work
unchanged.

``open_database()`` is the factory the rest of the codebase should call: it
returns a ``RemoteDatabase`` when the daemon is enabled and reachable (spawning
it on demand), else a direct ``Database`` — so the feature is safe to ship dark.

Retry rules across a daemon restart (``DaemonRestarting`` or a lost connection).
A request is re-sent — at most once, to a respawned daemon — only when that
cannot double a write:

(a) the frame was never delivered (connect / send failed);
(b) the daemon answered ``applied: false``;
(c) the request is side-effect free: ``ping`` / ``getattr`` / ``conn_open``, or a
    ``call`` of a method in ``READONLY_METHODS`` (``@db_readonly`` in db.py);
(d) the connection dropped after delivery but the *same* daemon is still up —
    the re-send carries the original ``request_id`` and the daemon's memo
    answers it instead of applying it again.

Anything else raises ``sqlite3.OperationalError`` rather than guessing; in a
``conn()`` session that has already written, the whole transaction is reported
aborted (``daemon restarted; transaction aborted``).
"""
from __future__ import annotations

import logging
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import db_locks
from .db import READONLY_METHODS
from .db_ipc import ProtocolError, decode, encode, recv_frame, send_frame
from .db_daemon import (
    DAEMON_RESTARTING,
    DAEMON_VERSION,
    DESTRUCTIVE_ADMIN_OPS,
    PROTOCOL_VERSION,
    SIDE_EFFECT_FREE_KINDS,
    socket_path_for,
)
from .db_locks import election_lock_path

__all__ = [
    "DaemonNotRunning",
    "DaemonRestarted",
    "DaemonUnreachable",
    "DeliveryUncertain",
    "READONLY_METHODS",
    "RemoteConnection",
    "RemoteDBError",
    "RemoteDatabase",
    "SocketPathTooLong",
    "db_background",
    "in_background",
    "open_database",
    "probe_daemon",
    "stop_daemon",
]

log = logging.getLogger(__name__)

# Reconstruct known sqlite exception types on the client so caller `except`
# clauses (e.g. `except sqlite3.OperationalError`) keep working across the wire.
_SQLITE_EXC: dict[str, type[BaseException]] = {
    "OperationalError": sqlite3.OperationalError,
    "IntegrityError": sqlite3.IntegrityError,
    "DatabaseError": sqlite3.DatabaseError,
    "ProgrammingError": sqlite3.ProgrammingError,
    "InterfaceError": sqlite3.InterfaceError,
    "DataError": sqlite3.DataError,
    "NotSupportedError": sqlite3.NotSupportedError,
    "Error": sqlite3.Error,
}


class RemoteDBError(sqlite3.Error):
    """Daemon-side error whose type isn't a standard sqlite3 exception."""


class DeliveryUncertain(ConnectionError):
    """The frame reached the daemon, the reply did not: it may have been applied."""


class DaemonRestarted(ConnectionError):
    """The daemon answered ``DaemonRestarting``; ``applied`` says if it may have run."""

    def __init__(self, message: str, *, applied: bool) -> None:
        super().__init__(message)
        self.applied = applied


class DaemonUnreachable(ConnectionError):
    """No daemon could be reached (respawns exhausted). Nothing was delivered."""


class SocketPathTooLong(DaemonUnreachable):
    """The socket path exceeds AF_UNIX's limit: no daemon can ever serve it."""


class DaemonNotRunning(DaemonUnreachable):
    """A background request found no daemon; background work never spawns one."""


TRANSACTION_ABORTED = "daemon restarted; transaction aborted"
# Respawn policy: at most this many spawns per connect, first retry after the
# backoff, doubling — bounded overall by connect_timeout_s.
_MAX_SPAWNS = 3
_SPAWN_BACKOFF_S = 0.5
# While on the direct fallback, how often to look for the daemon again.
FALLBACK_REPROBE_S = 30.0

_BG = threading.local()


@contextmanager
def db_background() -> Iterator[None]:
    """Mark this thread's DB requests as background (``bg: true``) for the block.

    Background frames never refresh the daemon's idle clock, never spawn a
    daemon, and never open a direct fallback of their own — so periodic
    housekeeping (health probe, warm-path loop) can neither keep an otherwise
    idle daemon alive nor resurrect it. Work that finds no daemon raises
    ``DaemonNotRunning``; callers skip the iteration and try again later.
    """
    depth = getattr(_BG, "depth", 0)
    _BG.depth = depth + 1
    try:
        yield
    finally:
        _BG.depth = depth


def in_background() -> bool:
    return getattr(_BG, "depth", 0) > 0


def mark_background_thread() -> None:
    """Make every DB request of the *calling thread* background, for good.

    For dedicated housekeeping threads, where wrapping each iteration in
    ``db_background()`` would only add indentation.
    """
    _BG.depth = max(1, getattr(_BG, "depth", 0))


def background_db_available(db: object) -> bool:
    """Can background work use *db* right now without spawning anything?

    True for a direct ``Database``, a ``RemoteDatabase`` already on its
    fallback, or one whose daemon is accepting. Lets a periodic loop skip an
    iteration quietly instead of failing every stage with ``DaemonNotRunning``.
    """
    if not isinstance(db, RemoteDatabase):
        return db is not None
    return db.in_fallback or db._daemon_is_live()
_SESSION_KINDS = frozenset(
    {"conn_execute", "conn_executemany", "conn_commit", "conn_rollback", "conn_close"}
)
# Statements a session may re-issue on a fresh daemon session. Conservative:
# only plain SELECT/EXPLAIN (a WITH clause can front an INSERT).
_READ_SQL = re.compile(r"^\s*(SELECT|EXPLAIN)\b", re.IGNORECASE)


def _may_have_applied(exc: BaseException) -> bool:
    if isinstance(exc, DaemonRestarted):
        return exc.applied
    return isinstance(exc, DeliveryUncertain)


def _raise_remote(error: dict) -> None:
    etype = str(error.get("type", "Error"))
    msg = str(error.get("message", "remote db error"))
    exc_cls = _SQLITE_EXC.get(etype)
    if exc_cls is not None:
        raise exc_cls(msg)
    raise RemoteDBError(f"{etype}: {msg}")


class RemoteCursor:
    """Materialized result of a proxied execute (rows already fetched)."""

    def __init__(self, rows: list, lastrowid: int | None, rowcount: int) -> None:
        self._rows = list(rows or [])
        self.lastrowid = lastrowid
        self.rowcount = rowcount if rowcount is not None else -1

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchmany(self, size: int = 1):
        out = self._rows[:size]
        del self._rows[:size]
        return out

    def fetchall(self):
        out = self._rows
        self._rows = []
        return out

    def __iter__(self):
        while self._rows:
            yield self._rows.pop(0)


def _positional_params(params: Any) -> list:
    """Coerce a positional param binding to a wire list, rejecting named (dict) binds.

    Named parameters (``dict`` for ``:name`` placeholders) cannot survive the
    ``list(params)`` conversion — they would silently degrade to a list of keys —
    so they are rejected loudly rather than corrupting the query.
    """
    if isinstance(params, dict):
        raise sqlite3.NotSupportedError(
            "named (dict) parameters are not supported over the DB daemon; "
            "use positional (?) parameters with a sequence binding"
        )
    return list(params) if params else []


class RemoteConnection:
    """Proxy for a live sqlite3.Connection bound to one daemon session.

    Supported surface: ``execute`` / ``executemany`` (positional ``?`` params
    only), ``commit`` / ``rollback`` / ``close``, and cursor row access via
    ``fetchone`` / ``fetchmany`` / ``fetchall`` / iteration + ``lastrowid`` /
    ``rowcount``. NOT supported: named (dict) parameters, ``cursor()``,
    ``executescript()``, ``row_factory``, and ``description`` — no current caller
    uses them, and they raise / are absent rather than silently misbehaving.

    A session lives in one daemon handler, so a daemon restart ends it. Until
    the session has written, nothing is lost by that (sqlite3 runs plain
    SELECTs outside a transaction): the statement is re-issued on a fresh
    session. After a write, the transaction is gone and the error says so.
    """

    def __init__(self, db: "RemoteDatabase", session: str) -> None:
        self._db = db
        self._session: str | None = session
        self._wrote = False

    def _aborted(self, exc: BaseException) -> sqlite3.OperationalError:
        self._session = None
        err = sqlite3.OperationalError(TRANSACTION_ABORTED)
        err.__cause__ = exc
        return err

    def _session_lost(self, exc: BaseException) -> bool:
        if isinstance(exc, ConnectionError):
            return True
        return isinstance(exc, RemoteDBError) and "unknown session" in str(exc)

    def _session_rpc(self, kind: str, *, reads_only: bool, **fields: Any) -> dict:
        for attempt in range(2):
            if self._session is None:
                self._reopen()
            try:
                return self._db._rpc(kind, session=self._session, **fields)
            except (ConnectionError, RemoteDBError) as exc:
                if not self._session_lost(exc):
                    raise
                if self._wrote or attempt == 1 or (_may_have_applied(exc) and not reads_only):
                    raise self._aborted(exc) from exc
                self._session = None  # nothing written yet: a new session loses nothing
        raise AssertionError("unreachable")  # pragma: no cover

    def _reopen(self) -> None:
        try:
            resp = self._db._rpc("conn_open")
        except ConnectionError as exc:
            raise self._aborted(exc) from exc
        session = resp.get("session")
        if not session:
            raise RemoteDBError("daemon did not return a session id")
        self._session = session

    def execute(self, sql: str, params: Any = ()) -> RemoteCursor:
        reads_only = bool(_READ_SQL.match(sql or ""))
        resp = self._session_rpc(
            "conn_execute", reads_only=reads_only,
            sql=sql, params=_positional_params(params),
        )
        if not reads_only:
            self._wrote = True
        return RemoteCursor(decode(resp.get("rows", [])), resp.get("lastrowid"), resp.get("rowcount"))

    def executemany(self, sql: str, seq_of_params: Any) -> RemoteCursor:
        resp = self._session_rpc(
            "conn_executemany", reads_only=False,
            sql=sql, seq=[_positional_params(p) for p in seq_of_params],
        )
        self._wrote = True
        return RemoteCursor([], resp.get("lastrowid"), resp.get("rowcount"))

    def commit(self) -> None:
        if self._session is None and not self._wrote:
            return  # session ended by a restart before anything was written
        try:
            self._db._rpc("conn_commit", session=self._session)
        except (ConnectionError, RemoteDBError) as exc:
            if not self._session_lost(exc):
                raise
            if self._wrote:
                raise self._aborted(exc) from exc
            self._session = None
            return
        self._wrote = False

    def rollback(self) -> None:
        self._wrote = False
        if self._session is None:
            return
        try:
            self._db._rpc("conn_rollback", session=self._session)
        except (ConnectionError, RemoteDBError) as exc:
            if not self._session_lost(exc):
                raise
            # The daemon that held the transaction is gone; so is the transaction.
            log.debug("rollback on a lost daemon session", exc_info=True)
            self._session = None

    def close(self) -> None:
        if self._session is None:
            return
        try:
            self._db._rpc("conn_close", session=self._session)
        except Exception:
            log.debug("conn_close failed", exc_info=True)


class RemoteDatabase:
    """Drop-in proxy for shared.db.Database backed by the single-writer daemon."""

    def __init__(self, db_path: str | Path, *, config=None) -> None:
        self._db_path = str(Path(db_path))
        self._config = config
        daemon_cfg = getattr(config, "db_daemon", None)
        configured_socket = (getattr(daemon_cfg, "socket_path", "") or "")
        # A configured socket belongs to the configured DB. Pointing `--db` at a
        # different file must not talk to a daemon serving the configured one.
        if configured_socket and not _same_path(getattr(config, "db_path", None), self._db_path):
            configured_socket = ""
        self._socket_path = configured_socket or socket_path_for(db_path)
        self._connect_timeout_s = float(getattr(daemon_cfg, "connect_timeout_s", 5.0))
        self._idle_timeout_s = float(getattr(daemon_cfg, "idle_timeout_s", 900.0))
        self._max_lifetime_s = float(getattr(daemon_cfg, "max_lifetime_s", 86400.0))
        self._watch_interval_s = float(getattr(daemon_cfg, "watch_interval_s", 5.0))
        mode = str(getattr(daemon_cfg, "fallback_mode", "") or "")
        if not mode:
            mode = "direct" if bool(getattr(daemon_cfg, "fallback_to_direct", True)) else "off"
        self._fallback_mode = mode if mode in ("direct", "readonly", "off") else "direct"
        self._fallback_ok = self._fallback_mode != "off"
        self._local = threading.local()
        self._spawn_lock = threading.Lock()
        self._direct = None  # lazily-created fallback Database if the daemon dies
        self._fallback_lock = threading.Lock()
        self._next_probe = 0.0

    # -- socket / spawn -------------------------------------------------
    def _daemon_is_live(self) -> bool:
        """True only when something is actually accepting on the socket.

        Existence of the socket *file* proves nothing: a daemon that was
        terminated rather than idling out leaves the file behind (``_cleanup``
        unlinks it, but nothing runs on SIGTERM/SIGKILL). The probe is a
        connect() with a short timeout — a stale path answers ECONNREFUSED
        immediately, so this costs microseconds in the common case.
        """
        if not os.path.exists(self._socket_path):
            return False
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            probe.connect(self._socket_path)
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _spawn_daemon(self) -> None:
        with self._spawn_lock:
            # Another thread may have spawned + connected already. This probes
            # rather than stat()ing the path, because os.path.exists() cannot
            # tell a live listener from a leftover file — and skipping the spawn
            # on mere existence made a stale socket *permanently* unspawnable:
            # connect refuses, _connect asks us to spawn, we see the file and
            # return, the retry refuses again, and the client falls back to a
            # direct connection forever. Every process then mmap'd its own -shm
            # over one DB file, which is precisely the corruption hazard the
            # daemon exists to remove. Observed live: nine quarantined images in
            # five weeks with the daemon down and a stale socket on disk.
            #
            # Spawning while a stale file is present is safe: DBDaemon._bind()
            # unlinks the path before binding, and _elect()'s flock means a
            # redundant spawn loses the election and exits rather than serving a
            # second writer.
            if self._daemon_is_live():
                return
            import sys as _sys
            # Spawn from the package root (where `shared/` lives), not the DB dir —
            # the two differ under tempdirs/tests and when THRENODY_INSTALL_DIR is set.
            pkg_root = str(Path(__file__).resolve().parent.parent)
            cmd = [
                _sys.executable, "-m", "shared.db_daemon", self._db_path,
                "--socket", self._socket_path, "--idle-timeout", str(self._idle_timeout_s),
                "--max-lifetime", str(self._max_lifetime_s),
                "--watch-interval", str(self._watch_interval_s),
            ]
            env = {**os.environ}
            env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
            try:
                # The daemon logs to its own rotating file (db_daemon._configure_logging).
                subprocess.Popen(
                    cmd, cwd=pkg_root, start_new_session=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                )
            except Exception as exc:  # pragma: no cover
                raise DaemonUnreachable(f"failed to spawn db daemon: {exc}") from exc

    def _connect(self) -> socket.socket:
        """Connect, spawning the daemon up to ``_MAX_SPAWNS`` times with backoff.

        Raises ``DaemonUnreachable`` — never retried by ``_rpc`` — once the
        deadline passes, at once for a socket path no daemon could ever bind,
        and at once (without spawning) for a background frame.
        """
        deadline = time.monotonic() + self._connect_timeout_s
        spawns = 0
        next_spawn = time.monotonic()
        backoff = _SPAWN_BACKOFF_S
        last_exc: Exception | None = None
        background = in_background()
        while time.monotonic() < deadline:
            sock: socket.socket | None = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(self._connect_timeout_s)
                sock.connect(self._socket_path)
                return sock
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                last_exc = exc
                if sock is not None:
                    sock.close()  # one descriptor per failed attempt used to leak until GC
                if "path too long" in str(exc):
                    # Permanent: no daemon can ever bind this path either. Without
                    # this the loop polled for the full connect timeout — twice,
                    # once per _rpc attempt — before open_database fell back to a
                    # direct DB: ~10s per call under pytest's deep tmp paths.
                    raise SocketPathTooLong(
                        f"db daemon socket path is too long for AF_UNIX: {self._socket_path}"
                    ) from exc
                if background:
                    # Background work never brings the daemon back on its own:
                    # that would defeat the idle exit it is excluded from.
                    raise DaemonNotRunning(
                        f"db daemon not running at {self._socket_path} (background request)"
                    ) from exc
                now = time.monotonic()
                if spawns < _MAX_SPAWNS and now >= next_spawn:
                    self._spawn_daemon()
                    spawns += 1
                    next_spawn = now + backoff
                    backoff *= 2
                time.sleep(0.05)
        raise DaemonUnreachable(
            f"db daemon unreachable at {self._socket_path} after {spawns} spawn(s): {last_exc}"
        )

    def _handshake(self, sock: socket.socket) -> None:
        """Protocol hello on a fresh socket; a pre-protocol daemon → legacy mode."""
        send_frame(sock, {
            "kind": "hello", "protocol": PROTOCOL_VERSION, "version": DAEMON_VERSION,
            "request_id": uuid.uuid4().hex,
        })
        resp = recv_frame(sock)
        daemon_id = resp.get("daemon_id")
        if daemon_id:
            self._local.daemon_id = daemon_id
        if resp.get("ok"):
            served = resp.get("db_path")
            if served and not _same_path(served, self._db_path):
                raise DaemonUnreachable(
                    f"socket {self._socket_path} serves {served}, not {self._db_path}"
                )
            self._local.peer = {
                "protocol": resp.get("protocol"), "version": resp.get("version"),
                "pid": resp.get("pid"), "legacy": False,
            }
        else:
            # An old daemon answers "unknown request kind" and keeps the socket
            # usable; it just cannot do anything protocol 2 added.
            self._local.peer = {"protocol": 1, "version": None, "pid": None, "legacy": True}

    def _sock(self) -> socket.socket:
        sock = getattr(self._local, "sock", None)
        if sock is None:
            sock = self._connect()
            try:
                self._handshake(sock)
            except BaseException:
                sock.close()
                raise
            self._local.sock = sock
        return sock

    def _drop_sock(self) -> None:
        sock = getattr(self._local, "sock", None)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
            self._local.sock = None

    def peer(self) -> dict | None:
        """Hello result for this thread's connection (connects if needed)."""
        self._sock()
        return getattr(self._local, "peer", None)

    # -- RPC ------------------------------------------------------------
    def _rpc(self, kind: str, *, _retry_safe: bool = False, **fields: Any) -> dict:
        """One request/response round-trip, with at most one transparent re-send.

        See the module docstring for when a re-send is allowed. Raises plain
        ``ConnectionError`` when nothing was delivered (``DaemonUnreachable``
        when no daemon could be reached at all), ``DeliveryUncertain`` /
        ``DaemonRestarted`` when the request may have been applied.
        """
        request_id = uuid.uuid4().hex
        req = {"kind": kind, "request_id": request_id, **{k: encode(v) for k, v in fields.items()}}
        if in_background():
            req["bg"] = True
        retry_safe = _retry_safe or kind in SIDE_EFFECT_FREE_KINDS
        # A session lives in the daemon handler behind this thread's socket, so a
        # reconnect can never reach it: session ops report and let
        # RemoteConnection decide.
        session_op = kind in _SESSION_KINDS
        for attempt in range(2):  # one transparent reconnect / re-send
            last = attempt == 1
            sent = False
            try:
                sock = self._sock()
                send_frame(sock, req)
                # Past this point the daemon may have received and applied the
                # request; a partial/failed sendall (sent still False) cannot have
                # been applied, so only that case is unconditionally safe to retry.
                sent = True
                resp = recv_frame(sock)
            except DaemonUnreachable:
                raise  # connect + respawns already exhausted; a 2nd round doubles the wait
            except (ConnectionError, OSError) as exc:
                daemon_id = getattr(self._local, "daemon_id", None)
                self._drop_sock()
                if not sent:
                    if last or session_op:
                        raise ConnectionError(f"db daemon rpc failed: {exc}") from exc
                    continue
                # Delivered, reply lost. Never blindly re-send: a write would be
                # doubled (INSERT into escalations / approval_queue / telemetry ...).
                if not last and not session_op:
                    if retry_safe:
                        continue
                    if kind == "call" and daemon_id and self._reattach(daemon_id):
                        continue  # same daemon: its request_id memo dedups the re-send
                raise DeliveryUncertain(f"db daemon rpc failed after delivery: {exc}") from exc
            daemon_id = resp.get("daemon_id")
            if daemon_id:
                self._local.daemon_id = daemon_id
            if not resp.get("ok", False):
                error = resp.get("error") or {}
                if error.get("type") == DAEMON_RESTARTING:
                    self._drop_sock()
                    applied = bool(error.get("applied", True))
                    message = str(error.get("message", "db daemon restarting"))
                    if last or session_op or (applied and not retry_safe):
                        raise DaemonRestarted(message, applied=applied)
                    log.info("db daemon restarting; retrying %s once: %s", kind, message)
                    continue
                _raise_remote(error)
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    def _reattach(self, daemon_id: str) -> bool:
        """Reconnect without spawning; True when the same daemon still answers.

        Only that daemon holds the memo of the lost request's ``request_id``; a
        replacement would apply the re-send as new. On success the fresh socket
        (after its own hello) becomes this thread's socket.
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._connect_timeout_s)
            sock.connect(self._socket_path)
            self._handshake(sock)
        except (ConnectionError, OSError, ProtocolError):
            log.debug("reattach probe failed", exc_info=True)
            sock.close()
            return False
        if getattr(self._local, "daemon_id", None) != daemon_id:
            sock.close()
            return False
        self._local.sock = sock
        return True

    # -- constrained direct fallback ------------------------------------
    def _active_fallback(self):
        """The engaged fallback Database, or None once the daemon is back.

        While engaged the daemon is re-probed at most every
        ``FALLBACK_REPROBE_S``: a cheap connect probe, plus — when nothing is
        listening — a fire-and-forget spawn, so a later probe can switch back
        without any caller ever waiting on a spawn.
        """
        direct = self._direct
        if direct is None:
            return None
        now = time.monotonic()
        if now < self._next_probe:
            return direct
        with self._fallback_lock:
            if self._direct is None:
                return None
            if now < self._next_probe:
                return self._direct
            self._next_probe = now + FALLBACK_REPROBE_S
            if not self._daemon_is_live():
                if not in_background():
                    try:
                        self._spawn_daemon()
                    except ConnectionError:
                        log.debug("fallback re-spawn failed", exc_info=True)
                return self._direct
            log.warning("db daemon is back — leaving direct fallback for %s", self._db_path)
            direct, self._direct = self._direct, None
        try:
            direct.close()
        except Exception:
            log.debug("closing direct fallback failed", exc_info=True)
        return None

    def _engage_fallback(self, cause: BaseException):
        """Switch this process to the constrained direct fallback (or refuse)."""
        if not self._fallback_ok:
            raise cause
        if in_background() and self._direct is None:
            raise cause  # background work never opens a fallback of its own
        return self._direct_db(cause)

    def _direct_db(self, cause: BaseException | None = None):
        """Lazily open the constrained fallback Database (daemon unreachable).

        Holds the access lock SHARED like any other opener, but does no
        automatic maintenance (no open-time recovery, backup, salvage or journal
        replay — a degraded peer of a live daemon must not be the one to swap
        files), keeps no connection between operations, and — in
        ``readonly`` mode — cannot write at all.
        """
        with self._fallback_lock:
            if self._direct is None:
                from .db import Database
                key = self._db_path
                with _FALLBACK_WARNED_LOCK:
                    first = key not in _FALLBACK_WARNED
                    _FALLBACK_WARNED.add(key)
                (log.warning if first else log.debug)(
                    "db daemon unavailable (%s) — constrained %s fallback for %s",
                    cause, self._fallback_mode, self._db_path,
                )
                self._direct = Database(
                    Path(self._db_path),
                    resilience=getattr(self._config, "resilience", None),
                    maintenance=False,
                    readonly=self._fallback_mode == "readonly",
                    persistent_connections=False,
                )
                self._next_probe = time.monotonic() + FALLBACK_REPROBE_S
            return self._direct

    @property
    def in_fallback(self) -> bool:
        return self._direct is not None

    def _call(self, method: str, args: tuple, kwargs: dict) -> Any:
        direct = self._active_fallback()
        if direct is not None:
            return getattr(direct, method)(*args, **kwargs)
        readonly = method in READONLY_METHODS
        try:
            resp = self._rpc(
                "call", _retry_safe=readonly, method=method, args=list(args), kwargs=kwargs
            )
        except ConnectionError as exc:
            # A request that may have been applied must not be replayed against a
            # direct connection either — that is the same double write.
            if _may_have_applied(exc) and not readonly:
                raise sqlite3.OperationalError(
                    f"daemon restarted; outcome of {method} unknown"
                ) from exc
            return getattr(self._engage_fallback(exc), method)(*args, **kwargs)
        return decode(resp.get("result"))

    # -- Database-compatible surface -----------------------------------
    def __getattr__(self, name: str):
        # Called only for attributes not defined on the instance/class → treat as
        # a proxied Database method. (Private names never proxy.)
        if name.startswith("_"):
            raise AttributeError(name)

        def _proxy(*args: Any, **kwargs: Any) -> Any:
            return self._call(name, args, kwargs)

        return _proxy

    def _getattr_remote(self, name: str):
        direct = self._active_fallback()
        if direct is not None:
            return getattr(direct, name)
        try:
            return decode(self._rpc("getattr", name=name).get("result"))
        except Exception:
            return None

    @property
    def last_integrity_ok(self):
        return self._getattr_remote("last_integrity_ok")

    @property
    def last_backup_ts(self):
        return self._getattr_remote("last_backup_ts")

    @contextmanager
    def conn(self) -> Iterator[RemoteConnection]:
        direct = self._active_fallback()
        if direct is None:
            try:
                resp = self._rpc("conn_open")
            except ConnectionError as exc:
                direct = self._engage_fallback(exc)
        if direct is not None:
            with direct.conn() as c:
                yield c  # type: ignore[misc]
            return
        session = resp.get("session")
        if not session:
            raise RemoteDBError("daemon did not return a session id")
        rconn = RemoteConnection(self, session)
        try:
            yield rconn
            rconn.commit()
        except Exception:
            try:
                rconn.rollback()
            except Exception:
                log.debug("remote rollback failed", exc_info=True)
            raise
        finally:
            rconn.close()

    # -- admin ----------------------------------------------------------
    def admin(self, op: str, **fields: Any) -> Any:
        """Run a daemon admin op (status / shutdown / repair / checkpoint)."""
        if op in DESTRUCTIVE_ADMIN_OPS:
            peer = self.peer() or {}
            if peer.get("legacy"):
                raise RemoteDBError(
                    f"legacy db daemon (protocol 1) cannot run admin {op}; "
                    "stop it with SIGTERM and let the next client spawn a current one"
                )
        return decode(self._rpc("admin", op=op, **fields).get("result"))

    def daemon_status(self) -> dict:
        return self.admin("status")

    def repair(self, timeout_s: float | None = None) -> str:
        """Repair through the daemon, which quiesces itself first.

        In fallback (daemon unreachable) the repair runs here, still gated by
        the access lock EXCLUSIVE — so it declines while anyone has the
        database open.
        """
        direct = self._active_fallback()
        if direct is None:
            try:
                return self.admin("repair", timeout_s=timeout_s)
            except DaemonUnreachable as exc:
                direct = self._engage_fallback(exc)
        return direct.repair(timeout_s=timeout_s)

    def close(self) -> None:
        # Close only THIS process's client sockets — never the daemon's DB.
        self._drop_sock()
        if self._direct is not None:
            try:
                self._direct.close()
            except Exception:
                log.debug("direct fallback close failed", exc_info=True)

    def ping(self) -> bool:
        return bool(self._rpc("ping").get("pong"))


_FALLBACK_WARNED: set[str] = set()
_FALLBACK_WARNED_LOCK = threading.Lock()


def _same_path(a: object, b: object) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))
    except OSError:
        return str(a) == str(b)


def _election_lock_state(db_path: str | Path) -> str:
    """``free`` / ``held`` via a non-blocking EXCLUSIVE probe on the election lock."""
    lock_path = election_lock_path(db_path)
    if not lock_path.exists():
        return "free"
    handle = db_locks.acquire(lock_path, db_locks.EXCLUSIVE, 0.0)
    if handle is None:
        return "held"
    handle.release()
    return "free"


def probe_daemon(
    db_path: str | Path, *, socket_path: str | None = None, timeout_s: float = 2.0
) -> dict[str, object]:
    """What is serving *db_path*, without ever spawning anything.

    ``running`` + ``status`` (the daemon's admin status) for a current daemon;
    ``legacy: true`` and no status for a pre-protocol one. With no daemon,
    ``election_lock`` says whether something still holds the election lock (a
    daemon starting up, or wedged without its socket) and ``access_lock`` who
    has the database open.
    """
    sock_path = socket_path or socket_path_for(db_path)
    info: dict[str, object] = {
        "db_path": str(db_path), "socket_path": sock_path, "running": False,
    }
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect(sock_path)
        send_frame(sock, {"kind": "hello", "protocol": PROTOCOL_VERSION,
                          "version": DAEMON_VERSION, "bg": True})
        hello = recv_frame(sock)
        info["running"] = True
        info["daemon_id"] = hello.get("daemon_id")
        if not hello.get("ok"):
            info["legacy"] = True
            send_frame(sock, {"kind": "ping", "bg": True})
            info["pid"] = recv_frame(sock).get("pid")
            return info
        info["legacy"] = False
        info["pid"] = hello.get("pid")
        send_frame(sock, {"kind": "admin", "op": "status", "bg": True})
        resp = recv_frame(sock)
        if resp.get("ok"):
            info["status"] = decode(resp.get("result"))
        else:
            info["status_error"] = (resp.get("error") or {}).get("message")
        return info
    except (ConnectionError, OSError, ProtocolError) as exc:
        if info["running"]:
            info["error"] = str(exc)
            return info
        info["connect_error"] = str(exc)
    finally:
        sock.close()
    info["election_lock"] = _election_lock_state(db_path)
    try:
        info["access_lock"] = db_locks.access_lock(db_path).probe()
    except OSError:
        info["access_lock"] = "unknown"
    return info


def _pids_holding(path: Path) -> list[int]:
    """PIDs holding the election lock *path*: ``lsof -t``, else the pid it records.

    The daemon writes its pid into the lock file when it wins the election, so
    the fallback works without lsof — a pre-protocol daemon never did, which is
    why lsof stays the first choice.
    """
    import shutil as _shutil

    if not path.exists():
        return []
    lsof = _shutil.which("lsof")
    if not lsof:
        try:
            text = path.read_text().strip()
        except OSError:
            return []
        return [int(text)] if text.isdigit() else []
    try:
        out = subprocess.run(
            [lsof, "-t", str(path)], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        log.debug("lsof failed", exc_info=True)
        return []
    return sorted({int(p) for p in out.split() if p.strip().isdigit()})


def _is_db_daemon(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "shared.db_daemon" in out


def stop_daemon(
    db_path: str | Path, *, socket_path: str | None = None, wait_s: float = 15.0
) -> dict[str, object]:
    """Stop whatever daemon serves *db_path* and wait for its election lock.

    A current daemon gets ``admin shutdown`` (an ordered handover). A legacy
    one — or a daemon that does not answer — gets SIGTERM, but only a process
    holding the election lock whose command line is ``shared.db_daemon``.
    ``released`` says whether the election lock was free within *wait_s*.
    """
    import signal as _signal

    db_path = Path(db_path)
    result: dict[str, object] = {"requested": None, "signalled": [], "released": False}
    info = probe_daemon(db_path, socket_path=socket_path)
    result["probe"] = {k: v for k, v in info.items() if k != "status"}
    if info.get("running") and not info.get("legacy"):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect(str(info["socket_path"]))
            send_frame(sock, {"kind": "hello", "protocol": PROTOCOL_VERSION,
                              "version": DAEMON_VERSION})
            recv_frame(sock)
            send_frame(sock, {"kind": "admin", "op": "shutdown"})
            result["requested"] = bool(recv_frame(sock).get("ok"))
            sock.close()
        except (ConnectionError, OSError, ProtocolError) as exc:
            result["requested"] = False
            result["error"] = str(exc)
    deadline = time.monotonic() + max(0.0, wait_s)
    signalled = False
    while time.monotonic() < deadline:
        if _election_lock_state(db_path) == "free":
            result["released"] = True
            return result
        # Graceful first; SIGTERM once the admin path is unavailable or slow.
        if not signalled and (not result["requested"] or time.monotonic() > deadline - wait_s / 2):
            signalled = True
            for pid in _pids_holding(election_lock_path(db_path)):
                if pid != os.getpid() and _is_db_daemon(pid):
                    try:
                        os.kill(pid, _signal.SIGTERM)
                        result["signalled"].append(pid)
                    except ProcessLookupError:
                        pass
        time.sleep(0.1)
    return result


def open_database(db_path: str | Path | None = None, *, config=None):
    """Return a daemon-backed RemoteDatabase when enabled, else a direct Database.

    With the daemon enabled and unreachable (after its bounded respawns), the
    result is still a ``RemoteDatabase`` — running on the constrained direct
    fallback and switching back by itself once the daemon returns — unless
    ``fallback_mode`` is ``off``, which raises. A DB whose socket path can never
    be bound (too long for AF_UNIX) has no daemon by construction and gets a
    full direct ``Database``, exactly as with the daemon disabled.
    """
    from .config import (
        DB_BACKUP_INTERVAL_HOURS,
        DB_BACKUP_KEEP,
        DB_INTEGRITY_REPROBE_INTERVAL_HOURS,
        DB_SYNCHRONOUS_DEFAULT,
        TGsConfig,
    )
    from .db import Database

    if config is None:
        try:
            config = TGsConfig.from_yaml()
        except Exception:
            config = None
    resolved_path = db_path or getattr(config, "db_path", None)

    daemon_cfg = getattr(config, "db_daemon", None)
    if daemon_cfg is not None and getattr(daemon_cfg, "enabled", False) and resolved_path is not None:
        remote = RemoteDatabase(resolved_path, config=config)
        try:
            remote.ping()  # forces connect/spawn; validates the daemon is live
            return remote
        except SocketPathTooLong as exc:
            key = str(resolved_path)
            with _FALLBACK_WARNED_LOCK:
                first = key not in _FALLBACK_WARNED
                _FALLBACK_WARNED.add(key)
            (log.warning if first else log.debug)(
                "db daemon impossible for %s (%s) — using a direct DB", resolved_path, exc
            )
        except ConnectionError as exc:
            remote._engage_fallback(exc)  # raises when fallback_mode is off
            return remote

    backup_keep = int(getattr(config, "db_backup_keep", DB_BACKUP_KEEP) or DB_BACKUP_KEEP)
    backup_interval_hours = int(
        getattr(config, "db_backup_interval_hours", DB_BACKUP_INTERVAL_HOURS) or 0
    )
    integrity_reprobe_interval_hours = float(
        getattr(
            config,
            "db_integrity_reprobe_interval_hours",
            DB_INTEGRITY_REPROBE_INTERVAL_HOURS,
        )
        or 0.0
    )
    synchronous = str(
        getattr(config, "db_synchronous", DB_SYNCHRONOUS_DEFAULT) or DB_SYNCHRONOUS_DEFAULT
    )
    resilience = getattr(config, "resilience", None)
    if resolved_path:
        return Database(
            Path(resolved_path),
            backup_keep=backup_keep,
            backup_interval_hours=backup_interval_hours,
            resilience=resilience,
            integrity_reprobe_interval_hours=integrity_reprobe_interval_hours,
            synchronous=synchronous,
        )
    return Database(
        backup_keep=backup_keep,
        backup_interval_hours=backup_interval_hours,
        resilience=resilience,
        integrity_reprobe_interval_hours=integrity_reprobe_interval_hours,
        synchronous=synchronous,
    )
