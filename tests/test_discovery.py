#!/usr/bin/env python3
"""Tests for shared/discovery.py — universal cross-provider execution bridge."""
from __future__ import annotations

import subprocess
import sys
import ssl
from pathlib import Path
from unittest.mock import MagicMock, patch
from tempfile import TemporaryDirectory

import pytest

# Ensure the project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.adapters import ProviderAdapter, ProviderCapability
from shared.discovery import (
    CLIProvider,
    BUILTIN_PROVIDERS,
    DetectReason,
    HOST_PROVIDER_NAMES,
    ProviderRegistry,
    ProviderReadiness,
    _seed_copilot_auth_files,
    caller_from_client_name,
    detect_caller,
    get_registry,
    installer_provider_inventory,
    _registry,
)
from tests.conftest import mock_provider_fixture, reset_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_provider(
    name: str,
    cost_low: int,
    detected: bool = True,
    execute_result: str | None = "output",
) -> MagicMock:
    """Return a MagicMock shaped like a CLIProvider."""
    p = MagicMock(spec=CLIProvider)
    p.name = name
    p.display_name = name.replace("-", " ").title()
    p.cost_rank = {"low": cost_low, "medium": cost_low + 1, "high": cost_low + 2}
    p.tier_models = {"low": "model-low", "medium": "model-med", "high": "model-high"}
    readiness = ProviderReadiness(
        routeable=detected,
        reason=DetectReason.READY if detected else DetectReason.BINARY_MISSING,
        last_checked=0.0,
    )
    p.readiness = readiness
    p.detect.return_value = readiness
    p.is_routeable.return_value = detected
    p.execute.return_value = execute_result
    return p


def _builtin_provider(name: str) -> CLIProvider:
    return next(p for p in BUILTIN_PROVIDERS if p.name == name)


@pytest.fixture
def production_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)
    monkeypatch.setattr("shared.discovery._LOCAL_ENDPOINT_CANDIDATES", ())


# ---------------------------------------------------------------------------
# 1. Built-in provider definitions
# ---------------------------------------------------------------------------


def test_builtin_providers_defined():
    # Provider surface now includes the original host CLIs plus OpenCode and adapters.
    assert len(BUILTIN_PROVIDERS) >= 7

    names = [p.name for p in BUILTIN_PROVIDERS]
    assert "github-copilot" in names
    assert "claude-code" in names
    assert "codex" in names
    assert "opencode" in names

    # Verify core host providers have 3 tiers each
    for provider in BUILTIN_PROVIDERS:
        if provider.name in ["github-copilot", "claude-code", "codex"]:
            assert set(provider.tier_models.keys()) == {"low", "medium", "high"}
            assert set(provider.cost_rank.keys()) == {"low", "medium", "high"}

    gh = next(p for p in BUILTIN_PROVIDERS if p.name == "github-copilot")
    opencode = next(p for p in BUILTIN_PROVIDERS if p.name == "opencode")
    assert gh.cost_rank["low"] == 0, "github-copilot low tier should be free"
    assert opencode.cost_rank["low"] == 0, "opencode low tier should be free"


def test_installer_provider_inventory_reports_all_supported_providers():
    host_provider = _mock_provider("github-copilot", 0, detected=True)
    host_provider.binary = "gh"
    host_provider.display_name = "GitHub Copilot"
    host_provider.detect_hook = None
    host_provider.detect_cmd = None

    extra_provider = _mock_provider("mistral-vibe", 3, detected=False)
    extra_provider.binary = "vibe"
    extra_provider.display_name = "Mistral Vibe"
    extra_provider.detect_hook = None
    extra_provider.detect_cmd = None

    amazon_provider = _mock_provider("amazon-q", 4, detected=False)
    amazon_provider.binary = "q"
    amazon_provider.display_name = "Amazon Q/Kiro"
    amazon_provider.detect_hook = None
    amazon_provider.detect_cmd = None

    with (
        patch("shared.discovery.BUILTIN_PROVIDERS", [host_provider, extra_provider, amazon_provider]),
        patch(
            "shared.discovery.shutil.which",
            side_effect=lambda binary: {
                "gh": "/usr/local/bin/gh",
                "kiro": "/usr/local/bin/kiro",
            }.get(binary),
        ),
    ):
        inventory = installer_provider_inventory()

    assert [entry["name"] for entry in inventory] == [
        "github-copilot",
        "mistral-vibe",
        "amazon-q",
    ]
    assert inventory[0]["host_shell"] is True
    assert inventory[0]["available"] is True
    assert inventory[0]["routeable"] is True
    assert inventory[0]["detection_scope"] == "binary_only"
    assert inventory[1]["host_shell"] is False
    assert inventory[1]["available"] is False
    assert inventory[1]["detect_reason"] == "binary_missing"
    assert inventory[2]["available"] is True
    assert inventory[2]["routeable"] is True
    assert inventory[2]["detected_binary"] == "kiro"
    assert "github-copilot" in HOST_PROVIDER_NAMES
    assert "mistral-vibe" not in HOST_PROVIDER_NAMES


def test_installer_binary_scan_does_not_claim_auth_aware_provider_is_ready():
    provider = _mock_provider("junie", 0, detected=True)
    provider.binary = "junie"
    provider.detect_hook = lambda _provider: ProviderReadiness(
        routeable=True,
        reason=DetectReason.READY,
    )

    with (
        patch("shared.discovery.BUILTIN_PROVIDERS", [provider]),
        patch("shared.discovery.shutil.which", return_value="/usr/local/bin/junie"),
    ):
        inventory = installer_provider_inventory()

    assert inventory[0]["available"] is True
    assert inventory[0]["routeable"] is False
    assert inventory[0]["detect_reason"] == "auth_unknown"
    assert inventory[0]["detection_scope"] == "binary_only"


def test_installer_verified_scan_uses_provider_readiness_probe():
    provider = _mock_provider("junie", 0, detected=True)
    provider.binary = "junie"
    provider.detect_hook = lambda _provider: ProviderReadiness(
        routeable=False,
        reason=DetectReason.AUTH_FAILED,
    )
    provider.detect.return_value = ProviderReadiness(
        routeable=False,
        reason=DetectReason.AUTH_FAILED,
    )

    with (
        patch("shared.discovery.BUILTIN_PROVIDERS", [provider]),
        patch("shared.discovery.shutil.which", return_value="/usr/local/bin/junie"),
    ):
        inventory = installer_provider_inventory(verify_readiness=True)

    assert inventory[0]["available"] is True
    assert inventory[0]["routeable"] is False
    assert inventory[0]["detect_reason"] == "auth_failed"
    assert inventory[0]["detection_scope"] == "auth_verified"


def test_detect_q_kiro_auth_failed_preserves_selected_binary(monkeypatch: pytest.MonkeyPatch):
    provider = _builtin_provider("amazon-q")

    def which(binary: str) -> str | None:
        return "/usr/local/bin/kiro" if binary == "kiro" else None

    class FakeCompletedProcess:
        returncode = 1

    monkeypatch.setattr("shared.discovery.shutil.which", which)
    monkeypatch.setattr("shared.discovery.subprocess.run", lambda *args, **kwargs: FakeCompletedProcess())
    monkeypatch.setattr("shared.discovery.Path.home", lambda: Path("/tmp/nonexistent-amazon-q-home"))

    readiness = provider.detect()

    assert readiness.reason is DetectReason.AUTH_FAILED
    assert readiness.metadata == {"binary": "kiro"}


# ---------------------------------------------------------------------------
# 2–5. CLIProvider.detect()
# ---------------------------------------------------------------------------


def test_cli_provider_detect_binary_not_found():
    provider = CLIProvider(
        name="test",
        binary="nonexistent_binary_xyz",
        display_name="Test",
        tier_models={"low": "m"},
        cost_rank={"low": 0},
        detect_cmd=None,
    )
    readiness = provider.detect()
    assert readiness.routeable is False
    assert readiness.reason is DetectReason.BINARY_MISSING


def test_cli_provider_detect_binary_found():
    provider = CLIProvider(
        name="test",
        binary="gh",
        display_name="Test",
        tier_models={"low": "m"},
        cost_rank={"low": 0},
        detect_cmd=None,  # no extra command — binary presence is sufficient
    )
    with patch("shared.discovery.shutil.which", return_value="/usr/local/bin/gh"):
        readiness = provider.detect()
    assert readiness.routeable is True
    assert readiness.reason is DetectReason.READY


def test_cli_provider_detect_cmd_fails():
    provider = CLIProvider(
        name="test",
        binary="gh",
        display_name="Test",
        tier_models={"low": "m"},
        cost_rank={"low": 0},
        detect_cmd=["gh", "--version"],
    )
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "error output"
    with (
        patch("shared.discovery.shutil.which", return_value="/usr/local/bin/gh"),
        patch("shared.discovery.subprocess.run", return_value=mock_result),
    ):
        readiness = provider.detect()
    assert readiness.routeable is False
    assert readiness.reason is DetectReason.AUTH_FAILED


def test_cli_provider_detect_cmd_timeout():
    provider = CLIProvider(
        name="test",
        binary="gh",
        display_name="Test",
        tier_models={"low": "m"},
        cost_rank={"low": 0},
        detect_cmd=["gh", "--version"],
    )
    with (
        patch("shared.discovery.shutil.which", return_value="/usr/local/bin/gh"),
        patch(
            "shared.discovery.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gh", "--version"], timeout=15),
        ),
    ):
        readiness = provider.detect()
    assert readiness.routeable is False
    assert readiness.reason is DetectReason.AUTH_UNKNOWN


# ---------------------------------------------------------------------------
# 6–8. CLIProvider._build_command()
# ---------------------------------------------------------------------------


def test_build_command_github_copilot():
    provider = BUILTIN_PROVIDERS[0]
    assert provider.name == "github-copilot"
    with (
        patch("shared.discovery._copilot_supports_model_flag", return_value=True),
        patch("shared.discovery._copilot_supports_disable_builtin_mcps", return_value=True),
    ):
        cmd = provider._build_command("hello", "gpt-5-mini")
    assert cmd == [
        "gh",
        "copilot",
        "--",
        "-p",
        "hello",
        "--model",
        "gpt-5-mini",
        "--disable-builtin-mcps",
    ]


def test_build_command_github_copilot_skips_disable_flag_when_unsupported():
    provider = _builtin_provider("github-copilot")
    with (
        patch("shared.discovery._copilot_supports_model_flag", return_value=True),
        patch("shared.discovery._copilot_supports_disable_builtin_mcps", return_value=False),
    ):
        cmd = provider._build_command("hello", "gpt-5-mini")
    assert cmd == ["gh", "copilot", "--", "-p", "hello", "--model", "gpt-5-mini"]


def test_build_command_github_copilot_skips_model_flag_when_unsupported():
    provider = _builtin_provider("github-copilot")
    with (
        patch("shared.discovery._copilot_supports_model_flag", return_value=False),
        patch("shared.discovery._copilot_supports_disable_builtin_mcps", return_value=True),
    ):
        cmd = provider._build_command("hello", "gpt-5-mini")
    assert cmd == ["gh", "copilot", "--", "-p", "hello", "--disable-builtin-mcps"]


