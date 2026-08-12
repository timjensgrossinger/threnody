#!/usr/bin/env python3
"""IPC framing + codec shared by the DB daemon and its clients.

Wire format: a 4-byte big-endian unsigned length prefix followed by a UTF-8
JSON body. JSON keeps the socket traffic auditable and avoids pickle
deserialization risk; a small type codec round-trips the non-JSON values that
``Database`` methods use (``Path``, ``bytes``, and ``tuple``).

The socket is a local, user-owned (0600) AF_UNIX stream — same trust boundary as
the process — but we still avoid arbitrary-object deserialization on principle.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import socket
import struct
from pathlib import Path
from typing import Any

_HEADER = struct.Struct(">I")
MAX_FRAME_BYTES = 256 * 1024 * 1024  # 256 MiB safety cap


class ProtocolError(Exception):
    """Malformed frame or codec failure."""


# ---------------------------------------------------------------------------
# Type codec — tag the few non-JSON-native types Database uses.
# ---------------------------------------------------------------------------
# Dataclasses Database methods return across the RPC boundary. Reconstruction on
# decode is limited to this whitelist on purpose — the module stays free of
# arbitrary-object deserialization even though the wire form carries a class name.
# An unregistered dataclass still crosses (as its field dict, not stringified),
# it just decodes as a dict instead of its original type.
_DATACLASS_REGISTRY: dict[str, type] = {}


def register_dataclass(cls: type) -> type:
    """Register a dataclass type so it round-trips through encode/decode intact.

    Without this, ``encode()`` falls back to ``str(value)`` for any type it does
    not recognize, and a caller on the other side of the daemon RPC gets a string
    where it expected the dataclass instance (e.g. ``lookup.status`` raising
    ``AttributeError`` because ``lookup`` decoded to a string).
    """
    _DATACLASS_REGISTRY[cls.__name__] = cls
    return cls
_TAG = "__tgs_t__"


def encode(value: Any) -> Any:
    """Recursively convert a Python value into JSON-safe form with type tags."""
    if isinstance(value, Path):
        return {_TAG: "path", "v": str(value)}
    if isinstance(value, bytes):
        return {_TAG: "bytes", "v": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return {_TAG: "tuple", "v": [encode(x) for x in value]}
    if isinstance(value, list):
        return [encode(x) for x in value]
    if isinstance(value, dict):
        # Common case — a plain string-keyed dict with no reserved-key collision —
        # keeps the lean JSON-object form. Otherwise fall back to a tagged
        # association list that losslessly preserves NON-string keys (JSON objects
        # only allow string keys) and any user key that happens to equal the codec
        # tag (which would otherwise be mis-read as a tagged envelope on decode).
        if _TAG not in value and all(isinstance(k, str) for k in value):
            return {k: encode(v) for k, v in value.items()}
        return {_TAG: "dict", "v": [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            _TAG: "dataclass",
            "cls": type(value).__name__,
            "v": {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)},
        }
    # Fallback: stringify unknown types (e.g. enums) — lossy but safe.
    return str(value)


def decode(value: Any) -> Any:
    """Inverse of :func:`encode`."""
    if isinstance(value, dict):
        tag = value.get(_TAG)
        if tag is not None:
            inner = value.get("v")
            if tag == "path":
                return Path(str(inner))
            if tag == "bytes":
                try:
                    return base64.b64decode(inner or "")
                except (ValueError, TypeError) as exc:
                    raise ProtocolError(f"invalid bytes payload: {exc}") from exc
            if tag == "tuple":
                return tuple(decode(x) for x in (inner or []))
            if tag == "dict":
                # Association list → dict, restoring non-string / tag-colliding keys.
                return {decode(k): decode(v) for k, v in (inner or [])}
            if tag == "dataclass":
                fields = {k: decode(v) for k, v in (inner or {}).items()}
                cls = _DATACLASS_REGISTRY.get(value.get("cls"))
                return cls(**fields) if cls is not None else fields
            raise ProtocolError(f"unknown type tag: {tag!r}")
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(x) for x in value]
    return value


# ---------------------------------------------------------------------------
# Frame I/O
# ---------------------------------------------------------------------------
def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("socket closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_frame(sock: socket.socket, obj: dict) -> None:
    """Serialize *obj* (already codec-encoded values allowed) and send one frame."""
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame too large: {len(body)} bytes")
    sock.sendall(_HEADER.pack(len(body)) + body)


def recv_frame(sock: socket.socket) -> dict:
    """Read exactly one frame and return the decoded JSON object."""
    header = _recv_exact(sock, _HEADER.size)
    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(f"declared frame too large: {length} bytes")
    body = _recv_exact(sock, length)
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"invalid frame body: {exc}") from exc


def make_request(kind: str, **fields: Any) -> dict:
    """Build a codec-encoded request frame."""
    return {"kind": kind, **{k: encode(v) for k, v in fields.items()}}


def make_ok(**fields: Any) -> dict:
    return {"ok": True, **{k: encode(v) for k, v in fields.items()}}


def make_err(exc_type: str, message: str) -> dict:
    return {"ok": False, "error": {"type": exc_type, "message": message}}
