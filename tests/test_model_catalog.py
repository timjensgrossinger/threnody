#!/usr/bin/env python3
"""Wave 0 model-catalog scaffolding tests for Phase 6 discovery work."""
from __future__ import annotations

import copy
import threading
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import shared.model_catalog as model_catalog
from shared.discovery import (
    BUILTIN_PROVIDERS,
    CLIProvider,
    DetectReason,
    ProviderReadiness,
    ProviderRegistry,
)
from shared.model_catalog import ModelCatalog, rank_models_with_price_data
from tests.conftest import mock_provider_fixture, reset_registry


@pytest.fixture
def temp_catalog(temp_db_fixture):
    return ModelCatalog(db=temp_db_fixture)


def test_successful_refresh_does_not_merge_static_bootstrap(temp_catalog: ModelCatalog):
    temp_catalog.refresh(
        "catalog-test-provider",
        [{"model_id": "test-model-a"}],
    )

    stored = temp_catalog.get("catalog-test-provider")
    model_ids = {entry["model_id"] for entry in stored}

    assert model_ids == {"test-model-a"}
    assert all(entry["source"] == "live_provider_catalog" for entry in stored)


def test_unknown_cost_models_not_auto_routeable(temp_catalog: ModelCatalog):
    temp_catalog.refresh("test-provider", [{"model_id": "unknown-model"}])

    stored = temp_catalog.get("test-provider")

    assert len(stored) == 1
    assert stored[0]["tier"] == "unknown"
    assert stored[0]["auto_routeable"] is False
    assert temp_catalog.is_auto_routeable("unknown-model") is False


def test_stale_while_refresh_non_blocking(temp_catalog: ModelCatalog, temp_db_fixture):
    temp_catalog.refresh("catalog-test-provider", [{"model_id": "test-model-a"}])

    with temp_db_fixture.conn() as conn:
        conn.execute(
            "UPDATE model_catalog SET stale_until = ? WHERE provider = ?",
            (int(time.time()) - 1, "catalog-test-provider"),
        )

    provider = CLIProvider(
        name="catalog-test-provider",
        binary="catalog-test-bin",
        display_name="Catalog Test Provider",
        tier_models={"low": "test-model-a"},
        cost_rank={"low": 0},
        model_discovery_cmd=["catalog-test-bin", "models"],
        model_discovery_parser=lambda p, raw: {"catalog-test-provider": [raw.strip()]},
        readiness=ProviderReadiness(
            routeable=True,
            reason=DetectReason.READY,
            last_checked=time.time(),
        ),
    )
    registry = MagicMock(available_providers=[provider])

    def slow_run(*_args, **_kwargs):
        time.sleep(0.05)
        return MagicMock(returncode=0, stdout="test-model-a")

    with patch("shared.model_catalog.subprocess.run", side_effect=slow_run):
        worker = threading.Thread(
            target=temp_catalog.refresh_all,
            args=(registry,),
            daemon=True,
        )
        worker.start()
        stale_models = temp_catalog.get("catalog-test-provider")
        worker.join(timeout=1)

    assert any(entry["model_id"] == "test-model-a" for entry in stale_models)
    assert all("stale_until" in entry for entry in stale_models)


def test_refresh_cooldown_keeps_last_good_catalog(temp_catalog: ModelCatalog):
    temp_catalog.refresh("catalog-test-provider", [{"model_id": "test-model-a"}])
    with temp_catalog._db.conn() as conn:
        conn.execute(
            "UPDATE model_catalog SET stale_until = ? WHERE provider = ?",
            (int(time.time()) - 1, "catalog-test-provider"),
        )

    provider = CLIProvider(
        name="catalog-test-provider",
        binary="catalog-test-bin",
        display_name="Catalog Test Provider",
        tier_models={"low": "test-model-a"},
        cost_rank={"low": 0},
        model_discovery_cmd=["catalog-test-bin", "models"],
        model_discovery_parser=lambda p, raw: {"catalog-test-provider": [raw.strip()]},
        readiness=ProviderReadiness(
            routeable=True,
            reason=DetectReason.READY,
            last_checked=time.time(),
        ),
    )
    registry = MagicMock(available_providers=[provider])

    failing_run = MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr="auth failed"))
    with patch("shared.model_catalog.subprocess.run", failing_run):
        for _ in range(3):
            temp_catalog.refresh_all(registry)

    state = temp_catalog.provider_state("catalog-test-provider")
    stored = temp_catalog.get("catalog-test-provider")

    assert state["failed_refresh_count"] == 3
    assert float(state["cooldown_until"]) >= float(state["last_failure_ts"])
    assert any(entry["model_id"] == "test-model-a" for entry in stored)
    assert provider.readiness.reason is DetectReason.STALE_BUT_ROUTEABLE


