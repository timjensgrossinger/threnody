"""Release version — single source of truth for Threnody."""
from __future__ import annotations

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def get_version() -> str:
    """Return the current release version string."""
    return _VERSION_FILE.read_text(encoding="utf-8").strip()


__version__ = get_version()