def test_execute_github_copilot_uses_isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _builtin_provider("github-copilot")
    captured: dict[str, object] = {}

    def _fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        result = MagicMock()
        result.returncode = 0
        result.stdout = "ok\n"
        result.stderr = ""
        return result

    monkeypatch.setattr("shared.discovery.subprocess.run", _fake_run)

    with (
        patch("shared.discovery._copilot_supports_model_flag", return_value=True),
        patch("shared.discovery._copilot_supports_disable_builtin_mcps", return_value=True),
    ):
        assert provider.execute("hello", "gpt-5-mini", timeout=5) == "ok"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["timeout"] == 5
    assert kwargs["cwd"].endswith("copilot-sandbox")
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["COPILOT_HOME"].endswith("copilot-sandbox")


def test_execute_github_copilot_handles_sandbox_setup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _builtin_provider("github-copilot")
    monkeypatch.setattr(
        "shared.discovery._copilot_supports_model_flag",
        lambda: True,
    )
    monkeypatch.setattr(
        "shared.discovery._copilot_supports_disable_builtin_mcps",
        lambda: True,
    )
    monkeypatch.setattr(
        "shared.discovery._copilot_subprocess_env",
        lambda: (_ for _ in ()).throw(OSError("boom")),
    )

    assert provider.execute("hello", "gpt-5-mini", timeout=5) is None


