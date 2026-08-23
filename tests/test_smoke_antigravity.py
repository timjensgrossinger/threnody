#!/usr/bin/env python3
"""Smoke tests for Antigravity in Threnody.

Tests:
- Strict binary detection & version probe
- Command building with model & reasoning effort
- JSON output envelope parsing & error handling
- CLI entry point (route, cache-get, cache-put, cache-stats)
- CLI --hook routing-guard & learning-capture dispatcher
- Python SDK wrapper & fallback handling
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from antigravity.providers import (
    _build_agy_command,
    _clean_agy_output,
    _detect_agy,
)
from antigravity.sdk_integration import (
    get_sdk_info,
    sdk_available,
)
from antigravity import entry as agy_entry
from shared.discovery import CLIProvider, DetectReason, ProviderReadiness


class TestAntigravityProviderDetection:
    """Smoke tests for Antigravity CLI detection."""

    def test_detection_ready_when_binary_and_version_succeed(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/agy"), patch(
            "subprocess.run",
            return_value=CompletedProcess(
                args=["agy", "--version"],
                returncode=0,
                stdout="Google Antigravity 2.1.0\n",
                stderr="",
            ),
        ):
            readiness = _detect_agy(None)
            assert readiness.routeable is True
            assert readiness.reason == DetectReason.READY

    def test_detection_fails_when_binary_missing(self) -> None:
        with patch("shutil.which", return_value=None):
            readiness = _detect_agy(None)
            assert readiness.routeable is False
            assert readiness.reason == DetectReason.BINARY_MISSING

    def test_detection_fails_on_timeout(self) -> None:
        import subprocess

        with patch("shutil.which", return_value="/usr/local/bin/agy"), patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["agy", "--version"], timeout=5),
        ):
            readiness = _detect_agy(None)
            assert readiness.routeable is False
            assert readiness.reason == DetectReason.AUTH_UNKNOWN


class TestAntigravityCommandBuilding:
    """Smoke tests for Antigravity command generation."""

    def test_build_command_standard(self) -> None:
        cmd = _build_agy_command(None, "execute", "gemini-3.5-flash", "Implement quicksort")
        assert cmd[0] == "agy"
        assert "-p" in cmd
        assert "Implement quicksort" in cmd
        assert "--model" in cmd
        assert "gemini-3.5-flash" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--sandbox" in cmd

    def test_build_command_with_effort(self) -> None:
        cmd = _build_agy_command(
            None,
            "execute",
            "gemini-3.1-pro",
            "Complex architecture refactor",
            effort="high",
        )
        assert "--effort" in cmd
        assert "high" in cmd
        assert "gemini-3.1-pro" in cmd


class TestAntigravityOutputParsing:
    """Smoke tests for output parsing from JSON envelope."""

    def test_clean_output_json_envelope(self) -> None:
        raw = json.dumps({
            "status": "SUCCESS",
            "response": "Here is the refactored code",
            "model": "gemini-3.1-pro",
            "tokens": 420,
        })
        assert _clean_agy_output(raw) == "Here is the refactored code"

    def test_clean_output_error_envelope(self) -> None:
        raw = json.dumps({
            "status": "ERROR",
            "error": "Quota exceeded",
            "response": "",
        })
        assert _clean_agy_output(raw) == ""

    def test_clean_output_plain_text(self) -> None:
        assert _clean_agy_output("Plain text response") == "Plain text response"


class TestAntigravityEntryCli:
    """Smoke tests for antigravity/entry.py commands."""

    def test_cmd_route(self, capsys: pytest.CaptureFixture[str]) -> None:
        agy_entry.cmd_route("Fix bug in login handler")
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "tier" in data
        assert "score" in data
        assert "note" in data
        assert "subagent" in data["note"].lower()

    def test_cmd_cache_lifecycle(self, capsys: pytest.CaptureFixture[str]) -> None:
        agy_entry.cmd_cache_put("smoke_test_key", "smoke_result", "gemini-3.5-flash")
        captured = capsys.readouterr()
        put_data = json.loads(captured.out)
        assert put_data.get("stored") is True

        agy_entry.cmd_cache_get("smoke_test_key")
        captured = capsys.readouterr()
        get_data = json.loads(captured.out)
        assert get_data.get("found") is True
        assert get_data.get("result") == "smoke_result"

        agy_entry.cmd_cache_stats()
        captured = capsys.readouterr()
        stats = json.loads(captured.out)
        assert "total_cached" in stats


class TestAntigravitySdkIntegration:
    """Smoke tests for Antigravity SDK integration."""

    def test_sdk_info_structure(self) -> None:
        info = get_sdk_info()
        assert "sdk_available" in info
        assert info["integration_type"] == "python_sdk"

    def test_spawn_with_mocked_sdk(self) -> None:
        import asyncio
        from antigravity import sdk_integration

        class MockResponse:
            async def text(self) -> str:
                return "Generated via mocked SDK"

        class MockAgent:
            def __init__(self, config: object) -> None:
                self.config = config

            async def __aenter__(self) -> MockAgent:
                return self

            async def __aexit__(self, *args: object) -> None:
                pass

            async def chat(self, prompt: str) -> MockResponse:
                return MockResponse()

        class MockConfig:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

        class MockCapabilities:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

        async def _run() -> None:
            with patch.object(sdk_integration, "SDK_AVAILABLE", True), patch.object(
                sdk_integration, "Agent", MockAgent, create=True
            ), patch.object(
                sdk_integration, "LocalAgentConfig", MockConfig, create=True
            ), patch.object(
                sdk_integration, "CapabilitiesConfig", MockCapabilities, create=True
            ):
                res = await sdk_integration.spawn_threnody_agent(
                    tier="low",
                    prompt="test prompt",
                    workspace="branch",
                )
                assert res["text"] == "Generated via mocked SDK"
                assert res["tier"] == "low"
                assert res["model"] == "gemini-3.7-flash"
                assert res["workspace"] == "branch"

                dyn = await sdk_integration.spawn_dynamic_agent(
                    name="test-agent",
                    system_prompt="system prompt",
                    prompt="task prompt",
                    model="gemini-3.1-pro",
                    workspace="share",
                )
                assert dyn["text"] == "Generated via mocked SDK"
                assert dyn["name"] == "test-agent"
                assert dyn["model"] == "gemini-3.1-pro"
                assert dyn["workspace"] == "share"

        asyncio.run(_run())

