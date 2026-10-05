"""Provider-native normalized model discovery adapters."""
from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .model_registry import (
    ADAPTER_TIER_KEY,
    ADAPTER_TIER_REASON_KEY,
    BOOTSTRAP_REGISTRY,
    TIERS,
    WIDENS_AUTO_ROUTE_KEY,
    DiscoveryResult,
    host_tier_families,
    is_version_token,
    load_claude_cache,
    load_codex_cache,
    model_family,
    model_name_tokens,
    model_version,
    normalize_claude_agent_sdk_models,
    normalize_models,
)

log = logging.getLogger(__name__)


def _parse_json_or_lines(raw: str) -> list[dict[str, Any] | str]:
    payload = raw.strip()
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return [
            line.strip()
            for line in payload.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "Available models"))
        ]
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("models", "data", "availableModels", "available_models"):
            entries = parsed.get(key)
            if isinstance(entries, list):
                return entries
    return []


@dataclass(slots=True)
class CommandModelDiscoveryAdapter:
    provider_id: str
    command: tuple[str, ...]
    source: str = "live_provider_catalog"
    env_factory: Callable[[], dict[str, str]] | None = None
    cwd_factory: Callable[[], str] | None = None

    def discover_live(self) -> DiscoveryResult | None:
        completed = subprocess.run(
            list(self.command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            env=self.env_factory() if self.env_factory else None,
            cwd=self.cwd_factory() if self.cwd_factory else None,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{self.provider_id}: model discovery exited {completed.returncode}"
            )
        models = normalize_models(
            self.provider_id,
            _parse_json_or_lines(completed.stdout),
            source=self.source,
        )
        return DiscoveryResult(
            provider_id=self.provider_id,
            models=models,
            source=self.source,
            successful=bool(models),
        )

    def discover_official_cache(self) -> DiscoveryResult | None:
        return None


@dataclass(slots=True)
class CodexModelDiscoveryAdapter:
    provider_id: str = "codex"
    app_server_catalog: Callable[[], dict[str, Any] | list[Any] | None] | None = None

    def discover_live(self) -> DiscoveryResult | None:
        if self.app_server_catalog is None:
            return None
        payload = self.app_server_catalog()
        entries = payload.get("models") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return None
        return DiscoveryResult(
            provider_id=self.provider_id,
            models=normalize_models(
                self.provider_id,
                entries,
                source="codex_app_server",
            ),
            source="codex_app_server",
        )

    def discover_official_cache(self) -> DiscoveryResult | None:
        return load_codex_cache()


@dataclass(slots=True)
class CallbackModelDiscoveryAdapter:
    provider_id: str
    catalog: Callable[[], dict[str, Any] | list[Any] | None] | None = None
    source: str = "live_provider_catalog"

    def discover_live(self) -> DiscoveryResult | None:
        if self.catalog is None:
            return None
        payload = self.catalog()
        entries = payload.get("models") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return None
        models = normalize_models(self.provider_id, entries, source=self.source)
        return DiscoveryResult(
            provider_id=self.provider_id,
            models=models,
            source=self.source,
            successful=bool(models),
        )

    def discover_official_cache(self) -> DiscoveryResult | None:
        return None


@dataclass(slots=True)
class ClaudeModelDiscoveryAdapter:
    provider_id: str = "claude-code"
    agent_sdk_init: Callable[[], dict[str, Any] | None] | None = None

    def discover_live(self) -> DiscoveryResult | None:
        if self.agent_sdk_init is None:
            return None
        payload = self.agent_sdk_init()
        return normalize_claude_agent_sdk_models(payload) if isinstance(payload, dict) else None

    def discover_official_cache(self) -> DiscoveryResult | None:
        return load_claude_cache()


def _run_listing(
    command: tuple[str, ...],
    *,
    provider_id: str,
    timeout: int,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> str:
    completed = subprocess.run(
        list(command),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{provider_id}: {' '.join(command)} exited {completed.returncode}")
    return completed.stdout


def _version_sort_key(model_id: str) -> tuple[int, ...]:
    """Newest first under ``min``/ascending sort; padded so ``4.8`` beats ``4``."""
    version = (model_version(model_id) + (0, 0, 0, 0))[:4]
    return tuple(-part for part in version)


# ---------------------------------------------------------------------------
# OpenCode: `opencode models --verbose`
# ---------------------------------------------------------------------------

# Providers whose zero-cost models form the low tier. `opencode` (Zen) is listed
# for every install, so it is never evidence of an account the user configured.
_OPENCODE_FREE_PROVIDERS = frozenset({"opencode", "opencode-go"})
_OPENCODE_UNCONFIGURED_PROVIDERS = frozenset({"opencode"})
_OPENCODE_EXCLUDED_STATUSES = frozenset({"deprecated", "inactive", "disabled", "retired", "removed"})
# A medium/high pick should be the family's standard model, not a speed or
# early-access variant of it, nor its small sibling.
_UPPER_TIER_SKIP_TOKENS = ("fast", "preview", "mini")


def _opencode_entry(model_id: str, details: dict[str, Any] | None) -> dict[str, Any]:
    provider = model_id.partition("/")[0]
    metadata: dict[str, Any] = {"provider": provider}
    entry: dict[str, Any] = {"model_id": model_id, "provider_metadata": metadata}
    if details is None:
        return entry
    provider_id = details.get("providerID")
    if isinstance(provider_id, str) and provider_id:
        metadata["provider"] = provider_id
    for key in ("family", "status", "release_date"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            metadata[key] = value.strip()
    name = details.get("name")
    if isinstance(name, str) and name.strip():
        entry["display_name"] = name.strip()
    status = str(metadata.get("status") or "").casefold()
    entry["deprecated"] = status in _OPENCODE_EXCLUDED_STATUSES
    cost = details.get("cost") if isinstance(details.get("cost"), dict) else {}
    for source_key, target_key in (("input", "input_price_per_million"), ("output", "output_price_per_million")):
        value = cost.get(source_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            entry[target_key] = float(value)
    limit = details.get("limit") if isinstance(details.get("limit"), dict) else {}
    if isinstance(limit.get("context"), (int, float)):
        entry["context_window"] = limit["context"]
    capabilities = details.get("capabilities") if isinstance(details.get("capabilities"), dict) else {}
    entry["capabilities"] = ["text"] + [
        label for key, label in (("reasoning", "reasoning"), ("toolcall", "tools"))
        if capabilities.get(key) is True
    ]
    variants = details.get("variants")
    if isinstance(variants, dict):
        entry["reasoning_levels"] = [str(name) for name in variants]
        metadata["variants_listed"] = True
    return entry


def parse_opencode_verbose(raw: str) -> list[dict[str, Any]]:
    """Pair each ``provider/model`` line with the JSON object that follows it.

    The object spans many lines, so it is decoded in place (``raw_decode`` at an
    offset) rather than line by line. Anything else — banners, warnings, a
    truncated object — is skipped; an id with no decodable object is kept bare.
    """
    decoder = json.JSONDecoder()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    pos, length = 0, len(raw)
    while pos < length:
        end = raw.find("\n", pos)
        end = length if end == -1 else end
        line = raw[pos:end].strip()
        pos = end + 1
        if not line or "/" not in line or line[0] in "{}[]\"" or any(ch.isspace() for ch in line):
            continue
        details: Any = None
        start = pos
        while start < length and raw[start].isspace():
            start += 1
        if start < length and raw[start] == "{":
            try:
                details, pos = decoder.raw_decode(raw, start)
            except json.JSONDecodeError:
                log.debug("opencode: undecodable details for %s", line, exc_info=True)
                details = None
        if line in seen:
            continue
        seen.add(line)
        entries.append(_opencode_entry(line, details if isinstance(details, dict) else None))
    return entries


def _metadata(entry: dict[str, Any]) -> dict[str, Any]:
    metadata = entry.get("provider_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        entry["provider_metadata"] = metadata
    return metadata


def _decide(entry: dict[str, Any], tier: str | None, reason: str, *, rank: int | None = None) -> None:
    metadata = _metadata(entry)
    metadata[ADAPTER_TIER_KEY] = tier
    metadata[ADAPTER_TIER_REASON_KEY] = reason
    if tier is not None and rank is not None:
        metadata.setdefault("tier_rank", {})[tier] = rank


def _opencode_router_reason(entry: dict[str, Any]) -> str | None:
    model_id = str(entry["model_id"])
    family = str(_metadata(entry).get("family") or "").casefold()
    if family == "auto" or model_id.startswith("openrouter/openrouter/") or model_id.endswith("/auto"):
        return "opencode:excluded=router"
    return None


def _zero_cost(entry: dict[str, Any]) -> bool:
    prices = [entry.get("input_price_per_million"), entry.get("output_price_per_million")]
    known = [price for price in prices if isinstance(price, (int, float))]
    return bool(known) and all(price == 0 for price in known)


def tier_opencode_catalog(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decide every OpenCode tier from listing evidence; mutates and returns *entries*.

    * routers (``family == auto``, ``openrouter/openrouter/*``, ``*/auto``) and
      deprecated/inactive models are catalogued but never routed;
    * **low** — a zero-cost model from a free provider: ``-free`` suffix first,
      then reasoning support, largest context, the Zen provider, id;
    * **medium/high** — only from providers the user configured (every listed
      prefix except Zen and routers), matched by family to the host tier
      families (``claude-sonnet`` → medium, ``claude-opus`` → high, gpt
      equivalents after), newest ``release_date``, then cheapest, then id.

    Without the verbose metadata (plain ``opencode models`` fallback) only the
    ``-free`` suffix of a Zen model is evidence, so only low can be filled.
    """
    families = host_tier_families()
    eligible: list[dict[str, Any]] = []
    for entry in entries:
        reason = _opencode_router_reason(entry)
        status = str(_metadata(entry).get("status") or "").casefold()
        if reason is None and status in _OPENCODE_EXCLUDED_STATUSES:
            reason = f"opencode:excluded=status:{status}"
        if reason is not None:
            _decide(entry, None, reason)
            continue
        _decide(entry, None, "opencode:unselected")
        eligible.append(entry)

    def provider_of(entry: dict[str, Any]) -> str:
        return str(_metadata(entry).get("provider") or str(entry["model_id"]).partition("/")[0])

    def free_suffix(entry: dict[str, Any]) -> bool:
        return str(entry["model_id"]).endswith("-free")

    def has_details(entry: dict[str, Any]) -> bool:
        return bool(_metadata(entry).get("variants_listed") or _metadata(entry).get("release_date"))

    low_pool = [
        entry for entry in eligible
        if provider_of(entry) in _OPENCODE_FREE_PROVIDERS
        and (
            _zero_cost(entry)
            or (not has_details(entry) and provider_of(entry) == "opencode" and free_suffix(entry))
        )
    ]
    low_winner: dict[str, Any] | None = None
    if low_pool:
        low_winner = min(
            low_pool,
            key=lambda entry: (
                not free_suffix(entry),
                "reasoning" not in entry.get("capabilities", ()),
                -int(entry.get("context_window") or 0),
                provider_of(entry) != "opencode",
                str(entry["model_id"]),
            ),
        )
        _decide(
            low_winner,
            "low",
            "opencode:zero_cost"
            f",provider={provider_of(low_winner)}"
            f",free_suffix={str(free_suffix(low_winner)).lower()}"
            f",reasoning={str('reasoning' in low_winner.get('capabilities', ())).lower()}"
            f",context={int(low_winner.get('context_window') or 0)}",
            rank=0,
        )

    configured = {provider_of(entry) for entry in eligible} - _OPENCODE_UNCONFIGURED_PROVIDERS
    upper_pool: list[dict[str, Any]] = []
    for entry in eligible:
        if entry is low_winner or provider_of(entry) not in configured or not has_details(entry):
            continue
        tokens = set(model_name_tokens(str(entry["model_id"])))
        skipped = next((token for token in _UPPER_TIER_SKIP_TOKENS if token in tokens), None)
        if skipped:
            _decide(entry, None, f"opencode:skipped={skipped}")
            continue
        upper_pool.append(entry)

    def family_of(entry: dict[str, Any]) -> str:
        return str(_metadata(entry).get("family") or model_family(str(entry["model_id"]))).casefold()

    def price(entry: dict[str, Any]) -> float:
        return float(entry.get("input_price_per_million") or 0.0) + float(entry.get("output_price_per_million") or 0.0)

    for tier in ("medium", "high"):
        for family in families.get(tier, ()):
            candidates = [entry for entry in upper_pool if family_of(entry) == family]
            if not candidates:
                continue
            newest = max(str(_metadata(entry).get("release_date") or "") for entry in candidates)
            winner = min(
                (entry for entry in candidates if str(_metadata(entry).get("release_date") or "") == newest),
                key=lambda entry: (price(entry), str(entry["model_id"])),
            )
            _decide(
                winner,
                tier,
                f"opencode:family={family},release={newest or 'unknown'},provider={provider_of(winner)}",
                rank=0,
            )
            _metadata(winner)[WIDENS_AUTO_ROUTE_KEY] = True
            upper_pool.remove(winner)
            break
    return entries


@dataclass(slots=True)
class OpenCodeModelDiscoveryAdapter:
    """``opencode models --verbose`` (cost/family/variants), plain list as fallback."""

    provider_id: str = "opencode"
    verbose_command: tuple[str, ...] = ("opencode", "models", "--verbose")
    plain_command: tuple[str, ...] = ("opencode", "models")
    source: str = "live_provider_catalog"
    timeout: int = 15

    def discover_live(self) -> DiscoveryResult | None:
        entries: list[dict[str, Any]] = []
        try:
            entries = parse_opencode_verbose(
                _run_listing(self.verbose_command, provider_id=self.provider_id, timeout=self.timeout)
            )
        except (FileNotFoundError, OSError, RuntimeError, subprocess.TimeoutExpired):
            log.debug("opencode: verbose model listing failed; using the plain list", exc_info=True)
        if not entries:
            raw = _run_listing(self.plain_command, provider_id=self.provider_id, timeout=self.timeout)
            entries = [
                {"model_id": item.strip(), "provider_metadata": {"provider": item.strip().partition("/")[0]}}
                for item in _parse_json_or_lines(raw)
                if isinstance(item, str) and item.strip()
            ]
        tier_opencode_catalog(entries)
        models = normalize_models(self.provider_id, entries, source=self.source)
        return DiscoveryResult(
            provider_id=self.provider_id,
            models=models,
            source=self.source,
            successful=bool(models),
        )

    def discover_official_cache(self) -> DiscoveryResult | None:
        return None


# ---------------------------------------------------------------------------
# GitHub Copilot: `gh copilot -- help config`
# ---------------------------------------------------------------------------

_COPILOT_MODEL_HEADER = re.compile(r"^\s*`model`\s*:")
_COPILOT_MODEL_ITEM = re.compile(r'^\s*-\s*"([^"\s]+)"\s*$')
_COPILOT_LOW_TOKENS = ("mini", "nano", "haiku", "flash", "lite", "small")
_COPILOT_HIGH_TOKENS = ("opus", "pro")
# Speed / early-access variants of a family: tiered with it, ranked after it.
_COPILOT_MODIFIER_TOKENS = frozenset({"fast", "preview"})


def parse_copilot_help_config(raw: str) -> list[str]:
    """Model ids from the `` `model`: `` block of ``copilot help config``.

    The block is a run of ``- "id"`` lines after the header; it ends at the first
    blank or non-item line once items have started.
    """
    ids: list[str] = []
    in_block = False
    for line in raw.splitlines():
        if not in_block:
            in_block = bool(_COPILOT_MODEL_HEADER.match(line))
            continue
        match = _COPILOT_MODEL_ITEM.match(line)
        if match:
            if match.group(1) not in ids:
                ids.append(match.group(1))
            continue
        if ids or not line.strip():
            break
    return ids


def tier_copilot_catalog(model_ids: list[str]) -> list[dict[str, Any]]:
    """Tier Copilot's listed ids deterministically; no cost data is published.

    Evidence, strongest first: the id is a bootstrap pick (curated with the
    premium-request multiplier the help text does not show — ``gpt-5-mini`` is
    the 0x model that makes Copilot's low tier free); its family is a host tier
    family; a size token (mini/nano/haiku/flash → low, opus/pro → high);
    otherwise medium. Within a tier: evidence, family precedence, standard
    before fast/preview, newest version, id.
    """
    from .model_capabilities import copilot_effort_levels, ordered_levels

    bootstrap = {model.model_id: model for model in BOOTSTRAP_REGISTRY.get("github-copilot", ())}
    family_tier: dict[str, tuple[str, int]] = {}
    for tier, names in host_tier_families().items():
        for index, family in enumerate(names):
            family_tier.setdefault(family, (tier, index))

    entries: list[dict[str, Any]] = []
    ranking: dict[str, list[tuple[tuple[Any, ...], dict[str, Any]]]] = {tier: [] for tier in TIERS}
    for model_id in model_ids:
        tokens = model_name_tokens(model_id)
        modifiers = sorted(set(tokens) & _COPILOT_MODIFIER_TOKENS)
        family = "-".join(
            token for token in tokens
            if not is_version_token(token) and token not in _COPILOT_MODIFIER_TOKENS
        )
        family_index = 99
        boot = bootstrap.get(model_id)
        if boot is not None and boot.tier in TIERS:
            tier, evidence, reason = boot.tier, 0, "copilot:bootstrap_listed"
        elif family in family_tier:
            tier, family_index = family_tier[family]
            evidence, reason = 1, f"copilot:family={family}"
        elif low := next((token for token in _COPILOT_LOW_TOKENS if token in tokens), None):
            tier, evidence, reason = "low", 2, f"copilot:size_token={low}"
        elif high := next((token for token in _COPILOT_HIGH_TOKENS if token in tokens), None):
            tier, evidence, reason = "high", 2, f"copilot:size_token={high}"
        else:
            tier, evidence, reason = "medium", 3, f"copilot:unrecognized_family={family}"
        if modifiers:
            reason += f",variant={'+'.join(modifiers)}"
        entry: dict[str, Any] = {
            "model_id": model_id,
            # Listed by the CLI itself, so the id is checked (unlike bootstrap).
            "provider_metadata": {"verified": True, "family": family},
        }
        if boot is not None and boot.request_multiplier is not None:
            entry["request_multiplier"] = boot.request_multiplier
        levels = copilot_effort_levels(model_id)
        if levels is not None:
            entry["reasoning_levels"] = ordered_levels(levels)
        _decide(entry, tier, reason)
        ranking[tier].append(
            ((evidence, family_index, bool(modifiers), _version_sort_key(model_id), model_id), entry)
        )
        entries.append(entry)
    for tier, ranked in ranking.items():
        for position, (_key, entry) in enumerate(sorted(ranked, key=lambda item: item[0])):
            _metadata(entry).setdefault("tier_rank", {})[tier] = position
    return entries


@dataclass(slots=True)
class CopilotHelpConfigDiscoveryAdapter:
    """Copilot's model list from ``gh copilot -- help config``.

    A help command: no model call, no premium request. ``None`` (bootstrap /
    last-known-good) when the block is absent from the output.
    """

    provider_id: str = "github-copilot"
    command: tuple[str, ...] = ("gh", "copilot", "--", "help", "config")
    source: str = "copilot_help_config"
    env_factory: Callable[[], dict[str, str]] | None = None
    cwd_factory: Callable[[], str] | None = None
    timeout: int = 15

    def discover_live(self) -> DiscoveryResult | None:
        raw = _run_listing(
            self.command,
            provider_id=self.provider_id,
            timeout=self.timeout,
            env=self.env_factory() if self.env_factory else None,
            cwd=self.cwd_factory() if self.cwd_factory else None,
        )
        model_ids = parse_copilot_help_config(raw)
        if not model_ids:
            log.debug("copilot: no `model` block in help config output")
            return None
        models = normalize_models(self.provider_id, tier_copilot_catalog(model_ids), source=self.source)
        return DiscoveryResult(
            provider_id=self.provider_id,
            models=models,
            source=self.source,
            successful=bool(models),
        )

    def discover_official_cache(self) -> DiscoveryResult | None:
        return None
