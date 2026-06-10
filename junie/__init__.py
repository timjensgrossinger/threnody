"""
junie — JetBrains Junie CLI adapter for Threnody.

Thin entry point for Junie provider detection and execution with
dual-auth support (BYOK + JetBrains-managed) and JSON telemetry extraction.
"""

# Lazy imports to avoid circular dependency with shared.discovery
def __getattr__(name):
    if name in (
        "_build_junie_command",
        "_detect_junie",
        "_parse_junie_json_output",
        "_clean_junie_output",
    ):
        from junie import providers
        return getattr(providers, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "_build_junie_command",
    "_detect_junie",
    "_parse_junie_json_output",
    "_clean_junie_output",
]