def test_refresh_all_marks_pending_provider_ready_after_success(temp_catalog: ModelCatalog):
    provider = CLIProvider(
        name="catalog-test-provider",
        binary="catalog-test-bin",
        display_name="Catalog Test Provider",
        tier_models={"low": "test-model-a"},
        cost_rank={"low": 0},
        model_discovery_cmd=["catalog-test-bin", "models"],
        model_discovery_parser=lambda p, raw: {"catalog-test-provider": [raw.strip()]},
        readiness=ProviderReadiness(
            routeable=False,
            reason=DetectReason.CATALOG_PENDING,
            last_checked=time.time(),
        ),
    )
    registry = MagicMock(available_providers=[provider])

    with patch(
        "shared.model_catalog.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="test-model-a"),
    ):
        temp_catalog.refresh_all(registry)

    assert provider.readiness.routeable is True
    assert provider.readiness.reason is DetectReason.READY


@pytest.mark.parametrize(
    ("provider_name", "allowed_tiers"),
    [
        ("opencode", {"low"}),
        ("junie", {"medium"}),
    ],
)
def test_catalog_projection_preserves_provider_auto_route_tier_constraints(
    temp_catalog: ModelCatalog,
    provider_name: str,
    allowed_tiers: set[str],
):
    provider = next(
        p for p in BUILTIN_PROVIDERS
        if p.name == provider_name
    )
    provider = copy.deepcopy(provider)
    provider.cost_rank.update({"low": 0, "medium": 1, "high": 1})

    temp_catalog.refresh(
        provider_name,
        [
            {
                "model_id": f"{provider_name}-fast",
                "capabilities": ["fast"],
            },
            {
                "model_id": f"{provider_name}-standard",
                "capabilities": ["text"],
            },
            {
                "model_id": f"{provider_name}-reasoning",
                "capabilities": ["advanced-reasoning"],
            },
        ],
    )

    temp_catalog._project_provider_catalog(provider)

    assert set(provider.tier_models) == allowed_tiers
    assert set(provider.cost_rank) == allowed_tiers
    for model in provider.model_catalog:
        assert model["auto_routeable"] is (model["tier"] in allowed_tiers)


def test_operator_pin_can_expand_constrained_provider_tier(temp_db_fixture):
    catalog = ModelCatalog(
        db=temp_db_fixture,
        user_overrides={"opencode/explicit-medium": "medium"},
    )
    provider = next(
        p for p in BUILTIN_PROVIDERS
        if p.name == "opencode"
    )
    provider = copy.deepcopy(provider)

    catalog.refresh(
        "opencode",
        [
            {"model_id": "opencode/free-fast", "capabilities": ["fast"]},
            {"model_id": "opencode/explicit-medium", "capabilities": ["text"]},
        ],
    )
    catalog._project_provider_catalog(provider)

    assert provider.tier_models["medium"] == "opencode/explicit-medium"
    assert provider.cost_rank["medium"] == 1
    pinned = next(
        model for model in provider.model_catalog
        if model["model_id"] == "opencode/explicit-medium"
    )
    assert pinned["auto_routeable"] is True
    assert pinned["tier_reason"] == "operator_pin"


