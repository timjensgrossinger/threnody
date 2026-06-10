"""
codex — OpenAI Codex CLI adapter for Threnody.

Thin entry point for Codex provider detection and execution.
"""

# Lazy imports to avoid circular dependency with shared.discovery
def __getattr__(name):
    if name in ("_build_codex_command", "_detect_codex", "_clean_codex_output"):
        from codex import providers
        return getattr(providers, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "_build_codex_command",
    "_detect_codex",
    "_clean_codex_output",
]
