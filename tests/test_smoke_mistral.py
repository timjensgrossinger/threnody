"""tests/test_smoke_mistral.py — hermetic smoke tests for the Mistral Vibe provider."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mistral.providers import (
    _build_mistral_command,
    _clean_mistral_output,
    _detect_mistral,
)
from shared.discovery import BUILTIN_PROVIDERS, DetectReason


def _get_provider():
    return copy.deepcopy(next(p for p in BUILTIN_PROVIDERS if p.name == "mistral-vibe"))


@pytest.fixture()
def provider():
    return _get_provider()


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------


def test_build_mistral_command_basic(provider):
    """Command must use vibe non-interactive flags and a private sandbox workdir."""
    cmd = _build_mistral_command(provider, "execute", "devstral-small", "hello")
    assert cmd[:6] == ["vibe", "-p", "hello", "--output", "text", "--workdir"]
    assert cmd[6] != "/tmp"


def test_build_mistral_command_ignores_model(provider):
    """No --model flag is ever emitted regardless of the model argument."""
    cmd = _build_mistral_command(provider, "execute", "codestral-2508", "test prompt")
    assert "--model" not in cmd
    assert "codestral-2508" not in cmd


def test_build_mistral_command_effort_ignored(provider):
    """effort kwarg is accepted but must not alter the command."""
    cmd_no_effort = _build_mistral_command(provider, "execute", "devstral", "p")
    cmd_with_effort = _build_mistral_command(provider, "execute", "devstral", "p", effort="high")
    assert cmd_no_effort[:6] == cmd_with_effort[:6]
    assert cmd_no_effort[6] != cmd_with_effort[6]


# ---------------------------------------------------------------------------
# Detection — binary missing
# ---------------------------------------------------------------------------


def test_detect_mistral_binary_missing(provider, monkeypatch):
    """Without vibe on PATH, detection must return BINARY_MISSING."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    result = _detect_mistral(provider)
    assert not result.routeable
    assert result.reason == DetectReason.BINARY_MISSING


# ---------------------------------------------------------------------------
# Detection — with auth
# ---------------------------------------------------------------------------


def test_detect_mistral_api_key(mock_mistral_cli, provider, monkeypatch, tmp_path):
    """MISTRAL_API_KEY present → routeable."""
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key-abc")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    result = _detect_mistral(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY
    assert provider.tier_models["low"] == "devstral-small"
    assert provider.tier_models["medium"] == "mistral-medium-3.5"
    assert provider.tier_models["high"] == "mistral-medium-3.5"


def test_detect_mistral_config_file(mock_mistral_cli, provider, monkeypatch, tmp_path):
    """~/.vibe/config.toml present with content → routeable."""
    import pathlib

    vibe_dir = tmp_path / ".vibe"
    vibe_dir.mkdir()
    config_path = vibe_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                'active_model = "mistral-medium-3.5"',
                "",
                "[[models]]",
                'name = "mistral-vibe-cli-latest"',
                'alias = "mistral-medium-3.5"',
                "",
                "[[models]]",
                'name = "devstral-small-latest"',
                'alias = "devstral-small"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    result = _detect_mistral(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY
    assert provider.tier_models["low"] == "devstral-small"
    assert provider.tier_models["medium"] == "mistral-medium-3.5"
    assert provider.tier_models["high"] == "mistral-medium-3.5"


def test_detect_mistral_config_file_resolves_internal_active_model_name(
    mock_mistral_cli, provider, monkeypatch, tmp_path
):
    """Internal active model names should resolve to the configured alias."""
    import pathlib

    vibe_dir = tmp_path / ".vibe"
    vibe_dir.mkdir()
    config_path = vibe_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                'active_model = "mistral-vibe-cli-latest"',
                "",
                "[[models]]",
                'name = "mistral-vibe-cli-latest"',
                'alias = "mistral-medium-3.5"',
                "",
                "[[models]]",
                'name = "devstral-small-latest"',
                'alias = "devstral-small"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    result = _detect_mistral(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY
    assert provider.tier_models["low"] == "devstral-small"
    assert provider.tier_models["medium"] == "mistral-medium-3.5"
    assert provider.tier_models["high"] == "mistral-medium-3.5"


