"""Normalized provider model discovery and provider-relative tier assignment."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

TIERS = ("low", "medium", "high")


@dataclass(slots=True)
class DiscoveredModel:
    model_id: str
    display_name: str
    available: bool = True
    deprecated: bool = False
    discovery_source: str = "bootstrap"
    discovered_at: float = field(default_factory=time.time)
    aliases: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    context_window: int | None = None
    reasoning_levels: tuple[str, ...] = ()
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    request_multiplier: float | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    tier: str | None = None
    tier_reason: str | None = None
    routeable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DiscoveredModel":
        values = dict(raw)
        for key in ("aliases", "capabilities", "reasoning_levels"):
            value = values.get(key, ())
            values[key] = tuple(value) if isinstance(value, (list, tuple)) else ()
        values["provider_metadata"] = (
            dict(values.get("provider_metadata") or {})
            if isinstance(values.get("provider_metadata"), dict)
            else {}
        )
        return cls(**values)


@dataclass(slots=True)
class DiscoveryResult:
    provider_id: str
    models: list[DiscoveredModel]
    source: str
    discovered_at: float = field(default_factory=time.time)
    successful: bool = True
    error: str | None = None


class ModelDiscoveryAdapter(Protocol):
    provider_id: str

    def discover_live(self) -> DiscoveryResult | None:
        """Return a successful live provider catalog, or None when unsupported."""

    def discover_official_cache(self) -> DiscoveryResult | None:
        """Return a CLI-owned cache catalog, or None when absent/unusable."""


def _model(
    model_id: str,
    tier: str,
    *,
    aliases: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = ("text", "tools"),
    request_multiplier: float | None = None,
    eligible_tiers: tuple[str, ...] = (),
) -> DiscoveredModel:
    return DiscoveredModel(
        model_id=model_id,
        display_name=model_id,
        aliases=aliases,
        capabilities=capabilities,
        request_multiplier=request_multiplier,
        provider_metadata={"eligible_tiers": list(eligible_tiers)} if eligible_tiers else {},
        tier=tier,
        tier_reason="bootstrap",
        routeable=True,
    )


# This is the only static model-to-tier bootstrap registry. Provider modules may
# expose compatibility projections, but must not maintain independent mappings.
BOOTSTRAP_REGISTRY: dict[str, tuple[DiscoveredModel, ...]] = {
    "github-copilot": (
        _model("gpt-5-mini", "low", request_multiplier=0.0),
        _model("gpt-5.4", "medium", request_multiplier=1.0),
        _model("claude-opus-4.6", "high", request_multiplier=3.0),
    ),
    "claude-code": (
        _model("haiku", "low", aliases=("claude-haiku-4.5",)),
        _model("sonnet", "medium", aliases=("claude-sonnet-4.6",)),
        _model("opus", "high", aliases=("claude-opus-4.6",)),
    ),
    "codex": (
        _model(
            "gpt-5.5",
            "medium",
            capabilities=("text", "tools", "reasoning"),
            eligible_tiers=("low", "medium", "high"),
        ),
    ),
    "opencode": (
        _model("opencode/nemotron-3-super-free", "low", request_multiplier=0.0),
    ),
    "aider": (
        _model("gpt-4o-mini", "low"),
        _model("gpt-4o", "medium"),
        _model("o3", "high", capabilities=("text", "tools", "reasoning")),
    ),
    "junie": (_model("configured-model", "medium"),),
    "cursor": (
        _model("claude-haiku", "low"),
        _model("claude-sonnet", "medium"),
        _model("claude-opus", "high"),
    ),
    "amazon-q": (
        _model("claude-haiku", "low"),
        _model("claude-3.7-sonnet", "medium"),
        _model("claude-sonnet-4", "high"),
    ),
    "mistral-vibe": (
        _model("devstral-small", "low"),
        _model("mistral-medium-3.5", "medium", eligible_tiers=("high",)),
    ),
    "blackbox-ai": (
        _model("blackboxai", "low"),
        _model("claude-sonnet-4.6", "medium"),
        _model("claude-opus-4.6", "high"),
    ),
}


def bootstrap_models(provider_id: str) -> list[DiscoveredModel]:
    return [DiscoveredModel.from_dict(model.to_dict()) for model in BOOTSTRAP_REGISTRY.get(provider_id, ())]


def bootstrap_tier_map(provider_id: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for model in bootstrap_models(provider_id):
        if model.tier and model.tier not in result:
            result[model.tier] = model.model_id
        for tier in model.provider_metadata.get("eligible_tiers", []):
            if tier in TIERS and tier not in result:
                result[tier] = model.model_id
    return result


def normalize_models(
    provider_id: str,
    raw_models: list[dict[str, Any] | str],
    *,
    source: str,
    discovered_at: float | None = None,
) -> list[DiscoveredModel]:
    timestamp = discovered_at or time.time()
    normalized: list[DiscoveredModel] = []
    seen: set[str] = set()
    for raw in raw_models:
        values = {"model_id": raw} if isinstance(raw, str) else dict(raw)
        model_id = (
            values.get("model_id")
            or values.get("id")
            or values.get("model")
            or values.get("slug")
        )
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()
        canonical = model_id.casefold()
        if canonical in seen:
            continue
        seen.add(canonical)
        pricing = values.get("pricing") if isinstance(values.get("pricing"), dict) else {}
        raw_capabilities = [
            str(v) for v in values.get("capabilities", ()) if isinstance(v, str)
        ]
        description = str(values.get("description") or "").casefold()
        if not raw_capabilities:
            if any(marker in description for marker in ("small", "fast", "cost-efficient", "lightweight")):
                raw_capabilities.append("fast")
            elif any(marker in description for marker in ("frontier", "most capable", "complex")):
                raw_capabilities.append("flagship")
            elif description:
                raw_capabilities.append("text")
        raw_reasoning = values.get("reasoning_levels") or values.get("supported_reasoning_levels") or ()
        reasoning_levels = []
        for item in raw_reasoning:
            if isinstance(item, str):
                reasoning_levels.append(item)
            elif isinstance(item, dict) and isinstance(item.get("effort"), str):
                reasoning_levels.append(item["effort"])
        normalized.append(
            DiscoveredModel(
                model_id=model_id,
                display_name=str(values.get("display_name") or values.get("name") or model_id),
                available=values.get("available", True) is not False,
                deprecated=bool(values.get("deprecated", False)),
                discovery_source=source,
                discovered_at=float(values.get("discovered_at") or timestamp),
                aliases=tuple(str(v) for v in values.get("aliases", ()) if isinstance(v, str)),
                capabilities=tuple(raw_capabilities),
                context_window=_positive_int(values.get("context_window") or values.get("context_length")),
                reasoning_levels=tuple(reasoning_levels),
                input_price_per_million=_number(
                    values.get("input_price_per_million", pricing.get("input"))
                ),
                output_price_per_million=_number(
                    values.get("output_price_per_million", pricing.get("output"))
                ),
                request_multiplier=_number(
                    values.get("request_multiplier", values.get("premium_request_multiplier"))
                ),
                provider_metadata=dict(values.get("provider_metadata") or {}),
            )
        )
    return normalized


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _positive_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and int(value) > 0 else None


def resolve_alias(models: list[DiscoveredModel], requested: str) -> DiscoveredModel | None:
    needle = requested.casefold()
    for model in models:
        if model.model_id.casefold() == needle:
            return model
        if any(alias.casefold() == needle for alias in model.aliases):
            return model
    return None


def assign_provider_relative_tiers(
    models: list[DiscoveredModel],
    *,
    pins: dict[str, str] | None = None,
) -> list[DiscoveredModel]:
    """Assign tiers within one provider; missing evidence remains unclassified."""
    pins = pins or {}
    active = [model for model in models if model.available and not model.deprecated]

    for model in models:
        pinned = pins.get(model.model_id)
        if pinned in TIERS:
            model.tier = pinned
            model.tier_reason = "operator_pin"
            model.routeable = model.available and not model.deprecated

    unassigned = [model for model in active if model.tier is None]
    _assign_ranked(unassigned, lambda m: m.request_multiplier, "request_multiplier")
    unassigned = [model for model in active if model.tier is None]
    _assign_ranked(
        unassigned,
        lambda m: (
            (m.input_price_per_million or 0.0) + (m.output_price_per_million or 0.0)
            if m.input_price_per_million is not None or m.output_price_per_million is not None
            else None
        ),
        "provider_relative_pricing",
    )
    unassigned = [model for model in active if model.tier is None]
    _assign_capabilities(unassigned)

    for model in models:
        model.routeable = bool(
            model.available and not model.deprecated and model.tier in TIERS
        )
    return models


def _assign_ranked(models: list[DiscoveredModel], value_getter: Any, reason: str) -> None:
    valued = [(model, value_getter(model)) for model in models]
    valued = [(model, value) for model, value in valued if value is not None]
    if not valued:
        return
    valued.sort(key=lambda item: (float(item[1]), item[0].model_id))
    count = len(valued)
    for index, (model, _value) in enumerate(valued):
        if count == 1:
            tier = "low"
        elif index < max(1, count // 3):
            tier = "low"
        elif index >= max(1, (2 * count) // 3):
            tier = "high"
        else:
            tier = "medium"
        model.tier = tier
        model.tier_reason = reason


def _assign_capabilities(models: list[DiscoveredModel]) -> None:
    for model in models:
        metadata_tier = model.provider_metadata.get("tier")
        if metadata_tier in TIERS:
            model.tier = str(metadata_tier)
            model.tier_reason = "provider_metadata"
            continue
        capability_set = {value.casefold() for value in model.capabilities}
        if "flagship" in capability_set or "advanced-reasoning" in capability_set:
            model.tier = "high"
            model.tier_reason = "capability_metadata"
        elif "mini" in capability_set or "fast" in capability_set or "small" in capability_set:
            model.tier = "low"
            model.tier_reason = "capability_metadata"
        elif capability_set or model.context_window or model.reasoning_levels:
            model.tier = "medium"
            model.tier_reason = "capability_metadata"


def tier_projection(models: list[DiscoveredModel]) -> dict[str, str]:
    result: dict[str, str] = {}
    for tier in TIERS:
        candidates = sorted(
            (
                model for model in models
                if (
                    model.tier == tier
                    or tier in model.provider_metadata.get("eligible_tiers", [])
                )
                and model.routeable
            ),
            key=lambda model: (
                model.request_multiplier if model.request_multiplier is not None else float("inf"),
                model.input_price_per_million if model.input_price_per_million is not None else float("inf"),
                model.model_id,
            ),
        )
        if candidates:
            result[tier] = candidates[0].model_id
    return result


def load_codex_cache(path: Path | None = None) -> DiscoveryResult | None:
    cache_path = path or Path.home() / ".codex" / "models_cache.json"
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entries = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return None
    timestamp = cache_path.stat().st_mtime
    return DiscoveryResult(
        provider_id="codex",
        models=normalize_models("codex", entries, source="official_cli_cache", discovered_at=timestamp),
        source="official_cli_cache",
        discovered_at=timestamp,
    )


def normalize_claude_agent_sdk_models(payload: dict[str, Any]) -> DiscoveryResult | None:
    """Normalize Agent SDK init data containing ``availableModels``."""
    entries = payload.get("availableModels") or payload.get("available_models")
    if not isinstance(entries, list):
        return None
    return DiscoveryResult(
        provider_id="claude-code",
        models=normalize_models(
            "claude-code",
            entries,
            source="agent_sdk_init",
        ),
        source="agent_sdk_init",
    )


def normalize_copilot_catalog(payload: dict[str, Any] | list[Any]) -> DiscoveryResult | None:
    entries = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return None
    return DiscoveryResult(
        provider_id="github-copilot",
        models=normalize_models(
            "github-copilot",
            entries,
            source="live_provider_catalog",
        ),
        source="live_provider_catalog",
    )
