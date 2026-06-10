# Model Discovery and Tiering

Threnody separates provider model discovery from routing tier assignment.

## Catalog Precedence

Catalog inputs are considered in this order:

1. Operator model tier pins and provider preferences
2. A successful live provider catalog
3. A fresh official CLI-owned cache
4. The last-known-good persisted catalog
5. The static bootstrap registry

A successful live catalog is authoritative. Bootstrap models are not merged
into it, so removed provider models stop being routed.

The central bootstrap registry is `shared/model_registry.py`. Provider modules
may expose compatibility projections, but must not define independent static
model-to-tier maps.

## Normalized Records

Each discovered model records its provider model ID and display name,
availability and deprecation state, source and timestamp, aliases,
capabilities, context window, reasoning levels, and either pricing or a
subscription request multiplier.

Unknown models without a pin, provider-relative cost signal, request
multiplier, or capability metadata remain unclassified and are not routed.

## Provider Sources

- Codex: app-server catalogs when supplied by the host, otherwise the fresh
  official `~/.codex/models_cache.json` cache.
- Claude Code: official aliases and Agent SDK init `availableModels` data.
- GitHub Copilot: CLI/provider catalog and premium-request multipliers.
- Gemini: authenticated CLI/provider catalog.
- OpenCode: `opencode models`.
- Aider: `aider --list-models --no-check-update`.
- Ollama and OpenAI-compatible endpoints: provider API discovery and metadata.

No private telemetry is scraped and no subscription quota is inferred.

## Tier Assignment

Tiering is provider-relative: explicit pins, subscription multipliers or
provider metadata, relative provider pricing, then capability metadata.
Global API price thresholds are not a routing authority.

Before execution, Threnody validates the selected model. An unavailable or
deprecated model is replaced only by an ordered same-tier model, and the
fallback reason is returned in execution and inspection telemetry.

## Migration

Existing `models.tier_pins`, `providers.preferred_routing`, caller-specific
preferences, cost overrides, and provider ordering remain valid.

The `model_catalog` table gains a nullable `metadata_json` column through the
normal additive migration. Existing rows remain readable. After a successful
provider refresh, obsolete bootstrap rows are removed.

Consumers should prefer `model_id`, `discovery_source`, `discovered_at`,
`catalog_stale_until`, and `fallback_reason` when present. Existing `model`,
`tier`, `provider`, and billing fields remain available.
