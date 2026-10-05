#!/usr/bin/env python3
"""Cross-process locks that guard the live SQLite files without touching them.

Two locks live here, both on sidecar files SQLite never opens:

``<db>.access.lock`` — readers/writer lock over *the right to swap files*.
    Every process that holds connections to the live database holds it SHARED
    for as long as it does. Destructive maintenance — restoring a backup,
    quarantining, swapping in a salvaged image, discarding ``-wal``/``-shm`` —
    needs it EXCLUSIVE, so it can only happen while nobody has the database
    open. SQLite's own locks cannot answer that question: an idle WAL reader
    holds only ``-shm`` locks, so ``BEGIN EXCLUSIVE`` succeeds right past it, and
    a file swapped under that reader turns every later connection in its process
    into ``disk I/O error``.

``<db>.daemon.lock`` — the db daemon's election lock (``election_lock_path``).
    ``DBDaemon._elect`` keeps its own descriptor handling (and the same
    flock-plus-inode-check discipline); only the path is shared from here.

Why ``flock`` and never ``fcntl``/``lockf``: POSIX record locks belong to the
(process, inode) pair and are dropped when the process closes *any* descriptor
for that inode — including one opened by unrelated code. That is precisely the
hazard that bit ``cache.db`` itself (a stray ``os.open``/``os.close`` silently
released SQLite's own locks). ``flock`` locks belong to the open file
description: an unrelated close cannot release them, the kernel releases them
when the holder dies (SIGKILL included), and two descriptions opened in one
process conflict with each other, which is what lets two ``Database`` instances
in one process exclude each other. On macOS ``flock`` and ``fcntl`` locks may
interact on the same file, so ``flock`` is kept off the database file entirely —
that is the other reason the lock lives on a separate file.

The lock files are never unlinked: a path that is deleted and recreated hands
the next locker a fresh inode and two "exclusive" holders. Every acquisition
re-checks that the path still names the inode it locked and retries otherwise.

Descriptors are opened ``O_CLOEXEC`` so an exec'd child never inherits a hold;
a plain ``fork()`` child does share the description (and therefore the lock)
until it closes it — the same semantics as any inherited descriptor.

Off POSIX (no ``fcntl``) every lock degrades to a no-op handle. The rest of the
DB layer is POSIX-first; there it would only lose the protection, not function.
"""
from __future__ import annotations

import logging
import os
import time
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:  # POSIX-only.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    _fcntl = None

log = logging.getLogger(__name__)

SHARED = "shared"
EXCLUSIVE = "exclusive"

# Poll cadence while waiting on a contended lock. flock has no timeout of its
# own, so a bounded wait is LOCK_NB in a loop.
_POLL_MIN_S = 0.005
_POLL_MAX_S = 0.05

_noop_warned = False


def access_lock_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".access.lock")


def election_lock_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".daemon.lock")


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        log.debug("closing lock fd %s failed", fd, exc_info=True)


class LockHandle:
    """One held ``flock``. Release is idempotent and also runs on GC."""

    def __init__(self, path: Path, mode: str, fd: int | None, ino: int | None) -> None:
        self.path = path
        self.mode = mode
        self.fd = fd
        self.ino = ino
        # Closing the description is what releases the flock, so a handle that
        # is dropped without release() (an unclosed Database) still frees it.
        self._finalizer = (
            weakref.finalize(self, _close_quietly, fd) if fd is not None else None
        )

    @property
    def held(self) -> bool:
        return self._finalizer is None or self._finalizer.alive

    def still_current(self) -> bool:
        """True while the lock path still names the inode this handle locked."""
        if self.fd is None or self.ino is None:
            return True
        try:
            return os.stat(self.path).st_ino == self.ino
        except OSError:
            return False

    def release(self) -> None:
        if self._finalizer is not None and self._finalizer.alive:
            if _fcntl is not None and self.fd is not None:
                try:
                    _fcntl.flock(self.fd, _fcntl.LOCK_UN)
                except OSError:
                    log.debug("flock unlock failed for %s", self.path, exc_info=True)
            self._finalizer()  # closes the fd exactly once

    def __enter__(self) -> "LockHandle":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _noop_handle(path: Path, mode: str) -> LockHandle:
    global _noop_warned
    if not _noop_warned:
        _noop_warned = True
        log.debug("fcntl unavailable: %s locks are no-ops on this platform", path.name)
    return LockHandle(path, mode, None, None)


def _open_lock_file(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(str(path), flags, 0o600)


def acquire(path: str | Path, mode: str, timeout_s: float | None = None) -> LockHandle | None:
    """Take ``mode`` (SHARED/EXCLUSIVE) on *path*. None when *timeout_s* elapses.

    ``timeout_s=None`` waits indefinitely; ``0`` is a single non-blocking try.
    """
    lock_path = Path(path)
    if _fcntl is None:
        return _noop_handle(lock_path, mode)
    op = _fcntl.LOCK_EX if mode == EXCLUSIVE else _fcntl.LOCK_SH
    deadline = None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
    delay = _POLL_MIN_S
    while True:
        fd = _open_lock_file(lock_path)
        try:
            while True:
                try:
                    _fcntl.flock(fd, op | _fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        _close_quietly(fd)
                        return None
                    time.sleep(delay)
                    delay = min(delay * 2, _POLL_MAX_S)
            ino = os.fstat(fd).st_ino
            try:
                current = os.stat(lock_path).st_ino == ino
            except FileNotFoundError:
                current = False
        except BaseException:
            _close_quietly(fd)
            raise
        if current:
            return LockHandle(lock_path, mode, fd, ino)
        # The path was replaced between open() and flock(): our lock is on an
        # orphaned inode and protects nothing. Drop it and lock the live one.
        _close_quietly(fd)
        if deadline is not None and time.monotonic() >= deadline:
            return None


class AccessLock:
    """Readers/writer lock on ``<db>.access.lock`` (see the module docstring)."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = access_lock_path(db_path)

    def acquire_shared(self, timeout_s: float | None = None) -> LockHandle | None:
        return acquire(self.path, SHARED, timeout_s)

    @contextmanager
    def hold_shared(self, timeout_s: float | None = None) -> Iterator[LockHandle | None]:
        handle = self.acquire_shared(timeout_s)
        try:
            yield handle
        finally:
            if handle is not None:
                handle.release()

    def try_exclusive(self, timeout_s: float = 0.0) -> LockHandle | None:
        return acquire(self.path, EXCLUSIVE, timeout_s)

    def probe(self) -> str:
        """``unused`` / ``shared`` / ``exclusive`` / ``unsupported``, without waiting.

        A process that itself holds SHARED reads ``shared`` — callers that care
        report their own hold alongside.
        """
        if _fcntl is None:
            return "unsupported"
        handle = acquire(self.path, EXCLUSIVE, 0.0)
        if handle is not None:
            handle.release()
            return "unused"
        handle = acquire(self.path, SHARED, 0.0)
        if handle is not None:
            handle.release()
            return "shared"
        return "exclusive"


def access_lock(db_path: str | Path) -> AccessLock:
    return AccessLock(db_path)
