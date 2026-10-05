"""Normalized provider model discovery and provider-relative tier assignment."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

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
    verified: bool | None = None,
) -> DiscoveredModel:
    metadata: dict[str, Any] = {"eligible_tiers": list(eligible_tiers)} if eligible_tiers else {}
    if verified is not None:
        # False = the id could not be checked against the CLI's own model list
        # (CLI absent, or the list it prints does not include it); see the
        # comment on that provider's entry.
        metadata["verified"] = verified
    return DiscoveredModel(
        model_id=model_id,
        display_name=model_id,
        aliases=aliases,
        capabilities=capabilities,
        request_multiplier=request_multiplier,
        provider_metadata=metadata,
        tier=tier,
        tier_reason="bootstrap",
        routeable=True,
    )


# This is the only static model-to-tier bootstrap registry. Provider modules may
# expose compatibility projections, but must not maintain independent mappings.
BOOTSTRAP_REGISTRY: dict[str, tuple[DiscoveredModel, ...]] = {
    # gpt-5-mini / gpt-5.4 appear in `copilot help config` (2026-10-02).
    # claude-opus-5 does not — that list tops out at claude-opus-4.8 — but the
    # GitHub Copilot API lists it (via `opencode models`, github-copilot/*), so it
    # is kept and marked unverified rather than downgraded.
    "github-copilot": (
        _model("gpt-5-mini", "low", request_multiplier=0.0),
        _model("gpt-5.4", "medium", request_multiplier=1.0),
        _model(
            "claude-opus-5",
            "high",
            aliases=("claude-opus-4.6",),
            request_multiplier=3.0,
            verified=False,
        ),
    ),
    "claude-code": (
        _model("haiku", "low", aliases=("claude-haiku-5", "claude-haiku-4.5")),
        _model("sonnet", "medium", aliases=("claude-sonnet-5", "claude-sonnet-4.6")),
        _model("opus", "high", aliases=("claude-opus-5", "claude-opus-4.6")),
    ),
    "codex": (
        _model(
            "gpt-5.6-terra",
            "medium",
            aliases=("gpt-5.5",),
            capabilities=("text", "tools", "reasoning"),
            eligible_tiers=("low", "medium"),
        ),
        _model(
            "gpt-5.6-sol",
            "high",
            capabilities=("text", "tools", "reasoning"),
            eligible_tiers=("high",),
        ),
    ),
    # opencode/nemotron-3-super-free is no longer in `opencode models`
    # (2026-10-02); nemotron-3-ultra-free is its listed successor. Only the
    # bootstrap — a machine with opencode installed routes on the live list.
    "opencode": (
        _model(
            "opencode/nemotron-3-ultra-free",
            "low",
            aliases=("opencode/nemotron-3-super-free",),
            request_multiplier=0.0,
        ),
    ),
    "aider": (
        _model("gpt-4o-mini", "low"),
        _model("gpt-4o", "medium"),
        _model("o3", "high", capabilities=("text", "tools", "reasoning")),
    ),
    "junie": (_model("configured-model", "medium"),),
    # Unverified: cursor-agent was not installed where these were last checked,
    # so `cursor-agent models` could not confirm them. Live discovery replaces
    # them wherever the CLI exists.
    "cursor": (
        _model("composer-2.5-fast", "low", verified=False),
        _model("composer-2.5", "medium", verified=False),
        _model(
            "claude-opus-5-thinking-high",
            "high",
            aliases=("claude-opus-4-8-thinking-high",),
            verified=False,
        ),
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
        _model("claude-sonnet-5", "medium", aliases=("claude-sonnet-4.6",)),
        _model("claude-opus-5", "high", aliases=("claude-opus-4.6",)),
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
                provider_metadata=_listing_metadata(values),
            )
        )
    return normalized


# Description phrasing a provider uses to mark a superseded model. Deliberately
# word-bounded and short: these are signals the provider *wrote*, not a guess
# from the slug, so no model id ever appears in this module's ranking logic.
_LEGACY_DESCRIPTION = re.compile(
    r"\b(older|legacy|previous[- ]generation|superseded|deprecated)\b",
    re.IGNORECASE,
)


def _listing_metadata(values: dict[str, Any]) -> dict[str, Any]:
    """Carry provider listing signals (visibility, priority, legacy) into metadata.

    Reads both the raw provider shape (top-level ``visibility``/``priority``, as
    in ``~/.codex/models_cache.json``) and an already-normalized record, whose
    signals live in ``provider_metadata`` — refresh() re-normalizes adapter output,
    so the second form must survive a round trip unchanged.
    """
    metadata = dict(values.get("provider_metadata") or {})
    visibility = values.get("visibility")
    if isinstance(visibility, str) and visibility.strip():
        metadata["visibility"] = visibility.strip().casefold()
    priority = _number(values.get("priority"))
    if priority is not None:
        metadata["priority"] = priority
    if isinstance(values.get("supported_in_api"), bool):
        metadata["supported_in_api"] = values["supported_in_api"]
    description = values.get("description")
    if isinstance(description, str) and description.strip():
        metadata.setdefault("description", description.strip())
    described = metadata.get("description")
    if "legacy" not in metadata and isinstance(described, str):
        metadata["legacy"] = bool(_LEGACY_DESCRIPTION.search(described))
    return metadata


def is_provider_hidden(model: DiscoveredModel) -> bool:
    """True when the provider itself keeps this model out of its user-facing list.

    Codex ships internal models (an auto-approval reviewer, reserve capacity) in
    the same cache as the picker models, marked ``visibility: "hide"``. They stay
    in the catalog for display but must never be routed to.
    """
    metadata = model.provider_metadata
    return (
        str(metadata.get("visibility") or "").casefold() in {"hide", "hidden"}
        or metadata.get("supported_in_api") is False
    )


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


# A discovery adapter that tiers its own catalog from provider evidence (OpenCode
# cost/family/release data, Copilot's listed ids) records the decision here, in
# provider_metadata, so it survives refresh()'s re-normalization and the DB round
# trip. ``adapter_tier`` may be None: the adapter looked and chose not to route it.
ADAPTER_TIER_KEY = "adapter_tier"
ADAPTER_TIER_REASON_KEY = "adapter_tier_reason"
# Set by an adapter on a tier pick allowed to widen a provider's static
# ``allowed_auto_route_tiers`` (see model_catalog.apply_catalog_projection).
WIDENS_AUTO_ROUTE_KEY = "widens_auto_route"


def assign_provider_relative_tiers(
    models: list[DiscoveredModel],
    *,
    pins: dict[str, str] | None = None,
) -> list[DiscoveredModel]:
    """Assign tiers within one provider; missing evidence remains unclassified."""
    pins = pins or {}
    adapter_decided: set[str] = set()

    for model in models:
        pinned = pins.get(model.model_id)
        adapter_reason = model.provider_metadata.get(ADAPTER_TIER_REASON_KEY)
        if pinned in TIERS:
            model.tier = pinned
            model.tier_reason = "operator_pin"
            model.routeable = model.available and not model.deprecated
        elif is_provider_hidden(model):
            # An operator pin is explicit intent and still wins; anything else the
            # provider hides is catalogued but never classified, so it cannot be
            # projected onto a tier.
            model.tier = None
            model.tier_reason = "hidden_by_provider"
        elif isinstance(adapter_reason, str) and adapter_reason:
            # Never re-ranked below: price ranking over a 466-model OpenCode list
            # is what made a $0 paid router the low tier.
            adapter_tier = model.provider_metadata.get(ADAPTER_TIER_KEY)
            model.tier = adapter_tier if adapter_tier in TIERS else None
            model.tier_reason = adapter_reason
            adapter_decided.add(model.model_id)

    active = [
        model for model in models
        if model.available
        and not model.deprecated
        and model.tier_reason != "hidden_by_provider"
        and model.model_id not in adapter_decided
    ]

    unassigned = [model for model in active if model.tier is None]
    _assign_prominence(unassigned)
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


# Capability class from the provider's own capability/description evidence.
_CLASS_NAMES = ("fast", "balanced", "flagship")


def _capability_class(model: DiscoveredModel) -> int:
    capability_set = {value.casefold() for value in model.capabilities}
    if "flagship" in capability_set or "advanced-reasoning" in capability_set:
        return 2
    if capability_set & {"mini", "fast", "small"}:
        return 0
    return 1


def _assign_prominence(models: list[DiscoveredModel]) -> None:
    """Tier a provider-ranked catalog (one that publishes a ``priority``).

    The rule, applied per tier, picks the first model by:

    1. **current before legacy** — a model the provider describes as older /
       legacy only fills a tier no current model can;
    2. **capability distance** — |class - tier| with classes fast=0,
       balanced=1, flagship=2 (from the provider's description/capabilities);
    3. **stronger on a tie** — the same direction ``_select_tier_model`` falls
       back in (medium tries high before low);
    4. **provider priority** — lower is more prominent in the provider's picker;
    5. model id, so the result is deterministic.

    Every ranked model is eligible for every tier, so all three tiers are filled
    whenever one listed model exists — a tier never falls back to a bootstrap id
    the provider's catalog does not contain. With a single current model that
    model holds every tier, and the tier then differs only in reasoning effort
    (``supported_reasoning_levels`` is preserved on the model for that). The
    per-tier position is stored as ``provider_metadata["tier_rank"]``, which
    ``tier_projection`` sorts on, and ``tier_reason`` names the evidence used.
    """
    ranked = [model for model in models if _number(model.provider_metadata.get("priority")) is not None]
    if not ranked:
        return
    for model in ranked:
        model.provider_metadata["tier_rank"] = {}
    for tier_index, tier in enumerate(TIERS):
        ordered = sorted(
            ranked,
            key=lambda m: (
                bool(m.provider_metadata.get("legacy")),
                abs(_capability_class(m) - tier_index),
                -_capability_class(m),
                float(m.provider_metadata["priority"]),
                m.model_id,
            ),
        )
        for position, model in enumerate(ordered):
            model.provider_metadata["tier_rank"][tier] = position
    for model in ranked:
        capability_class = _capability_class(model)
        legacy = bool(model.provider_metadata.get("legacy"))
        model.tier = TIERS[capability_class]
        model.provider_metadata["eligible_tiers"] = list(TIERS)
        model.tier_reason = (
            f"provider_prominence:class={_CLASS_NAMES[capability_class]},"
            f"priority={model.provider_metadata['priority']:g},"
            f"{'legacy' if legacy else 'current'}"
        )


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


_VERSION_TOKEN = re.compile(r"^v?\d+(?:\.\d+)*$")


def model_name_tokens(model_id: str) -> list[str]:
    """Lower-cased name tokens of the id's last path segment."""
    bare = model_id.rsplit("/", 1)[-1].casefold()
    return [token for token in re.split(r"[-_:]", bare) if token]


def is_version_token(token: str) -> bool:
    return bool(_VERSION_TOKEN.match(token))


def model_family(model_id: str) -> str:
    """Version-free family of a model id: ``claude-sonnet-4.6`` -> ``claude-sonnet``.

    The same naming models.dev / OpenCode publish as ``family`` (``gpt-5.6-terra``
    -> ``gpt-terra``), so a family derived here compares with one read there.
    """
    return "-".join(token for token in model_name_tokens(model_id) if not _VERSION_TOKEN.match(token))


def model_version(model_id: str) -> tuple[int, ...]:
    """Numeric version parts in id order (``claude-opus-4.8`` -> ``(4, 8)``)."""
    parts: list[int] = []
    for token in model_name_tokens(model_id):
        if _VERSION_TOKEN.match(token):
            parts.extend(int(piece) for piece in token.lstrip("v").split("."))
    return tuple(parts)


# Host shells whose bootstrap tiers anchor family-based tiering, in precedence
# order: the first host to place a family on a tier owns it.
_HOST_FAMILY_SOURCES = ("claude-code", "codex", "github-copilot")


def host_tier_families() -> dict[str, tuple[str, ...]]:
    """``{tier: families}`` derived from the host bootstraps, not a slug list.

    A name with no version token (``sonnet``) is a CLI alias, not a family, and
    is skipped; ``claude-sonnet-5`` contributes ``claude-sonnet``.
    """
    result: dict[str, list[str]] = {tier: [] for tier in TIERS}
    seen: set[str] = set()
    for provider_id in _HOST_FAMILY_SOURCES:
        for model in BOOTSTRAP_REGISTRY.get(provider_id, ()):
            if model.tier not in TIERS:
                continue
            for name in (model.model_id, *model.aliases):
                family = model_family(name)
                if not family or family == name.casefold() or family in seen:
                    continue
                seen.add(family)
                result[model.tier].append(family)
    return {tier: tuple(families) for tier, families in result.items()}


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
            key=lambda model, tier=tier: (
                # An operator pin for this tier is explicit intent; then a
                # provider-ranked position (see _assign_prominence); then cost.
                0 if model.tier_reason == "operator_pin" and model.tier == tier else 1,
                _tier_rank(model, tier),
                model.request_multiplier if model.request_multiplier is not None else float("inf"),
                model.input_price_per_million if model.input_price_per_million is not None else float("inf"),
                model.model_id,
            ),
        )
        if candidates:
            result[tier] = candidates[0].model_id
    return result


