#!/usr/bin/env python3
"""
Threnody MCP server.

Exposes planning, routing, and cache via Model Context Protocol (JSON-RPC/stdio).
Supports both Copilot and Claude Code backends depending on what's available.

Register with:
  gh copilot mcp add Threnody -- python3 ~/.local/lib/threnody/mcp_server.py
  claude mcp add Threnody -- python3 ~/.local/lib/threnody/mcp_server.py
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import dataclasses
import difflib
import hashlib
import hmac
import io
import json
import logging
import math
import errno
import signal
import os
import posixpath
import queue
import re
import secrets
import select
import sqlite3
import stat
import sys
import threading
import time
import uuid
import importlib
import inspect
import unicodedata
from pathlib import Path
from typing import Any, Mapping

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from shared.config import CONFIG_YAML, TGsConfig, DEFAULT_ROUTING_EXCEPTION_FILETYPES, DEFAULT_ROUTING_EXCEPTION_PATHS
from shared.version import get_version
from shared.context import is_within_repo, normalize_target_path
from shared.router import TaskRouter
from shared.planner import (
    Planner,
    PlannerParseError,
    GhCopilotBackend,
    ClaudeCodeBackend,
    ProviderAgnosticBackend,
    FanOutConfig,
    make_auto_topology_decision,
)
from shared.orchestrator import (
    Orchestrator,
    Provider,
    clamp_swarm_agent_count,
    seed_resume_from_checkpoint,
    _get_session_manager,
)
from shared.db import (
    Database,
    DEFAULT_PROJECT_FANOUT_CAP,
    PROJECT_SETTING_KEYS,
    ROUTING_GUARD_MODE_DIRECT,
    ROUTING_GUARD_MODE_EXECUTE_SUBTASK,
    ROUTING_GUARD_MODE_ROUTED_PLAN,
    ROUTING_GUARD_TTL_SECONDS,
)
from shared.eval import set_background_loop
from shared.model_catalog import ModelCatalog
from shared.memory import (
    MemoryNotFoundError,
    MemoryRequestError,
    memory_delete,
    memory_get,
    memory_list,
    memory_refresh_swarm_state_from_db,
    memory_set,
)
from shared import outcomes as shared_outcomes
from shared.status import build_status_snapshot
from shared.swarm import (
    build_wave_progress_payload,
    get_coordinator_round_checkpoint_by_index,
    list_resume_checkpoints,
)
from shared.agents import (
    approval_queue_list as list_approval_queue_items,
    approval_queue_approve as approve_queue_item,
    approval_queue_reject as reject_queue_item,
    approval_queue_merge as merge_queue_item,
    DEFAULT_PENDING_APPROVAL_LIMIT,
    activate_agent_locally,
    evaluate_pattern_readiness,
    register_agent_to_capable_clis,
)
from copilot.providers import CopilotProvider
from shared.resilience import RetryPolicy as _RetryPolicy
from shared.discovery import (
    ProviderRegistry,
    ProviderUsageChecker,
    caller_from_client_name,
    detect_caller,
    get_registry,
)
from shared.quota import ProviderQuotaService
from shared.adapters import ProviderCapability
from shared.snapshot import FileDiff, FileSnapshot, apply_unified_diff

log = logging.getLogger(__name__)
_EXECUTE_SWARM_PREVIEW_SECRET_FILE = (
    BASE / ".runtime" / "preview-token-secret"
)
_SELECT_PROVIDER_SUPPORTS_EFFORT_CACHE: dict[object, bool] = {}
_ROUTE_RESPONSE_SELECTION_KEYS = {
    "model",
    "provider",
    "provider_id",
    "is_free",
    "billing_tier",
    "provider_cost_hint",
    "cost_rank",
    "billing_source",
    "effort",
    "effort_source",
    "quota_source",
    "quota_routing_action",
    "model_id",
    "model_display_name",
    "model_available",
    "model_deprecated",
    "discovery_source",
    "discovered_at",
    "catalog_stale_until",
    "tier_reason",
    "fallback_reason",
}
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


# ---------------------------------------------------------------------------
# Lazy globals
# ---------------------------------------------------------------------------

_config: TGsConfig | None = None
_db: Database | None = None
_router: TaskRouter | None = None
_planner: Planner | None = None
_orchestrator: Orchestrator | None = None
_model_catalog: ModelCatalog | None = None
_shutdown_registered: bool = False
_health_probe_thread: threading.Thread | None = None
_client_name: str | None = None  # set from MCP initialize handshake
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_loop_thread: threading.Thread | None = None
_bg_loop_ready = threading.Event()
_bg_loop_lock = threading.Lock()
_stdout_lock = threading.Lock()  # serialise all JSON-RPC writes

# Active subtask tracking (thread-safe)
_active_subtasks: dict[str, dict] = {}
_subtask_history: list[dict] = []  # last 20 completed
_subtask_cancel_events: dict[str, threading.Event] = {}
_subtasks_lock = threading.Lock()


class SubtaskExecutionTimeout(RuntimeError):
    """Raised when provider launch or execution exceeds the task deadline."""


class SubtaskCancelled(RuntimeError):
    """Raised when a cancellation request is observed by the launcher."""


def _terminalize_active_subtask(
    task_id: str,
    status: str,
    *,
    elapsed: float | None = None,
    updates: Mapping[str, object] | None = None,
) -> dict | None:
    """Move one active subtask to bounded history exactly once."""
    with _subtasks_lock:
        entry = _active_subtasks.pop(task_id, None)
        if entry is None:
            return None
        entry["status"] = status
        if elapsed is not None:
            entry["elapsed"] = elapsed
        if updates:
            entry.update(updates)
        _subtask_history.append(entry)
        if len(_subtask_history) > 20:
            _subtask_history.pop(0)
        _write_status_file()
        return entry


def _request_subtask_cancel(task_id: str, reason: str) -> tuple[dict | None, int | None]:
    """Record cancellation and terminate a provider process when available."""
    pid: int | None = None
    with _subtasks_lock:
        entry = _active_subtasks.get(task_id)
        event = _subtask_cancel_events.get(task_id)
        if entry is None:
            return None, None
        entry["cancellation_reason"] = reason
        entry["status"] = "timing_out" if reason == "timeout" else "cancelling"
        pid = entry.get("pid")
        if event is not None:
            event.set()
        _write_status_file()
        snapshot = dict(entry)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            log.debug("Failed to terminate cancelled subtask pid=%s", pid, exc_info=True)
    return snapshot, pid


def _cancel_all_active_subtasks(reason: str) -> int:
    """Request cancellation for every active subtask and return the count."""
    with _subtasks_lock:
        task_ids = list(_active_subtasks)
    for task_id in task_ids:
        _request_subtask_cancel(task_id, reason)
    return len(task_ids)


def _store_active_pid(
    task_id: str,
    pid: int,
    cancel_event: threading.Event | None = None,
) -> None:
    """Thread-safe PID registration with cancellation-race handling."""
    should_terminate = cancel_event is not None and cancel_event.is_set()
    with _subtasks_lock:
        entry = _active_subtasks.get(task_id)
        if entry is not None:
            entry["pid"] = pid
            if should_terminate:
                reason = entry.get("cancellation_reason")
                entry["status"] = "timing_out" if reason == "timeout" else "cancelling"
            else:
                entry["status"] = "running"
            _write_status_file()
    if should_terminate:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            log.debug("Failed to terminate late-starting subtask pid=%s", pid, exc_info=True)


def _run_subtask_provider_call(
    task_id: str,
    *,
    deadline: float,
    timeout_seconds: int,
    cancel_event: threading.Event,
    call: Any,
) -> dict:
    """Run a provider call behind a deadline that includes provider startup."""
    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            result_queue.put((True, call()))
        except BaseException as exc:
            result_queue.put((False, exc))

    threading.Thread(
        target=_worker,
        name=f"tgs-subtask-{task_id[:24]}",
        daemon=True,
    ).start()

    while True:
        if cancel_event.is_set():
            raise SubtaskCancelled(f"Subtask {task_id} was cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _request_subtask_cancel(task_id, "timeout")
            raise SubtaskExecutionTimeout(
                f"Subtask {task_id} exceeded its {timeout_seconds}s deadline"
            )
        try:
            succeeded, payload = result_queue.get(timeout=min(0.1, remaining))
        except queue.Empty:
            continue
        if cancel_event.is_set():
            raise SubtaskCancelled(f"Subtask {task_id} was cancelled")
        if succeeded:
            if not isinstance(payload, dict):
                raise RuntimeError("Provider registry returned a non-object result")
            return payload
        if isinstance(payload, BaseException):
            raise payload
        raise RuntimeError("Provider registry failed without an exception")
_catalog_refresh_lock = threading.Lock()
_init_lock = threading.Lock()
_catalog_refresh_future: Future | None = None
_registry_override_lock = threading.Lock()
_registry_override_signature: tuple[bool, int | None, int | None] | None = None
_registry_override_cache: dict[str, object] | None = None
_MAX_PROGRESS_TOKEN_LENGTH = 256
_MAX_CONCURRENT_PROGRESS_HEARTBEATS = 16
_MANUAL_APPROVAL_TIMEOUT_SECONDS = 300
_progress_heartbeat_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_PROGRESS_HEARTBEATS)
_EXECUTE_SWARM_MAX_TASK_CHARS = 10_000
_EXECUTE_SWARM_MAX_URGENCY_HINT_CHARS = 512
_EXECUTE_SWARM_MAX_PREVIEW_TOKEN_CHARS = 256
_EXECUTE_SWARM_RATE_LIMIT_WINDOW_SECONDS = 10.0
_EXECUTE_SWARM_RATE_LIMIT_MAX_CALLS = 5
_EXECUTE_SWARM_RATE_LIMIT_MAX_CALLERS = 256
_EXECUTE_SWARM_PREVIEW_EXPIRY_SECONDS = 300
_execute_swarm_rate_limit: dict[str, list[float]] = {}
_execute_swarm_rate_limit_lock = threading.Lock()

# Thread-local storage for MCP progress tokens plumbed from tools/call
_request_context = threading.local()

# Providers with confirmed native per-call effort support today.
# Keep this intentionally small until support is wired through more adapters.
_EXPLICIT_EFFORT_SUPPORTED_PROVIDERS = frozenset({
    "claude-code",
    "codex",
    "cursor",
    "opencode",
})

# Live status file for external monitoring (tail from another terminal)
_STATUS_FILE = Path("/tmp/threnody-status.json")
_MODEL_CATALOG_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="model-catalog-refresh",
)


def _write_status_file() -> None:
    """Write current subtask state to a JSON file for external monitoring.

    Must be called while holding _subtasks_lock.
    """
    try:
        provider_health: list[dict] = []
        recent_failures: list[dict] = []
        if _db is not None:
            try:
                provider_health = _db.iter_provider_health()
            except Exception:
                log.debug("provider health iteration failed", exc_info=True)
            try:
                with _db.conn() as conn:
                    rows = conn.execute(
                        "SELECT payload, created_ts FROM swarm_events"
                        " WHERE event_type='provider_failure'"
                        " ORDER BY created_ts DESC LIMIT 5"
                    ).fetchall()
                    for row in rows:
                        try:
                            payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
                        except Exception:
                            payload = {}
                        payload["ts"] = row[1]
                        recent_failures.append(payload)
            except Exception:
                log.debug("swarm events query failed", exc_info=True)
        snapshot = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "active": [
                {k: v for k, v in entry.items() if k != "start_mono"}
                for entry in _active_subtasks.values()
            ],
            "recent": [
                {k: v for k, v in entry.items() if k != "start_mono"}
                for entry in _subtask_history[-10:]
            ],
            "provider_health": provider_health,
            "recent_failures": recent_failures,
        }
        # Atomic write: write to temp then rename
        tmp = _STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2, default=str))
        tmp.rename(_STATUS_FILE)
    except Exception:
        log.warning("status file write failed", exc_info=True)


class RecursionDepthError(RuntimeError):
    """Raised when cross-shell recursion exceeds the configured depth limit."""


def _run_bg_loop() -> None:
    global _bg_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _bg_loop = loop
    set_background_loop(loop)
    _bg_loop_ready.set()
    loop.run_forever()


def _ensure_bg_loop() -> None:
    global _bg_loop_thread
    if _bg_loop is not None and _bg_loop.is_running():
        set_background_loop(_bg_loop)
        return

    with _bg_loop_lock:
        if _bg_loop is not None and _bg_loop.is_running():
            set_background_loop(_bg_loop)
            return
        _bg_loop_ready.clear()
        _bg_loop_thread = threading.Thread(
            target=_run_bg_loop,
            name="tgs-bg-loop",
            daemon=True,
        )
        _bg_loop_thread.start()

    if not _bg_loop_ready.wait(timeout=5):
        raise RuntimeError("Background event loop failed to start")


def _run_health_probe_loop() -> None:
    """Daemon: every 30 s probe QUARANTINED providers whose cooldown has elapsed."""
    from shared.health import record_probe_result as _record_probe_result
    from shared.resilience import AuthProbe as _AuthProbe

    while True:
        try:
            time.sleep(30)
            if _db is None:
                continue
            rows = _db.iter_provider_health()
            now = time.time()
            for row in rows:
                if row.get("state") != "QUARANTINED":
                    continue
                until = row.get("quarantine_until_ts")
                if until is None or now < until:
                    continue
                provider_id = row.get("provider_id")
                if not provider_id:
                    continue
                try:
                    _db.update_provider_health_state(provider_id, "PROBING")
                    _AuthProbe.invalidate(provider_id)
                    ok = _AuthProbe.check(provider_id)
                    _record_probe_result(_db, provider_id, ok)
                    log.info(
                        "health probe %s: %s",
                        provider_id, "ok → HEALTHY" if ok else "fail → QUARANTINED",
                    )
                except Exception:
                    log.debug("health probe error for %s", provider_id, exc_info=True)
        except Exception:
            log.debug("health probe loop error", exc_info=True)


def _log_model_catalog_refresh(future: Future) -> None:
    try:
        result = future.result()
        log.info("Model catalog refresh finished: %s", result)
    except Exception:
        log.warning("Model catalog refresh failed", exc_info=True)


def _run_model_catalog_refresh() -> dict[str, list[str]]:
    if _model_catalog is None:
        return {"refreshed": [], "skipped": [], "cooldown": [], "failed": []}
    registry = _get_registry_with_config()
    return _model_catalog.refresh_all(registry)


def _schedule_model_catalog_refresh() -> None:
    global _catalog_refresh_future
    if _model_catalog is None:
        return

    with _catalog_refresh_lock:
        if _catalog_refresh_future is not None:
            if not _catalog_refresh_future.done():
                return
            try:
                if _catalog_refresh_future.exception() is None:
                    return
            except Exception:
                log.debug("catalog future check raised", exc_info=True)
                return
        # Schedule ModelCatalog.refresh_all() off the hot path so startup returns immediately.
        _catalog_refresh_future = _MODEL_CATALOG_EXECUTOR.submit(_run_model_catalog_refresh)
        _catalog_refresh_future.add_done_callback(_log_model_catalog_refresh)


def _ensure_init() -> tuple[TGsConfig, Database, TaskRouter, Planner, Orchestrator]:
    global _config, _db, _router, _planner, _orchestrator, _model_catalog
    if all(
        item is not None
        for item in (_config, _db, _router, _planner, _orchestrator, _model_catalog)
    ):
        return _config, _db, _router, _planner, _orchestrator

    with _init_lock:
        if all(
            item is not None
            for item in (_config, _db, _router, _planner, _orchestrator, _model_catalog)
        ):
            return _config, _db, _router, _planner, _orchestrator

        needs_full_init = any(
            item is None for item in (_config, _db, _router, _planner, _orchestrator)
        )
        if needs_full_init:
            try:
                config = TGsConfig.from_yaml()
                db = Database(config.db_path, backup_keep=config.db_backup_keep)
                router = TaskRouter(config)
                _planner_registry = None
                try:
                    from shared.discovery import get_registry as _get_registry
                    import dataclasses as _dc
                    _planner_registry = _get_registry(
                        config_overrides=_dc.asdict(config)
                    )
                    caller = _resolve_caller() or "mcp"
                    backend = ProviderAgnosticBackend(_planner_registry, caller=caller)
                except Exception:
                    log.warning("ProviderRegistry unavailable for planner, falling back to GhCopilotBackend")
                    backend = GhCopilotBackend()
                planner = Planner(config, backend, db)
                try:
                    if _planner_registry is not None:
                        from shared.provider_factory import resolve_default_provider
                        provider = resolve_default_provider(
                            _planner_registry,
                            caller=_resolve_caller() or "mcp",
                        )
                    else:
                        provider = CopilotProvider()
                except Exception:
                    log.warning("Provider factory unavailable; falling back to CopilotProvider", exc_info=True)
                    provider = CopilotProvider()
                runtime_registry = None
                runtime_providers_map = None
                try:
                    runtime_registry, runtime_providers_map = _build_runtime_spillover_support(
                        config,
                        db=db,
                    )
                    if not runtime_providers_map:
                        runtime_registry = None
                        runtime_providers_map = None
                except Exception:
                    log.warning("Failed to initialize spillover runtime support", exc_info=True)
                orchestrator = Orchestrator(
                    config,
                    provider,
                    planner,
                    db,
                    project_root=str(_active_workspace_root()),
                    provider_registry=runtime_registry,
                    providers_map=runtime_providers_map,
                    caller=_resolve_caller() or "mcp",
                )
                model_catalog = ModelCatalog(db)
            except Exception:
                _config = None
                _db = None
                _router = None
                _planner = None
                _orchestrator = None
                _model_catalog = None
                raise

            _config = config
            _db = db
            _router = router
            _planner = planner
            _orchestrator = orchestrator
            _model_catalog = model_catalog

            global _shutdown_registered, _health_probe_thread
            if _health_probe_thread is None or not _health_probe_thread.is_alive():
                _health_probe_thread = threading.Thread(
                    target=_run_health_probe_loop,
                    name="tgs-health-probe",
                    daemon=True,
                )
                _health_probe_thread.start()
                log.debug("health probe loop started")

            if not _shutdown_registered:
                import atexit as _atexit

                def _shutdown_handler(signum=None, frame=None):
                    if _db is not None:
                        try:
                            _db.close()
                        except Exception:
                            pass
                    if signum is not None:
                        import sys as _sys
                        _sys.exit(0)

                _atexit.register(_shutdown_handler)
                # signal.signal() only works from the main thread; worker threads
                # (e.g. BLOCKING_TOOLS dispatch) skip this to avoid ValueError.
                if threading.current_thread() is threading.main_thread():
                    signal.signal(signal.SIGTERM, _shutdown_handler)
                _shutdown_registered = True

        elif _model_catalog is None and _db is not None:
            _model_catalog = ModelCatalog(_db)

    assert _config is not None
    assert _db is not None
    assert _router is not None
    assert _planner is not None
    assert _orchestrator is not None
    return _config, _db, _router, _planner, _orchestrator


def _config_file_signature() -> tuple[bool, int | None, int | None]:
    if not CONFIG_YAML.exists():
        return (False, None, None)
    stat = CONFIG_YAML.stat()
    return (True, stat.st_mtime_ns, stat.st_size)


def _provider_override_payload(
    provider_cost_overrides: Mapping[str, object] | None,
    preferred_routing: Mapping[str, object] | None = None,
    provider_timeout_overrides: Mapping[str, object] | None = None,
    endpoint_providers: list[object] | None = None,
    preferred_routing_by_caller: Mapping[str, object] | None = None,
    provider_usage_windows: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    def _serialize_routing_preferences(raw: Mapping[str, object] | None) -> dict[str, list[object]]:
        serialized: dict[str, list[object]] = {}
        if not raw:
            return serialized
        for tier, entries in raw.items():
            if not isinstance(tier, str) or not isinstance(entries, list):
                continue
            serialized_entries: list[object] = []
            for entry in entries:
                to_dict = getattr(entry, "to_dict", None)
                if callable(to_dict):
                    serialized_entries.append(to_dict())
                else:
                    serialized_entries.append(entry)
            if serialized_entries:
                serialized[tier] = serialized_entries
        return serialized

    def _serialize_caller_routing(raw: Mapping[str, object] | None) -> dict[str, dict[str, list[object]]]:
        serialized: dict[str, dict[str, list[object]]] = {}
        if not raw:
            return serialized
        for caller, tier_map in raw.items():
            if not isinstance(caller, str) or not isinstance(tier_map, Mapping):
                continue
            caller_preferences = _serialize_routing_preferences(tier_map)
            if caller_preferences:
                serialized[caller] = caller_preferences
        return serialized

    def _serialize_usage_windows(raw: Mapping[str, object] | None) -> dict[str, object]:
        serialized: dict[str, object] = {}
        if not raw:
            return serialized
        for provider_id, config in raw.items():
            if not isinstance(provider_id, str):
                continue
            windows = getattr(config, "windows", None)
            if windows is None and isinstance(config, Mapping):
                windows = config.get("windows")
            if not isinstance(windows, list):
                continue
            serialized_windows: list[object] = []
            for window in windows:
                if dataclasses.is_dataclass(window):
                    serialized_windows.append(dataclasses.asdict(window))
                elif isinstance(window, Mapping):
                    serialized_windows.append(dict(window))
            normalized_provider_id = _normalize_runtime_provider_key(provider_id)
            if serialized_windows and normalized_provider_id:
                serialized[normalized_provider_id] = {"windows": serialized_windows}
        return serialized

    serialized_preferred_routing = _serialize_routing_preferences(preferred_routing)
    serialized_caller_routing = _serialize_caller_routing(preferred_routing_by_caller)
    serialized_usage_windows = _serialize_usage_windows(provider_usage_windows)

    payload: dict[str, object] = {}
    if provider_cost_overrides:
        payload["provider_cost_overrides"] = provider_cost_overrides
    if serialized_preferred_routing:
        payload["preferred_routing"] = serialized_preferred_routing
    if serialized_caller_routing:
        payload["preferred_routing_by_caller"] = serialized_caller_routing
    if provider_timeout_overrides:
        payload["provider_timeout_overrides"] = dict(provider_timeout_overrides)
    if serialized_usage_windows:
        payload["provider_usage_windows"] = serialized_usage_windows
    if endpoint_providers:
        serialized_endpoint_providers: list[object] = []
        for entry in endpoint_providers:
            to_dict = getattr(entry, "to_dict", None)
            if callable(to_dict):
                serialized_endpoint_providers.append(to_dict())
            else:
                serialized_endpoint_providers.append(entry)
        if serialized_endpoint_providers:
            payload["endpoint_providers"] = serialized_endpoint_providers
    return payload or None


def _registry_config_overrides(config: TGsConfig | None = None) -> dict[str, object] | None:
    global _registry_override_signature, _registry_override_cache

    if config is not None:
        return _provider_override_payload(
            config.provider_cost_overrides,
            config.preferred_routing,
            config.provider_timeout_overrides,
            config.endpoint_providers,
            config.preferred_routing_by_caller,
            config.provider_usage_windows,
        )

    signature = _config_file_signature()
    with _registry_override_lock:
        if signature != _registry_override_signature:
            try:
                fresh_config = TGsConfig.from_yaml()
                _registry_override_cache = _provider_override_payload(
                    fresh_config.provider_cost_overrides,
                    fresh_config.preferred_routing,
                    fresh_config.provider_timeout_overrides,
                    fresh_config.endpoint_providers,
                    fresh_config.preferred_routing_by_caller,
                    fresh_config.provider_usage_windows,
                )
                _registry_override_signature = signature
            except Exception:
                log.warning(
                    "Failed to refresh registry overrides from config.yaml; using last known good snapshot",
                    exc_info=True,
                )
                if _registry_override_signature is None and _config is not None:
                    _registry_override_cache = _provider_override_payload(
                        _config.provider_cost_overrides,
                        _config.preferred_routing,
                        _config.provider_timeout_overrides,
                        _config.endpoint_providers,
                        _config.preferred_routing_by_caller,
                        _config.provider_usage_windows,
                    )
                _registry_override_signature = signature
        return _registry_override_cache


def _get_registry_with_config(config: TGsConfig | None = None):
    global _model_catalog
    overrides = _registry_config_overrides(config)
    if _model_catalog is None and _db is not None:
        _model_catalog = ModelCatalog(_db)
    _schedule_model_catalog_refresh()
    try:
        return get_registry(overrides, db=_db)
    except TypeError:
        return get_registry()


def _normalize_runtime_provider_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    normalized = normalized.replace("_", "-")
    normalized = "-".join(normalized.split())
    return normalized or None


# ---------------------------------------------------------------------------
# MCP protocol helpers
# ---------------------------------------------------------------------------

def send_response(request_id: int | str | None, result: dict) -> None:
    msg = {"jsonrpc": "2.0", "id": request_id, "result": result}
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def send_error(request_id: int | str | None, code: int, message: str) -> None:
    msg = {"jsonrpc": "2.0", "id": request_id,
           "error": {"code": code, "message": message}}
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def send_notification(method: str, params: dict) -> None:
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    with _stdout_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _normalize_progress_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token or len(token) > _MAX_PROGRESS_TOKEN_LENGTH:
        return None
    if any(ord(char) < 32 for char in token):
        return None
    return token


def _heartbeat_loop(
    progress_token: str,
    stop_event: threading.Event,
    interval: int = 15,
) -> None:
    """Send MCP progress notifications at *interval* seconds until stopped.

    Only runs when the client supplied a ``_meta.progressToken`` in the
    original ``tools/call`` request.  Silently stops on write errors
    (e.g. broken pipe) to avoid crashing the worker thread.
    """
    tick = 0
    while not stop_event.wait(interval):
        tick += 1
        try:
            send_notification("notifications/progress", {
                "progressToken": progress_token,
                "progress": tick,
                "total": 0,
            })
        except Exception:
            log.debug("heartbeat write failed for token %s — stopping", progress_token)
            break


def _normalize_swarm_id(swarm_id: object) -> str:
    if not isinstance(swarm_id, str):
        raise ValueError("swarm_id must be a non-empty string")
    normalized_swarm_id = swarm_id.strip()
    if not normalized_swarm_id:
        raise ValueError("swarm_id must be a non-empty string")
    return normalized_swarm_id


def _emit_wave_progress(
    swarm_id: str,
    wave: int,
    completed_subtasks: int,
    pending_subtasks: int,
    artifacts_produced: int,
    round: int = 0,
    *,
    db: Database | None = None,
) -> tuple[dict[str, object], bool]:
    normalized_swarm_id = _normalize_swarm_id(swarm_id)
    counters = {
        "wave": wave,
        "completed_subtasks": completed_subtasks,
        "pending_subtasks": pending_subtasks,
        "artifacts_produced": artifacts_produced,
        "round": round,
    }
    normalized_counters: dict[str, int] = {}
    for key, raw_value in counters.items():
        if isinstance(raw_value, bool):
            raise ValueError(f"{key} must be an integer")
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if value < 0:
            raise ValueError(f"{key} must be >= 0")
        normalized_counters[key] = value
    payload = build_wave_progress_payload(
        normalized_swarm_id,
        normalized_counters.get("wave", 0),
        normalized_counters.get("completed_subtasks", 0),
        normalized_counters.get("pending_subtasks", 0),
        normalized_counters.get("artifacts_produced", 0),
        round=normalized_counters.get("round", 0),
    )
    notification_sent = False
    try:
        send_notification("notifications/progress", payload)
        notification_sent = True
    except Exception:
        log.warning(
            "execute_swarm progress notification failed",
            extra={"swarm_id": normalized_swarm_id, "wave": normalized_counters["wave"]},
            exc_info=True,
        )
    database = db
    if database is None:
        _, database, *_ = _ensure_init()
    memory_refresh_swarm_state_from_db(normalized_swarm_id, db=database)
    return payload, notification_sent


def emit_wave_progress(
    swarm_id: str,
    wave: int,
    completed_subtasks: int,
    pending_subtasks: int,
    artifacts_produced: int,
    round: int = 0,
) -> dict[str, object]:
    """Emit one stable wave-progress notification and refresh swarm_state."""
    payload, _ = _emit_wave_progress(
        swarm_id,
        wave,
        completed_subtasks,
        pending_subtasks,
        artifacts_produced,
        round=round,
    )
    return payload


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "plan_task",
        "description": (
            "Ask the planner (sonnet 4.6 via gh copilot) to analyse a coding task, "
            "decompose it into subtasks, and assign each subtask a model tier.\n\n"
            "Returns an execution plan with:\n"
            "- analysis: the planner's reasoning\n"
            "- subtasks: list with id, description, tier, model, depends_on\n"
            "- waves: groups of subtask IDs that run in parallel\n"
            "- strategy: parallel | sequential | dag\n\n"
            "Spawn one agent per subtask. Run waves in order — "
            "all subtasks within a wave run in parallel."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Full description of the coding task"},
                "cwd": {
                    "type": "string",
                    "description": "Caller working directory for routing guard scoping",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "decompose_task",
        "description": (
            "Alias for plan_task. Preferred entry point for multi-file or multi-concern tasks.\n\n"
            "Calls the LLM planner (sonnet 4.6 via gh copilot) to decompose a task into "
            "independent subtasks with model tier assignments and dependency waves.\n\n"
            "Returns:\n"
            "- analysis: planner reasoning\n"
            "- subtasks: list with id, description, tier, model, depends_on\n"
            "- waves: parallel execution groups\n"
            "- strategy: parallel | sequential | dag\n\n"
            "Use this instead of route_task whenever the task spans more than one file, "
            "module, or concern. Spawn one agent per subtask; run waves in order."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Full description of the coding task"},
                "cwd": {
                    "type": "string",
                    "description": "Caller working directory for routing guard scoping",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "fleet_plan",
        "description": (
            "Plan a task AND format it for /fleet execution.\n\n"
            "Calls the LLM planner to decompose the task, then produces ready-to-run "
            "/fleet command strings — one per wave, respecting dependency order.\n\n"
            "Returns:\n"
            "- plan: full plan (same as plan_task)\n"
            "- fleet_waves: list of wave objects, each with:\n"
            "    wave_number: int\n"
            "    command: '/fleet \"[tier] subtask1\" \"[tier] subtask2\"'\n"
            "    agents: list of {tier, model, prompt}\n"
            "- execution_note: how to run the waves\n"
            "- cache_hit: bool\n\n"
            "Use this when you want copilot-router's model intelligence combined with "
            "/fleet's true parallel execution. Run wave 1 command, wait, run wave 2, etc."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Full description of the coding task"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "route_task",
        "description": (
            "Quick heuristic classification of a task — no LLM call.\n"
            "Returns model, score, reason, agents. Use for simple tasks "
            "or when speed matters more than accuracy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task to classify"},
                "cwd": {
                    "type": "string",
                    "description": "Caller working directory for routing guard scoping",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "validate_routing_guard",
        "description": (
            "Validate whether a direct Edit or Write tool call is allowed for the "
            "current routed task context. Intended for Claude Code PreToolUse hooks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_file": {
                    "type": "string",
                    "description": "Absolute path of the file about to be edited or written",
                },
                "tool_name": {
                    "type": "string",
                    "description": "Host tool name, for example Edit or Write",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory reported by the host hook",
                },
                "skill": {
                    "type": "string",
                    "description": "Optional skill name (e.g. 'auto-time') — matched against routing exceptions",
                },
            },
        },
    },
    {
        "name": "cache_get",
        "description": "Look up a cached result for a task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task to look up"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "cache_put",
        "description": "Store a completed task result in the cache.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task key"},
                "result": {"type": "string", "description": "The result to cache"},
                "model": {"type": "string", "description": "Model that produced it"},
            },
            "required": ["task", "result", "model"],
        },
    },
    {
        "name": "cache_stats",
        "description": "Return cache statistics: total entries and breakdown by model.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "execute_subtask",
        "description": (
            "Execute a prompt via the cheapest available AI CLI provider.\n\n"
            "Routes to the cheapest model for the given tier across all installed "
            "CLI tools (GitHub Copilot, Claude Code, Gemini CLI). Falls back to "
            "next cheapest on failure.\n\n"
            "When target_file is provided, writes the result directly to that path "
            "and returns file metadata. This is the preferred way to create files "
            "for low-tier subtasks — saves tokens by avoiding round-trip through "
            "the main agent.\n\n"
            "Surgical edit modes (set mode=): rewrite (full-file injection + length-ratio "
            "guard), blocks (Aider-style SEARCH/REPLACE, token-efficient), patch (unified diff).\n\n"
            "Returns:\n"
            "- result: the model's response text\n"
            "- provider: which CLI tool was used\n"
            "- model: which model handled it\n"
            "- tier: the tier that was requested\n"
            "- fallback_used: whether a fallback provider was needed\n"
            "- file_written: path written to (when target_file is set)\n"
            "- lines_written: line count of written file\n"
            "- diff: unified diff showing changes (when target_file is set)\n"
            "- change_type: 'created', 'modified', or 'unchanged'\n"
            "- lines_added: number of lines added\n"
            "- lines_removed: number of lines removed"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The prompt to send to the model"},
                "tier": {
                    "type": "string",
                    "description": "Complexity tier: low, medium, or high (default: low)",
                    "enum": ["low", "medium", "high"],
                },
                "prefer_free": {
                    "type": "boolean",
                    "description": "Prefer free-tier providers (default: true)",
                },
                "provider_id": {
                    "type": "string",
                    "description": (
                        "Optional exact provider identifier, such as 'codex'. "
                        "When set, execution is restricted to that provider."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: per-tier from config, max: 600)",
                },
                "effort": {
                    "type": "string",
                    "description": (
                        "Optional reasoning effort hint. When supported by the "
                        "selected provider, this is passed through to execution."
                    ),
                },
                "target_file": {
                    "type": "string",
                    "description": (
                        "Absolute path to write the result to. When set, the model's "
                        "output is written directly to this file. Parent directories "
                        "are created automatically. Ideal for low-tier file generation."
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Optional caller-supplied task identifier used for "
                        "inspection and telemetry correlation."
                    ),
                },
                "wave_id": {
                    "type": "string",
                    "description": (
                        "Optional wave identifier. Subtasks sharing the same wave_id "
                        "are shown as a parallel group in list_subtasks. "
                        "Use the same value for all execute_subtask calls dispatched "
                        "simultaneously (e.g. 'wave-1', 'wave-2')."
                    ),
                },
                "allow_out_of_workspace": {
                    "type": "boolean",
                    "description": (
                        "Allow writing target_file to a path outside the workspace root. "
                        "Every grant is logged. Explicit per-call opt-in."
                    ),
                },
                "mode": {
                    "type": "string",
                    "description": (
                        "Write mode for target_file edits:\n"
                        "  'write'   (default) — model output written verbatim. Safe for new files.\n"
                        "  'rewrite' — injects current file, asks for complete rewrite with\n"
                        "              length-ratio guard (rejects if output < 50% of original).\n"
                        "              Max file size: 32 KiB.\n"
                        "  'blocks'  — Aider-style SEARCH/REPLACE blocks. Token-efficient surgical\n"
                        "              edits. Max file size: 128 KiB.\n"
                        "  'patch'   — provider returns unified diff applied with patch semantics."
                    ),
                    "enum": ["write", "rewrite", "blocks", "patch"],
                },
                "convergence_target": {
                    "type": "object",
                    "description": (
                        "Optional quality convergence policy (plan 14). Re-executes until "
                        "gate score meets min_score or max_rounds is exhausted. "
                        "Each round appends prior output to the prompt."
                    ),
                    "properties": {
                        "min_score": {
                            "type": "number",
                            "description": "Gate score threshold 0.0–1.0. Default 0.8.",
                        },
                        "max_rounds": {
                            "type": "integer",
                            "description": "Maximum retry rounds. Default 3.",
                        },
                        "backoff_seconds": {
                            "type": "number",
                            "description": "Sleep between rounds in seconds. Default 0.",
                        },
                    },
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "execute_swarm",
        "description": (
            "Start a swarm run; returns an immediate run contract "
            "(swarm_id, wave summary, cost estimate) or a budget preview."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "description": "Task to execute as a string or structured object",
                    "anyOf": [
                        {"type": "string"},
                        {"type": "object"},
                    ],
                },
                "topology": {
                    "type": "string",
                    "enum": ["star", "hierarchical", "dag", "auto"],
                },
                "max_agents": {
                    "type": "integer",
                    "minimum": 1,
                },
                "workspace_root": {
                    "type": "string",
                    "description": (
                        "Workspace where declared subtask outputs are materialized. "
                        "Defaults to the active MCP workspace."
                    ),
                },
                "urgency_hint": {
                    "type": "string",
                },
                "budget_limit": {
                    "type": "number",
                },
                "preview_token": {
                    "type": "string",
                },
                "unlimited_budget": {
                    "type": "boolean",
                    "description": "Disable token budget circuit breaker and cost limit checks — swarm runs to completion regardless of token usage.",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "apply_preview",
        "description": (
            "Approve or deny a pending outside-workspace file write preview "
            "created by execute_subtask."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "preview_token": {
                    "type": "string",
                    "description": "Preview token returned by execute_subtask",
                },
                "approve": {
                    "type": "boolean",
                    "description": "True to apply the write, false to deny it",
                },
            },
            "required": ["preview_token", "approve"],
        },
    },
    {
        "name": "inspect_task",
        "description": (
            "Return structured provider/model/tier telemetry and fallback/speculation "
            "flags for a previously routed task."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task identifier returned by execute_subtask or run",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "resume_swarm_inspect",
        "description": (
            "List compact coordinator checkpoints available for resuming a failed swarm."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "failed_swarm_id": {
                    "type": "string",
                    "description": "Swarm ID whose checkpoints should be inspected",
                },
                "plan_revision": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional plan revision filter for checkpoint listing",
                },
            },
            "required": ["failed_swarm_id"],
        },
    },
    {
        "name": "resume_swarm_confirm",
        "description": (
            "Resume a failed swarm from a chosen coordinator checkpoint using a new swarm_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "failed_swarm_id": {
                    "type": "string",
                    "description": "Original failed swarm_id to resume from",
                },
                "checkpoint_index": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Checkpoint index selected from resume_swarm_inspect",
                },
                "plan_revision": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional plan revision paired with checkpoint_index",
                },
            },
            "required": ["failed_swarm_id", "checkpoint_index"],
        },
    },
    {
        "name": "inspect_write_audit",
        "description": "Return recent out-of-workspace write audit log entries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max entries to return (default 50, max 500)"},
            },
        },
    },
    {
        "name": "inspect_status",
        "description": (
            "Return a compact readiness/status snapshot for one project, "
            "including enabled features, current limits, and pending approvals."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Optional project identifier or workspace path",
                },
            },
        },
    },
    {
        "name": "agent_queue_list",
        "description": "List pending approval-queue items for one project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "approval_queue_list",
        "description": "Compatibility alias for agent_queue_list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "agent_queue_approve",
        "description": "Approve one pending approval-queue item for one project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "queue_id": {"type": "integer"},
                "operator_id": {"type": "string"},
            },
            "required": ["project_id", "queue_id", "operator_id"],
        },
    },
    {
        "name": "approval_queue_approve",
        "description": "Compatibility alias for agent_queue_approve.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "queue_id": {"type": "integer"},
                "operator_id": {"type": "string"},
            },
            "required": ["project_id", "queue_id", "operator_id"],
        },
    },
    {
        "name": "agent_queue_reject",
        "description": "Reject one pending approval-queue item for one project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "queue_id": {"type": "integer"},
                "operator_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["project_id", "queue_id", "operator_id"],
        },
    },
    {
        "name": "approval_queue_reject",
        "description": "Compatibility alias for agent_queue_reject.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "queue_id": {"type": "integer"},
                "operator_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["project_id", "queue_id", "operator_id"],
        },
    },
    {
        "name": "agent_queue_merge",
        "description": "Merge one pending approval-queue item into a canonical agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "queue_id": {"type": "integer"},
                "canonical_agent_id": {"type": "string"},
                "operator_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["project_id", "queue_id", "canonical_agent_id", "operator_id"],
        },
    },
    {
        "name": "approval_queue_merge",
        "description": "Compatibility alias for agent_queue_merge.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "queue_id": {"type": "integer"},
                "canonical_agent_id": {"type": "string"},
                "operator_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["project_id", "queue_id", "canonical_agent_id", "operator_id"],
        },
    },
    {
        "name": "memory_list",
        "description": "List keys and lightweight metadata for one explicit memory scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string"},
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["scope"],
        },
    },
    {
        "name": "memory_get",
        "description": "Fetch one full memory envelope from an explicit scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string"},
                "key": {"type": "string"},
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["scope", "key"],
        },
    },
    {
        "name": "memory_set",
        "description": "Store or overwrite one memory value in an explicit scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string"},
                "key": {"type": "string"},
                "value": {"description": "JSON-serializable value to store"},
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["scope", "key", "value"],
        },
    },
    {
        "name": "memory_delete",
        "description": "Hard-delete one memory value from an explicit scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string"},
                "key": {"type": "string"},
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["scope", "key"],
        },
    },
    {
        "name": "record_outcome",
        "description": "Record an explicit routed-task outcome and persist the latest task snapshot. operator_id must match the authenticated caller when provided; omitted values are stored as anonymous.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "outcome": {"type": "string"},
                "operator_id": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["task_id", "outcome"],
        },
    },
    {
        "name": "tune_show",
        "description": "Show persisted operator-facing tuning controls for one project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "key": {"type": "string"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "routing_exception_add",
        "description": (
            "Add a routing bypass rule so that matching tasks skip validate_routing_guard.\n\n"
            "exception_type must be one of: skill, filetype, project, command, caller, path.\n"
            "pattern supports glob wildcards (e.g. 'tgsd-*', '.md', '/home/user/notes').\n\n"
            "Examples:\n"
            "  routing_exception_add(exception_type='skill',    pattern='auto-time')\n"
            "  routing_exception_add(exception_type='skill',    pattern='tgsd-*')\n"
            "  routing_exception_add(exception_type='filetype', pattern='.md')\n"
            "  routing_exception_add(exception_type='project',  pattern='/home/me/notes')\n"
            "  routing_exception_add(exception_type='command',  pattern='Write')\n"
            "  routing_exception_add(exception_type='caller',   pattern='github-copilot')\n"
            "  routing_exception_add(exception_type='path',     pattern='/tmp/')"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "exception_type": {
                    "type": "string",
                    "description": "One of: skill, filetype, project, command, caller, path",
                },
                "pattern": {
                    "type": "string",
                    "description": "Pattern to match (supports * globs, case-insensitive)",
                },
                "note": {
                    "type": "string",
                    "description": "Optional human-readable note / reason for this exception",
                },
            },
            "required": ["exception_type", "pattern"],
        },
    },
    {
        "name": "routing_exception_remove",
        "description": "Remove a routing bypass rule by type and pattern.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "exception_type": {
                    "type": "string",
                    "description": "One of: skill, filetype, project, command, caller, path",
                },
                "pattern": {
                    "type": "string",
                    "description": "Exact pattern string to remove",
                },
            },
            "required": ["exception_type", "pattern"],
        },
    },
    {
        "name": "routing_exception_list",
        "description": "List all active routing bypass rules (from the DB; static config.yaml entries are separate).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_providers",
        "description": (
            "List detected AI CLI providers with routeability, detection reason, model summary, and health status. "
            "Output is compact and secret-safe."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_subtasks",
        "description": (
            "Return structured status of currently running and recently completed "
            "execute_subtask calls.\n\n"
            "active: tasks currently executing (show elapsed time, model, prompt excerpt, target file).\n"
            "recent: last 10 completed or failed tasks this session.\n\n"
            "Use this to monitor parallel execute_subtask calls — similar to /tasks for background agents."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "stop_subtask",
        "description": (
            "Send SIGSTOP to a running subtask, pausing its execution. "
            "Use list_subtasks to find the task_id. "
            "Resume with resume_subtask. macOS/Linux only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "task_id from list_subtasks"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "resume_subtask",
        "description": (
            "Send SIGCONT to a stopped subtask, resuming its execution. "
            "Use after stop_subtask. macOS/Linux only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "task_id from list_subtasks"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "learning_agent_summary",
        "description": (
            "Get summary of all learned agents by status (active, pending, rejected).\n\n"
            "Returns compact agent list with description, lane, pattern hash, and status. "
            "Sensitive data (tokens, secrets) is filtered out."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "learning_pattern_health",
        "description": (
            "Get health metrics for the pattern tracking system.\n\n"
            "Reveals: total patterns tracked, mature patterns ready for drafting, "
            "patterns awaiting proof, draft proposals in approval queue, and active agent count. "
            "Use to monitor learning loop maturity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Optional project filter"}
            },
        },
    },
    {
        "name": "learning_audit_log",
        "description": (
            "Get audit trail for agent creation, approval, and registration events.\n\n"
            "Returns event stream with timestamps and operator identity. "
            "Sensitive fields (tokens, API keys) are filtered out."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Optional filter by agent"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Max events (default 50)"},
            },
        },
    },
    {
        "name": "learning_outcome_stats",
        "description": (
            "Get outcome distribution snapshot over 1-hour recent window grouped by tier and model.\n\n"
            "Returns: outcome counts (accepted, revised, rejected, reworked) per tier:model combination, "
            "coverage percentage, and window timestamps. Aggregates are computed in background and retrieved from memory. "
            "Use for operator observability into routing quality by model."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "remote_dispatch",
        "description": (
            "Dispatch a task to a remote Threnody server.\n\n"
            "Sends the task to a remote HTTP(S) server running \'threnody serve\'. "
            "The remote server runs the full plan+execute pipeline using its own AI CLI providers.\n\n"
            "Falls back to config.yaml remote_client settings when remote_url/remote_token are omitted.\n\n"
            "Returns:\n"
            "- If async_mode=false: {status: completed, result: ...}\n"
            "- If async_mode=true:  {job_id: ..., status: pending} — poll with remote_job_status"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task to execute on the remote server"},
                "topology": {"type": "string", "description": "Optional execution topology hint (linear/dag/hierarchical/star)"},
                "async_mode": {
                    "type": "boolean",
                    "description": "If true, submit async and return a job_id for polling (default: false)",
                },
                "remote_url": {
                    "type": "string",
                    "description": "Remote server URL, e.g. https://myhost:8765 or http://192.168.1.5:8765. Overrides config.yaml.",
                },
                "remote_token": {
                    "type": "string",
                    "description": "Bearer token for the remote server. Overrides config.yaml.",
                },
                "verify_tls": {
                    "type": "boolean",
                    "description": "If false, skip TLS certificate verification (for self-signed certs or IP-based URLs). Default: true.",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "remote_job_status",
        "description": (
            "Poll the status of an async remote_dispatch job.\n\n"
            "Returns {status, result, error, created_ts, updated_ts}.\n"
            "status is one of: pending, running, completed, failed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID returned by remote_dispatch with async_mode=true"},
                "remote_url": {
                    "type": "string",
                    "description": "Remote server URL. Overrides config.yaml.",
                },
                "remote_token": {
                    "type": "string",
                    "description": "Bearer token for the remote server. Overrides config.yaml.",
                },
                "verify_tls": {
                    "type": "boolean",
                    "description": "If false, skip TLS certificate verification. Default: true.",
                },
            },
            "required": ["job_id"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def handle_plan_task(args: dict) -> dict:
    global _config, _db, _router, _planner, _orchestrator
    # Respect injected planner/config/db during tests; this handler does not use router/orchestrator.
    if _planner is None or _db is None or _config is None:
        config, db, router, planner, orchestrator = _ensure_init()
    else:
        config, db, router, planner, orchestrator = _config, _db, _router, _planner, _orchestrator
    caller = _resolve_caller()
    task = args.get("task", "")
    cached = db.cache_get(task)
    if cached is not None:
        result_str, model = cached
        try:
            result = _attach_models_to_subtasks(json.loads(result_str))
            result["cache_hit"] = True
            result["tip"] = "Cache hit — skipped planner call, zero tokens used."
            guard = _issue_routing_guard(
                db,
                caller=caller,
                cwd=args.get("cwd"),
                task=task,
                source_tool="plan_task",
                mode=ROUTING_GUARD_MODE_ROUTED_PLAN if _task_likely_writes_files(task) else None,
            )
            if guard is not None:
                result["routing_guard"] = guard
            return result
        except (json.JSONDecodeError, TypeError):
            pass

    try:
        plan = planner.plan(task)
    except PlannerParseError as exc:
        return {
            "error": "PlannerParseError",
            "details": str(exc),
            "parse_diagnostics_id": exc.parse_diagnostics_id,
            "task": task,
        }
    result = _attach_models_to_subtasks(planner.plan_to_dict(plan))

    _sfi_subtasks = [st for st in result.get("subtasks", []) if isinstance(st, dict) and st.get("single_file_insertion")]
    _sfi_hint = (
        f"\n\nSubtasks with single_file_insertion=true ({len(_sfi_subtasks)} found): "
        "use execute_subtask(tier='low', effort='high') instead of spawning a Task agent — "
        "free providers can handle single-file insertions with effort boost."
    ) if _sfi_subtasks else ""
    result["tip"] = (
        f"Spawn {plan.total_agents} agent(s) across {len(plan.waves)} wave(s). "
        "Each subtask has its own tier and model. "
        "Wave 1 agents run in parallel; wave 2 waits for wave 1; etc.\n\n"
        "TRANSPARENCY: Before each wave, print a table showing Agent #, Tier, Model, "
        "and Target files. After each agent, print: model used, files touched, tokens, "
        "and wall time. After all waves, print a cost summary by tier."
        + _sfi_hint
    )
    guard = _issue_routing_guard(
        db,
        caller=caller,
        cwd=args.get("cwd"),
        task=task,
        source_tool="plan_task",
        mode=ROUTING_GUARD_MODE_ROUTED_PLAN if _task_likely_writes_files(task) else None,
    )
    if guard is not None:
        result["routing_guard"] = guard
    cacheable_result = dict(result)
    cacheable_result.pop("routing_guard", None)
    db.cache_put(task, json.dumps(cacheable_result), "planner")
    return result


def handle_fleet_plan(args: dict) -> dict:
    config, db, router, planner, orchestrator = _ensure_init()
    caller = _resolve_caller()
    task = args.get("task", "")
    cached = db.cache_get(task)
    if cached is not None:
        result_str, model = cached
        try:
            cached_result = json.loads(result_str)
            if "fleet_waves" in cached_result:
                plan_payload = cached_result.get("plan")
                if isinstance(plan_payload, dict):
                    cached_result["plan"] = _attach_models_to_subtasks(plan_payload)
                    cached_result["fleet_waves"] = _attach_plan_routing_to_fleet_waves(
                        cached_result.get("plan") or {},
                    )
                cached_result["cache_hit"] = True
                guard = _issue_routing_guard(
                    db,
                    caller=caller,
                    cwd=args.get("cwd"),
                    task=task,
                    source_tool="fleet_plan",
                    mode=ROUTING_GUARD_MODE_ROUTED_PLAN if _task_likely_writes_files(task) else None,
                )
                if guard is not None:
                    cached_result["routing_guard"] = guard
                return cached_result
            if "subtasks" in cached_result:
                cached_result = _attach_models_to_subtasks(cached_result)
                fleet_waves = _attach_plan_routing_to_fleet_waves(
                    cached_result,
                )
                result = {
                    "plan": cached_result,
                    "fleet_waves": fleet_waves,
                    "execution_note": _fleet_note(len(fleet_waves)),
                    "cache_hit": True,
                }
                guard = _issue_routing_guard(
                    db,
                    caller=caller,
                    cwd=args.get("cwd"),
                    task=task,
                    source_tool="fleet_plan",
                    mode=ROUTING_GUARD_MODE_ROUTED_PLAN if _task_likely_writes_files(task) else None,
                )
                if guard is not None:
                    result["routing_guard"] = guard
                return result
        except (json.JSONDecodeError, TypeError):
            pass

    try:
        plan = planner.plan(task)
    except PlannerParseError as exc:
        return {
            "error": "PlannerParseError",
            "details": str(exc),
            "parse_diagnostics_id": exc.parse_diagnostics_id,
            "task": task,
        }
    plan_dict = _attach_models_to_subtasks(planner.plan_to_dict(plan))

    fleet_waves = _attach_plan_routing_to_fleet_waves(
        plan_dict,
    )
    result = {
        "plan": plan_dict,
        "fleet_waves": fleet_waves,
        "execution_note": _fleet_note(len(fleet_waves)),
        "cache_hit": False,
    }
    guard = _issue_routing_guard(
        db,
        caller=caller,
        cwd=args.get("cwd"),
        task=task,
        source_tool="fleet_plan",
        mode=ROUTING_GUARD_MODE_ROUTED_PLAN if _task_likely_writes_files(task) else None,
    )
    if guard is not None:
        result["routing_guard"] = guard
    cacheable_result = dict(result)
    cacheable_result.pop("routing_guard", None)
    db.cache_put(task, json.dumps(cacheable_result), "planner")
    return result


def _fleet_note(wave_count: int) -> str:
    if wave_count == 1:
        return "Single wave — run all agents in parallel with one /fleet call."
    return f"{wave_count} waves — run wave 1, wait for completion, then run wave 2, etc."


def _normalize_effort_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_route_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _is_placeholder_route_model(value: object) -> bool:
    normalized = _normalize_route_text(value)
    return normalized is not None and normalized.lower() in {"low", "medium", "high"}


def _safe_provider_route_text(provider: object, attr_name: str) -> str | None:
    try:
        value = getattr(provider, attr_name, None)
    except Exception:
        return None
    return _normalize_route_text(value)


def _format_fleet_prompt(subtask: Mapping[str, object]) -> str:
    tier = _sanitize_terminal_text(str(subtask.get("tier") or ""))
    model = _sanitize_terminal_text(str(subtask.get("model") or ""))
    description = _sanitize_terminal_text(str(subtask.get("description") or ""))
    return f"[{tier}|{model}] {description}"


def _quote_fleet_command_prompt(prompt: str) -> str:
    escaped = (
        _sanitize_terminal_text(prompt)
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace('"', "'")
    )
    return f'"{escaped}"'


def _resolved_effort_metadata(
    provider_id: object,
    tier: str,
    *,
    effort: str | None = None,
    config: TGsConfig | None = None,
) -> dict[str, str]:
    explicit_effort = _normalize_effort_value(effort)
    if explicit_effort is not None:
        return {
            "effort": explicit_effort,
            "effort_source": "explicit",
        }

    cfg = config or _config
    getter = getattr(cfg, "get_default_effort", None)
    if callable(getter) and isinstance(provider_id, str):
        default_effort = _normalize_effort_value(getter(provider_id, tier))
        if default_effort is not None:
            return {
                "effort": default_effort,
                "effort_source": "config_default",
            }

    return {}


def _selection_with_effort_metadata(
    selection: Mapping[str, object] | None,
    *,
    tier: str,
    effort: str | None = None,
    config: TGsConfig | None = None,
    caller_allowlists: dict[str, list[str]] | None = None,
) -> dict[str, object] | None:
    if not isinstance(selection, Mapping):
        return None
    normalized = dict(selection)
    for key in ("model", "provider", "provider_id"):
        value = _normalize_route_text(normalized.get(key))
        if value is None:
            normalized.pop(key, None)
        else:
            normalized[key] = value
    if "effort" not in normalized:
        normalized.update(
            _resolved_effort_metadata(
                normalized.get("provider_id"),
                tier,
                effort=effort,
                config=config,
            )
        )
    return normalized


def _provider_supports_explicit_effort(provider_id: object) -> bool:
    return (
        isinstance(provider_id, str)
        and provider_id.strip().lower() in _EXPLICIT_EFFORT_SUPPORTED_PROVIDERS
    )


def _unsupported_effort_override_result(
    *,
    task_id: str,
    tier: str,
    provider: object,
    provider_id: object,
    effort: object,
    effort_source: object,
    caller: str | None,
    provenance: Mapping[str, object],
    details: str,
) -> dict[str, object]:
    return {
        "error": "UnsupportedEffortOverride",
        "details": details,
        "task_id": task_id,
        "tier": tier,
        "provider": provider,
        "provider_id": provider_id,
        "effort": effort,
        "effort_source": effort_source,
        "caller_detected": caller,
        "provenance": provenance,
    }


def _routing_unavailable_result(
    *,
    task_id: str,
    tier: str,
    caller: str | None,
    provenance: Mapping[str, object],
    details: str,
    selection: Mapping[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "error": "RoutingUnavailable",
        "details": details,
        "task_id": task_id,
        "tier": tier,
        "caller_detected": caller,
        "provenance": provenance,
    }
    if isinstance(selection, Mapping):
        for key in ("model", "provider", "provider_id"):
            value = _normalize_route_text(selection.get(key))
            if value is not None:
                result[key] = value
    return result


def _safe_registry_diagnostics(registry: object) -> dict[str, object]:
    compact = getattr(registry, "to_compact_dict", None)
    if callable(compact):
        try:
            value = compact()
        except Exception:
            return {"providers": [], "total": 0}
        if isinstance(value, Mapping):
            return dict(value)
    return {"providers": [], "total": 0}


def _has_complete_routing_metadata(selection: Mapping[str, object] | None) -> bool:
    if not isinstance(selection, Mapping):
        return False
    model = _normalize_route_text(selection.get("model"))
    if model is None or _is_placeholder_route_model(model):
        return False
    return all(
        _normalize_route_text(selection.get(key)) is not None
        for key in ("provider", "provider_id")
    )


def _has_executable_routing_metadata(selection: Mapping[str, object] | None) -> bool:
    if not isinstance(selection, Mapping):
        return False
    return (
        (
            _normalize_route_text(selection.get("provider")) is not None
            or _normalize_route_text(selection.get("provider_id")) is not None
        )
        and not _is_placeholder_route_model(selection.get("model"))
        and _normalize_route_text(selection.get("model")) is not None
    )


def _select_provider_metadata(
    registry: Any,
    tier: str,
    *,
    caller: str | None,
    code_only: bool = False,
    prefer_free: bool = True,
    effort: str | None = None,
    config: TGsConfig | None = None,
    caller_allowlists: dict[str, list[str]] | None = None,
    provider_id: str | None = None,
) -> dict[str, object] | None:
    """Return the cheapest-safe provider metadata for a tier.

    Uses the registry's native selection helper when available so route/plan
    surfaces match execute_subtask behaviour. Falls back to the older
    cheapest-provider-by-cost ordering for compatibility with lightweight stubs.
    """
    provider_obj = CopilotProvider()
    partial_selection: dict[str, object] | None = None

    if hasattr(registry, "select_provider_for_tier"):
        select_provider = registry.select_provider_for_tier
        signature_cache_key = getattr(select_provider, "__func__", select_provider)
        supports_effort = _SELECT_PROVIDER_SUPPORTS_EFFORT_CACHE.get(
            signature_cache_key,
        )
        if supports_effort is None:
            supports_effort = True
            try:
                parameters = inspect.signature(select_provider).parameters.values()
                supports_effort = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    or parameter.name == "effort"
                    for parameter in parameters
                )
            except (TypeError, ValueError):
                supports_effort = True
            _SELECT_PROVIDER_SUPPORTS_EFFORT_CACHE[signature_cache_key] = supports_effort
        try:
            selector_kwargs: dict[str, object] = {
                "prefer_free": prefer_free,
                "caller": caller,
                "code_only": code_only,
            }
            if provider_id is not None:
                selector_kwargs["provider_id"] = provider_id
            if supports_effort:
                selector_kwargs["effort"] = effort
            if caller_allowlists:
                selector_kwargs["caller_allowlists"] = caller_allowlists
            selection = select_provider(
                tier,
                **selector_kwargs,
            )
        except TypeError as exc:
            if not supports_effort:
                raise
            try:
                selection = select_provider(
                    tier,
                    prefer_free=prefer_free,
                    caller=caller,
                    code_only=code_only,
                )
            except Exception:
                selection = None
        except Exception:
            selection = None
        if isinstance(selection, Mapping):
            normalized_selection = _selection_with_effort_metadata(
                selection,
                tier=tier,
                effort=effort,
                config=config,
            )
            if _has_complete_routing_metadata(normalized_selection):
                return normalized_selection
            partial_selection = normalized_selection

    if hasattr(registry, "get_providers_for_tier"):
        try:
            all_candidates = registry.get_providers_for_tier(tier, caller=caller)
        except TypeError:
            try:
                all_candidates = registry.get_providers_for_tier(tier)
            except Exception:
                all_candidates = []
        except Exception:
            all_candidates = []
        normalized_caller = _normalize_route_text(caller)
        candidates = [
            provider
            for provider in all_candidates
            if (
                _safe_provider_route_text(provider, "name") != normalized_caller
                and _safe_provider_route_text(provider, "display_name") != normalized_caller
            )
        ] if normalized_caller else all_candidates
        if not candidates:
            candidates = all_candidates
        if candidates:
            selected_provider_id = _normalize_route_text(
                partial_selection.get("provider_id") if partial_selection else None
            )
            selected_provider = _normalize_route_text(
                partial_selection.get("provider") if partial_selection else None
            )
            preferred_candidate = next(
                (
                    candidate
                    for candidate in candidates
                    if (
                        selected_provider_id is not None
                        and _safe_provider_route_text(candidate, "name") == selected_provider_id
                    )
                    or (
                        selected_provider is not None
                        and _safe_provider_route_text(candidate, "display_name") == selected_provider
                    )
                ),
                None,
            )
            cheapest = preferred_candidate or candidates[0]
            try:
                cost_rank_map = getattr(cheapest, "cost_rank", {})
            except Exception:
                cost_rank_map = {}
            if not isinstance(cost_rank_map, Mapping):
                cost_rank_map = {}
            cost_rank = cost_rank_map.get(tier)
            is_free = cost_rank == 0
            try:
                billing_model = getattr(cheapest, "billing_model", "subscription")
            except Exception:
                billing_model = "subscription"
            billing_tier = "free" if is_free else billing_model
            if billing_tier == "free":
                provider_cost_hint = "free"
            elif billing_tier == "metered":
                provider_cost_hint = "metered / per-token"
            else:
                provider_cost_hint = "included in subscription/quota"
            provider_name = (
                _safe_provider_route_text(cheapest, "display_name")
                or _safe_provider_route_text(cheapest, "name")
            )
            provider_id = _safe_provider_route_text(cheapest, "name")
            try:
                tier_models = getattr(cheapest, "tier_models", {})
            except Exception:
                tier_models = {}
            if not isinstance(tier_models, Mapping):
                tier_models = {}
            selection = {
                "provider": provider_name,
                "provider_id": provider_id,
                "model": tier_models.get(tier) or provider_obj.resolve_model(tier),
                "tier": tier,
                "is_free": is_free,
                "billing_tier": billing_tier,
                "provider_cost_hint": provider_cost_hint,
                "cost_rank": cost_rank,
                "billing_source": getattr(cheapest, "billing_source_for", lambda _tier: "provider_default")(tier),
            }
            selection = _selection_with_effort_metadata(
                selection,
                tier=tier,
                effort=effort,
                config=config,
            )
            return selection

    return partial_selection


_ROUTE_WRITE_HINTS = (
    "add",
    "change",
    "create",
    "delete",
    "fix",
    "generate",
    "implement",
    "move",
    "refactor",
    "remove",
    "rename",
    "update",
    "write",
)
_ROUTE_FILE_HINT_RE = re.compile(
    r"(?<![\w-])(?:"
    r"[\w./-]+\.(?:c|cc|cpp|cs|go|h|hpp|html|java|js|json|jsx|md|php|py|rb|rs|sh|sql|toml|ts|tsx|txt|xml|yaml|yml)"
    r"|(?:[\w.-]+/)*(?:dockerfile|makefile|procfile|jenkinsfile)"
    r"|(?:[\w.-]+/)*\.[\w.-]+"
    r")(?![\w-])",
    re.IGNORECASE,
)


def _task_likely_writes_files(task: str) -> bool:
    lowered = task.lower()
    has_write_verb = any(
        f" {hint}" in lowered or lowered.startswith(f"{hint} ")
        for hint in _ROUTE_WRITE_HINTS
    )
    if has_write_verb:
        return True
    return any(
        marker in lowered
        for marker in (
            " docstring",
            " docstrings",
            " type hint",
            " type hints",
        )
    ) and bool(_ROUTE_FILE_HINT_RE.search(lowered))


def _subtask_likely_writes_files(subtask: dict[str, object]) -> bool:
    direct_targets = (
        subtask.get("target_file"),
        subtask.get("file"),
    )
    if any(isinstance(value, str) and value.strip() for value in direct_targets):
        return True

    text_parts = [
        str(subtask.get(key, ""))
        for key in ("title", "description", "prompt")
    ]
    files_value = subtask.get("files")
    if isinstance(files_value, list):
        text_parts.extend(str(item) for item in files_value if isinstance(item, str))
    return _task_likely_writes_files(" ".join(text_parts))


def _routing_guard_cwd(raw_cwd: object | None = None) -> str:
    value = _normalize_fs_text(raw_cwd) or ""
    base = Path(value).expanduser() if value else Path.cwd()
    return str(base.resolve(strict=False))


def _normalize_fs_text(raw_value: object | None) -> str | None:
    if not isinstance(raw_value, (str, os.PathLike)):
        return None
    value = os.fsdecode(os.fspath(raw_value)).strip()
    return value or None


def _normalized_cwd_or_none(raw_cwd: object | None = None) -> str | None:
    value = _normalize_fs_text(raw_cwd)
    return _routing_guard_cwd(value) if value is not None else None


def _normalize_path_input(raw_path: object | None) -> str | None:
    return _normalize_fs_text(raw_path)


_WINDOWS_DRIVE_ROOT_RE = re.compile(r"^[a-z]:/$")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[a-z]:/")


def _normalize_match_path_text(raw_path: str) -> str:
    normalized = os.path.expanduser(raw_path).lower().replace("\\", "/")
    if normalized == "/" or _WINDOWS_DRIVE_ROOT_RE.fullmatch(normalized):
        return normalized
    return posixpath.normpath(normalized)


def _is_absolute_match_path(pattern: str) -> bool:
    return pattern.startswith("/") or bool(_WINDOWS_ABSOLUTE_PATH_RE.match(pattern))


def _path_prefix_matches(resolved: str, prefix: str) -> bool:
    if resolved == prefix:
        return True
    if prefix == "/" or _WINDOWS_DRIVE_ROOT_RE.fullmatch(prefix):
        return resolved.startswith(prefix)
    return resolved.startswith(prefix + "/")


def _safe_resolve_path(raw_path: object | None) -> str | None:
    normalized = _normalize_path_input(raw_path)
    if normalized is None:
        return None
    try:
        return str(Path(normalized).expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return None


def _safe_coerce_int(value: object) -> int | None:
    """Safely coerce *value* to int, returning None on malformed input."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        log.debug("_safe_coerce_int: could not coerce %r to int", value)
        return None


