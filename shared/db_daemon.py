#!/usr/bin/env python3
"""Single-writer database daemon.

One daemon per database path owns the sole live SQLite connection(s) to
``cache.db``. Every Threnody process talks to it over a local AF_UNIX socket
instead of opening the WAL directly. Because only ONE process ever mmaps the
``-shm`` shared-memory file, the multi-process ``-shm`` truncation race that can
SIGBUS under heavy concurrency disappears entirely, and writers serialize
in-process with no cross-process WAL-index desync.

Run:  python3 -m shared.db_daemon <db_path> [--socket PATH] [--idle-timeout S]
                                  [--max-lifetime S] [--watch-interval S]
                                  [--log-file PATH] [--log-level LEVEL]

Wire protocol: see shared/db_ipc.py. Requests (``kind``):
  hello    {protocol, version}                      -> {ok, protocol, version, daemon_id, pid, db_path}
  ping                                              -> {ok, pong, pid}
  call     {method, args, kwargs}                   -> {ok, result}
  getattr  {name}                                   -> {ok, result}
  admin    {op: status|shutdown|repair|checkpoint}  -> {ok, result}
  conn_open                                         -> {ok, session}
  conn_execute {session, sql, params}               -> {ok, rows, lastrowid, rowcount}
  conn_executemany {session, sql, seq}              -> {ok, rowcount, lastrowid}
  conn_commit/conn_rollback/conn_close {session}    -> {ok}

Every request may carry ``request_id`` and ``bg``; every response carries
``daemon_id``. All of it is optional on the wire — an older peer that omits or
ignores these fields interoperates unchanged. A connection that never sent
``hello`` is *legacy*: it is served normally but may not run the destructive
admin ops (``shutdown`` / ``repair``), and ``status`` counts it so ``threnody
doctor`` can flag pre-protocol clients.

Lifetime. The daemon exits when no *foreground* request arrived for
``idle_timeout_s`` and no session is open — background frames (``bg: true``, the
MCP server's health probe and warm-path loop) never keep it alive, and an idle
but connected client no longer does either. After ``max_lifetime_s`` it
recycles at the first moment no session is open. Both are an ordered handover:
stop accepting, drain in-flight requests, close the database, release the access
lock, release the election lock last — so the next client's spawn can take over
cleanly. Requests that race the shutdown are answered ``DaemonRestarting`` with
``applied: false`` and the client retries them against the successor.

Restart protocol. The daemon exits with ``EXIT_RESTART`` (75) when its view of
the database can no longer be trusted: the db, ``-wal`` or ``-shm`` it opened
was replaced or removed underneath it (while the keeper connection is open
SQLite itself never deletes the sidecars, so any change is foreign), the keeper
probe or a request hits an I/O-level error (``DB_IOERR``), or an in-process
recovery swapped the files. That is never treated as corruption — no recovery
runs. Requests that arrive after the decision are answered with error type
``DaemonRestarting`` and ``applied: false``; one that failed mid-execution says
whether it may have been applied, so the client knows whether a retry is safe.

Logging. The daemon's own stdout/stderr go nowhere (the client spawns it
detached), so it logs to a rotating file, ``<db dir>/logs/db_daemon.log``
(1 MB x 5) — for the installed DB, ``<install>/logs/db_daemon.log``.
"""
from __future__ import annotations

import argparse
import signal
import atexit
import logging
import logging.handlers
import os
import socket
import sqlite3
import sys
import threading
import time
import uuid
from collections import OrderedDict
from typing import Callable

try:  # POSIX-only cross-process election.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None

from pathlib import Path

from .db_ipc import ProtocolError, decode, encode, make_err, make_ok, recv_frame, send_frame
from .db_locks import election_lock_path
from .resilience import ErrorCategory, classify_sqlite_error

log = logging.getLogger(__name__)

# EX_TEMPFAIL: "try again" — the daemon's handle on the files is stale; a fresh
# process (spawned on demand by the next client) is the remedy.
EXIT_RESTART = 75
DAEMON_RESTARTING = "DaemonRestarting"
WATCH_INTERVAL_ENV = "THRENODY_DB_DAEMON_WATCH_INTERVAL_S"
_DEFAULT_WATCH_INTERVAL_S = 5.0
# Wire protocol generation. 1 = implicit (no hello); 2 = hello, bg, admin, request_id.
PROTOCOL_VERSION = 2
# Request kinds with no side effects: a failure inside them never applied anything.
SIDE_EFFECT_FREE_KINDS = frozenset({"ping", "getattr", "conn_open", "hello"})
ADMIN_OPS = frozenset({"status", "shutdown", "repair", "checkpoint"})
DESTRUCTIVE_ADMIN_OPS = frozenset({"shutdown", "repair"})
_SESSION_KINDS = frozenset(
    {"conn_execute", "conn_executemany", "conn_commit", "conn_rollback", "conn_close"}
)
_CHECKPOINT_MODES = frozenset({"PASSIVE", "FULL", "RESTART", "TRUNCATE"})
_REQUEST_LOG_CAPACITY = 256
_REQUEST_LOG_WAIT_S = 120.0
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 5


def _read_version() -> str:
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


DAEMON_VERSION = _read_version()


def _default_watch_interval() -> float:
    raw = os.environ.get(WATCH_INTERVAL_ENV, "")
    try:
        value = float(raw) if raw else _DEFAULT_WATCH_INTERVAL_S
    except ValueError:
        log.debug("ignoring invalid %s=%r", WATCH_INTERVAL_ENV, raw)
        value = _DEFAULT_WATCH_INTERVAL_S
    return value if value > 0 else _DEFAULT_WATCH_INTERVAL_S


