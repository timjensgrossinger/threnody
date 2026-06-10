import os
from types import SimpleNamespace

from shared.config import TGsConfig, SpilloverConfig
from shared.discovery import ProviderRegistry


def test_spillover_defaults(monkeypatch):
    """Defaults enable spillover and unspecified provider capacity is None."""
    cfg = TGsConfig.defaults()
    assert isinstance(cfg.spillover, SpilloverConfig)
    assert cfg.spillover.enabled is True
    assert cfg.spillover.get_provider_capacity("anything") is None

    # Ensure registry in test mode exposes adapter metadata with concurrency key
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")
    registry = ProviderRegistry()
    adapters = registry.list_adapters()
    assert adapters, "Expected at least one test adapter in test mode"
    adapter = adapters[0]
    # concurrency field should be present (None when unspecified)
    assert "concurrency" in adapter.metadata
    assert adapter.metadata["concurrency"] is None


def test_spillover_parsing_and_override(monkeypatch):
    """Config overrides with per_provider_concurrency are respected by registry."""
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")

    overrides = {
        "providers": {
            "spillover": {
                "enabled": True,
                "per_provider_concurrency": {
                    "test-provider": 5
                }
            }
        }
    }

    registry = ProviderRegistry(config_overrides=overrides)
    adapters = registry.list_adapters()
    # find test-provider adapter
    found = None
    for a in adapters:
        if a.name == "test-provider":
            found = a
            break
    assert found is not None, "test-provider adapter not found"
    assert found.metadata.get("concurrency") == 5


def test_selection_metadata_includes_concurrency(monkeypatch):
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")
    overrides = {
        "providers": {
            "spillover": {
                "enabled": True,
                "per_provider_concurrency": {
                    "test-provider": 7
                }
            }
        }
    }
    registry = ProviderRegistry(config_overrides=overrides)
    sel = registry.select_provider_for_tier("low")
    assert sel is not None
    # concurrency must be present on selection metadata
    assert "concurrency" in sel
    # For test-provider the configured value should be surfaced
    assert sel["concurrency"] == 7


def test_plan_spillover_allocation_overflows_when_primary_is_capped(monkeypatch):
    registry = ProviderRegistry(config_overrides={
        "providers": {
            "spillover": {
                "enabled": True,
                "per_provider_concurrency": {
                    "primary-provider": 1,
                    "secondary-provider": 2,
                },
            }
        }
    })
    primary = SimpleNamespace(name="primary-provider", display_name="Primary")
    secondary = SimpleNamespace(name="secondary-provider", display_name="Secondary")

    monkeypatch.setattr(
        registry,
        "_ordered_execution_candidates",
        lambda tier, **kwargs: ([primary, secondary], []),
    )
    monkeypatch.setattr(
        registry,
        "_selection_metadata_for_provider_with_effort",
        lambda provider, tier, effort: {
            "provider_id": provider.name,
            "tier": tier,
            "concurrency": registry._config_provider_capacity(provider.name),
        },
    )

    allocation = registry.plan_spillover_allocation("low", 3)

    assert allocation["remaining"] == 0
    assert allocation["primary"]["provider_id"] == "primary-provider"
    assert allocation["assignments"] == [
        {
            "provider_id": "primary-provider",
            "provider": "Primary",
            "slots": 1,
            "metadata": {
                "provider_id": "primary-provider",
                "tier": "low",
                "concurrency": 1,
            },
        },
        {
            "provider_id": "secondary-provider",
            "provider": "Secondary",
            "slots": 2,
            "metadata": {
                "provider_id": "secondary-provider",
                "tier": "low",
                "concurrency": 2,
            },
        },
    ]
