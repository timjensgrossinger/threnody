from __future__ import annotations

import json
import time

from shared.db import Database
from shared.discovery import CLIProvider, DetectReason, ProviderReadiness, ProviderRegistry
from shared.model_catalog import ModelCatalog
from shared.model_registry import (
    assign_provider_relative_tiers,
    bootstrap_tier_map,
    load_codex_cache,
    normalize_models,
    resolve_alias,
    tier_projection,
)
from shared.provider_model_adapters import CallbackModelDiscoveryAdapter


def test_codex_cache_drives_low_tier_without_one_off_model_pin(tmp_path):
    cache = tmp_path / "models_cache.json"
    cache.write_text(json.dumps({
        "models": [
            {
                "slug": "gpt-5.5",
                "display_name": "GPT-5.5",
                "description": "Frontier model for complex coding.",
            },
            {
                "slug": "gpt-5.4",
                "display_name": "GPT-5.4",
                "description": "Strong model for everyday coding.",
            },
            {
                "slug": "gpt-5.4-mini",
                "display_name": "GPT-5.4-Mini",
                "description": "Small, fast, and cost-efficient model for simpler coding tasks.",
            },
        ],
    }), encoding="utf-8")

    result = load_codex_cache(cache)
    assert result is not None
    assign_provider_relative_tiers(result.models)

    assert tier_projection(result.models)["low"] == "gpt-5.4-mini"
    assert next(model for model in result.models if model.model_id == "gpt-5.4-mini").tier_reason == "capability_metadata"


def test_new_and_removed_live_models_replace_previous_catalog(tmp_path):
    catalog = ModelCatalog(Database(tmp_path / "catalog.db"))
    catalog.refresh("catalog-test-provider", [
        {"model_id": "old-model", "capabilities": ["fast"]},
    ])
    catalog.refresh("catalog-test-provider", [
        {"model_id": "new-model", "capabilities": ["fast"]},
    ])

    assert [row["model_id"] for row in catalog.get("catalog-test-provider")] == ["new-model"]


def test_alias_resolves_renamed_model():
    models = normalize_models("claude-code", [
        {"model_id": "claude-sonnet-4.6", "aliases": ["sonnet", "claude-sonnet-latest"]},
    ], source="agent_sdk_init")

    assert resolve_alias(models, "sonnet").model_id == "claude-sonnet-4.6"
    assert resolve_alias(models, "claude-sonnet-latest").model_id == "claude-sonnet-4.6"


def test_claude_bootstrap_uses_cli_stable_aliases():
    assert bootstrap_tier_map("claude-code") == {
        "low": "haiku",
        "medium": "sonnet",
        "high": "opus",
    }


def test_operator_pin_assigns_model_with_missing_pricing():
    models = normalize_models("custom", [{"model_id": "unknown"}], source="live_provider_catalog")
    assign_provider_relative_tiers(models, pins={"unknown": "high"})

    assert models[0].tier == "high"
    assert models[0].tier_reason == "operator_pin"
    assert models[0].routeable is True


def test_missing_metadata_remains_unclassified():
    models = normalize_models("custom", [{"model_id": "unknown"}], source="live_provider_catalog")
    assign_provider_relative_tiers(models)

    assert models[0].tier is None
    assert models[0].routeable is False


def test_provider_relative_pricing_not_global_thresholds():
    models = normalize_models("custom", [
        {"model_id": "a", "input_price_per_million": 20.0},
        {"model_id": "b", "input_price_per_million": 30.0},
        {"model_id": "c", "input_price_per_million": 40.0},
    ], source="live_provider_catalog")
    assign_provider_relative_tiers(models)

    assert {model.model_id: model.tier for model in models} == {
        "a": "low",
        "b": "medium",
        "c": "high",
    }


def test_unavailable_selected_model_uses_same_tier_fallback():
    provider = CLIProvider(
        name="test",
        binary="test",
        display_name="Test",
        tier_models={"low": "removed"},
        cost_rank={"low": 0},
        readiness=ProviderReadiness(True, DetectReason.READY, time.time()),
        model_catalog=[
            {
                "model_id": "removed",
                "tier": "low",
                "available": False,
                "deprecated": False,
                "auto_routeable": False,
            },
            {
                "model_id": "replacement",
                "tier": "low",
                "available": True,
                "deprecated": False,
                "auto_routeable": True,
            },
        ],
        execute_hook=lambda _provider, _prompt, model, **_kwargs: model,
    )
    registry = ProviderRegistry()
    registry.available_providers = [provider]

    result = registry.execute_cheapest("hello", tier="low")

    assert result["result"] == "replacement"
    assert result["model_fallbacks"][0]["from_model"] == "removed"
    assert result["fallback_reason"] == "removed is unavailable"


def test_stale_catalog_is_last_known_good_on_outage(tmp_path):
    catalog = ModelCatalog(Database(tmp_path / "catalog.db"), stale_ttl_seconds=1)
    catalog.refresh("custom", [
        {"model_id": "known", "capabilities": ["fast"]},
    ])
    with catalog._db.conn() as conn:
        conn.execute(
            "UPDATE model_catalog SET stale_until = ? WHERE provider = ?",
            (int(time.time()) - 1, "custom"),
        )
    catalog.refresh("custom", [], successful=False)

    assert [row["model_id"] for row in catalog.get("custom")] == ["known"]


def test_provider_catalog_callback_normalizes_subscription_multiplier():
    adapter = CallbackModelDiscoveryAdapter(
        "github-copilot",
        catalog=lambda: {
            "models": [
                {
                    "id": "fast",
                    "display_name": "Fast",
                    "premium_request_multiplier": 0.0,
                },
                {
                    "id": "deep",
                    "display_name": "Deep",
                    "premium_request_multiplier": 3.0,
                },
            ]
        },
        source="copilot_provider_catalog",
    )

    result = adapter.discover_live()
    assert result is not None
    assign_provider_relative_tiers(result.models)
    assert {model.model_id: model.tier for model in result.models} == {
        "fast": "low",
        "deep": "high",
    }
