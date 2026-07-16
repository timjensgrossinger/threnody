"""Entry-point shim for the flat-layout MCP server."""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_server_module = sys.modules.get("mcp_server")
if _server_module is None:
    _server_path = _PACKAGE_ROOT / "mcp_server.py"
    _server_spec = spec_from_file_location("mcp_server", _server_path)
    if _server_spec is None or _server_spec.loader is None:
        raise ImportError(f"Unable to load MCP server from {_server_path}")
    _server_module = module_from_spec(_server_spec)
    sys.modules["mcp_server"] = _server_module
    try:
        _server_spec.loader.exec_module(_server_module)
    except Exception:
        sys.modules.pop("mcp_server", None)
        raise

main = _server_module.main

__all__ = ["main"]
