"""Provider-ranked catalogs (Codex) -> tier map, freshness, and process agreement.

Fixture caches only — never the operator's real ``~/.codex`` (conftest points
``model_registry._codex_home`` at a nonexistent dir; tests here repoint it at
``tmp_path``).
"""
from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared import model_registry
from shared.db import Database
from shared.discovery import (
    BUILTIN_PROVIDERS,
    DetectReason,
    ProviderReadiness,
    ProviderRegistry,
)
from shared.model_catalog import ModelCatalog, project_official_cache
from shared.model_registry import (
    assign_provider_relative_tiers,
    bootstrap_tier_map,
    load_codex_cache,
    tier_projection,
)


def _entry(slug: str, description: str, *, priority: int, visibility: str = "list", **extra):
    return {
        "slug": slug,
        "display_name": slug.upper(),
        "description": description,
        "visibility": visibility,
        "priority": priority,
        "supported_in_api": True,
        "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high")],
        **extra,
    }


# Shaped like the real 2026-10 cache: one current fast model, three legacy
# listed models, two hidden internal ones (one with the best priority).
TODAY = [
    _entry("cur-fast", "Fast and affordable model for easier tasks.", priority=4),
    _entry("internal-reserve", "Fast and affordable agentic coding model.", priority=4, visibility="hide"),
    _entry("old-balanced", "Older balanced model for straightforward work.", priority=8),
    _entry("old-fast", "Older fast and efficient model.", priority=9),
    _entry("old-coding", "Legacy coding model.", priority=13),
    _entry("auto-review", "Automatic approval review model for Codex.", priority=43, visibility="hide"),
]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _write_cache(path: Path, models: list[dict], *, fetched_at: float | None = None) -> Path:
    payload = {"fetched_at": _iso(fetched_at or time.time()), "etag": "x", "models": models}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _project(models: list[dict], tmp_path: Path) -> tuple[dict[str, str], list]:
    result = load_codex_cache(_write_cache(tmp_path / "models_cache.json", models))
    assert result is not None
    assign_provider_relative_tiers(result.models)
    return tier_projection(result.models), result.models


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setattr(model_registry, "_codex_home", lambda: home)
    return home


def _codex_provider():
    template = next(p for p in BUILTIN_PROVIDERS if p.name == "codex")
    return copy.deepcopy(template)


# ---------------------------------------------------------------------------
# Tier assignment
# ---------------------------------------------------------------------------


def test_hidden_models_are_catalogued_but_never_routed(tmp_path: Path) -> None:
    projection, models = _project(TODAY, tmp_path)
    by_id = {m.model_id: m for m in models}
    for hidden in ("internal-reserve", "auto-review"):
        assert by_id[hidden].routeable is False
        assert by_id[hidden].tier is None
        assert by_id[hidden].tier_reason == "hidden_by_provider"
    assert not {"internal-reserve", "auto-review"} & set(projection.values())


def test_not_supported_in_api_is_excluded(tmp_path: Path) -> None:
    projection, _ = _project(
        [
            _entry("api-less", "Fast model.", priority=1, supported_in_api=False),
            _entry("usable", "Balanced model.", priority=5),
        ],
        tmp_path,
    )
    assert set(projection.values()) == {"usable"}


def test_every_tier_is_filled_from_listed_models_only(tmp_path: Path) -> None:
    projection, _ = _project(TODAY, tmp_path)
    assert set(projection) == {"low", "medium", "high"}
    # The single current model beats every legacy one on every tier, and the
    # bootstrap high id (absent from the cache) is never used.
    assert projection == {"low": "cur-fast", "medium": "cur-fast", "high": "cur-fast"}
    assert bootstrap_tier_map("codex")["high"] not in projection.values()


def test_legacy_only_fills_what_no_current_model_can(tmp_path: Path) -> None:
    projection, models = _project(
        [
            _entry("legacy-fast", "Older fast model.", priority=1),
            _entry("new-fast", "Fast and affordable model.", priority=6),
        ],
        tmp_path,
    )
    assert projection["low"] == "new-fast"
    reasons = {m.model_id: m.tier_reason for m in models}
    assert reasons["legacy-fast"].endswith(",legacy")
    assert reasons["new-fast"] == "provider_prominence:class=fast,priority=6,current"


def test_priority_breaks_ties_within_a_class(tmp_path: Path) -> None:
    projection, _ = _project(
        [
            _entry("fast-b", "Fast model.", priority=7),
            _entry("fast-a", "Fast model.", priority=3),
        ],
        tmp_path,
    )
    assert projection["low"] == "fast-a"


def test_capability_classes_map_to_their_own_tiers(tmp_path: Path) -> None:
    projection, _ = _project(
        [
            _entry("flag", "Frontier model for complex work.", priority=9),
            _entry("bal", "Balanced model for everyday coding.", priority=5),
            _entry("quick", "Small, fast model.", priority=2),
        ],
        tmp_path,
    )
    assert projection == {"low": "quick", "medium": "bal", "high": "flag"}


def test_medium_gap_falls_upward_like_select_tier_model(tmp_path: Path) -> None:
    projection, _ = _project(
        [
            _entry("flag", "Frontier model for complex work.", priority=9),
            _entry("quick", "Fast model.", priority=2),
        ],
        tmp_path,
    )
    assert projection == {"low": "quick", "medium": "flag", "high": "flag"}


