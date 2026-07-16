#!/usr/bin/env python3
"""Legacy shim that exposes ClaudeCodeProvider through ProviderAdapter."""
from __future__ import annotations

from typing import Any

from shared.adapters import ProviderAdapter, ProviderCapability
from shared.claude_compat import load_claude_module

ClaudeCodeProvider = load_claude_module("providers").ClaudeCodeProvider


def _provider_factory(provider_module: Any) -> Any:
    return provider_module if callable(provider_module) else (lambda: provider_module)


def adapter_from_legacy(provider_module: Any = ClaudeCodeProvider) -> ProviderAdapter:
    """Wrap the Claude provider class in the adapter contract."""
    factory = _provider_factory(provider_module)
    instance = factory()
    return ProviderAdapter(
        name="claude",
        version="legacy-1",
        capabilities=[ProviderCapability.EXECUTE],
        metadata={
            "shell_names": ["claude", "claude-code"],
            "legacy_provider": "claude-code.providers.ClaudeCodeProvider",
            "opt_out": True,
            "opt_out_reason": "claude-code",
        },
        callables={
            "build_provider": lambda: instance,
            "run": lambda *args, **kwargs: instance.execute(*args, **kwargs),
        },
    )