def _extract_route_file_hints(task: str, cwd: str) -> list[str]:
    if not isinstance(task, str) or not task.strip():
        return []
    base = Path(cwd).expanduser().resolve()
    matches = sorted(set(_ROUTE_FILE_HINT_RE.findall(task)))
    hints: list[str] = []
    for match in matches:
        try:
            normalized = normalize_target_path(match, base)
        except ValueError:
            continue
        if is_within_repo(normalized, base):
            hints.append(str(normalized))
    return hints


def _route_guard_mode_for_task(task: str, tier: str) -> str | None:
    if not _task_likely_writes_files(task):
        return None
    if tier == "low":
        return ROUTING_GUARD_MODE_EXECUTE_SUBTASK
    return ROUTING_GUARD_MODE_DIRECT


def _issue_routing_guard(
    db: Database,
    *,
    caller: str | None,
    cwd: object | None,
    task: str,
    source_tool: str,
    mode: str | None,
    tier: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, object] | None:
    if mode is None:
        return None
    normalized_caller = _normalize_route_text(caller) or "mcp"
    normalized_cwd = _routing_guard_cwd(cwd)
    file_hints = _extract_route_file_hints(task, normalized_cwd)
    if file_hints:
        config = _ensure_init()[0]
        non_exempt_hints: list[str] = []
        db_rows: list[Mapping[str, object]] | None = None
        for hint in file_hints:
            hint_filetype = Path(hint).suffix.lower() or None
            is_exempt, _reason = _is_routing_exception_exempt(
                db,
                config,
                skill=None,
                filetype=hint_filetype,
                cwd=normalized_cwd,
                tool_name=None,
                caller=normalized_caller,
                target_file=hint,
                check_db=False,
            )
            if _is_routing_guard_exempt(hint, normalized_cwd) or is_exempt:
                continue
            if db_rows is None:
                try:
                    db_rows = db.routing_exception_list()
                except Exception:
                    db_rows = []
            is_exempt, _reason = _is_routing_exception_exempt(
                db,
                config,
                skill=None,
                filetype=hint_filetype,
                cwd=normalized_cwd,
                tool_name=None,
                caller=normalized_caller,
                target_file=hint,
                db_rows=db_rows,
            )
            if not is_exempt:
                non_exempt_hints.append(hint)
        if not non_exempt_hints:
            return None
        file_hints = non_exempt_hints
    try:
        return db.routing_guard_put(
            caller=normalized_caller,
            cwd=normalized_cwd,
            mode=mode,
            tier=_normalize_route_text(tier),
            provider=_normalize_route_text(provider),
            model=_normalize_route_text(model),
            source_tool=source_tool,
            task_text=task,
            file_hints=file_hints,
            ttl_seconds=ROUTING_GUARD_TTL_SECONDS,
        )
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        try:
            db.routing_guard_clear(caller=normalized_caller, cwd=normalized_cwd)
        except (ValueError, sqlite3.DatabaseError):
            log.debug("Failed to clear stale routing guard after issuance failure", exc_info=True)
        log.warning("Failed to issue routing guard via %s: %s", source_tool, exc)
        log.debug("Routing guard issuance failed", exc_info=True)
        return None


def _deny_routing_guard(reason: str, *, guard: Mapping[str, object] | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "valid": False,
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }
    if guard is not None:
        result["routing_guard"] = dict(guard)
    return result


# System-managed paths for AI CLI tools — exempt from routing enforcement.
_ROUTING_GUARD_EXEMPT_DIRS: tuple[str, ...] = (
    ".claude", ".continue", ".cursor",
    ".aider", ".codeium", ".github/copilot",
    ".gemini", ".codex", ".junie", ".opencode",
    ".config/opencode",
)


def _routing_guard_exempt_prefixes() -> tuple[Path, ...]:
    home = Path("~").expanduser().resolve(strict=False)
    return tuple((home / directory).resolve(strict=False) for directory in _ROUTING_GUARD_EXEMPT_DIRS)


def _resolve_guard_target(raw_path: object | None, cwd: str | None = None) -> str | None:
    normalized = _normalize_path_input(raw_path)
    if normalized is None:
        return None
    path = Path(normalized).expanduser()
    if not path.is_absolute():
        base = Path(_normalized_cwd_or_none(cwd) or _routing_guard_cwd())
        path = base / path
    return str(path.resolve(strict=False))


def _is_routing_guard_exempt(target_file: object | None, cwd: str | None = None) -> bool:
    resolved = _resolve_guard_target(target_file, cwd)
    if resolved is None:
        return False
    resolved_path = Path(resolved)
    return any(
        resolved_path == prefix or prefix in resolved_path.parents
        for prefix in _routing_guard_exempt_prefixes()
    )