def test_unrestricted_provider_projects_all_discovered_tiers(temp_catalog: ModelCatalog):
    provider = CLIProvider(
        name="unrestricted",
        binary="unrestricted",
        display_name="Unrestricted",
        tier_models={},
        cost_rank={},
    )
    temp_catalog.refresh(
        "unrestricted",
        [
            {"model_id": "fast", "capabilities": ["fast"]},
            {"model_id": "standard", "capabilities": ["text"]},
            {"model_id": "reasoning", "capabilities": ["advanced-reasoning"]},
        ],
    )

    temp_catalog._project_provider_catalog(provider)

    assert set(provider.tier_models) == {"low", "medium", "high"}
    assert set(provider.cost_rank) == {"low", "medium", "high"}


def test_compact_diagnostics_report_effective_routeable_catalog_tiers(
    temp_catalog: ModelCatalog,
):
    provider = copy.deepcopy(
        next(p for p in BUILTIN_PROVIDERS if p.name == "opencode")
    )
    provider.readiness = ProviderReadiness(
        routeable=True,
        reason=DetectReason.READY,
        last_checked=time.time(),
    )
    temp_catalog.refresh(
        "opencode",
        [
            {"model_id": "opencode/free-fast", "capabilities": ["fast"]},
            {"model_id": "opencode/standard", "capabilities": ["text"]},
            {"model_id": "opencode/reasoning", "capabilities": ["advanced-reasoning"]},
        ],
    )
    temp_catalog._project_provider_catalog(provider)
    registry = ProviderRegistry(config_overrides={})
    registry.available_providers = [provider]

    diagnostics = registry.to_compact_dict()["providers"][0]

    assert diagnostics["models_summary"] == {"low": 1, "medium": 0, "high": 0}
    routeability_by_tier = {
        model["tier"]: model["auto_routeable"]
        for model in diagnostics["models"]
    }
    assert routeability_by_tier == {
        "low": True,
        "medium": False,
        "high": False,
    }


def test_refresh_all_seeds_catalog_for_endpoint_provider_without_discovery_cmd(temp_catalog: ModelCatalog):
    provider = CLIProvider(
        name="studio",
        binary="http",
        display_name="Studio",
        tier_models={"low": "gpt-oss-20b"},
        cost_rank={"low": 0},
        transport="http",
        endpoint_kind="openai-compatible",
        endpoint_scope="local",
        endpoint_base_url="http://127.0.0.1:1234/v1",
        readiness=ProviderReadiness(
            routeable=True,
            reason=DetectReason.READY,
            last_checked=time.time(),
        ),
    )
    registry = MagicMock(available_providers=[provider])

    temp_catalog.refresh_all(registry)

    stored = temp_catalog.get("studio")
    assert len(stored) == 1
    assert stored[0]["model_id"] == "gpt-oss-20b"
    assert stored[0]["tier"] == "low"


def test_refresh_skips_malformed_discovered_entries(temp_catalog: ModelCatalog):
    temp_catalog.refresh(
        "catalog-test-provider",
        [
            {"model_id": "test-model-a"},
            {"broken": "entry"},
        ],
    )

    stored = temp_catalog.get("catalog-test-provider")

    assert any(entry["model_id"] == "test-model-a" for entry in stored)


def test_rank_models_with_price_data_uses_bundled_snapshot():
    ranked = rank_models_with_price_data([{"model_id": "gpt-5-mini"}])

    assert ranked[0]["cost"] is not None
    assert ranked[0]["tier"] == "low"
    assert ranked[0]["auto_routeable"] is True


def test_rank_models_with_price_data_rejects_missing_model_id():
    with pytest.raises(ValueError, match="model_id"):
        rank_models_with_price_data([{"provider": "catalog-test-provider"}])


