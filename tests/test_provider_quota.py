from __future__ import annotations

import time
from typing import Any

import pytest

from shared.quota import (
    CodexQuotaAdapter,
    ProviderQuotaResult,
    ProviderQuotaService,
    ProviderQuotaSnapshot,
    UnsupportedQuotaAdapter,
)
from shared.db import Database


def test_codex_adapter_normalizes_multiple_percentage_windows(monkeypatch):
    monkeypatch.setattr("shared.quota.shutil.which", lambda _: "/usr/bin/codex")

    def runner(_requests: list[dict[str, object]], _timeout: float) -> dict[str, Any]:
        return {
            "id": 2,
            "result": {
                "rateLimits": {
                    "limitName": "ChatGPT Plus",
                    "primary": {
                        "usedPercent": 81.5,
                        "windowDurationMins": 300,
                        "resetsAt": 1_780_000_000,
                    },
                    "secondary": {
                        "usedPercent": 30,
                        "windowDurationMins": 10_080,
                        "resetsAt": 1_780_500_000,
                    },
                }
            },
        }

    result = CodexQuotaAdapter(rpc_runner=runner).collect(now=1000.0)

    assert result.status == "supported"
    assert result.source == "codex_app_server:account/rateLimits/read"
    assert len(result.snapshots) == 2
    assert result.snapshots[0].unit == "percent"
    assert result.snapshots[0].limit == 100.0
    assert result.snapshots[0].used_ratio == pytest.approx(0.815)
    assert result.snapshots[0].remaining == pytest.approx(18.5)
    assert result.snapshots[1].window_duration_seconds == pytest.approx(604_800.0)


def test_codex_adapter_reports_malformed_response(monkeypatch):
    monkeypatch.setattr("shared.quota.shutil.which", lambda _: "/usr/bin/codex")
    result = CodexQuotaAdapter(rpc_runner=lambda _r, _t: {"id": 2, "result": []}).collect(now=1000.0)
    assert result.status == "malformed"
    assert "result object" in (result.error or "")


def test_unsupported_adapter_is_explicit():
    result = UnsupportedQuotaAdapter("claude-code").collect(now=1000.0)
    assert result.status == "unsupported"
    assert result.source == "unsupported"
    assert result.snapshots == ()
    assert "machine-readable" in (result.error or "")


@pytest.mark.parametrize(
    ("provider", "surface"),
    [
        ("github-copilot", "/usage"),
        ("amazon-q", "/usage"),
        ("opencode", "local token and cost telemetry"),
        ("junie", "IDE"),
    ],
)
def test_unsupported_reason_identifies_non_structured_surface(provider, surface):
    result = UnsupportedQuotaAdapter(provider).collect(now=1000.0)
    assert result.status == "unsupported"
    assert surface in (result.error or "")


def test_quota_service_caches_and_persists():
    calls = 0

    class Adapter:
        def collect(self, *, now: float | None = None):
            nonlocal calls
            calls += 1
            observed = time.time() if now is None else now
            return ProviderQuotaResult(
                provider="codex",
                status="supported",
                source="fixture",
                observed_timestamp=observed,
                snapshots=(
                    ProviderQuotaSnapshot(
                        provider="codex",
                        window_name="fixture",
                        window_duration_seconds=3600,
                        used=90,
                        remaining=10,
                        limit=100,
                        unit="percent",
                        reset_timestamp=None,
                        observed_timestamp=observed,
                        source="fixture",
                    ),
                ),
            )

    class DB:
        def __init__(self) -> None:
            self.observations: list[dict[str, object]] = []

        def record_provider_quota_observation(self, observation: dict[str, object]) -> None:
            self.observations.append(observation)

    db = DB()
    service = ProviderQuotaService(db, ttl_seconds=60, adapters={"codex": Adapter()})  # type: ignore[arg-type]

    first = service.get("codex")
    second = service.get("codex")

    assert calls == 1
    assert first.cached is False
    assert second.cached is True
    assert len(db.observations) == 1


def test_database_persists_latest_quota_observation(tmp_path):
    db = Database(tmp_path / "quota.sqlite3")
    db.record_provider_quota_observation(
        {
            "provider": "codex",
            "status": "supported",
            "source": "fixture",
            "observed_timestamp": 1000.0,
            "windows": [{"window_name": "five-hour", "used_ratio": 0.5}],
        }
    )

    latest = db.get_latest_provider_quota_observation("codex")

    assert latest is not None
    assert latest["source"] == "fixture"
    assert latest["windows"][0]["used_ratio"] == 0.5
