"""
Threnody remote server.

Exposes routing, planning, and execution over HTTP(S) with Bearer token auth.
Start with:  threnody serve [--port 8765] [--no-tls] [--token TOKEN]

Supports multiple users: register users via ``threnody users add``.  Each user
authenticates with their own Bearer token and executes tasks through their own
stored CLI credentials.

Requires the Threnody install dir on sys.path (set by threnody serve / install.sh).
"""
from __future__ import annotations

import argparse
import dataclasses
import errno
import hmac
import json
import logging
import os
import secrets
import socket
import ssl
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

log = logging.getLogger(__name__)

_VERSION = "3.2.0-alpha.1"
_RATE_WINDOW = 10       # seconds
_RATE_MAX = 20          # requests per window per IP / user token
_MAX_BODY = 4 * 1024 * 1024  # 4 MB max request body

def _safe_write_text(path: Path, content: str) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise OSError(f"Refusing to write through symlink path: {candidate}")

    path.parent.mkdir(parents=True, exist_ok=True)
    oflags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        oflags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), oflags, 0o666)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OSError(f"Refusing to write through symlink path: {path}") from exc
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, window: int = _RATE_WINDOW, max_calls: int = _RATE_MAX) -> None:
        self._window = window
        self._max = max_calls
        self._lock = threading.Lock()
        self._buckets: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            calls = self._buckets.get(key, [])
            calls = [t for t in calls if now - t < self._window]
            if len(calls) >= self._max:
                return False
            calls.append(now)
            self._buckets[key] = calls
            return True


# ---------------------------------------------------------------------------
# Threaded HTTP server
# ---------------------------------------------------------------------------

