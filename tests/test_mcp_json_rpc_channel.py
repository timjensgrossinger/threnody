#!/usr/bin/env python3
"""Verify MCP JSON-RPC channel works end-to-end via subprocess.

All other swarm tests import mcp_server directly. This test spawns
mcp_server.py as a subprocess and sends real JSON-RPC lines over
stdin/stdout — the path that Claude Code uses in a live session.

Covers:
- initialize handshake returns protocolVersion
- tools/list includes execute_swarm and resume_swarm_confirm
- Process exits cleanly when stdin closes (no crash on EOF)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER = ROOT / "mcp_server.py"


def _send_lines(*requests: dict) -> list[str]:
    """Spawn the MCP server, pipe in requests, collect output lines."""
    input_bytes = (
        "\n".join(json.dumps(r) for r in requests) + "\n"
    ).encode()

    env = {**os.environ, "THRENODY_TEST_MODE": "1"}

    proc = subprocess.run(
        [sys.executable, str(MCP_SERVER)],
        input=input_bytes,
        capture_output=True,
        timeout=15,
        env=env,
        cwd=str(ROOT),
    )
    stdout = proc.stdout.decode(errors="replace")
    return [ln for ln in stdout.splitlines() if ln.strip()]


def _parse_json_lines(lines: list[str]) -> list[dict]:
    parsed = []
    for ln in lines:
        try:
            parsed.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return parsed


_INIT_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-channel", "version": "0.1"},
    },
}

_LIST_REQUEST = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {},
}


def test_initialize_returns_protocol_version() -> None:
    lines = _send_lines(_INIT_REQUEST)
    responses = _parse_json_lines(lines)

    init_resp = next(
        (r for r in responses if r.get("id") == 1), None
    )
    assert init_resp is not None, f"no response with id=1; got: {responses}"
    result = init_resp.get("result", {})
    assert result.get("protocolVersion") == "2024-11-05"
    assert "capabilities" in result
    assert "serverInfo" in result


def test_tools_list_includes_swarm_tools() -> None:
    lines = _send_lines(_INIT_REQUEST, _LIST_REQUEST)
    responses = _parse_json_lines(lines)

    list_resp = next(
        (r for r in responses if r.get("id") == 2), None
    )
    assert list_resp is not None, f"no response with id=2; got: {responses}"
    tools = list_resp.get("result", {}).get("tools", [])
    tool_names = {t["name"] for t in tools}

    assert "execute_swarm" in tool_names, f"execute_swarm missing; tools={tool_names}"
    assert "resume_swarm_confirm" in tool_names, (
        f"resume_swarm_confirm missing; tools={tool_names}"
    )


def test_server_exits_cleanly_on_stdin_close() -> None:
    env = {**os.environ, "THRENODY_TEST_MODE": "1"}

    proc = subprocess.run(
        [sys.executable, str(MCP_SERVER)],
        input=b"",  # immediate EOF
        capture_output=True,
        timeout=10,
        env=env,
        cwd=str(ROOT),
    )
    # Should exit 0 (or at worst non-crash exit code)
    assert proc.returncode == 0, (
        f"server exited {proc.returncode}; stderr={proc.stderr.decode()[:200]}"
    )