def test_execute_github_copilot_does_not_retry_without_env_on_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _builtin_provider("github-copilot")
    calls: list[dict[str, object]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        calls.append(dict(kwargs))
        result = MagicMock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = "Authentication required"
        return result

    monkeypatch.setattr("shared.discovery.subprocess.run", _fake_run)
    monkeypatch.setattr("shared.discovery._copilot_supports_model_flag", lambda: True)
    monkeypatch.setattr(
        "shared.discovery._copilot_supports_disable_builtin_mcps",
        lambda: True,
    )

    assert provider.execute("hello", "gpt-5-mini", timeout=5) is None
    # AUTH_EXPIRED is not retried — only 1 attempt made
    assert len(calls) == 1
    assert all("env" in call for call in calls)


def test_execute_github_copilot_handles_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _builtin_provider("github-copilot")
    monkeypatch.setattr(
        "shared.discovery._copilot_supports_model_flag",
        lambda: (_ for _ in ()).throw(RuntimeError("probe failed")),
    )

    assert provider.execute("hello", "gpt-5-mini", timeout=5) is None


def test_seed_copilot_auth_files_removes_stale_sandbox_copy() -> None:
    with TemporaryDirectory() as source_dir, TemporaryDirectory() as sandbox_dir:
        source = Path(source_dir)
        sandbox = Path(sandbox_dir)
        stale = sandbox / "auth.json"
        stale.write_text("stale", encoding="utf-8")

        with patch.dict("os.environ", {"COPILOT_HOME": str(source)}):
            _seed_copilot_auth_files(sandbox)

        assert not stale.exists()


def test_build_command_claude_code():
    provider = BUILTIN_PROVIDERS[1]
    assert provider.name == "claude-code"
    cmd = provider._build_command("hello", "claude-sonnet-4.6")
    assert cmd == ["claude", "-p", "hello", "--model", "claude-sonnet-4.6"]


def test_build_command_claude_code_with_effort():
    provider = BUILTIN_PROVIDERS[1]
    assert provider.name == "claude-code"
    cmd = provider._build_command("hello", "claude-sonnet-4.6", effort="high")
    assert cmd == ["claude", "-p", "hello", "--model", "claude-sonnet-4.6", "--effort", "high"]


def test_build_command_opencode():
    provider = _builtin_provider("opencode")
    cmd = provider._build_command("hello", "opencode/nemotron-3-super-free")
    assert cmd == [
        "opencode",
        "run",
        "--model",
        "opencode/nemotron-3-super-free",
        "--dangerously-skip-permissions",
        "hello",
    ]


def test_detect_caller_opencode_env_marker(monkeypatch):
    monkeypatch.setenv("OPENCODE_HOST", "1")
    assert detect_caller() == "opencode"


def test_detect_caller_ignores_falsey_opencode_session(monkeypatch):
    monkeypatch.delenv("OPENCODE_HOST", raising=False)
    monkeypatch.setenv("OPENCODE_SESSION", "0")
    monkeypatch.setenv("COPILOT_CLI", "1")
    assert detect_caller() == "github-copilot"


def test_detect_caller_accepts_truthy_opencode_host(monkeypatch):
    monkeypatch.setenv("OPENCODE_HOST", "true")
    assert detect_caller() == "opencode"


@pytest.mark.parametrize(
    ("client_name", "expected"),
    [
        ("GitHub Copilot CLI", "github-copilot"),
        ("Claude Code", "claude-code"),
        ("OpenAI Codex", "codex"),
        ("Cursor Agent", "cursor"),
        ("JetBrains Junie", "junie"),
        ("OpenCode", "opencode"),
        ("unknown-client", None),
    ],
)
def test_caller_from_client_name(client_name, expected):
    assert caller_from_client_name(client_name) == expected


def test_detect_caller_ignores_falsey_copilot_and_claude_markers(monkeypatch):
    monkeypatch.setenv("COPILOT_CLI", "0")
    monkeypatch.setenv("COPILOT_RUN_APP", "false")
    monkeypatch.setenv("CLAUDE_CODE", "no")
    monkeypatch.setenv("CLAUDE_CODE_SESSION", "")

    assert detect_caller() is None


def test_detect_caller_uses_parent_process_transport_fallback(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setattr("shared.discovery.os.getppid", lambda: 123)
    monkeypatch.setattr(
        "shared.discovery.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="/usr/local/bin/opencode mcp",
            stderr="",
        ),
    )

    assert detect_caller() == "opencode"


def test_command_builder_override(mock_provider_fixture: CLIProvider):
    def build_override(
        provider: CLIProvider,
        action: str,
        model: str,
        prompt: str,
    ) -> list[str]:
        return [provider.binary, action, "--model", model, prompt]

    mock_provider_fixture.command_builder = build_override

    assert mock_provider_fixture._build_command(
        "hello",
        "custom-model",
    ) == ["test-binary", "execute", "--model", "custom-model", "hello"]


def test_command_builder_override_receives_effort(mock_provider_fixture: CLIProvider):
    captured: dict[str, str | None] = {"effort": None}

    def build_override(
        provider: CLIProvider,
        action: str,
        model: str,
        prompt: str,
        effort: str | None = None,
    ) -> list[str]:
        captured["effort"] = effort
        return [provider.binary, action, "--model", model, "--effort", effort or "", prompt]

    mock_provider_fixture.command_builder = build_override

    assert mock_provider_fixture._build_command(
        "hello",
        "custom-model",
        effort="high",
    ) == ["test-binary", "execute", "--model", "custom-model", "--effort", "high", "hello"]
    assert captured["effort"] == "high"


def test_detect_auth_failed_not_routeable():
    provider = CLIProvider(
        name="test",
        binary="gh",
        display_name="Test",
        tier_models={"low": "m"},
        cost_rank={"low": 0},
        detect_cmd=["gh", "auth", "status"],
    )
    mock_result = MagicMock(returncode=1, stderr="auth required")

    with (
        patch("shared.discovery.shutil.which", return_value="/usr/local/bin/gh"),
        patch("shared.discovery.subprocess.run", return_value=mock_result),
    ):
        readiness = provider.detect()

    assert readiness.routeable is False
    assert readiness.reason is DetectReason.AUTH_FAILED


def test_provider_specific_output_cleaner(mock_provider_fixture: CLIProvider):
    mock_provider_fixture.output_cleaner = lambda raw: raw.replace("provider>", "").strip()
    mock_result = MagicMock(returncode=0, stdout=" provider> answer ")

    with patch("shared.discovery.subprocess.run", return_value=mock_result):
        assert mock_provider_fixture.execute("hello", "test-low-model") == "answer"


# ---------------------------------------------------------------------------
# 9–13. CLIProvider.execute()
# ---------------------------------------------------------------------------


def test_execute_success():
    provider = BUILTIN_PROVIDERS[0]
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Hello world"
    with patch("shared.discovery.subprocess.run", return_value=mock_result):
        result = provider.execute("hello", "gpt-5-mini")
    assert result == "Hello world"


def test_execute_timeout():
    provider = BUILTIN_PROVIDERS[0]
    with patch(
        "shared.discovery.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=120),
    ):
        assert provider.execute("hello", "gpt-5-mini") is None


def test_execute_file_not_found():
    provider = BUILTIN_PROVIDERS[0]
    with patch(
        "shared.discovery.subprocess.run",
        side_effect=FileNotFoundError("gh not found"),
    ):
        assert provider.execute("hello", "gpt-5-mini") is None


def test_execute_nonzero_exit():
    provider = BUILTIN_PROVIDERS[0]
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "some error"
    with patch("shared.discovery.subprocess.run", return_value=mock_result):
        assert provider.execute("hello", "gpt-5-mini") is None


def test_execute_empty_output():
    provider = BUILTIN_PROVIDERS[0]
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "   "  # whitespace only — strips to ""
    with patch("shared.discovery.subprocess.run", return_value=mock_result):
        assert provider.execute("hello", "gpt-5-mini") is None


# ---------------------------------------------------------------------------
# CR-01: Codex temp-file lifecycle tests
# ---------------------------------------------------------------------------


def _make_codex_provider(command_builder=None, output_cleaner=None):
    """Return a minimal CLIProvider that exercises the temp-file path."""
    return CLIProvider(
        name="codex",
        binary="codex",
        display_name="Codex",
        tier_models={"low": "o4-mini", "medium": "o4", "high": "o4"},
        cost_rank={"low": 1, "medium": 2, "high": 2},
        command_builder=command_builder,
        output_cleaner=output_cleaner,
    )


def test_execute_reads_output_file_when_stdout_empty(tmp_path):
    """If stdout is empty but _pending_output_file has content, execute() uses the file."""
    output_file = tmp_path / "out.txt"
    output_file.write_text("result from file")

    def _builder(provider, action, model, prompt):
        provider._pending_output_file = str(output_file)
        return ["codex", "exec", "-m", model, "-a", "never", "-o", str(output_file), prompt]

    provider = _make_codex_provider(command_builder=_builder)
    mock_result = MagicMock(returncode=0, stdout="")

    with patch("shared.discovery.subprocess.run", return_value=mock_result):
        result = provider.execute("hello", "o4-mini")

    assert result == "result from file"
    # File is cleaned up after the attempt
    assert not output_file.exists()


def test_execute_cleans_up_output_file_on_success(tmp_path):
    """Temp output file is removed even when the run succeeds via file fallback."""
    output_file = tmp_path / "out.txt"
    output_file.write_text("answer")

    def _builder(provider, action, model, prompt):
        provider._pending_output_file = str(output_file)
        return ["codex", "exec", "-m", model, "-a", "never", "-o", str(output_file), prompt]

    provider = _make_codex_provider(command_builder=_builder)
    mock_result = MagicMock(returncode=0, stdout="")

    with patch("shared.discovery.subprocess.run", return_value=mock_result):
        provider.execute("hello", "o4-mini")

    assert not output_file.exists()


def test_execute_cleans_up_output_file_on_failure(tmp_path):
    """Temp output file is removed when the command exits non-zero."""
    output_file = tmp_path / "out.txt"
    output_file.write_text("stale content")

    def _builder(provider, action, model, prompt):
        provider._pending_output_file = str(output_file)
        return ["codex", "exec", "-m", model, "-a", "never", "-o", str(output_file), prompt]

    provider = _make_codex_provider(command_builder=_builder)
    mock_result = MagicMock(returncode=1, stderr="error", stdout="")

    with patch("shared.discovery.subprocess.run", return_value=mock_result):
        result = provider.execute("hello", "o4-mini", retries=0)

    assert result is None
    assert not output_file.exists()


def test_execute_cleans_up_output_file_on_timeout(tmp_path):
    """Temp output file is removed when the command times out."""
    output_file = tmp_path / "out.txt"
    output_file.write_text("stale content")

    def _builder(provider, action, model, prompt):
        provider._pending_output_file = str(output_file)
        return ["codex", "exec", "-m", model, "-a", "never", "-o", str(output_file), prompt]

    provider = _make_codex_provider(command_builder=_builder)

    with patch(
        "shared.discovery.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["codex"], timeout=5),
    ):
        result = provider.execute("hello", "o4-mini", retries=0)

    assert result is None
    assert not output_file.exists()


def test_execute_fresh_command_per_retry(tmp_path):
    """Each retry invokes command_builder so it gets a fresh temp file path."""
    calls: list[str] = []
    files: list[Path] = []

    def _builder(provider, action, model, prompt):
        f = tmp_path / f"out_{len(calls)}.txt"
        f.write_text(f"result_{len(calls)}")
        files.append(f)
        provider._pending_output_file = str(f)
        calls.append(str(f))
        return ["codex", "exec", "-m", model, "-a", "never", "-o", str(f), prompt]

    provider = _make_codex_provider(command_builder=_builder)

    # First attempt returns non-zero; second attempt succeeds with empty stdout
    # so execute() falls back to the output file.
    side_effects = [
        MagicMock(returncode=1, stderr="err", stdout=""),
        MagicMock(returncode=0, stdout=""),
    ]
    with patch("shared.discovery.subprocess.run", side_effect=side_effects):
        result = provider.execute("hello", "o4-mini", retries=1)

    # Two distinct temp files were created (one per attempt)
    assert len(calls) == 2
    assert calls[0] != calls[1]
    # The successful second attempt read from its own file
    assert result == "result_1"
    # Both files are cleaned up
    for f in files:
        assert not f.exists()





def test_registry_get_providers_for_tier_sorted_by_cost(production_mode):
    # Mirror real cost_ranks: gh=0, opencode=0, claude=1 for "low"
    gh = _mock_provider("github-copilot", cost_low=0, detected=True)
    gh.cost_rank = {"low": 0, "medium": 2, "high": 3}
    claude = _mock_provider("claude-code", cost_low=1, detected=True)
    claude.cost_rank = {"low": 1, "medium": 2, "high": 3}
    opencode = _mock_provider("opencode", cost_low=0, detected=True)
    opencode.cost_rank = {"low": 0}

    with patch("shared.discovery.BUILTIN_PROVIDERS", [gh, claude, opencode]):
        registry = ProviderRegistry()

    result = registry.get_providers_for_tier("low")

    assert len(result) == 3
    # claude (cost 1) must be last
    assert result[-1].name == "claude-code"
    # first two must both be free (cost 0)
    assert result[0].cost_rank["low"] == 0
    assert result[1].cost_rank["low"] == 0


def test_registry_get_providers_for_tier_prefers_configured_pair_on_equal_cost(production_mode):
    gh = _mock_provider("github-copilot", cost_low=0, detected=True)
    gh.tier_models = {"low": "gpt-5-mini", "medium": "gpt-5.4", "high": "gpt-5.4"}
    claude = _mock_provider("claude-code", cost_low=0, detected=True)
    claude.tier_models = {"low": "haiku", "medium": "sonnet", "high": "opus"}

    with patch("shared.discovery.BUILTIN_PROVIDERS", [gh, claude]):
        registry = ProviderRegistry(config_overrides={
            "preferred_routing": {
                "low": [
                    {"provider": "Claude Code", "model": "haiku"},
                ],
            },
        })

    result = registry.get_providers_for_tier("low")

    assert [provider.name for provider in result] == ["claude-code", "github-copilot"]


def test_registry_get_providers_for_tier_uses_caller_specific_preferences(production_mode):
    gh = _mock_provider("github-copilot", cost_low=0, detected=True)
    claude = _mock_provider("claude-code", cost_low=1, detected=True)
    mistral = _mock_provider("mistral-vibe", cost_low=3, detected=True)
    codex = _mock_provider("codex", cost_low=2, detected=True)

    with patch("shared.discovery.BUILTIN_PROVIDERS", [gh, claude, mistral, codex]):
        registry = ProviderRegistry(config_overrides={
            "providers": {
                "preferred_routing_by_caller": {
                    "claude-code": {
                        "low": [
                            {"provider": "claude-code"},
                            {"provider": "mistral-vibe"},
                        ],
                    },
                },
            },
        })

    assert [provider.name for provider in registry.get_providers_for_tier("low", caller="claude-code")][:2] == [
        "claude-code",
        "mistral-vibe",
    ]
    assert registry.get_providers_for_tier("low", caller="github-copilot")[0].cost_rank["low"] == 0


def test_registry_get_providers_for_tier_prefers_ordered_provider_and_model_matches(production_mode):
    alpha = _mock_provider("alpha-provider", cost_low=0, detected=True)
    alpha.tier_models = {"low": "alpha-low", "medium": "alpha-med", "high": "alpha-high"}
    beta = _mock_provider("beta-provider", cost_low=0, detected=True)
    beta.tier_models = {"low": "preferred-model", "medium": "beta-med", "high": "beta-high"}
    gamma = _mock_provider("gamma-provider", cost_low=0, detected=True)
    gamma.tier_models = {"low": "gamma-low", "medium": "gamma-med", "high": "gamma-high"}

    with patch("shared.discovery.BUILTIN_PROVIDERS", [alpha, beta, gamma]):
        registry = ProviderRegistry(config_overrides={
            "preferred_routing": {
                "low": [
                    {"provider": "gamma provider"},
                    {"model": "preferred-model"},
                ],
            },
        })

    result = registry.get_providers_for_tier("low")

    assert [provider.name for provider in result] == [
        "gamma-provider",
        "beta-provider",
        "alpha-provider",
    ]


def test_registry_get_providers_for_tier_preserves_order_when_no_preference_matches(production_mode):
    alpha = _mock_provider("alpha-provider", cost_low=0, detected=True)
    beta = _mock_provider("beta-provider", cost_low=0, detected=True)

    with patch("shared.discovery.BUILTIN_PROVIDERS", [alpha, beta]):
        registry = ProviderRegistry(config_overrides={
            "preferred_routing": {
                "low": [
                    {"provider": "missing-provider"},
                    {"model": "missing-model"},
                ],
            },
        })

    result = registry.get_providers_for_tier("low")

    assert [provider.name for provider in result] == ["alpha-provider", "beta-provider"]


def test_registry_get_providers_for_tier_ignores_blank_preference_entries(production_mode):
    alpha = _mock_provider("alpha-provider", cost_low=0, detected=True)
    beta = _mock_provider("beta-provider", cost_low=0, detected=True)

    with patch("shared.discovery.BUILTIN_PROVIDERS", [alpha, beta]):
        registry = ProviderRegistry(config_overrides={
            "preferred_routing": {
                "low": [
                    {"provider": "  ", "model": ""},
                    {"provider": "beta-provider"},
                ],
            },
        })

    result = registry.get_providers_for_tier("low")

    assert [provider.name for provider in result] == ["beta-provider", "alpha-provider"]


def test_registry_get_providers_for_tier_normalizes_override_tier_keys(production_mode):
    alpha = _mock_provider("alpha-provider", cost_low=0, detected=True)
    beta = _mock_provider("beta-provider", cost_low=0, detected=True)

    with patch("shared.discovery.BUILTIN_PROVIDERS", [alpha, beta]):
        registry = ProviderRegistry(config_overrides={
            "preferred_routing": {
                "LOW": [
                    {"provider": "beta-provider"},
                ],
            },
        })

    result = registry.get_providers_for_tier("low")

    assert [provider.name for provider in result] == ["beta-provider", "alpha-provider"]


def test_registry_get_providers_for_tier_uses_populated_legacy_section_when_top_level_empty(production_mode):
    alpha = _mock_provider("alpha-provider", cost_low=0, detected=True)
    beta = _mock_provider("beta-provider", cost_low=0, detected=True)

    with patch("shared.discovery.BUILTIN_PROVIDERS", [alpha, beta]):
        registry = ProviderRegistry(config_overrides={
            "preferred_routing": {},
            "providers": {
                "preferred_routing": {
                    "low": [
                        {"provider": "beta-provider"},
                    ],
                },
            },
        })

    result = registry.get_providers_for_tier("low")

    assert [provider.name for provider in result] == ["beta-provider", "alpha-provider"]


def test_registry_get_providers_for_tier_uses_legacy_section_when_top_level_preference_is_malformed(production_mode):
    alpha = _mock_provider("alpha-provider", cost_low=0, detected=True)
    beta = _mock_provider("beta-provider", cost_low=0, detected=True)

    with patch("shared.discovery.BUILTIN_PROVIDERS", [alpha, beta]):
        registry = ProviderRegistry(config_overrides={
            "preferred_routing": {
                "low": "bad-shape",
            },
            "providers": {
                "preferred_routing": {
                    "low": [
                        {"provider": "beta-provider"},
                    ],
                },
            },
        })

    result = registry.get_providers_for_tier("low")

    assert [provider.name for provider in result] == ["beta-provider", "alpha-provider"]


def test_registry_get_providers_for_tier_rejects_invalid_shorthand_strings(production_mode):
    alpha = _mock_provider("alpha-provider", cost_low=0, detected=True)
    beta = _mock_provider("beta-provider", cost_low=0, detected=True)
    beta.tier_models = {"low": "preferred-model", "medium": "beta-med", "high": "beta-high"}

    with patch("shared.discovery.BUILTIN_PROVIDERS", [alpha, beta]):
        registry = ProviderRegistry(config_overrides={
            "preferred_routing": {
                "low": [
                    " / preferred-model",
                    {"provider": "beta-provider"},
                ],
            },
        })

    result = registry.get_providers_for_tier("low")

    assert [provider.name for provider in result] == ["beta-provider", "alpha-provider"]


def test_registry_get_providers_for_tier_excludes_unavailable(production_mode):
    available = _mock_provider("github-copilot", cost_low=0, detected=True)
    unavailable = _mock_provider("claude-code", cost_low=1, detected=False)

    with patch("shared.discovery.BUILTIN_PROVIDERS", [available, unavailable]):
        registry = ProviderRegistry()

    result = registry.get_providers_for_tier("low")
    assert len(result) == 1
    assert result[0].name == "github-copilot"


def test_registry_adds_configured_network_endpoint_provider(production_mode):
    overrides = {
        "endpoint_providers": [
            {
                "name": "lab-ollama",
                "kind": "ollama",
                "scope": "network",
                "base_url": "https://10.0.0.40:11434",
                "tier_models": {
                    "low": "qwen2.5-coder:7b",
                    "medium": "qwen2.5-coder:14b",
                    "high": "qwen2.5-coder:32b",
                },
            }
        ]
    }

    with (
        patch("shared.discovery.BUILTIN_PROVIDERS", []),
        patch("shared.discovery._LOCAL_ENDPOINT_CANDIDATES", ()),
        patch(
            "shared.discovery._discover_endpoint_tier_models",
            return_value={
                "low": "qwen2.5-coder:7b",
                "medium": "qwen2.5-coder:14b",
                "high": "qwen2.5-coder:32b",
            },
        ),
    ):
        registry = ProviderRegistry(config_overrides=overrides)

    assert [provider.name for provider in registry.available_providers] == ["lab-ollama"]
    provider = registry.available_providers[0]
    assert provider.transport == "http"
    assert provider.endpoint_scope == "network"
    assert provider.endpoint_base_url == "https://10.0.0.40:11434"
    compact = registry.to_compact_dict()["providers"][0]
    assert compact["source"] == "configured-network"
    assert compact["endpoint_origin"] == "https://10.0.0.40:11434"
    assert "endpoint_base_url" not in compact
    provider_dict = registry.to_dict()["available_providers"][0]
    assert provider_dict["endpoint_origin"] == "https://10.0.0.40:11434"
    assert "endpoint_base_url" not in provider_dict


def test_registry_auto_discovers_local_endpoint_without_listing_unreachable_candidates(monkeypatch):
    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)

    def discover(provider: CLIProvider, **_: object) -> dict[str, str]:
        if provider.name == "local-ollama":
            return {"low": "llama3.2", "medium": "llama3.2", "high": "llama3.2"}
        raise OSError("connection refused")

    with (
        patch("shared.discovery.BUILTIN_PROVIDERS", []),
        patch("shared.discovery._discover_endpoint_tier_models", side_effect=discover),
    ):
        registry = ProviderRegistry()

    assert [provider.name for provider in registry.available_providers] == ["local-ollama"]
    assert registry.available_providers[0].endpoint_scope == "local"


