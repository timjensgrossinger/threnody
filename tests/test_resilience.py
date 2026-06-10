#!/usr/bin/env python3
"""Tests for shared/resilience.py — RetryPolicy, classify, AuthProbe."""
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("THRENODY_TEST_MODE", "1")

from shared.resilience import (
    AuthProbe,
    ErrorCategory,
    RetryPolicy,
    classify,
)


# ---------------------------------------------------------------------------
# RetryPolicy
# ---------------------------------------------------------------------------

def test_sleep_for_zero_attempt():
    p = RetryPolicy(base_delay_s=1.0, max_delay_s=8.0, jitter_ratio=0.0)
    assert p.sleep_for(0) == 1.0


def test_sleep_for_caps_at_max():
    p = RetryPolicy(base_delay_s=1.0, max_delay_s=4.0, jitter_ratio=0.0)
    assert p.sleep_for(10) == 4.0


def test_sleep_for_never_negative():
    p = RetryPolicy(base_delay_s=0.5, max_delay_s=8.0, jitter_ratio=1.0)
    for attempt in range(5):
        assert p.sleep_for(attempt) >= 0.0


def test_should_retry_transient():
    p = RetryPolicy()
    assert p.should_retry(ErrorCategory.TRANSIENT_NETWORK) is True


def test_should_not_retry_auth():
    p = RetryPolicy()
    assert p.should_retry(ErrorCategory.AUTH_EXPIRED) is False


def test_should_not_retry_quota():
    p = RetryPolicy()
    assert p.should_retry(ErrorCategory.QUOTA_EXCEEDED) is False


def test_should_not_retry_binary_missing():
    p = RetryPolicy()
    assert p.should_retry(ErrorCategory.BINARY_MISSING) is False


def test_timeout_retry_within_limit():
    p = RetryPolicy(max_timeout_retries=1)
    assert p.should_retry(ErrorCategory.TIMEOUT, timeout_retry_count=0) is True
    assert p.should_retry(ErrorCategory.TIMEOUT, timeout_retry_count=1) is False


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

def test_classify_timeout():
    cat = classify(1, "", "", timed_out=True)
    assert cat == ErrorCategory.TIMEOUT


def test_classify_binary_missing_returncode():
    cat = classify(127, "", "", timed_out=False)
    assert cat == ErrorCategory.BINARY_MISSING


def test_classify_binary_missing_stderr():
    cat = classify(1, "command not found: gh", "", timed_out=False)
    assert cat == ErrorCategory.BINARY_MISSING


def test_classify_auth_expired_401():
    cat = classify(1, "error: 401 unauthorized", "", timed_out=False)
    assert cat == ErrorCategory.AUTH_EXPIRED


def test_classify_auth_expired_token():
    cat = classify(1, "your token has expired, please login again", "", timed_out=False)
    assert cat == ErrorCategory.AUTH_EXPIRED


def test_classify_quota_exceeded():
    cat = classify(1, "you have exceeded your quota", "", timed_out=False)
    assert cat == ErrorCategory.QUOTA_EXCEEDED


def test_classify_rate_limited():
    cat = classify(1, "429 too many requests", "", timed_out=False)
    assert cat == ErrorCategory.RATE_LIMITED


def test_classify_network():
    cat = classify(1, "network unreachable", "", timed_out=False)
    assert cat == ErrorCategory.TRANSIENT_NETWORK


def test_classify_malformed_empty_output():
    cat = classify(0, "", "", timed_out=False)
    assert cat == ErrorCategory.MALFORMED_OUTPUT


def test_classify_unknown_nonzero():
    cat = classify(1, "something unexpected", "some output", timed_out=False)
    assert cat == ErrorCategory.UNKNOWN


# ---------------------------------------------------------------------------
# AuthProbe (THRENODY_TEST_MODE=1 → always True)
# ---------------------------------------------------------------------------

def test_auth_probe_test_mode():
    assert AuthProbe.check("github-copilot") is True
    assert AuthProbe.check("claude-code") is True
    assert AuthProbe.check("gemini-cli") is True


def test_auth_probe_invalidate():
    AuthProbe.check("github-copilot")
    AuthProbe.invalidate("github-copilot")
    # After invalidation still returns True in test mode
    assert AuthProbe.check("github-copilot") is True


def test_claude_auth_probe_uses_cli_status():
    result = subprocess.CompletedProcess(
        ["claude", "auth", "status"],
        0,
        '{"loggedIn": true, "authMethod": "claude.ai"}',
        "",
    )
    with (
        patch.dict(os.environ, {"THRENODY_TEST_MODE": "0"}, clear=False),
        patch("shared.resilience.subprocess.run", return_value=result) as run,
    ):
        assert AuthProbe._probe("claude-code") is True

    run.assert_called_once_with(
        ["claude", "auth", "status"],
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_claude_auth_probe_rejects_logged_out_status():
    result = subprocess.CompletedProcess(
        ["claude", "auth", "status"],
        1,
        '{"loggedIn": false}',
        "not logged in",
    )
    with patch("shared.resilience.subprocess.run", return_value=result):
        assert AuthProbe._probe("claude-code") is False


def test_claude_auth_probe_falls_back_after_timeout(tmp_path):
    credentials = tmp_path / ".claude" / ".credentials.json"
    credentials.parent.mkdir()
    credentials.write_text('{"token": "test-value"}', encoding="utf-8")

    with (
        patch(
            "shared.resilience.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                ["claude", "auth", "status"],
                5,
            ),
        ),
        patch("shared.resilience.pathlib.Path.home", return_value=tmp_path),
    ):
        assert AuthProbe._probe("claude-code") is True
