"""Tests for the Antigravity CLI provider hooks."""
from __future__ import annotations

import json

import pytest

from antigravity.providers import (
    _build_agy_command,
    _clean_agy_output,
    _detect_agy,
)
from shared.discovery import DetectReason


def test_antigravity_command_basic() -> None:
    cmd = _build_agy_command(None, "execute", "gemini-3.5-flash", "do the thing")
    assert cmd[0] == "agy"
    assert "-p" in cmd
    assert "do the thing" in cmd
    assert "--model" in cmd
    assert "gemini-3.5-flash" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd
    assert "--sandbox" in cmd


def test_antigravity_command_with_effort() -> None:
    cmd = _build_agy_command(None, "execute", "gemini-3.1-pro", "hard task", effort="high")
    assert "--effort" in cmd
    assert "high" in cmd
    assert "gemini-3.1-pro" in cmd


def test_antigravity_output_cleaning_json_envelope() -> None:
    envelope = json.dumps({
        "conversation_id": "abc123",
        "status": "SUCCESS",
        "response": "  Hello from Gemini  ",
        "duration_seconds": 3.5,
    })
    assert _clean_agy_output(envelope) == "Hello from Gemini"


def test_antigravity_output_cleaning_error_status() -> None:
    envelope = json.dumps({
        "status": "ERROR",
        "error": "auth failed",
        "response": "",
    })
    assert _clean_agy_output(envelope) == ""


def test_antigravity_output_cleaning_plain_text() -> None:
    assert _clean_agy_output("  plain text  ") == "plain text"


def test_antigravity_output_cleaning_empty() -> None:
    assert _clean_agy_output("") == ""
    assert _clean_agy_output("   ") == ""


def test_antigravity_detection_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("antigravity.providers.shutil.which", lambda _: None)
    result = _detect_agy(None)
    assert result.routeable is False
    assert result.reason == DetectReason.BINARY_MISSING