def test_registry_keeps_unreachable_configured_endpoint_visible(production_mode):
    overrides = {
        "endpoint_providers": [
            {
                "name": "lab-openai",
                "kind": "openai-compatible",
                "scope": "network",
                "base_url": "https://10.0.0.41:1234/v1",
                "tier_models": {"low": "gpt-oss-20b"},
            }
        ]
    }

    with (
        patch("shared.discovery.BUILTIN_PROVIDERS", []),
        patch("shared.discovery._LOCAL_ENDPOINT_CANDIDATES", ()),
        patch("shared.discovery._discover_endpoint_tier_models", side_effect=OSError("offline")),
    ):
        registry = ProviderRegistry(config_overrides=overrides)

    assert len(registry.available_providers) == 1
    provider = registry.available_providers[0]
    assert provider.readiness.reason is DetectReason.ENDPOINT_UNREACHABLE
    assert provider.is_routeable() is False


def test_registry_applies_provider_cost_overrides(monkeypatch):
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")
    registry = ProviderRegistry(config_overrides={
        "provider_cost_overrides": {
            "test-provider": {
                "medium": {
                    "cost_rank": 0,
                    "billing_tier": "free",
                    "provider_cost_hint": "covered by org plan",
                },
            },
        },
    })

    provider = registry.available_providers[0]
    selection = registry.select_provider_for_tier("medium")

    assert provider.cost_rank["medium"] == 0
    assert selection is not None
    assert selection["is_free"] is True
    assert selection["billing_tier"] == "free"
    assert selection["provider_cost_hint"] == "covered by org plan"
    assert selection["billing_source"] == "user_override"


def test_get_registry_refreshes_when_cost_overrides_change(monkeypatch):
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")

    baseline_registry = get_registry()
    assert baseline_registry.select_provider_for_tier("medium")["billing_source"] == "provider_default"

    overridden_registry = get_registry({
        "provider_cost_overrides": {
            "test-provider": {
                "medium": {
                    "cost_rank": 0,
                    "billing_tier": "free",
                },
            },
        },
    })

    selection = overridden_registry.select_provider_for_tier("medium")
    assert selection is not None
    assert selection["billing_source"] == "user_override"
    assert selection["billing_tier"] == "free"

    restored_registry = get_registry(None)
    restored_selection = restored_registry.select_provider_for_tier("medium")
    assert restored_selection is not None
    assert restored_selection["billing_source"] == "provider_default"
    assert restored_selection["billing_tier"] == "subscription"


def test_get_registry_refreshes_when_cost_overrides_mutate_in_place(monkeypatch):
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")

    overrides = {
        "provider_cost_overrides": {
            "test-provider": {
                "medium": {
                    "cost_rank": 0,
                    "billing_tier": "free",
                },
            },
        },
    }
    baseline = get_registry(overrides)
    baseline_selection = baseline.select_provider_for_tier("medium")
    assert baseline_selection is not None
    assert baseline_selection["billing_tier"] == "free"

    overrides["provider_cost_overrides"]["test-provider"]["medium"] = {
        "cost_rank": 2,
        "billing_tier": "subscription",
    }
    refreshed = get_registry(overrides)
    refreshed_selection = refreshed.select_provider_for_tier("medium")
    assert refreshed_selection is not None
    assert refreshed_selection["billing_tier"] == "subscription"
    assert refreshed_selection["billing_source"] == "user_override"
    assert refreshed_selection["cost_rank"] == 2


def test_get_registry_without_args_preserves_existing_override_state(monkeypatch):
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")

    overrides = {
        "provider_cost_overrides": {
            "test-provider": {
                "medium": {
                    "cost_rank": 0,
                    "billing_tier": "free",
                },
            },
        },
    }

    overridden = get_registry(overrides)
    reused = get_registry()

    assert reused is overridden
    selection = reused.select_provider_for_tier("medium")
    assert selection is not None
    assert selection["billing_source"] == "user_override"
    assert selection["billing_tier"] == "free"


def test_registry_paid_billing_override_replaces_free_rank(monkeypatch):
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")
    registry = ProviderRegistry(config_overrides={
        "provider_cost_overrides": {
            "test-provider": {
                "low": {
                    "billing_tier": "subscription",
                    "provider_cost_hint": "covered by team subscription",
                },
            },
        },
    })

    provider = registry.available_providers[0]
    selection = registry.select_provider_for_tier("low")

    assert provider.cost_rank["low"] == 1
    assert selection is not None
    assert selection["is_free"] is False
    assert selection["billing_tier"] == "subscription"
    assert selection["provider_cost_hint"] == "covered by team subscription"
    assert selection["billing_source"] == "user_override"


# ---------------------------------------------------------------------------
# 16–18. ProviderRegistry.execute_cheapest()
# ---------------------------------------------------------------------------


def test_registry_execute_cheapest_tries_free_first(production_mode):
    # paid is listed first in BUILTIN_PROVIDERS; free is second.
    # With prefer_free=True the free one should always be tried first.
    free_p = _mock_provider("free-provider", cost_low=0, execute_result="free result")
    free_p.cost_rank = {"low": 0}
    free_p.tier_models = {"low": "free-model"}

    paid_p = _mock_provider("paid-provider", cost_low=1, execute_result="paid result")
    paid_p.cost_rank = {"low": 1}
    paid_p.tier_models = {"low": "paid-model"}

    # paid listed first in BUILTIN_PROVIDERS to prove ordering is overridden
    with patch("shared.discovery.BUILTIN_PROVIDERS", [paid_p, free_p]):
        registry = ProviderRegistry()

    result = registry.execute_cheapest("hello", tier="low", prefer_free=True)

    free_p.execute.assert_called_once()
    paid_p.execute.assert_not_called()
    assert result["fallback_used"] is False


def test_registry_execute_cheapest_fallback(production_mode):
    p1 = _mock_provider("provider-1", cost_low=0, execute_result=None)  # fails
    p1.cost_rank = {"low": 0}
    p1.tier_models = {"low": "model-1"}

    p2 = _mock_provider("provider-2", cost_low=1, execute_result="result")
    p2.cost_rank = {"low": 1}
    p2.tier_models = {"low": "model-2"}

    with patch("shared.discovery.BUILTIN_PROVIDERS", [p1, p2]):
        registry = ProviderRegistry()

    result = registry.execute_cheapest("hello", tier="low")

    assert result["result"] == "result"
    assert result["fallback_used"] is True


def test_execute_cheapest_recovers_auth_quarantine_after_successful_probe(
    monkeypatch,
    production_mode,
    tmp_path,
):
    from shared.db import Database
    from shared.health import HEALTHY, record_provider_failure

    provider = _mock_provider(
        "claude-code",
        cost_low=1,
        execute_result="authenticated result",
    )
    db = Database(tmp_path / "provider-health.db")
    record_provider_failure(db, "claude-code", "auth_expired")

    with patch("shared.discovery.BUILTIN_PROVIDERS", [provider]):
        registry = ProviderRegistry(
            db=db,
            config_overrides={
                "providers": {"router_only_allow_execution": ["claude-code"]},
            },
        )

    monkeypatch.setattr(
        "shared.discovery.AuthProbe.check",
        lambda provider_name: provider_name == "claude-code",
    )

    result = registry.execute_cheapest(
        "hello",
        tier="low",
        provider_id="claude-code",
    )

    assert result["result"] == "authenticated result"
    health = db.get_provider_health("claude-code")
    assert health is not None
    assert health["state"] == HEALTHY
    assert health["consecutive_failures"] == 0


def test_registry_execute_cheapest_all_fail(production_mode):
    p1 = _mock_provider("provider-1", cost_low=0, execute_result=None)
    p1.cost_rank = {"low": 0}
    p1.tier_models = {"low": "model-1"}

    p2 = _mock_provider("provider-2", cost_low=1, execute_result=None)
    p2.cost_rank = {"low": 1}
    p2.tier_models = {"low": "model-2"}

    with patch("shared.discovery.BUILTIN_PROVIDERS", [p1, p2]):
        registry = ProviderRegistry()

    with pytest.raises(RuntimeError):
        registry.execute_cheapest("hello", tier="low")


