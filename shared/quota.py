"""Provider-reported subscription quota collection and normalization."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

SUPPORTED_PROVIDER_IDS = frozenset({"codex"})
BUILTIN_PROVIDER_IDS = frozenset({
    "github-copilot",
    "claude-code",
    "codex",
    "junie",
    "opencode",
    "cursor",
    "aider",
    "amazon-q",
    "mistral-vibe",
    "blackbox-ai",
    "windsurf",
})

UNSUPPORTED_REASONS = {
    "claude-code": (
        "Claude Code exposes interactive usage views and limit errors, but no "
        "documented machine-readable subscription quota API"
    ),
    "github-copilot": (
        "Copilot CLI exposes interactive /usage and status-line quota displays, "
        "but no documented structured quota command"
    ),
    "junie": (
        "JetBrains exposes quota in IDE and Junie license UI, but no documented "
        "machine-readable Junie CLI quota command"
    ),
    "opencode": (
        "OpenCode stats reports local token and cost telemetry, not an upstream "
        "subscription allowance or remaining quota"
    ),
    "cursor": "Cursor exposes account usage UI but no documented machine-readable CLI quota API",
    "aider": "Aider delegates billing and quota to separately configured upstream providers",
    "amazon-q": (
        "Kiro CLI documents interactive /usage with remaining credits, but no "
        "documented structured quota command"
    ),
    "mistral-vibe": "No documented machine-readable Vibe subscription quota command",
    "blackbox-ai": "No documented machine-readable Blackbox CLI subscription quota command",
    "windsurf": "No documented machine-readable Windsurf CLI subscription quota command",
}


@dataclass(frozen=True)
class ProviderQuotaSnapshot:
    """One provider-reported quota window.

    Percentage-only providers use ``unit="percent"`` and ``limit=100``. This
    deliberately avoids fabricating an absolute token or request allowance.
    """

    provider: str
    window_name: str
    window_duration_seconds: float | None
    used: float | None
    remaining: float | None
    limit: float | None
    unit: str
    reset_timestamp: float | None
    observed_timestamp: float
    source: str
    confidence: str = "confirmed"
    freshness_seconds: float = 0.0

    @property
    def used_ratio(self) -> float | None:
        if self.used is not None and self.limit not in (None, 0):
            return max(0.0, min(1.0, self.used / self.limit))
        if self.remaining is not None and self.limit not in (None, 0):
            return max(0.0, min(1.0, 1.0 - self.remaining / self.limit))
        return None

    def to_dict(self, *, now: float | None = None) -> dict[str, object]:
        payload = asdict(self)
        payload["freshness_seconds"] = max(
            0.0, (time.time() if now is None else now) - self.observed_timestamp
        )
        payload["used_ratio"] = self.used_ratio
        return payload


@dataclass(frozen=True)
class ProviderQuotaResult:
    provider: str
    status: str
    snapshots: tuple[ProviderQuotaSnapshot, ...] = ()
    source: str = "unsupported"
    observed_timestamp: float = 0.0
    error: str | None = None
    cached: bool = False

    def to_dict(self, *, now: float | None = None) -> dict[str, object]:
        current = time.time() if now is None else now
        return {
            "provider": self.provider,
            "status": self.status,
            "source": self.source,
            "observed_timestamp": self.observed_timestamp,
            "freshness_seconds": (
                max(0.0, current - self.observed_timestamp)
                if self.observed_timestamp
                else None
            ),
            "cached": self.cached,
            "error": self.error,
            "windows": [snapshot.to_dict(now=current) for snapshot in self.snapshots],
        }


class ProviderQuotaAdapter:
    provider_id = ""
    source = "unsupported"

    def collect(self, *, now: float | None = None) -> ProviderQuotaResult:
        raise NotImplementedError


class UnsupportedQuotaAdapter(ProviderQuotaAdapter):
    def __init__(self, provider_id: str, reason: str | None = None) -> None:
        self.provider_id = provider_id
        self.reason = reason or UNSUPPORTED_REASONS.get(
            provider_id,
            "provider exposes no documented machine-readable subscription quota API",
        )

    def collect(self, *, now: float | None = None) -> ProviderQuotaResult:
        observed = time.time() if now is None else now
        return ProviderQuotaResult(
            provider=self.provider_id,
            status="unsupported",
            source="unsupported",
            observed_timestamp=observed,
            error=self.reason,
        )


class CodexQuotaAdapter(ProviderQuotaAdapter):
    """Read ChatGPT Codex rate limits through the documented app-server RPC."""

    provider_id = "codex"
    source = "codex_app_server:account/rateLimits/read"

    def __init__(
        self,
        *,
        binary: str = "codex",
        timeout_seconds: float = 8.0,
        rpc_runner: Callable[[list[dict[str, object]], float], dict[str, Any]] | None = None,
    ) -> None:
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self._rpc_runner = rpc_runner or self._run_rpc

    def collect(self, *, now: float | None = None) -> ProviderQuotaResult:
        observed = time.time() if now is None else now
        if shutil.which(self.binary) is None:
            return self._error("unavailable", "Codex CLI is not installed", observed)
        requests = [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "threnody", "title": "Threnody", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            },
            {"id": 2, "method": "account/rateLimits/read", "params": None},
        ]
        try:
            response = self._rpc_runner(requests, self.timeout_seconds)
        except subprocess.TimeoutExpired:
            return self._error("unavailable", "Codex quota request timed out", observed)
        except FileNotFoundError:
            return self._error("unavailable", "Codex CLI is not installed", observed)
        except PermissionError:
            return self._error("auth_error", "Codex quota request was not authorized", observed)
        except Exception as exc:
            log.debug("Codex quota collection failed", exc_info=True)
            return self._error("unavailable", f"Codex quota request failed: {type(exc).__name__}", observed)

        if "error" in response:
            error = response.get("error")
            text = json.dumps(error, sort_keys=True).lower()
            status = "auth_error" if any(word in text for word in ("auth", "login", "unauthorized", "forbidden")) else "unavailable"
            if "rate" in text and "limit" in text:
                status = "rate_limited"
            return self._error(status, "Codex app-server rejected the quota request", observed)

        result = response.get("result")
        if not isinstance(result, dict):
            return self._error("malformed", "Codex quota response has no result object", observed)
        snapshots = tuple(self._parse_response(result, observed))
        if not snapshots:
            return self._error("unavailable", "Codex returned no subscription quota windows", observed)
        return ProviderQuotaResult(
            provider=self.provider_id,
            status="supported",
            snapshots=snapshots,
            source=self.source,
            observed_timestamp=observed,
        )

    def _error(self, status: str, error: str, observed: float) -> ProviderQuotaResult:
        return ProviderQuotaResult(
            provider=self.provider_id,
            status=status,
            source=self.source,
            observed_timestamp=observed,
            error=error,
        )

    def _parse_response(
        self, result: dict[str, Any], observed: float
    ) -> Iterable[ProviderQuotaSnapshot]:
        records: list[tuple[str, dict[str, Any]]] = []
        primary = result.get("rateLimits")
        if isinstance(primary, dict):
            records.append(("default", primary))
        by_id = result.get("rateLimitsByLimitId")
        if isinstance(by_id, dict):
            records.extend(
                (str(limit_id), value)
                for limit_id, value in by_id.items()
                if isinstance(value, dict)
            )

        seen: set[tuple[str, float | None, float | None]] = set()
        for fallback_name, record in records:
            limit_name = str(record.get("limitName") or record.get("limitId") or fallback_name)
            for window_key in ("primary", "secondary"):
                window = record.get(window_key)
                if not isinstance(window, dict):
                    continue
                used_percent = window.get("usedPercent")
                if not isinstance(used_percent, (int, float)):
                    continue
                duration_minutes = window.get("windowDurationMins")
                duration = (
                    float(duration_minutes) * 60.0
                    if isinstance(duration_minutes, (int, float))
                    else None
                )
                reset = window.get("resetsAt")
                reset_ts = float(reset) if isinstance(reset, (int, float)) else None
                dedupe_key = (f"{limit_name}:{window_key}", duration, reset_ts)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                used = max(0.0, min(100.0, float(used_percent)))
                yield ProviderQuotaSnapshot(
                    provider=self.provider_id,
                    window_name=f"{limit_name}:{window_key}",
                    window_duration_seconds=duration,
                    used=used,
                    remaining=100.0 - used,
                    limit=100.0,
                    unit="percent",
                    reset_timestamp=reset_ts,
                    observed_timestamp=observed,
                    source=self.source,
                )

    def _run_rpc(
        self, requests: list[dict[str, object]], timeout_seconds: float
    ) -> dict[str, Any]:
        input_text = "".join(json.dumps(request) + "\n" for request in requests)
        completed = subprocess.run(
            [self.binary, "app-server", "--listen", "stdio://"],
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        response: dict[str, Any] | None = None
        for line in completed.stdout.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("id") == 2:
                response = payload
                break
        if response is None:
            stderr = completed.stderr.lower()
            if any(word in stderr for word in ("unauthorized", "not logged in", "authentication")):
                raise PermissionError("Codex is not authenticated")
            raise ValueError("Codex app-server returned no quota response")
        return response


class ProviderQuotaService:
    """TTL cache, persistence, and adapter registry for provider quota."""

    def __init__(
        self,
        db: Any | None = None,
        *,
        ttl_seconds: float = 60.0,
        adapters: dict[str, ProviderQuotaAdapter] | None = None,
    ) -> None:
        self.db = db
        self.ttl_seconds = ttl_seconds
        self.adapters = adapters or {
            provider_id: (
                CodexQuotaAdapter()
                if provider_id == "codex"
                else UnsupportedQuotaAdapter(provider_id)
            )
            for provider_id in BUILTIN_PROVIDER_IDS
        }
        self._cache: dict[str, tuple[float, ProviderQuotaResult]] = {}
        self._lock = threading.Lock()

    def get(self, provider_id: str, *, force: bool = False) -> ProviderQuotaResult:
        normalized = provider_id.strip().lower()
        now = time.time()
        with self._lock:
            cached = self._cache.get(normalized)
            if not force and cached is not None and now < cached[0]:
                result = cached[1]
                return ProviderQuotaResult(**{**result.__dict__, "cached": True})
        adapter = self.adapters.get(normalized) or UnsupportedQuotaAdapter(normalized)
        result = adapter.collect(now=now)
        with self._lock:
            self._cache[normalized] = (now + self.ttl_seconds, result)
        self._persist(result)
        return result

    def _persist(self, result: ProviderQuotaResult) -> None:
        if self.db is None:
            return
        try:
            self.db.record_provider_quota_observation(result.to_dict())
        except Exception:
            log.warning("Failed to persist provider quota observation", exc_info=True)


def iso_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
