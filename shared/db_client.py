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
"""
from __future__ import annotations

import logging
import os
import socket
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .db_ipc import decode, encode, recv_frame, send_frame
from .db_daemon import socket_path_for

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
    """

    def __init__(self, db: "RemoteDatabase", session: str) -> None:
        self._db = db
        self._session = session

    def execute(self, sql: str, params: Any = ()) -> RemoteCursor:
        resp = self._db._rpc(
            "conn_execute", session=self._session, sql=sql, params=_positional_params(params)
        )
        return RemoteCursor(decode(resp.get("rows", [])), resp.get("lastrowid"), resp.get("rowcount"))

    def executemany(self, sql: str, seq_of_params: Any) -> RemoteCursor:
        resp = self._db._rpc(
            "conn_executemany", session=self._session, sql=sql,
            seq=[_positional_params(p) for p in seq_of_params],
        )
        return RemoteCursor([], resp.get("lastrowid"), resp.get("rowcount"))

    def commit(self) -> None:
        self._db._rpc("conn_commit", session=self._session)

    def rollback(self) -> None:
        self._db._rpc("conn_rollback", session=self._session)

    def close(self) -> None:
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
        self._socket_path = (getattr(daemon_cfg, "socket_path", "") or "") or socket_path_for(db_path)
        self._connect_timeout_s = float(getattr(daemon_cfg, "connect_timeout_s", 5.0))
        self._idle_timeout_s = float(getattr(daemon_cfg, "idle_timeout_s", 900.0))
        self._fallback_ok = bool(getattr(daemon_cfg, "fallback_to_direct", True))
        self._local = threading.local()
        self._spawn_lock = threading.Lock()
        self._direct = None  # lazily-created fallback Database if the daemon dies

    # -- socket / spawn -------------------------------------------------
    def _spawn_daemon(self) -> None:
        with self._spawn_lock:
            # Another thread may have spawned + connected already.
            if os.path.exists(self._socket_path):
                return
            import sys as _sys
            # Spawn from the package root (where `shared/` lives), not the DB dir —
            # the two differ under tempdirs/tests and when THRENODY_INSTALL_DIR is set.
            pkg_root = str(Path(__file__).resolve().parent.parent)
            cmd = [
                _sys.executable, "-m", "shared.db_daemon", self._db_path,
                "--socket", self._socket_path, "--idle-timeout", str(self._idle_timeout_s),
            ]
            env = {**os.environ}
            env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
            try:
                subprocess.Popen(
                    cmd, cwd=pkg_root, start_new_session=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                )
            except Exception as exc:  # pragma: no cover
                raise ConnectionError(f"failed to spawn db daemon: {exc}") from exc

    def _connect(self) -> socket.socket:
        deadline = time.monotonic() + self._connect_timeout_s
        spawned = False
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(self._connect_timeout_s)
                sock.connect(self._socket_path)
                return sock
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                last_exc = exc
                if not spawned:
                    self._spawn_daemon()
                    spawned = True
                time.sleep(0.05)
        raise ConnectionError(f"db daemon unreachable at {self._socket_path}: {last_exc}")

    def _sock(self) -> socket.socket:
        sock = getattr(self._local, "sock", None)
        if sock is None:
            sock = self._connect()
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

    # -- RPC ------------------------------------------------------------
    def _rpc(self, kind: str, **fields: Any) -> dict:
        req = {"kind": kind, **{k: encode(v) for k, v in fields.items()}}
        for attempt in range(2):  # one transparent reconnect on a broken socket
            sent = False
            try:
                sock = self._sock()
                send_frame(sock, req)
                # Past this point the daemon may have received and applied the
                # request; a partial/failed sendall (sent still False) cannot have
                # been applied, so only that case is safe to retry.
                sent = True
                resp = recv_frame(sock)
                break
            except (ConnectionError, OSError) as exc:
                self._drop_sock()
                # Never re-send a frame that was already delivered: we cannot tell
                # whether the daemon applied it, and a blind retry would double a
                # write (INSERT into escalations / approval_queue / telemetry ...).
                # Retry only pre-delivery failures (stale cached socket, connect,
                # partial send) — those are unambiguously not-yet-applied.
                if sent or attempt == 1:
                    raise ConnectionError(f"db daemon rpc failed: {exc}") from exc
        if not resp.get("ok", False):
            _raise_remote(resp.get("error", {}))
        return resp

    def _call(self, method: str, args: tuple, kwargs: dict) -> Any:
        try:
            resp = self._rpc("call", method=method, args=list(args), kwargs=kwargs)
        except ConnectionError:
            if self._fallback_ok:
                return getattr(self._direct_db(), method)(*args, **kwargs)
            raise
        return decode(resp.get("result"))

    def _direct_db(self):
        """Lazily open a direct Database as a degraded fallback (daemon down)."""
        if self._direct is None:
            from .db import Database
            log.warning("db daemon unavailable — falling back to direct DB for this process")
            self._direct = Database(
                Path(self._db_path), resilience=getattr(self._config, "resilience", None)
            )
        return self._direct

    # -- Database-compatible surface -----------------------------------
    def __getattr__(self, name: str):
        # Called only for attributes not defined on the instance/class → treat as
        # a proxied Database method. (Private names never proxy.)
        if name.startswith("_"):
            raise AttributeError(name)

        def _proxy(*args: Any, **kwargs: Any) -> Any:
            return self._call(name, args, kwargs)

        return _proxy

    @property
    def last_integrity_ok(self):
        try:
            return decode(self._rpc("getattr", name="last_integrity_ok").get("result"))
        except Exception:
            return None

    @property
    def last_backup_ts(self):
        try:
            return decode(self._rpc("getattr", name="last_backup_ts").get("result"))
        except Exception:
            return None

    @contextmanager
    def conn(self) -> Iterator[RemoteConnection]:
        try:
            resp = self._rpc("conn_open")
        except ConnectionError:
            if self._fallback_ok:
                with self._direct_db().conn() as c:
                    yield c  # type: ignore[misc]
                return
            raise
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


def open_database(db_path: str | Path | None = None, *, config=None):
    """Return a daemon-backed RemoteDatabase when enabled+reachable, else a direct Database.

    Safe default: any failure to reach/spawn the daemon falls back to a direct
    ``Database`` (today's behavior) unless the operator disables fallback.
    """
    from .config import TGsConfig
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
        except Exception as exc:
            if not getattr(daemon_cfg, "fallback_to_direct", True):
                raise
            log.warning("db daemon unavailable (%s) — using direct DB", exc)

    backup_keep = int(getattr(config, "db_backup_keep", 3) or 3)
    backup_interval_hours = int(getattr(config, "db_backup_interval_hours", 6) or 0)
    resilience = getattr(config, "resilience", None)
    if resolved_path:
        return Database(
            Path(resolved_path),
            backup_keep=backup_keep,
            backup_interval_hours=backup_interval_hours,
            resilience=resilience,
        )
    return Database(
        backup_keep=backup_keep,
        backup_interval_hours=backup_interval_hours,
        resilience=resilience,
    )