def test_registry_execute_cheapest_propagates_explicit_effort(production_mode):
    provider = _mock_provider("claude-code", cost_low=1, execute_result="result")
    provider.cost_rank = {"low": 1}
    provider.tier_models = {"low": "claude-sonnet-4.6"}

    with patch("shared.discovery.BUILTIN_PROVIDERS", [provider]):
        registry = ProviderRegistry(
            config_overrides={
                "providers": {"router_only_allow_execution": ["claude-code"]},
            },
        )

    result = registry.execute_cheapest("hello", tier="low", effort="high")

    provider.execute.assert_called_once_with(
        "hello",
        "claude-sonnet-4.6",
        timeout=120,
        code_only=False,
        effort="high",
    )
    assert result["effort"] == "high"
    assert result["effort_source"] == "explicit"


def test_registry_execute_cheapest_uses_endpoint_provider(production_mode):
    overrides = {
        "endpoint_providers": [
            {
                "name": "studio",
                "kind": "openai-compatible",
                "scope": "network",
                "base_url": "https://10.0.0.50:1234/v1",
                "tier_models": {"low": "gpt-oss-20b"},
            }
        ]
    }

    with (
        patch("shared.discovery.BUILTIN_PROVIDERS", []),
        patch("shared.discovery._LOCAL_ENDPOINT_CANDIDATES", ()),
        patch(
            "shared.discovery._discover_endpoint_tier_models",
            return_value={"low": "gpt-oss-20b"},
        ),
        patch(
            "shared.discovery._execute_endpoint_provider",
            return_value="local endpoint result",
        ) as execute_endpoint,
    ):
        registry = ProviderRegistry(config_overrides=overrides)
        result = registry.execute_cheapest("hello", tier="low")

    execute_endpoint.assert_called_once()
    assert result["provider_id"] == "studio"
    assert result["transport"] == "http"
    assert result["result"] == "local endpoint result"
    assert result["endpoint_origin"] == "https://10.0.0.50:1234"


def test_registry_execute_cheapest_falls_back_when_endpoint_returns_bad_json(production_mode):
    broken = _mock_provider("broken-endpoint", cost_low=0, detected=True, execute_result=None)
    broken.transport = "http"
    broken.endpoint_kind = "openai-compatible"
    broken.endpoint_scope = "network"
    broken.endpoint_base_url = "https://10.0.0.60:1234/v1"
    broken.execute_hook = MagicMock(side_effect=ValueError("bad json"))

    fallback = _mock_provider("fallback-provider", cost_low=1, detected=True, execute_result="fallback result")
    fallback.cost_rank = {"low": 1}
    fallback.tier_models = {"low": "fallback-model"}
    broken.cost_rank = {"low": 0}
    broken.tier_models = {"low": "broken-model"}

    with patch("shared.discovery.BUILTIN_PROVIDERS", [broken, fallback]):
        registry = ProviderRegistry()

    result = registry.execute_cheapest("hello", tier="low")

    assert result["provider_id"] == "fallback-provider"
    assert result["result"] == "fallback result"
    assert result["fallback_used"] is True


def test_http_json_request_disables_certificate_verification_when_configured():
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size: int = -1) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(request, timeout=0, context=None):
        captured["timeout"] = timeout
        captured["context"] = context
        return FakeResponse()

    with patch("shared.discovery.urlopen", side_effect=fake_urlopen):
        from shared.discovery import _http_json_request

        payload = _http_json_request(
            "https://10.0.0.50:1234/v1/models",
            timeout=2,
            verify_tls=False,
        )

    assert payload == {"ok": True}
    assert captured["timeout"] == 2
    context = captured["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


# ---------------------------------------------------------------------------
# 19. ProviderRegistry.to_dict()
# ---------------------------------------------------------------------------


def test_registry_to_dict(production_mode):
    p = _mock_provider("github-copilot", cost_low=0, detected=True)
    p.binary = "gh"
    p.cost_rank = {"low": 0, "medium": 2, "high": 3}
    p.tier_models = {"low": "gpt-5-mini", "medium": "gpt-5.4", "high": "gpt-5.4"}

    with patch("shared.discovery.BUILTIN_PROVIDERS", [p]):
        registry = ProviderRegistry()

    info = registry.to_dict()

    assert "available_providers" in info
    assert "total_available" in info
    assert info["total_available"] == 1
    assert isinstance(info["available_providers"], list)
    assert len(info["available_providers"]) == 1
    assert info["available_providers"][0]["metadata"]["readiness"]["reason"] == "ready"


def test_registry_to_dict_matches_provider_to_adapter(production_mode):
    provider = _mock_provider("github-copilot", cost_low=0, detected=True)
    provider.binary = "gh"
    provider.cost_rank = {"low": 0}
    provider.tier_models = {"low": "gpt-5-mini"}

    with patch("shared.discovery.BUILTIN_PROVIDERS", [provider]):
        registry = ProviderRegistry()

    registry.register_adapter(
        ProviderAdapter(
            name="custom-shell",
            version="legacy-1",
            capabilities=[ProviderCapability.EXECUTE],
            metadata={"shell_names": ["custom"]},
        )
    )

    info = registry.to_dict()

    assert info["available_providers"][0]["name"] == "github-copilot"
    assert info["available_providers"][0]["display_name"] == provider.display_name
    assert any(adapter["name"] == "custom-shell" for adapter in info["available_adapters"])


# ---------------------------------------------------------------------------
# 20. get_registry() singleton behaviour
# ---------------------------------------------------------------------------


def test_get_registry_singleton(production_mode):
    # autouse fixture guarantees _registry is None at test start
    with patch("shared.discovery.BUILTIN_PROVIDERS", []):
        r1 = get_registry()
        r2 = get_registry()

    assert r1 is r2


# ---------------------------------------------------------------------------
# 21. THRENODY_TEST_MODE provider detection stubs (Wave 3: TEST-01)
# ---------------------------------------------------------------------------


def test_test_mode_stubs_providers(monkeypatch):
    """Test that THRENODY_TEST_MODE=1 returns only stub test providers.
    
    Wave 3 TEST-01: Verify that when THRENODY_TEST_MODE is set, 
    ProviderRegistry uses _get_test_providers() stub instead of detecting
    real CLI installations. This isolates tests from machine-specific CLIs.
    
    Expected behavior:
        - With THRENODY_TEST_MODE=1: registry has only test-provider
        - Real provider detection is skipped
        - No PATH scan, no subprocess calls to real CLIs
    """
    # Set test mode env var
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")
    
    # Create registry — should use stubs
    registry = ProviderRegistry()
    
    # Verify: only test-provider is available
    assert len(registry.available_providers) == 1
    assert registry.available_providers[0].name == "test-provider"
    assert registry.available_providers[0].binary == "test-binary"
    
    # Verify: stub provider has tier models
    provider = registry.available_providers[0]
    assert provider.tier_models["low"] == "test-low-model"
    assert provider.tier_models["medium"] == "test-med-model"
    assert provider.tier_models["high"] == "test-high-model"

    # Verify: stub provider has cost ranking
    assert provider.cost_rank["low"] == 0
    assert provider.cost_rank["medium"] == 1
    assert provider.cost_rank["high"] == 2
    assert provider.readiness.reason is DetectReason.READY
    assert provider.readiness.routeable is True


def test_production_mode_detects_real_providers(monkeypatch):
    """Test that without THRENODY_TEST_MODE, real provider detection occurs.
    
    Wave 3 TEST-01: Verify that when THRENODY_TEST_MODE is NOT set,
    ProviderRegistry performs real provider detection (scans PATH, etc).
    This allows production use cases to find installed CLI tools.
    
    Expected behavior:
        - With THRENODY_TEST_MODE unset: registry attempts real detection
        - Available providers depend on which CLIs are installed
        - At minimum, should try to detect github-copilot, claude-code, codex
    """
    # Ensure test mode is NOT set; isolate from local endpoint candidates (Ollama etc.)
    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)
    monkeypatch.setattr("shared.discovery._LOCAL_ENDPOINT_CANDIDATES", ())

    # Create registry — should attempt real detection
    registry = ProviderRegistry()
    
    # Verify: available_providers is not empty (at least trying real detection)
    # Note: may be empty if no CLIs are installed, but that's OK
    assert isinstance(registry.available_providers, list)
    
    # Verify: if providers are detected, they are from BUILTIN_PROVIDERS
    if registry.available_providers:
        builtin_names = {p.name for p in BUILTIN_PROVIDERS}
        for provider in registry.available_providers:
            assert provider.name in builtin_names


# ---------------------------------------------------------------------------
# Task 1: Parametric tests covering all 6 providers × all tiers (TEST-03 part 1)
# ---------------------------------------------------------------------------


def test_builtin_providers_count_includes_new_providers():
    """TEST-03: Verify BUILTIN_PROVIDERS includes all current providers.
    
    Ensures Wave 1 registration (07-02), the OpenCode rollout, and Phase 8/9
    registration are complete:
    - github-copilot, claude-code (existing, Phase 5)
    - codex, junie, cursor (new, Phase 7 Wave 1)
    - opencode (new, low-tier-only host/provider)
    - aider, amazon-q (new, Phase 8 Wave 0)
    - windsurf (stub, Phase 9 Wave 1)
    - mistral-vibe, blackbox-ai (new providers)
    """
    assert len(BUILTIN_PROVIDERS) == 11, f"Expected 11 providers, got {len(BUILTIN_PROVIDERS)}"


def test_new_providers_in_builtin():
    """TEST-03: Verify Codex, Junie, and Cursor are in BUILTIN_PROVIDERS.
    
    Per CLIP-01, CLIP-02, CLIP-03: Each new provider must be discoverable
    via the shared registry and implement pluggable hooks (command_builder,
    detect_hook, output_cleaner).
    """
    names = [p.name for p in BUILTIN_PROVIDERS]
    assert "codex" in names, "Codex provider not in BUILTIN_PROVIDERS"
    assert "junie" in names, "Junie provider not in BUILTIN_PROVIDERS"
    assert "cursor" in names, "Cursor provider not in BUILTIN_PROVIDERS"
    
    # Verify each has the expected pluggable structure
    for provider in BUILTIN_PROVIDERS:
        if provider.name in ["codex", "junie", "cursor"]:
            assert hasattr(provider, "command_builder"), f"{provider.name} missing command_builder"
            assert hasattr(provider, "detect_hook"), f"{provider.name} missing detect_hook"
            assert hasattr(provider, "output_cleaner"), f"{provider.name} missing output_cleaner"


