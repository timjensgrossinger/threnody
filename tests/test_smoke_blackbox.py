"""tests/test_smoke_blackbox.py — hermetic smoke tests for the Blackbox AI provider."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from blackbox.providers import (
    _build_blackbox_command,
    _clean_blackbox_output,
    _detect_blackbox,
)
from shared.discovery import BUILTIN_PROVIDERS, DetectReason


def _get_provider():
    return next(p for p in BUILTIN_PROVIDERS if p.name == "blackbox-ai")


@pytest.fixture()
def provider():
    return _get_provider()


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------


def test_build_blackbox_command_with_model(provider):
    """--model MODEL is included when a model name is given."""
    cmd = _build_blackbox_command(provider, "execute", "blackboxai", "hello")
    assert cmd == ["blackbox", "--model", "blackboxai", "hello"]


def test_build_blackbox_command_empty_model(provider):
    """Falls back to bare invocation when model is empty."""
    cmd = _build_blackbox_command(provider, "execute", "", "hello")
    assert cmd == ["blackbox", "hello"]
    assert "--model" not in cmd


def test_build_blackbox_command_none_model(provider):
    """Falls back to bare invocation when model is None."""
    cmd = _build_blackbox_command(provider, "execute", None, "hello")
    assert cmd == ["blackbox", "hello"]


def test_build_blackbox_command_claude_model(provider):
    """Claude model names are passed through verbatim."""
    cmd = _build_blackbox_command(provider, "execute", "claude-sonnet-4.6", "do stuff")
    assert cmd == ["blackbox", "--model", "claude-sonnet-4.6", "do stuff"]


def test_build_blackbox_command_effort_ignored(provider):
    """effort kwarg must not alter the command."""
    cmd_no_effort = _build_blackbox_command(provider, "execute", "blackboxai", "p")
    cmd_with_effort = _build_blackbox_command(provider, "execute", "blackboxai", "p", effort="low")
    assert cmd_no_effort == cmd_with_effort


# ---------------------------------------------------------------------------
# Detection — binary missing
# ---------------------------------------------------------------------------


def test_detect_blackbox_binary_missing(provider, monkeypatch):
    """Without blackbox on PATH, detection must return BINARY_MISSING."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    result = _detect_blackbox(provider)
    assert not result.routeable
    assert result.reason == DetectReason.BINARY_MISSING


# ---------------------------------------------------------------------------
# Detection — with auth
# ---------------------------------------------------------------------------


def test_detect_blackbox_api_key(mock_blackbox_cli, provider, monkeypatch):
    """BLACKBOX_API_KEY present → routeable."""
    monkeypatch.setenv("BLACKBOX_API_KEY", "bbx-test-key")
    result = _detect_blackbox(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY


def test_detect_blackbox_settings_file(mock_blackbox_cli, provider, monkeypatch, tmp_path):
    """~/.blackboxcli/settings.json with content → routeable."""
    import pathlib

    settings_dir = tmp_path / ".blackboxcli"
    settings_dir.mkdir()
    settings_path = settings_dir / "settings.json"
    settings_path.write_text(json.dumps({"api_key": "bbx-test", "model": "blackboxai"}), encoding="utf-8")

    monkeypatch.delenv("BLACKBOX_API_KEY", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    result = _detect_blackbox(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY


def test_detect_blackbox_empty_settings_file(mock_blackbox_cli, provider, monkeypatch, tmp_path):
    """Empty settings.json → AUTH_FAILED."""
    import pathlib

    settings_dir = tmp_path / ".blackboxcli"
    settings_dir.mkdir()
    settings_path = settings_dir / "settings.json"
    settings_path.write_text("{}", encoding="utf-8")

    monkeypatch.delenv("BLACKBOX_API_KEY", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    result = _detect_blackbox(provider)
    assert not result.routeable
    assert result.reason == DetectReason.AUTH_FAILED


def test_detect_blackbox_no_auth(mock_blackbox_cli, provider, monkeypatch, tmp_path):
    """Binary present but no auth and no settings file → AUTH_FAILED."""
    import pathlib

    monkeypatch.delenv("BLACKBOX_API_KEY", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    result = _detect_blackbox(provider)
    assert not result.routeable
    assert result.reason == DetectReason.AUTH_FAILED


# ---------------------------------------------------------------------------
# Output cleaner
# ---------------------------------------------------------------------------


def test_clean_blackbox_output_plain(provider):
    """Clean output returned stripped."""
    raw = "Here is the answer.\n"
    assert _clean_blackbox_output(raw) == "Here is the answer."


def test_clean_blackbox_output_strips_preamble(provider):
    """Session/Model/Connecting lines are removed."""
    raw = (
        "Session: abc123\n"
        "Model: blackboxai\n"
        "Connecting to Blackbox AI...\n"
        "Connected.\n"
        "The result is here.\n"
    )
    result = _clean_blackbox_output(raw)
    assert result == "The result is here."


def test_clean_blackbox_output_empty(provider):
    """Empty input returns empty string."""
    assert _clean_blackbox_output("") == ""


# ---------------------------------------------------------------------------
# Provider metadata
# ---------------------------------------------------------------------------


def test_provider_metadata():
    p = _get_provider()
    assert p.name == "blackbox-ai"
    assert p.binary == "blackbox"
    assert p.tier_models["low"] == "blackboxai"
    assert p.tier_models["medium"] == "claude-sonnet-5"
    assert p.tier_models["high"] == "claude-opus-5"
    assert p.billing_model == "metered"