def test_load_price_data_caches_empty_snapshot(tmp_path, monkeypatch):
    snapshot = tmp_path / "model_prices.json"
    snapshot.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(model_catalog, "_PRICE_DATA_PATH", snapshot)
    monkeypatch.setattr(model_catalog, "_PRICE_DATA_CACHE", {})
    monkeypatch.setattr(model_catalog, "_PRICE_DATA_MTIME", 0.0)
    monkeypatch.setattr(model_catalog, "_PRICE_DATA_LOADED", False)

    assert model_catalog._load_price_data() == {}

    with patch("shared.model_catalog.json.load", side_effect=AssertionError("should use cache")):
        assert model_catalog._load_price_data() == {}


@pytest.mark.parametrize(
    ("provider_name", "tier", "model_id"),
    [
        (provider.name, tier, model_id)
        for provider in BUILTIN_PROVIDERS
        for tier, model_id in provider.tier_models.items()
    ],
)
def test_static_models_preserve_provider_tier_matrix(
    provider_name: str,
    tier: str,
    model_id: str,
):
    ranked = rank_models_with_price_data(
        [{"model_id": model_id, "provider": provider_name, "source": "static", "tier": tier}]
    )

    assert ranked[0]["tier"] == tier
    assert ranked[0]["auto_routeable"] is True


# === Phase 9 User Override Tests ===

def test_tier_from_cost_user_override_wins():
    """User override in user_overrides should take precedence over cost-based ranking."""
    # gpt-4-turbo costs ~0.00001 per token (very low), but we override to "low"
    tier = model_catalog._tier_from_cost("gpt-4-turbo", 0.00001, user_overrides={"gpt-4-turbo": "low"})
    assert tier == "low"


def test_tier_from_cost_bundled_override_still_works():
    """When user_overrides is empty, bundled _TIER_OVERRIDES should still apply."""
    # "o1" is in _TIER_OVERRIDES as "high"
    tier = model_catalog._tier_from_cost("o1", 0.00001, user_overrides={})
    assert tier == "high"


def test_tier_from_cost_user_beats_bundled():
    """User override should beat bundled _TIER_OVERRIDES."""
    # "o1" is normally "high" in _TIER_OVERRIDES, but user overrides to "medium"
    tier = model_catalog._tier_from_cost("o1", 0.00001, user_overrides={"o1": "medium"})
    assert tier == "medium"


def test_tier_from_cost_cost_ranking_without_overrides():
    """Without overrides, cost-based ranking should work as before."""
    # 0.0000001 per token = 0.0001 per million (very cheap, should be "low")
    tier = model_catalog._tier_from_cost("cheap-model", 0.0000001, user_overrides={})
    assert tier == "low"


def test_tier_from_cost_none_user_overrides_handled():
    """None user_overrides should be handled gracefully."""
    tier = model_catalog._tier_from_cost("cheap-model", 0.0000001, user_overrides=None)
    assert tier == "low"


def test_rank_models_with_user_override_applied():
    """rank_models_with_price_data should apply user_overrides parameter."""
    # Create a model entry with price data
    ranked = model_catalog.rank_models_with_price_data(
        [{"model_id": "gpt-5-mini", "provider": "github-copilot"}],
        user_overrides={"gpt-5-mini": "high"}
    )
    # Should be overridden to "high" regardless of price
    assert ranked[0]["tier"] == "high"
    assert ranked[0]["auto_routeable"] is True


def test_rank_models_user_override_without_price_data():
    """User override should apply even when model has no price data."""
    ranked = model_catalog.rank_models_with_price_data(
        [{"model_id": "unknown-expensive-model", "provider": "test"}],
        user_overrides={"unknown-expensive-model": "medium"}
    )
    # Should be tier "medium" from user override despite missing price data
    assert ranked[0]["tier"] == "medium"
    assert ranked[0]["auto_routeable"] is True