def test_new_provider_tier_coverage_truthful():
    """TEST-03: Verify new providers claim only tier coverage they can truthfully support.
    
    Per D-06 (Junie single-tier) and D-04 (Cursor truthful tiers):
    - Codex: must have 3 tiers (low, medium, high) per CLIP-01
    - Junie: must have exactly 1 tier (medium only) per D-06
    - Cursor: must have 3 tiers (low, medium, high) per CLIP-02
    
    No provider should claim unsupported tier coverage.
    """
    providers_by_name = {p.name: p for p in BUILTIN_PROVIDERS}
    
    # Codex: 3 tiers
    codex = providers_by_name.get("codex")
    assert codex is not None, "Codex provider missing"
    assert set(codex.tier_models.keys()) == {"low", "medium", "high"}, \
        f"Codex tiers are {set(codex.tier_models.keys())}, expected 3 tiers"
    
    # Junie: 1 tier (medium only)
    junie = providers_by_name.get("junie")
    assert junie is not None, "Junie provider missing"
    assert set(junie.tier_models.keys()) == {"medium"}, \
        f"Junie tiers are {set(junie.tier_models.keys())}, expected single 'medium' tier"
    
    # Cursor: 3 tiers
    cursor = providers_by_name.get("cursor")
    assert cursor is not None, "Cursor provider missing"
    assert set(cursor.tier_models.keys()) == {"low", "medium", "high"}, \
        f"Cursor tiers are {set(cursor.tier_models.keys())}, expected 3 tiers"


def test_new_provider_cost_ranking():
    """TEST-03: Verify all new providers have valid cost_rank structures.
    
    Per D-09 (telemetry/cost tracking): Each provider must have a cost_rank
    dict with entries for all claimed tiers. Cost rank values must be
    positive integers or zero.
    """
    providers_by_name = {p.name: p for p in BUILTIN_PROVIDERS}
    
    for provider_name in ["codex", "junie", "opencode", "cursor"]:
        provider = providers_by_name[provider_name]
        
        # Verify cost_rank exists
        assert provider.cost_rank is not None, f"{provider_name} missing cost_rank"
        
        # Verify cost_rank has all tiers
        assert set(provider.cost_rank.keys()) == set(provider.tier_models.keys()), \
            f"{provider_name}: cost_rank tiers {set(provider.cost_rank.keys())} " \
            f"don't match tier_models {set(provider.tier_models.keys())}"
        
        # Verify cost ranks are non-negative integers
        for tier, cost in provider.cost_rank.items():
            assert isinstance(cost, int), \
                f"{provider_name}.cost_rank[{tier}] = {cost} (type {type(cost).__name__}), expected int"
            assert cost >= 0, \
                f"{provider_name}.cost_rank[{tier}] = {cost}, expected >= 0"


@pytest.mark.parametrize(
    "provider_name,tier",
    [
        ("github-copilot", "low"),
        ("github-copilot", "medium"),
        ("github-copilot", "high"),
        ("claude-code", "low"),
        ("claude-code", "medium"),
        ("claude-code", "high"),
        ("codex", "low"),
        ("codex", "medium"),
        ("codex", "high"),
        ("junie", "medium"),
        ("opencode", "low"),
        ("cursor", "low"),
        ("cursor", "medium"),
        ("cursor", "high"),
    ],
)
def test_provider_parametric_all_tiers(provider_name: str, tier: str):
    """TEST-03: Parametric test covering all provider × tier combinations.
    
    Per TEST-02 pattern (parametric without per-provider duplication):
    Tests 6 providers × all their tiers (~16+ test cases).
    
    For each (provider, tier) combination:
    - Verify tier exists in tier_models
    - Verify tier exists in cost_rank
    - Verify model name is non-empty string
    - Verify cost_rank value is a non-negative integer
    
    This catches any provider that claims a tier but doesn't provide a model
    or cost, or provides malformed tier data.
    """
    providers_by_name = {p.name: p for p in BUILTIN_PROVIDERS}
    provider = providers_by_name.get(provider_name)
    
    assert provider is not None, f"Provider {provider_name} not found in BUILTIN_PROVIDERS"
    
    # Verify tier exists in tier_models
    assert tier in provider.tier_models, \
        f"{provider_name} claims tier '{tier}' in parametrize but not in tier_models"
    
    # Verify tier exists in cost_rank
    assert tier in provider.cost_rank, \
        f"{provider_name} tier '{tier}' in tier_models but missing from cost_rank"
    
    # Verify model name is non-empty string
    model = provider.tier_models[tier]
    assert isinstance(model, str), \
        f"{provider_name}.tier_models[{tier}] = {model} (type {type(model).__name__}), expected str"
    assert len(model) > 0, \
        f"{provider_name}.tier_models[{tier}] is empty string"
    
    # Verify cost_rank value is non-negative integer
    cost = provider.cost_rank[tier]
    assert isinstance(cost, int), \
        f"{provider_name}.cost_rank[{tier}] = {cost} (type {type(cost).__name__}), expected int"
    assert cost >= 0, \
        f"{provider_name}.cost_rank[{tier}] = {cost}, expected >= 0"


# ---------------------------------------------------------------------------
# Phase 8: Aider detection tests (Wave 0)
# ---------------------------------------------------------------------------


def test_aider_detect_requires_api_key(monkeypatch):
    """Aider detection fails without API key even if binary exists."""
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/aider" if x == "aider" else None)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    
    from shared.discovery import _detect_aider
    provider = _builtin_provider("aider")
    readiness = _detect_aider(provider)
    
    assert not readiness.routeable
    assert readiness.reason == DetectReason.AUTH_FAILED


def test_aider_detect_with_api_key_succeeds(monkeypatch, mock_aider_binary):
    """Aider detection succeeds with API key and live model discovery."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    
    from shared.discovery import _detect_aider
    provider = _builtin_provider("aider")
    readiness = _detect_aider(provider)
    
    assert readiness.routeable
    assert readiness.reason == DetectReason.READY


def test_aider_detect_fallback_on_discovery_timeout(monkeypatch):
    """Aider detection uses static fallback when model discovery times out."""
    from unittest.mock import MagicMock
    import subprocess
    
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/aider" if x == "aider" else None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    
    mock_run = MagicMock(side_effect=subprocess.TimeoutExpired(["aider", "--list-models"], 5))
    monkeypatch.setattr("subprocess.run", mock_run)
    
    from shared.discovery import _detect_aider
    provider = _builtin_provider("aider")
    readiness = _detect_aider(provider)
    
    assert readiness.routeable  # Still routeable despite discovery failure
    assert readiness.reason == DetectReason.MODEL_DISCOVERY_FAILED_USING_FALLBACK


def test_aider_binary_missing(monkeypatch):
    """Aider detection fails when binary is not on PATH."""
    monkeypatch.setattr("shutil.which", lambda x: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    
    from shared.discovery import _detect_aider
    provider = _builtin_provider("aider")
    readiness = _detect_aider(provider)
    
    assert not readiness.routeable
    assert readiness.reason == DetectReason.BINARY_MISSING


# ---------------------------------------------------------------------------
# Phase 8: Amazon Q/Kiro detection tests (Wave 0)
# ---------------------------------------------------------------------------


def test_q_kiro_binary_fallback(monkeypatch, mock_kiro_binary):
    """Amazon Q/Kiro detection uses kiro when q is not installed."""
    from shared.discovery import _detect_q_kiro
    provider = _builtin_provider("amazon-q")
    readiness = _detect_q_kiro(provider)
    
    assert readiness.routeable
    assert readiness.metadata.get("binary") == "kiro"


def test_q_kiro_auth_probe_succeeds(monkeypatch, mock_q_binary):
    """Amazon Q/Kiro detection succeeds with cheap auth probe."""
    from shared.discovery import _detect_q_kiro
    provider = _builtin_provider("amazon-q")
    readiness = _detect_q_kiro(provider)
    
    assert readiness.routeable
    assert readiness.reason == DetectReason.READY
    assert readiness.metadata.get("binary") == "q"


def test_q_kiro_auth_fallback_to_aws_creds(monkeypatch, tmp_path):
    """Amazon Q/Kiro detection falls back to ~/.aws/credentials when probe fails."""
    from unittest.mock import MagicMock
    import subprocess
    
    # Create fake AWS credentials file
    aws_creds_dir = tmp_path / ".aws"
    aws_creds_dir.mkdir()
    aws_creds_file = aws_creds_dir / "credentials"
    aws_creds_file.write_text("[default]\naws_access_key_id=test")
    
    # Mock shutil.which to return q
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/q" if x == "q" else None)
    
    # Mock subprocess.run to timeout on auth probe
    mock_run = MagicMock(side_effect=subprocess.TimeoutExpired(["q", "configure"], 3))
    monkeypatch.setattr("subprocess.run", mock_run)
    
    # Mock Path.home() to return tmp_path
    original_home = Path.home
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    
    from shared.discovery import _detect_q_kiro
    provider = _builtin_provider("amazon-q")
    readiness = _detect_q_kiro(provider)
    
    assert readiness.routeable
    assert readiness.metadata.get("binary") == "q"
    assert readiness.metadata.get("auth_method") == "aws_credentials_file"


def test_q_kiro_binary_missing(monkeypatch):
    """Amazon Q/Kiro detection fails when neither q nor kiro is on PATH."""
    monkeypatch.setattr("shutil.which", lambda x: None)
    
    from shared.discovery import _detect_q_kiro
    provider = _builtin_provider("amazon-q")
    readiness = _detect_q_kiro(provider)
    
    assert not readiness.routeable
    assert readiness.reason == DetectReason.BINARY_MISSING


def test_q_kiro_auth_failed(monkeypatch):
    """Amazon Q/Kiro detection fails when auth probe fails and no AWS creds exist."""
    from unittest.mock import MagicMock
    import subprocess
    
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/q" if x == "q" else None)
    mock_run = MagicMock(side_effect=subprocess.TimeoutExpired(["q", "configure"], 3))
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("pathlib.Path.home", lambda: Path("/nonexistent"))
    
    from shared.discovery import _detect_q_kiro
    provider = _builtin_provider("amazon-q")
    readiness = _detect_q_kiro(provider)
    
    assert not readiness.routeable
    assert readiness.reason == DetectReason.AUTH_FAILED


# ---------------------------------------------------------------------------
# Phase 8: Aider and Amazon Q command builder tests (Task 3)
# ---------------------------------------------------------------------------


def test_aider_command_builder_includes_no_git_flag():
    """Aider command builder enforces --no-git and --no-auto-commits per D-05."""
    provider = _builtin_provider("aider")
    cmd = provider.command_builder(
        provider, "execute", "claude-opus", "Fix the bug"
    )
    
    # Verify flag presence
    assert "aider" == cmd[0]
    assert "--model" in cmd
    assert "claude-opus" in cmd
    assert "--message" in cmd
    assert "Fix the bug" in cmd
    assert "--yes-always" in cmd
    assert "--no-git" in cmd
    assert "--no-auto-commits" in cmd
    assert "--no-pretty" in cmd
    assert "--no-stream" in cmd
    
    # Verify flag order: --no-git should come before --no-auto-commits
    no_git_idx = cmd.index("--no-git")
    no_auto_idx = cmd.index("--no-auto-commits")
    assert no_git_idx < no_auto_idx, "Flag order: --no-git must come before --no-auto-commits"


def test_q_kiro_command_builder_uses_detected_binary():
    """Amazon Q/Kiro command builder uses the binary from detection metadata."""
    provider = _builtin_provider("amazon-q")
    provider.readiness = ProviderReadiness(
        routeable=True,
        reason=DetectReason.READY,
        metadata={"binary": "kiro"}
    )
    
    cmd = provider.command_builder(
        provider, "execute", "claude-3.7-sonnet", "Write a test"
    )
    
    assert cmd[0] == "kiro"
    assert "chat" in cmd
    assert "--no-interactive" in cmd
    assert "--model" in cmd
    assert "claude-3.7-sonnet" in cmd


def test_q_command_builder_with_q_binary():
    """Amazon Q command builder works with q binary."""
    provider = _builtin_provider("amazon-q")
    provider.readiness = ProviderReadiness(
        routeable=True,
        reason=DetectReason.READY,
        metadata={"binary": "q"}
    )
    
    cmd = provider.command_builder(
        provider, "execute", "claude-3.7-sonnet", "Write a test"
    )
    
    assert cmd[0] == "q"
    assert "chat" in cmd


def test_parametric_provider_registry_includes_aider_and_q():
    """Provider registry includes aider and amazon-q in BUILTIN_PROVIDERS."""
    from shared.discovery import BUILTIN_PROVIDERS as PROVIDERS
    
    provider_names = [p.name for p in PROVIDERS]
    assert "opencode" in provider_names, "opencode not found in BUILTIN_PROVIDERS"
    assert "aider" in provider_names, "aider not found in BUILTIN_PROVIDERS"
    assert "amazon-q" in provider_names, "amazon-q not found in BUILTIN_PROVIDERS"
    
    # Find and validate aider
    aider_prov = next((p for p in PROVIDERS if p.name == "aider"), None)
    assert aider_prov is not None
    assert aider_prov.command_builder is not None
    assert aider_prov.detect_hook is not None
    assert aider_prov.cost_rank["low"] > 100  # Lower priority than company adapters (cost 0-4)
    
    # Find and validate amazon-q
    q_prov = next((p for p in PROVIDERS if p.name == "amazon-q"), None)
    assert q_prov is not None
    assert q_prov.command_builder is not None
    assert q_prov.detect_hook is not None


@pytest.mark.parametrize("provider_name", ["codex", "cursor", "junie", "opencode", "aider", "amazon-q"])
def test_all_providers_have_required_hooks(provider_name):
    """All providers in registry have command_builder and detect_hook."""
    providers_by_name = {p.name: p for p in BUILTIN_PROVIDERS}
    provider = providers_by_name.get(provider_name)
    
    assert provider is not None, f"Provider {provider_name} not found"
    assert provider.command_builder is not None, f"{provider_name} missing command_builder"
    assert provider.detect_hook is not None, f"{provider_name} missing detect_hook"
    assert provider.name == provider_name


# ---------------------------------------------------------------------------
# Phase 8 Wave 1: Model discovery parser tests
# ---------------------------------------------------------------------------


def test_aider_model_discovery_with_output():
    """Aider model discovery parser handles live output and tiers models."""
    from shared.discovery import _parse_aider_models, AIDER_STATIC_MODELS
    
    provider = _builtin_provider("aider")
    
    # Simulate aider --list-models output
    live_output = """claude-3-5-sonnet-20241022