def _tier_rank(model: DiscoveredModel, tier: str) -> float:
    ranks = model.provider_metadata.get("tier_rank")
    if isinstance(ranks, dict):
        value = _number(ranks.get(tier))
        if value is not None:
            return value
    return float("inf")


def _codex_home() -> Path:
    """Codex's state directory — ``$CODEX_HOME`` when set, as the CLI honors it."""
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _parse_iso_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        log.debug("model_registry: unparseable timestamp %r", value, exc_info=True)
        return None


def load_codex_cache(path: Path | None = None) -> DiscoveryResult | None:
    cache_path = path or _codex_home() / "models_cache.json"
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entries = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return None
    # Codex stamps when it fetched the list; that, not the file mtime, is the
    # catalog's age. mtime is the fallback for caches without the field.
    fetched_at = _parse_iso_timestamp(raw.get("fetched_at")) if isinstance(raw, dict) else None
    try:
        timestamp = fetched_at if fetched_at is not None else cache_path.stat().st_mtime
    except OSError:
        log.debug("model_registry: codex cache stat failed", exc_info=True)
        return None
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


def load_claude_cache(path: Path | None = None) -> DiscoveryResult | None:
    """Load discovered models from official Claude Code cache if present."""
    cache_path = path or Path.home() / ".claude" / "models_cache.json"
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entries = raw.get("models") or raw.get("availableModels") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return None
    timestamp = cache_path.stat().st_mtime
    return DiscoveryResult(
        provider_id="claude-code",
        models=normalize_models("claude-code", entries, source="official_cli_cache", discovered_at=timestamp),
        source="official_cli_cache",
        discovered_at=timestamp,
    )


