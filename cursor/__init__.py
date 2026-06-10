"""
cursor — Cursor CLI adapter for Threnody.

Thin entry point for Cursor provider detection and execution with
strict headless binary verification per D-05.
"""

# Lazy imports to avoid circular dependency with shared.discovery
def __getattr__(name):
    if name in (
        "_build_cursor_command",
        "_clean_cursor_output",
        "_detect_cursor",
    ):
        from cursor import providers
        return getattr(providers, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "_build_cursor_command",
    "_detect_cursor",
    "_clean_cursor_output",
]
