"""
Threnody remote client.

Thin urllib-based HTTP client for calling a remote Threnody server.
No external dependencies — stdlib only.
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

_VERSION = "1.0.0"


class RemoteClientError(Exception):
    """Raised when the remote server returns a non-2xx response or a network error."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RemoteClient:
    """HTTP client for a remote Threnody server."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        verify_tls: bool = True,
        timeout: int = 300,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.verify_tls = verify_tls
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ssl_ctx(self) -> ssl.SSLContext | None:
        if not self.url.startswith("https"):
            return None
        ctx = ssl.create_default_context()
        if not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.url}{path}"
        data = json.dumps(body).encode("utf-8")
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": f"Threnody-client/{_VERSION}",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx(), timeout=self.timeout) as resp:
                return json.loads(resp.read(16 * 1024 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = ""
            try:
                body_text = exc.read(64 * 1024).decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RemoteClientError(
                f"Remote server returned HTTP {exc.code}: {body_text}",
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise RemoteClientError(f"Network error reaching {url}: {exc.reason}") from exc

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self.url}{path}"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.token}",
                "User-Agent": f"Threnody-client/{_VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx(), timeout=self.timeout) as resp:
                return json.loads(resp.read(16 * 1024 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = ""
            try:
                body_text = exc.read(64 * 1024).decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RemoteClientError(
                f"Remote server returned HTTP {exc.code}: {body_text}",
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise RemoteClientError(f"Network error reaching {url}: {exc.reason}") from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Unauthenticated health check."""
        url = f"{self.url}/health"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx(), timeout=10) as resp:
                return json.loads(resp.read(16 * 1024 * 1024).decode("utf-8"))
        except Exception as exc:
            raise RemoteClientError(f"Health check failed: {exc}") from exc

    def route(self, task: str) -> dict[str, Any]:
        """Route a task and return tier/model recommendation."""
        return self._post("/v1/route", {"task": task})

    def decompose(self, task: str) -> dict[str, Any]:
        """Decompose a task into subtasks via the remote planner."""
        return self._post("/v1/decompose", {"task": task})

    def execute(
        self,
        prompt: str,
        tier: str = "low",
        target_file: str = "",
    ) -> dict[str, Any]:
        """Execute a single prompt on the remote server."""
        body: dict[str, Any] = {"prompt": prompt, "tier": tier}
        if target_file:
            body["target_file"] = target_file
        return self._post("/v1/execute", body)

    def dispatch(
        self,
        task: str,
        *,
        async_mode: bool = False,
        topology: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Full plan+execute dispatch. Returns result or {"job_id": ...} if async."""
        body: dict[str, Any] = {"task": task, "async": async_mode}
        if topology:
            body["topology"] = topology
        extra: dict[str, str] | None = (
            {"Idempotency-Key": idempotency_key} if idempotency_key else None
        )
        return self._post("/v1/dispatch", body, extra_headers=extra)

    def job_status(self, job_id: str) -> dict[str, Any]:
        """Poll async job status."""
        return self._get(f"/v1/job/{job_id}")
