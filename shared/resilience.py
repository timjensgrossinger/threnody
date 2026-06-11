#!/usr/bin/env python3
"""
Resilience primitives: error classification, retry policy, auth probing.

Used by shared/discovery.py execute paths to replace ad-hoc retry loops
with structured, configurable backoff and provider selection.
"""
from __future__ import annotations

import enum
import json
import logging
import os
import pathlib
import secrets
import subprocess
import time

log = logging.getLogger(__name__)

# Module-level auth-probe cache: provider_name → (ok, expires_at)
_AUTH_CACHE: dict[str, tuple[bool, float]] = {}
_AUTH_CACHE_TTL = 600.0


class ErrorCategory(str, enum.Enum):
    TRANSIENT_NETWORK = "transient_network"
    AUTH_EXPIRED = "auth_expired"
    QUOTA_EXCEEDED = "quota_exceeded"
    RATE_LIMITED = "rate_limited"
    BINARY_MISSING = "binary_missing"
    MALFORMED_OUTPUT = "malformed_output"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


# Categories for which retries make sense.
# TIMEOUT gets special handling via max_timeout_retries in RetryPolicy.
_RETRY_MAP: dict[ErrorCategory, bool] = {
    ErrorCategory.TRANSIENT_NETWORK: True,
    ErrorCategory.AUTH_EXPIRED: False,
    ErrorCategory.QUOTA_EXCEEDED: False,
    ErrorCategory.RATE_LIMITED: True,
    ErrorCategory.BINARY_MISSING: False,
    ErrorCategory.MALFORMED_OUTPUT: True,
    ErrorCategory.TIMEOUT: False,
    ErrorCategory.UNKNOWN: True,
}

# Categories that should trigger circuit-breaker increment.
BREAKER_CATEGORIES: frozenset[ErrorCategory] = frozenset({
    ErrorCategory.AUTH_EXPIRED,
    ErrorCategory.QUOTA_EXCEEDED,
    ErrorCategory.BINARY_MISSING,
    ErrorCategory.TIMEOUT,
    ErrorCategory.UNKNOWN,
})


def classify(
    returncode: int,
    stderr: str,
    stdout: str,
    timed_out: bool,
) -> ErrorCategory:
    """Map subprocess exit info to a structured ErrorCategory."""
    if timed_out:
        return ErrorCategory.TIMEOUT

    s = (stderr or "").lower()
    o = (stdout or "").lower()

    if returncode == 127 or "command not found" in s or "no such file" in s:
        return ErrorCategory.BINARY_MISSING

    if any(k in s for k in ("network", "econnreset", "dns ", "connection refused", "no route to host")):
        return ErrorCategory.TRANSIENT_NETWORK

    if any(k in s for k in ("unauthor", "expired", "invalid token", "login required", "401", "403", "authentication")):
        return ErrorCategory.AUTH_EXPIRED

    if any(k in s or k in o for k in ("quota", "limit reached", "insufficient_quota", "quota_exceeded", "billing")):
        return ErrorCategory.QUOTA_EXCEEDED

    if any(k in s for k in ("429", "rate limit", "too many requests", "ratelimit")):
        return ErrorCategory.RATE_LIMITED

    if returncode == 0 and not (stderr or "").strip() and not (stdout or "").strip():
        return ErrorCategory.MALFORMED_OUTPUT

    if returncode != 0:
        return ErrorCategory.UNKNOWN

    return ErrorCategory.UNKNOWN


class RetryPolicy:
    """Exponential backoff with jitter and per-category retry rules."""

    def __init__(
        self,
        attempts: int = 3,
        base_delay_s: float = 0.5,
        max_delay_s: float = 8.0,
        jitter_ratio: float = 0.3,
        max_timeout_retries: int = 1,
    ) -> None:
        self.attempts = attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.jitter_ratio = jitter_ratio
        self.max_timeout_retries = max_timeout_retries

    def sleep_for(self, attempt: int) -> float:
        """Return sleep duration in seconds for a given attempt index (0-based)."""
        delay = min(self.base_delay_s * (2 ** attempt), self.max_delay_s)
        jitter = delay * self.jitter_ratio * (secrets.randbelow(1001) / 500.0 - 1.0)
        return max(0.0, delay + jitter)

    def should_retry(
        self,
        category: ErrorCategory,
        timeout_retry_count: int = 0,
    ) -> bool:
        if category == ErrorCategory.TIMEOUT:
            return timeout_retry_count < self.max_timeout_retries
        return _RETRY_MAP.get(category, False)

    def wait(self, attempt: int) -> None:
        t = self.sleep_for(attempt)
        if t > 0:
            time.sleep(t)


_DEFAULT_POLICY = RetryPolicy()


def default_policy() -> RetryPolicy:
    """Return the module-level default RetryPolicy (lazy singleton)."""
    return _DEFAULT_POLICY


class AuthProbe:
    """Cheap, cached per-provider auth pre-flight checks."""

    TTL = _AUTH_CACHE_TTL

    @classmethod
    def check(cls, provider_name: str) -> bool:
        """Return True if provider appears authenticated. Cached for TTL seconds."""
        if os.environ.get("THRENODY_TEST_MODE") == "1":
            return True

        key = provider_name.lower()
        entry = _AUTH_CACHE.get(key)
        if entry is not None and entry[1] > time.time():
            return entry[0]

        ok = cls._probe(key)
        _AUTH_CACHE[key] = (ok, time.time() + cls.TTL)
        return ok

    @classmethod
    def _probe(cls, key: str) -> bool:
        try:
            if key in ("github-copilot", "gh-copilot", "copilot"):
                result = subprocess.run(
                    ["gh", "auth", "status", "--hostname", "github.com"],
                    capture_output=True,
                    timeout=5,
                )
                return result.returncode == 0

            if key in ("claude-code", "claude"):
                if os.environ.get("ANTHROPIC_API_KEY"):
                    return True
                try:
                    result = subprocess.run(
                        ["claude", "auth", "status"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    result = None
                if result is not None:
                    if result.returncode != 0:
                        return False
                    try:
                        payload = json.loads(result.stdout or "{}")
                    except json.JSONDecodeError:
                        return True
                    return payload.get("loggedIn") is not False
                cred_paths = [
                    pathlib.Path.home() / ".claude" / ".credentials.json",
                    pathlib.Path.home() / ".config" / "claude" / "credentials.json",
                ]
                return any(p.exists() and p.stat().st_size > 10 for p in cred_paths)

        except Exception:
            log.warning("auth probe failed for provider %r", key, exc_info=True)
            return False

        # Unknown provider — assume ok; execute() will surface real failures.
        return True

    @classmethod
    def invalidate(cls, provider_name: str) -> None:
        """Drop cached result for a provider (e.g. after explicit auth failure)."""
        _AUTH_CACHE.pop(provider_name.lower(), None)
