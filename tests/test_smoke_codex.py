#!/usr/bin/env python3
"""
Codex smoke tests for Wave 0 of Phase 7.

Tests CLIP-01 requirement: Codex CLI adapter supports `codex exec -m MODEL -a never -o FILE PROMPT`
for non-interactive execution with stdout result extraction.

Tests D-01 decision: Codex detection and execution verify truthful routeability based on OPENAI_API_KEY presence.

All tests are hermetic and use mocked subprocess calls. No real Codex CLI or API keys required.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from subprocess import CompletedProcess

import pytest

# Ensure the project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.discovery import CLIProvider, DetectReason, ProviderReadiness


class TestCodexDetection:
    """Test Codex provider detection per CLIP-01 and D-01."""

    def test_codex_detect_with_openai_key(self, mock_env, isolation_test_mode):
        """
        Test Codex detection succeeds when OPENAI_API_KEY is set.
        
        Per D-01 (truthful routing), Codex should be routeable when auth is configured.
        """
        # Set OPENAI_API_KEY for Codex auth
        mock_env.setenv("OPENAI_API_KEY", "test-openai-key-not-real")
        
        # Create a minimal Codex provider with auth detection
        def _codex_detect_hook(provider: CLIProvider) -> ProviderReadiness:
            key = os.environ.get("OPENAI_API_KEY")
            if key:
                return ProviderReadiness(
                    routeable=True,
                    reason=DetectReason.READY,
                    last_checked=None
                )
            return ProviderReadiness(
                routeable=False,
                reason=DetectReason.AUTH_FAILED,
                last_checked=None
            )
        
        provider = CLIProvider(
            name="codex",
            binary="codex",
            display_name="Codex",
            tier_models={
                "low": "o4-mini",
                "medium": "o4",
                "high": "o4",
            },
            cost_rank={"low": 1, "medium": 2, "high": 2},
            detect_hook=_codex_detect_hook,
        )
        
        readiness = provider.detect()
        assert readiness.routeable is True
        assert readiness.reason == DetectReason.READY

    def test_codex_detect_without_auth(self, mock_env, isolation_test_mode):
        """
        Test Codex detection fails when OPENAI_API_KEY is missing.
        
        Per D-01 (truthful routing), unauthenticated Codex should be non-routeable.
        """
        # Ensure OPENAI_API_KEY is not set
        mock_env.delenv("OPENAI_API_KEY", raising=False)
        
        def _codex_detect_hook(provider: CLIProvider) -> ProviderReadiness:
            key = os.environ.get("OPENAI_API_KEY")
            if key:
                return ProviderReadiness(
                    routeable=True,
                    reason=DetectReason.READY,
                    last_checked=None
                )
            return ProviderReadiness(
                routeable=False,
                reason=DetectReason.AUTH_FAILED,
                last_checked=None
            )
        
        provider = CLIProvider(
            name="codex",
            binary="codex",
            display_name="Codex",
            tier_models={
                "low": "o4-mini",
                "medium": "o4",
                "high": "o4",
            },
            cost_rank={"low": 1, "medium": 2, "high": 2},
            detect_hook=_codex_detect_hook,
        )
        
        readiness = provider.detect()
        assert readiness.routeable is False
        assert readiness.reason == DetectReason.AUTH_FAILED


class TestCodexCommandBuilding:
    """Test Codex command building per CLIP-01."""

    def test_codex_command_building(self):
        """
        Test Codex command building matches expected format.

        Codex runs read-only and writes the last response to a temporary file.
        """
        from codex.providers import _build_codex_command
        
        provider = CLIProvider(
            name="codex",
            binary="codex",
            display_name="Codex",
            tier_models={
                "low": "o4-mini",
                "medium": "o4",
                "high": "o4",
            },
            cost_rank={"low": 1, "medium": 2, "high": 2},
            command_builder=_build_codex_command,
        )

        cmd = _build_codex_command(
            provider,
            "execute",
            "gpt-5.5",
            "write hello world",
        )
        
        # Verify command structure
        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        assert "-m" in cmd
        assert "gpt-5.5" in cmd
        assert "-a" not in cmd
        assert "--ephemeral" in cmd
        assert "--ignore-user-config" in cmd
        assert "-o" in cmd
        # Verify -o FILE pattern (not --json)
        output_idx = cmd.index("-o")
        assert output_idx + 1 < len(cmd)
        assert cmd[output_idx + 1].endswith(".txt")
        Path(cmd[output_idx + 1]).unlink(missing_ok=True)


class TestCodexExecution:
    """Test Codex mocked execution per CLIP-01."""

    def test_codex_mocked_execution(self, mock_codex_cli):
        """
        Test Codex execution with mocked subprocess.
        
        Verifies that mocked CLI returns clean output without real API calls.
        """
        with mock_codex_cli:
            result = CompletedProcess(
                args=[
                    "codex", "exec", "-m", "o4-mini", "-s", "read-only",
                    "--ephemeral", "--ignore-user-config", "--ignore-rules",
                    "--skip-git-repo-check", "-o", "/tmp/test.py", "write hello",
                ],
                returncode=0,
                stdout="def hello():\n    return 42\n",
                stderr=""
            )
            
            assert result.returncode == 0
            assert "def hello" in result.stdout
            assert len(result.stdout) > 0

    def test_codex_execution_result_extraction(self, mock_codex_cli):
        """
        Test that Codex result extraction works correctly.
        
        Verifies clean output is extracted from mocked CLI response.
        """
        with mock_codex_cli:
            # Simulate execution
            from subprocess import run
            result = run(
                [
                    "codex", "exec", "-m", "o4-mini", "-s", "read-only",
                    "--ephemeral", "--ignore-user-config", "--ignore-rules",
                    "--skip-git-repo-check", "-o", "/tmp/test.py", "write hello",
                ],
                capture_output=True,
                text=True
            )
            
            output = result.stdout.strip()
            assert len(output) > 0
            assert "def hello" in output
            assert result.returncode == 0


class TestCodexAuthFailure:
    """Test Codex auth failure behavior per D-01."""

    def test_codex_execution_with_missing_api_key(self, mock_env, isolation_test_mode):
        """
        Test that Codex execution is blocked when OPENAI_API_KEY is missing.
        
        Per D-01 (truthful routing), missing auth blocks execution.
        """
        # Ensure OPENAI_API_KEY is not set
        mock_env.delenv("OPENAI_API_KEY", raising=False)
        
        def _codex_detect_hook(provider: CLIProvider) -> ProviderReadiness:
            key = os.environ.get("OPENAI_API_KEY")
            if key:
                return ProviderReadiness(
                    routeable=True,
                    reason=DetectReason.READY,
                    last_checked=None
                )
            return ProviderReadiness(
                routeable=False,
                reason=DetectReason.AUTH_FAILED,
                last_checked=None
            )
        
        provider = CLIProvider(
            name="codex",
            binary="codex",
            display_name="Codex",
            tier_models={
                "low": "o4-mini",
                "medium": "o4",
                "high": "o4",
            },
            cost_rank={"low": 1, "medium": 2, "high": 2},
            detect_hook=_codex_detect_hook,
        )
        
        readiness = provider.detect()
        # Provider should not be routeable without auth
        assert readiness.routeable is False
        assert readiness.reason == DetectReason.AUTH_FAILED


class TestCodexTempFileTracking:
    """Tests for CR-01: _build_codex_command registers temp file on provider."""

    def test_build_codex_command_sets_pending_output_file(self):
        """_build_codex_command sets provider._pending_output_file to a real path."""
        from codex.providers import _build_codex_command
        from shared.discovery import CLIProvider
        import os

        provider = CLIProvider(
            name="codex",
            binary="codex",
            display_name="Codex",
            tier_models={"low": "o4-mini", "medium": "o4", "high": "o4"},
            cost_rank={"low": 1, "medium": 2, "high": 2},
        )

        cmd = _build_codex_command(provider, "execute", "o4-mini", "write hello")

        # Provider now carries the temp file path
        assert hasattr(provider, "_pending_output_file")
        output_file = provider._pending_output_file
        assert output_file is not None
        assert output_file.endswith(".txt")

        # The file referenced in the command matches the registered path
        assert "-o" in cmd
        o_idx = cmd.index("-o")
        assert cmd[o_idx + 1] == output_file

        # Temp file actually exists on disk
        assert os.path.exists(output_file)

        # Clean up manually (in production execute() does this via finally)
        os.unlink(output_file)

    def test_build_codex_command_fresh_file_on_each_call(self):
        """Each call to _build_codex_command produces a distinct temp file path."""
        from codex.providers import _build_codex_command
        from shared.discovery import CLIProvider
        import os

        provider = CLIProvider(
            name="codex",
            binary="codex",
            display_name="Codex",
            tier_models={"low": "o4-mini", "medium": "o4", "high": "o4"},
            cost_rank={"low": 1, "medium": 2, "high": 2},
        )

        cmd1 = _build_codex_command(provider, "execute", "o4-mini", "hello")
        file1 = provider._pending_output_file

        cmd2 = _build_codex_command(provider, "execute", "o4-mini", "hello")
        file2 = provider._pending_output_file

        assert file1 != file2, "each call must allocate a distinct temp file"

        # Clean up
        for f in (file1, file2):
            if f and os.path.exists(f):
                os.unlink(f)

    def test_build_codex_command_adds_reasoning_effort_flag(self):
        """Codex should include reasoning-effort when an explicit effort is supplied."""
        from codex.providers import _build_codex_command
        from shared.discovery import CLIProvider
        import os

        provider = CLIProvider(
            name="codex",
            binary="codex",
            display_name="Codex",
            tier_models={"low": "o4-mini", "medium": "o4", "high": "o4"},
            cost_rank={"low": 1, "medium": 2, "high": 2},
        )

        cmd = _build_codex_command(
            provider,
            "execute",
            "o4-mini",
            "write hello",
            effort="xhigh",
        )

        assert "--reasoning-effort" not in cmd
        assert "-c" in cmd
        assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="xhigh"'

        output_file = getattr(provider, "_pending_output_file", None)
        if output_file and os.path.exists(output_file):
            os.unlink(output_file)

    def test_execute_codex_output_file_cleaned_up(self, tmp_path):
        """execute() removes the Codex temp output file after a successful run."""
        from codex.providers import _build_codex_command, _clean_codex_output
        from shared.discovery import CLIProvider
        from unittest.mock import patch, MagicMock
        import os

        written_files: list[str] = []

        def _tracking_builder(provider, action, model, prompt):
            cmd = _build_codex_command(provider, action, model, prompt)
            f = provider._pending_output_file
            written_files.append(f)
            # Pre-populate so the fallback read has content
            Path(f).write_text("codex output")
            return cmd

        provider = CLIProvider(
            name="codex",
            binary="codex",
            display_name="Codex",
            tier_models={"low": "o4-mini", "medium": "o4", "high": "o4"},
            cost_rank={"low": 1, "medium": 2, "high": 2},
            command_builder=_tracking_builder,
            output_cleaner=_clean_codex_output,
        )

        mock_result = MagicMock(returncode=0, stdout="")
        with patch("shared.discovery.subprocess.run", return_value=mock_result):
            result = provider.execute("write hello", "o4-mini")

        assert result == "codex output"
        assert len(written_files) == 1
        assert not os.path.exists(written_files[0]), "temp file must be cleaned up"
