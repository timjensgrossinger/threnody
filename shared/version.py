"""Release version — single source of truth for Threnody."""
from __future__ import annotations

from importlib.metadata import version as distribution_version
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def get_version() -> str:
    """Return the current release version string."""
    if _VERSION_FILE.exists():
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    try:
        return distribution_version("threnody-mcp")
    except Exception:
        return "0.0.0+unknown"


__version__ = get_version()