@pytest.mark.parametrize(
    "model_id,cost,user_override,expected_tier",
    [
        # User override wins
        ("model-a", 0.00001, {"model-a": "low"}, "low"),
        # Bundled override wins (o1 is high)
        ("o1", 0.00001, {}, "high"),
        # User beats bundled (o1 normally high, but user says medium)
        ("o1", 0.00001, {"o1": "medium"}, "medium"),
        # Cost-based (no overrides) - low tier (< 0.5 per million)
        ("cheap", 0.0000001, {}, "low"),
        # Cost-based medium (between 0.5 and 5.0 per million)
        ("medium-cost", 0.000002, {}, "medium"),
        # Cost-based high (> 5.0 per million)
        ("expensive", 0.00001, {}, "high"),
    ],
)
def test_tier_override_precedence_matrix(model_id, cost, user_override, expected_tier):
    """Comprehensive precedence test: user > bundled > cost > fallback."""
    tier = model_catalog._tier_from_cost(model_id, cost, user_overrides=user_override)
    assert tier == expected_tier


def test_model_catalog_applies_user_overrides_on_refresh(temp_catalog: ModelCatalog):
    """ModelCatalog should store user_overrides and apply them during refresh."""
    # Create a new catalog with user overrides
    user_overrides = {"gpt-5-mini": "high"}
    catalog_with_overrides = ModelCatalog(
        db=temp_catalog._db,
        user_overrides=user_overrides
    )
    
    # Refresh with a model
    catalog_with_overrides.refresh(
        "github-copilot",
        [{"model_id": "gpt-5-mini"}]
    )
    
    # Get the stored entry
    stored = catalog_with_overrides.get("github-copilot")
    gpt_5_mini = next((m for m in stored if m["model_id"] == "gpt-5-mini"), None)
    
    # Should be "high" from override
    assert gpt_5_mini is not None
    assert gpt_5_mini["tier"] == "high"


def test_config_yaml_invalid_tier_rejected(tmp_path, monkeypatch):
    """TGsConfig should reject invalid tier values during YAML loading."""
    from shared.config import TGsConfig
    
    # Create a temporary config.yaml with an invalid tier
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
models:
  tier_pins:
    gpt-4-turbo: invalid_tier
    o1: high
    claude: low
""")
    
    cfg = TGsConfig.from_yaml(config_file)
    
    # "invalid_tier" should be rejected, but "high" and "low" should be accepted
    assert "gpt-4-turbo" not in cfg.model_tier_pins  # Rejected
    assert cfg.model_tier_pins.get("o1") == "high"  # Accepted
    assert cfg.model_tier_pins.get("claude") == "low"  # Accepted


def test_config_yaml_provider_cost_overrides_load(tmp_path):
    """TGsConfig should load provider cost overrides from YAML."""
    from shared.config import TGsConfig

    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
providers:
  cost_overrides:
    cursor:
      low:
        cost_rank: 0
        billing_tier: free
        provider_cost_hint: Cursor Pro subscription
      medium:
        billing_tier: metered
        billing_note: Usage-based medium tier
""")

    cfg = TGsConfig.from_yaml(config_file)

    assert cfg.provider_cost_overrides["cursor"]["low"].cost_rank == 0
    assert cfg.provider_cost_overrides["cursor"]["low"].billing_tier == "free"
    assert cfg.provider_cost_overrides["cursor"]["low"].provider_cost_hint == "Cursor Pro subscription"
    assert cfg.provider_cost_overrides["cursor"]["medium"].billing_tier == "metered"
    assert cfg.provider_cost_overrides["cursor"]["medium"].provider_cost_hint == "Usage-based medium tier"


def test_config_yaml_invalid_provider_cost_override_rejected(tmp_path):
    """Invalid provider cost override entries should be skipped."""
    from shared.config import TGsConfig

    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
providers:
  cost_overrides:
    cursor:
      invalid:
        cost_rank: 0
      low:
        cost_rank: true
        billing_tier: unknown
""")

    cfg = TGsConfig.from_yaml(config_file)

    assert cfg.provider_cost_overrides == {}


def test_config_yaml_non_mapping_root_falls_back_to_defaults(tmp_path):
    """Malformed top-level YAML should not crash config loading."""
    from shared.config import TGsConfig

    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
- not
- a
- mapping
""")

    cfg = TGsConfig.from_yaml(config_file)

    assert cfg.provider_cost_overrides == {}
