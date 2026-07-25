#!/usr/bin/env python3
"""Single-writer database daemon.

One daemon per database path owns the sole live SQLite connection(s) to
``cache.db``. Every Threnody process talks to it over a local AF_UNIX socket
instead of opening the WAL directly. Because only ONE process ever mmaps the
``-shm`` shared-memory file, the multi-process ``-shm`` truncation race that can
SIGBUS under heavy concurrency disappears entirely, and writers serialize
in-process with no cross-process WAL-index desync.

Run:  python3 -m shared.db_daemon <db_path> [--socket PATH] [--idle-timeout S]

Wire protocol: see shared/db_ipc.py. Requests (``kind``):
  ping                                              -> {ok}
  call     {method, args, kwargs}                   -> {ok, result}
  conn_open                                         -> {ok, session}
  conn_execute {session, sql, params}               -> {ok, rows, lastrowid, rowcount}
  conn_executemany {session, sql, seq}              -> {ok, rowcount, lastrowid}
  conn_commit/conn_rollback/conn_close {session}    -> {ok}
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time

try:  # POSIX-only cross-process election.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None

from pathlib import Path

from .db_ipc import ProtocolError, decode, encode, make_err, make_ok, recv_frame, send_frame

log = logging.getLogger(__name__)


def socket_path_for(db_path: str | Path) -> str:
    return str(Path(db_path)) + ".sock"


def _lock_path_for(db_path: str | Path) -> str:
    return str(Path(db_path)) + ".daemon.lock"


class _Session:
    """A client `with db.conn()` mapped to a dedicated daemon-side connection."""

    __slots__ = ("conn",)

    def __init__(self, conn) -> None:
        self.conn = conn


class DBDaemon:
    def __init__(self, db_path: str, *, socket_path: str | None = None,
                 idle_timeout_s: float = 900.0,
                 client_idle_reap_s: float = 900.0) -> None:
        self._db_path = db_path
        self._socket_path = socket_path or socket_path_for(db_path)
        self._idle_timeout_s = idle_timeout_s
        # Reap a connected-but-silent client handler after this long WITH NO open
        # session, so per-thread client sockets that outlive their worker thread
        # don't pin a daemon handler thread indefinitely. Clients reconnect
        # transparently on next use (db_client retries a stale socket).
        self._client_idle_reap_s = client_idle_reap_s
        self._lock_fd: int | None = None
        self._srv: socket.socket | None = None
        self._db = None  # lazy — created after election
        self._clients = 0
        self._clients_lock = threading.Lock()
        self._last_active = time.monotonic()
        self._stop = threading.Event()

    # -- lifecycle ------------------------------------------------------
    def _elect(self) -> bool:
        """Acquire the exclusive daemon lock. False if another daemon owns it."""
        if _fcntl is None:
            return True  # best-effort: no election off-POSIX
        self._lock_fd = os.open(_lock_path_for(self._db_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _fcntl.flock(self._lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError:
            os.close(self._lock_fd)
            self._lock_fd = None
            return False

    def _bind(self) -> None:
        # Winner of the election owns the socket; clear any stale one first.
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self._socket_path)
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
        # Keeper connection: hold one live connection for the daemon's lifetime so
        # the DB is never quiescent and -shm is never re-truncated underneath us.
        self._keeper = db._connect()
        return db

    def serve(self) -> int:
        if not self._elect():
            log.info("another db daemon owns %s; exiting", self._db_path)
            return 0
        self._db = self._open_db()
        self._bind()
        log.info("db daemon serving %s on %s (pid=%s)", self._db_path, self._socket_path, os.getpid())
        idle_thread = threading.Thread(target=self._idle_watch, name="db-daemon-idle", daemon=True)
        idle_thread.start()
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
            self._cleanup()
        return 0

    def _idle_watch(self) -> None:
        if self._idle_timeout_s <= 0:
            return
        while not self._stop.wait(min(30.0, self._idle_timeout_s)):
            with self._clients_lock:
                idle = self._clients == 0 and (time.monotonic() - self._last_active) > self._idle_timeout_s
            if idle:
                log.info("db daemon idle for %.0fs — exiting", self._idle_timeout_s)
                self._stop.set()
                try:
                    if self._srv:
                        self._srv.close()
                except Exception:
                    pass
                return

    def _cleanup(self) -> None:
        try:
            if self._srv:
                self._srv.close()
        except Exception:
            pass
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        except Exception:
            log.debug("socket unlink failed", exc_info=True)
        try:
            if self._db is not None:
                self._db.close()
        except Exception:
            log.debug("db close failed", exc_info=True)

    # -- request handling ----------------------------------------------
    def _handle_client(self, client: socket.socket) -> None:
        with self._clients_lock:
            self._clients += 1
            self._last_active = time.monotonic()
        # Accepted sockets can inherit the listener's 1.0s poll timeout — set our
        # own explicitly (reap window, or blocking when reaping is disabled) so a
        # client is never reaped on the inherited 1s.
        try:
            reap = self._client_idle_reap_s
            client.settimeout(reap if reap and reap > 0 else None)
        except OSError:  # pragma: no cover - platform edge
            pass
        sessions: dict[str, _Session] = {}
        session_seq = 0
        try:
            while not self._stop.is_set():
                try:
                    req = recv_frame(client)
                except socket.timeout:
                    # Silent past the reap window. If the client holds an open
                    # session it is a live transactional peer — stop reaping (go
                    # blocking) so we never tear down its transaction or mis-frame.
                    # Otherwise reap this handler; the client reconnects on demand.
                    if sessions:
                        try:
                            client.settimeout(None)
                        except OSError:  # pragma: no cover
                            pass
                        continue
                    break
                except (ConnectionError, ProtocolError):
                    break
                try:
                    resp, session_seq = self._dispatch(req, sessions, session_seq)
                except Exception as exc:  # any handler failure → structured error
                    resp = make_err(type(exc).__name__, str(exc))
                try:
                    send_frame(client, resp)
                except (ConnectionError, OSError):
                    break
        finally:
            # Roll back + release any transactions the disconnecting client held.
            for sess in sessions.values():
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
                self._last_active = time.monotonic()

    def _dispatch(self, req: dict, sessions: dict, session_seq: int) -> tuple[dict, int]:
        kind = req.get("kind")
        if kind == "ping":
            return make_ok(pong=True), session_seq

        if kind == "call":
            method = req.get("method")
            args = decode(req.get("args", []))
            kwargs = decode(req.get("kwargs", {}))
            if not isinstance(method, str) or method.startswith("_"):
                raise ProtocolError(f"illegal method: {method!r}")
            target = getattr(self._db, method, None)
            if target is None or not callable(target):
                raise AttributeError(f"Database has no callable method {method!r}")
            result = target(*args, **(kwargs or {}))
            return make_ok(result=result), session_seq

        if kind == "getattr":
            # Read a public, non-callable attribute/property (e.g. last_integrity_ok).
            name = req.get("name")
            if not isinstance(name, str) or name.startswith("_"):
                raise ProtocolError(f"illegal attribute: {name!r}")
            value = getattr(self._db, name)
            if callable(value):
                raise ProtocolError(f"{name!r} is callable; use 'call'")
            return make_ok(result=value), session_seq

        if kind == "conn_open":
            session_seq += 1
            sid = f"s{session_seq}"
            sessions[sid] = _Session(self._db._connect())
            return make_ok(session=sid), session_seq

        # session-scoped ops
        sid = req.get("session")
        sess = sessions.get(sid) if isinstance(sid, str) else None
        if sess is None:
            raise ProtocolError(f"unknown session: {sid!r}")

        if kind == "conn_execute":
            sql = req.get("sql")
            params = decode(req.get("params", []))
            cur = sess.conn.execute(sql, params) if params else sess.conn.execute(sql)
            rows = cur.fetchall()
            return make_ok(rows=rows, lastrowid=cur.lastrowid, rowcount=cur.rowcount), session_seq

        if kind == "conn_executemany":
            sql = req.get("sql")
            seq = decode(req.get("seq", []))
            cur = sess.conn.executemany(sql, seq)
            return make_ok(rowcount=cur.rowcount, lastrowid=cur.lastrowid), session_seq

        if kind == "conn_commit":
            sess.conn.commit()
            return make_ok(), session_seq

        if kind == "conn_rollback":
            sess.conn.rollback()
            return make_ok(), session_seq

        if kind == "conn_close":
            try:
                sess.conn.close()
            finally:
                sessions.pop(sid, None)
            return make_ok(), session_seq

        raise ProtocolError(f"unknown request kind: {kind!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Threnody single-writer DB daemon")
    parser.add_argument("db_path")
    parser.add_argument("--socket", default=None)
    parser.add_argument("--idle-timeout", type=float, default=900.0)
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.WARNING))
    daemon = DBDaemon(
        args.db_path, socket_path=args.socket, idle_timeout_s=args.idle_timeout
    )
    return daemon.serve()


if __name__ == "__main__":
    sys.exit(main())