def test_operator_pin_still_wins_over_prominence(tmp_path: Path) -> None:
    result = load_codex_cache(_write_cache(tmp_path / "c.json", TODAY))
    assert result is not None
    assign_provider_relative_tiers(result.models, pins={"old-balanced": "high"})
    assert tier_projection(result.models)["high"] == "old-balanced"


def test_catalogs_without_priority_keep_the_capability_path(tmp_path: Path) -> None:
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"models": [
        {"slug": "big", "description": "Frontier model for complex coding."},
        {"slug": "small", "description": "Small, fast model."},
    ]}), encoding="utf-8")
    result = load_codex_cache(cache)
    assert result is not None
    assign_provider_relative_tiers(result.models)
    assert {m.model_id: m.tier_reason for m in result.models} == {
        "big": "capability_metadata", "small": "capability_metadata",
    }


def test_fetched_at_is_the_catalog_age(tmp_path: Path) -> None:
    fetched = time.time() - 3600
    result = load_codex_cache(_write_cache(tmp_path / "c.json", TODAY, fetched_at=fetched))
    assert result is not None
    assert result.discovered_at == pytest.approx(fetched, abs=1)


# ---------------------------------------------------------------------------
# Freshness and process agreement
# ---------------------------------------------------------------------------


def test_newer_cache_reprojects_before_ttl_expiry(tmp_path: Path, codex_home: Path) -> None:
    cache = codex_home / "models_cache.json"
    _write_cache(cache, [_entry("first", "Fast model.", priority=1)], fetched_at=time.time() - 60)
    db = Database(tmp_path / "catalog.db")
    catalog = ModelCatalog(db)
    provider = _codex_provider()
    registry = SimpleNamespace(available_providers=[provider])

    catalog.refresh_all(registry)
    assert provider.tier_models["low"] == "first"
    # Pretend that refresh happened 100s ago, so a cache fetched since is newer.
    with db.conn() as conn:
        conn.execute("UPDATE model_catalog SET last_seen = last_seen - 100")

    # Still inside the TTL, so before the fix this pass was a "skipped".
    _write_cache(cache, [_entry("second", "Fast model.", priority=1)], fetched_at=time.time() - 10)
    results = catalog.refresh_all(registry)
    assert results["refreshed"] == ["codex"]
    assert provider.tier_models == {"low": "second", "medium": "second", "high": "second"}

    # Unchanged cache -> plain skip, no rewrite.
    assert catalog.refresh_all(registry)["skipped"] == ["codex"]


def test_fresh_registry_and_catalog_agree(tmp_path: Path, codex_home: Path) -> None:
    _write_cache(codex_home / "models_cache.json", TODAY)

    server_side = _codex_provider()
    ModelCatalog(Database(tmp_path / "catalog.db")).refresh_all(
        SimpleNamespace(available_providers=[server_side])
    )

    fresh = _codex_provider()
    registry = ProviderRegistry()  # THRENODY_TEST_MODE: stub providers only
    registry.register_detected(
        fresh,
        ProviderReadiness(routeable=True, reason=DetectReason.READY, last_checked=time.time()),
    )

    assert fresh.tier_models == server_side.tier_models == {
        "low": "cur-fast", "medium": "cur-fast", "high": "cur-fast",
    }
    entry = next(row for row in fresh.model_catalog if row["model_id"] == "cur-fast")
    assert entry["tier_reason"].startswith("provider_prominence:")


def test_fresh_projection_keeps_bootstrap_without_a_cache(codex_home: Path) -> None:
    provider = _codex_provider()
    assert project_official_cache(provider) is False
    assert provider.tier_models == bootstrap_tier_map("codex")


def test_fresh_projection_keeps_bootstrap_when_everything_is_hidden(codex_home: Path) -> None:
    _write_cache(
        codex_home / "models_cache.json",
        [_entry("internal", "Internal model.", priority=1, visibility="hide")],
    )
    provider = _codex_provider()
    assert project_official_cache(provider) is False
    assert provider.tier_models == bootstrap_tier_map("codex")


def test_old_cache_still_beats_bootstrap(codex_home: Path) -> None:
    _write_cache(
        codex_home / "models_cache.json",
        [_entry("only", "Fast model.", priority=1)],
        fetched_at=time.time() - 30 * 86_400,
    )
    provider = _codex_provider()
    assert project_official_cache(provider) is True
    assert provider.tier_models["high"] == "only"
    # An explicit TTL is still honoured.
    assert project_official_cache(_codex_provider(), stale_ttl_seconds=86_400) is False


# ---------------------------------------------------------------------------
# Bootstrap catalogs are re-seeded, real catalogs are last-known-good
# ---------------------------------------------------------------------------


def test_bootstrap_catalog_is_reseeded_from_current_bootstrap(tmp_path: Path) -> None:
    catalog = ModelCatalog(Database(tmp_path / "catalog.db"))
    catalog.refresh(
        "github-copilot",
        [{"model_id": "retired-model", "provider_metadata": {"tier": "high"}}],
        source="bootstrap",
    )
    catalog.refresh("github-copilot", [], successful=False)
    ids = {row["model_id"] for row in catalog.get("github-copilot")}
    assert "retired-model" not in ids
    assert ids == set(bootstrap_tier_map("github-copilot").values())


def test_real_catalog_is_kept_as_last_known_good(tmp_path: Path) -> None:
    catalog = ModelCatalog(Database(tmp_path / "catalog.db"))
    catalog.refresh("github-copilot", [{"model_id": "live-one", "capabilities": ["fast"]}])
    catalog.refresh("github-copilot", [], successful=False)
    assert [row["model_id"] for row in catalog.get("github-copilot")] == ["live-one"]