def test_detect_mistral_config_file_with_header_comments_and_following_section(
    mock_mistral_cli, provider, monkeypatch, tmp_path
):
    """Model parsing should ignore header comments and stop at normal tables."""
    import pathlib

    vibe_dir = tmp_path / ".vibe"
    vibe_dir.mkdir()
    config_path = vibe_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                'active_model = "mistral-vibe-cli-latest"',
                "",
                "[[ models ]] # preferred medium model",
                'name = "mistral-vibe-cli-latest"',
                'alias = "mistral-medium-3.5"',
                "",
                "[[models]]",
                'name = "devstral-small-latest"',
                'alias = "devstral-small"',
                "",
                "[telemetry]",
                "active_model = 'should-not-overwrite-model'",
                'alias = "should-not-overwrite-model"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    result = _detect_mistral(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY
    assert provider.tier_models["low"] == "devstral-small"
    assert provider.tier_models["medium"] == "mistral-medium-3.5"
    assert provider.tier_models["high"] == "mistral-medium-3.5"


def test_detect_mistral_config_file_supports_single_quoted_strings(
    mock_mistral_cli, provider, monkeypatch, tmp_path
):
    """Single-quoted TOML strings should parse like double-quoted ones."""
    import pathlib

    vibe_dir = tmp_path / ".vibe"
    vibe_dir.mkdir()
    config_path = vibe_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "active_model = 'mistral-vibe-cli-latest'",
                "",
                "[[models]]",
                "name = 'mistral-vibe-cli-latest'",
                "alias = 'mistral-medium-3.5'",
                "",
                "[[models]]",
                "name = 'devstral-small-latest'",
                "alias = 'devstral-small'",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    result = _detect_mistral(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY
    assert provider.tier_models["low"] == "devstral-small"
    assert provider.tier_models["medium"] == "mistral-medium-3.5"
    assert provider.tier_models["high"] == "mistral-medium-3.5"


def test_detect_mistral_bad_config_still_routes_with_env_auth(
    mock_mistral_cli, provider, monkeypatch, tmp_path
):
    """Unreadable config should not block env-authenticated routing."""
    import pathlib

    vibe_dir = tmp_path / ".vibe"
    vibe_dir.mkdir()
    config_path = vibe_dir / "config.toml"
    config_path.write_bytes(b"\xff\xfe\x00")

    monkeypatch.setenv("MISTRAL_API_KEY", "test-key-abc")
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    result = _detect_mistral(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY
    assert provider.tier_models["low"] == "devstral-small"
    assert provider.tier_models["medium"] == "mistral-medium-3.5"
    assert provider.tier_models["high"] == "mistral-medium-3.5"


def test_detect_mistral_config_file_supports_utf8_bom(
    mock_mistral_cli, provider, monkeypatch, tmp_path
):
    """A UTF-8 BOM should not block active_model parsing."""
    import pathlib

    vibe_dir = tmp_path / ".vibe"
    vibe_dir.mkdir()
    config_path = vibe_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "\ufeffactive_model = 'mistral-vibe-cli-latest'",
                "",
                "[[models]]",
                "name = 'mistral-vibe-cli-latest'",
                "alias = 'mistral-medium-3.5'",
                "",
                "[[models]]",
                "name = 'devstral-small-latest'",
                "alias = 'devstral-small'",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    result = _detect_mistral(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY
    assert provider.tier_models["low"] == "devstral-small"
    assert provider.tier_models["medium"] == "mistral-medium-3.5"
    assert provider.tier_models["high"] == "mistral-medium-3.5"


def test_detect_mistral_api_key_still_uses_config_model_aliases(
    mock_mistral_cli, provider, monkeypatch, tmp_path
):
    """Env-based auth should not skip config-based model detection."""
    import pathlib

    vibe_dir = tmp_path / ".vibe"
    vibe_dir.mkdir()
    config_path = vibe_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                'active_model = "mistral-vibe-cli-latest"',
                "",
                "[[models]]",
                'name = "mistral-vibe-cli-latest"',
                'alias = "mistral-medium-3.5"',
                "",
                "[[models]]",
                'name = "devstral-small-latest"',
                'alias = "devstral-small"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("MISTRAL_API_KEY", "test-key-abc")
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    result = _detect_mistral(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY
    assert provider.tier_models["low"] == "devstral-small"
    assert provider.tier_models["medium"] == "mistral-medium-3.5"
    assert provider.tier_models["high"] == "mistral-medium-3.5"


def test_detect_mistral_unknown_active_model_falls_back_to_default_medium(
    mock_mistral_cli, provider, monkeypatch, tmp_path
):
    """Unknown active_model values should not relabel medium/high tiers."""
    import pathlib

    vibe_dir = tmp_path / ".vibe"
    vibe_dir.mkdir()
    config_path = vibe_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                'active_model = "unknown-internal-model"',
                "",
                "[[models]]",
                'name = "devstral-small-latest"',
                'alias = "devstral-small"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    result = _detect_mistral(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY
    assert provider.tier_models["low"] == "devstral-small"
    assert provider.tier_models["medium"] == "mistral-medium-3.5"
    assert provider.tier_models["high"] == "mistral-medium-3.5"


def test_detect_mistral_low_active_model_does_not_replace_medium_high(
    mock_mistral_cli, provider, monkeypatch, tmp_path
):
    """A low-tier active_model should not relabel medium/high tiers."""
    import pathlib

    vibe_dir = tmp_path / ".vibe"
    vibe_dir.mkdir()
    config_path = vibe_dir / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                'active_model = "devstral-small-latest"',
                "",
                "[[models]]",
                'name = "mistral-vibe-cli-latest"',
                'alias = "mistral-medium-3.5"',
                "",
                "[[models]]",
                'name = "devstral-small-latest"',
                'alias = "devstral-small"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    result = _detect_mistral(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY
    assert provider.tier_models["low"] == "devstral-small"
    assert provider.tier_models["medium"] == "mistral-medium-3.5"
    assert provider.tier_models["high"] == "mistral-medium-3.5"


def test_detect_mistral_no_auth(mock_mistral_cli, provider, monkeypatch, tmp_path):
    """Binary present but no auth → AUTH_FAILED."""
    import pathlib

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    class FakeHomeEmpty:
        @staticmethod
        def home():
            return tmp_path  # no .vibe/config.toml

    monkeypatch.setattr(pathlib.Path, "home", FakeHomeEmpty.home)

    result = _detect_mistral(provider)
    assert not result.routeable
    assert result.reason == DetectReason.AUTH_FAILED


def test_detect_mistral_api_key_works_when_home_lookup_fails(mock_mistral_cli, provider, monkeypatch):
    """Env-based auth should still work if Path.home() raises."""
    import pathlib

    monkeypatch.setenv("MISTRAL_API_KEY", "test-key-abc")
    monkeypatch.setattr(pathlib.Path, "home", lambda: (_ for _ in ()).throw(RuntimeError("no home")))

    result = _detect_mistral(provider)
    assert result.routeable
    assert result.reason == DetectReason.READY


# ---------------------------------------------------------------------------
# Output cleaner
# ---------------------------------------------------------------------------


def test_clean_mistral_output_plain(provider):
    """Clean output is returned as-is (stripped)."""
    raw = "Here is the answer to your question.\n"
    assert _clean_mistral_output(raw) == "Here is the answer to your question."


def test_clean_mistral_output_strips_preamble(provider):
    """> status lines at the top are removed."""
    raw = "> Processing request...\nHere is the result."
    assert _clean_mistral_output(raw) == "Here is the result."


def test_clean_mistral_output_empty(provider):
    """Empty input returns empty string."""
    assert _clean_mistral_output("") == ""


# ---------------------------------------------------------------------------
# Provider metadata
# ---------------------------------------------------------------------------


def test_provider_metadata():
    p = _get_provider()
    assert p.name == "mistral-vibe"
    assert p.binary == "vibe"
    assert p.tier_models["low"] == "devstral-small"
    assert p.tier_models["medium"] == "mistral-medium-3.5"
    assert p.tier_models["high"] == "mistral-medium-3.5"
    assert p.billing_model == "metered"