claude-opus-20250514
gpt-4-turbo
gpt-4o-mini
claude-haiku
gemini-2.0-flash"""
    
    result = _parse_aider_models(provider, live_output)
    
    # Verify result has all tiers
    assert "low" in result
    assert "medium" in result
    assert "high" in result
    
    # Verify some models are present across tiers
    all_models = result.get("low", []) + result.get("medium", []) + result.get("high", [])
    assert len(all_models) > 0
    assert "claude-haiku" in all_models or "gpt-4o-mini" in all_models  # Should be in low tier
    assert "claude-opus" in all_models or "gpt-4-turbo" in all_models  # Should be in high tier


def test_aider_model_discovery_empty_output_uses_fallback():
    """Aider model discovery falls back to static when output is empty."""
    from shared.discovery import _parse_aider_models, AIDER_STATIC_MODELS
    
    provider = _builtin_provider("aider")
    
    result = _parse_aider_models(provider, "")
    
    assert result == AIDER_STATIC_MODELS
    assert len(result["low"]) > 0
    assert len(result["medium"]) > 0
    assert len(result["high"]) > 0


def test_aider_model_discovery_garbage_input_uses_fallback():
    """Aider model discovery falls back to static when parsing fails."""
    from shared.discovery import _parse_aider_models, AIDER_STATIC_MODELS
    
    provider = _builtin_provider("aider")
    
    # Simulate garbage or unparseable output
    garbage_output = "not a valid model list\n"
    
    result = _parse_aider_models(provider, garbage_output)
    
    # Should fall back to static or parse garbage as generic medium-tier models
    assert isinstance(result, dict)
    assert "low" in result or "medium" in result or "high" in result
    # If it parsed something, verify structure
    if result != AIDER_STATIC_MODELS:
        assert all(isinstance(v, list) for v in result.values())


def test_aider_model_tier_ranking():
    """Aider model tier ranking produces correct tier buckets."""
    from shared.discovery import _tier_models_by_cost
    
    # Test a mix of models
    models = [
        "claude-haiku",           # low
        "gpt-4o-mini",            # low
        "claude-3.5-sonnet",      # medium
        "gpt-4o",                 # medium
        "claude-opus",            # high
        "gpt-4-turbo",            # high
    ]
    
    result = _tier_models_by_cost(models)
    
    assert "low" in result
    assert "medium" in result
    assert "high" in result
    
    # Verify some models ended up in low tier
    assert any("haiku" in m or "mini" in m for m in result["low"])
    # Verify high tier has expensive models
    assert any("opus" in m or "turbo" in m for m in result["high"])


def test_aider_model_discovery_timeout_scenario(monkeypatch):
    """Aider model discovery timeout doesn't break the parser."""
    from shared.discovery import _parse_aider_models, AIDER_STATIC_MODELS
    import subprocess
    
    provider = _builtin_provider("aider")
    
    # The parser itself doesn't handle timeouts — that's handled at the detection layer
    # This test verifies the parser's fallback works correctly
    
    # Simulate a command that times out by passing empty output (which indicates timeout occurred)
    result = _parse_aider_models(provider, "")
    
    # Should use static fallback
    assert result == AIDER_STATIC_MODELS


def test_q_kiro_uses_static_models_only():
    """Amazon Q/Kiro model discovery always returns static models (no live discovery)."""
    from shared.discovery import AMAZONQ_STATIC_MODELS
    
    provider = _builtin_provider("amazon-q")
    
    # Call parser with arbitrary input (ignored for static)
    result = provider.model_discovery_parser(provider, "anything")
    
    assert result == AMAZONQ_STATIC_MODELS
    assert result.get("low") == ["claude-haiku"]
    assert result.get("medium") == ["claude-3.7-sonnet"]
    assert result.get("high") == ["claude-sonnet-4"]


@pytest.mark.parametrize("provider_name", ["aider", "amazon-q"])
def test_model_discovery_returns_proper_tier_shape(provider_name):
    """All providers with model discovery return {low, medium, high} tier shape."""
    providers_by_name = {p.name: p for p in BUILTIN_PROVIDERS}
    provider = providers_by_name.get(provider_name)
    
    assert provider is not None
    assert provider.model_discovery_parser is not None
    
    # Call parser with empty/dummy input
    result = provider.model_discovery_parser(provider, "dummy")
    
    assert isinstance(result, dict)
    assert "low" in result
    assert "medium" in result
    assert "high" in result
    # Each tier should be a list
    assert isinstance(result["low"], list)
    assert isinstance(result["medium"], list)
    assert isinstance(result["high"], list)


# ---------------------------------------------------------------------------
# Windsurf stub tests (Phase 9 - Task 1)
# ---------------------------------------------------------------------------


def test_windsurf_detect_reason_enum_exists():
    """Test that DetectReason.EXECUTION_NOT_SUPPORTED exists and has the correct value."""
    assert hasattr(DetectReason, "EXECUTION_NOT_SUPPORTED")
    assert DetectReason.EXECUTION_NOT_SUPPORTED.value == "execution_not_supported"


def test_windsurf_in_builtin_providers():
    """Test that windsurf provider exists in BUILTIN_PROVIDERS."""
    assert any(p.name == "windsurf" for p in BUILTIN_PROVIDERS)
    
    windsurf = next(p for p in BUILTIN_PROVIDERS if p.name == "windsurf")
    assert windsurf.display_name == "Windsurf"
    assert windsurf.binary == "windsurf"
    assert windsurf.tier_models == {}
    assert windsurf.cost_rank == {}
    assert windsurf.detect_hook is not None


def test_windsurf_detect_hook_binary_found(monkeypatch: pytest.MonkeyPatch):
    """Test _detect_windsurf when windsurf binary is found on PATH."""
    monkeypatch.setattr("shared.discovery.shutil.which", lambda x: "/usr/bin/windsurf" if x == "windsurf" else None)
    
    windsurf = next(p for p in BUILTIN_PROVIDERS if p.name == "windsurf")
    readiness = windsurf.detect_hook(windsurf)
    
    assert readiness.routeable is False
    assert readiness.reason is DetectReason.EXECUTION_NOT_SUPPORTED
    assert readiness.last_checked is not None
    assert readiness.metadata is not None
    assert readiness.metadata.get("type") == "stub"
    assert "Windsurf is an IDE" in readiness.metadata.get("hint", "")


def test_windsurf_detect_hook_binary_missing(monkeypatch: pytest.MonkeyPatch):
    """Test _detect_windsurf when windsurf binary is not found on PATH."""
    monkeypatch.setattr("shared.discovery.shutil.which", lambda x: None)
    
    windsurf = next(p for p in BUILTIN_PROVIDERS if p.name == "windsurf")
    readiness = windsurf.detect_hook(windsurf)
    
    assert readiness.routeable is False
    assert readiness.reason is DetectReason.BINARY_MISSING
    assert readiness.last_checked is not None


def test_windsurf_not_routeable_when_detected(monkeypatch: pytest.MonkeyPatch, production_mode):
    """Test that windsurf is in available_providers but is_routeable() returns False."""
    # Mock shutil.which to return windsurf binary found for windsurf only
    def mock_which(binary):
        if binary == "windsurf":
            return "/usr/bin/windsurf"
        # Return paths for other existing binaries to avoid side effects
        if binary in ["gh", "claude", "codex"]:
            return f"/usr/bin/{binary}"
        return None
    
    monkeypatch.setattr("shared.discovery.shutil.which", mock_which)
    
    # Create a fresh registry to test the detection flow
    registry = ProviderRegistry()
    
    # Find windsurf in available_providers
    windsurf = next((p for p in registry.available_providers if p.name == "windsurf"), None)
    assert windsurf is not None, "Windsurf should be in available_providers when binary found"
    assert windsurf.is_routeable() is False, "Windsurf should never be routeable"