def _is_routing_exception_exempt(
    db: Database,
    config: object,
    *,
    skill: str | None,
    filetype: str | None,
    cwd: str | None,
    tool_name: str | None,
    caller: str | None,
    target_file: object | None,
    db_rows: list[Mapping[str, object]] | None = None,
    check_db: bool = True,
) -> tuple[bool, str]:
    """Check user-defined routing exceptions from DB and config.yaml.

    Returns (is_exempt, reason_string).
    Uses fnmatch for glob-style pattern matching (case-insensitive).
    """
    import fnmatch as _fnmatch

    def _matches(value: str | None, patterns: list[str]) -> bool:
        if not value or not patterns:
            return False
        v = value.strip().lower()
        return any(_fnmatch.fnmatchcase(v, p.lower()) for p in patterns)

    def _path_matches(resolved: str, pattern: str) -> bool:
        raw_pat = pattern.strip()
        pat = os.path.expanduser(raw_pat)
        if not resolved or not pat:
            return False
        resolved_norm = _normalize_match_path_text(resolved)
        pat_norm = _normalize_match_path_text(pat)
        if _is_absolute_match_path(pat_norm):
            if _path_prefix_matches(resolved_norm, pat_norm):
                return True
            return _fnmatch.fnmatchcase(resolved_norm, pat_norm)
        normalized_cwd = _normalized_cwd_or_none(cwd)
        if normalized_cwd is None:
            return False
        scoped_pattern = _normalize_match_path_text(
            f"{_normalize_match_path_text(normalized_cwd)}/{pat_norm}"
        )
        return _fnmatch.fnmatchcase(resolved_norm, scoped_pattern)

    def _pattern_list(source: object, key: str) -> list[str]:
        if isinstance(source, Mapping):
            raw = source.get(key, [])
        else:
            raw = getattr(source, key, [])
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw if str(item).strip()]

    # Collect patterns from config.yaml (static) and DB (dynamic).
    cfg_exc = getattr(config, "routing_exceptions", None)
    if cfg_exc is None and isinstance(config, Mapping):
        cfg_exc = config.get("routing_exceptions")
    cfg_skills:    list[str] = _pattern_list(cfg_exc, "skills")
    cfg_filetypes: list[str] = _pattern_list(cfg_exc, "filetypes")
    cfg_projects:  list[str] = _pattern_list(cfg_exc, "projects")
    cfg_commands:  list[str] = _pattern_list(cfg_exc, "commands")
    cfg_callers:   list[str] = _pattern_list(cfg_exc, "callers")
    cfg_paths:     list[str] = _pattern_list(cfg_exc, "paths")

    if skill and cfg_skills and _matches(skill, cfg_skills):
        return True, "routing_exception_skill"

    if filetype and cfg_filetypes and _matches(filetype, cfg_filetypes):
        return True, "routing_exception_filetype"

    if tool_name and cfg_commands and _matches(tool_name, cfg_commands):
        return True, "routing_exception_command"

    if caller and cfg_callers and _matches(caller, cfg_callers):
        return True, "routing_exception_caller"

    if cwd and cfg_projects:
        cwd_norm = cwd.rstrip("/") + "/"
        for pat in cfg_projects:
            pat_norm = pat.rstrip("/") + "/"
            if cwd_norm.lower().startswith(pat_norm.lower()):
                return True, "routing_exception_project"

    if target_file is not None and cfg_paths:
        resolved = _resolve_guard_target(target_file, cwd) or ""
        for pat in cfg_paths:
            if _path_matches(resolved, pat):
                return True, "routing_exception_path"

    if not check_db:
        return False, ""

    if db_rows is None:
        try:
            db_rows = db.routing_exception_list()
        except Exception:
            db_rows = []

    db_by_type: dict[str, list[str]] = {}
    for row in db_rows:
        t = str(row.get("exception_type", ""))
        db_by_type.setdefault(t, []).append(str(row.get("pattern", "")))

    db_skills    = db_by_type.get("skill",    [])
    db_filetypes = db_by_type.get("filetype", [])
    db_projects  = db_by_type.get("project",  [])
    db_commands  = db_by_type.get("command",  [])
    db_callers   = db_by_type.get("caller",   [])
    db_paths     = db_by_type.get("path",     [])

    if skill and db_skills and _matches(skill, db_skills):
        return True, "routing_exception_skill"

    if filetype and db_filetypes and _matches(filetype, db_filetypes):
        return True, "routing_exception_filetype"

    if tool_name and db_commands and _matches(tool_name, db_commands):
        return True, "routing_exception_command"

    if caller and db_callers and _matches(caller, db_callers):
        return True, "routing_exception_caller"

    if cwd and db_projects:
        cwd_norm = cwd.rstrip("/") + "/"
        for pat in db_projects:
            pat_norm = pat.rstrip("/") + "/"
            if cwd_norm.lower().startswith(pat_norm.lower()):
                return True, "routing_exception_project"

    if target_file is not None and db_paths:
        resolved = _resolve_guard_target(target_file, cwd) or ""
        for pat in db_paths:
            if _path_matches(resolved, pat):
                return True, "routing_exception_path"

    return False, ""


def _validate_routing_guard(
    db: Database,
    *,
    caller: str | None,
    cwd: object | None,
    target_file: object | None,
    tool_name: object | None,
    skill: str | None = None,
) -> dict[str, object]:
    config = _ensure_init()[0]
    _cwd_str = _routing_guard_cwd(cwd)
    _explicit_cwd = _normalized_cwd_or_none(cwd)
    if _is_routing_guard_exempt(target_file, _cwd_str):
        return {"valid": True, "reason": "exempt_system_path", "mode": "exempt"}

    _target_str = _normalize_path_input(target_file) if target_file is not None else None
    _filetype: str | None = None
    if _target_str:
        from pathlib import Path as _P
        _suffix = _P(_target_str).suffix
        if _suffix:
            _filetype = _suffix.lower()
    _caller_str = _normalize_route_text(caller)
    _tool_str = _normalize_route_text(tool_name)

    is_exc, exc_reason = _is_routing_exception_exempt(
        db,
        config,
        skill=_normalize_route_text(skill),
        filetype=_filetype,
        cwd=_explicit_cwd,
        tool_name=_tool_str,
        caller=_caller_str,
        target_file=target_file,
    )
    if is_exc:
        return {"valid": True, "reason": exc_reason, "mode": "exempt"}

    normalized_caller = _caller_str or "mcp"
    normalized_cwd = _cwd_str
    normalized_tool = _tool_str or "Edit"
    try:
        guard = db.routing_guard_get(caller=normalized_caller, cwd=normalized_cwd)
    except (ValueError, sqlite3.DatabaseError):
        return _deny_routing_guard(
            f"Routing guard lookup failed for {normalized_tool}. Re-run route_task or decompose_task first."
        )
    if guard is None:
        return _deny_routing_guard(
            f"No routing decision found for {normalized_tool}. Call route_task or decompose_task first."
        )
    if not isinstance(guard, Mapping):
        return _deny_routing_guard(
            f"Routing guard state is invalid for {normalized_tool}. Re-run route_task or decompose_task first."
        )

    mode = _normalize_route_text(guard.get("mode"))
    if mode not in {
        ROUTING_GUARD_MODE_DIRECT,
        ROUTING_GUARD_MODE_EXECUTE_SUBTASK,
        ROUTING_GUARD_MODE_ROUTED_PLAN,
    }:
        return _deny_routing_guard(
            f"Routing guard mode is invalid for {normalized_tool}. Re-run route_task or decompose_task first.",
            guard=guard,
        )
    if mode == ROUTING_GUARD_MODE_EXECUTE_SUBTASK:
        if config.execute_subtask_guard_strict:
            return _deny_routing_guard(
                f"Latest routing decision is low-tier write work. Use execute_subtask(target_file=...) instead of {normalized_tool}.",
                guard=guard,
            )
        return {
            "valid": True,
            "reason": "execute_subtask_hint",
            "mode": "execute_subtask_hint",
            "hint": (
                f"Routing guard suggests execute_subtask(target_file=...) for {normalized_tool}. "
                "Low-tier work is cheaper via execute_subtask."
            ),
            "routing_guard": dict(guard),
        }
    if mode == ROUTING_GUARD_MODE_ROUTED_PLAN:
        # Allow if at least one subtask was executed AND target is in file_hints
        _rg_file_hints = guard.get("file_hints") or []
        _rg_target = _normalize_path_input(target_file)
        _target_in_hints = False
        if _rg_target and _rg_file_hints:
            try:
                _resolved_tgt = str(normalize_target_path(_rg_target, normalized_cwd).resolve())
                _target_in_hints = any(
                    _resolved_tgt == _safe_resolve_path(h)
                    for h in _rg_file_hints
                )
            except Exception:
                pass
        if _target_in_hints:
            try:
                _has_exec = db.routing_guard_has_executions(caller=normalized_caller, cwd=normalized_cwd)
            except Exception:
                _has_exec = False
            if _has_exec:
                pass  # fall through to workspace/file-hints validation below
            else:
                return _deny_routing_guard(
                    f"Latest routing decision is a multi-file routed plan. Execute routed subtasks first, then {normalized_tool} is allowed.",
                    guard=guard,
                )
        else:
            return _deny_routing_guard(
                f"Latest routing decision is a multi-file routed plan. Execute routed subtasks instead of direct {normalized_tool}.",
                guard=guard,
            )

    normalized_target = _normalize_path_input(target_file)
    if not normalized_target:
        return _deny_routing_guard(
            f"{normalized_tool} target_file is required for routing guard validation.",
            guard=guard,
        )
    try:
        resolved_target = normalize_target_path(normalized_target, normalized_cwd)
    except ValueError as exc:
        return _deny_routing_guard(
            f"{normalized_tool} target is invalid: {exc}",
            guard=guard,
        )
    resolved_target_str = _safe_resolve_path(resolved_target)
    if resolved_target_str is None:
        return _deny_routing_guard(
            f"{normalized_tool} target could not be resolved: {normalized_target}",
            guard=guard,
        )
    if not is_within_repo(resolved_target_str, normalized_cwd):
        return _deny_routing_guard(
            f"{normalized_tool} target is outside the latest routed workspace. Re-run route_task for {resolved_target_str} from the correct directory.",
            guard=guard,
        )
    file_hints = guard.get("file_hints")
    if isinstance(file_hints, list) and file_hints:
        normalized_hints = {
            resolved_hint
            for hint in file_hints
            for resolved_hint in [_safe_resolve_path(hint)]
            if resolved_hint is not None
        }
        if normalized_hints and resolved_target_str not in normalized_hints:
            hint_preview = ", ".join(sorted(normalized_hints)[:3])
            return _deny_routing_guard(
                f"{normalized_tool} target is outside the latest routed file scope. Re-run route_task for {resolved_target_str}. Current routed files: {hint_preview}.",
                guard=guard,
            )

    return {
        "valid": True,
        "routing_guard": dict(guard),
    }


def handle_route_task(args: dict) -> dict:
    config, db, router, planner, orchestrator = _ensure_init()
    task = args.get("task", "")
    decision = router.classify(task)

    # Only preview the raw file-generation path when the task itself looks like
    # a write/edit operation. Plain low-tier text tasks still use the normal
    # non-code-only selection path.
    caller = args.get("caller") or _resolve_caller()
    caller_allowlists = getattr(config, "caller_provider_allowlists", None) or None
    selection = None
    try:
        registry = _get_registry_with_config()
        selection = _select_provider_metadata(
            registry,
            decision.tier,
            caller=caller,
            code_only=decision.tier == "low" and _task_likely_writes_files(task),
            config=config,
            caller_allowlists=caller_allowlists,
        )
    except Exception:
        selection = None

    model = (
        _normalize_route_text(selection.get("model"))
        if isinstance(selection, dict)
        else None
    ) or CopilotProvider().resolve_model(decision.tier)

    cached = db.cache_get(task)
    result = {
        "tier": decision.tier,
        "model": model,
        "score": decision.score,
        "reason": decision.reason,
        "agents": decision.agents,
        "cache_hit": cached is not None,
        "override": decision.override,
    }
    result["quick_action"] = {
        "low":    "→ execute_subtask(prompt=..., tier='low', target_file='...')",
        "medium": "→ spawn Task agent with model='sonnet'",
        "high":   "→ spawn Task agent with model='opus'",
    }.get(decision.tier, "→ proceed per tier guidance")
    if isinstance(selection, dict) and _has_executable_routing_metadata(selection):
        result.update({
            key: value
            for key, value in selection.items()
            if (
                key in _ROUTE_RESPONSE_SELECTION_KEYS
                and not callable(value)
                and isinstance(value, (str, int, float, bool))
            )
        })
        if isinstance(selection.get("quota_rationale"), list):
            result["quota_rationale"] = selection["quota_rationale"]
    guard = _issue_routing_guard(
        db,
        caller=caller,
        cwd=args.get("cwd"),
        task=task,
        source_tool="route_task",
        mode=_route_guard_mode_for_task(task, decision.tier),
        tier=decision.tier,
        provider=result.get("provider") if isinstance(result.get("provider"), str) else None,
        model=result.get("model") if isinstance(result.get("model"), str) else None,
    )
    if guard is not None:
        result["routing_guard"] = guard
    _print_dispatch_info(
        tier=result.get("tier", decision.tier),
        model=result.get("model", model),
        provider=str(result.get("provider", "")),
        billing=str(result.get("billing_tier", "")),
        caller=caller,
        task_excerpt=task,
    )
    return result


def handle_validate_routing_guard(args: dict) -> dict:
    _config, db, *_ = _ensure_init()
    caller = args.get("caller") or _resolve_caller()
    return _validate_routing_guard(
        db,
        caller=caller,
        cwd=args.get("cwd"),
        target_file=args.get("target_file"),
        tool_name=args.get("tool_name"),
        skill=args.get("skill"),
    )


def _parse_requested_swarm_agents(raw_value: object, default_value: int) -> int:
    if raw_value is None:
        return default_value
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_agents must be an integer") from exc
    if parsed < 1:
        raise ValueError("max_agents must be at least 1")
    return parsed


def prepare_swarm_execution_request(
    args: Mapping[str, object],
    *,
    config: TGsConfig,
    db: Database | None = None,
    swarm_id: str | None = None,
) -> dict[str, object]:
    """Normalize a future execute_swarm request without changing current MCP surfaces."""
    requested_agents = _parse_requested_swarm_agents(
        args.get("max_agents"),
        config.swarm_max_agents,
    )
    resolved_swarm_id = swarm_id or str(args.get("swarm_id") or f"swarm-{uuid.uuid4().hex}")
    allocation = clamp_swarm_agent_count(
        requested_agents,
        config,
        db=db,
        swarm_id=resolved_swarm_id,
        source="mcp_server",
    )
    requested_topology = str(args.get("topology") or "auto").strip().lower() or "auto"
    selected_topology: str | None = None
    topology_rationale: str | None = None
    urgency_score = 0.0
    effective_topology = requested_topology
    if requested_topology == "auto":
        task_payload = args.get("task")
        task_text = _stringify_execute_swarm_task(task_payload)
        urgency_hint = args.get("urgency_hint")
        urgency_score = _compute_execute_swarm_urgency_score(
            task_text,
            urgency_hint=urgency_hint if isinstance(urgency_hint, str) else None,
        )
        plan_meta = _build_execute_swarm_plan_meta(
            task_payload,
            task_text=task_text,
        )
        selected_topology, topology_rationale = make_auto_topology_decision(
            plan_meta,
            urgency_score,
            allocation.effective_agents,
            config=config,
            db=None,
        )
        effective_topology = selected_topology
    return {
        "swarm_id": resolved_swarm_id,
        "requested_agents": allocation.requested_agents,
        "effective_agents": allocation.effective_agents,
        "hard_cap": allocation.hard_cap,
        "clamped": allocation.clamped,
        "requested_vs_effective_agent_count": {
            "requested": allocation.requested_agents,
            "effective": allocation.effective_agents,
        },
        "topology": effective_topology,
        "selected_topology": selected_topology,
        "topology_rationale": topology_rationale,
        "urgency_score": urgency_score,
    }


