"""Environment variable helpers with deprecated prefix fallbacks."""
from __future__ import annotations

import os

_TRUE = frozenset({"1", "true", "yes"})


def env_truthy(*names: str) -> bool:
    for name in names:
        if os.getenv(name, "").lower() in _TRUE:
            return True
    return False


def test_mode_enabled() -> bool:
    return env_truthy("THRENODY_TEST_MODE", "SWITCHYARD_TEST_MODE", "TGSROUTER_TEST_MODE")


def env_value(primary: str, *fallbacks: str, default: str = "") -> str:
    for name in (primary, *fallbacks):
        value = os.getenv(name)
        if value is not None and value != "":
            return value
    return default
