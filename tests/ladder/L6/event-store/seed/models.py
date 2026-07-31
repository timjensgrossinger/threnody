from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    aggregate_id: str
    kind: str
    payload: dict[str, Any]
    expected_version: int