# ---------------------------------------------------------------------------
# Claude Code alias resolution — LEDGER ATTRIBUTION ONLY
# ---------------------------------------------------------------------------
# Claude Code is spawned with tier aliases (``haiku``/``sonnet``/``opus``) and
# resolves them to the latest model itself, which is the right thing for
# execution. It is the wrong thing for the quality ledger: rows keyed on the
# alias merge Sonnet 5 history with Sonnet 5.5 history across an upgrade, so a
# graded result silently transfers to a model it was never measured on.
# Nothing here may change what is spawned — callers that execute keep the alias.
#
# Maintained table of what each alias resolves to today. When Claude Code moves
# an alias, update the entry: old ledger rows keep the id they were graded on and
# the new model starts its own history, which is the point.
CLAUDE_ALIAS_TABLE: dict[str, str] = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5-1",
}
CLAUDE_CODE_PROVIDER_IDS = frozenset({"claude-code"})
_ALIAS_SUFFIX = re.compile(r"\[[^\]]*\]$")


def _claude_settings_path() -> Path | None:
    """Claude Code's user settings file (``$CLAUDE_CONFIG_DIR`` when set)."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return base / "settings.json"


def _load_claude_settings(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        log.debug("model_registry: unreadable Claude settings %s", path, exc_info=True)
        return {}
    return raw if isinstance(raw, dict) else {}


def _family_match(alias: str, value: Any) -> str | None:
    """A *concrete* id naming the alias's family (``sonnet`` in ``claude-sonnet-5-5``).

    ``ANTHROPIC_MODEL`` and settings ``model`` set the session default, not the
    alias mapping, so they only count as evidence when they unambiguously pin the
    same family. An alias value (``"opus"``) carries no version and is ignored.
    """
    if not isinstance(value, str):
        return None
    candidate = _ALIAS_SUFFIX.sub("", value.strip())
    if not candidate or candidate.casefold() in CLAUDE_ALIAS_TABLE:
        return None
    return candidate if alias in candidate.casefold() else None


def resolve_model_alias(
    provider_id: str | None,
    model: str | None,
    *,
    env: dict[str, str] | None = None,
    settings_path: Path | None | object = ...,
) -> tuple[str, str]:
    """Return ``(concrete_model_id, source)`` for a model as it should be ATTRIBUTED.

    Never use the result to spawn. Sources, first match wins:

    * ``reported`` — not a known alias (already concrete, e.g.
      ``record_outcome(actual_model="claude-sonnet-5-5")``, or another
      provider's id): passed through unchanged;
    * ``env:ANTHROPIC_DEFAULT_<ALIAS>_MODEL`` — the per-alias override Claude Code
      honors, then ``env:ANTHROPIC_MODEL`` when it pins the same family;
    * ``settings:env.<VAR>`` / ``settings:model`` — the same, read from Claude
      Code's ``settings.json``;
    * ``alias_table`` — :data:`CLAUDE_ALIAS_TABLE`.

    ``provider_id=None`` means "unknown provider": the bare aliases are Claude
    Code's own vocabulary, so they still resolve. Any other provider passes
    through, because ``opus`` there would be that provider's name, not Claude's.
    """
    raw = (model or "").strip()
    if not raw:
        return "", "unresolved"
    if provider_id is not None and provider_id not in CLAUDE_CODE_PROVIDER_IDS:
        return raw, "reported"
    alias = _ALIAS_SUFFIX.sub("", raw).casefold()
    if alias not in CLAUDE_ALIAS_TABLE:
        return raw, "reported"

    environ = os.environ if env is None else env
    per_alias_var = f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL"
    value = str(environ.get(per_alias_var) or "").strip()
    if value and value.casefold() not in CLAUDE_ALIAS_TABLE:
        return value, f"env:{per_alias_var}"
    matched = _family_match(alias, environ.get("ANTHROPIC_MODEL"))
    if matched:
        return matched, "env:ANTHROPIC_MODEL"

    path = _claude_settings_path() if settings_path is ... else settings_path
    settings = _load_claude_settings(path if isinstance(path, Path) else None)
    settings_env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    value = str(settings_env.get(per_alias_var) or "").strip()
    if value and value.casefold() not in CLAUDE_ALIAS_TABLE:
        return value, f"settings:env.{per_alias_var}"
    matched = _family_match(alias, settings_env.get("ANTHROPIC_MODEL"))
    if matched:
        return matched, "settings:env.ANTHROPIC_MODEL"
    matched = _family_match(alias, settings.get("model"))
    if matched:
        return matched, "settings:model"

    return CLAUDE_ALIAS_TABLE[alias], "alias_table"