def socket_path_for(db_path: str | Path) -> str:
    return str(Path(db_path)) + ".sock"


def _lock_path_for(db_path: str | Path) -> str:
    return str(election_lock_path(db_path))


def default_log_path(db_path: str | Path) -> Path:
    return Path(db_path).expanduser().parent / "logs" / "db_daemon.log"


class _Session:
    """A client `with db.conn()` mapped to a dedicated daemon-side connection."""

    __slots__ = ("conn",)

    def __init__(self, conn) -> None:
        self.conn = conn


class _ClientState:
    """Per-connection state: open sessions, and whether the peer said hello."""

    __slots__ = ("sessions", "seq", "hello", "legacy")

    def __init__(self) -> None:
        self.sessions: dict[str, _Session] = {}
        self.seq = 0
        self.hello: dict | None = None
        self.legacy = False  # first request was not a hello


class _RequestLog:
    """Bounded ``request_id`` → response memo for side-effecting calls.

    A client that lost the response to a delivered frame may re-send it (same
    ``request_id``) once it has confirmed it is talking to the same daemon. The
    memo answers that re-send instead of applying the write a second time; a
    re-send that races the original still in flight waits for its result.
    """

    def __init__(self, capacity: int = _REQUEST_LOG_CAPACITY) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, list] = OrderedDict()  # rid -> [Event, resp]

    def begin(self, request_id: str) -> tuple[list, bool]:
        """Return (entry, owner). The owner executes; others wait on the entry."""
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is not None:
                return entry, False
            entry = [threading.Event(), None]
            self._entries[request_id] = entry
            return entry, True

    def complete(self, request_id: str, entry: list, resp: dict) -> None:
        entry[1] = resp
        entry[0].set()
        with self._lock:
            while len(self._entries) > self._capacity:
                oldest_id, oldest = next(iter(self._entries.items()))
                if not oldest[0].is_set():
                    break  # never evict an in-flight request
                self._entries.pop(oldest_id, None)

    @staticmethod
    def wait(entry: list, timeout_s: float = _REQUEST_LOG_WAIT_S) -> dict | None:
        return entry[1] if entry[0].wait(timeout_s) else None