def _stringify_execute_swarm_task(raw_task: object) -> str:
    if isinstance(raw_task, str):
        return raw_task
    try:
        return json.dumps(
            raw_task,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return str(raw_task)


def _build_execute_swarm_plan_meta(
    raw_task: object,
    *,
    task_text: str,
) -> dict[str, object]:
    plan_meta: dict[str, object] = {
        "task_chars": len(task_text),
    }
    if not isinstance(raw_task, Mapping):
        return plan_meta
    if isinstance(raw_task.get("has_parent_children"), bool):
        plan_meta["has_parent_children"] = raw_task.get("has_parent_children")
    raw_complexity = raw_task.get("complexity_score")
    if isinstance(raw_complexity, (int, float)):
        plan_meta["complexity_score"] = float(raw_complexity)
    raw_subtasks = raw_task.get("subtasks")
    if not isinstance(raw_subtasks, list):
        return plan_meta
    normalized_subtasks: list[dict[str, object]] = []
    has_parent_children = False
    for raw_subtask in raw_subtasks:
        if not isinstance(raw_subtask, Mapping):
            continue
        parent_id = raw_subtask.get("parent_id")
        if parent_id not in (None, "", []):
            has_parent_children = True
        normalized_subtasks.append(
            {
                "id": raw_subtask.get("id"),
                "parent_id": parent_id,
            }
        )
    if normalized_subtasks:
        plan_meta["subtasks"] = normalized_subtasks
    if has_parent_children:
        plan_meta["has_parent_children"] = True
    return plan_meta


def _compute_execute_swarm_urgency_score(
    task_text: str,
    *,
    urgency_hint: str | None = None,
) -> float:
    urgency_input = task_text
    if urgency_hint:
        urgency_input = f"{task_text}\n{urgency_hint}"
    urgency_score, _ = TaskRouter._compute_urgency_modifier(urgency_input.lower())
    return urgency_score


def _write_execute_swarm_topology_telemetry(
    db: Database,
    *,
    swarm_id: str,
    urgency_score: float,
    selected_topology: str,
    topology_rationale: str,
) -> None:
    try:
        db.write_telemetry_row(
            session_id=swarm_id,
            task_hash=swarm_id,
            agent_id=0,
            tier="medium",
            model="execute_swarm",
            urgency_score=urgency_score,
            selected_topology=selected_topology,
            fanout_final_action="auto_topology",
            parse_diagnostics=json.dumps(
                {
                    "selected_topology": selected_topology,
                    "topology_rationale": topology_rationale,
                    "urgency_score": urgency_score,
                },
                sort_keys=True,
            ),
            reason="execute_swarm_auto_topology",
            version="execute_swarm",
        )
    except Exception:
        log.debug("execute_swarm auto-topology telemetry write failed", exc_info=True)


def _task_payload_size(raw_task: object) -> int:
    try:
        return len(
            json.dumps(
                raw_task,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    except (MemoryError, OverflowError, TypeError, ValueError) as exc:
        raise ValueError("task must be JSON-serializable") from exc


def _rate_limit_execute_swarm(caller_id: str) -> bool:
    now = time.monotonic()
    with _execute_swarm_rate_limit_lock:
        recent = [
            stamp
            for stamp in _execute_swarm_rate_limit.get(caller_id, [])
            if now - stamp < _EXECUTE_SWARM_RATE_LIMIT_WINDOW_SECONDS
        ]
        rate_limited = len(recent) >= _EXECUTE_SWARM_RATE_LIMIT_MAX_CALLS
        recent.append(now)
        if (
            caller_id not in _execute_swarm_rate_limit
            and len(_execute_swarm_rate_limit) >= _EXECUTE_SWARM_RATE_LIMIT_MAX_CALLERS
        ):
            oldest_key = min(
                _execute_swarm_rate_limit.items(),
                key=lambda item: item[1][-1] if item[1] else float("-inf"),
            )[0]
            _execute_swarm_rate_limit.pop(oldest_key, None)
        _execute_swarm_rate_limit[caller_id] = recent
    return rate_limited


def _execute_swarm_preview_secret() -> bytes:
    raw_secret = os.environ.get("PREVIEW_TOKEN_SECRET", "").strip()
    if raw_secret:
        return raw_secret.encode("utf-8")

    secret_path = _EXECUTE_SWARM_PREVIEW_SECRET_FILE
    try:
        secret_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not secret_path.exists():
            temp_path = secret_path.with_name(
                f".{secret_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            generated_secret = secrets.token_hex(32).encode("ascii")
            try:
                written = 0
                while written < len(generated_secret):
                    written += os.write(fd, generated_secret[written:])
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(temp_path, secret_path)
            except FileExistsError:
                pass
            finally:
                temp_path.unlink(missing_ok=True)

        read_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            read_flags |= os.O_NOFOLLOW
        read_fd = os.open(secret_path, read_flags)
        try:
            file_stat = os.fstat(read_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise RuntimeError("preview token secret path is not a regular file")
            stored_secret = os.read(read_fd, 4097)
        finally:
            os.close(read_fd)
        normalized_secret = stored_secret.strip()
        if not normalized_secret or len(stored_secret) > 4096:
            raise RuntimeError("preview token secret file is invalid")
        os.chmod(secret_path, 0o600)
        return normalized_secret
    except OSError as exc:
        raise RuntimeError(
            f"unable to load preview token secret from {secret_path}"
        ) from exc


def _execute_swarm_preview_token_hmac(preview_token: str) -> str:
    if not isinstance(preview_token, str):
        raise ValueError("preview_token must be a string")
    normalized_token = preview_token.strip()
    if not normalized_token:
        raise ValueError("preview_token is required")
    if len(normalized_token) > _EXECUTE_SWARM_MAX_PREVIEW_TOKEN_CHARS:
        raise ValueError(
            f"preview_token must be <= {_EXECUTE_SWARM_MAX_PREVIEW_TOKEN_CHARS} characters"
        )
    return hmac.new(
        _execute_swarm_preview_secret(),
        normalized_token.encode("utf-8"),
        "sha256",
    ).hexdigest()


def _execute_swarm_request_fingerprint(
    raw_task: object,
    *,
    topology: str,
    requested_agents: object,
) -> str:
    def _normalize(value: object) -> object:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return {
                str(key): _normalize(val)
                for key, val in sorted(value.items(), key=lambda item: str(item[0]))
            }
        if isinstance(value, (list, tuple)):
            return [_normalize(item) for item in value]
        raise ValueError("task contains unsupported values for request fingerprinting")

    request_payload = {
        "task": _normalize(raw_task),
        "topology": topology,
        "requested_agents": _normalize(requested_agents),
    }
    encoded = json.dumps(
        request_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _log_swarm_event_safe(
    db: Database,
    swarm_id: str,
    event_type: str,
    payload: Mapping[str, object],
) -> None:
    try:
        normalized_swarm_id = _normalize_swarm_id(swarm_id)
    except ValueError:
        log.warning(
            "execute_swarm telemetry write skipped due to invalid swarm_id",
            extra={"event_type": event_type},
            exc_info=True,
        )
        return
    try:
        db.log_swarm_event(normalized_swarm_id, event_type, payload)
    except Exception:
        log.warning(
            "execute_swarm telemetry write failed for %s",
            event_type,
            exc_info=True,
        )
        return
    if event_type != "wave_progress":
        return
    try:
        emitted_payload, notification_sent = _emit_wave_progress(
            normalized_swarm_id,
            wave=payload.get("wave", 0),
            completed_subtasks=payload.get("completed_subtasks", []),
            pending_subtasks=payload.get("pending_subtasks", []),
            artifacts_produced=payload.get("artifacts_produced", []),
            round=payload.get("round", 0),
            db=db,
        )
    except Exception:
        log.warning(
            "execute_swarm progress emission skipped due to invalid wave payload",
            extra={
                "swarm_id": normalized_swarm_id,
                "event_type": event_type,
            },
            exc_info=True,
        )
        return
    if not notification_sent:
        return
    try:
        db.log_swarm_event(
            normalized_swarm_id,
            "progress_emitted",
            emitted_payload,
        )
    except Exception:
        log.warning(
            "execute_swarm telemetry write failed for progress_emitted",
            exc_info=True,
        )


def _infer_target_file_from_text(text: str) -> str | None:
    """Try to extract a target filename from a subtask description or output.

    Looks for patterns like 'Create math_utils.py', 'file called foo/bar.py',
    or bare code-fence language headers (```python\n# math_utils.py).
    Returns the first plausible relative path, or None.
    """
    import re
    # Pattern: explicit "file called/named X" mention
    explicit = re.search(
        r"(?:file\s+(?:called|named)\s+|create\s+(?:a\s+)?file\s+)([^\s,;:'\"]+\.[a-zA-Z0-9]+)",
        text,
        re.IGNORECASE,
    )
    if explicit:
        candidate = explicit.group(1).strip("/\\")
        if "/" not in candidate and len(candidate) < 100:
            return candidate
        if candidate.count("/") <= 3 and len(candidate) < 100:
            return candidate

    # Pattern: code-fence header comment (# filename or // filename)
    header = re.search(
        r"```[a-z]*\s*\n\s*(?:#|//|--)\s*([^\s]+\.[a-zA-Z0-9]+)",
        text,
    )
    if header:
        candidate = header.group(1).strip()
        if len(candidate) < 100:
            return candidate

    # Fallback: bare filename-like token in text
    bare = re.search(r"\b([a-zA-Z_][\w/]*\.[a-zA-Z0-9]{1,8})\b", text)
    if bare:
        candidate = bare.group(1)
        ext = candidate.rsplit(".", 1)[-1]
        if ext in {"py", "js", "ts", "go", "rs", "java", "cs", "rb", "sh", "yaml", "yml", "json", "md", "txt"}:
            return candidate
    return None


def _materialize_swarm_outputs(
    *,
    db: Database,
    swarm_id: str,
    result: object,
    workspace_root: str | None,
) -> None:
    """Write subtask output to disk and persist a WorkerSnapshot per subtask.

    Called after orchestrator.run() completes. For each subtask result:
    - If the subtask declared a target_file and the agent produced code, write it.
    - Always persist a WorkerSnapshot so worker_snapshot_count > 0 after the run.

    Errors are caught and logged; file-write failures do not abort the run.
    """
    if not isinstance(result, Mapping):
        return

    plan = result.get("plan")
    all_results = result.get("results")
    if not isinstance(all_results, Mapping):
        return

    # Build subtask_id → target_file lookup from the plan
    # Also build subtask_id → description for inference fallback
    target_files: dict[int, str] = {}
    descriptions: dict[int, str] = {}
    if plan is not None:
        subtasks = getattr(plan, "subtasks", None) or []
        for st in subtasks:
            sid = int(getattr(st, "id", -1))
            tf = getattr(st, "target_file", None)
            if tf and isinstance(tf, str) and tf.strip():
                target_files[sid] = tf.strip()
            desc = getattr(st, "description", None)
            if desc and isinstance(desc, str):
                descriptions[sid] = desc

    root = Path(workspace_root).resolve() if workspace_root else None

    from shared.swarm import WorkerSnapshot, persist_worker_snapshot

    for worker_index, (subtask_id, agent_result) in enumerate(all_results.items()):
        output = getattr(agent_result, "output", None) or ""
        snapshot_data: dict[str, object] = {
            "subtask_id": subtask_id,
            "output_chars": len(output),
            "tier": getattr(agent_result, "tier", None),
            "model": getattr(agent_result, "model", None),
        }

        # Write to disk if we have a target file and a workspace
        sid_int = int(subtask_id) if subtask_id is not None else -1
        tf = target_files.get(sid_int)
        # Infer target_file from description or output when planner didn't set it
        if not tf and root and output and output != "(no output)":
            desc = descriptions.get(sid_int, "")
            tf = (
                _infer_target_file_from_text(desc)
                or _infer_target_file_from_text(output)
            )
            if tf:
                log.debug("Swarm %s: inferred target_file=%r from description/output", swarm_id, tf)
        if tf and root and output and output != "(no output)":
            try:
                target_path = (root / tf).resolve()
                target_path.relative_to(root)  # path-traversal guard
                if target_path.exists() and target_path.stat().st_size > 0:
                    snapshot_data["file_written"] = str(target_path)
                    log.debug("Swarm %s: %s exists, skipping materialization", swarm_id, target_path)
                    continue
                if _is_text_doc_target(str(target_path)):
                    code = _extract_text_for_file(output, str(target_path))
                else:
                    code = _extract_code_for_file(output, str(target_path))
                if code:
                    _write_file_with_audit(
                        db,
                        requested_path=target_path,
                        content=code,
                        caller=f"swarm/{swarm_id}",
                        outcome="swarm_materialized",
                    )
                    snapshot_data["file_written"] = str(target_path)
                    log.info(
                        "Swarm %s: materialized %s (%d chars)",
                        swarm_id, target_path, len(code),
                    )
                else:
                    log.debug(
                        "Swarm %s: no extractable code for %s in subtask %s",
                        swarm_id, tf, subtask_id,
                    )
            except ValueError:
                log.warning(
                    "Swarm %s: target_file %r escapes workspace root — skipping",
                    swarm_id, tf,
                )
            except Exception:
                log.warning(
                    "Swarm %s: failed to write %r", swarm_id, tf, exc_info=True
                )

        try:
            persist_worker_snapshot(
                WorkerSnapshot(
                    swarm_id=swarm_id,
                    worker_index=worker_index,
                    snapshot=snapshot_data,
                ),
                db=db,
            )
        except Exception:
            log.warning(
                "Swarm %s: failed to persist worker snapshot for subtask %s",
                swarm_id, subtask_id, exc_info=True,
            )


def _execute_swarm_runtime_handoff(
    db: Database,
    orchestrator: Orchestrator,
    swarm_id: str,
    execution_context: object,
    started_response: dict | None = None,
    progress_token: object | None = None,
    workspace_root: str | None = None,
) -> None:
    """Execute a swarm run via the shared Orchestrator and persist final state.

    Behavior notes (traceability: D-01..D-04, D-09..D-12):
    - Start execution immediately and attempt to stream progress when a
      progress_token is provided. Streaming errors must not raise.
    - Persist final swarm_run record via shared.swarm.persist_swarm_run so the
      run is inspectable even if the MCP channel drops.
    - Record runtime exceptions as swarm_events with event_type="runtime_error"
      and do not crash the MCP process.
    - Materialize subtask output files into workspace_root when subtasks declare
      a target_file (or when target files can be inferred from output).
    """
    try:
        normalized_swarm_id = _normalize_swarm_id(swarm_id)
    except ValueError as exc:
        log.warning("runtime handoff skipped due to invalid swarm_id: %s", swarm_id)
        return

    # Start optional heartbeat to the caller (best-effort) (D-02)
    # When progressToken is available, stream progress notifications to keep channel open
    # and deliver final payload to the client as per hybrid contract (D-01)
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    token = _normalize_progress_token(progress_token) if progress_token is not None else None
    if token is not None:
        try:
            heartbeat_stop = threading.Event()
            heartbeat_thread = threading.Thread(
                target=_heartbeat_loop, args=(token, heartbeat_stop), daemon=True
            )
            heartbeat_thread.start()
        except Exception:
            log.debug("Failed to start heartbeat for progress token", exc_info=True)

    try:
        if execution_context is None:
            raise ValueError("execution_context must include the original task")
        task_text = _task_text_from_execution_context(execution_context)
        if not task_text.strip():
            raise ValueError("execution_context produced an empty task")
        topology, max_agents = _swarm_runtime_constraints(execution_context)
        unlimited_budget = bool(execution_context.get("unlimited_budget", False)) if isinstance(execution_context, Mapping) else False

        router = _router if "_router" in globals() else None
        # Call orchestrator.run — this is the shared hot-path that returns plan/results
        # Per D-04: if channel drops mid-run, orchestrator continues and persisted state
        # remains inspectable via shared/swarm.get_swarm_summary
        result = orchestrator.run(
            task_text,
            router=router,
            execution_id=normalized_swarm_id,
            topology=topology,
            max_agents=max_agents,
            unlimited_budget=unlimited_budget,
            workspace_root=workspace_root,
        )

        # Materialize subtask outputs as files and persist worker snapshots
        _materialize_swarm_outputs(
            db=db,
            swarm_id=normalized_swarm_id,
            result=result,
            workspace_root=workspace_root,
        )

        # Build a summary-first completion payload and persist authoritative SwarmRun
        try:
            from shared.swarm import SwarmRun, persist_swarm_run, get_swarm_summary

            synthesis = result.get("synthesis") if isinstance(result, Mapping) else None
            cost_summary = None
            summary = get_swarm_summary(normalized_swarm_id, db=db)
            if isinstance(summary, Mapping):
                run_record = SwarmRun(
                    swarm_id=normalized_swarm_id,
                    task_hash=str(summary.get("task_hash") or ""),
                    status="completed",
                    requested_agents=int(summary.get("requested_agents") or 0),
                    effective_agents=int(summary.get("effective_agents") or 0),
                    progress_counters=summary.get("progress_counters") or {},
                    cost_summary_ref=summary.get("cost_summary_ref"),
                    topology=str(summary.get("topology") or "") or None,
                    round=int(summary.get("round") or 0),
                    resumable=bool(summary.get("resumable") or False),
                    resume_status=str(summary.get("resume_status") or "not_resumable"),
                    parent_swarm_id=summary.get("parent_swarm_id"),
                    chosen_checkpoint_index=(
                        _safe_coerce_int(summary.get("chosen_checkpoint_index"))
                        if summary.get("chosen_checkpoint_index") is not None
                        else None
                    ),
                )
            else:
                run_record = SwarmRun(swarm_id=normalized_swarm_id, status="completed")
            persist_swarm_run(run_record, db=db)
        except Exception:
            log.warning("Failed to persist swarm completion record", exc_info=True)

    except Exception as exc:
        # Record runtime error as a swarm_event and persist a failed swarm_run
        try:
            _log_swarm_event_safe(db, normalized_swarm_id, "runtime_error", {"message": str(exc)})
        except Exception:
            log.warning("Failed to log runtime error event", exc_info=True)
        try:
            from shared.swarm import SwarmRun, get_swarm_summary, persist_swarm_run

            summary = get_swarm_summary(normalized_swarm_id, db=db) or {}
            persist_swarm_run(
                SwarmRun(
                    swarm_id=normalized_swarm_id,
                    task_hash=str(summary.get("task_hash") or ""),
                    status="failed",
                    requested_agents=int(summary.get("requested_agents") or 0),
                    effective_agents=int(summary.get("effective_agents") or 0),
                    progress_counters=summary.get("progress_counters") or {},
                    cost_summary_ref=summary.get("cost_summary_ref"),
                    topology=str(summary.get("topology") or "") or None,
                    round=int(summary.get("round") or 0),
                    resumable=False,
                    resume_status="failed",
                    parent_swarm_id=summary.get("parent_swarm_id"),
                    chosen_checkpoint_index=(
                        _safe_coerce_int(summary.get("chosen_checkpoint_index"))
                        if summary.get("chosen_checkpoint_index") is not None
                        else None
                    ),
                ),
                db=db,
            )
        except Exception:
            log.warning("Failed to persist failed swarm_run after runtime error", exc_info=True)
    finally:
        if heartbeat_thread is not None and heartbeat_stop is not None:
            heartbeat_stop.set()
            try:
                heartbeat_thread.join(timeout=0.1)
            except Exception:
                log.debug("heartbeat join failed", exc_info=True)


def _build_swarm_completion_result(
    swarm_id: str,
    status: str,
    synthesis_summary: object | None,
    cost_summary: object | None,
    *,
    db: Database | None = None,
) -> dict[str, object]:
    """Format a summary-first completion payload for operator-facing APIs.

    Uses shared.swarm.get_swarm_summary to hydrate metadata where possible.
    """
    from shared.swarm import get_swarm_summary

    normalized_swarm_id = ""
    try:
        normalized_swarm_id = _normalize_swarm_id(swarm_id)
    except ValueError:
        normalized_swarm_id = str(swarm_id or "")

    summary = get_swarm_summary(normalized_swarm_id, db=db) if db is not None else None
    payload: dict[str, object] = {
        "swarm_id": normalized_swarm_id,
        "status": str(status or "") or "unknown",
        "synthesis": synthesis_summary or {},
        "cost_summary": cost_summary or {},
        "topology": None,
        "wave_summary": [],
        "parent_swarm_id": None,
        "chosen_checkpoint_index": None,
    }
    if isinstance(summary, Mapping):
        payload["topology"] = summary.get("topology")
        payload["wave_summary"] = summary.get("wave_summary") or []
        payload["parent_swarm_id"] = summary.get("parent_swarm_id")
        payload["chosen_checkpoint_index"] = summary.get("chosen_checkpoint_index")
    return payload


def _task_text_from_execution_context(execution_context: object) -> str:
    if isinstance(execution_context, Mapping):
        task_text = execution_context.get("task_text")
        if task_text is not None:
            return str(task_text)
        task_value = execution_context.get("task")
        if task_value is not None:
            return str(task_value)
    return str(execution_context)


def _swarm_runtime_constraints(
    execution_context: object,
) -> tuple[str | None, int | None]:
    if not isinstance(execution_context, Mapping):
        return None, None
    raw_topology = execution_context.get("topology")
    topology = str(raw_topology or "").strip().lower() or None
    raw_max_agents = execution_context.get("max_agents")
    max_agents = _safe_coerce_int(raw_max_agents)
    if max_agents is not None and max_agents < 1:
        max_agents = None
    return topology, max_agents


def _normalize_execute_swarm_task_text(raw_task: object) -> str:
    if isinstance(raw_task, Mapping):
        task_value = raw_task.get("task")
        if task_value is not None:
            return _stringify_execute_swarm_task(task_value)
    return _stringify_execute_swarm_task(raw_task)


def _resolve_swarm_execution_context(db: Database, swarm_id: str) -> object | None:
    for event_type in ("preview_required", "execute_swarm_requested"):
        payload = db.get_latest_swarm_event_payload(swarm_id, event_type)
        if isinstance(payload, Mapping):
            if "task_text" in payload:
                return {
                    "task_text": payload.get("task_text"),
                    "topology": payload.get("effective_topology")
                    or payload.get("topology"),
                    "max_agents": payload.get("effective_agents"),
                }
            if "task" in payload:
                return payload.get("task")
    summary = db.get_swarm_summary(swarm_id)
    if isinstance(summary, Mapping):
        counters = summary.get("progress_counters")
        if isinstance(counters, Mapping):
            if "task_text" in counters:
                return counters.get("task_text")
            if "task" in counters:
                return counters.get("task")
    return None


def _resume_swarm_runtime_handoff(
    db: Database,
    orchestrator: Orchestrator,
    new_swarm_id: str,
    parent_lineage: Mapping[str, object],
    progress_token: object | None = None,
) -> dict[str, object]:
    """Resume runtime handoff semantics for a new swarm seeded from a checkpoint.

    Traceability: D-05, D-06, D-07
    - Start resumed execution immediately after checkpoint confirmation (D-05)
    - Expose parent swarm + checkpoint lineage in normal output and inspection (D-06)
    - Resumed runs follow same live + inspect mirror contract as fresh execute_swarm runs (D-07)
    
    Ensures the persisted completion payload includes parent_swarm_id and
    chosen_checkpoint_index per D-06 and D-11. On resume failure, persist an
    explicit resume failure (resume_status="failed") and return a failure
    payload with reason and next action (D-15, D-16).
    """
    try:
        normalized_swarm_id = _normalize_swarm_id(new_swarm_id)
    except ValueError as exc:
        raise

    # Stream progress when progressToken available (D-07: same contract as fresh runs)
    token = _normalize_progress_token(progress_token) if progress_token is not None else None
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    if token is not None:
        try:
            heartbeat_stop = threading.Event()
            heartbeat_thread = threading.Thread(
                target=_heartbeat_loop, args=(token, heartbeat_stop), daemon=True
            )
            heartbeat_thread.start()
        except Exception:
            log.debug("Failed to start heartbeat for resume progress token", exc_info=True)

    parent_swarm_id = str(parent_lineage.get("parent_swarm_id") or "").strip() if isinstance(parent_lineage, Mapping) else None
    chosen_checkpoint_index = None
    chosen_plan_revision = None
    try:
        if isinstance(parent_lineage, Mapping):
            try:
                chosen_checkpoint_index = int(parent_lineage.get("chosen_checkpoint_index"))
            except Exception:
                chosen_checkpoint_index = None
            try:
                chosen_plan_revision = int(parent_lineage.get("plan_revision"))
            except Exception:
                chosen_plan_revision = None

        try:
            execution_context = (
                parent_lineage.get("execution_context")
                if isinstance(parent_lineage, Mapping)
                else None
            )
            if execution_context is None and isinstance(parent_lineage, Mapping):
                execution_context = parent_lineage.get("task")
            if execution_context is None:
                raise ValueError("resume source task is unavailable for failed_swarm_id")
            task_text = _task_text_from_execution_context(execution_context)
            if not task_text.strip():
                raise ValueError("resume source task is empty for failed_swarm_id")

            router = _router if "_router" in globals() else None
            # Per D-04 & D-07: channel drop doesn't cancel resumed runs; persisted result
            # remains inspectable and recoverable via shared/swarm
            result = orchestrator.run(
                task_text or "",
                router=router,
                execution_id=normalized_swarm_id,
            )
            synthesis = result.get("synthesis") if isinstance(result, Mapping) else None
            completion = _build_swarm_completion_result(
                normalized_swarm_id, "completed", synthesis, None, db=db
            )
            from shared.swarm import SwarmRun, persist_swarm_run
            summary = db.get_swarm_summary(normalized_swarm_id)

            persist_swarm_run(
                SwarmRun(
                    swarm_id=normalized_swarm_id,
                    task_hash=str(summary.get("task_hash") or "") if isinstance(summary, Mapping) else "",
                    status="completed",
                    requested_agents=int(summary.get("requested_agents") or 0) if isinstance(summary, Mapping) else 0,
                    effective_agents=int(summary.get("effective_agents") or 0) if isinstance(summary, Mapping) else 0,
                    progress_counters=summary.get("progress_counters") or {} if isinstance(summary, Mapping) else {},
                    cost_summary_ref=summary.get("cost_summary_ref") if isinstance(summary, Mapping) else None,
                    topology=(str(summary.get("topology") or "") or None) if isinstance(summary, Mapping) else None,
                    round=int(summary.get("round") or 0) if isinstance(summary, Mapping) else 0,
                    resumable=False,
                    resume_status="resumed",
                    parent_swarm_id=parent_swarm_id,
                    chosen_checkpoint_index=chosen_checkpoint_index,
                ),
                db=db,
            )
            completion["parent_swarm_id"] = parent_swarm_id
            completion["chosen_checkpoint_index"] = chosen_checkpoint_index
            completion["lineage"] = {
                "parent_swarm_id": parent_swarm_id,
                "chosen_checkpoint_index": chosen_checkpoint_index,
                "plan_revision": chosen_plan_revision,
            }
            return completion
        except Exception as exc:
            # Persist explicit resume failure and return controlled payload
            _log_swarm_event_safe(db, normalized_swarm_id, "resume_error", {"message": str(exc)})
            from shared.swarm import SwarmRun, persist_swarm_run
            summary = db.get_swarm_summary(normalized_swarm_id)

            persist_swarm_run(
                SwarmRun(
                    swarm_id=normalized_swarm_id,
                    task_hash=str(summary.get("task_hash") or "") if isinstance(summary, Mapping) else "",
                    status="failed",
                    requested_agents=int(summary.get("requested_agents") or 0) if isinstance(summary, Mapping) else 0,
                    effective_agents=int(summary.get("effective_agents") or 0) if isinstance(summary, Mapping) else 0,
                    progress_counters=summary.get("progress_counters") or {} if isinstance(summary, Mapping) else {},
                    cost_summary_ref=summary.get("cost_summary_ref") if isinstance(summary, Mapping) else None,
                    topology=(str(summary.get("topology") or "") or None) if isinstance(summary, Mapping) else None,
                    round=int(summary.get("round") or 0) if isinstance(summary, Mapping) else 0,
                    resumable=False,
                    resume_status="failed",
                    parent_swarm_id=parent_swarm_id,
                    chosen_checkpoint_index=chosen_checkpoint_index,
                ),
                db=db,
            )
            return {
                "swarm_id": normalized_swarm_id,
                "status": "failed",
                "reason": str(exc),
                "next_action": "inspect/resume",
                "parent_swarm_id": parent_swarm_id,
                "chosen_checkpoint_index": chosen_checkpoint_index,
                "plan_revision": chosen_plan_revision,
            }
    finally:
        if heartbeat_thread is not None and heartbeat_stop is not None:
            heartbeat_stop.set()
            try:
                heartbeat_thread.join(timeout=0.1)
            except Exception:
                log.debug("heartbeat join failed", exc_info=True)




def _fast_swarm_cost_estimate(
    *,
    effective_agents: int,
    task_chars: int,
    conservative: bool = False,
) -> float:
    base_cost = 1.0
    per_agent_cost = 0.5 * max(effective_agents, 0)
    payload_cost = min(max(task_chars, 0), _EXECUTE_SWARM_MAX_TASK_CHARS) / 1_000.0 * 0.1
    multiplier = 1.5 if conservative else 1.0
    return round((base_cost + per_agent_cost + payload_cost) * multiplier, 2)


def _snapshot_execute_swarm_response(response: Mapping[str, object]) -> dict[str, object]:
    snapshot = dict(response)
    snapshot.pop("preview", None)
    snapshot.pop("preview_token", None)
    snapshot.pop("expires_in", None)
    snapshot.pop("estimated_cost", None)
    snapshot.pop("budget_limit", None)
    snapshot.pop("budget_delta", None)
    return snapshot


def _extract_estimated_cost(response: Mapping[str, object]) -> float | None:
    nested = response.get("cost_estimate")
    if isinstance(nested, Mapping):
        raw_estimated = nested.get("estimated")
        if isinstance(raw_estimated, (int, float)) and math.isfinite(float(raw_estimated)):
            return float(raw_estimated)
    raw_estimated = response.get("estimated_cost")
    if isinstance(raw_estimated, (int, float)) and math.isfinite(float(raw_estimated)):
        return float(raw_estimated)
    return None


def _spawn_execute_swarm_runtime_handoff(
    db: Database,
    swarm_id: str,
    execution_context: object,
    workspace_root: str | None = None,
) -> None:
    """Spawn a background thread to execute a swarm via the runtime handoff.
    
    Traceability: D-01, D-02, D-04
    - Start execution in background to preserve immediate return semantics per Phase 36
    - Best-effort stream progress if progressToken is available (via _request_context)
    - Persist final state so runs remain inspectable even if MCP channel drops
    """
    try:
        _, _, _, _, orchestrator = _ensure_init()
    except Exception as exc:
        log.warning(
            "Failed to get orchestrator for swarm %s background execution",
            swarm_id,
            exc_info=True,
        )
        _log_swarm_event_safe(
            db,
            swarm_id,
            "runtime_start_failed",
            {"message": str(exc)},
        )
        return
    
    # Resolve workspace_root from the persisted event if not passed directly
    resolved_workspace_root = workspace_root
    if resolved_workspace_root is None:
        payload = db.get_latest_swarm_event_payload(swarm_id, "execute_swarm_requested")
        if isinstance(payload, Mapping):
            raw_wr = payload.get("workspace_root")
            if isinstance(raw_wr, str) and raw_wr.strip():
                resolved_workspace_root = raw_wr.strip()

    # Extract progress token from thread-local MCP context if available
    progress_token = getattr(_request_context, "progress_token", None)
    
    # Spawn background thread to run the handoff (D-01, D-04)
    try:
        thread = threading.Thread(
            target=_execute_swarm_runtime_handoff,
            args=(db, orchestrator, swarm_id, execution_context, None, progress_token, resolved_workspace_root),
            name=f"tgs-swarm-{swarm_id[-12:]}",
            daemon=True,
        )
        summary = db.get_swarm_summary(swarm_id) or {}
        topology, max_agents = _swarm_runtime_constraints(execution_context)
        db.persist_swarm_run({
            "swarm_id": swarm_id,
            "task_hash": str(summary.get("task_hash") or ""),
            "status": "running",
            "requested_agents": int(summary.get("requested_agents") or max_agents or 0),
            "effective_agents": int(summary.get("effective_agents") or max_agents or 0),
            "progress_counters": summary.get("progress_counters") or {},
            "cost_summary_ref": summary.get("cost_summary_ref"),
            "topology": summary.get("topology") or topology,
            "round": int(summary.get("round") or 0),
            "resumable": bool(summary.get("resumable") or False),
            "resume_status": str(summary.get("resume_status") or "not_resumable"),
            "parent_swarm_id": summary.get("parent_swarm_id"),
            "chosen_checkpoint_index": summary.get("chosen_checkpoint_index"),
        })
        _log_swarm_event_safe(
            db,
            swarm_id,
            "runtime_handoff_started",
            {
                "thread_name": thread.name,
                "workspace_root": resolved_workspace_root,
            },
        )
        thread.start()
    except Exception as exc:
        log.warning(
            "Failed to spawn runtime handoff thread for swarm %s",
            swarm_id,
            exc_info=True,
        )
        _log_swarm_event_safe(
            db,
            swarm_id,
            "runtime_start_failed",
            {"message": str(exc)},
        )


def confirm_preview_and_start(
    preview_token: str,
    operator_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(preview_token, str):
        return {"error": "invalid_request", "details": "preview_token must be a string"}
    normalized_preview_token = preview_token.strip()
    if not normalized_preview_token:
        return {"error": "invalid_request", "details": "preview_token is required"}

    del operator_id  # Reserved for future audit enrichment without breaking the helper.

    try:
        token_hmac = _execute_swarm_preview_token_hmac(normalized_preview_token)
        _config, db, *_ = _ensure_init()
    except ValueError as exc:
        return {"error": "invalid_request", "details": str(exc)}
    except RuntimeError:
        return {
            "error": "execution_error",
            "details": "preview token signing is unavailable",
        }
    except Exception:
        log.warning("confirm_preview_and_start initialization failed", exc_info=True)
        return {
            "error": "execution_error",
            "details": "execute_swarm initialization failed",
        }

    swarm_id = db.lookup_preview_token_swarm_id(token_hmac)
    if swarm_id is None:
        return {
            "error": "invalid_preview_token",
            "details": "preview_token is invalid, expired, or already used",
        }

    preview_payload = db.get_latest_swarm_event_payload(swarm_id, "preview_required")
    if not isinstance(preview_payload, Mapping):
        return {
            "error": "invalid_preview_token",
            "details": "preview_token is invalid, expired, or already used",
        }

    stored_response = preview_payload.get("response")
    if not isinstance(stored_response, Mapping):
        return {
            "error": "invalid_preview_token",
            "details": "preview_token is missing preview response metadata",
        }

    task_from_preview = preview_payload.get("task") if isinstance(preview_payload, Mapping) else None
    if task_from_preview is None:
        task_from_preview = _resolve_swarm_execution_context(db, swarm_id)
    if task_from_preview is None:
        return {
            "error": "execution_error",
            "details": "preview metadata is missing original task context",
        }
    if not db.consume_preview_token(token_hmac):
        return {
            "error": "invalid_preview_token",
            "details": "preview_token is invalid, expired, or already used",
        }

    confirmed_response = dict(stored_response)
    confirmed_response["confirmed"] = True
    estimated_cost = _extract_estimated_cost(confirmed_response)
    _log_swarm_event_safe(
        db,
        swarm_id,
        "preview_confirmed",
        {
            "estimated_cost": estimated_cost,
            "topology": confirmed_response.get("effective_values", {}).get("topology")
            if isinstance(confirmed_response.get("effective_values"), Mapping)
            else None,
        },
    )
    
    # Spawn background runtime handoff (D-01, D-02) — preserve immediate return semantics
    # workspace_root is resolved from the persisted event inside _spawn_execute_swarm_runtime_handoff
    effective_values = confirmed_response.get("effective_values")
    execution_context = {
        "task_text": _task_text_from_execution_context(task_from_preview),
        "topology": effective_values.get("topology")
        if isinstance(effective_values, Mapping)
        else None,
        "max_agents": effective_values.get("max_agents")
        if isinstance(effective_values, Mapping)
        else None,
    }
    _spawn_execute_swarm_runtime_handoff(db, swarm_id, execution_context)
    
    return {"result": confirmed_response, "started": True}


def handle_execute_swarm(args: dict) -> dict:
    """Implement the Phase 36 immediate execute_swarm contract (D-01..D-04)."""
    raw_task = args.get("task")
    if raw_task is None:
        return {"error": "invalid_request", "details": "task is required"}
    normalized_task_text = _normalize_execute_swarm_task_text(raw_task).strip()
    if not normalized_task_text:
        return {"error": "invalid_request", "details": "task must not be empty"}

    raw_workspace_root = args.get("workspace_root")
    workspace_root: str | None = None
    if raw_workspace_root is not None:
        if not isinstance(raw_workspace_root, str):
            return {"error": "invalid_request", "details": "workspace_root must be a string"}
        workspace_root = raw_workspace_root.strip() or None

    try:
        task_chars = _task_payload_size(raw_task)
    except ValueError as exc:
        return {"error": "invalid_request", "details": str(exc)}
    if task_chars > _EXECUTE_SWARM_MAX_TASK_CHARS:
        return {
            "error": "input_too_large",
            "details": (
                f"task must be <= {_EXECUTE_SWARM_MAX_TASK_CHARS} characters when JSON-encoded"
            ),
        }

    raw_topology = args.get("topology")
    if raw_topology is not None and not isinstance(raw_topology, str):
        return {"error": "invalid_request", "details": "topology must be a string"}
    topology = str(raw_topology or "auto").strip().lower() or "auto"
    if topology not in {"star", "hierarchical", "dag", "auto"}:
        return {"error": "invalid_request", "details": "topology must be one of: star, hierarchical, dag, auto"}
    raw_urgency_hint = args.get("urgency_hint")
    if raw_urgency_hint is not None:
        if not isinstance(raw_urgency_hint, str):
            return {"error": "invalid_request", "details": "urgency_hint must be a string"}
        if len(raw_urgency_hint) > _EXECUTE_SWARM_MAX_URGENCY_HINT_CHARS:
            return {
                "error": "invalid_request",
                "details": (
                    f"urgency_hint must be <= {_EXECUTE_SWARM_MAX_URGENCY_HINT_CHARS} characters"
                ),
            }

    raw_budget_limit = args.get("budget_limit")
    budget_limit: float | None = None
    if raw_budget_limit is not None:
        try:
            budget_limit = float(raw_budget_limit)
        except (TypeError, ValueError) as exc:
            return {"error": "invalid_request", "details": "budget_limit must be a number"}
        if not math.isfinite(budget_limit):
            return {
                "error": "invalid_request",
                "details": "budget_limit must be a finite number",
            }
        if budget_limit < 0:
            return {"error": "invalid_request", "details": "budget_limit must be >= 0"}

    unlimited_budget: bool = bool(args.get("unlimited_budget", False))
    if unlimited_budget:
        budget_limit = None

    raw_preview_token = args.get("preview_token")
    preview_token: str | None = None
    if raw_preview_token is not None:
        if not isinstance(raw_preview_token, str):
            return {"error": "invalid_request", "details": "preview_token must be a string"}
        preview_token = raw_preview_token.strip()
        if not preview_token:
            return {"error": "invalid_request", "details": "preview_token is required"}

    request_args = dict(args)
    request_args.pop("swarm_id", None)
    request_args.pop("preview_token", None)
    request_args["topology"] = topology
    try:
        config, db, *_ = _ensure_init()
        preview_swarm_id: str | None = None
        preview_token_hmac: str | None = None
        if preview_token is not None:
            try:
                preview_token_hmac = _execute_swarm_preview_token_hmac(preview_token)
            except ValueError as exc:
                return {"error": "invalid_request", "details": str(exc)}
            except RuntimeError:
                return {
                    "error": "execution_error",
                    "details": "preview token signing is unavailable",
                }
            preview_swarm_id = db.lookup_preview_token_swarm_id(preview_token_hmac)
            if preview_swarm_id is None:
                return {
                    "error": "invalid_preview_token",
                    "details": "preview_token is invalid, expired, or already used",
                }
        request_meta = prepare_swarm_execution_request(
            request_args,
            config=config,
            db=db,
            swarm_id=preview_swarm_id,
        )
    except ValueError as exc:
        return {"error": "invalid_request", "details": str(exc)}
    except Exception:
        log.warning("execute_swarm initialization failed", exc_info=True)
        return {
            "error": "execution_error",
            "details": "execute_swarm initialization failed",
        }
    try:
        request_fingerprint = _execute_swarm_request_fingerprint(
            raw_task,
            topology=topology,
            requested_agents=request_meta.get("requested_agents"),
        )
    except ValueError as exc:
        return {"error": "invalid_request", "details": str(exc)}

    caller_id = _resolve_caller() or "anonymous"
    rate_limited = _rate_limit_execute_swarm(caller_id)
    try:
        _raw_ea = request_meta.get("effective_agents") or 0
        effective_agents = int(_raw_ea)
    except (TypeError, ValueError):
        return {
            "error": "execution_error",
            "details": "execute_swarm returned invalid agent metadata",
        }
    estimated_cost = _fast_swarm_cost_estimate(
        effective_agents=effective_agents,
        task_chars=task_chars,
        conservative=rate_limited,
    )

    swarm_id = str(request_meta.get("swarm_id", ""))
    effective_topology = str(request_meta.get("topology") or topology)
    runtime_context = {
        "task_text": normalized_task_text,
        "topology": effective_topology,
        "max_agents": effective_agents,
        "unlimited_budget": unlimited_budget,
    }
    _log_swarm_event_safe(
        db,
        swarm_id,
        "execute_swarm_requested",
        {
            "topology": topology,
            "effective_topology": effective_topology,
            "effective_agents": effective_agents,
            "task_chars": task_chars,
            "task_text": normalized_task_text,
            "estimated_cost": estimated_cost,
            "rate_limited": rate_limited,
            **({"workspace_root": workspace_root} if workspace_root else {}),
        },
    )

    requested_vs_effective = request_meta.get("requested_vs_effective_agent_count")
    if not isinstance(requested_vs_effective, Mapping):
        requested_vs_effective = {
            "requested": request_meta.get("requested_agents"),
            "effective": request_meta.get("effective_agents"),
        }

    initial_response: dict[str, object] = {
        "swarm_id": swarm_id,
        "requested_vs_effective_agent_count": requested_vs_effective,
        "adjusted": bool(
            requested_vs_effective.get("requested")
            != requested_vs_effective.get("effective")
        ),
        "wave_summary": [
            {
                "wave": 1,
                "count": effective_agents,
                "label": "start-workers",
            }
        ],
        "cost_estimate": {
            "estimated": float(estimated_cost),
            "currency": "USD",
            "unit": "credits",
            "method": "fast_heuristic",
        },
        "requested_values": {
            "topology": raw_topology,
            "max_agents": args.get("max_agents"),
        },
        "effective_values": {
            "topology": request_meta.get("topology", topology),
            "max_agents": request_meta.get("effective_agents"),
        },
    }
    if bool(request_meta.get("clamped")):
        initial_response["clamped"] = True
    if rate_limited:
        initial_response["rate_limited"] = True
    selected_topology = request_meta.get("selected_topology")
    topology_rationale = request_meta.get("topology_rationale")
    if isinstance(selected_topology, str) and selected_topology and selected_topology != "dag":
        initial_response["selected_topology"] = selected_topology
        if isinstance(topology_rationale, str) and topology_rationale:
            initial_response["topology_rationale"] = topology_rationale
    if topology == "auto" and isinstance(selected_topology, str) and selected_topology:
        _write_execute_swarm_topology_telemetry(
            db,
            swarm_id=swarm_id,
            urgency_score=float(request_meta.get("urgency_score", 0.0) or 0.0),
            selected_topology=selected_topology,
            topology_rationale=str(topology_rationale or ""),
        )

    if preview_token is not None:
        assert preview_token_hmac is not None
        preview_payload = db.get_latest_swarm_event_payload(swarm_id, "preview_required")
        if not isinstance(preview_payload, Mapping):
            return {
                "error": "invalid_preview_token",
                "details": "preview_token is invalid, expired, or already used",
            }
        if preview_payload.get("request_fingerprint") != request_fingerprint:
            return {
                "error": "invalid_preview_token",
                "details": "preview_token does not match the previewed request",
            }
        if not db.consume_preview_token(preview_token_hmac):
            return {
                "error": "invalid_preview_token",
                "details": "preview_token is invalid, expired, or already used",
            }
        initial_response["confirmed"] = True
        _log_swarm_event_safe(
            db,
            swarm_id,
            "preview_confirmed",
            {
                "estimated_cost": estimated_cost,
                "topology": topology,
            },
        )
        
        # Spawn background runtime handoff (D-01, D-02) — preserve immediate return semantics
        _spawn_execute_swarm_runtime_handoff(
            db,
            swarm_id,
            runtime_context,
            workspace_root=workspace_root,
        )
        
        return {"result": initial_response, "started": True}

    started = True
    if budget_limit is not None and estimated_cost > budget_limit:
        preview_token = secrets.token_hex(32)
        try:
            preview_token_hmac = _execute_swarm_preview_token_hmac(preview_token)
        except RuntimeError:
            return {
                "error": "execution_error",
                "details": "preview token signing is unavailable",
            }
        expires_in = _EXECUTE_SWARM_PREVIEW_EXPIRY_SECONDS
        expires_ts = time.time() + expires_in
        budget_delta = round(estimated_cost - budget_limit, 2)
        confirmation_response = _snapshot_execute_swarm_response(initial_response)
        preview_payload = {
            "budget_limit": budget_limit,
            "estimated_cost": estimated_cost,
            "budget_delta": budget_delta,
            "expires_in": expires_in,
            "request_fingerprint": request_fingerprint,
            "response": confirmation_response,
        }
        try:
            if not db.persist_preview_token_with_event(
                preview_token_hmac,
                swarm_id,
                expires_ts,
                event_type="preview_required",
                payload=preview_payload,
            ):
                raise RuntimeError("preview token persistence returned false")
        except Exception:
            log.warning("execute_swarm preview token persist failed", exc_info=True)
            return {
                "error": "execution_error",
                "details": "failed to persist preview token",
            }
        initial_response["preview"] = True
        initial_response["preview_token"] = preview_token
        initial_response["expires_in"] = expires_in
        initial_response["estimated_cost"] = estimated_cost
        initial_response["budget_limit"] = budget_limit
        initial_response["budget_delta"] = budget_delta
        return {"result": initial_response, "started": False}

    # Spawn background runtime handoff (D-01, D-02) — preserve immediate return semantics
    _spawn_execute_swarm_runtime_handoff(
        db,
        swarm_id,
        runtime_context,
        workspace_root=workspace_root,
    )
    
    return {"result": initial_response, "started": True}


def handle_cache_get(args: dict) -> dict:
    try:
        _, db, *_ = _ensure_init()
    except Exception:
        return {"error": "database unavailable — route_task still works", "code": "DB_UNAVAILABLE"}
    hit = db.cache_get(args.get("task", ""))
    if hit:
        result, model = hit
        return {"found": True, "result": result, "model": model}
    return {"found": False}


def handle_cache_put(args: dict) -> dict:
    try:
        _, db, *_ = _ensure_init()
    except Exception:
        return {"error": "database unavailable — route_task still works", "code": "DB_UNAVAILABLE"}
    db.cache_put(args.get("task", ""), args.get("result", ""), args.get("model", ""))
    return {"stored": True}


def handle_cache_stats(_args: dict) -> dict:
    try:
        _, db, *_ = _ensure_init()
    except Exception:
        return {"error": "database unavailable — route_task still works", "code": "DB_UNAVAILABLE"}
    return db.cache_stats()


def _resolve_caller() -> str | None:
    """Determine which provider is hosting us, preferring host env markers over clientInfo."""
    env_caller = detect_caller()
    client_caller = caller_from_client_name(_client_name)
    if env_caller:
        if client_caller and client_caller != env_caller:
            log.warning(
                "MCP caller detection conflict: clientInfo=%s env=%s; using env marker",
                client_caller,
                env_caller,
            )
        return env_caller
    return client_caller


def _register_shell_adapters(registry: object) -> None:
    """Register shell-specific legacy adapters for adapter-aware routing.
    
    Registers all built-in and Phase 7+ company-priority adapters:
    - Phase 6: Codex, Cursor, Junie
    - Phase 8: Aider, Amazon Q/Kiro
    
    All adapters are exposed via the shared MCP routing surface using the
    ProviderRegistry list_adapters() method, which converts available BUILTIN_PROVIDERS
    to ProviderAdapter objects. Per Phase 7 D-11: both registry/MCP path and thin
    entry-point pattern are supported.
    """
    if getattr(registry, "_shell_adapters_registered", False):
        return
    register = getattr(registry, "register_adapter", None)
    if not callable(register):
        return
    from copilot.providers_legacy import adapter_from_legacy as copilot_adapter
    from gemini.providers_legacy import adapter_from_legacy as gemini_adapter
    from codex.providers_legacy import adapter_from_legacy as codex_adapter_from_legacy
    from junie.providers_legacy import adapter_from_legacy as junie_adapter_from_legacy
    from opencode.providers_legacy import adapter_from_legacy as opencode_adapter_from_legacy
    from cursor.providers_legacy import adapter_from_legacy as cursor_adapter_from_legacy

    register(copilot_adapter())
    register(gemini_adapter())
    try:
        claude_adapter = importlib.import_module(
            "claude-code.providers_legacy"
        ).adapter_from_legacy
        register(claude_adapter())
    except ModuleNotFoundError:
        log.debug("Claude legacy adapter unavailable", exc_info=True)
    
    # Phase 7 company-priority adapters (per D-11 both registry/MCP path and thin entry pattern)
    try:
        register(codex_adapter_from_legacy())
    except Exception as e:
        log.warning("Failed to register Codex adapter: %s", e)
    
    try:
        register(junie_adapter_from_legacy())
    except Exception as e:
        log.warning("Failed to register Junie adapter: %s", e)

    try:
        register(opencode_adapter_from_legacy())
    except Exception as e:
        log.warning("Failed to register OpenCode adapter: %s", e)


    try:
        register(cursor_adapter_from_legacy())
    except Exception as e:
        log.warning("Failed to register Cursor adapter: %s", e)
    
    # Phase 8 secondary adapters (Aider, Amazon Q/Kiro)
    # These are discoverable via BUILTIN_PROVIDERS; thin directories with providers_legacy.py
    # are optional. If available, register thin adapters; otherwise adapters are auto-generated
    # from registry.list_adapters() using BUILTIN_PROVIDERS detection results.
    try:
        aider_adapter = importlib.import_module(
            "aider.providers_legacy"
        ).adapter_from_legacy
        register(aider_adapter())
    except (ModuleNotFoundError, AttributeError):
        log.debug("Aider thin adapter directory not available; using BUILTIN_PROVIDERS")
    
    try:
        amazon_q_adapter = importlib.import_module(
            "amazon_q.providers_legacy"
        ).adapter_from_legacy
        register(amazon_q_adapter())
    except (ModuleNotFoundError, AttributeError):
        log.debug("Amazon Q thin adapter directory not available; using BUILTIN_PROVIDERS")
    
    setattr(registry, "_shell_adapters_registered", True)


def _provider_aliases_for_runtime(adapter: object, provider: object) -> set[str]:
    aliases: set[str] = set()

    adapter_name = _normalize_runtime_provider_key(getattr(adapter, "name", None))
    if adapter_name:
        aliases.add(adapter_name)

    metadata = getattr(adapter, "metadata", None)
    if isinstance(metadata, Mapping):
        shell_names = metadata.get("shell_names", [])
        if isinstance(shell_names, list):
            for alias in shell_names:
                normalized = _normalize_runtime_provider_key(alias)
                if normalized:
                    aliases.add(normalized)

    for attr_name in ("provider_id", "name"):
        normalized = _normalize_runtime_provider_key(getattr(provider, attr_name, None))
        if normalized:
            aliases.add(normalized)

    provider_info = getattr(provider, "provider_info", None)
    if callable(provider_info):
        try:
            info = provider_info()
        except Exception:
            log.debug("provider_info lookup failed while building runtime map", exc_info=True)
        else:
            if isinstance(info, Mapping):
                normalized = _normalize_runtime_provider_key(info.get("primary"))
                if normalized:
                    aliases.add(normalized)

    return aliases


def _build_runtime_providers_map(registry: object) -> dict[str, Provider]:
    list_adapters = getattr(registry, "list_adapters_supporting", None)
    if not callable(list_adapters):
        return {}

    providers_map: dict[str, Provider] = {}
    for adapter in list_adapters(ProviderCapability.EXECUTE):
        try:
            provider = adapter.invoke("build_provider")
        except Exception:
            log.debug(
                "Skipping runtime provider adapter %r because build_provider failed",
                getattr(adapter, "name", "<unknown>"),
                exc_info=True,
            )
            continue
        if provider is None:
            continue
        if not isinstance(provider, Provider):
            log.debug(
                "Skipping runtime provider adapter %r because build_provider returned incompatible provider %r",
                getattr(adapter, "name", "<unknown>"),
                type(provider).__name__,
            )
            continue
        for alias in _provider_aliases_for_runtime(adapter, provider):
            existing = providers_map.get(alias)
            if existing is not None and existing is not provider:
                log.warning("Skipping duplicate runtime provider alias %r", alias)
                continue
            providers_map[alias] = provider
    return providers_map


def _build_runtime_spillover_support(
    config: TGsConfig,
    db: Any | None = None,
) -> tuple[ProviderRegistry, dict[str, Provider]]:
    config_overrides = dataclasses.asdict(config)
    # Pre-warm the shared registry singleton with the already-loaded config so that
    # nested get_registry() calls inside provider_info() (e.g. CopilotProvider) hit
    # the cache and don't trigger a redundant TGsConfig.from_yaml() load.
    get_registry(config_overrides=config_overrides)
    runtime_registry = ProviderRegistry(config_overrides=config_overrides, db=db)
    _register_shell_adapters(runtime_registry)

    providers_map = _build_runtime_providers_map(runtime_registry)
    if providers_map:
        runtime_registry.available_providers = [
            provider
            for provider in runtime_registry.available_providers
            if (
                _normalize_runtime_provider_key(getattr(provider, "name", None)) in providers_map
                or _normalize_runtime_provider_key(getattr(provider, "display_name", None)) in providers_map
            )
        ]

    return runtime_registry, providers_map


def _normalize_provenance(raw: object, caller: str | None) -> dict[str, str | int]:
    """Build a stable provenance envelope for routed execution."""
    provenance = raw if isinstance(raw, Mapping) else {}
    try:
        parent_depth = int(provenance.get("depth", 0))
    except (TypeError, ValueError):
        parent_depth = 0
    return {
        "trace_id": str(provenance.get("trace_id") or uuid.uuid4()),
        "depth": parent_depth + 1,
        "caller_id": str(provenance.get("caller_id") or caller or "mcp"),
    }


# ---------------------------------------------------------------------------
# Code extraction for target_file writes
# ---------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_OUTER_CODE_FENCE_RE = re.compile(r"^```(?:\w+)?\r?\n(.*?)\r?\n?```$", re.DOTALL)
_HEREDOC_LINE_RE = re.compile(r"^\s*[│┃]\s")
_HEREDOC_CMD_RE = re.compile(r"^\s*[│┃]?\s*cat\s+>.*<<")
_BOXED_LINE_RE = re.compile(r"^\s*[│┃](?:\s.*)?$")
_PREVIEW_EXCERPT_LINES = 12
_ACTIVE_WORKSPACE_ENV = "TGS_ACTIVE_WORKSPACE"

# Extensions that are text/doc content (not source code).
# These receive a different prompt and a relaxed extraction path.
_TEXT_DOC_EXTS: frozenset[str] = frozenset({
    ".md", ".markdown", ".mdx",
    ".txt", ".text",
    ".rst",
    ".adoc", ".asciidoc",
    ".org",
    ".tex",
    ".log",
})
# Bare filenames (no extension) that are also text/doc targets.
_TEXT_DOC_NAMES: frozenset[str] = frozenset({
    "readme", "changelog", "changes", "history",
    "license", "licence", "notice", "authors", "contributors",
    "todo", "notes",
})
# First line of model output that signals pure agent reasoning rather than
# usable file content.
_REASONING_ONLY_RE = re.compile(
    r"^(i (will|would|can|cannot|don'?t)\b|let me\b|"
    r"sure[,!]?\s|sorry[,!]?\s|unfortunately\b)",
    re.IGNORECASE,
)
_CONTENT_WRAPPER_RE = re.compile(
    r"^(below|here)( is|'s)? the "
    r"(readme|file|document|notice|license|authors?|contributors?|"
    r"changelog|history|notes?|todo)( content)?( for [^:]+)?\s*:\s*$",
    re.IGNORECASE,
)
_REASONING_META_RE = re.compile(
    r"\b(explain|walk through|what i('ll| will) do|for you|step by step|"
    r"outline|the following|"
    r"(write|draft|generate) the (readme|file|document|content|notice))\b",
    re.IGNORECASE,
)

# First-line-of-code patterns by file extension
_CODE_START: dict[str, re.Pattern] = {
    ".py":   re.compile(r"^(from\s+\S|import\s+\S|#!/|class\s+\S|def\s+\S|@\S|\"\"\"|\'\'\'|\w+\s*[=(])"),  # +assignment/call
    ".js":   re.compile(r"^(import\s|const\s|let\s|var\s|function[\s(]|//|/\*|export\s|module\.|'use )"),
    ".ts":   re.compile(r"^(import\s|const\s|let\s|var\s|function[\s(]|//|/\*|export\s|interface\s|type\s|'use )"),
    ".jsx":  re.compile(r"^(import\s|const\s|let\s|var\s|function[\s(]|//|/\*|export\s|'use )"),
    ".tsx":  re.compile(r"^(import\s|const\s|let\s|var\s|function[\s(]|//|/\*|export\s|interface\s|type\s|'use )"),
    ".go":   re.compile(r"^(package\s|//|/\*|import\s)"),
    ".rs":   re.compile(r"^(use\s|//|/\*|pub\s|fn\s|mod\s|extern\s|#\[)"),
    ".java": re.compile(r"^(package\s|import\s|//|/\*|public\s|class\s|@)"),
    ".kt":   re.compile(r"^(package\s|import\s|//|/\*|fun\s|class\s|object\s|@)"),
    ".kts":  re.compile(r"^(package\s|import\s|//|/\*|fun\s|class\s|@)"),
    ".swift":re.compile(r"^(import\s|//|/\*|class\s|struct\s|enum\s|func\s|let\s|var\s|@)"),
    ".c":    re.compile(r"^(#include|#define|#ifdef|#ifndef|//|/\*|static\s|extern\s|int\s|void\s|char\s|struct\s)"),
    ".h":    re.compile(r"^(#include|#define|#ifdef|#ifndef|#pragma|//|/\*|typedef\s|struct\s|extern\s)"),
    ".cpp":  re.compile(r"^(#include|#define|#ifdef|#ifndef|//|/\*|namespace\s|using\s|class\s|template\s|int\s|void\s|auto\s)"),
    ".cc":   re.compile(r"^(#include|#define|//|/\*|namespace\s|using\s|class\s|template\s)"),
    ".hpp":  re.compile(r"^(#include|#pragma|#ifndef|//|/\*|namespace\s|template\s|class\s)"),
    ".cs":   re.compile(r"^(using\s|namespace\s|//|/\*|public\s|class\s|internal\s|@)"),
    ".php":  re.compile(r"^(<\?php|<\?=|//|/\*|#|namespace\s|use\s|class\s|function\s)"),
    ".rb":   re.compile(r"^(require\s|#|class\s|module\s|def\s)"),
    ".lua":  re.compile(r"^(require\s|--|local\s|function\s|return\s|if\s)"),
    ".dart": re.compile(r"^(import\s|//|/\*|class\s|void\s|final\s|var\s|@)"),
    ".scala":re.compile(r"^(package\s|import\s|//|/\*|object\s|class\s|def\s|val\s|@)"),
    ".ex":   re.compile(r"^(defmodule\s|import\s|alias\s|use\s|#|@)"),
    ".exs":  re.compile(r"^(defmodule\s|import\s|alias\s|use\s|#|@)"),
    ".sh":   re.compile(r"^(#!/|#\s|set\s)"),
    ".bash": re.compile(r"^(#!/|#\s|set\s)"),
    ".zsh":  re.compile(r"^(#!/|#\s|set\s)"),
    ".fish": re.compile(r"^(#!/|#|function\s|set\s)"),
    ".yaml": re.compile(r"^(\w+:|---|\s*-\s+\w)"),
    ".yml":  re.compile(r"^(\w+:|---|\s*-\s+\w)"),
    ".json": re.compile(r"^\s*[\[{]"),
    ".toml": re.compile(r"^(\[|#|\w+\s*=)"),
    ".xml":  re.compile(r"^(<\?xml|<!--|<[a-zA-Z])", re.IGNORECASE),
    ".html": re.compile(r"^(<|<!DOCTYPE)", re.IGNORECASE),
    ".css":  re.compile(r"^(/\*|@|\.|\#|\*|body|html|:root)"),
    ".scss": re.compile(r"^(/\*|//|@|\$|\.|\#|\*|body|html|:root)"),
    ".sql":  re.compile(r"^(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|WITH|--)", re.IGNORECASE),
    ".tf":   re.compile(r"^(resource\s|variable\s|output\s|module\s|provider\s|data\s|#|//)"),
    ".proto":re.compile(r"^(syntax\s|package\s|import\s|message\s|service\s|//|/\*)"),
}
_CODE_START_GENERIC = re.compile(
    r"^(from\s+\S|import\s+\S|#!/|class\s|def\s|fn\s|fun\s|func\s|@\S|"
    r"//|/\*|#include|#define|#ifndef|#ifdef|#pragma|"
    r"const\s|let\s|var\s|val\s|function[\s(]|export\s|"
    r"package\s|namespace\s|use\s|using\s|pub\s|"
    r"SELECT\b|INSERT\b|CREATE\b|resource\s|syntax\s)",
    re.IGNORECASE,
)


def _extract_code_for_file(raw: str, target_path: str) -> str | None:
    """Strip model preamble/artifacts from output before writing to a file.

    Models (especially gpt-5-mini via ``gh copilot``) often prepend
    explanation prose and/or │-prefixed heredoc previews before the actual
    code.  This function aggressively strips that when we know the output
    must be pure source code.

    Returns *None* when no recognisable code is found (the model produced
    only reasoning / error text).  The caller should treat this as a
    subtask failure rather than writing prose to disk.
    """
    if not raw or not raw.strip():
        return None

    # 1. Code fences always win — extract last fence body
    fences = _CODE_FENCE_RE.findall(raw)
    if fences:
        return fences[-1].strip()

    # 2. Remove │-prefixed heredoc preview lines and cat > ... << lines
    lines = raw.split("\n")
    cleaned: list[str] = []
    for line in lines:
        if _HEREDOC_LINE_RE.match(line) or _HEREDOC_CMD_RE.match(line):
            continue
        cleaned.append(line)

    # 3. Find the first line that matches a code-start pattern
    ext = Path(target_path).suffix.lower()
    pattern = _CODE_START.get(ext, _CODE_START_GENERIC)

    for i, line in enumerate(cleaned):
        stripped = line.strip()
        if stripped and pattern.match(stripped):
            return "\n".join(cleaned[i:]).rstrip()

    # 4. Unknown extension with no registered pattern — return stripped raw
    #    rather than silently dropping the file, so any language works.
    if ext not in _CODE_START:
        stripped_cleaned = "\n".join(cleaned).strip()
        if stripped_cleaned and not _REASONING_ONLY_RE.match(stripped_cleaned.split("\n")[0]):
            return stripped_cleaned
    return None


def _is_text_doc_target(path: str) -> bool:
    """Return True when *path* is a text/doc target that must NOT go through
    the strict code-extraction path.

    Extensions in ``_TEXT_DOC_EXTS`` and bare filenames in
    ``_TEXT_DOC_NAMES`` are treated as prose/document content; everything
    else (source, config, data formats already in ``_CODE_START``) keeps the
    existing strict code flow.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext in _TEXT_DOC_EXTS:
        return True
    if not ext and p.name.lower() in _TEXT_DOC_NAMES:
        return True
    return False


def _extract_text_for_file(raw: str, _target_path: str) -> str | None:
    """Strip model preamble/artifacts from output before writing a text/doc file.

    Unlike :func:`_extract_code_for_file` this function does *not* require a
    code-start line.  Instead it:

    1. Accepts output that is already plain prose/markdown.
    2. Unwraps a single outer code fence if the model wrapped the whole
       document in one (common for markdown targets).
    3. Strips │-prefixed shell preview lines.
    4. Returns ``None`` when the output appears to be pure agent reasoning
       with no structural content — this prevents writing explanation prose
       to disk when the model failed to produce the actual file.
    """
    if not raw or not raw.strip():
        return None

    def unwrap_outer_fence(value: str) -> str:
        outer_fence = _OUTER_CODE_FENCE_RE.match(value.strip())
        if outer_fence:
            return outer_fence.group(1).strip()
        return value.strip()

    def parse_heredoc_command(line: str) -> tuple[str, str] | None:
        stripped = line.strip()
        if stripped.startswith(("│", "┃")):
            stripped = stripped[1:].strip()
        match = re.match(
            r'^cat\s+>\s+("([^"]+)"|\'([^\']+)\'|(\S+))\s+<<[\'"]?([^\'"]+)[\'"]?$',
            stripped,
        )
        if not match:
            return None
        target = match.group(2) or match.group(3) or match.group(4) or ""
        delimiter = match.group(5)
        return target, delimiter

    text = unwrap_outer_fence(raw)
    raw_lines = text.split("\n")
    non_empty_lines = [line.strip() for line in raw_lines if line.strip()]
    target_name = Path(_target_path).name
    if non_empty_lines:
        parsed_heredoc = parse_heredoc_command(non_empty_lines[0])
        first_non_empty_raw = next((line for line in raw_lines if line.strip()), "")
        last_non_empty_raw = next((line for line in reversed(raw_lines) if line.strip()), "")
        if (
            parsed_heredoc
            and Path(parsed_heredoc[0]).name == target_name
            and _HEREDOC_LINE_RE.match(first_non_empty_raw) is None
            and last_non_empty_raw.strip() == parsed_heredoc[1]
        ):
            delimiter = parsed_heredoc[1]
            delimiter_indexes = [
                index for index, line in enumerate(raw_lines[1:], start=1)
                if line.strip() == delimiter
            ]
            if delimiter_indexes:
                text = "\n".join(raw_lines[1:delimiter_indexes[-1]]).strip()
            if not text:
                return None
            raw_lines = text.split("\n")

    first_non_empty_index = next((i for i, line in enumerate(raw_lines) if line.strip()), None)
    has_boxed_heredoc_preview = (
        first_non_empty_index is not None
        and _HEREDOC_LINE_RE.match(raw_lines[first_non_empty_index]) is not None
        and (
            parsed := parse_heredoc_command(raw_lines[first_non_empty_index])
        ) is not None
        and Path(parsed[0]).name == target_name
    )
    if has_boxed_heredoc_preview:
        delimiter = parsed[1]
        non_empty_boxed = [line for line in raw_lines if line.strip()]
        boxed_wrapper_only = (
            non_empty_boxed
            and all(_BOXED_LINE_RE.match(line) for line in non_empty_boxed)
            and non_empty_boxed[-1].lstrip(" │┃") == delimiter
        )
        if boxed_wrapper_only:
            body_lines: list[str] = []
            for line in non_empty_boxed[1:]:
                stripped = re.sub(r"^\s*[│┃]\s?", "", line, count=1)
                if stripped == delimiter:
                    break
                body_lines.append(stripped)
            text = "\n".join(body_lines).strip()
        else:
            preview_end = 0
            while preview_end < len(raw_lines) and (
                not raw_lines[preview_end].strip()
                or _HEREDOC_LINE_RE.match(raw_lines[preview_end])
            ):
                preview_end += 1
            text = "\n".join(raw_lines[preview_end:]).strip()
        if not text:
            return None

    # 2. Reject reasoning-only output: first real line looks like agent prose,
    #    the body contains meta-explanation cues, and there are no structural
    #    markers anywhere in the text.
    structure_re = re.compile(r"^(?:[#*\->`|]|\d+\.)")
    raw_lines = text.split("\n")
    real_lines = [l.strip() for l in raw_lines if l.strip()]
    first_line = real_lines[0] if real_lines else ""
    if _CONTENT_WRAPPER_RE.match(first_line):
        for index, line in enumerate(raw_lines[1:], start=1):
            if line.strip():
                text = unwrap_outer_fence("\n".join(raw_lines[index:]))
                break
        else:
            return None
        raw_lines = text.split("\n")
        real_lines = [l.strip() for l in raw_lines if l.strip()]
        first_line = real_lines[0] if real_lines else ""
    if _REASONING_ONLY_RE.match(first_line):
        if (
            re.match(r"^(sorry[,!]?\s|unfortunately\b)", first_line, re.IGNORECASE)
            and len(real_lines) <= 2
        ):
            return None
        # Accept anyway if any line carries real document structure
        # (heading, list item, table row, blockquote, code fence, …)
        has_structure = any(
            structure_re.match(l)
            for l in real_lines
        )
        has_meta_reasoning = _REASONING_META_RE.search(text) is not None
        if has_meta_reasoning:
            if has_structure:
                for index, line in enumerate(raw_lines):
                    if structure_re.match(line.strip()):
                        text = unwrap_outer_fence("\n".join(raw_lines[index:]))
                        break
            else:
                for index, line in enumerate(raw_lines[1:], start=1):
                    if line.strip():
                        text = unwrap_outer_fence("\n".join(raw_lines[index:]))
                        break
                else:
                    return None
                candidate_lines = [l.strip() for l in text.split("\n") if l.strip()]
                candidate_first = candidate_lines[0] if candidate_lines else ""
                candidate_has_structure = any(structure_re.match(line) for line in candidate_lines)
                if candidate_first and (
                    (_REASONING_ONLY_RE.match(candidate_first) and _REASONING_META_RE.search(text))
                    or (_REASONING_META_RE.search(text) and not candidate_has_structure)
                ):
                    return None

    return text


def _active_workspace_root() -> Path:
    """Return the pre-approved workspace root for direct writes."""
    override = os.environ.get(_ACTIVE_WORKSPACE_ENV)
    if override:
        candidate = Path(override).expanduser().resolve()
        home = Path.home().resolve()
        if (
            candidate.exists()
            and candidate.is_dir()
            and candidate != Path(candidate.anchor)
            and candidate != home
            and candidate != home.parent
        ):
            return candidate
        log.warning(
            "Ignoring unsafe %s override outside a project-like boundary: %s",
            _ACTIVE_WORKSPACE_ENV,
            candidate,
        )
    return Path.cwd().resolve()


def validate_target_path(
    target_str: str,
    allowed_bases: list[Path] | None = None,
) -> Path:
    """Validate target_file path against allowlist of trusted bases.

    Wave 3: FNDX-04 - Path traversal protection guard.
    Ensures all model-generated file writes are constrained to project root.

    Args:
        target_str: Raw path string from task/model output
        allowed_bases: List of trusted base directories. Default: project root (cwd).

    Returns:
        Validated Path object if path is inside allowed bases.

    Raises:
        ValueError: if path is outside all allowed bases or cannot be resolved.

    Implementation:
        - Use Path.expanduser().resolve() to get canonical form
        - Use Path.relative_to() to check against allowed bases
        - Reject paths that escape allowed bases with clear error
        - Log path traversal attempts for audit trail
    """
    if not target_str or not target_str.strip():
        raise ValueError("target_file path cannot be empty")

    # Expand ~ and environment variables, resolve to canonical path
    target = Path(target_str).expanduser().resolve(strict=False)

    # Default allowlist: current working directory (project root)
    if allowed_bases is None:
        allowed_bases = [Path.cwd().resolve()]

    # Check if target is under any allowed base
    for allowed_base in allowed_bases:
        allowed_base_resolved = allowed_base.resolve()
        try:
            # Try to compute relative path — if succeeds, target is under base
            target.relative_to(allowed_base_resolved)
            # Success — path is under allowed base
            log.debug("Path write validation passed: %s", target)
            return target
        except ValueError:
            # Not under this base, try next
            continue

    # No allowed base matched — path traversal attempt
    log.warning("Path write rejected: %s outside allowed bases", target_str)
    raise ValueError(
        f"Path {target} is outside allowed write bases: {allowed_bases}. "
        f"Use 'apply_preview' tool to request approval for out-of-root writes."
    )


def _ensure_write_tables(db: Database) -> None:
    # Preview/audit tables now live in the shared DB schema.
    return None


def _attach_models_to_subtasks(result: dict) -> dict:
    """Ensure cached and fresh plan responses expose per-subtask model fields.

    Shows which provider+model execute_subtask will actually pick, including
    free-tier preference and the safe self-hosted Copilot exception for
    sandboxed low-tier code generation.
    """
    subtasks = result.get("subtasks")
    if not isinstance(subtasks, list):
        return result

    try:
        registry = _get_registry_with_config()
    except Exception:
        registry = None

    caller = _resolve_caller()
    provider_obj = CopilotProvider()
    selection_cache: dict[tuple[str, bool, str | None], dict[str, object] | None] = {}
    for st in subtasks:
        if not isinstance(st, dict):
            continue
        tier = st.get("tier")
        if not isinstance(tier, str):
            continue
        raw_model = _normalize_route_text(st.get("model"))
        explicit_model = raw_model
        explicit_provider = _normalize_route_text(st.get("provider"))
        explicit_provider_id = _normalize_route_text(st.get("provider_id"))
        placeholder_model = _is_placeholder_route_model(explicit_model)
        if placeholder_model:
            explicit_model = None
        has_explicit_route = any(
            value is not None
            for value in (explicit_model, explicit_provider, explicit_provider_id)
        )
        for key in (
            "is_free",
            "billing_tier",
            "provider_cost_hint",
            "cost_rank",
            "billing_source",
            "effort",
            "effort_source",
        ):
            st.pop(key, None)
        selection = None
        if registry is not None and (
            not has_explicit_route
            or explicit_model is None
            or explicit_provider is None
            or explicit_provider_id is None
        ):
            code_only = tier == "low" and _subtask_likely_writes_files(st)
            cache_key = (tier, code_only, caller)
            if cache_key not in selection_cache:
                selection_cache[cache_key] = _select_provider_metadata(
                    registry,
                    tier,
                    caller=caller,
                    code_only=code_only,
                    config=_config,
                    caller_allowlists=getattr(_config, "caller_provider_allowlists", None) or None,
                )
            selection = selection_cache[cache_key]
        selected_model = (
            _normalize_route_text(selection.get("model"))
            if isinstance(selection, dict)
            else None
        )
        if _is_placeholder_route_model(selected_model):
            selected_model = None
        selected_provider = (
            _normalize_route_text(selection.get("provider"))
            if isinstance(selection, dict)
            else None
        )
        selected_provider_id = (
            _normalize_route_text(selection.get("provider_id"))
            if isinstance(selection, dict)
            else None
        )
        route_matches_selection = (
            not has_explicit_route
            or (
                (explicit_model is None or explicit_model == selected_model)
                and (explicit_provider is None or explicit_provider == selected_provider)
                and (
                    explicit_provider_id is None
                    or explicit_provider_id == selected_provider_id
                )
            )
        )
        if explicit_model is not None:
            st["model"] = explicit_model
        elif route_matches_selection and selected_model is not None:
            st["model"] = selected_model
        elif has_explicit_route:
            st["model"] = raw_model or tier
        else:
            st["model"] = provider_obj.resolve_model(tier)
        if explicit_provider is not None:
            st["provider"] = explicit_provider
        elif route_matches_selection and selected_provider is not None:
            st["provider"] = selected_provider
        else:
            st.pop("provider", None)
        if explicit_provider_id is not None:
            st["provider_id"] = explicit_provider_id
        elif route_matches_selection and selected_provider_id is not None:
            st["provider_id"] = selected_provider_id
        else:
            st.pop("provider_id", None)
        if route_matches_selection and isinstance(selection, Mapping):
            st.update({
                key: value
                for key, value in selection.items()
                if (
                    key in _ROUTE_RESPONSE_SELECTION_KEYS
                    and key not in {"model", "provider", "provider_id"}
                    and not callable(value)
                    and isinstance(value, (str, int, float, bool))
                )
            })
    return result


def _attach_plan_routing_to_fleet_waves(
    plan_payload: dict,
    fleet_waves: object | None = None,
) -> list[dict[str, object]]:
    subtasks = plan_payload.get("subtasks")
    waves = plan_payload.get("waves")
    if not isinstance(subtasks, list) or not isinstance(waves, list):
        return []
    existing_fleet_waves = fleet_waves if isinstance(fleet_waves, list) else []

    subtask_by_id: dict[object, dict[str, object]] = {}
    for st in subtasks:
        if isinstance(st, dict):
            try:
                subtask_by_id[st.get("id")] = st
            except TypeError:
                continue

    normalized_waves: list[dict[str, object]] = []
    for index, wave_ids in enumerate(waves, start=1):
        existing_wave = existing_fleet_waves[index - 1] if index - 1 < len(existing_fleet_waves) and isinstance(existing_fleet_waves[index - 1], dict) else {}
        if not isinstance(wave_ids, list):
            normalized_waves.append(dict(existing_wave) if existing_wave else {
                "wave_number": index,
                "parallel": False,
                "command": "/fleet (no agents)",
                "agents": [],
            })
            continue
        agents: list[dict[str, object]] = []
        for sid in wave_ids:
            try:
                st = subtask_by_id.get(sid)
            except TypeError:
                continue
            if not isinstance(st, dict):
                continue
            tier = str(st.get("tier") or "")
            model = str(st.get("model") or "")
            agent: dict[str, object] = {
                "tier": tier,
                "model": model,
                "prompt": _format_fleet_prompt(st),
            }
            provider = _normalize_route_text(st.get("provider"))
            provider_id = _normalize_route_text(st.get("provider_id"))
            if provider is not None:
                agent["provider"] = provider
            if provider_id is not None:
                agent["provider_id"] = provider_id
            agents.append(agent)

        quoted = " ".join(
            _quote_fleet_command_prompt(str(agent.get("prompt", "")))
            for agent in agents
        )
        normalized_wave = dict(existing_wave)
        normalized_wave.update({
            "wave_number": existing_wave.get("wave_number", index),
            "parallel": len(agents) > 1,
            "command": f"/fleet {quoted}" if quoted else "/fleet (no agents)",
            "agents": agents,
        })
        normalized_waves.append(normalized_wave)
    return normalized_waves


def _log_write_audit(
    db: Database,
    *,
    requested_path: Path,
    caller: str | None,
    outcome: str,
    preview_token: str | None = None,
    details: str | None = None,
) -> None:
    preview_token_ref = _write_preview_token_ref(preview_token)
    _ensure_write_tables(db)
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO write_audit "
            "(preview_token, requested_path, caller, outcome, details, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                preview_token_ref,
                str(requested_path),
                caller,
                outcome,
                details,
                time.time(),
            ),
        )


def emit_concise_agent_notification(project_id: str, agent_id: str, event: str) -> dict[str, object]:
    _config, db, *_ = _ensure_init()
    normalized_event = event.strip().lower()
    if normalized_event not in {"generated", "merged", "promoted"}:
        normalized_event = "generated"

    with db.conn() as conn:
        row = conn.execute(
            "SELECT id FROM agent_audit WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
            (agent_id,),
        ).fetchone()

    audit_id = int(row[0]) if row else None
    payload = {
        "project_id": project_id,
        "agent_id": agent_id,
        "event": normalized_event,
        "audit_id": audit_id,
        "message": f"agent {normalized_event}: {agent_id}",
    }

    if _client_name is not None:
        send_notification("agent_notification", payload)
    else:
        log.info("agent notification: %s", payload.get("message", ""))
    return payload


def _store_preview_record(
    db: Database,
    *,
    preview_token: str,
    requested_path: Path,
    content: str,
    caller: str | None,
) -> None:
    preview_token_ref = _write_preview_token_ref(preview_token)
    _ensure_write_tables(db)
    with db.conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO preview_records "
            "(preview_token, requested_path, content, caller, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                preview_token_ref,
                str(requested_path),
                content,
                caller,
                time.time(),
            ),
        )


def _write_preview_token_ref(preview_token: str | None) -> str | None:
    normalized_preview_token = str(preview_token or "").strip()
    if not normalized_preview_token:
        return None
    return hashlib.sha256(normalized_preview_token.encode("utf-8")).hexdigest()


def _preview_excerpt(content: str) -> str:
    lines = content.splitlines()
    excerpt = "\n".join(lines[:_PREVIEW_EXCERPT_LINES])
    if len(lines) > _PREVIEW_EXCERPT_LINES:
        excerpt += "\n..."
    return excerpt


def _write_file_with_audit(
    db: Database,
    *,
    requested_path: Path,
    content: str,
    caller: str | None,
    outcome: str,
    preview_token: str | None = None,
    idempotency_key: str | None = None,
    policy: object = None,
) -> dict[str, object]:
    # Policy path check (plan 12)
    if policy is not None:
        try:
            from shared.policy import evaluate
            verdict = evaluate(policy, 'file_write', str(requested_path))
            if verdict.denied:
                raise PermissionError(
                    f"Policy denied write to {requested_path}: {verdict.reason}"
                )
        except ImportError:
            pass
    if idempotency_key is not None:
        cached = db.get_file_write("file_writes", idempotency_key)
        if cached is not None:
            return {
                "file_written": cached.get("target_path", str(requested_path)),
                "lines_written": cached.get("lines_written") or 0,
                "idempotent_replay": True,
            }
    for candidate in (requested_path, *requested_path.parents):
        if candidate.is_symlink():
            raise OSError(f"Refusing to write through symlink path: {candidate}")

    requested_path.parent.mkdir(parents=True, exist_ok=True)
    # Use O_NOFOLLOW on the final component to close the TOCTOU race window
    # between the symlink check loop above and the actual open syscall.
    _oflags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        _oflags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(requested_path), _oflags, 0o666)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OSError(f"Refusing to write through symlink path: {requested_path}") from exc
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())

    lines_written = content.count("\n") + 1
    _log_write_audit(
        db,
        requested_path=requested_path,
        caller=caller,
        outcome=outcome,
        preview_token=preview_token,
    )
    if idempotency_key is not None:
        try:
            db.record_file_write(
                "file_writes",
                idempotency_key,
                str(requested_path),
                lines_written,
            )
        except Exception:
            log.debug("record_file_write failed for key %s", idempotency_key, exc_info=True)
    return {
        "file_written": str(requested_path),
        "lines_written": lines_written,
    }


def apply_preview(preview_token: str, approve: bool) -> dict:
    if not preview_token:
        return {"error": "MissingPreviewToken", "details": "preview_token is required"}

    preview_token_ref = _write_preview_token_ref(preview_token)
    if preview_token_ref is None:
        return {"error": "MissingPreviewToken", "details": "preview_token is required"}

    _config, db, *_ = _ensure_init()
    _ensure_write_tables(db)
    with db.conn() as conn:
        row = conn.execute(
            "SELECT requested_path, content, caller FROM preview_records "
            "WHERE preview_token = ?",
            (preview_token_ref,),
        ).fetchone()

    if row is None:
        return {
            "error": "PreviewNotFound",
            "details": "No pending preview for the supplied preview_token",
        }

    with db.conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "DELETE FROM preview_records "
            "WHERE preview_token = ? "
            "RETURNING requested_path, content, caller",
            (preview_token_ref,),
        ).fetchone()
        if row is None:
            return {
                "error": "PreviewNotFound",
                "details": "No pending preview for the supplied preview_token",
            }

    requested_path = Path(row[0])
    content = row[1]
    caller = row[2]

    # Restrict writes to the pre-approved workspace root to prevent path traversal.
    workspace_root = _active_workspace_root()
    try:
        requested_path.resolve().relative_to(workspace_root)
    except (OSError, ValueError):
        _store_preview_record(
            db,
            preview_token=preview_token,
            requested_path=requested_path,
            content=content,
            caller=caller,
        )
        _log_write_audit(
            db,
            requested_path=requested_path,
            caller=caller,
            outcome="denied-path-traversal",
            preview_token=preview_token,
        )
        return {
            "error": "PathTraversal",
            "details": f"Requested path is outside the workspace root {workspace_root}",
        }

    if not approve:
        with db.conn() as conn:
            conn.execute(
                "DELETE FROM preview_records WHERE preview_token = ?",
                (preview_token_ref,),
            )
        _log_write_audit(
            db,
            requested_path=requested_path,
            caller=caller,
            outcome="denied",
            preview_token=preview_token,
        )
        return {
            "approved": False,
            "preview_token": preview_token,
            "requested_path": str(requested_path),
        }

    try:
        write_result = _write_file_with_audit(
            db,
            requested_path=requested_path,
            content=content,
            caller=caller,
            outcome="approved",
            preview_token=preview_token,
        )
    except OSError as exc:
        _store_preview_record(
            db,
            preview_token=preview_token,
            requested_path=requested_path,
            content=content,
            caller=caller,
        )
        log.warning("apply_preview: failed to write %s", requested_path, exc_info=True)
        _log_write_audit(
            db,
            requested_path=requested_path,
            caller=caller,
            outcome="write-failed",
            preview_token=preview_token,
            details=str(exc),
        )
        return {
            "error": "WriteError",
            "details": str(exc),
            "preview_token": preview_token,
            "requested_path": str(requested_path),
        }

    return {
        "approved": True,
        "preview_token": preview_token,
        "requested_path": str(requested_path),
        **write_result,
    }


def handle_apply_preview(args: dict) -> dict:
    preview_token = args.get("preview_token", "")
    approve = bool(args.get("approve", False))
    return apply_preview(preview_token, approve)


def _runtime_subtask_snapshot(task_id: str) -> dict[str, object] | None:
    with _subtasks_lock:
        active_entry = _active_subtasks.get(task_id)
        if active_entry is not None:
            return {
                key: value
                for key, value in active_entry.items()
                if key != "start_mono"
            }
        for entry in reversed(_subtask_history):
            if entry.get("task_id") == task_id:
                return {
                    key: value
                    for key, value in entry.items()
                    if key != "start_mono"
                }
    return None


def inspect_task(task_id: str) -> dict[str, object]:
    if not task_id:
        return {"error": "MissingTaskId", "details": "task_id is required"}

    _config, db, *_ = _ensure_init()
    # Fetch core telemetry fields including Phase-15 explainability columns and parse_diagnostics
    with db.conn() as conn:
        rows = conn.execute(
            "SELECT agent_id, provider_name, model, tier, tokens_used, "
            "used_fallback, used_speculation, success, reason, provenance_trace_id, "
            "provenance_depth, provenance_caller_id, provider_opt_out_reason, "
            "parse_diagnostics, urgency_score, selected_topology, fanout_final_action, "
            "artifact_publish_count, artifact_consume_count, coordinator_amendment_count, ts "
            "FROM telemetry WHERE task_hash = ? "
            "ORDER BY agent_id, ts",
            (task_id,),
        ).fetchall()

    events = []
    for row in rows:
        (
            agent_id,
            provider_name,
            model,
            tier,
            tokens_used,
            used_fallback,
            used_speculation,
            success,
            reason,
            prov_trace,
            prov_depth,
            prov_caller,
            provider_opt_out_reason,
            parse_diag,
            urgency_score,
            selected_topology,
            fanout_final_action,
            artifact_publish_count,
            artifact_consume_count,
            coordinator_amendment_count,
            ts,
        ) = row

        # Try to parse parse_diagnostics as JSON for explainability extras
        explain_extras = None
        truncated = False
        if isinstance(parse_diag, str) and parse_diag:
            try:
                explain_extras = json.loads(parse_diag)
            except json.JSONDecodeError:
                # keep a short preview as fallback
                explain_extras = None
                truncated = len(parse_diag) > 200
                parse_preview = parse_diag[:200]
        else:
            parse_preview = None

        event = {
            "subtask_id": agent_id,
            "provider": provider_name,
            "model": model,
            "tier": tier,
            "tokens_used": tokens_used,
            "used_fallback": bool(used_fallback),
            "used_speculation": bool(used_speculation),
            "success": bool(success),
            "reason": reason,
            "provenance": {
                "trace_id": prov_trace,
                "depth": prov_depth,
                "caller_id": prov_caller,
            },
            "provider_opt_out_reason": provider_opt_out_reason,
            "timestamp": ts,
            # Phase-15 explainability fields (kept nested under explainability)
            "explainability": {
                "urgency_score": urgency_score,
                "selected_topology": selected_topology,
                "fanout_final_action": fanout_final_action,
                "artifact_publish_count": int(artifact_publish_count) if artifact_publish_count is not None else 0,
                "artifact_consume_count": int(artifact_consume_count) if artifact_consume_count is not None else 0,
                "coordinator_amendment_count": int(coordinator_amendment_count) if coordinator_amendment_count is not None else 0,
            },
        }
        try:
            quota_provider = str(provider_name or "").strip().lower()
            quota_observation = (
                db.get_latest_provider_quota_observation(quota_provider)
                if quota_provider
                else None
            )
            if quota_observation is not None:
                event["quota"] = quota_observation
                event["quota_rationale"] = {
                    "basis": "latest_persisted_observation",
                    "source": quota_observation.get("source"),
                    "status": quota_observation.get("status"),
                    "note": (
                        "Route-time quota rationale is returned directly by routing results; "
                        "this task inspection shows the latest persisted provider observation."
                    ),
                }
        except Exception:
            pass

        # Attach parsed extras if available without altering top-level keys
        _explain_block = event.get("explainability")
        if _explain_block is not None:
            if explain_extras is not None:
                _explain_block["extras"] = explain_extras
            elif parse_preview is not None:
                _explain_block["extras_preview"] = parse_preview
                if truncated:
                    _explain_block["truncated"] = True

        events.append(event)

    latest_by_subtask: dict[str | int, dict] = {}
    for event in events:
        latest_by_subtask[event.get("subtask_id")] = event

    runtime = _runtime_subtask_snapshot(task_id)
    if runtime is not None:
        for event in events:
            for key in ("status", "target_file", "wave_id", "effort", "effort_source", "op_class"):
                if key in runtime and runtime[key] is not None:
                    event.setdefault(key, runtime[key])

    # Provider failure events for this task
    failure_events: list[dict] = []
    try:
        task_hash = task_id
        with db.conn() as conn:
            fail_rows = conn.execute(
                "SELECT payload, created_ts FROM swarm_events"
                " WHERE event_type='provider_failure'"
                " AND json_extract(payload, '$.task_hash') = ?"
                " ORDER BY created_ts DESC LIMIT 20",
                (task_hash,),
            ).fetchall()
        for fr in fail_rows:
            try:
                payload = json.loads(fr[0]) if isinstance(fr[0], str) else (fr[0] or {})
            except Exception:
                payload = {}
            payload["ts"] = fr[1]
            # Attach resilience columns from telemetry if available
            failure_events.append(payload)
    except Exception:
        pass

    response: dict[str, object] = {
        "task_id": task_id,
        "subtasks": list(latest_by_subtask.values()),
        "events": events,
        "failure_events": failure_events,
    }
    if runtime is not None:
        response["runtime"] = runtime
    return response


def handle_inspect_task(args: dict) -> dict:
    return inspect_task(args.get("task_id", ""))


def _resolve_resume_swarm_plan_revision(
    raw_plan_revision: object,
) -> tuple[int | None, dict[str, str] | None]:
    if raw_plan_revision is None:
        return None, None
    try:
        normalized_plan_revision = int(raw_plan_revision)
    except (TypeError, ValueError):
        return None, {
            "error": "invalid_request",
            "details": "plan_revision must be an integer when provided",
        }
    if normalized_plan_revision < 1:
        return None, {
            "error": "invalid_request",
            "details": "plan_revision must be >= 1",
        }
    return normalized_plan_revision, None


def _resolve_resume_swarm_operator_id(
    raw_operator_id: object,
) -> tuple[str | None, dict[str, str] | None]:
    if raw_operator_id is None:
        return None, None
    if not isinstance(raw_operator_id, str):
        return None, {
            "error": "invalid_request",
            "details": "operator_id must be a string when provided",
        }
    if not raw_operator_id.strip():
        return None, None
    return None, {
        "error": "invalid_request",
        "details": "operator_id cannot be asserted by this tool; omit it to resume anonymously",
    }


def resume_swarm_inspect(
    failed_swarm_id: str,
    *,
    plan_revision: int | None = None,
) -> dict[str, object]:
    """List compact checkpoints for one failed swarm per D-06 and D-07."""
    normalized_swarm_id = _normalize_swarm_id(failed_swarm_id)
    _config, db, *_ = _ensure_init()
    checkpoints = list_resume_checkpoints(
        normalized_swarm_id,
        plan_revision=plan_revision,
        db=db,
    )
    return {
        "failed_swarm_id": normalized_swarm_id,
        "checkpoints": checkpoints,
        "checkpoint_count": len(checkpoints),
    }


def handle_resume_swarm_inspect(args: dict) -> dict:
    plan_revision, plan_revision_error = _resolve_resume_swarm_plan_revision(
        args.get("plan_revision")
    )
    if plan_revision_error is not None:
        return plan_revision_error
    try:
        return resume_swarm_inspect(
            str(args.get("failed_swarm_id") or ""),
            plan_revision=plan_revision,
        )
    except ValueError as exc:
        return {"error": "invalid_request", "details": str(exc)}


def _spawn_resume_swarm_runtime_handoff(
    db: Database,
    new_swarm_id: str,
    parent_lineage: Mapping[str, object],
) -> None:
    """Spawn a background thread to execute a resumed swarm via runtime handoff.
    
    Traceability: D-05, D-06, D-07
    - Start resumed execution immediately after checkpoint confirmation succeeds
    - Preserve parent/checkpoint lineage in execution context
    - Persist final state with lineage so inspection and recovery work correctly
    """
    try:
        _, _, _, _, orchestrator = _ensure_init()
    except Exception:
        log.warning("Failed to get orchestrator for resumed swarm %s background execution", new_swarm_id)
        return
    
    # Extract progress token from thread-local MCP context if available
    progress_token = getattr(_request_context, "progress_token", None)
    
    # Spawn background thread to run the resume handoff (D-05, D-06, D-07)
    try:
        thread = threading.Thread(
            target=_resume_swarm_runtime_handoff,
            args=(db, orchestrator, new_swarm_id, parent_lineage, progress_token),
            daemon=True,
        )
        thread.start()
    except Exception:
        log.warning("Failed to spawn resume runtime handoff thread for swarm %s", new_swarm_id)


def resume_swarm_confirm(
    failed_swarm_id: str,
    checkpoint_index: int,
    *,
    plan_revision: int | None = None,
    operator_id: str | None = None,
) -> dict[str, object]:
    normalized_swarm_id = _normalize_swarm_id(failed_swarm_id)
    del operator_id  # Reserved for authenticated/internal resume flows.
    try:
        normalized_checkpoint_index = int(checkpoint_index)
    except (TypeError, ValueError) as exc:
        raise ValueError("checkpoint_index must be an integer") from exc
    if normalized_checkpoint_index < 1:
        raise ValueError("checkpoint_index must be >= 1")

    _config, db, *_ = _ensure_init()
    if plan_revision is None:
        matching_revisions = {
            int(item.get("plan_revision") or 0)
            for item in db.list_coordinator_round_checkpoints(normalized_swarm_id)
            if int(item.get("round_index") or 0) == normalized_checkpoint_index
        }
        if len(matching_revisions) > 1:
            return {
                "error": "invalid_request",
                "details": "checkpoint_index is ambiguous across plan revisions; provide plan_revision from resume_swarm_inspect",
            }
    checkpoint = get_coordinator_round_checkpoint_by_index(
        normalized_swarm_id,
        normalized_checkpoint_index,
        plan_revision=plan_revision,
        db=db,
    )
    if checkpoint is None:
        return {
            "error": "invalid_request",
            "details": "checkpoint_index was not found for failed_swarm_id",
        }
    execution_context = _resolve_swarm_execution_context(db, normalized_swarm_id)
    if execution_context is None:
        return {
            "error": "invalid_request",
            "details": "resume source task is unavailable for failed_swarm_id",
        }
    chosen_plan_revision = int(checkpoint.get("plan_revision") or 0)
    new_swarm_id = seed_resume_from_checkpoint(
        checkpoint,
        db=db,
    )
    memory_refresh_swarm_state_from_db(new_swarm_id, db=db)
    summary = db.get_swarm_summary(new_swarm_id)
    if summary is None:
        return {
            "error": "execution_error",
            "details": "resume_swarm failed to persist resumed swarm state",
        }
    requested = int(summary.get("requested_agents") or 0)
    effective = int(summary.get("effective_agents") or 0)
    
    # Build lineage for the resumed swarm (D-05, D-06, D-07)
    parent_lineage: Mapping[str, object] = {
        "parent_swarm_id": normalized_swarm_id,
        "chosen_checkpoint_index": normalized_checkpoint_index,
        "plan_revision": chosen_plan_revision,
        "execution_context": execution_context,
    }
    
    # Spawn background runtime handoff to start resumed execution immediately (D-05, D-07)
    _spawn_resume_swarm_runtime_handoff(db, new_swarm_id, parent_lineage)
    
    return {
        "result": {
            "swarm_id": new_swarm_id,
            "requested_vs_effective_agent_count": {
                "requested": requested,
                "effective": effective,
            },
            "adjusted": requested != effective,
            "wave_summary": [
                {
                    "wave": 1,
                    "count": effective,
                    "label": "resume-workers",
                }
            ],
            "requested_values": {
                "checkpoint_index": normalized_checkpoint_index,
                "plan_revision": chosen_plan_revision,
            },
            "effective_values": {
                "topology": summary.get("topology"),
                "max_agents": effective,
            },
            "lineage": {
                "parent_swarm_id": normalized_swarm_id,
                "chosen_checkpoint_index": normalized_checkpoint_index,
                "plan_revision": chosen_plan_revision,
            },
            "resume": True,
        },
        "started": True,
    }


def handle_resume_swarm_confirm(args: dict) -> dict:
    plan_revision, plan_revision_error = _resolve_resume_swarm_plan_revision(
        args.get("plan_revision")
    )
    if plan_revision_error is not None:
        return plan_revision_error
    operator_id, operator_error = _resolve_resume_swarm_operator_id(
        args.get("operator_id")
    )
    if operator_error is not None:
        return operator_error
    try:
        return resume_swarm_confirm(
            str(args.get("failed_swarm_id") or ""),
            args.get("checkpoint_index"),
            plan_revision=plan_revision,
            operator_id=operator_id,
        )
    except ValueError as exc:
        return {"error": "invalid_request", "details": str(exc)}


TUNE_KEY_ALIASES = {
    "learning": "learning_enabled",
    "learning_enable": "learning_enabled",
    "learning_enabled": "learning_enabled",
    "concurrency": "concurrency_limit",
    "concurrency_limit": "concurrency_limit",
    "budget": "budget_hard_cap_tokens",
    "budget_cap": "budget_hard_cap_tokens",
    "budget_hard_cap_tokens": "budget_hard_cap_tokens",
    "fanout": "fanout_cap",
    "fanout_cap": "fanout_cap",
    "pending_approval_limit": "pending_approval_limit",
    "allow_out_of_workspace_writes": "allow_out_of_workspace_writes",
    "allow_out_of_workspace": "allow_out_of_workspace_writes",
    "out_of_workspace_writes": "allow_out_of_workspace_writes",
}
TUNE_HARD_CAPS = {
    "budget_hard_cap_tokens": 10_000,
    "pending_approval_limit": 10,
}
NEGATIVE_ONE_ALLOWED_TUNE_KEYS = {
    "concurrency_limit",
    "fanout_cap",
}
ZERO_ALLOWED_TUNE_KEYS = {
    "fanout_cap",
    "pending_approval_limit",
}


def _normalize_project_id(project_id: str) -> str:
    normalized_project_id = (project_id or "").strip()
    if not normalized_project_id:
        return ""

    workspace_root = _active_workspace_root()
    candidate = normalize_target_path(normalized_project_id, workspace_root)
    if not is_within_repo(candidate, workspace_root):
        raise ValueError("project_path must resolve inside the active workspace")
    return str(candidate)


def _require_project_id(project_id: str) -> str:
    normalized_project_id = _normalize_project_id(project_id)
    if not normalized_project_id:
        raise ValueError("project_id is required")
    return normalized_project_id


def _normalize_tune_key(key: str) -> str:
    canonical = TUNE_KEY_ALIASES.get((key or "").strip().lower())
    if canonical is None or canonical not in PROJECT_SETTING_KEYS:
        raise ValueError(f"unsupported tune key: {key}")
    return canonical


def _parse_tune_value(key: str, raw_value: object) -> int | bool:
    raw_text = "" if raw_value is None else str(raw_value).strip()
    if key in {"learning_enabled", "allow_out_of_workspace_writes"}:
        lowered = raw_text.lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
        raise ValueError(f"{key} must be one of true/false/on/off/1/0")

    try:
        value = int(raw_text)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if key in NEGATIVE_ONE_ALLOWED_TUNE_KEYS and value == -1:
        return value
    minimum = 0 if key in ZERO_ALLOWED_TUNE_KEYS else 1
    if value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return value


def approval_queue_list(project_id: str, limit: int = 25) -> list[dict]:
    _config, db, *_ = _ensure_init()
    normalized_project_id = _require_project_id(project_id)
    return list_approval_queue_items(
        normalized_project_id,
        limit=limit,
        db=db,
    )


def agent_queue_list(project_id: str, limit: int = 25) -> list[dict]:
    return approval_queue_list(project_id, limit=limit)


def _require_approval_operator_id(operator_id: object) -> dict | None:
    if not isinstance(operator_id, str) or not operator_id.strip():
        return {
            "error": "ApprovalActionError",
            "details": "operator_id is required",
        }
    return None


def approval_queue_approve(project_id: str, queue_id: int, operator_id: str) -> dict:
    validation_error = _require_approval_operator_id(operator_id)
    if validation_error is not None:
        return validation_error
    _config, db, *_ = _ensure_init()
    normalized_project_id = _require_project_id(project_id)
    try:
        # Call existing approval logic
        result = approve_queue_item(
            normalized_project_id,
            int(queue_id),
            operator_id=operator_id,
            db=db,
        )
        
        # If approval succeeded, attempt registration to compatible CLIs (non-blocking)
        if result.get("approved"):
            agent_id = result.get("agent_id")
            if agent_id:
                try:
                    provider_registry = ProviderRegistry()
                    reg_results = register_agent_to_capable_clis(db, agent_id, provider_registry)
                    
                    # Include registration results in response (non-blocking - don't fail if registration fails)
                    result["registration_results"] = reg_results
                    
                    if reg_results.get("failed_targets"):
                        log.warning(
                            f"Agent {agent_id} approved but registration failed for "
                            f"{len(reg_results.get('failed_targets', []))} targets"
                        )
                except Exception as e:
                    log.warning(f"Registration attempt failed for agent {agent_id}: {e}", exc_info=True)
                    # Non-blocking: don't fail approval if registration fails
                    result["registration_error"] = str(e)
        
        return result
    except (ValueError, TypeError) as exc:
        return {"error": "ApprovalActionError", "details": str(exc)}


def agent_queue_approve(project_id: str, queue_id: int, operator_id: str) -> dict:
    return approval_queue_approve(project_id, queue_id, operator_id)


def approval_queue_reject(
    project_id: str,
    queue_id: int,
    operator_id: str,
    *,
    reason: str = "deferred",
) -> dict:
    validation_error = _require_approval_operator_id(operator_id)
    if validation_error is not None:
        return validation_error
    _config, db, *_ = _ensure_init()
    normalized_project_id = _require_project_id(project_id)
    try:
        return reject_queue_item(
            normalized_project_id,
            int(queue_id),
            operator_id=operator_id,
            reason=reason,
            db=db,
        )
    except (ValueError, TypeError) as exc:
        return {"error": "ApprovalActionError", "details": str(exc)}


def agent_queue_reject(
    project_id: str,
    queue_id: int,
    operator_id: str,
    *,
    reason: str = "deferred",
) -> dict:
    return approval_queue_reject(project_id, queue_id, operator_id, reason=reason)


def approval_queue_merge(
    project_id: str,
    queue_id: int,
    canonical_agent_id: str,
    operator_id: str,
    *,
    reason: str = "operator-merge",
) -> dict:
    validation_error = _require_approval_operator_id(operator_id)
    if validation_error is not None:
        return validation_error
    _config, db, *_ = _ensure_init()
    normalized_project_id = _require_project_id(project_id)
    try:
        return merge_queue_item(
            normalized_project_id,
            int(queue_id),
            canonical_agent_id,
            operator_id=operator_id,
            reason=reason,
            db=db,
        )
    except (ValueError, TypeError) as exc:
        return {"error": "ApprovalActionError", "details": str(exc)}


def agent_queue_merge(
    project_id: str,
    queue_id: int,
    canonical_agent_id: str,
    operator_id: str,
    *,
    reason: str = "operator-merge",
) -> dict:
    return approval_queue_merge(
        project_id,
        queue_id,
        canonical_agent_id,
        operator_id,
        reason=reason,
    )


def _memory_error_response(exc: Exception) -> dict:
    if isinstance(exc, MemoryNotFoundError):
        return {"error": "not_found", "details": str(exc)}
    return {"error": "invalid_request", "details": str(exc)}


def _optional_memory_string_arg(args: Mapping[str, Any], key: str) -> str | None:
    raw_value = args.get(key)
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise MemoryRequestError(f"{key} must be a string")
    normalized = raw_value.strip()
    return normalized or None


def _required_memory_string_arg(args: Mapping[str, Any], key: str) -> str:
    raw_value = args.get(key)
    if not isinstance(raw_value, str):
        raise MemoryRequestError(f"{key} must be a string")
    return raw_value


def _normalize_memory_request(
    args: Mapping[str, Any],
    *,
    require_key: bool = False,
) -> dict[str, Any]:
    raw_scope = args.get("scope")
    if not isinstance(raw_scope, str):
        raise MemoryRequestError("scope must be a string")

    scope = raw_scope.strip().lower()
    project_id = _optional_memory_string_arg(args, "project_id")
    if project_id is not None:
        try:
            project_id = _normalize_project_id(project_id)
        except ValueError as exc:
            raise MemoryRequestError(str(exc)) from exc
    task_id = _optional_memory_string_arg(args, "task_id")

    normalized: dict[str, Any] = {
        "scope": scope,
        "project_id": project_id,
        "task_id": task_id,
    }
    if require_key:
        normalized["key"] = _required_memory_string_arg(args, "key")
    return normalized


def handle_memory_list(args: dict) -> dict | list[dict[str, Any]]:
    try:
        _config, db, *_ = _ensure_init()
    except Exception:
        return {"error": "database unavailable — route_task still works", "code": "DB_UNAVAILABLE"}
    try:
        normalized = _normalize_memory_request(args)
        return memory_list(
            normalized.get("scope"),
            project_id=normalized.get("project_id"),
            task_id=normalized.get("task_id"),
            db=db,
        )
    except (MemoryNotFoundError, MemoryRequestError) as exc:
        return _memory_error_response(exc)


def handle_memory_get(args: dict) -> dict:
    try:
        _config, db, *_ = _ensure_init()
    except Exception:
        return {"error": "database unavailable — route_task still works", "code": "DB_UNAVAILABLE"}
    try:
        normalized = _normalize_memory_request(args, require_key=True)
        return memory_get(
            normalized.get("scope"),
            normalized.get("key"),
            project_id=normalized.get("project_id"),
            task_id=normalized.get("task_id"),
            db=db,
        )
    except (MemoryNotFoundError, MemoryRequestError) as exc:
        return _memory_error_response(exc)


def handle_memory_set(args: dict) -> dict:
    try:
        if "value" not in args:
            raise MemoryRequestError("value is required")
        _config, db, *_ = _ensure_init()
    except MemoryRequestError as exc:
        return _memory_error_response(exc)
    except Exception:
        return {"error": "database unavailable — route_task still works", "code": "DB_UNAVAILABLE"}
    try:
        normalized = _normalize_memory_request(args, require_key=True)
        return memory_set(
            normalized.get("scope"),
            normalized.get("key"),
            args.get("value"),
            project_id=normalized.get("project_id"),
            task_id=normalized.get("task_id"),
            db=db,
        )
    except (MemoryNotFoundError, MemoryRequestError) as exc:
        return _memory_error_response(exc)


def handle_memory_delete(args: dict) -> dict:
    try:
        _config, db, *_ = _ensure_init()
    except Exception:
        return {"error": "database unavailable — route_task still works", "code": "DB_UNAVAILABLE"}
    try:
        normalized = _normalize_memory_request(args, require_key=True)
        return memory_delete(
            normalized.get("scope", ""),
            normalized.get("key", ""),
            project_id=normalized.get("project_id"),
            task_id=normalized.get("task_id"),
            db=db,
        )
    except (MemoryNotFoundError, MemoryRequestError) as exc:
        return _memory_error_response(exc)


def _resolve_record_outcome_operator_id(raw_operator_id: object) -> tuple[str, dict | None]:
    if raw_operator_id is None:
        return shared_outcomes.ANONYMOUS_OPERATOR_ID, None
    if not isinstance(raw_operator_id, str):
        return "", {
            "error": "invalid_request",
            "details": "operator_id must be a string when provided",
        }

    normalized_operator_id = raw_operator_id.strip()
    if not normalized_operator_id:
        return shared_outcomes.ANONYMOUS_OPERATOR_ID, None
    return "", {
        "error": "invalid_request",
        "details": "operator_id cannot be asserted by this tool; omit it to record anonymous feedback",
    }


def handle_record_outcome(args: dict) -> dict:
    raw_task_id = args.get("task_id")
    if not isinstance(raw_task_id, str) or not raw_task_id.strip():
        return {"error": "invalid_request", "details": "task_id is required"}

    raw_outcome = args.get("outcome")
    if not isinstance(raw_outcome, str) or not raw_outcome.strip():
        return {"error": "invalid_request", "details": "outcome is required"}

    normalized_outcome = raw_outcome.strip().lower()
    if normalized_outcome not in shared_outcomes.OUTCOME_ALLOWLIST:
        return {
            "error": "invalid_outcome",
            "allowed": list(shared_outcomes.OUTCOME_VALUES),
        }

    operator_id, operator_error = _resolve_record_outcome_operator_id(args.get("operator_id"))
    if operator_error is not None:
        return operator_error

    note = args.get("note")
    if note is not None and not isinstance(note, str):
        return {
            "error": "invalid_request",
            "details": "note must be a string when provided",
        }

    try:
        _config, db, *_ = _ensure_init()
        return shared_outcomes.record_outcome(
            db,
            raw_task_id,
            normalized_outcome,
            operator_id=operator_id,
            note=note,
            project_id=str(_active_workspace_root()),
        )
    except shared_outcomes.OutcomeReadonlyWindowError as exc:
        return {"error": "readonly_window_expired", "details": str(exc)}
    except ValueError as exc:
        return {"error": "invalid_request", "details": str(exc)}


def tune_show(project_id: str, key: str | None = None) -> dict:
    _config, db, *_ = _ensure_init()
    try:
        normalized_project_id = _require_project_id(project_id)
    except ValueError as exc:
        return {"error": "InvalidProjectPath", "details": str(exc)}

    settings = db.get_project_settings(normalized_project_id)
    if key is None:
        return {
            "project_id": normalized_project_id,
            "settings": settings,
        }

    try:
        canonical_key = _normalize_tune_key(key)
    except ValueError as exc:
        return {"error": "InvalidTuneKey", "details": str(exc)}
    return {
        "project_id": normalized_project_id,
        "key": canonical_key,
        "value": settings[canonical_key],
        "settings": settings,
    }


def tune_set(project_id: str, key: str, value: object, *, force: bool = False) -> dict:
    _config, db, *_ = _ensure_init()
    try:
        normalized_project_id = _require_project_id(project_id)
    except ValueError as exc:
        return {"error": "InvalidProjectPath", "details": str(exc)}

    try:
        canonical_key = _normalize_tune_key(key)
        parsed_value = _parse_tune_value(canonical_key, value)
    except ValueError as exc:
        return {"error": "InvalidTuneValue", "details": str(exc)}

    warning: str | None = None
    hard_cap = TUNE_HARD_CAPS.get(canonical_key)
    if isinstance(parsed_value, int) and hard_cap is not None and parsed_value > hard_cap:
        warning = (
            f"{canonical_key}={parsed_value} exceeds the hard cap {hard_cap}"
        )
        if not force:
            current = db.get_project_settings(normalized_project_id)
            return {
                "updated": False,
                "project_id": normalized_project_id,
                "key": canonical_key,
                "requested_value": parsed_value,
                "effective_value": current[canonical_key],
                "hard_cap": hard_cap,
                "requires_force": True,
                "warning": f"{warning}; rerun with force to store the capped value",
                "settings": current,
            }
        parsed_value = hard_cap
        warning = f"{warning}; stored capped value {hard_cap}"

    settings = db.set_project_setting(
        normalized_project_id,
        canonical_key,
        parsed_value,
    )
    return {
        "updated": True,
        "project_id": normalized_project_id,
        "key": canonical_key,
        "value": settings[canonical_key],
        "warning": warning,
        "settings": settings,
    }


def tune_reset(project_id: str, key: str | None = None) -> dict:
    _config, db, *_ = _ensure_init()
    try:
        normalized_project_id = _require_project_id(project_id)
    except ValueError as exc:
        return {"error": "InvalidProjectPath", "details": str(exc)}

    canonical_key: str | None = None
    if key is not None:
        try:
            canonical_key = _normalize_tune_key(key)
        except ValueError as exc:
            return {"error": "InvalidTuneKey", "details": str(exc)}

    settings = db.reset_project_setting(normalized_project_id, canonical_key)
    return {
        "reset": True,
        "project_id": normalized_project_id,
        "key": canonical_key,
        "settings": settings,
    }


def inspect_status(project_id: str = "") -> dict:
    _config, db, *_ = _ensure_init()
    try:
        normalized_project_id = _normalize_project_id(project_id)
    except ValueError as exc:
        return {"error": "InvalidProjectPath", "details": str(exc)}
    return build_status_snapshot(_config, db, normalized_project_id)


def handle_inspect_status(args: dict) -> dict:
    return inspect_status(args.get("project_id", ""))


def handle_approval_queue_list(args: dict) -> dict:
    try:
        _limit_raw = args.get("limit", 25)
        _limit = int(_limit_raw)
    except (TypeError, ValueError):
        _limit = 25
    try:
        project_id = _normalize_project_id(args.get("project_id", ""))
        items = approval_queue_list(project_id, limit=_limit)
    except (TypeError, ValueError) as exc:
        return {"error": "InvalidProjectPath", "details": str(exc)}
    return {
        "project_id": project_id,
        "items": items,
        "count": len(items),
    }


def handle_approval_queue_approve(args: dict) -> dict:
    return approval_queue_approve(
        args.get("project_id", ""),
        args.get("queue_id", 0),
        args.get("operator_id", ""),
    )


def handle_approval_queue_reject(args: dict) -> dict:
    return approval_queue_reject(
        args.get("project_id", ""),
        args.get("queue_id", 0),
        args.get("operator_id", ""),
        reason=args.get("reason", "deferred"),
    )


def handle_approval_queue_merge(args: dict) -> dict:
    return approval_queue_merge(
        args.get("project_id", ""),
        args.get("queue_id", 0),
        args.get("canonical_agent_id", ""),
        args.get("operator_id", ""),
        reason=args.get("reason", "operator-merge"),
    )


def handle_tune_show(args: dict) -> dict:
    return tune_show(args.get("project_id", ""), args.get("key"))


def handle_tune_set(args: dict) -> dict:
    return tune_set(
        args.get("project_id", ""),
        args.get("key", ""),
        args.get("value", ""),
        force=bool(args.get("force", False)),
    )


def handle_tune_reset(args: dict) -> dict:
    return tune_reset(args.get("project_id", ""), args.get("key"))


# ------------------------------------------------------------------
# Routing exceptions handlers
# ------------------------------------------------------------------

_VALID_EXCEPTION_TYPES_MCP = frozenset({
    "skill", "filetype", "project", "command", "caller", "path",
})


def handle_routing_exception_add(args: dict) -> dict:
    _config, db, *_ = _ensure_init()
    exc_type = (args.get("exception_type") or "").strip().lower()
    pattern = (args.get("pattern") or "").strip()
    note = args.get("note")
    if exc_type not in _VALID_EXCEPTION_TYPES_MCP:
        return {
            "error": "InvalidExceptionType",
            "details": (
                f"exception_type must be one of: {', '.join(sorted(_VALID_EXCEPTION_TYPES_MCP))}"
            ),
        }
    if not pattern:
        return {"error": "MissingPattern", "details": "pattern must not be empty"}
    if exc_type == "filetype":
        _normalized_ft = pattern.lower().strip()
        if _normalized_ft in DEFAULT_ROUTING_EXCEPTION_FILETYPES:
            return {
                "already_in_builtins": True,
                "tip": (
                    f"'{pattern}' is already a built-in exempt filetype — "
                    "write to it directly without calling routing_exception_add."
                ),
            }
    elif exc_type == "path":
        _path_basename = os.path.basename(pattern.strip())
        if _path_basename in DEFAULT_ROUTING_EXCEPTION_PATHS or pattern.strip() in DEFAULT_ROUTING_EXCEPTION_PATHS:
            return {
                "already_in_builtins": True,
                "tip": (
                    f"'{pattern}' is already a built-in exempt path — "
                    "write to it directly without calling routing_exception_add."
                ),
            }
    try:
        row = db.routing_exception_add(exc_type, pattern, note)
    except ValueError as exc:
        return {"error": "InvalidInput", "details": str(exc)}
    return {"added": True, "exception": row}


def handle_routing_exception_remove(args: dict) -> dict:
    _config, db, *_ = _ensure_init()
    exc_type = (args.get("exception_type") or "").strip().lower()
    pattern = (args.get("pattern") or "").strip()
    if not exc_type or not pattern:
        return {"error": "MissingInput", "details": "exception_type and pattern are required"}
    removed = db.routing_exception_remove(exc_type, pattern)
    return {"removed": removed, "exception_type": exc_type, "pattern": pattern}


def handle_routing_exception_list(args: dict) -> dict:
    _config, db, *_ = _ensure_init()
    rows = db.routing_exception_list()
    return {"exceptions": rows, "count": len(rows)}


def _compute_file_diff(
    old_content: str | None, new_content: str, filepath: str
) -> dict:
    """Compute a unified diff between old and new file content.

    Returns a dict with change_type, diff (unified diff string),
    and summary stats (lines_added, lines_removed).
    """
    new_lines = new_content.splitlines(keepends=True)
    if old_content is None:
        # New file — show all lines as additions
        diff_lines = list(difflib.unified_diff(
            [], new_lines,
            fromfile="/dev/null",
            tofile=filepath,
            lineterm="",
        ))
        return {
            "change_type": "created",
            "lines_added": len(new_lines),
            "lines_removed": 0,
            "diff": "\n".join(diff_lines) if diff_lines else "",
        }

    old_lines = old_content.splitlines(keepends=True)
    if old_lines == new_lines:
        return {
            "change_type": "unchanged",
            "lines_added": 0,
            "lines_removed": 0,
            "diff": "",
        }

    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filepath}",
        tofile=f"b/{filepath}",
        lineterm="",
    ))
    added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
    return {
        "change_type": "modified",
        "lines_added": added,
        "lines_removed": removed,
        "diff": "\n".join(diff_lines),
    }


# ── ANSI color codes ──────────────────────────────────────────────────
_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[2m"
_ANSI_GREEN = "\033[32m"
_ANSI_RED = "\033[31m"
_ANSI_CYAN = "\033[36m"
_ANSI_YELLOW = "\033[33m"
_terminal_diff_lock = threading.Lock()


def _sanitize_terminal_text(text: str) -> str:
    return "".join(
        ch
        for ch in text
        if (
            ch == "\t"
            or ((0x20 <= ord(ch) < 0x7F) or ord(ch) >= 0xA0)
            and unicodedata.category(ch) != "Cf"
        )
    )


def _print_diff_to_terminal(
    all_diffs: list[dict],
    agent_label: str,
    tier: str,
    provider: str,
) -> None:
    """Print colored diffs to stderr so the developer sees them inline."""
    stderr = sys.stderr
    if stderr is None:
        return
    if not os.environ.get("TGS_SHOW_DIFF_ALWAYS"):
        try:
            if not stderr.isatty():
                return
        except Exception:
            return
    if not all_diffs:
        return

    try:
        with _terminal_diff_lock:
            tier_color = {
                "low": _ANSI_GREEN,
                "medium": _ANSI_YELLOW,
                "high": _ANSI_CYAN,
            }.get(tier, _ANSI_DIM)
            safe_agent_label = _sanitize_terminal_text(agent_label)
            safe_provider = _sanitize_terminal_text(provider)

            stderr.write(
                f"\n{_ANSI_DIM}{'─' * 60}{_ANSI_RESET}\n"
                f"{_ANSI_BOLD}{safe_agent_label}{_ANSI_RESET}  "
                f"{tier_color}[{tier}]{_ANSI_RESET}  "
                f"{_ANSI_DIM}via {safe_provider}{_ANSI_RESET}\n"
            )

            total_added = 0
            total_removed = 0

            for item in all_diffs:
                if not item.get("diff") or item.get("change_type") == "unchanged":
                    continue

                added = int(item.get("lines_added", 0))
                removed = int(item.get("lines_removed", 0))
                path = _sanitize_terminal_text(str(item.get("path", "?")))
                total_added += added
                total_removed += removed

                stderr.write(
                    f"\n{_ANSI_BOLD}{path}{_ANSI_RESET}  "
                    f"{_ANSI_GREEN}+{added}{_ANSI_RESET}  "
                    f"{_ANSI_RED}-{removed}{_ANSI_RESET}\n"
                )

                remaining = 0
                for index, raw_line in enumerate(io.StringIO(str(item.get("diff", ""))), start=1):
                    line = _sanitize_terminal_text(raw_line.rstrip("\n"))
                    if index > 80:
                        remaining += 1
                        continue
                    if line.startswith("+") and not line.startswith("+++"):
                        stderr.write(f"{_ANSI_GREEN}{line}{_ANSI_RESET}\n")
                    elif line.startswith("-") and not line.startswith("---"):
                        stderr.write(f"{_ANSI_RED}{line}{_ANSI_RESET}\n")
                    elif line.startswith("@@"):
                        stderr.write(f"{_ANSI_CYAN}{line}{_ANSI_RESET}\n")
                    elif line.startswith(("+++", "---")):
                        stderr.write(f"{_ANSI_BOLD}{line}{_ANSI_RESET}\n")
                    else:
                        stderr.write(f"{_ANSI_DIM}{line}{_ANSI_RESET}\n")

                if remaining:
                    stderr.write(
                        f"{_ANSI_DIM}  … +{remaining} more lines{_ANSI_RESET}\n"
                    )

            if len(all_diffs) > 1:
                stderr.write(
                    f"\n{_ANSI_DIM}total  "
                    f"{_ANSI_GREEN}+{total_added}{_ANSI_RESET}  "
                    f"{_ANSI_RED}-{total_removed}{_ANSI_RESET}  "
                    f"{_ANSI_DIM}across {len(all_diffs)} file(s){_ANSI_RESET}\n"
                )

            stderr.write(f"{_ANSI_DIM}{'─' * 60}{_ANSI_RESET}\n")
            stderr.flush()
    except Exception:
        return


def _print_dispatch_info(
    tier: str,
    model: str,
    provider: str,
    billing: str,
    caller: str | None = None,
    task_excerpt: str = "",
) -> None:
    """Print a one-line dispatch summary to stderr: tier / model / provider / billing."""
    stderr = sys.stderr
    if stderr is None:
        return
    if not os.environ.get("TGS_SHOW_DIFF_ALWAYS"):
        try:
            if not stderr.isatty():
                return
        except Exception:
            return
    try:
        with _terminal_diff_lock:
            tier_color = {
                "low": _ANSI_GREEN,
                "medium": _ANSI_YELLOW,
                "high": _ANSI_CYAN,
            }.get(tier, _ANSI_DIM)
            billing_color = _ANSI_GREEN if billing == "free" else _ANSI_DIM
            safe_model = _sanitize_terminal_text(model or "?")
            safe_provider = _sanitize_terminal_text(provider or "?")
            safe_billing = _sanitize_terminal_text(billing or "?")
            excerpt = _sanitize_terminal_text(task_excerpt[:60]) if task_excerpt else ""
            caller_part = (
                f"  {_ANSI_DIM}from {_sanitize_terminal_text(caller)}{_ANSI_RESET}"
                if caller else ""
            )
            excerpt_part = (
                f"  {_ANSI_DIM}{excerpt}…{_ANSI_RESET}" if excerpt else ""
            )
            stderr.write(
                f"{_ANSI_DIM}→{_ANSI_RESET} "
                f"{tier_color}[{tier}]{_ANSI_RESET} "
                f"{_ANSI_BOLD}{safe_model}{_ANSI_RESET} "
                f"{_ANSI_DIM}via {safe_provider}{_ANSI_RESET} "
                f"{billing_color}({safe_billing}){_ANSI_RESET}"
                f"{caller_part}{excerpt_part}\n"
            )
            stderr.flush()
    except Exception:
        return


def _approval_gate(
    snapshot: "FileSnapshot",
    all_diffs: list[dict],
    tier: str,
    auto_approve_timeout: int,
) -> bool:
    """Prompt for approval in the current terminal and return True to apply."""
    _ = snapshot, tier
    stdin = sys.stdin
    stderr = sys.stderr
    if os.environ.get("TGS_AUTO_APPROVE") == "1":
        return True
    if stdin is None or stderr is None:
        return True
    try:
        if not stdin.isatty():
            return True
    except Exception:
        return True
    try:
        if not stderr.isatty():
            return True
    except Exception:
        return True
    if not any(diff.get("change_type") != "unchanged" for diff in all_diffs):
        return True

    if auto_approve_timeout > 0:
        stderr.write(
            f"\n{_ANSI_BOLD}Apply these changes?{_ANSI_RESET}  "
            f"[Y/n]  "
            f"{_ANSI_DIM}(auto-yes in {auto_approve_timeout}s, Enter to confirm){_ANSI_RESET} "
        )
    else:
        stderr.write(
            f"\n{_ANSI_BOLD}Apply these changes?{_ANSI_RESET}  "
            f"[Y/n]  "
            f"{_ANSI_DIM}(auto-yes in {_MANUAL_APPROVAL_TIMEOUT_SECONDS}s, Enter to confirm){_ANSI_RESET} "
        )
    stderr.flush()

    try:
        timeout = auto_approve_timeout if auto_approve_timeout > 0 else _MANUAL_APPROVAL_TIMEOUT_SECONDS
        ready, _, _ = select.select([stdin], [], [], timeout)
        if ready:
            answer = stdin.readline().strip().lower()
            approved = answer in ("", "y", "yes")
        else:
            timeout_seconds = auto_approve_timeout if auto_approve_timeout > 0 else _MANUAL_APPROVAL_TIMEOUT_SECONDS
            stderr.write(
                f"\n{_ANSI_DIM}(auto-approved after {timeout_seconds}s){_ANSI_RESET}\n"
            )
            stderr.flush()
            approved = True
    except Exception:
        approved = True

    if not approved:
        stderr.write(f"{_ANSI_RED}✗ reverted{_ANSI_RESET}\n")
        stderr.flush()

    return approved


def handle_inspect_write_audit(args: dict) -> dict:
    """Return recent out-of-workspace write audit entries."""
    _cfg, db, *_ = _ensure_init()
    _raw_limit = args.get("limit") or 50
    try:
        limit = int(_raw_limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))
    entries = db.get_write_audit(limit=limit)
    return {"entries": entries, "count": len(entries), "limit": limit}


def handle_execute_subtask(args: dict) -> dict:
    _config, db, *_ = _ensure_init()
    _ensure_write_tables(db)
    registry = _get_registry_with_config()
    _register_shell_adapters(registry)
    prompt = args.get("prompt")
    if not prompt:
        return {"error": "Missing required parameter: prompt"}

    tier = args.get("tier", "low")
    if tier not in ("low", "medium", "high"):
        return {"error": f"Invalid tier: {tier!r}. Must be low, medium, or high."}

    prefer_free = args.get("prefer_free", True)
    raw_provider_id = args.get("provider_id")
    if raw_provider_id is not None and not isinstance(raw_provider_id, str):
        return {
            "error": "InvalidProvider",
            "details": "provider_id must be a string when provided",
        }
    provider_id = _normalize_route_text(raw_provider_id)
    if raw_provider_id is not None and provider_id is None:
        return {
            "error": "InvalidProvider",
            "details": "provider_id must not be empty when provided",
        }
    tier_default = _config.tier_timeouts.get(tier, 120)
    raw_timeout = args.get("timeout", tier_default)
    try:
        timeout = max(1, min(int(raw_timeout), 600))
    except (TypeError, ValueError):
        return {
            "error": "InvalidTimeout",
            "details": "timeout must be an integer between 1 and 600",
        }
    raw_effort = args.get("effort")
    if raw_effort is not None and not isinstance(raw_effort, str):
        return {
            "error": "InvalidEffort",
            "details": "effort must be a string when provided",
        }
    effort = _normalize_effort_value(raw_effort)
    if raw_effort is not None and effort is None:
        return {
            "error": "InvalidEffort",
            "details": "effort must not be empty when provided",
        }
    mode = args.get("mode", "write")
    if mode not in ("write", "patch", "rewrite", "blocks"):
        return {
            "error": "InvalidMode",
            "details": "mode must be 'write', 'patch', 'rewrite', or 'blocks'",
        }
    target_file = args.get("target_file")
    if target_file is not None and not isinstance(target_file, str):
        return {
            "error": "InvalidTargetPath",
            "details": "target_file must be a string path",
        }
    if isinstance(target_file, str) and not target_file.strip():
        return {
            "error": "InvalidTargetPath",
            "details": "target_file must not be empty",
        }
    task_id = args.get("task_id") or f"execute-{uuid.uuid4().hex}"
    # Stable task_ids (from orchestrator, not auto-generated) anchor idempotency for file writes.
    _file_idem_key: str | None = (
        task_id if (task_id and not task_id.startswith("execute-")) else None
    )
    # Parse convergence_target (plan 14).
    _ct_raw = args.get("convergence_target")
    _ct_min_score: float = 0.8
    _ct_max_rounds: int = 1
    _ct_backoff: float = 0.0
    if isinstance(_ct_raw, dict):
        try:
            _ct_min_score = float(_ct_raw.get("min_score", 0.8))
            _ct_max_rounds = max(1, int(_ct_raw.get("max_rounds", 3)))
            _ct_backoff = float(_ct_raw.get("backoff_seconds", 0.0))
        except (TypeError, ValueError):
            pass
    else:
        _ct_max_rounds = 1  # no convergence target = single execution
    _original_prompt = prompt
    wave_id = args.get("wave_id") or None
    workspace_root = _active_workspace_root()
    normalized_target: Path | None = None
    validated_target: Path | None = None
    allowed_bases = list(_config.write_safety_trusted_bases)
    workspace_root_resolved = Path(workspace_root).resolve()
    cwd_resolved = Path.cwd().resolve()
    if not allowed_bases:
        allowed_bases = [workspace_root_resolved]
    elif len(allowed_bases) == 1 and allowed_bases[0].resolve() == cwd_resolved and workspace_root_resolved != cwd_resolved:
        allowed_bases = [workspace_root_resolved]

    if mode == "write" and target_file is not None:
        _tf_write_check = Path(target_file)
        if not _tf_write_check.is_absolute():
            _tf_write_check = (Path(workspace_root) / _tf_write_check).resolve()
        if _tf_write_check.exists():
            return {
                "error": "ExistingFileWriteMode",
                "details": (
                    f"target_file '{target_file}' already exists. "
                    "mode='write' writes model output verbatim and will corrupt the file "
                    "if the model returns partial content. "
                    "Use mode='rewrite' (full-file, <32KB), mode='blocks' (SEARCH/REPLACE, <128KB), "
                    "or mode='patch' (unified diff) for existing files."
                ),
            }

    # When mode=patch, inject current file content and ask for unified diff.
    if mode == "patch" and target_file is not None:
        try:
            _patch_target = Path(target_file)
            if not _patch_target.is_absolute():
                _patch_target = (Path(workspace_root) / _patch_target).resolve()
            _current_content = _patch_target.read_text(encoding="utf-8")
            fname_patch = _patch_target.name
            prompt = (
                f"Current content of {fname_patch}:\n```\n{_current_content}\n```\n\n"
                f"{prompt}\n\n"
                "Return ONLY a unified diff (--- a/... / +++ b/... / @@ ... @@ lines). "
                "No prose, no fences around the diff."
            )
        except (OSError, UnicodeDecodeError) as _patch_read_exc:
            return {
                "error": "PatchReadError",
                "details": f"Could not read {target_file} for patch mode: {_patch_read_exc}",
            }

    # When mode=rewrite, inject current file content and ask for complete rewrite.
    if mode == "rewrite" and target_file is not None:
        try:
            _rw_target = Path(target_file)
            if not _rw_target.is_absolute():
                _rw_target = (Path(workspace_root) / _rw_target).resolve()
            if _rw_target.exists():
                _rw_size = _rw_target.stat().st_size
                if _rw_size > _config.surgical_edit_max_file_bytes:
                    if not _config.auto_cascade_mode:
                        return {
                            "error": "FileTooLarge",
                            "details": (
                                f"{target_file} is {_rw_size} bytes, exceeding the rewrite "
                                f"limit ({_config.surgical_edit_max_file_bytes} bytes). "
                                "Use mode='blocks' for large files."
                            ),
                        }
                    log.info("execute_subtask: rewrite\u2192blocks cascade (%d bytes)", _rw_size)
                    mode = "blocks"
                elif _rw_size > 0:
                    _rw_current = _rw_target.read_text(encoding="utf-8")
                    fname_rw = _rw_target.name
                    prompt = (
                        f"Current content of {fname_rw}:\n```\n{_rw_current}\n```\n\n"
                        f"{prompt}\n\n"
                        "Return the COMPLETE file content with your changes applied. "
                        "Do NOT return only a fragment or diff — output the entire file."
                    )
        except (OSError, UnicodeDecodeError) as _rw_exc:
            return {
                "error": "RewriteReadError",
                "details": f"Could not read {target_file} for rewrite mode: {_rw_exc}",
            }

    # When mode=blocks, inject current file content and ask for SEARCH/REPLACE blocks.
    if mode == "blocks" and target_file is not None:
        try:
            _blk_target = Path(target_file)
            if not _blk_target.is_absolute():
                _blk_target = (Path(workspace_root) / _blk_target).resolve()
            if _blk_target.exists():
                _blk_size = _blk_target.stat().st_size
                if _blk_size > _config.surgical_edit_blocks_max_file_bytes:
                    if not _config.auto_cascade_mode:
                        return {
                            "error": "FileTooLarge",
                            "details": (
                                f"{target_file} is {_blk_size} bytes, exceeding the blocks "
                                f"limit ({_config.surgical_edit_blocks_max_file_bytes} bytes). "
                                "Split the file or use mode='patch'."
                            ),
                        }
                    log.info("execute_subtask: blocks\u2192patch cascade (%d bytes)", _blk_size)
                    mode = "patch"
                    _blk_current = _blk_target.read_text(encoding="utf-8")
                    fname_blk = _blk_target.name
                    prompt = (
                        f"Current content of {fname_blk}:\n```\n{_blk_current}\n```\n\n"
                        f"{prompt}\n\n"
                        "Return ONLY a unified diff (--- a/... / +++ b/... / @@ ... @@ lines). "
                        "No prose, no fences around the diff."
                    )
                elif _blk_size > 0:
                    _blk_current = _blk_target.read_text(encoding="utf-8")
                    fname_blk = _blk_target.name
                    prompt = (
                        f"Current content of {fname_blk}:\n```\n{_blk_current}\n```\n\n"
                        f"{prompt}\n\n"
                        "Return ONLY SEARCH/REPLACE edit blocks in this exact format — "
                        "no prose, no other fences:\n"
                        "<<<<<<< SEARCH\n"
                        "<exact lines to replace>\n"
                        "=======\n"
                        "<replacement lines>\n"
                        ">>>>>>> REPLACE\n"
                        "Use one block per change. "
                        "SEARCH content must match the file exactly (including whitespace)."
                    )
        except (OSError, UnicodeDecodeError) as _blk_exc:
            return {
                "error": "BlocksReadError",
                "details": f"Could not read {target_file} for blocks mode: {_blk_exc}",
            }

    # When writing to a file, we shape the prompt for the target type.
    # Code targets also enable code_only mode to suppress agentic/tool-heavy
    # provider behaviour. Text/doc targets keep prose-friendly output mode.
    text_doc_target = target_file is not None and _is_text_doc_target(target_file)
    code_only = target_file is not None and not text_doc_target
    if target_file is not None:
        if mode != "patch":
            fname = Path(target_file).name
            if text_doc_target:
                prompt = (
                    f"RULES: Your entire response must be the direct content of "
                    f"{fname}. Output the file contents only — no explanations, "
                    f"no preamble, no outer code fences wrapping the whole "
                    f"document. Start writing the file immediately.\n\n{prompt}"
                )
            else:
                ext = Path(target_file).suffix.lstrip(".")
                lang = {
                    "py": "Python", "js": "JavaScript", "ts": "TypeScript",
                    "jsx": "JavaScript", "tsx": "TypeScript",
                    "go": "Go", "rs": "Rust", "java": "Java", "rb": "Ruby",
                    "kt": "Kotlin", "kts": "Kotlin", "swift": "Swift",
                    "c": "C", "h": "C", "cpp": "C++", "cc": "C++", "hpp": "C++",
                    "cs": "C#", "php": "PHP", "lua": "Lua", "dart": "Dart",
                    "scala": "Scala", "ex": "Elixir", "exs": "Elixir",
                    "sh": "Shell", "bash": "Shell", "zsh": "Shell", "fish": "Shell",
                    "yaml": "YAML", "yml": "YAML", "json": "JSON", "toml": "TOML",
                    "xml": "XML", "html": "HTML", "css": "CSS", "scss": "SCSS",
                    "sql": "SQL", "tf": "Terraform", "proto": "Protocol Buffers",
                }.get(ext, ext or "source")
                prompt = (
                    f"RULES: Your entire response must be valid {lang} source code "
                    f"for {fname}. No prose, no markdown fences, no tool calls. "
                    f"Start with the first line of code.\n\n{prompt}"
                )
        try:
            normalized_target = normalize_target_path(target_file, workspace_root)
        except ValueError as exc:
            log.warning("execute_subtask: invalid target path %s", target_file, exc_info=True)
            return {
                "error": "InvalidTargetPath",
                "details": str(exc),
                "requested_path": target_file,
            }
        if not is_within_repo(normalized_target, workspace_root):
            _oow_grant: str | None = None
            # Layer 1 — config.yaml write_safety.extra_paths (exact prefix)
            for _ep in getattr(_config, "write_safety_extra_paths", []):
                try:
                    normalized_target.relative_to(Path(_ep).expanduser().resolve())
                    _oow_grant = "extra_path"
                    break
                except ValueError:
                    pass
            # Layer 2 — per-call flag
            if _oow_grant is None and args.get("allow_out_of_workspace"):
                _oow_grant = "per_call_flag"
            # Layer 3 — session tune key
            if _oow_grant is None:
                try:
                    if db.get_project_settings(str(workspace_root)).get("allow_out_of_workspace_writes"):
                        _oow_grant = "tune_db_key"
                except Exception:
                    pass
            if _oow_grant is None:
                log.warning("execute_subtask: rejecting out-of-root target %s", target_file)
                return {
                    "error": "PathTraversalRejected",
                    "details": (
                        f"Path {normalized_target} is outside workspace root "
                        f"{Path(workspace_root).resolve()}. "
                        "Use allow_out_of_workspace=true, set write_safety.extra_paths "
                        "in config.yaml, or run 'threnody tune set allow_out_of_workspace_writes true'."
                    ),
                    "requested_path": str(normalized_target),
                }
            allowed_bases.append(normalized_target.parent)
            try:
                db.log_out_of_workspace_write(
                    target_path=str(normalized_target),
                    provider=_resolve_caller() or "unknown",
                    tier=args.get("tier", "low"),
                    grant_reason=_oow_grant,
                )
            except Exception:
                pass
            log.info("execute_subtask: out-of-workspace write granted (%s) for %s",
                     _oow_grant, str(normalized_target))
        try:
            validated_target = validate_target_path(
                str(normalized_target),
                allowed_bases=allowed_bases,
            )
        except ValueError as exc:
            log.warning("execute_subtask: path validation failed for %s", target_file, exc_info=True)
            return {
                "error": "PathTraversalRejected",
                "details": str(exc),
                "requested_path": str(normalized_target),
            }

    caller = args.get("caller") or _resolve_caller()
    provenance = _normalize_provenance(args.get("provenance"), caller)
    provenance_trace_id = str(provenance.get("trace_id", ""))
    provenance_depth = int(provenance.get("depth", 0))
    provenance_caller_id = str(provenance.get("caller_id", ""))
    routing_caller = caller or "mcp"
    caller_allowlists = getattr(_config, "caller_provider_allowlists", None) or None
    if provenance_depth > 2:
        db.log_agent_result(
            session_id=caller or "mcp",
            task_hash=task_id,
            agent_id=0,
            tier=tier,
            model="",
            success=False,
            tokens_used=0,
            provider_name="mcp",
            used_fallback=False,
            used_speculation=False,
            provenance_trace_id=provenance_trace_id,
            provenance_depth=provenance_depth,
            provenance_caller_id=provenance_caller_id,
            reason="recursion_depth_exceeded",
            version="mcp",
        )
        return {
            "error": "RecursionDepthError",
            "details": "provenance depth exceeded limit=2",
            "task_id": task_id,
            "caller_detected": caller,
            "provenance": provenance,
        }

    import time as _time
    t0 = _time.monotonic()
    selection_metadata = _select_provider_metadata(
        registry,
        tier,
        caller=routing_caller,
        code_only=code_only,
        prefer_free=prefer_free,
        effort=effort,
        config=_config,
        caller_allowlists=caller_allowlists,
        provider_id=provider_id,
    ) or {}
    if not _has_executable_routing_metadata(selection_metadata):
        with _subtasks_lock:
            _subtask_history.append({
                "task_id": task_id,
                "prompt_excerpt": (args.get("prompt") or prompt)[:100],
                "tier": tier,
                "target_file": target_file,
                "wave_id": wave_id,
                "status": "failed",
                "started_at": time.strftime("%H:%M:%S"),
                "elapsed": 0.0,
                "model": _normalize_route_text(selection_metadata.get("model")),
                "provider": _normalize_route_text(selection_metadata.get("provider")),
                "provider_id": _normalize_route_text(selection_metadata.get("provider_id")),
                "effort": selection_metadata.get("effort"),
                "effort_source": selection_metadata.get("effort_source"),
            })
            if len(_subtask_history) > 20:
                _subtask_history.pop(0)
            _write_status_file()
        db.log_agent_result(
            session_id=caller or "mcp",
            task_hash=task_id,
            agent_id=0,
            tier=tier,
            model=str(selection_metadata.get("model", "")),
            success=False,
            tokens_used=0,
            provider_name=str(selection_metadata.get("provider", "mcp")),
            used_fallback=False,
            used_speculation=False,
            provenance_trace_id=provenance_trace_id,
            provenance_depth=provenance_depth,
            provenance_caller_id=provenance_caller_id,
            reason="routing_unavailable",
            version="mcp",
        )
        return _routing_unavailable_result(
            task_id=task_id,
            tier=tier,
            caller=caller,
            provenance=provenance,
            selection=selection_metadata,
            details=(
                f"Could not resolve executable routing metadata for tier {tier!r}. "
                "Expected non-empty model and provider before execution."
            ),
        )
    if (
        selection_metadata.get("effort_source") == "explicit"
        and selection_metadata.get("effort") is not None
        and not _provider_supports_explicit_effort(selection_metadata.get("provider_id"))
    ):
        provider_name = str(selection_metadata.get("provider") or selection_metadata.get("provider_id") or "selected provider")
        provider_id = str(selection_metadata.get("provider_id") or "")
        with _subtasks_lock:
            _subtask_history.append({
                "task_id": task_id,
                "prompt_excerpt": (args.get("prompt") or prompt)[:100],
                "tier": tier,
                "target_file": target_file,
                "wave_id": wave_id,
                "status": "failed",
                "started_at": time.strftime("%H:%M:%S"),
                "elapsed": 0.0,
                "model": None,
                "provider": selection_metadata.get("provider"),
                "provider_id": selection_metadata.get("provider_id"),
                "effort": selection_metadata.get("effort"),
                "effort_source": selection_metadata.get("effort_source"),
            })
            if len(_subtask_history) > 20:
                _subtask_history.pop(0)
            _write_status_file()
        db.log_agent_result(
            session_id=caller or "mcp",
            task_hash=task_id,
            agent_id=0,
            tier=tier,
            model="",
            success=False,
            tokens_used=0,
            provider_name=str(selection_metadata.get("provider", "mcp")),
            used_fallback=False,
            used_speculation=False,
            provenance_trace_id=provenance_trace_id,
            provenance_depth=provenance_depth,
            provenance_caller_id=provenance_caller_id,
            reason="unsupported_effort_override",
            version="mcp",
        )
        return _unsupported_effort_override_result(
            task_id=task_id,
            tier=tier,
            provider=selection_metadata.get("provider"),
            provider_id=selection_metadata.get("provider_id"),
            effort=selection_metadata.get("effort"),
            effort_source=selection_metadata.get("effort_source"),
            caller=caller,
            provenance=provenance,
            details=(
                f"Explicit effort override {selection_metadata.get('effort')!r} cannot be honored by "
                f"{provider_name} ({provider_id or 'unknown provider'}). "
                "Choose a provider with native effort support or omit the explicit effort override."
            ),
        )

    # Register as starting. The status becomes running only after a PID exists.
    _inferred_op_class = "side_effecting" if target_file else "replayable"
    cancel_event = threading.Event()
    with _subtasks_lock:
        _subtask_cancel_events[task_id] = cancel_event
        _active_subtasks[task_id] = {
            "task_id": task_id,
            "prompt_excerpt": (args.get("prompt") or prompt)[:100],
            "tier": tier,
            "target_file": target_file,
            "wave_id": wave_id,
            "op_class": _inferred_op_class,
            "status": "starting",
            "started_at": time.strftime("%H:%M:%S"),
            "start_mono": t0,
            "model": None,  # filled after provider resolves
            "provider": selection_metadata.get("provider"),
            "provider_id": selection_metadata.get("provider_id"),
            "effort": selection_metadata.get("effort"),
            "effort_source": selection_metadata.get("effort_source"),
            "pid": None,  # filled after subprocess starts
        }
        _write_status_file()

    try:
        _snapshot = FileSnapshot(workspace_root)
        _snapshot.take()

        # Compute a monotonic deadline so that total execution (including
        # provider fallbacks/retries) never exceeds the caller's budget.
        deadline = time.monotonic() + timeout

        # Start MCP progress heartbeat if the client supplied a progressToken.
        progress_token = getattr(_request_context, "progress_token", None)
        heartbeat_stop: threading.Event | None = None
        heartbeat_thread: threading.Thread | None = None
        heartbeat_slot_acquired = False
        if progress_token:
            heartbeat_slot_acquired = _progress_heartbeat_slots.acquire(blocking=False)
            if heartbeat_slot_acquired:
                heartbeat_stop = threading.Event()
                heartbeat_thread = threading.Thread(
                    target=_heartbeat_loop,
                    args=(progress_token, heartbeat_stop),
                    daemon=True,
                )
                heartbeat_thread.start()
            else:
                log.debug("execute_subtask: skipping progress heartbeat; limit reached")

        def _execute_provider() -> dict:
            try:
                return registry.execute_cheapest(
                    prompt=prompt,
                    tier=tier,
                    prefer_free=prefer_free,
                    timeout=timeout,
                    deadline=deadline,
                    caller=routing_caller,
                    code_only=code_only,
                    effort=effort,
                    caller_allowlists=caller_allowlists,
                    on_pid=lambda pid: _store_active_pid(task_id, pid, cancel_event),
                    **({"provider_id": provider_id} if provider_id is not None else {}),
                )
            except TypeError as exc:
                if "effort" not in str(exc) and "deadline" not in str(exc):
                    raise
                # Compatibility fallback for third-party registries.
                return registry.execute_cheapest(
                    prompt=prompt,
                    tier=tier,
                    prefer_free=prefer_free,
                    timeout=timeout,
                    caller=routing_caller,
                    code_only=code_only,
                    caller_allowlists=caller_allowlists,
                    on_pid=lambda pid: _store_active_pid(task_id, pid, cancel_event),
                )

        try:
            result = _run_subtask_provider_call(
                task_id,
                deadline=deadline,
                timeout_seconds=timeout,
                cancel_event=cancel_event,
                call=_execute_provider,
            )
        finally:
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=2)
            if heartbeat_slot_acquired:
                _progress_heartbeat_slots.release()
        if isinstance(selection_metadata, dict):
            for key in ("effort", "effort_source"):
                if selection_metadata.get(key) is not None:
                    result.setdefault(key, selection_metadata[key])
        if cancel_event.is_set():
            raise SubtaskCancelled(f"Subtask {task_id} was cancelled")
        if (
            result.get("effort_source") == "explicit"
            and result.get("effort") is not None
            and not _provider_supports_explicit_effort(result.get("provider_id"))
        ):
            actual_provider = str(result.get("provider") or result.get("provider_id") or "provider")
            actual_provider_id = str(result.get("provider_id") or "")
            elapsed = round(_time.monotonic() - t0, 2)
            with _subtasks_lock:
                entry = _active_subtasks.pop(task_id, None)
                if entry:
                    entry.update({
                        "status": "failed",
                        "provider": str(result.get("provider", "")),
                        "provider_id": str(result.get("provider_id", "")),
                        "model": str(result.get("model", "")),
                        "elapsed": elapsed,
                        "effort": result.get("effort"),
                        "effort_source": result.get("effort_source"),
                    })
                    _subtask_history.append(entry)
                    if len(_subtask_history) > 20:
                        _subtask_history.pop(0)
                _write_status_file()
            db.log_agent_result(
                session_id=caller or "mcp",
                task_hash=task_id,
                agent_id=0,
                tier=tier,
                model=str(result.get("model", "")),
                success=False,
                tokens_used=0,
                provider_name=str(result.get("provider", "mcp")),
                used_fallback=bool(result.get("fallback_used", False)),
                used_speculation=False,
                provenance_trace_id=provenance_trace_id,
                provenance_depth=provenance_depth,
                provenance_caller_id=provenance_caller_id,
                reason="unsupported_effort_override",
                version="mcp",
            )
            return _unsupported_effort_override_result(
                task_id=task_id,
                tier=tier,
                provider=result.get("provider"),
                provider_id=result.get("provider_id"),
                effort=result.get("effort"),
                effort_source=result.get("effort_source"),
                caller=caller,
                provenance=provenance,
                details=(
                    f"Explicit effort override {result.get('effort')!r} could not be honored because "
                    f"execution resolved to {actual_provider} ({actual_provider_id or 'unknown provider'})."
                ),
            )

        elapsed = round(_time.monotonic() - t0, 2)
        result["wall_time_seconds"] = elapsed
        result["task_id"] = task_id
        result["provenance"] = provenance
        _print_dispatch_info(
            tier=tier,
            model=str(result.get("model", "")),
            provider=str(result.get("provider", "")),
            billing=str(
                result.get("billing_tier")
                or selection_metadata.get("billing_tier", "")
            ),
            caller=caller,
            task_excerpt=(args.get("prompt") or prompt)[:60],
        )

        # Rough token estimate (~4 chars per token for English code)
        output_text = result.get("result", "")
        prompt_tokens_est = len(prompt) // 4
        completion_tokens_est = len(output_text) // 4
        result["usage_estimate"] = {
            "prompt_tokens": prompt_tokens_est,
                "completion_tokens": completion_tokens_est,
                "total_tokens": prompt_tokens_est + completion_tokens_est,
            }

        # Convergence loop (plan 14): re-execute if target set and score < min_score.
        if _ct_max_rounds > 1 and not result.get("error"):
            import time as _ct_time
            _ct_rounds_data: list[dict] = [{"round": 1, "score": 1.0, "output": output_text[:500]}]
            for _ct_round in range(2, _ct_max_rounds + 1):
                _ct_score = 0.0 if result.get("error") else 1.0
                _ct_rounds_data[-1]["score"] = _ct_score
                if _ct_score >= _ct_min_score:
                    break
                if _ct_backoff > 0:
                    _ct_time.sleep(_ct_backoff)
                _ct_prior = output_text[:500]
                prompt = (
                    f"{_original_prompt}\n\n"
                    f"[Prior attempt {_ct_round - 1} output]\n{_ct_prior}"
                )
                try:
                    _ct_result = _run_subtask_provider_call(
                        task_id,
                        deadline=deadline,
                        timeout_seconds=timeout,
                        cancel_event=cancel_event,
                        call=lambda: registry.execute_cheapest(
                            prompt=prompt,
                            tier=tier,
                            prefer_free=prefer_free,
                            timeout=max(1, int(deadline - time.monotonic())),
                            deadline=deadline,
                            caller=routing_caller,
                            caller_allowlists=caller_allowlists,
                            on_pid=lambda pid: _store_active_pid(
                                task_id,
                                pid,
                                cancel_event,
                            ),
                            **({"provider_id": provider_id} if provider_id is not None else {}),
                        ),
                    )
                except (SubtaskExecutionTimeout, SubtaskCancelled):
                    raise
                except Exception:
                    break
                if not _ct_result.get("error"):
                    result = _ct_result
                    output_text = result.get("result", "")
                _ct_rounds_data.append({
                    "round": _ct_round,
                    "score": 0.0 if _ct_result.get("error") else 1.0,
                    "output": output_text[:500],
                })
            _ct_final_score = _ct_rounds_data[-1]["score"]
            result["convergence_rounds"] = _ct_rounds_data
            result["convergence_exhausted"] = (
                _ct_final_score < _ct_min_score and len(_ct_rounds_data) >= _ct_max_rounds
            )

        if caller:
            result["caller_detected"] = caller
            result["self_excluded"] = any(
                isinstance(item, Mapping)
                and "opt-out" in str(item.get("reason", ""))
                for item in result.get("excluded_providers", [])
            )

        if cancel_event.is_set():
            raise SubtaskCancelled(f"Subtask {task_id} was cancelled")

        # Write to target_file if requested
        if mode == "patch" and target_file and output_text:
            # Patch mode: apply unified diff returned by provider
            if normalized_target is None or validated_target is None:
                return {
                    "error": "InvalidTargetPath",
                    "details": "target_file validation did not complete for patch mode",
                    "requested_path": target_file,
                }
            try:
                new_content, lines_added, lines_removed = apply_unified_diff(validated_target, output_text)
            except ValueError as _diff_exc:
                result["file_write_error"] = f"Patch apply failed: {_diff_exc}"
                result["raw_output_preview"] = output_text[:300]
                log.warning("execute_subtask patch: apply_unified_diff failed for %s: %s", target_file, _diff_exc)
                return result
            old_content_patch: str | None = None
            try:
                old_content_patch = validated_target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                old_content_patch = None
            try:
                write_result = _write_file_with_audit(
                    db,
                    requested_path=validated_target,
                    content=new_content,
                    caller=caller,
                    outcome="written",
                    idempotency_key=_file_idem_key,
                )
            except OSError as exc:
                log.warning("execute_subtask patch: failed to write %s", normalized_target, exc_info=True)
                result["file_write_error"] = str(exc)
                return result
            result.update(write_result)
            result["result"] = new_content
            diff_info = _compute_file_diff(old_content_patch, new_content, str(validated_target))
            result["diff"] = diff_info.get("diff", "")
            result["change_type"] = diff_info.get("change_type", "modified")
            result["lines_added"] = lines_added
            result["lines_removed"] = lines_removed
            result["patch_mode"] = True
        elif mode == "rewrite" and target_file and output_text:
            # Rewrite mode: model returns complete file; apply length-ratio guard
            if normalized_target is None or validated_target is None:
                return {
                    "error": "InvalidTargetPath",
                    "details": "target_file validation did not complete for rewrite mode",
                    "requested_path": target_file,
                }
            code_rw = _extract_code_for_file(output_text, target_file)
            if code_rw is None:
                code_rw = output_text  # fall back to raw output for rewrite mode
            old_content_rw: str | None = None
            try:
                old_content_rw = validated_target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                old_content_rw = None
            # Length-ratio guard: reject if output is suspiciously short
            if old_content_rw:
                _ratio_min = _config.surgical_edit_length_ratio_min
                _shrink_keywords = getattr(
                    _config, "_SHRINK_KEYWORDS",
                    frozenset({"delete", "remove", "drop", "strip", "cleanup",
                               "clean up", "prune", "trim", "shrink",
                               "minimise", "minimize", "consolidate", "collapse"}),
                )
                _prompt_lower = prompt.lower() if isinstance(prompt, str) else ""
                _is_shrink = any(kw in _prompt_lower for kw in _shrink_keywords)
                _length_ratio = len(code_rw) / max(len(old_content_rw), 1)
                if not _is_shrink and _length_ratio < _ratio_min:
                    log.info(
                        "execute_subtask: rewrite length guard rejected %s "
                        "(original=%d output=%d ratio=%.3f threshold=%.3f)",
                        target_file,
                        len(old_content_rw),
                        len(code_rw),
                        _length_ratio,
                        _ratio_min,
                    )
                    result["file_write_error"] = (
                        f"Rewrite output ({len(code_rw)} chars) is less than "
                        f"{_ratio_min*100:.0f}% of original ({len(old_content_rw)} chars). "
                        "Model likely returned a fragment. Retry or use mode='blocks'."
                    )
                    result["error_category"] = "length_ratio_rejected"
                    result["retryable"] = True
                    result["length_ratio"] = round(_length_ratio, 3)
                    return result
            try:
                write_result_rw = _write_file_with_audit(
                    db,
                    requested_path=validated_target,
                    content=code_rw,
                    caller=caller,
                    outcome="written",
                    idempotency_key=_file_idem_key,
                )
            except OSError as exc:
                log.warning("execute_subtask rewrite: failed to write %s", normalized_target, exc_info=True)
                result["file_write_error"] = str(exc)
                return result
            result.update(write_result_rw)
            result["result"] = code_rw
            if code_rw != output_text:
                result["preamble_stripped"] = True
            diff_info_rw = _compute_file_diff(old_content_rw, code_rw, str(validated_target))
            result["diff"] = diff_info_rw.get("diff", "")
            result["change_type"] = diff_info_rw.get("change_type", "modified")
            result["lines_added"] = diff_info_rw.get("lines_added", 0)
            result["lines_removed"] = diff_info_rw.get("lines_removed", 0)
            result["rewrite_mode"] = True
        elif mode == "blocks" and target_file and output_text:
            # Blocks mode: parse Aider-style SEARCH/REPLACE and apply
            if normalized_target is None or validated_target is None:
                return {
                    "error": "InvalidTargetPath",
                    "details": "target_file validation did not complete for blocks mode",
                    "requested_path": target_file,
                }
            try:
                from shared.edit_blocks import parse_and_apply as _parse_and_apply
                new_content_blk, blk_added, blk_removed = _parse_and_apply(validated_target, output_text)
            except ValueError as _blk_exc:
                result["file_write_error"] = f"Edit-blocks apply failed: {_blk_exc}"
                result["raw_output_preview"] = output_text[:300]
                result["error_category"] = "blocks_apply_failed"
                result["retryable"] = True
                log.warning("execute_subtask blocks: apply failed for %s: %s", target_file, _blk_exc)
                return result
            old_content_blk: str | None = None
            try:
                old_content_blk = validated_target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                old_content_blk = None
            try:
                write_result_blk = _write_file_with_audit(
                    db,
                    requested_path=validated_target,
                    content=new_content_blk,
                    caller=caller,
                    outcome="written",
                    idempotency_key=_file_idem_key,
                )
            except OSError as exc:
                log.warning("execute_subtask blocks: failed to write %s", normalized_target, exc_info=True)
                result["file_write_error"] = str(exc)
                return result
            result.update(write_result_blk)
            result["result"] = new_content_blk
            diff_info_blk = _compute_file_diff(old_content_blk, new_content_blk, str(validated_target))
            result["diff"] = diff_info_blk.get("diff", "")
            result["change_type"] = diff_info_blk.get("change_type", "modified")
            result["lines_added"] = blk_added
            result["lines_removed"] = blk_removed
            result["blocks_mode"] = True
        elif target_file and output_text:
            if _is_text_doc_target(target_file):
                code = _extract_text_for_file(output_text, target_file)
                file_write_err_msg = (
                    "No valid text content detected in model output. "
                    "The model may have returned reasoning or error text "
                    "instead of document content."
                )
            else:
                code = _extract_code_for_file(output_text, target_file)
                file_write_err_msg = (
                    "No valid code detected in model output. "
                    "The model may have returned reasoning or error text "
                    "instead of source code."
                )
            if code is None:
                # Model returned reasoning/errors instead of content — don't
                # write garbage to disk.  Return the raw output so the
                # caller can decide what to do (e.g., retry or write directly).
                result["file_write_error"] = file_write_err_msg
                result["raw_output_preview"] = output_text[:300]
                result["error_category"] = "malformed_output"
                result["retryable"] = True
                log.warning(
                    "execute_subtask: no content found for %s — raw: %s",
                    target_file, output_text[:200],
                )
            else:
                if normalized_target is None or validated_target is None:
                    return {
                        "error": "InvalidTargetPath",
                        "details": "target_file validation did not complete",
                        "requested_path": target_file,
                    }

                old_content: str | None = None
                try:
                    old_content = validated_target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    old_content = None

                try:
                    write_result = _write_file_with_audit(
                        db,
                        requested_path=validated_target,
                        content=code,
                        caller=caller,
                        outcome="written",
                        idempotency_key=_file_idem_key,
                    )
                except OSError as exc:
                    log.warning("execute_subtask: failed to write %s", normalized_target, exc_info=True)
                    with _subtasks_lock:
                        entry = _active_subtasks.pop(task_id, None)
                        if entry:
                            entry.update({
                                "status": "failed",
                                "model": str(result.get("model", "")),
                                "provider": str(result.get("provider", "")),
                                "provider_id": str(result.get("provider_id", "")),
                                "elapsed": elapsed,
                                "target_file": target_file,
                                "effort": result.get("effort"),
                                "effort_source": result.get("effort_source"),
                            })
                            _subtask_history.append(entry)
                            if len(_subtask_history) > 20:
                                _subtask_history.pop(0)
                        _write_status_file()
                    db.log_agent_result(
                        session_id=caller or "mcp",
                        task_hash=task_id,
                        agent_id=0,
                        tier=tier,
                        model=str(result.get("model", "")),
                        success=False,
                        tokens_used=prompt_tokens_est + completion_tokens_est,
                        provider_name=str(result.get("provider", "mcp")),
                        used_fallback=bool(result.get("fallback_used", False)),
                        used_speculation=False,
                        provenance_trace_id=provenance_trace_id,
                        provenance_depth=provenance_depth,
                        provenance_caller_id=provenance_caller_id,
                        provider_opt_out_reason="; ".join(
                            reason
                            for item in result.get("excluded_providers", [])
                            if isinstance(item, dict)
                            for reason in [str(item.get("reason", ""))]
                            if reason.startswith("adapter opt-out")
                        ) or None,
                        reason="write_error",
                        version="mcp",
                    )
                    _log_write_audit(
                        db,
                        requested_path=normalized_target,
                        caller=caller,
                        outcome="write-failed",
                        details=str(exc),
                    )
                    return {
                        "error": "WriteError",
                        "details": str(exc),
                        "requested_path": str(normalized_target),
                    }

                result.update(write_result)
                result["result"] = code
                if code != output_text:
                    result["preamble_stripped"] = True

                diff_info = _compute_file_diff(
                    old_content, code, str(validated_target)
                )
                result["diff"] = diff_info.get("diff", "")
                result["change_type"] = diff_info.get("change_type", "modified")
                result["lines_added"] = diff_info.get("lines_added", 0)
                result["lines_removed"] = diff_info.get("lines_removed", 0)

                log.info(
                    "execute_subtask: wrote %d lines to %s (%s: +%d/-%d)",
                    write_result.get("lines_written", 0),
                    normalized_target,
                    diff_info.get("change_type", "modified"),
                    diff_info.get("lines_added", 0),
                    diff_info.get("lines_removed", 0),
                )

        safe_target_path = str(validated_target or normalized_target or target_file or "")
        if target_file and result.get("diff"):
            _snapshot_diffs = [{
                "path": safe_target_path,
                "change_type": result.get("change_type", "unchanged"),
                "lines_added": result.get("lines_added", 0),
                "lines_removed": result.get("lines_removed", 0),
                "diff": result.get("diff", ""),
            }]
        else:
            _snapshot_diffs = [
                {
                    "path": diff.path,
                    "change_type": diff.change_type,
                    "lines_added": diff.lines_added,
                    "lines_removed": diff.lines_removed,
                    "diff": diff.diff,
                }
                for diff in _snapshot.diff_since(target_file=target_file)
            ]
        if _snapshot_diffs and not result.get("diff"):
            primary = next(
                (diff for diff in _snapshot_diffs if diff.get("path") == target_file),
                _snapshot_diffs[0],
            )
            result["diff"] = primary.get("diff", "")
            result["change_type"] = primary.get("change_type", "unchanged")
            result["lines_added"] = primary.get("lines_added", 0)
            result["lines_removed"] = primary.get("lines_removed", 0)
        result["all_diffs"] = _snapshot_diffs
        _print_diff_to_terminal(
            all_diffs=result.get("all_diffs", []),
            agent_label=safe_target_path or str(result.get("file_written") or target_file or task_id[:16]),
            tier=tier,
            provider=str(result.get("provider", "?")),
        )
        _stdin = sys.stdin
        try:
            _stdin_is_tty = _stdin is not None and _stdin.isatty()
        except Exception:
            _stdin_is_tty = False
        _should_gate = (
            _config.code_review
            and _stdin_is_tty
            and (
                _config.code_review_tier == "all"
                or (_config.code_review_tier == "medium" and tier in ("medium", "high"))
                or (_config.code_review_tier == "high" and tier == "high")
            )
        )
        if _should_gate:
            _approved = _approval_gate(
                snapshot=_snapshot,
                all_diffs=result.get("all_diffs", []),
                tier=tier,
                auto_approve_timeout=_config.auto_approve_timeout,
            )
            if not _approved:
                _snapshot.revert(
                    [FileDiff(**diff) for diff in result.get("all_diffs", [])]
                )
                result["status"] = "reverted"
                result["output"] = "Changes reverted by developer."
                result["result"] = "Changes reverted by developer."
                result.pop("diff", None)
                result.pop("all_diffs", None)
                result.pop("lines_added", None)
                result.pop("lines_removed", None)
                result.pop("change_type", None)
            else:
                result["review_status"] = "approved"

        resolved_model = str(result.get("model", ""))
        resolved_provider = str(result.get("provider", ""))
        resolved_status = str(result.get("status") or "done")
        with _subtasks_lock:
            entry = _active_subtasks.pop(task_id, None)
            if entry:
                entry.update({
                    "status": resolved_status,
                    "model": resolved_model,
                    "provider": resolved_provider,
                    "provider_id": str(result.get("provider_id", "")),
                    "elapsed": elapsed,
                    "target_file": target_file,
                    "effort": result.get("effort"),
                    "effort_source": result.get("effort_source"),
                })
                _subtask_history.append(entry)
                if len(_subtask_history) > 20:
                    _subtask_history.pop(0)
            _write_status_file()
        db.log_agent_result(
            session_id=caller or "mcp",
            task_hash=task_id,
            agent_id=0,
            tier=tier,
            model=resolved_model,
            success=resolved_status != "reverted",
            tokens_used=prompt_tokens_est + completion_tokens_est,
            provider_name=resolved_provider or "mcp",
            used_fallback=bool(result.get("fallback_used", False)),
            used_speculation=False,
            provenance_trace_id=provenance_trace_id,
            provenance_depth=provenance_depth,
            provenance_caller_id=provenance_caller_id,
            provider_opt_out_reason="; ".join(
                reason
                for item in result.get("excluded_providers", [])
                if isinstance(item, dict)
                for reason in [str(item.get("reason", ""))]
                if reason.startswith("adapter opt-out")
            ) or None,
            parse_diagnostics=json.dumps({
                "model_id": result.get("model_id") or resolved_model,
                "discovery_source": result.get("discovery_source"),
                "discovered_at": result.get("discovered_at"),
                "catalog_stale_until": result.get("catalog_stale_until"),
                "fallback_reason": result.get("fallback_reason"),
                "model_fallbacks": result.get("model_fallbacks", []),
            }, sort_keys=True),
            reason="execute_subtask_reverted" if resolved_status == "reverted" else "execute_subtask",
            version="mcp",
        )

        # Fix 2: record execution so routing guard allows subsequent Edit/Write on file_hints
        _exec_cwd = args.get("cwd") or str(Path.cwd())
        _exec_file_written = str(validated_target or normalized_target or target_file or "")
        try:
            db.routing_guard_record_execution(
                caller=routing_caller,
                cwd=_exec_cwd,
                task_id=task_id,
                file_written=_exec_file_written or None,
            )
        except Exception:
            log.debug("execute_subtask: routing_guard_record_execution failed", exc_info=True)

        if _exec_file_written:
            try:
                db.routing_exception_add(
                    exception_type="path",
                    pattern=_exec_file_written,
                    note="auto:execute_subtask",
                )
                log.debug("execute_subtask: auto path exception registered for %s", _exec_file_written)
            except Exception:
                log.debug("execute_subtask: auto path exception failed", exc_info=True)

        return result
    except SubtaskExecutionTimeout:
        elapsed = round(_time.monotonic() - t0, 2)
        _terminalize_active_subtask(
            task_id,
            "timed_out",
            elapsed=elapsed,
            updates={"cancellation_reason": "timeout"},
        )
        db.log_agent_result(
            session_id=caller or "mcp",
            task_hash=task_id,
            agent_id=0,
            tier=tier,
            model="",
            success=False,
            tokens_used=0,
            provider_name=str(selection_metadata.get("provider") or "mcp"),
            used_fallback=False,
            used_speculation=False,
            provenance_trace_id=provenance_trace_id,
            provenance_depth=provenance_depth,
            provenance_caller_id=provenance_caller_id,
            reason="execution_timeout",
            version="mcp",
        )
        return {
            "error": "Timeout",
            "details": f"Provider launch or execution exceeded {timeout} seconds.",
            "task_id": task_id,
            "status": "timed_out",
            "wall_time_seconds": elapsed,
            "tier": tier,
            "provenance": provenance,
        }
    except SubtaskCancelled:
        elapsed = round(_time.monotonic() - t0, 2)
        with _subtasks_lock:
            active_entry = _active_subtasks.get(task_id)
            cancellation_reason = (
                str(active_entry.get("cancellation_reason") or "user")
                if active_entry is not None
                else "user"
            )
        _terminalize_active_subtask(
            task_id,
            "cancelled",
            elapsed=elapsed,
            updates={"cancellation_reason": cancellation_reason},
        )
        db.log_agent_result(
            session_id=caller or "mcp",
            task_hash=task_id,
            agent_id=0,
            tier=tier,
            model="",
            success=False,
            tokens_used=0,
            provider_name=str(selection_metadata.get("provider") or "mcp"),
            used_fallback=False,
            used_speculation=False,
            provenance_trace_id=provenance_trace_id,
            provenance_depth=provenance_depth,
            provenance_caller_id=provenance_caller_id,
            reason="execution_cancelled",
            version="mcp",
        )
        return {
            "error": "Cancelled",
            "details": (
                "Subtask was cancelled because the MCP transport disconnected."
                if cancellation_reason == "transport_disconnect"
                else "Subtask was cancelled before completion."
            ),
            "task_id": task_id,
            "status": "cancelled",
            "cancellation_reason": cancellation_reason,
            "wall_time_seconds": elapsed,
            "tier": tier,
            "provenance": provenance,
        }
    except RuntimeError as exc:
        elapsed = round(_time.monotonic() - t0, 2)
        log.warning("execute_subtask: provider/orchestration failure", exc_info=True)
        with _subtasks_lock:
            entry = _active_subtasks.pop(task_id, None)
            if entry:
                entry.update({
                    "status": "failed",
                    "elapsed": elapsed,
                    "effort": entry.get("effort"),
                    "effort_source": entry.get("effort_source"),
                })
                _subtask_history.append(entry)
                if len(_subtask_history) > 20:
                    _subtask_history.pop(0)
            _write_status_file()
        db.log_agent_result(
            session_id=caller or "mcp",
            task_hash=task_id,
            agent_id=0,
            tier=tier,
            model="",
            success=False,
            tokens_used=0,
            provider_name="mcp",
            used_fallback=False,
            used_speculation=False,
            provenance_trace_id=provenance_trace_id,
            provenance_depth=provenance_depth,
            provenance_caller_id=provenance_caller_id,
            reason="provider_error",
            version="mcp",
        )
        return {
            "error": "ProviderError",
            "details": "Provider execution failed. Check server logs for details.",
            "task_id": task_id,
            "wall_time_seconds": elapsed,
            "caller_detected": caller,
            "tier": tier,
            "provenance": provenance,
            "providers_checked": _safe_registry_diagnostics(registry),
            "hint": (
                "All CLI providers failed. Common causes: "
                "gh copilot subprocess timeout, rate limiting, or network error. "
                "Check stderr logs for details."
            ),
        }
    except Exception as exc:
        elapsed = round(_time.monotonic() - t0, 2)
        log.warning("execute_subtask: unexpected failure", exc_info=True)
        db.log_agent_result(
            session_id=caller or "mcp",
            task_hash=task_id,
            agent_id=0,
            tier=tier,
            model="",
            success=False,
            tokens_used=0,
            provider_name="mcp",
            used_fallback=False,
            used_speculation=False,
            provenance_trace_id=provenance_trace_id,
            provenance_depth=provenance_depth,
            provenance_caller_id=provenance_caller_id,
            reason="execution_error",
            version="mcp",
        )
        return {
            "error": "ExecutionError",
            "details": str(exc),
            "task_id": task_id,
            "wall_time_seconds": elapsed,
            "caller_detected": caller,
            "tier": tier,
            "provenance": provenance,
        }
    finally:
        elapsed = round(_time.monotonic() - t0, 2)
        _terminalize_active_subtask(task_id, "failed", elapsed=elapsed)
        with _subtasks_lock:
            _subtask_cancel_events.pop(task_id, None)


def handle_check_providers(_args: dict) -> dict:
    """Return compact, secret-safe provider diagnostics augmented with usage windows.

    Per D-03: defaults to compact summary with detect_reason, source,
    and health per provider. No credentials or sensitive state exposed.
    """
    registry = _get_registry_with_config()
    base = registry.to_compact_dict()

    try:
        config, db, router, planner, orchestrator = _ensure_init()
    except Exception:
        return base

    try:
        quota_service = ProviderQuotaService(db)
        checker = ProviderUsageChecker(quota_service)
    except Exception:
        return base

    usage_cfg = getattr(config, "provider_usage_windows", {}) or {}
    for prov in base.get("providers", []):
        prov_name = (prov.get("name") or "").lower()
        try:
            prov["quota"] = quota_service.get(prov_name).to_dict()
        except Exception:
            prov["quota"] = {
                "provider": prov_name,
                "status": "unavailable",
                "source": "quota_adapter",
                "error": "quota adapter failed",
                "windows": [],
            }
        windows_list = []
        cfg = usage_cfg.get(prov_name)
        if cfg and getattr(cfg, "windows", None):
            for w in cfg.windows:
                try:
                    decision = checker.query_window_decision(
                        prov_name,
                        w.hours,
                        w.budget_tokens,
                        w.threshold,
                        w.action,
                        db,
                    )
                except Exception:
                    decision = {"ratio": None, "source": "unavailable", "triggered": False}
                ratio = decision.get("ratio")
                windows_list.append({
                    "hours": w.hours,
                    "ratio": ratio,
                    "threshold": w.threshold,
                    "triggered": bool(decision.get("triggered")),
                    "action": w.action,
                    "source": decision.get("source"),
                    "fallback_reason": decision.get("fallback_reason"),
                })
        prov["usage_windows"] = windows_list

    return base


def handle_list_subtasks(_args: dict) -> dict:
    """Return currently running and recently completed execute_subtask calls."""
    now = time.monotonic()
    with _subtasks_lock:
        active_raw = [
            {
                **entry,
                "elapsed": round(now - entry.get("start_mono", now), 1),
            }
            for entry in _active_subtasks.values()
        ]
        recent = list(_subtask_history[-10:])

    # Group active tasks by wave_id; auto-group unnamed tasks that started
    # within 2 seconds of each other (parallel dispatch fingerprint).
    waves: dict[str, list[dict]] = {}
    ungrouped: list[dict] = []
    for entry in active_raw:
        w = entry.get("wave_id")
        if w:
            waves.setdefault(w, []).append(entry)
        else:
            ungrouped.append(entry)

    # Auto-group ungrouped tasks by start proximity (±2 s bucket)
    if ungrouped:
        ungrouped_sorted = sorted(ungrouped, key=lambda e: e.get("elapsed", 0), reverse=True)
        bucket_anchor: float | None = None
        bucket_name = ""
        for entry in ungrouped_sorted:
            elapsed = entry.get("elapsed", 0.0)
            if bucket_anchor is None or abs(elapsed - bucket_anchor) > 2.0:
                bucket_anchor = elapsed
                bucket_name = f"auto-{round(elapsed)}s"
            waves.setdefault(bucket_name, []).append(entry)

    # Build structured output
    active_groups = []
    for wave_name, tasks in waves.items():
        active_groups.append({
            "wave": wave_name,
            "parallel": len(tasks) > 1,
            "count": len(tasks),
            "tasks": tasks,
        })

    active_swarms: list[dict[str, object]] = []
    try:
        _config, db, *_ = _ensure_init()
        with db.conn() as conn:
            rows = conn.execute(
                """
                SELECT swarm_id, status, requested_agents, effective_agents,
                       progress_counters, topology, created_ts
                FROM swarm_runs
                WHERE status IN ('planned', 'running')
                ORDER BY created_ts DESC
                LIMIT 10
                """
            ).fetchall()
        for row in rows:
            try:
                progress = (
                    json.loads(row[4])
                    if isinstance(row[4], str)
                    else (row[4] or {})
                )
            except (TypeError, json.JSONDecodeError):
                progress = {}
            active_swarms.append({
                "swarm_id": row[0],
                "status": row[1],
                "requested_agents": row[2],
                "effective_agents": row[3],
                "progress_counters": progress,
                "topology": row[5],
                "created_ts": row[6],
            })
    except Exception:
        log.debug("Could not load active swarm status", exc_info=True)

    return {
        "active_groups": active_groups,
        "active_count": len(active_raw),
        "active_swarms": active_swarms,
        "active_swarm_count": len(active_swarms),
        "recent": recent,
        "recent_count": len(recent),
        "hint": (
            "parallel=true means multiple subtasks in this wave are running simultaneously. "
            "Pass wave_id to execute_subtask to group tasks explicitly."
        ),
    }


def handle_stop_subtask(args: dict) -> dict:
    """Pause a running process or cancel a task that has not started yet."""
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return {"error": "task_id is required"}

    with _subtasks_lock:
        entry = _active_subtasks.get(task_id)
        if entry is None:
            return {"error": f"No active subtask with task_id={task_id!r}"}
        pid = entry.get("pid")
        if pid is None:
            if entry.get("status") == "cancelling":
                return {"status": "cancellation_requested", "task_id": task_id, "pid": None}
            cancel_event = _subtask_cancel_events.get(task_id)
            entry["status"] = "cancelling"
            entry["cancellation_reason"] = "user"
            if cancel_event is not None:
                cancel_event.set()
            _write_status_file()
            return {"status": "cancellation_requested", "task_id": task_id, "pid": None}
        if entry.get("status") == "stopped":
            return {"status": "already_stopped", "task_id": task_id, "pid": pid}
        try:
            os.kill(pid, signal.SIGSTOP)
        except ProcessLookupError:
            entry["status"] = "completed"
            _write_status_file()
            return {"error": f"Process {pid} not found — marked as completed"}
        except OSError as exc:
            return {"error": f"Failed to send SIGSTOP to pid={pid}: {exc}"}
        entry["status"] = "stopped"
        _write_status_file()

    return {"status": "stopped", "task_id": task_id, "pid": pid}


def handle_resume_subtask(args: dict) -> dict:
    """Send SIGCONT to a stopped subtask by task_id."""
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return {"error": "task_id is required"}

    with _subtasks_lock:
        entry = _active_subtasks.get(task_id)
        if entry is None:
            return {"error": f"No active subtask with task_id={task_id!r}"}
        pid = entry.get("pid")
        if pid is None:
            return {"error": f"Subtask {task_id!r} has no PID"}
        if entry.get("status") == "running":
            return {"status": "already_running", "task_id": task_id, "pid": pid}
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            entry["status"] = "completed"
            _write_status_file()
            return {"error": f"Process {pid} not found — marked as completed"}
        except OSError as exc:
            return {"error": f"Failed to send SIGCONT to pid={pid}: {exc}"}
        entry["status"] = "running"
        _write_status_file()

    return {"status": "running", "task_id": task_id, "pid": pid}


# ---------------------------------------------------------------------------
# Trace replay + forking tools (plan 13)
# ---------------------------------------------------------------------------

def handle_trace_show(args: dict) -> dict:
    """Show checkpoint timeline for a swarm run.

    Args:
        run_id: swarm run ID to inspect
    """
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        return {"error": "run_id is required"}
    try:
        _, db, _, _, _ = _ensure_init()
        if db is None:
            return {"error": "DB not available"}
        from shared.replay import ReplayEngine
        return ReplayEngine(db).show_run(run_id)
    except Exception as exc:
        log.exception("trace_show failed")
        return {"error": str(exc)}


def handle_trace_replay(args: dict) -> dict:
    """Replay a swarm run from a coordinator checkpoint.

    Args:
        run_id: source run ID
        from_checkpoint_id: checkpoint row id to start from (optional)
    """
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        return {"error": "run_id is required"}
    from_cp = args.get("from_checkpoint_id")
    try:
        from_cp = int(from_cp) if from_cp is not None else None
    except (TypeError, ValueError):
        from_cp = None
    try:
        _, db, _, _, _ = _ensure_init()
        if db is None:
            return {"error": "DB not available"}
        from shared.replay import ReplayEngine
        return ReplayEngine(db).execute_replay(run_id, from_checkpoint_id=from_cp)
    except Exception as exc:
        log.exception("trace_replay failed")
        return {"error": str(exc)}


def handle_trace_fork(args: dict) -> dict:
    """Fork a swarm run from a coordinator checkpoint.

    Args:
        run_id: source run ID
        from_checkpoint_id: checkpoint to fork from (optional)
        overrides: dict of key=val overrides (e.g. {"tier": "high"})
        dry_run: if true, plan only — do not write fork row
    """
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        return {"error": "run_id is required"}
    from_cp = args.get("from_checkpoint_id")
    try:
        from_cp = int(from_cp) if from_cp is not None else None
    except (TypeError, ValueError):
        from_cp = None
    overrides = args.get("overrides") or {}
    if not isinstance(overrides, dict):
        overrides = {}
    dry_run = bool(args.get("dry_run", False))
    try:
        _, db, _, _, _ = _ensure_init()
        if db is None:
            return {"error": "DB not available"}
        from shared.replay import ReplayEngine
        return ReplayEngine(db).fork(run_id, from_checkpoint_id=from_cp,
                                     overrides=overrides, dry_run=dry_run)
    except Exception as exc:
        log.exception("trace_fork failed")
        return {"error": str(exc)}


def handle_trace_diff(args: dict) -> dict:
    """Side-by-side comparison of two swarm run trajectories.

    Args:
        run_a: first run ID
        run_b: second run ID
    """
    run_a = str(args.get("run_a") or "").strip()
    run_b = str(args.get("run_b") or "").strip()
    if not run_a or not run_b:
        return {"error": "run_a and run_b are required"}
    try:
        _, db, _, _, _ = _ensure_init()
        if db is None:
            return {"error": "DB not available"}
        from shared.replay import ReplayEngine
        return ReplayEngine(db).diff(run_a, run_b)
    except Exception as exc:
        log.exception("trace_diff failed")
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Persistent worker session tools (plan 10)
# ---------------------------------------------------------------------------

def handle_session_start(args: dict) -> dict:
    """Start a persistent worker session. Returns session_id.

    Args:
        provider: provider name (e.g. 'claude-code')
        model: model string (e.g. 'claude-sonnet-4-6')
        context: optional initial context injected on session start
    """
    provider = str(args.get("provider") or "").strip()
    model = str(args.get("model") or "").strip()
    context = str(args.get("context") or "")
    if not provider:
        return {"error": "provider is required"}
    if not model:
        return {"error": "model is required"}
    try:
        _, db, _, _, _ = _ensure_init()
        mgr = _get_session_manager(db=db)
        session_id = mgr.start(provider, model, initial_context=context)
        return {"session_id": session_id, "provider": provider, "model": model}
    except Exception as exc:
        log.exception("session_start failed")
        return {"error": str(exc)}


def handle_session_send(args: dict) -> dict:
    """Send a message to an active worker session.

    Args:
        session_id: session to send to
        message: prompt text to send
        timeout: optional response timeout seconds (default 120)
    """
    session_id = str(args.get("session_id") or "").strip()
    message = str(args.get("message") or "").strip()
    try:
        timeout = int(args.get("timeout") or 120)
    except (TypeError, ValueError):
        timeout = 120
    if not session_id:
        return {"error": "session_id is required"}
    if not message:
        return {"error": "message is required"}
    try:
        _, db, _, _, _ = _ensure_init()
        mgr = _get_session_manager(db=db)
        result = mgr.send(session_id, message, timeout=timeout)
        return result
    except KeyError:
        return {"error": f"Unknown session: {session_id!r}"}
    except Exception as exc:
        log.exception("session_send failed")
        return {"error": str(exc)}


def handle_session_close(args: dict) -> dict:
    """Close (terminate) a worker session.

    Args:
        session_id: session to close
        cancel: if true, send SIGINT before closing (default false)
    """
    session_id = str(args.get("session_id") or "").strip()
    cancel = bool(args.get("cancel", False))
    if not session_id:
        return {"error": "session_id is required"}
    try:
        _, db, _, _, _ = _ensure_init()
        mgr = _get_session_manager(db=db)
        if cancel:
            mgr.cancel(session_id)
        mgr.close(session_id)
        return {"closed": True, "session_id": session_id}
    except Exception as exc:
        log.exception("session_close failed")
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Learning inspection tools (Phase 10)
# ---------------------------------------------------------------------------

def handle_learning_agent_summary(_args: dict) -> dict:
    """Get summary of all learned agents (active, pending, rejected).
    
    Exposes agent state without sensitive data (tokens, secrets, API keys).
    """
    try:
        config, db, router, planner, orchestrator = _ensure_init()
        
        # Query agents by status
        active = db.get_active_agents() or []  # Only active agents
        pending = db.list_pending_approvals("") or []  # Get pending approvals
        
        # Query rejected agents by lane
        rejected = []
        for lane in ['project', 'shared']:
            try:
                lane_agents = db.agent_definitions_list(lane=lane) or []
                rejected.extend([a for a in lane_agents if a.get('status') == 'rejected'])
            except Exception:
                pass
        
        # Format response (without sensitive data like tokens)
        def format_agent(agent: dict) -> dict:
            """Format agent for operator review, truncating sensitive fields."""
            description = agent.get('description', '')
            # Truncate description to 100 chars to avoid leaking large prompts
            if isinstance(description, str) and len(description) > 100:
                description = description[:97] + '...'
            
            return {
                'id': agent.get('id', agent.get('agent_id', 'unknown')),
                'description': description,
                'lane': agent.get('lane', 'unknown'),
                'pattern_hash': agent.get('pattern_hash', 'unknown'),
                'status': agent.get('status', 'unknown'),
                'created_at': agent.get('created_at', 0)
            }
        
        return {
            'success': True,
            'total': len(active) + len(pending) + len(rejected),
            'active': [format_agent(a) for a in active] if active else [],
            'pending': [format_agent(p) for p in pending] if pending else [],
            'rejected': [format_agent(r) for r in rejected] if rejected else [],
            'approval_queue_length': len(pending) if pending else 0
        }
    except Exception as e:
        log.exception(f"learning_agent_summary failed: {e}")
        return {'error': str(e), 'success': False}


def handle_learning_outcome_stats(_args: dict) -> dict:
    """Get outcome distribution and coverage from memory snapshot.
    
    Returns:
        success: bool
        window_start_time: float (Unix timestamp)
        window_end_time: float (Unix timestamp)
        outcome_distribution: dict[str, dict[str, int]] keyed by "tier:model"
        coverage_percentage: float or None
        total_tasks_in_window: int
        tasks_with_feedback: int
        computed_at: float
        error: str (if snapshot not available)
    """
    try:
        config, db, router, planner, orchestrator = _ensure_init()
        
        # Retrieve snapshot from memory (global scope, learning_stats key)
        try:
            result = memory_get("global", "learning_stats", db=db)
        except MemoryNotFoundError:
            return {
                "success": False,
                "error": "Outcome snapshot not yet available (background computation may be initializing)"
            }
        
        if result is None or not result.get("value"):
            return {
                "success": False,
                "error": "Outcome snapshot not yet available (background computation may be initializing)"
            }
        
        snapshot = result.get("value", {})
        
        return {
            "success": True,
            "window_start_time": snapshot.get("window_start_time"),
            "window_end_time": snapshot.get("window_end_time"),
            "outcome_distribution": snapshot.get("outcome_distribution", {}),
            "coverage_percentage": snapshot.get("coverage_percentage"),
            "total_tasks_in_window": snapshot.get("total_tasks_in_window", 0),
            "tasks_with_feedback": snapshot.get("tasks_with_feedback", 0),
            "computed_at": snapshot.get("computed_at"),
        }
    
    except Exception as e:
        log.exception("learning_outcome_stats failed: %s", e)
        return {"success": False, "error": str(e)}


def handle_learning_pattern_health(_args: dict) -> dict:
    """Get health of pattern tracking system (maturity, drafting readiness).
    
    Args:
        project_id (str): Optional filter by project
    
    Returns:
        patterns_tracked: total patterns tracked
        mature_patterns: ready for drafting
        pending_proof: not yet ready
        draft_proposals: currently pending approval
        active_agents: actively in use
        outcome_coverage_percentage: % of tasks with outcome feedback (from 1-hour snapshot)
        outcome_window_hours: window size in hours (always 1 for v1.8)
        feedback_scope: scope of feedback (always "global" for v1.8)
    """
    try:
        config, db, router, planner, orchestrator = _ensure_init()
        raw_project_id = _args.get("project_id")
        project_id = raw_project_id if isinstance(raw_project_id, str) else None
        
        # Query patterns
        patterns = db.get_mature_patterns(min_occurrences=1) or []  # Get all patterns
        readiness = [evaluate_pattern_readiness(pattern, project_id) for pattern in patterns]
        
        # Categorize by readiness
        mature = [
            pattern for pattern, state in zip(patterns, readiness)
            if bool(state.get("ready", False))
        ]

        pending_proof = [
            pattern for pattern, state in zip(patterns, readiness)
            if not bool(state.get("ready", False))
        ]
        
        # Count agents
        active_agents = db.get_active_agents() or []
        pending_drafts = db.list_pending_approvals("") or []
        
        # Retrieve outcome snapshot for coverage metric
        outcome_coverage = None
        try:
            snapshot_result = memory_get("global", "learning_stats", db=db)
            if snapshot_result and snapshot_result.get("value"):
                snapshot = snapshot_result.get("value", {})
                outcome_coverage = snapshot.get("coverage_percentage")
        except MemoryNotFoundError:
            # Snapshot not yet available, coverage remains None
            pass
        except Exception as e:
            log.warning("Failed to retrieve outcome snapshot for coverage: %s", e)
        
        return {
            'success': True,
            'patterns_tracked': len(patterns),
            'mature_patterns': len(mature),
            'pending_proof': len(pending_proof),
            'draft_proposals': len(pending_drafts) if pending_drafts else 0,
            'active_agents': len(active_agents) if active_agents else 0,
            'outcome_coverage_percentage': outcome_coverage,
            'outcome_window_hours': 1,
            'feedback_scope': 'global',
        }
    except Exception as e:
        log.exception(f"learning_pattern_health failed: {e}")
        return {'error': str(e), 'success': False}


def handle_learning_audit_log(args: dict) -> dict:
    """Get audit trail for agent creation, approval, registration events.
    
    Args:
        agent_id (str): Optional filter by agent
        limit (int): Max events to return (default 50)
    
    Returns:
        events: List of audit events with timestamps and details (no secrets)
    """
    try:
        _, db, *_ = _ensure_init()

        raw_agent_id = args.get("agent_id")
        agent_id = raw_agent_id.strip() if isinstance(raw_agent_id, str) else None
        if agent_id == "":
            agent_id = None
        raw_limit = args.get("limit", 50)
        limit = raw_limit if type(raw_limit) is int else 50
        limit = max(1, min(limit, 100))

        sensitive_keys = {
            "authorization",
            "credential",
            "credentials",
            "password",
            "secret",
            "token",
            "api_key",
            "apikey",
        }
        sensitive_value_patterns = (
            re.compile(r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
            re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{8,}\b", re.IGNORECASE),
        )

        def redact(value: object) -> object:
            if isinstance(value, dict):
                redacted: dict[str, object] = {}
                for key, item in value.items():
                    normalized_key = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
                    if normalized_key in sensitive_keys or any(
                        fragment in normalized_key
                        for fragment in ("password", "secret", "token", "credential", "api_key")
                    ):
                        redacted[str(key)] = "<redacted>"
                    else:
                        redacted[str(key)] = redact(item)
                return redacted
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, tuple):
                return [redact(item) for item in value]
            if isinstance(value, str):
                result = value
                for pattern in sensitive_value_patterns:
                    result = pattern.sub("<redacted>", result)
                return result
            return value

        events = []
        for row in db.list_agent_audit_events(agent_id=agent_id, limit=limit):
            try:
                details = json.loads(row.pop("details_json"))
            except (TypeError, json.JSONDecodeError):
                details = {"parse_error": "invalid_details_json"}
            if not isinstance(details, dict):
                details = {"value": details}
            row["details"] = redact(details)
            events.append(row)

        return {
            "success": True,
            "events": events,
            "limit": limit,
            "count": len(events),
        }
    except Exception as e:
        log.exception("learning_audit_log failed: %s", e)
        return {"error": str(e), "success": False}


def handle_remote_dispatch(args: dict) -> dict:
    """Dispatch a task to a remote Threnody HTTP server."""
    from shared.remote_client import RemoteClient, RemoteClientError
    config, db, router, planner, orchestrator = _ensure_init()

    url = args.get("remote_url") or getattr(getattr(config, "remote_client", None), "url", "")
    token = args.get("remote_token") or getattr(getattr(config, "remote_client", None), "token", "")
    verify_tls = args.get("verify_tls")
    if verify_tls is None:
        verify_tls = getattr(getattr(config, "remote_client", None), "verify_tls", True)
    timeout = getattr(getattr(config, "remote_client", None), "timeout", 300)

    if not url:
        return {"error": "remote_url is required (or set remote_client.url in config.yaml)"}
    if not token:
        return {"error": "remote_token is required (or set remote_client.token in config.yaml)"}

    client = RemoteClient(url, token, verify_tls=bool(verify_tls), timeout=int(timeout))
    try:
        result = client.dispatch(
            args.get("task", ""),
            async_mode=bool(args.get("async_mode", False)),
            topology=str(args.get("topology", "")),
        )
        return result
    except RemoteClientError as exc:
        return {"error": str(exc), "status_code": exc.status}


def handle_remote_job_status(args: dict) -> dict:
    """Poll status of an async remote_dispatch job."""
    from shared.remote_client import RemoteClient, RemoteClientError
    config, db, router, planner, orchestrator = _ensure_init()

    url = args.get("remote_url") or getattr(getattr(config, "remote_client", None), "url", "")
    token = args.get("remote_token") or getattr(getattr(config, "remote_client", None), "token", "")
    verify_tls = args.get("verify_tls")
    if verify_tls is None:
        verify_tls = getattr(getattr(config, "remote_client", None), "verify_tls", True)
    timeout = getattr(getattr(config, "remote_client", None), "timeout", 300)

    if not url:
        return {"error": "remote_url is required (or set remote_client.url in config.yaml)"}
    if not token:
        return {"error": "remote_token is required (or set remote_client.token in config.yaml)"}

    client = RemoteClient(url, token, verify_tls=bool(verify_tls), timeout=int(timeout))
    try:
        return client.job_status(args.get("job_id", ""))
    except RemoteClientError as exc:
        return {"error": str(exc), "status_code": exc.status}


HANDLERS = {
    "plan_task":      handle_plan_task,
    "decompose_task": handle_plan_task,
    "fleet_plan":     handle_fleet_plan,
    "route_task":     handle_route_task,
    "validate_routing_guard": handle_validate_routing_guard,
    "cache_get":      handle_cache_get,
    "cache_put":      handle_cache_put,
    "cache_stats":    handle_cache_stats,
    "execute_subtask": handle_execute_subtask,
    "execute_swarm": handle_execute_swarm,
    "apply_preview": handle_apply_preview,
    "inspect_task": handle_inspect_task,
    "resume_swarm_inspect": handle_resume_swarm_inspect,
    "resume_swarm_confirm": handle_resume_swarm_confirm,
    "inspect_status": handle_inspect_status,
    "agent_queue_list": handle_approval_queue_list,
    "approval_queue_list": handle_approval_queue_list,
    "agent_queue_approve": handle_approval_queue_approve,
    "approval_queue_approve": handle_approval_queue_approve,
    "agent_queue_reject": handle_approval_queue_reject,
    "approval_queue_reject": handle_approval_queue_reject,
    "agent_queue_merge": handle_approval_queue_merge,
    "approval_queue_merge": handle_approval_queue_merge,
    "memory_list": handle_memory_list,
    "memory_get": handle_memory_get,
    "memory_set": handle_memory_set,
    "memory_delete": handle_memory_delete,
    "record_outcome": handle_record_outcome,
    "tune_show": handle_tune_show,
    "inspect_write_audit": handle_inspect_write_audit,
    "check_providers": handle_check_providers,
    "list_subtasks": handle_list_subtasks,
    "stop_subtask": handle_stop_subtask,
    "resume_subtask": handle_resume_subtask,
    "learning_agent_summary": handle_learning_agent_summary,
    "learning_outcome_stats": handle_learning_outcome_stats,
    "learning_pattern_health": handle_learning_pattern_health,
    "learning_audit_log": handle_learning_audit_log,
    "trace_show": handle_trace_show,
    "trace_replay": handle_trace_replay,
    "trace_fork": handle_trace_fork,
    "trace_diff": handle_trace_diff,
    "session_start": handle_session_start,
    "session_send": handle_session_send,
    "session_close": handle_session_close,
    "remote_dispatch": handle_remote_dispatch,
    "remote_job_status": handle_remote_job_status,
    "routing_exception_add": handle_routing_exception_add,
    "routing_exception_remove": handle_routing_exception_remove,
    "routing_exception_list": handle_routing_exception_list,
}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def handle_request(request: dict) -> None:
    req_id = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    if method == "initialize":
        global _client_name
        client_info = params.get("clientInfo", {})
        _client_name = client_info.get("name")
        log.info("MCP initialize — client: %s", _client_name)
        send_response(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "Threnody", "version": get_version()},
        })
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send_response(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        # Plumb _meta.progressToken so blocking tools can send heartbeats.
        raw_meta = params.get("_meta")
        meta = raw_meta if isinstance(raw_meta, Mapping) else {}
        _request_context.progress_token = _normalize_progress_token(meta.get("progressToken"))
        try:
            handler = HANDLERS.get(tool_name)
            if handler:
                _retryable = tool_name in _RETRYABLE_TOOLS
                _max_attempts = (_RETRY_LIMIT + 1) if _retryable else 1
                _retry_policy = _RetryPolicy(attempts=_max_attempts)
                _last_exc: Exception | None = None
                for _attempt in range(_max_attempts):
                    try:
                        result = handler(tool_args)
                        # execute_subtask may signal a retryable soft failure via
                        # result dict (e.g. malformed_output) without raising.
                        if (
                            _retryable
                            and isinstance(result, dict)
                            and result.get("retryable")
                            and _attempt < _max_attempts - 1
                        ):
                            log.warning(
                                "Tool %s soft-failure (attempt %d/%d): %s — retrying",
                                tool_name, _attempt + 1, _max_attempts,
                                result.get("error_category", "unknown"),
                            )
                            _retry_policy.wait(_attempt)
                            continue
                        send_response(req_id, {
                            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                        })
                        _last_exc = None
                        break
                    except Exception as e:
                        _last_exc = e
                        if _attempt < _max_attempts - 1:
                            log.warning(
                                "Tool %s failed (attempt %d/%d), retrying: %s",
                                tool_name, _attempt + 1, _max_attempts, e,
                            )
                            _retry_policy.wait(_attempt)
                if _last_exc is not None:
                    log.exception("Tool %s failed after %d attempt(s)", tool_name, _max_attempts, exc_info=_last_exc)
                    send_response(req_id, {
                        "content": [{"type": "text", "text": json.dumps({
                            "error": f"Tool '{tool_name}' failed — see server log for details.",
                        })}],
                        "isError": True,
                    })
            else:
                send_error(req_id, -32601, f"Unknown tool: {tool_name}")
        finally:
            _request_context.progress_token = None
    elif method == "ping":
        send_response(req_id, {})
    else:
        if req_id is not None:
            send_error(req_id, -32601, f"Method not found: {method}")


def _dispatch_request(request: dict) -> None:
    """Handle one request — runs in a worker thread for blocking tools."""
    try:
        handle_request(request)
    except Exception:
        log.exception("Unhandled error in dispatch thread")
        send_error(request.get("id"), -32603, "Internal error")


# Tools that call subprocesses and can block for 30–120 s.
_BLOCKING_TOOLS = frozenset({"execute_subtask", "plan_task", "decompose_task", "fleet_plan"})

# Tools that are safe to retry on transient failure (fast heuristic, no side-effects).
# execute_subtask is also retried when result dict carries retryable=True.
_RETRYABLE_TOOLS = frozenset({"route_task", "plan_task", "decompose_task", "execute_subtask"})
_RETRY_LIMIT = 2  # up to 2 retries = 3 total attempts


def main() -> None:
    log.info("Threnody MCP server %s — cross-provider orchestrator", get_version())
    dispatch_threads: list[threading.Thread] = []
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                send_error(None, -32700, "Parse error")
                continue

            # Dispatch blocking tools to worker threads so the main loop keeps
            # reading stdin and the client never times out waiting for fast calls.
            params = request.get("params", {}) if isinstance(request, dict) else {}
            tool_name = params.get("name", "") if isinstance(params, dict) else ""
            method = request.get("method", "") if isinstance(request, dict) else ""
            is_blocking = (
                method == "tools/call" and tool_name in _BLOCKING_TOOLS
            )
            if is_blocking:
                thread = threading.Thread(
                    target=_dispatch_request, args=(request,), daemon=True
                )
                dispatch_threads.append(thread)
                thread.start()
            else:
                try:
                    handle_request(request)
                except Exception:
                    log.exception("Unhandled error")
                    send_error(request.get("id"), -32603, "Internal error")
    finally:
        cancelled = _cancel_all_active_subtasks("transport_disconnect")
        if cancelled:
            log.info(
                "MCP transport closed; requested cancellation for %d active subtasks",
                cancelled,
            )
        for thread in dispatch_threads:
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
