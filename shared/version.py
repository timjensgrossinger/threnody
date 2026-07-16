"""Release version — single source of truth for Threnody."""
from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
_PEP440_STAGE = re.compile(
    r"^(?:(?P<epoch>\d+)!)?(?P<base>\d+(?:\.\d+)*)(?:[-.]?"
    r"(?P<stage>alpha|beta|rc|a|b)(?:[.-]?(?P<number>\d+))?)?"
    r"(?P<post>(?:[-.]?post[.-]?(?P<post_number>\d+)))?"
    r"(?P<dev>(?:[-.]?dev[.-]?(?P<dev_number>\d+)))?"
    r"(?:\+(?P<local>[0-9A-Za-z.-]+))?$",
    re.IGNORECASE,
)


def _normalize_version(value: str) -> str:
    """Normalize the release file's human-friendly prerelease spelling."""
    raw = value.strip()
    match = _PEP440_STAGE.fullmatch(raw)
    if match is None:
        return ""
    stage = {"alpha": "a", "beta": "b", "a": "a", "b": "b", "rc": "rc"}.get(
        (match.group("stage") or "").lower(),
        "",
    )
    number = match.group("number") or ""
    epoch = f"{match.group('epoch')}!" if match.group("epoch") else ""
    post = f".post{match.group('post_number')}" if match.group("post") else ""
    dev = f".dev{match.group('dev_number')}" if match.group("dev") else ""
    local = f"+{match.group('local')}" if match.group("local") else ""
    return f"{epoch}{match.group('base')}{stage}{number}{post}{dev}{local}"


def get_version() -> str:
    """Return the current release version string."""
    if _VERSION_FILE.exists():
        try:
            value = _VERSION_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value and _normalize_version(value):
            return value
    try:
        return distribution_version("threnody-mcp")
    except (PackageNotFoundError, TypeError, ValueError):
        return "0.0.0+unknown"


def get_display_version() -> str:
    """Return the human-facing release spelling used by registry metadata."""
    try:
        value = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    return value or get_version()


__version__ = get_version()