class DBDaemon:
    def __init__(self, db_path: str, *, socket_path: str | None = None,
                 idle_timeout_s: float = 900.0,
                 client_idle_reap_s: float = 900.0,
                 watch_interval_s: float | None = None,
                 schema_probe_interval_s: float = 30.0,
                 checkpoint_interval_s: float = 60.0,
                 election_wait_s: float = 10.0,
                 fatal_grace_s: float = 1.0,
                 max_lifetime_s: float = 86400.0,
                 drain_timeout_s: float = 5.0,
                 repair_drain_s: float = 10.0,
                 pause_wait_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._db_path = db_path
        self._socket_path = socket_path or socket_path_for(db_path)
        self._idle_timeout_s = idle_timeout_s
        # Reap a connected-but-silent client handler after this long WITH NO open
        # session, so per-thread client sockets that outlive their worker thread
        # don't pin a daemon handler thread indefinitely. Clients reconnect
        # transparently on next use (db_client retries a stale socket).
        self._client_idle_reap_s = client_idle_reap_s
        self._lock_fd: int | None = None
        self._lock_ino: int | None = None
        self._srv: socket.socket | None = None
        # True only between a successful bind() and cleanup. Gates the socket
        # unlink so an election loser cannot remove the winner's socket.
        self._owns_socket = False
        self._db = None  # lazy — created after election
        self._clock = clock
        self._clients = 0
        self._legacy_clients = 0
        self._clients_lock = threading.Lock()
        self._started = clock()
        # Only foreground requests refresh this; see _idle_due.
        self._last_fg = self._started
        self._open_sessions = 0
        self._fg_requests = 0
        self._bg_requests = 0
        self._stop = threading.Event()
        # Set once the daemon has decided to stop (fatal or graceful). Handlers
        # then answer DaemonRestarting/applied=false until the process exits.
        self._closing = threading.Event()
        self._stop_reason: str | None = None
        self._daemon_id = uuid.uuid4().hex
        # Watchdog cadence: file identity every watch_interval_s, keeper
        # liveness probe and a PASSIVE checkpoint on their own slower clocks.
        self._watch_interval_s = (
            watch_interval_s if watch_interval_s and watch_interval_s > 0
            else _default_watch_interval()
        )
        self._schema_probe_interval_s = schema_probe_interval_s
        self._checkpoint_interval_s = checkpoint_interval_s
        # A freshly spawned replacement waits this long for a restarting
        # predecessor to release the election lock before giving up.
        self._election_wait_s = election_wait_s
        self._fatal_grace_s = fatal_grace_s
        self._max_lifetime_s = max_lifetime_s
        self._drain_timeout_s = drain_timeout_s
        self._repair_drain_s = repair_drain_s
        self._pause_wait_s = pause_wait_s
        self._fatal = threading.Event()
        self._fatal_lock = threading.Lock()
        self._fatal_reason: str | None = None
        self._inflight = 0
        self._inflight_cv = threading.Condition()
        self._keeper = None
        self._keeper_lock = threading.Lock()
        self._file_ids: dict[str, tuple[int, int] | None] = {}
        self._recovery_gen_baseline = 0
        self._socket_ino: int | None = None
        self._db_released = False
        self._requests = _RequestLog()
        self._readonly_methods: frozenset[str] = frozenset()
        # Cleared while admin repair quiesces the daemon: new work waits.
        self._accepting = threading.Event()
        self._accepting.set()
        # Held by admin repair for its whole run and by each watchdog tick, so
        # the watchdog never mistakes the daemon's own swap for a foreign one.
        self._maintenance_lock = threading.Lock()
        self._repair_lock = threading.Lock()
        self._last_error: dict | None = None
        self._last_checkpoint: dict | None = None

    # -- lifecycle ------------------------------------------------------
    def _elect(self) -> bool:
        """Acquire the exclusive daemon lock. False if another daemon owns it."""
        if _fcntl is None:
            return True  # best-effort: no election off-POSIX
        lock_path = _lock_path_for(self._db_path)
        self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _fcntl.flock(self._lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            self._lock_fd = None
            return False
        # Record which inode we actually hold the flock on. If the lock file at
        # this path is ever deleted and recreated while we hold it (observed live:
        # cause not fully pinned, but the effect is reproducible), a second
        # process can independently flock() the NEW inode and also believe it won
        # election — the exact bug that let two daemons serve the same DB file
        # concurrently. `_lock_still_current` (polled from `_idle_watch`) detects
        # this after the fact; this immediate check catches the narrower race
        # where the swap already happened between our open() and flock().
        self._lock_ino = os.fstat(self._lock_fd).st_ino
        if not self._lock_still_current():
            try:
                _fcntl.flock(self._lock_fd, _fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
            self._lock_fd = None
            self._lock_ino = None
            return False
        # The winner's pid, for operators and install.sh when lsof is missing.
        # Content only — the flock, not the text, is what holds the election.
        try:
            os.ftruncate(self._lock_fd, 0)
            os.pwrite(self._lock_fd, f"{os.getpid()}\n".encode(), 0)
        except OSError:
            log.debug("could not record pid in the election lock", exc_info=True)
        return True

    def _lock_still_current(self) -> bool:
        """True if the lock path still refers to the inode we hold the flock on."""
        if self._lock_fd is None or self._lock_ino is None:
            return True  # no election in effect (off-POSIX) — nothing to check
        try:
            return os.stat(_lock_path_for(self._db_path)).st_ino == self._lock_ino
        except OSError:
            return False  # lock file gone entirely — treat as lost ownership

    def _bind(self) -> None:
        # Winner of the election owns the socket; clear any stale one first.
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self._socket_path)
        # Only a daemon that actually bound may ever unlink this path. See
        # _cleanup: a process that lost the election must not delete the winner's
        # socket, and _cleanup now runs from atexit in every daemon process.
        self._owns_socket = True
        try:
            self._socket_ino = os.stat(self._socket_path).st_ino
        except OSError:
            log.debug("could not stat the bound socket", exc_info=True)
        os.chmod(self._socket_path, 0o600)
        self._srv.listen(128)
        self._srv.settimeout(1.0)  # so accept() polls _stop / idle

    def _open_db(self):
        from .config import TGsConfig
        from .db import Database

        try:
            resilience = TGsConfig.from_yaml().resilience
        except Exception:
            resilience = None
        db = Database(Path(self._db_path), resilience=resilience)
        self._db = db
        self._open_keeper()
        try:
            from .db import READONLY_METHODS

            self._readonly_methods = READONLY_METHODS
        except ImportError:  # pragma: no cover - db.py always exports it
            log.debug("READONLY_METHODS unavailable", exc_info=True)
        return db

    def _open_keeper(self) -> None:
        """(Re)open the keeper and re-baseline everything the watchdog compares."""
        # Keeper connection: hold one live connection for the daemon's lifetime so
        # the DB is never quiescent and -shm is never re-truncated underneath us.
        keeper = self._db._connect()
        keeper.execute("PRAGMA schema_version").fetchone()
        with self._keeper_lock:
            self._keeper = keeper
        self._recovery_gen_baseline = int(getattr(self._db, "recovery_generation", 0) or 0)

    # -- file identity watchdog ----------------------------------------
    def _watched_paths(self) -> tuple[str, str, str]:
        return (self._db_path, self._db_path + "-wal", self._db_path + "-shm")

    def _file_identity(self) -> dict[str, tuple[int, int] | None]:
        ids: dict[str, tuple[int, int] | None] = {}
        for path in self._watched_paths():
            try:
                st = os.stat(path)
                ids[path] = (st.st_dev, st.st_ino)
            except FileNotFoundError:
                ids[path] = None
        return ids

    def _record_file_identity(self) -> None:
        """Baseline (st_dev, st_ino) of db/-wal/-shm, taken with the keeper open."""
        self._file_ids = self._file_identity()

    def _file_identity_drift(self) -> str | None:
        try:
            current = self._file_identity()
        except OSError as exc:
            return f"could not stat the database files: {exc}"
        for path, baseline in self._file_ids.items():
            now = current.get(path)
            if baseline is None:
                if now is not None:
                    self._file_ids[path] = now  # created by our own connections
                continue
            if now is None:
                return f"{path} was removed underneath the daemon"
            if now != baseline:
                return f"{path} was replaced underneath the daemon"
        return None

    def _keeper_probe(self, sql: str) -> bool:
        """Run *sql* on the keeper. False (and fatal) on an I/O-level failure."""
        with self._keeper_lock:
            keeper = self._keeper
            if keeper is None:
                return True
            if getattr(keeper, "closed", False):
                # Closed by an in-process recovery that then declined; the
                # watchdog is the one place that notices, so reopen here.
                try:
                    keeper = self._keeper = self._db._connect()
                except sqlite3.Error:
                    log.debug("keeper reopen failed", exc_info=True)
                    return True
            try:
                rows = keeper.execute(sql).fetchall()
            except sqlite3.Error as exc:
                category = classify_sqlite_error(exc)
                self._note_error(exc)
                if category == ErrorCategory.DB_IOERR:
                    self._trigger_fatal(f"keeper probe {sql!r} failed: {exc}")
                    return False
                log.debug("keeper probe %r failed (%s)", sql, category.value, exc_info=True)
                return True
        if sql.startswith("PRAGMA wal_checkpoint"):
            self._last_checkpoint = {
                "ts": time.time(),
                "mode": sql[sql.index("(") + 1:sql.index(")")],
                "result": list(rows[0]) if rows else None,
            }
        return True

    def _watchdog(self) -> None:
        last_schema = last_checkpoint = time.monotonic()
        while not self._stop.wait(self._watch_interval_s):
            # Skip a tick while admin repair swaps files on purpose.
            if not self._maintenance_lock.acquire(blocking=False):
                continue
            try:
                reason = self._file_identity_drift()
                if reason is None and self._db is not None:
                    generation = int(getattr(self._db, "recovery_generation", 0) or 0)
                    if generation != self._recovery_gen_baseline:
                        reason = "an in-process recovery swapped the database files"
                if reason is not None:
                    self._trigger_fatal(reason)
                    return
                now = time.monotonic()
                if now - last_schema >= self._schema_probe_interval_s:
                    last_schema = now
                    if not self._keeper_probe("PRAGMA schema_version"):
                        return
                if now - last_checkpoint >= self._checkpoint_interval_s:
                    last_checkpoint = now
                    if not self._keeper_probe("PRAGMA wal_checkpoint(PASSIVE)"):
                        return
            finally:
                self._maintenance_lock.release()

    def _trigger_fatal(self, reason: str) -> None:
        """Stop serving and restart. Idempotent; safe from any thread."""
        with self._fatal_lock:
            if self._fatal.is_set():
                return
            self._fatal_reason = reason
            self._fatal.set()
        log.error(
            "db daemon for %s must restart (exit %d): %s",
            self._db_path, EXIT_RESTART, reason,
        )
        self._begin_stop(f"restart: {reason}")

    def _begin_stop(self, reason: str) -> None:
        """Stop accepting; handlers answer DaemonRestarting from here on."""
        if self._stop_reason is None:
            self._stop_reason = reason
        self._closing.set()
        self._stop.set()
        self._close_listener()

    def _close_listener(self) -> None:
        try:
            if self._srv:
                self._srv.close()  # refuse new connections; accept() wakes within 1s
        except Exception:
            log.debug("closing listener failed", exc_info=True)

    def _drain_inflight(self, timeout_s: float, *, allow: int = 0) -> bool:
        """Wait (bounded) until at most *allow* requests are executing."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._inflight_cv:
            while self._inflight > allow:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning("db daemon: %d request(s) still in flight", self._inflight - allow)
                    return False
                self._inflight_cv.wait(remaining)
        return True

    def _socket_is_live(self) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            probe.connect(self._socket_path)
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _await_election(self) -> bool:
        """Lost the election: wait for a restarting predecessor, or defer to a live one.

        The client spawns a replacement the moment a restarting daemon stops
        accepting, which is before that daemon has released the election lock.
        Exiting at once there would leave no daemon at all; so a loser waits —
        but only while nothing is accepting on the socket. A healthy daemon
        answers the probe and the redundant spawn exits immediately as before.
        """
        deadline = time.monotonic() + max(0.0, self._election_wait_s)
        while time.monotonic() < deadline:
            if self._socket_is_live():
                return False
            if self._elect():
                return True
            time.sleep(0.05)
        return False

    def request_stop(self) -> None:
        """Signal-safe stop request: sets the flag the accept loop polls.

        Only touches an Event, so it is safe from a signal handler. The 1.0s
        accept() timeout bounds how long the loop takes to notice.
        """
        self._stop.set()

    def serve(self) -> int:
        """Serve until stopped. Returns 0, or ``EXIT_RESTART`` after a fatal check."""
        if not self._elect() and not self._await_election():
            log.info("another db daemon owns %s; exiting", self._db_path)
            return 0
        self._db = self._open_db()
        self._record_file_identity()
        self._bind()
        log.info(
            "db daemon serving %s on %s (pid=%s version=%s protocol=%s)",
            self._db_path, self._socket_path, os.getpid(), DAEMON_VERSION, PROTOCOL_VERSION,
        )
        idle_thread = threading.Thread(target=self._idle_watch, name="db-daemon-idle", daemon=True)
        idle_thread.start()
        watch_thread = threading.Thread(target=self._watchdog, name="db-daemon-watch", daemon=True)
        watch_thread.start()
        try:
            while not self._stop.is_set():
                try:
                    client, _ = self._srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=self._handle_client, args=(client,), daemon=True
                ).start()
        finally:
            # Ordered handover: stop accepting, drain, then _cleanup closes the
            # database, releases the access lock, and the election lock last.
            self._begin_stop(self._stop_reason or "stop requested")
            self._drain_inflight(
                self._fatal_grace_s if self._fatal.is_set() else self._drain_timeout_s
            )
            self._cleanup()
            log.info("db daemon for %s stopped: %s", self._db_path, self._stop_reason)
        return EXIT_RESTART if self._fatal.is_set() else 0

    # -- idle / lifetime --------------------------------------------------
    def _idle_due(self, now: float | None = None) -> bool:
        """No foreground request for idle_timeout_s and no open session."""
        if self._idle_timeout_s <= 0:
            return False
        now = self._clock() if now is None else now
        with self._clients_lock:
            return self._open_sessions == 0 and now - self._last_fg > self._idle_timeout_s

    def _recycle_due(self, now: float | None = None) -> bool:
        """Past max_lifetime_s and, at this moment, no open session."""
        if self._max_lifetime_s <= 0:
            return False
        now = self._clock() if now is None else now
        with self._clients_lock:
            return self._open_sessions == 0 and now - self._started >= self._max_lifetime_s

    def _idle_watch(self) -> None:
        # Runs regardless of idle_timeout_s: the lock-currency check is a safety
        # invariant, not an idle-exit feature, so it must not be skippable by
        # disabling idle timeout.
        horizons = [x for x in (self._idle_timeout_s, self._max_lifetime_s) if x > 0]
        check_interval = max(0.05, min([30.0, *horizons]) / 2 if horizons else 30.0)
        while not self._stop.wait(check_interval):
            if not self._lock_still_current():
                log.warning(
                    "db daemon lock file %s was replaced by another process — "
                    "no longer the sole owner of %s, shutting down",
                    _lock_path_for(self._db_path), self._db_path,
                )
                self._begin_stop("election lock replaced")
                return
            if self._idle_due():
                log.info(
                    "db daemon: no foreground request for %.0fs and no open session — exiting",
                    self._idle_timeout_s,
                )
                self._begin_stop("idle")
                return
            if self._recycle_due():
                log.info(
                    "db daemon: max lifetime %.0fs reached with no open session — recycling",
                    self._max_lifetime_s,
                )
                self._begin_stop("max lifetime")
                return

    def _socket_still_ours(self) -> bool:
        """True while the socket path is the inode this process bound.

        A replacement daemon unlinks and re-binds the path while a restarting
        one is still cleaning up; the old one must not delete the new socket.
        """
        if self._socket_ino is None:
            return True  # bound but never stat'ed: fall back to ownership alone
        try:
            return os.stat(self._socket_path).st_ino == self._socket_ino
        except FileNotFoundError:
            return False

    def _cleanup(self) -> None:
        """Release this daemon's resources. Idempotent; safe from atexit.

        The socket is unlinked **only if this process bound it** — and only
        while the path still names the socket it bound. Every daemon process
        runs this at exit, and a process that lost the flock election never
        binds — unlinking unconditionally there deletes the *winner's* live
        socket, after which clients find nothing, spawn again, and race.

        After a fatal check the SQLite handles are deliberately left open (the
        process ``os._exit``s next): closing the last WAL connection checkpoints
        and unlinks ``-wal``/``-shm`` *by path*, and once the files were swapped
        those paths name somebody else's live files.
        """
        try:
            if self._srv:
                self._srv.close()
        except Exception:
            pass
        if self._owns_socket:
            self._owns_socket = False  # idempotent: never unlink twice
            try:
                if self._socket_still_ours():
                    os.unlink(self._socket_path)
            except FileNotFoundError:
                pass
            except Exception:
                log.debug("socket unlink failed", exc_info=True)
        if self._db is not None and not self._db_released:
            self._db_released = True
            try:
                if self._fatal.is_set():
                    with self._inflight_cv:
                        stragglers = self._inflight
                    if stragglers == 0:
                        self._db._abandon_for_exit()
                    # else: a request outlived the grace period and may still be
                    # writing; let the kernel drop the access lock at exit rather
                    # than invite a peer's EXCLUSIVE swap under that write.
                else:
                    with self._keeper_lock:
                        keeper, self._keeper = self._keeper, None
                        if keeper is not None and not getattr(keeper, "closed", False):
                            self._db._drain_wal(keeper)
                            keeper.close()
                    # Every handle — handler threads' cached ones and any session
                    # a client left open — so close() can drop the access lock.
                    self._db._close_all_connections()
                    self._db.close()
            except Exception:
                log.debug("db close failed", exc_info=True)
        # The election lock goes last, once this process no longer serves.
        if self._lock_fd is not None:
            fd, self._lock_fd = self._lock_fd, None
            try:
                if _fcntl is not None:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
            except OSError:
                log.debug("election unlock failed", exc_info=True)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    log.debug("closing election lock fd failed", exc_info=True)

    # -- request handling ----------------------------------------------
    def _handle_client(self, client: socket.socket) -> None:
        with self._clients_lock:
            self._clients += 1
        # Accepted sockets can inherit the listener's 1.0s poll timeout — set our
        # own explicitly (reap window, or blocking when reaping is disabled) so a
        # client is never reaped on the inherited 1s.
        try:
            reap = self._client_idle_reap_s
            client.settimeout(reap if reap and reap > 0 else None)
        except OSError:  # pragma: no cover - platform edge
            pass
        state = _ClientState()
        try:
            # Once stopping, keep answering (DaemonRestarting, applied=false)
            # until the process exits, so a frame that races the shutdown gets a
            # definite "not applied" instead of a dropped connection.
            while not self._stop.is_set() or self._closing.is_set():
                try:
                    req = recv_frame(client)
                except socket.timeout:
                    # Silent past the reap window. If the client holds an open
                    # session it is a live transactional peer — stop reaping (go
                    # blocking) so we never tear down its transaction or mis-frame.
                    # Otherwise reap this handler; the client reconnects on demand.
                    if state.sessions:
                        try:
                            client.settimeout(None)
                        except OSError:  # pragma: no cover
                            pass
                        continue
                    break
                except (ConnectionError, ProtocolError):
                    break
                resp = self._serve_one(req, state)
                resp = {**resp, "daemon_id": self._daemon_id}
                try:
                    send_frame(client, resp)
                except (ConnectionError, OSError):
                    break
        finally:
            # Roll back + release any transactions the disconnecting client held.
            for sess in state.sessions.values():
                try:
                    sess.conn.rollback()
                    sess.conn.close()
                except Exception:
                    log.debug("session cleanup failed", exc_info=True)
            try:
                client.close()
            except Exception:
                pass
            with self._clients_lock:
                self._clients -= 1
                self._open_sessions -= len(state.sessions)
                if state.legacy:
                    self._legacy_clients -= 1
            state.sessions.clear()

    def _serve_one(self, req: dict, state: _ClientState) -> dict:
        kind = req.get("kind")
        if state.hello is None and not state.legacy and kind != "hello":
            state.legacy = True
            with self._clients_lock:
                self._legacy_clients += 1
        if self._closing.is_set():
            # Decided to stop before this request ran: nothing applied.
            return self._restarting_response(applied=False)
        self._account(req)
        # Admin repair quiesces new work; session ops on already-open sessions
        # keep flowing so their transactions can finish inside the drain window.
        new_work = kind not in _SESSION_KINDS and not (
            kind == "admin" and req.get("op") == "status"
        ) and kind not in ("hello", "ping")
        if new_work and not self._accepting.is_set():
            if not self._accepting.wait(self._pause_wait_s):
                return self._restarting_response(
                    applied=False, message="db daemon busy with maintenance"
                )
        with self._inflight_cv:
            self._inflight += 1
        try:
            return self._execute(req, state)
        finally:
            with self._inflight_cv:
                self._inflight -= 1
                self._inflight_cv.notify_all()

    def _account(self, req: dict) -> None:
        """Foreground requests keep the daemon alive; background ones never do."""
        kind = req.get("kind")
        if kind == "hello" or (kind == "admin" and req.get("op") == "status"):
            return  # handshakes and monitoring are neutral
        with self._clients_lock:
            if req.get("bg"):
                self._bg_requests += 1
            else:
                self._fg_requests += 1
                self._last_fg = self._clock()

    def _restarting_response(self, *, applied: bool, message: str | None = None) -> dict:
        resp = make_err(
            DAEMON_RESTARTING,
            message or (
                f"db daemon restarting: "
                f"{self._fatal_reason or self._stop_reason or 'unknown reason'}"
            ),
        )
        resp["error"]["applied"] = bool(applied)
        return resp

    def _is_side_effecting_call(self, req: dict) -> bool:
        return req.get("kind") == "call" and req.get("method") not in self._readonly_methods

    def _may_have_applied(self, req: dict) -> bool:
        """Could a request that failed mid-execution have changed the database?"""
        kind = req.get("kind")
        if kind in SIDE_EFFECT_FREE_KINDS:
            return False
        if kind == "call":
            return req.get("method") not in self._readonly_methods
        if kind == "admin":
            return req.get("op") in ("repair", "checkpoint")
        return True

    def _execute(self, req: dict, state: _ClientState | None = None) -> dict:
        """Dispatch one request, memoizing side-effecting calls by ``request_id``."""
        state = state if state is not None else _ClientState()
        request_id = req.get("request_id")
        entry = None
        if isinstance(request_id, str) and request_id and self._is_side_effecting_call(req):
            entry, owner = self._requests.begin(request_id)
            if not owner:
                cached = self._requests.wait(entry)
                if cached is None:
                    return make_err("RequestInFlight", "original request still running")
                return cached
        resp: dict | None = None
        try:
            try:
                resp = self._dispatch(req, state)
            except Exception as exc:  # any handler failure → structured error
                resp = self._error_response(req, exc)
        finally:
            if entry is not None:
                self._requests.complete(
                    request_id, entry, resp or make_err("Error", "request aborted")
                )
        return resp

    def _note_error(self, exc: BaseException) -> None:
        code = getattr(exc, "sqlite_errorcode", None)
        self._last_error = {
            "code": code if code is not None else type(exc).__name__,
            "message": str(exc)[:500],
            "ts": time.time(),
        }

    def _error_response(self, req: dict, exc: Exception) -> dict:
        self._note_error(exc)
        if isinstance(exc, sqlite3.Error) and classify_sqlite_error(exc) == ErrorCategory.DB_IOERR:
            # Not corruption: this process's handle on the files is broken (a
            # stale -shm mapping, a swapped file). No recovery — a restart.
            self._trigger_fatal(f"request failed with an I/O error: {exc}")
            return self._restarting_response(
                applied=self._may_have_applied(req),
                message=f"db daemon restarting after I/O error: {exc}",
            )
        # Previously unlogged: the daemon itself had no record of a
        # handler failure, only whatever the peer chose to do with it.
        log.warning("db_daemon: handler failed: %s", exc, exc_info=True)
        return make_err(type(exc).__name__, str(exc))

    def _dispatch(self, req: dict, state: _ClientState) -> dict:
        kind = req.get("kind")
        if kind == "hello":
            state.hello = {
                "protocol": req.get("protocol"),
                "version": req.get("version"),
            }
            return make_ok(
                protocol=PROTOCOL_VERSION, version=DAEMON_VERSION,
                pid=os.getpid(), db_path=str(self._db_path),
            )

        if kind == "ping":
            return make_ok(pong=True, pid=os.getpid())

        if kind == "admin":
            return make_ok(result=self._admin(req, state))

        if kind == "call":
            method = req.get("method")
            args = decode(req.get("args", []))
            kwargs = decode(req.get("kwargs", {}))
            if not isinstance(method, str) or method.startswith("_"):
                raise ProtocolError(f"illegal method: {method!r}")
            if method == "repair":
                # Run as the admin op: a plain call would make the daemon the
                # very holder its own recovery has to wait out.
                return make_ok(result=self._admin_repair(**(kwargs or {})))
            target = getattr(self._db, method, None)
            if target is None or not callable(target):
                raise AttributeError(f"Database has no callable method {method!r}")
            result = target(*args, **(kwargs or {}))
            return make_ok(result=result)

        if kind == "getattr":
            # Read a public, non-callable attribute/property (e.g. last_integrity_ok).
            name = req.get("name")
            if not isinstance(name, str) or name.startswith("_"):
                raise ProtocolError(f"illegal attribute: {name!r}")
            value = getattr(self._db, name)
            if callable(value):
                raise ProtocolError(f"{name!r} is callable; use 'call'")
            return make_ok(result=value)

        if kind == "conn_open":
            state.seq += 1
            sid = f"s{state.seq}"
            state.sessions[sid] = _Session(self._db._connect())
            with self._clients_lock:
                self._open_sessions += 1
            return make_ok(session=sid)

        # session-scoped ops
        sid = req.get("session")
        sess = state.sessions.get(sid) if isinstance(sid, str) else None
        if sess is None:
            raise ProtocolError(f"unknown session: {sid!r}")

        if kind == "conn_execute":
            sql = req.get("sql")
            params = decode(req.get("params", []))
            cur = sess.conn.execute(sql, params) if params else sess.conn.execute(sql)
            rows = cur.fetchall()
            return make_ok(rows=rows, lastrowid=cur.lastrowid, rowcount=cur.rowcount)

        if kind == "conn_executemany":
            sql = req.get("sql")
            seq = decode(req.get("seq", []))
            cur = sess.conn.executemany(sql, seq)
            return make_ok(rowcount=cur.rowcount, lastrowid=cur.lastrowid)

        if kind == "conn_commit":
            sess.conn.commit()
            return make_ok()

        if kind == "conn_rollback":
            sess.conn.rollback()
            return make_ok()

        if kind == "conn_close":
            try:
                sess.conn.close()
            finally:
                if state.sessions.pop(sid, None) is not None:
                    with self._clients_lock:
                        self._open_sessions -= 1
            return make_ok()

        raise ProtocolError(f"unknown request kind: {kind!r}")

    # -- admin ------------------------------------------------------------
    def _admin(self, req: dict, state: _ClientState) -> object:
        op = req.get("op")
        if op not in ADMIN_OPS:
            raise ProtocolError(f"unknown admin op: {op!r}")
        if op in DESTRUCTIVE_ADMIN_OPS and state.hello is None:
            raise PermissionError(
                f"admin {op} requires a protocol hello (legacy client); "
                "upgrade the client or stop the daemon with SIGTERM"
            )
        if op == "status":
            return self.status()
        if op == "shutdown":
            log.info("db daemon: admin shutdown requested")
            # Reply first; the accept loop notices within a second.
            threading.Thread(
                target=self._begin_stop, args=("admin shutdown",), daemon=True
            ).start()
            return {"stopping": True, "pid": os.getpid()}
        if op == "checkpoint":
            mode = str(req.get("mode") or "PASSIVE").upper()
            if mode not in _CHECKPOINT_MODES:
                raise ProtocolError(f"unknown checkpoint mode: {mode!r}")
            self._keeper_probe(f"PRAGMA wal_checkpoint({mode})")
            return self._last_checkpoint
        timeout = req.get("timeout_s")
        return self._admin_repair(timeout_s=float(timeout) if timeout is not None else None)

    def _admin_repair(self, timeout_s: float | None = None) -> str:
        """Quiesce, run ``Database.repair``, re-baseline, resume.

        Pause new work, give in-flight requests and open sessions up to
        ``repair_drain_s`` to finish, then close every handle (sessions still
        open after that are aborted), trade SHARED for EXCLUSIVE inside
        ``repair`` (which declines while any *other* process holds the
        database), take SHARED back, reopen the keeper, re-record file identity
        so the watchdog does not mistake this swap for a foreign one.
        """
        from .db import RECOVERY_DECLINED_IN_USE

        if not self._repair_lock.acquire(blocking=False):
            return f"{RECOVERY_DECLINED_IN_USE} (a repair is already running)"
        try:
            with self._maintenance_lock:
                self._accepting.clear()
                try:
                    deadline = time.monotonic() + self._repair_drain_s
                    # This repair request itself is in flight.
                    self._drain_inflight(self._repair_drain_s, allow=1)
                    while time.monotonic() < deadline:
                        with self._clients_lock:
                            if self._open_sessions == 0:
                                break
                        time.sleep(0.05)
                    with self._keeper_lock:
                        self._keeper = None  # closed below with every other handle
                    result = self._db.repair(timeout_s=timeout_s)
                    log.warning("db daemon: admin repair → %s", result)
                    self._open_keeper()
                    self._record_file_identity()
                    return result
                finally:
                    self._accepting.set()
        finally:
            self._repair_lock.release()

    def status(self) -> dict[str, object]:
        """Everything ``threnody db status`` / ``doctor`` report about this daemon."""
        now = self._clock()
        current = self._file_identity()
        with self._clients_lock:
            counters = {
                "fg": self._fg_requests,
                "bg": self._bg_requests,
            }
            sessions = self._open_sessions
            clients = self._clients
            legacy = self._legacy_clients
            idle_for = now - self._last_fg
        access = {}
        if self._db is not None:
            try:
                access = self._db._access_lock_state()
            except Exception:
                log.debug("access lock state failed", exc_info=True)
        return {
            "pid": os.getpid(),
            "daemon_id": self._daemon_id,
            "version": DAEMON_VERSION,
            "protocol": PROTOCOL_VERSION,
            "db_path": str(self._db_path),
            "socket_path": self._socket_path,
            "uptime_s": round(now - self._started, 3),
            "idle_for_s": round(idle_for, 3),
            "idle_timeout_s": self._idle_timeout_s,
            "max_lifetime_s": self._max_lifetime_s,
            "requests": counters,
            "sessions_open": sessions,
            "clients_connected": clients,
            "legacy_clients": legacy,
            "files": {
                path: {
                    "recorded": list(self._file_ids[path]) if self._file_ids.get(path) else None,
                    "current": list(current[path]) if current.get(path) else None,
                }
                for path in self._watched_paths()
            },
            "last_error": self._last_error,
            "last_checkpoint": self._last_checkpoint,
            "access_lock": access,
            "paused": not self._accepting.is_set(),
            "stopping": self._stop_reason,
            "fatal": self._fatal_reason,
        }


def _install_signal_handlers(daemon: "DBDaemon") -> None:
    """Ask the accept loop to stop on SIGTERM/SIGINT so ``_cleanup`` runs.

    Without this the socket file survives every termination that is not an idle
    exit — a machine sleeping, a reboot, a session teardown, ``pkill``. That
    leftover file used to be unrecoverable: the client skipped spawning whenever
    the path existed, so the daemon could never come back and every process fell
    back to its own direct connection over one DB file.

    The client no longer trusts the file's existence (``_daemon_is_live``), so a
    stale socket is now merely wasteful rather than fatal — but not creating one
    is still strictly better, and it keeps ``db check`` from reporting a socket
    for a daemon that is gone. SIGKILL cannot be handled; that case is exactly
    what the client-side probe covers.
    """
    def _handle(signum, _frame):  # pragma: no cover - signal path
        log.info("db daemon received signal %s; shutting down", signum)
        daemon.request_stop()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _handle)
        except (OSError, ValueError):
            # Not all signals exist or are settable on every platform/thread.
            log.debug("could not install handler for %s", sig, exc_info=True)


def _configure_logging(log_file: Path | None, level: str) -> None:
    """Rotating file log for a process whose stdout/stderr are /dev/null."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s pid=%(process)d %(threadName)s %(name)s: %(message)s"
    )
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            )
            handler.setFormatter(fmt)
            root.addHandler(handler)
        except OSError:
            log.debug("could not open daemon log %s", log_file, exc_info=True)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    def _log_uncaught(exc_type, exc, tb):  # pragma: no cover - crash path
        log.critical("db daemon crashed", exc_info=(exc_type, exc, tb))

    def _log_thread(args):  # pragma: no cover - crash path
        log.error(
            "db daemon thread %s crashed", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Threnody single-writer DB daemon")
    parser.add_argument("db_path")
    parser.add_argument("--socket", default=None)
    parser.add_argument("--idle-timeout", type=float, default=900.0)
    parser.add_argument("--max-lifetime", type=float, default=86400.0)
    parser.add_argument(
        "--watch-interval", type=float, default=None,
        help=f"file-identity check cadence in seconds (default ${WATCH_INTERVAL_ENV} or "
             f"{_DEFAULT_WATCH_INTERVAL_S:g})",
    )
    parser.add_argument(
        "--log-file", default=None,
        help="rotating log file (default <db dir>/logs/db_daemon.log; '-' disables)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    log_file = (
        None if args.log_file == "-"
        else Path(args.log_file) if args.log_file
        else default_log_path(args.db_path)
    )
    _configure_logging(log_file, args.log_level)
    daemon = DBDaemon(
        args.db_path, socket_path=args.socket, idle_timeout_s=args.idle_timeout,
        watch_interval_s=args.watch_interval, max_lifetime_s=args.max_lifetime,
    )
    _install_signal_handlers(daemon)
    # Belt and braces: _cleanup is idempotent, and an unhandled exit path that
    # bypasses serve()'s finally still needs the socket gone.
    atexit.register(daemon._cleanup)
    rc = daemon.serve()
    if rc == EXIT_RESTART:
        # serve() already ran _cleanup (socket, access lock, election lock).
        # os._exit rather than sys.exit: interpreter teardown would deallocate
        # the still-open SQLite handles, and closing the last WAL connection
        # checkpoints and unlinks -wal/-shm *by path* — after a foreign swap,
        # somebody else's live files. The kernel releases fds and locks on exit
        # without touching any path.
        logging.shutdown()
        os._exit(rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
