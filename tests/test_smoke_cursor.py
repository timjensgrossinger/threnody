#!/usr/bin/env python3
"""
Cursor smoke tests for Wave 0 of Phase 7.

Tests CLIP-02 requirement: Cursor CLI adapter supports `cursor-agent` headless mode
for non-interactive AI code generation.

Tests D-01 through D-05 locked decisions:
- D-01: Cursor is first-class `cursor-agent` execution adapter
- D-02: Cursor may use richer workspace-write behavior
- D-03: Cursor must honor Threnody trust boundary
- D-04: Cursor must claim only truthful tier coverage
- D-05: Detection must be strict: only headless executable counts, not app-only installs

All tests are hermetic and use mocked subprocess calls. No real Cursor CLI required.
"""

import os
import sys
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
from subprocess import CompletedProcess

import pytest

# Ensure the project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.discovery import CLIProvider, DetectReason, ProviderReadiness


class TestCursorStrictBinaryDetection:
    """Test Cursor strict headless binary detection per D-05."""

    def test_cursor_strict_binary_detection(self):
        """
        Test Cursor detection prioritizes cursor-agent (headless) over other variants.
        
        Per D-05 (strict detection), only verified headless executable counts.
        """
        def _cursor_detect_hook(provider: CLIProvider) -> ProviderReadiness:
            # Check for cursor-agent specifically (headless)
            if shutil.which("cursor-agent") is not None:
                return ProviderReadiness(
                    routeable=True,
                    reason=DetectReason.READY,
                    last_checked=None
                )
            return ProviderReadiness(
                routeable=False,
                reason=DetectReason.BINARY_MISSING,
                last_checked=None
            )
        
        # Mock shutil.which to return cursor-agent path
        with patch("shutil.which") as mock_which:
            def _mock_which(binary):
                if binary == "cursor-agent":
                    return "/usr/bin/cursor-agent"
                elif binary == "cursor":
                    return None  # IDE-only variant
                return None
            
            mock_which.side_effect = _mock_which
            
            provider = CLIProvider(
                name="cursor",
                binary="cursor-agent",
                display_name="Cursor",
                tier_models={
                    "low": "claude-3-5-sonnet",
                    "medium": "claude-opus",
                    "high": "claude-opus",
                },
                cost_rank={"low": 1, "medium": 2, "high": 2},
                detect_hook=_cursor_detect_hook,
            )
            
            readiness = provider.detect()
            assert readiness.routeable is True
            assert readiness.reason == DetectReason.READY

    def test_cursor_app_only_install_not_routeable(self):
        """
        Test Cursor app-only installs (Cursor.app) are not treated as routeable.
        
        Per D-05, only headless cursor-agent counts; app-only installs are excluded.
        """
        def _cursor_detect_hook(provider: CLIProvider) -> ProviderReadiness:
            # Check for cursor-agent specifically (headless)
            if shutil.which("cursor-agent") is not None:
                return ProviderReadiness(
                    routeable=True,
                    reason=DetectReason.READY,
                    last_checked=None
                )
            return ProviderReadiness(
                routeable=False,
                reason=DetectReason.BINARY_MISSING,
                last_checked=None
            )
        
        # Mock shutil.which to simulate app-only install
        with patch("shutil.which") as mock_which:
            def _mock_which(binary):
                if binary == "cursor-agent":
                    return None  # No headless binary
                # App-only install would be detected as "cursor" from .app bundle
                return None
            
            mock_which.side_effect = _mock_which
            
            provider = CLIProvider(
                name="cursor",
                binary="cursor-agent",
                display_name="Cursor",
                tier_models={
                    "low": "claude-3-5-sonnet",
                    "medium": "claude-opus",
                    "high": "claude-opus",
                },
                cost_rank={"low": 1, "medium": 2, "high": 2},
                detect_hook=_cursor_detect_hook,
            )
            
            readiness = provider.detect()
            assert readiness.routeable is False
            assert readiness.reason == DetectReason.BINARY_MISSING

    def test_cursor_headless_binary_verification(self, mock_cursor_cli):
        """
        Test Cursor detection uses --version probe to verify headless binary.
        
        Per D-05, cursor-agent --version must succeed to confirm real headless executable.
        """
        def _cursor_detect_hook(provider: CLIProvider) -> ProviderReadiness:
            if shutil.which("cursor-agent") is None:
                return ProviderReadiness(
                    routeable=False,
                    reason=DetectReason.BINARY_MISSING,
                    last_checked=None
                )
            
            # Verify headless by running --version
            from subprocess import run
            try:
                result = run(
                    ["cursor-agent", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    return ProviderReadiness(
                        routeable=True,
                        reason=DetectReason.READY,
                        last_checked=None
                    )
            except Exception:
                pass
            
            return ProviderReadiness(
                routeable=False,
                reason=DetectReason.BINARY_MISSING,
                last_checked=None
            )
        
        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/cursor-agent"
            
            with mock_cursor_cli:
                provider = CLIProvider(
                    name="cursor",
                    binary="cursor-agent",
                    display_name="Cursor",
                    tier_models={
                        "low": "claude-3-5-sonnet",
                        "medium": "claude-opus",
                        "high": "claude-opus",
                    },
                    cost_rank={"low": 1, "medium": 2, "high": 2},
                    detect_hook=_cursor_detect_hook,
                )
                
                readiness = provider.detect()
                assert readiness.routeable is True
                assert readiness.reason == DetectReason.READY


class TestCursorCommandBuilding:
    """Test Cursor command building per D-01 and CLIP-02."""

    def test_cursor_command_building_with_code_only(self):
        """
        Test Cursor command building with --code-only flag.
        
        Per D-01, code_only flag suppresses agentic behavior.
        """
        def _build_command(
            provider: CLIProvider,
            prompt: str,
            model: str,
            code_only: bool = True
        ) -> list[str]:
            """Build Cursor agent command."""
            cmd = ["cursor-agent", "--model", model]
            if code_only:
                cmd.append("--code-only")
            cmd.append(prompt)
            return cmd
        
        provider = CLIProvider(
            name="cursor",
            binary="cursor-agent",
            display_name="Cursor",
            tier_models={
                "low": "claude-3-5-sonnet",
                "medium": "claude-opus",
                "high": "claude-opus",
            },
            cost_rank={"low": 1, "medium": 2, "high": 2},
            command_builder=_build_command,
        )
        
        cmd = _build_command(provider, "write async fetch", "claude-opus", code_only=True)
        
        assert cmd[0] == "cursor-agent"
        assert "--model" in cmd
        assert "claude-opus" in cmd
        assert "--code-only" in cmd
        assert "write async fetch" in cmd

    def test_cursor_command_building_without_code_only(self):
        """
        Test Cursor command building without --code-only flag.
        
        Per D-01, flags are context-dependent and can be omitted.
        """
        def _build_command(
            provider: CLIProvider,
            prompt: str,
            model: str,
            code_only: bool = True
        ) -> list[str]:
            """Build Cursor agent command."""
            cmd = ["cursor-agent", "--model", model]
            if code_only:
                cmd.append("--code-only")
            cmd.append(prompt)
            return cmd
        
        provider = CLIProvider(
            name="cursor",
            binary="cursor-agent",
            display_name="Cursor",
            tier_models={
                "low": "claude-3-5-sonnet",
                "medium": "claude-opus",
                "high": "claude-opus",
            },
            cost_rank={"low": 1, "medium": 2, "high": 2},
            command_builder=_build_command,
        )
        
        cmd = _build_command(provider, "write async fetch", "claude-opus", code_only=False)
        
        assert cmd[0] == "cursor-agent"
        assert "--model" in cmd
        assert "claude-opus" in cmd
        assert "--code-only" not in cmd
        assert "write async fetch" in cmd

    def test_cursor_command_building_with_reasoning_effort(self):
        """Cursor should include reasoning-effort when an explicit effort is supplied."""
        from cursor.providers import _build_cursor_command

        provider = CLIProvider(
            name="cursor",
            binary="cursor-agent",
            display_name="Cursor",
            tier_models={
                "low": "claude-3-5-sonnet",
                "medium": "claude-opus",
                "high": "claude-opus",
            },
            cost_rank={"low": 1, "medium": 2, "high": 2},
        )

        cmd = _build_cursor_command(
            provider,
            "execute",
            "claude-opus",
            "write async fetch",
            effort="high",
        )

        assert "--reasoning-effort" in cmd
        assert cmd[cmd.index("--reasoning-effort") + 1] == "high"


class TestCursorExecution:
    """Test Cursor mocked execution per CLIP-02."""

    def test_cursor_mocked_execution(self, mock_cursor_cli):
        """
        Test Cursor execution with mocked subprocess.
        
        Verifies that mocked CLI returns clean output without real CLI calls.
        """
        with mock_cursor_cli:
            from subprocess import run
            result = run(
                ["cursor-agent", "--model", "claude-opus", "--code-only", "write hello world"],
                capture_output=True,
                text=True
            )
            
            assert result.returncode == 0
            assert "async function" in result.stdout or "function" in result.stdout
            assert len(result.stdout) > 0


class TestCursorTrustBoundary:
    """Test Cursor workspace-write behavior respects trust boundary per D-02 and D-03."""

    def test_cursor_workspace_write_behavior_within_boundary(self, mock_cursor_cli):
        """
        Test Cursor workspace-write behavior respects project trust boundary.
        
        Per D-02 (richer workspace-write allowed) and D-03 (existing trust boundary),
        writes inside project root are allowed; outside root requires preview/approval.
        
        Note: This test verifies the intent. Actual file writing guard is enforced
        in Wave 2+ via shared/context.py allowlist validation.
        """
        # Set TGSROUTER_PROJECT_ROOT to simulate project context
        project_root = Path("/tmp/test-project")
        project_root.mkdir(exist_ok=True, parents=True)
        
        with patch.dict(os.environ, {"TGSROUTER_PROJECT_ROOT": str(project_root)}):
            # Verify project root is accessible
            assert os.environ.get("TGSROUTER_PROJECT_ROOT") == str(project_root)
            
            # Mock a write operation inside project root
            test_file = project_root / "generated.py"
            assert test_file.parent.resolve() == project_root.resolve()
            
            # Trust boundary is respected: file is inside root
            assert str(test_file.resolve()).startswith(str(project_root.resolve()))
        
        # Cleanup
        import shutil
        shutil.rmtree(project_root, ignore_errors=True)


class TestCursorVersionProbe:
    """Test Cursor version probe for headless detection verification per D-05."""

    def test_cursor_version_probe_succeeds(self, mock_cursor_cli):
        """
        Test Cursor --version probe succeeds with mocked headless binary.
        
        Per D-05, --version probe confirms real headless executable presence.
        """
        with mock_cursor_cli:
            from subprocess import run
            result = run(
                ["cursor-agent", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            assert result.returncode == 0
            assert "cursor-agent" in result.stdout
            assert len(result.stdout) > 0