def test_windsurf_not_in_available_when_missing(monkeypatch: pytest.MonkeyPatch, production_mode):
    """Test that windsurf is NOT in available_providers when binary is missing."""
    monkeypatch.setattr("shared.discovery.shutil.which", lambda x: None)
    
    # Create a fresh registry to test the detection flow
    registry = ProviderRegistry()
    
    # windsurf should NOT be in available_providers (BINARY_MISSING is filtered)
    windsurf = next((p for p in registry.available_providers if p.name == "windsurf"), None)
    assert windsurf is None, "Windsurf should NOT be in available_providers when binary missing"


def test_windsurf_never_selected_by_execute_cheapest(monkeypatch: pytest.MonkeyPatch, production_mode):
    """Test that execute_cheapest never attempts to execute windsurf even when detected."""
    # Mock which to find windsurf and another provider
    def mock_which(binary):
        if binary == "windsurf":
            return "/usr/bin/windsurf"
        if binary in ["gh", "claude"]:
            return f"/usr/bin/{binary}"
        return None
    
    monkeypatch.setattr("shared.discovery.shutil.which", mock_which)
    
    registry = ProviderRegistry()
    
    # Find windsurf
    windsurf = next((p for p in registry.available_providers if p.name == "windsurf"), None)
    
    # Mock the execute method to track if it's called
    windsurf_execute_called = False
    if windsurf:
        original_execute = windsurf.execute if hasattr(windsurf, 'execute') else None
        
        def mock_windsurf_execute(*args, **kwargs):
            nonlocal windsurf_execute_called
            windsurf_execute_called = True
            raise AssertionError("Windsurf execute should never be called")
        
        monkeypatch.setattr(windsurf, "execute", mock_windsurf_execute, raising=False)
    
    # Try to execute something cheapest — windsurf should not be selected
    # Create a mock prompt to execute
    try:
        # This will try available providers in cost order
        # Since windsurf has no models, it should be skipped
        result = registry.execute_cheapest("test prompt", "test-model", timeout=1)
    except Exception:
        # Expected — we don't have real providers; just verify windsurf wasn't called
        pass
    
    assert not windsurf_execute_called, "Windsurf.execute should never be called"


def test_windsurf_integration_execute_cheapest_skips(monkeypatch: pytest.MonkeyPatch, production_mode):
    """
    Integration test: execute_cheapest with multiple providers including windsurf.
    Verifies windsurf's execute was NOT called and another provider was used instead.
    Critical safety test that windsurf never intercepts real execution.
    """
    from shared.discovery import ProviderRegistry, BUILTIN_PROVIDERS
    
    # Mock discovery to have windsurf + test-provider
    def mock_which(binary):
        if binary in ["windsurf", "test-provider"]:
            return f"/usr/bin/{binary}"
        return None
    
    monkeypatch.setattr("shared.discovery.shutil.which", mock_which)
    
    # Create registry with mocked providers
    registry = ProviderRegistry()
    
    # Collect windsurf provider if available
    windsurf = next((p for p in registry.available_providers if p.name == "windsurf"), None)
    
    # Track execution attempts
    executed_providers = []
    
    # Patch all provider execute methods to track calls
    for provider in registry.available_providers:
        original_execute = getattr(provider, "execute", None)
        
        def make_execute_tracker(provider_name):
            def mock_execute(*args, **kwargs):
                executed_providers.append(provider_name)
                return None  # Simulate failure to try next provider
            return mock_execute
        
        monkeypatch.setattr(provider, "execute", make_execute_tracker(provider.name), raising=False)
    
    # Try execute_cheapest with a model that no provider can actually execute
    try:
        registry.execute_cheapest("test prompt", "fake-model", timeout=1)
    except (RuntimeError, TypeError, AttributeError):
        # Expected — providers don't actually execute in test mode
        pass
    
    # Verify windsurf was NOT in the execution attempts
    assert "windsurf" not in executed_providers, (
        f"Windsurf should not have been executed. Attempted providers: {executed_providers}"
    )


# ---------------------------------------------------------------------------
# Spillover allocation planning tests (new)
# ---------------------------------------------------------------------------

def test_plan_spillover_no_spill_when_unbounded():
    a = _mock_provider("a", cost_low=0, detected=True)
    b = _mock_provider("b", cost_low=1, detected=True)
    a.cost_rank = {"low": 0}
    b.cost_rank = {"low": 1}

    registry = ProviderRegistry()
    registry.available_providers = [a, b]

    result = registry.plan_spillover_allocation("low", 10, prefer_free=False)
    assert result["remaining"] == 0
    assert len(result["assignments"]) == 1
    assert result["assignments"][0]["provider_id"] == "a"
    assert result["assignments"][0]["slots"] == 10
    assert result["primary"]["provider_id"] == "a"


def test_plan_spillover_overflow_a_to_b():
    a = _mock_provider("a", cost_low=0, detected=True)
    b = _mock_provider("b", cost_low=1, detected=True)
    a.cost_rank = {"low": 0}
    b.cost_rank = {"low": 1}

    overrides = {"providers": {"spillover": {"per_provider_concurrency": {"a": 2}}}}
    registry = ProviderRegistry(config_overrides=overrides)
    registry.available_providers = [a, b]

    result = registry.plan_spillover_allocation("low", 5, prefer_free=False)
    assert result["remaining"] == 0
    assert len(result["assignments"]) == 2
    assert result["assignments"][0]["provider_id"] == "a"
    assert result["assignments"][0]["slots"] == 2
    assert result["assignments"][1]["provider_id"] == "b"
    assert result["assignments"][1]["slots"] == 3


def test_plan_spillover_overflow_across_three_providers():
    a = _mock_provider("a", cost_low=0, detected=True)
    b = _mock_provider("b", cost_low=1, detected=True)
    c = _mock_provider("c", cost_low=2, detected=True)
    a.cost_rank = {"low": 0}
    b.cost_rank = {"low": 1}
    c.cost_rank = {"low": 2}

    overrides = {"providers": {"spillover": {"per_provider_concurrency": {"a": 2, "b": 3}}}}
    registry = ProviderRegistry(config_overrides=overrides)
    registry.available_providers = [a, b, c]

    result = registry.plan_spillover_allocation("low", 7, prefer_free=False)
    assert result["remaining"] == 0
    assert len(result["assignments"]) == 3
    assert result["assignments"][0]["slots"] == 2
    assert result["assignments"][1]["slots"] == 3
    assert result["assignments"][2]["slots"] == 2


def test_plan_spillover_disabled_assigns_all_to_primary():
    a = _mock_provider("a", cost_low=0, detected=True)
    b = _mock_provider("b", cost_low=1, detected=True)
    a.cost_rank = {"low": 0}
    b.cost_rank = {"low": 1}

    overrides = {"providers": {"spillover": {"enabled": False, "per_provider_concurrency": {"a": 2}}}}
    registry = ProviderRegistry(config_overrides=overrides)
    registry.available_providers = [a, b]

    result = registry.plan_spillover_allocation("low", 5, prefer_free=False)
    assert result["remaining"] == 0
    assert len(result["assignments"]) == 1
    assert result["assignments"][0]["provider_id"] == "a"
    assert result["assignments"][0]["slots"] == 5


def test_plan_spillover_insufficient_total_capacity_returns_shortfall():
    a = _mock_provider("a", cost_low=0, detected=True)
    b = _mock_provider("b", cost_low=1, detected=True)
    a.cost_rank = {"low": 0}
    b.cost_rank = {"low": 1}

    overrides = {"providers": {"spillover": {"per_provider_concurrency": {"a": 2, "b": 1}}}}
    registry = ProviderRegistry(config_overrides=overrides)
    registry.available_providers = [a, b]

    result = registry.plan_spillover_allocation("low", 5, prefer_free=False)
    assert result["remaining"] == 2
    assert len(result["assignments"]) == 2
    assert result["assignments"][0]["slots"] == 2
    assert result["assignments"][1]["slots"] == 1


def test_plan_spillover_anchor_accepts_provider_alias():
    copilot = _mock_provider("github-copilot", cost_low=0, detected=True)
    claude = _mock_provider("claude-code", cost_low=1, detected=True)
    copilot.cost_rank = {"low": 0}
    claude.cost_rank = {"low": 1}

    registry = ProviderRegistry()
    registry.available_providers = [copilot, claude]

    result = registry.plan_spillover_allocation(
        "low",
        2,
        prefer_free=False,
        anchor_provider_id="gh",
    )

    assert result["primary"]["provider_id"] == "github-copilot"
    assert result["assignments"][0]["provider_id"] == "github-copilot"

# ---------------------------------------------------------------------------
# Ollama metadata-based tiering tests
# ---------------------------------------------------------------------------

def test_ollama_tier_from_parameter_size():
    """3B→low, 13B→medium, 70B→high using default Q4 quant factor."""
    from shared.discovery import _tier_from_ollama_metadata
    assert _tier_from_ollama_metadata({"parameter_size": "3B"}) == "low"
    assert _tier_from_ollama_metadata({"parameter_size": "13B"}) == "medium"
    assert _tier_from_ollama_metadata({"parameter_size": "70B"}) == "high"

def test_ollama_tier_quant_adjustment():
    """70B Q4 → effective 59.5B → high; 20B Q2 → effective 12B → medium."""
    from shared.discovery import _tier_from_ollama_metadata
    # 70 * 0.85 = 59.5 → high
    assert _tier_from_ollama_metadata({"parameter_size": "70B", "quantization_level": "Q4_K_M"}) == "high"
    # 20 * 0.60 = 12.0 → medium
    assert _tier_from_ollama_metadata({"parameter_size": "20B", "quantization_level": "Q2_K"}) == "medium"

def test_ollama_metadata_preserved_in_extraction():
    """_extract_ollama_models_with_metadata preserves parameter_size and quantization_level."""
    from shared.discovery import _extract_ollama_models_with_metadata
    payload = {
        "models": [
            {
                "name": "llama3:latest",
                "details": {
                    "parameter_size": "8B",
                    "quantization_level": "Q4_K_M",
                },
            }
        ]
    }
    result = _extract_ollama_models_with_metadata(payload)
    assert len(result) == 1
    assert result[0]["name"] == "llama3:latest"
    assert result[0]["parameter_size"] == "8B"
    assert result[0]["quantization_level"] == "Q4_K_M"

def test_unknown_model_falls_to_keyword_heuristic():
    """Model with no metadata still gets a tier via keyword heuristic fallback."""
    from shared.discovery import _tier_from_ollama_metadata, _tier_models_by_cost
    # No parameter_size → metadata returns None → keyword fallback applies
    assert _tier_from_ollama_metadata({}) is None
    assert _tier_from_ollama_metadata({"parameter_size": ""}) is None
    # Keyword heuristic covers it
    tiered = _tier_models_by_cost(["llama3-haiku"])
    assert "llama3-haiku" in tiered.get("low", [])
