"""Tests for Antigravity model discovery parser and tier mapping."""
from __future__ import annotations

import pytest

from antigravity.providers import _parse_agy_models, _parse_agy_models_safe
from shared.discovery import BUILTIN_PROVIDERS


SAMPLE_AGY_MODELS_OUTPUT = """Fetching available models...
gemini-3.7-flash-high\tGemini 3.7 Flash (High)
gemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)
gemini-3.7-flash-low\tGemini 3.7 Flash (Low)
gemini-3.6-flash-high\tGemini 3.6 Flash (High)
gemini-3.6-flash-medium\tGemini 3.6 Flash (Medium)
gemini-3.6-flash-low\tGemini 3.6 Flash (Low)
gemini-3.5-flash-high\tGemini 3.5 Flash (High)
gemini-3.5-flash-medium\tGemini 3.5 Flash (Medium)
gemini-3.5-flash-low\tGemini 3.5 Flash (Low)
gemini-3.1-pro-high\tGemini 3.1 Pro (High)
gemini-3.1-pro-low\tGemini 3.1 Pro (Low)
claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)
claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""


def test_parse_agy_models_real_output() -> None:
    provider = next(p for p in BUILTIN_PROVIDERS if p.name == "antigravity")
    result = _parse_agy_models(provider, SAMPLE_AGY_MODELS_OUTPUT)

    assert "low" in result
    assert "medium" in result
    assert "high" in result

    # Check high tier models
    assert "gemini-3.1-pro-high" in result["high"]
    assert "gemini-3.1-pro-low" in result["high"]
    assert "claude-opus-4-6-thinking" in result["high"]

    # Check low tier models
    assert "gemini-3.7-flash-low" in result["low"]
    assert "gemini-3.6-flash-low" in result["low"]
    assert "gemini-3.5-flash-low" in result["low"]

    # Check medium tier models
    assert "gemini-3.7-flash-high" in result["medium"]
    assert "gemini-3.7-flash-medium" in result["medium"]
    assert "claude-sonnet-4-6" in result["medium"]
    assert "gpt-oss-120b-medium" in result["medium"]


def test_parse_agy_models_empty() -> None:
    provider = next(p for p in BUILTIN_PROVIDERS if p.name == "antigravity")
    assert _parse_agy_models(provider, "") == {}
    assert _parse_agy_models(provider, "   \n\n  ") == {}


def test_parse_agy_models_safe_wrapper() -> None:
    provider = next(p for p in BUILTIN_PROVIDERS if p.name == "antigravity")
    # Should not raise on None or invalid input
    assert _parse_agy_models_safe(provider, None) == {}
    result = _parse_agy_models_safe(provider, SAMPLE_AGY_MODELS_OUTPUT)
    assert len(result["high"]) > 0


def test_antigravity_provider_has_parser_hook() -> None:
    provider = next(p for p in BUILTIN_PROVIDERS if p.name == "antigravity")
    assert provider.model_discovery_parser is not None
    assert provider.model_discovery_cmd == ["agy", "models"]
