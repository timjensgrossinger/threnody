"""Rotating file log for the MCP server and the hook entry points.

The MCP server speaks JSON-RPC over stdio, so its stderr is effectively invisible
to the user, and ``logging.basicConfig(level=WARNING)`` dropped everything below
WARNING anyway. Failures that matter for routing, reporting and learning (a
receipt that did not persist, a finalize that never ran) therefore vanished.

:func:`configure_file_logging` adds one ``RotatingFileHandler`` (1 MB x 5, the
same budget as the db daemon's log) at ``<install>/logs/threnody.log`` and lets
the ``shared`` package and the calling component log at INFO into it. stderr is
pinned to WARNING so the extra INFO records never reach the host's stream.

Environment:

* ``THRENODY_LOG_DIR`` — directory for ``threnody.log`` (default ``<install>/logs``).
* ``THRENODY_LOG_LEVEL`` — file level (default ``INFO``; ``DEBUG`` for diagnosis).

Setup never raises: an unwritable directory leaves logging exactly as it was.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

LOG_DIR_ENV = "THRENODY_LOG_DIR"
LOG_LEVEL_ENV = "THRENODY_LOG_LEVEL"
LOG_FILE_NAME = "threnody.log"
# Same rotation budget as shared/db_daemon.py's daemon log.
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 5
_DEFAULT_LEVEL = logging.INFO
_HANDLER_MARKER = "_threnody_file_log"
_STDERR_MARKER = "_threnody_stderr_log"
_FORMAT = "%(asctime)s %(levelname)s pid=%(process)d %(threadName)s [%(component)s] %(name)s: %(message)s"

log = logging.getLogger(__name__)


def _install_root() -> Path:
    # shared/ lives directly under the install (or checkout) root.
    return Path(__file__).resolve().parent.parent


def log_file_path() -> Path:
    """Where ``threnody.log`` is (or would be) written. Pure; creates nothing."""
    raw = str(os.environ.get(LOG_DIR_ENV) or "").strip()
    base = Path(raw).expanduser() if raw else _install_root() / "logs"
    return base / LOG_FILE_NAME


def _resolve_level() -> int:
    raw = str(os.environ.get(LOG_LEVEL_ENV) or "").strip().upper()
    if not raw:
        return _DEFAULT_LEVEL
    level = logging.getLevelName(raw)
    return level if isinstance(level, int) else _DEFAULT_LEVEL


class _ComponentFilter(logging.Filter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self._component = component

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "component"):
            record.component = self._component
        return True


def _existing_handler(root: logging.Logger) -> logging.Handler | None:
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            return handler
    return None


def _raise_loggers(names: tuple[str, ...], level: int) -> None:
    """Let *names* emit at *level*; never lower a logger an operator set more verbose."""
    for name in names:
        target = logging.getLogger(name)
        if target.level == logging.NOTSET or target.level > level:
            target.setLevel(level)


def configure_file_logging(
    component: str,
    *,
    logger_names: tuple[str, ...] | list[str] = (),
) -> Path | None:
    """Attach the rotating ``threnody.log`` handler once per process.

    *component* labels every line (``mcp_server``, ``learning_hook``, ...), so one
    file can hold the server and the short-lived hook processes. *logger_names*
    are raised to the file level in addition to ``shared`` — pass the caller's
    own logger name (``mcp_server.py`` run as a script logs as ``__main__``).

    Returns the log file path, or ``None`` when the file could not be opened.
    Never raises.
    """
    try:
        root = logging.getLogger()
        level = _resolve_level()
        names = ("shared", *[str(n) for n in logger_names if n])
        existing = _existing_handler(root)
        if existing is not None:
            _raise_loggers(names, level)
            base_filename = getattr(existing, "baseFilename", "")
            return Path(base_filename) if base_filename else None

        # Pin pre-existing stream handlers (basicConfig's stderr) to WARNING:
        # raising the ``shared`` logger to INFO below would otherwise push INFO
        # records through them, because propagation checks handler levels only.
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ) and handler.level == logging.NOTSET:
                handler.setLevel(logging.WARNING)

        path = log_file_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
                delay=False,
            )
        except (OSError, ValueError):
            log.warning("could not open threnody log %s", path, exc_info=True)
            return None
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(_ComponentFilter(component or "threnody"))
        setattr(handler, _HANDLER_MARKER, True)
        if not root.handlers:
            # A hook process configures no handler, so its warnings reached stderr
            # through logging.lastResort — which stops firing as soon as the file
            # handler exists. Keep that stderr channel, at WARNING, explicitly.
            stderr_handler = logging.StreamHandler(sys.stderr)
            stderr_handler.setLevel(logging.WARNING)
            setattr(stderr_handler, _STDERR_MARKER, True)
            root.addHandler(stderr_handler)
        root.addHandler(handler)
        _raise_loggers(names, level)
        return path
    except Exception:  # logging setup must never break the server or a hook
        log.warning("threnody file logging setup failed", exc_info=True)
        return None


def remove_file_logging() -> None:
    """Detach and close the handlers added by :func:`configure_file_logging` (tests)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False) or getattr(handler, _STDERR_MARKER, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except (OSError, ValueError):
                log.debug("closing threnody log handler failed", exc_info=True)
