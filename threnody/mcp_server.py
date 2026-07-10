"""Entry-point shim for the flat-layout MCP server."""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from mcp_server import main  # noqa: E402

__all__ = ["main"]