class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Handles all incoming HTTP requests."""

    # Injected by the server builder
    _admin_token: str = ""
    _rate: _RateLimiter = _RateLimiter()
    _executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=8)
    _db: Any = None  # shared/db.py Database instance

    def log_message(self, fmt: str, *args: object) -> None:
        log.debug("%s - " + fmt, self.address_string(), *args)

    # ------------------------------------------------------------------
    # Auth & rate-limit helpers
    # ------------------------------------------------------------------

    def _client_ip(self) -> str:
        return self.client_address[0]

    def _check_auth(self) -> tuple[str, str | None] | None:
        """Authenticate the request.

        Returns:
            ``("admin", None)``        — valid admin token.
            ``("user", user_id)``      — valid, enabled user token.
            ``None``                   — authentication failed.
        """
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        provided = auth[7:]
        if not provided:
            return None

        # Admin token check (constant-time)
        if self._admin_token and hmac.compare_digest(provided, self._admin_token):
            return ("admin", None)

        # User token check: HMAC the provided token against the admin secret
        # and look it up in the database.
        if self._db is not None and self._admin_token:
            token_hmac = _hmac_token(provided, self._admin_token)
            try:
                user = self._db.get_user_by_token_hmac(token_hmac)
            except Exception:
                log.debug("user token lookup failed", exc_info=True)
                user = None
            if user is not None and user.get("enabled") and user.get("user_id") is not None:
                return ("user", user.get("user_id"))

        return None

    def _check_rate(self, rate_key: str) -> bool:
        return self._rate.allow(rate_key)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-TGs-Router-Version", _VERSION)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            length = 0
        if length > _MAX_BODY:
            self._send_json(413, {"error": "Request body too large"})
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return None

    def _guard(self) -> tuple[str, str | None] | None:
        """Check rate limit and auth.

        Returns the ``(role, user_id)`` tuple on success, or ``None`` after
        sending an error response.
        """
        auth_result = self._check_auth()
        # Rate-limit key: user_id for users, IP address for admin/anonymous
        if auth_result is not None and auth_result[0] == "user":
            rate_key = f"user:{auth_result[1]}"
        else:
            rate_key = self._client_ip()

        if not self._check_rate(rate_key):
            self._send_json(429, {"error": "Rate limit exceeded"})
            return None
        if auth_result is None:
            self._send_json(401, {"error": "Unauthorized"})
            return None
        return auth_result

    # ------------------------------------------------------------------
    # GET /health  (unauthenticated)
    # ------------------------------------------------------------------

    def _handle_health(self) -> None:
        self._send_json(200, {"status": "ok", "version": _VERSION})

    # ------------------------------------------------------------------
    # POST /v1/route
    # ------------------------------------------------------------------

    def _handle_route(self, body: dict[str, Any], _role: str, _user_id: str | None) -> None:
        task = body.get("task", "")
        if not task:
            self._send_json(400, {"error": "task is required"})
            return
        try:
            from shared.router import TaskRouter
            from shared.config import TGsConfig
            cfg = TGsConfig.from_yaml()
            router = TaskRouter(cfg)
            result = router.route(task)
            self._send_json(200, result if isinstance(result, dict) else {"tier": result})
        except Exception as exc:
            log.exception("route error")
            self._send_json(500, {"error": str(exc)})

    # ------------------------------------------------------------------
    # POST /v1/decompose
    # ------------------------------------------------------------------

    def _handle_decompose(self, body: dict[str, Any], _role: str, _user_id: str | None) -> None:
        task = body.get("task", "")
        if not task:
            self._send_json(400, {"error": "task is required"})
            return
        try:
            from shared.config import TGsConfig
            from shared.planner import Planner, ProviderAgnosticBackend, GhCopilotBackend
            from shared.discovery import get_registry
            from shared.db import Database
            cfg = TGsConfig.from_yaml()
            db = self._db or Database()
            try:
                backend = ProviderAgnosticBackend(get_registry())
            except Exception:
                log.warning("ProviderRegistry unavailable for planner, falling back to GhCopilotBackend")
                backend = GhCopilotBackend()
            planner = Planner(cfg, backend, db)
            plan = planner.decompose(task)
            self._send_json(200, plan.to_dict() if hasattr(plan, "to_dict") else {"plan": str(plan)})
        except Exception as exc:
            log.exception("decompose error")
            self._send_json(500, {"error": str(exc)})

    # ------------------------------------------------------------------
    # POST /v1/execute
    # ------------------------------------------------------------------

    def _handle_execute(self, body: dict[str, Any], _role: str, user_id: str | None) -> None:
        prompt = body.get("prompt", "")
        tier = body.get("tier", "low")
        target_file = str(body.get("target_file", "") or "").strip()
        trusted_root = os.getcwd()
        requested_workspace = str(body.get("cwd") or body.get("workspace") or trusted_root)
        if not prompt:
            self._send_json(400, {"error": "prompt is required"})
            return
        try:
            resolved_target_file: Path | None = None
            if target_file:
                from shared.context import is_within_repo, normalize_target_path

                try:
                    workspace_root = normalize_target_path(requested_workspace, trusted_root)
                except ValueError as exc:
                    self._send_json(400, {"error": "PathTraversalRejected", "details": str(exc)})
                    return
                if not is_within_repo(workspace_root, trusted_root):
                    self._send_json(
                        400,
                        {
                            "error": "PathTraversalRejected",
                            "details": f"workspace resolves outside server root {trusted_root}",
                        },
                    )
                    return
                try:
                    resolved_target_file = normalize_target_path(target_file, workspace_root)
                except ValueError as exc:
                    self._send_json(400, {"error": "PathTraversalRejected", "details": str(exc)})
                    return
                if not is_within_repo(resolved_target_file, workspace_root):
                    self._send_json(
                        400,
                        {
                            "error": "PathTraversalRejected",
                            "details": f"target_file resolves outside workspace {workspace_root}",
                        },
                    )
                    return

            from shared.discovery import ProviderRegistry, _user_subprocess_env
            from shared.config import TGsConfig
            cfg = TGsConfig.from_yaml()
            registry = ProviderRegistry(config_overrides=dataclasses.asdict(cfg), db=self._db)
            provider = registry.cheapest_for_tier(tier)
            if provider is None:
                self._send_json(503, {"error": f"No provider available for tier {tier!r}"})
                return
            model = provider.tier_models.get(tier, "")
            env_overrides = _build_user_env(self._db, user_id)
            result_text = provider.execute(prompt, model, env_overrides=env_overrides)
            if resolved_target_file is not None and result_text:
                try:
                    _safe_write_text(resolved_target_file, result_text)
                except OSError as exc:
                    self._send_json(400, {"error": "WriteError", "details": str(exc)})
                    return
            self._send_json(200, {
                "result": result_text or "",
                "provider": provider.name,
                "tier": tier,
                "file_written": str(resolved_target_file) if resolved_target_file is not None and result_text else "",
            })
        except Exception as exc:
            log.exception("execute error")
            self._send_json(500, {"error": str(exc)})

    # ------------------------------------------------------------------
    # POST /v1/dispatch  (full plan + execute, sync or async)
    # ------------------------------------------------------------------

    def _handle_dispatch(self, body: dict[str, Any], _role: str, user_id: str | None) -> None:
        task = body.get("task", "")
        async_mode = bool(body.get("async", False))
        topology = body.get("topology", "")
        if not task:
            self._send_json(400, {"error": "task is required"})
            return

        idempotency_key: str | None = self.headers.get("Idempotency-Key") or None

        env_overrides = _build_user_env(self._db, user_id)

        if async_mode:
            job_id = str(uuid.uuid4())
            db = self._db
            if db is not None:
                db.create_remote_job(job_id, task, user_id=user_id, idempotency_key=idempotency_key)
            self._executor.submit(
                self._run_dispatch_job, job_id, task, topology, db, env_overrides
            )
            self._send_json(202, {"job_id": job_id, "status": "pending"})
        else:
            try:
                result = _run_full_dispatch(task, topology, env_overrides=env_overrides)
                self._send_json(200, {"status": "completed", "result": result})
            except Exception as exc:
                log.exception("sync dispatch error")
                self._send_json(500, {"error": str(exc)})

    @staticmethod
    def _run_dispatch_job(
        job_id: str,
        task: str,
        topology: str,
        db: Any,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        if db is not None:
            db.update_remote_job(job_id, "running")

        # Acquire a server-side lease so a coordinator can detect crashes.
        worker_id = f"{socket.gethostname()}:{os.getpid()}:job-{job_id}"
        lease_ttl = 120
        lease_acquired = False
        stop_heartbeat = threading.Event()

        if db is not None:
            try:
                lease_acquired = db.acquire_lease(job_id, worker_id, lease_ttl)
            except Exception:
                log.debug("Lease acquire failed for job %s", job_id, exc_info=True)

        def _hb_loop() -> None:
            while not stop_heartbeat.wait(30):
                try:
                    db.heartbeat(job_id, worker_id)
                except Exception:
                    log.debug("Heartbeat failed for job %s", job_id, exc_info=True)

        hb_thread: threading.Thread | None = None
        if lease_acquired and db is not None:
            hb_thread = threading.Thread(
                target=_hb_loop, daemon=True, name=f"lease-hb-job-{job_id}"
            )
            hb_thread.start()

        try:
            result = _run_full_dispatch(task, topology, env_overrides=env_overrides)
            if db is not None:
                db.update_remote_job(job_id, "completed", result=result)
        except Exception as exc:
            log.exception("async dispatch job %s failed", job_id)
            if db is not None:
                db.update_remote_job(job_id, "failed", error=str(exc))
        finally:
            stop_heartbeat.set()
            if hb_thread is not None:
                hb_thread.join(timeout=2)
            if lease_acquired and db is not None:
                try:
                    db.release_lease(job_id, worker_id)
                except Exception:
                    log.debug("Lease release failed for job %s", job_id, exc_info=True)

    # ------------------------------------------------------------------
    # GET /v1/job/{id}
    # ------------------------------------------------------------------

    def _handle_job_status(self, job_id: str, role: str, user_id: str | None) -> None:
        db = self._db
        if db is None:
            self._send_json(503, {"error": "Job storage not available"})
            return
        # Admin can access any job; users only see their own
        scoped_user_id = user_id if role == "user" else None
        row = db.get_remote_job(job_id, user_id=scoped_user_id)
        if row is None:
            self._send_json(404, {"error": f"Job {job_id!r} not found"})
            return
        self._send_json(200, row)

    # ------------------------------------------------------------------
    # GET /v1/jobs
    # ------------------------------------------------------------------

    def _handle_list_jobs(self, role: str, user_id: str | None) -> None:
        db = self._db
        if db is None:
            self._send_json(503, {"error": "Job storage not available"})
            return
        try:
            if role == "admin":
                jobs = db.list_all_jobs()
            else:
                jobs = db.list_user_jobs(user_id or "")
            self._send_json(200, {"jobs": jobs})
        except Exception as exc:
            log.exception("list jobs error")
            self._send_json(500, {"error": str(exc)})

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/health":
            self._handle_health()
            return
        auth = self._guard()
        if auth is None:
            return
        role, user_id = auth
        if path.startswith("/v1/job/"):
            job_id = path[len("/v1/job/"):]
            if not job_id:
                self._send_json(400, {"error": "job_id required"})
                return
            self._handle_job_status(job_id, role, user_id)
            return
        if path == "/v1/jobs":
            self._handle_list_jobs(role, user_id)
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        auth = self._guard()
        if auth is None:
            return
        role, user_id = auth
        body = self._read_body()
        if body is None:
            return
        routes = {
            "/v1/route": self._handle_route,
            "/v1/decompose": self._handle_decompose,
            "/v1/execute": self._handle_execute,
            "/v1/dispatch": self._handle_dispatch,
        }
        handler = routes.get(path)
        if handler is None:
            self._send_json(404, {"error": "Not found"})
            return
        handler(body, role, user_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hmac_token(token: str, secret: str) -> str:
    """Return the HMAC-SHA256 hex digest of *token* keyed with *secret*."""
    import hashlib
    return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def _build_user_env(
    db: Any,
    user_id: str | None,
) -> dict[str, str] | None:
    """Return per-user env overrides, or None for admin/anonymous callers."""
    if user_id is None or db is None:
        return None
    try:
        user = db.get_user_by_id(user_id)
        if user is None:
            return None
        from shared.discovery import _user_subprocess_env
        return _user_subprocess_env(user_id, user.get("providers_json") or "{}")
    except Exception:
        log.debug("failed to build user env for %s", user_id, exc_info=True)
        return None


def _run_full_dispatch(
    task: str,
    topology: str = "",
    env_overrides: dict[str, str] | None = None,
) -> str:
    """Run the full plan+execute pipeline synchronously. Returns result text."""
    from shared.config import TGsConfig
    from shared.planner import Planner, ProviderAgnosticBackend, GhCopilotBackend
    from shared.orchestrator import Orchestrator
    from shared.discovery import ProviderRegistry, get_registry
    from shared.provider_factory import resolve_default_provider
    from shared.db import Database

    cfg = TGsConfig.from_yaml()
    db = Database()
    registry = ProviderRegistry(config_overrides=dataclasses.asdict(cfg), db=db)
    try:
        backend = ProviderAgnosticBackend(get_registry())
    except Exception:
        log.warning("ProviderRegistry unavailable for planner, falling back to GhCopilotBackend")
        backend = GhCopilotBackend()
    planner = Planner(cfg, backend, db)
    provider = resolve_default_provider(registry)
    orchestrator = Orchestrator(cfg, provider, planner, db, provider_registry=registry)

    plan = planner.decompose(task)
    if topology and hasattr(plan, "topology"):
        object.__setattr__(plan, "topology", topology)

    # Pass env_overrides through to the orchestrator if it supports it
    if env_overrides and hasattr(orchestrator, "execute"):
        import inspect as _inspect
        sig = _inspect.signature(orchestrator.execute)
        if "env_overrides" in sig.parameters:
            result = orchestrator.execute(plan, env_overrides=env_overrides)
        else:
            result = orchestrator.execute(plan)
    else:
        result = orchestrator.execute(plan)

    if isinstance(result, dict):
        return json.dumps(result)
    return str(result)


# ---------------------------------------------------------------------------
# Server builder
# ---------------------------------------------------------------------------

def build_server(
    host: str,
    port: int,
    token: str,
    *,
    tls_cert: str = "",
    tls_key: str = "",
    no_tls: bool = False,
    db: Any = None,
) -> _ThreadingHTTPServer:
    # Shared state injected into handler class
    rate = _RateLimiter()
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tgs-remote-job")

    if db is None:
        try:
            from shared.db import Database
            db = Database()
        except Exception:
            log.warning("Could not initialise Database; async jobs will not persist")

    class _BoundHandler(_Handler):
        pass

    _BoundHandler._admin_token = token
    _BoundHandler._rate = rate
    _BoundHandler._executor = executor
    _BoundHandler._db = db

    server = _ThreadingHTTPServer((host, port), _BoundHandler)

    if not no_tls and tls_cert and tls_key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_cert, tls_key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        log.info("TLS enabled — cert=%s", tls_cert)
    elif not no_tls and (tls_cert or tls_key):
        log.warning("TLS: both --tls-cert and --tls-key required; running plain HTTP")

    return server


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="threnody serve",
        description="Start Threnody as an HTTP(S) server.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Listen port (default: 8765)")
    parser.add_argument("--token", default="", help="Bearer token for auth (generated if omitted)")
    parser.add_argument("--tls-cert", default="", metavar="FILE", help="Path to TLS certificate PEM")
    parser.add_argument("--tls-key", default="", metavar="FILE", help="Path to TLS private key PEM")
    parser.add_argument(
        "--no-tls",
        action="store_true",
        help="Run plain HTTP without TLS (for LAN / behind reverse proxy)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    token = args.token or os.environ.get("THRENODY_SERVER_TOKEN") or os.environ.get("SWITCHYARD_SERVER_TOKEN", "")
    if not token:
        token = secrets.token_urlsafe(32)
        print(f"[threnody serve] No token provided — generated token: {token}")
        print("[threnody serve] Set THRENODY_SERVER_TOKEN (or deprecated SWITCHYARD_SERVER_TOKEN) or pass --token to reuse this token.")

    scheme = "http" if args.no_tls or not (args.tls_cert and args.tls_key) else "https"
    log.info("Starting Threnody remote server %s on %s://%s:%d", _VERSION, scheme, args.host, args.port)

    server = build_server(
        args.host,
        args.port,
        token,
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
        no_tls=args.no_tls,
    )

    print(f"[threnody serve] Listening on {scheme}://{args.host}:{args.port}")
    print(f"[threnody serve] Health: {scheme}://127.0.0.1:{args.port}/health")
    print("[threnody serve] Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[threnody serve] Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
