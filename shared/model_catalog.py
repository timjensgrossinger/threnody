#!/usr/bin/env python3
"""Persistent normalized model catalogs and discovery-source precedence."""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from .db import Database
from .discovery import CLIProvider, DetectReason, ProviderReadiness, ProviderRegistry
from .model_registry import (
    DiscoveredModel,
    assign_provider_relative_tiers,
    bootstrap_models,
    normalize_models,
    load_codex_cache,
    tier_projection,
)

_PRICE_DATA_PATH = Path(__file__).resolve().parent / "data" / "model_prices.json"
_LOW_TIER_MAX_PER_MILLION = 0.50
_MEDIUM_TIER_MAX_PER_MILLION = 5.00
_TIER_OVERRIDES: dict[str, str] = {
    "o1": "high",
    "o3": "high",
}
log = logging.getLogger(__name__)
_PRICE_DATA_CACHE: dict[str, dict[str, Any]] = {}
_PRICE_DATA_MTIME = 0.0
_PRICE_DATA_LOADED = False


def _load_price_data() -> dict[str, dict[str, Any]]:
    global _PRICE_DATA_CACHE, _PRICE_DATA_MTIME, _PRICE_DATA_LOADED
    try:
        mtime = _PRICE_DATA_PATH.stat().st_mtime
    except FileNotFoundError:
        _PRICE_DATA_CACHE = {}
        _PRICE_DATA_MTIME = 0.0
        _PRICE_DATA_LOADED = True
        return {}
    if _PRICE_DATA_LOADED and _PRICE_DATA_MTIME == mtime:
        return _PRICE_DATA_CACHE
    with _PRICE_DATA_PATH.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        _PRICE_DATA_CACHE = {}
        _PRICE_DATA_MTIME = mtime
        _PRICE_DATA_LOADED = True
        return {}
    _PRICE_DATA_CACHE = {
        str(model_id): details
        for model_id, details in raw.items()
        if isinstance(details, dict)
    }
    _PRICE_DATA_MTIME = mtime
    _PRICE_DATA_LOADED = True
    return _PRICE_DATA_CACHE


def _tier_from_cost(
    model_id: str,
    input_cost_per_token: float,
    user_overrides: dict[str, str] | None = None,
) -> str:
    """Resolve model tier: user config > bundled override > cost-based ranking."""
    # 1. User override (highest priority, per D-02)
    if user_overrides and model_id in user_overrides:
        return user_overrides[model_id]
    
    # 2. Bundled override
    if model_id in _TIER_OVERRIDES:
        return _TIER_OVERRIDES[model_id]
    
    # 3. Cost-based ranking
    cost_per_million = input_cost_per_token * 1_000_000
    if cost_per_million <= _LOW_TIER_MAX_PER_MILLION:
        return "low"
    if cost_per_million <= _MEDIUM_TIER_MAX_PER_MILLION:
        return "medium"
    return "high"


def _normalize_model_entry(provider: str, raw: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(raw, str):
        return {
            "model_id": raw,
            "provider": provider,
            "source": "discovered",
        }
    if not isinstance(raw, dict):
        raise TypeError(f"Unsupported model catalog entry: {type(raw)!r}")

    model_id = raw.get("model_id") or raw.get("id") or raw.get("model")
    if not isinstance(model_id, str) or not model_id:
        raise ValueError(f"Model entry must include model_id/id/model: {raw!r}")

    normalized = dict(raw)
    normalized["model_id"] = model_id
    normalized.setdefault("provider", provider)
    normalized.setdefault("source", "discovered")
    return normalized


def rank_models_with_price_data(
    discovered: list[dict[str, Any]],
    user_overrides: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper using provider-relative tier assignment.

    Bundled prices enrich records, but no global price boundary is used.
    """
    prices = _load_price_data()
    ranked: list[dict[str, Any]] = []

    for entry in discovered:
        ranked_entry = dict(entry)
        model_id = ranked_entry.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError(f"Model entry missing model_id: {entry!r}")
        
        # Operator pins take precedence even without price data.
        if user_overrides and model_id in user_overrides:
            ranked_entry["tier"] = user_overrides[model_id]
            ranked_entry["cost"] = ranked_entry.get("cost")
            ranked_entry["auto_routeable"] = True
            ranked.append(ranked_entry)
            continue
        
        price_info = prices.get(model_id)
        source = str(ranked_entry.get("source", "discovered"))
        fallback_tier = ranked_entry.get("tier")

        if price_info is None:
            if source == "static" and isinstance(fallback_tier, str):
                ranked_entry["tier"] = fallback_tier
                ranked_entry["cost"] = None
                ranked_entry["auto_routeable"] = True
            else:
                ranked_entry["tier"] = "unknown"
                ranked_entry["cost"] = None
                ranked_entry["auto_routeable"] = False
            ranked.append(ranked_entry)
            continue

        input_cost = price_info.get("input_cost_per_token")
        ranked_entry["cost"] = input_cost
        if source == "static" and isinstance(fallback_tier, str):
            ranked_entry["tier"] = fallback_tier
        elif isinstance(input_cost, (int, float)):
            ranked_entry["input_price_per_million"] = float(input_cost) * 1_000_000
            ranked_entry["tier"] = None
        else:
            ranked_entry["tier"] = "unknown"

        ranked_entry["auto_routeable"] = ranked_entry["tier"] != "unknown"
        ranked.append(ranked_entry)

    normalized = [
        DiscoveredModel.from_dict({
            "model_id": entry["model_id"],
            "display_name": entry.get("display_name") or entry["model_id"],
            "available": entry.get("available", True),
            "deprecated": entry.get("deprecated", False),
            "discovery_source": entry.get("source", "discovered"),
            "discovered_at": entry.get("discovered_at", time.time()),
            "aliases": entry.get("aliases", ()),
            "capabilities": entry.get("capabilities", ()),
            "context_window": entry.get("context_window"),
            "reasoning_levels": entry.get("reasoning_levels", ()),
            "input_price_per_million": entry.get("input_price_per_million"),
            "output_price_per_million": entry.get("output_price_per_million"),
            "request_multiplier": entry.get("request_multiplier"),
            "provider_metadata": entry.get("provider_metadata", {}),
            "tier": entry.get("tier") if entry.get("tier") != "unknown" else None,
            "tier_reason": "bootstrap" if entry.get("source") == "static" else None,
            "routeable": bool(entry.get("auto_routeable", False)),
        })
        for entry in ranked
    ]
    assign_provider_relative_tiers(normalized, pins=user_overrides)
    by_id = {model.model_id: model for model in normalized}
    for entry in ranked:
        model = by_id[entry["model_id"]]
        entry["tier"] = model.tier or "unknown"
        entry["auto_routeable"] = model.routeable
        entry["tier_reason"] = model.tier_reason
    return ranked


class ModelCatalog:
    """Read-through SQLite-backed model catalog with static fallback support."""

    def __init__(
        self,
        db: Database | None = None,
        stale_ttl_seconds: int = 86_400,
        user_overrides: dict[str, str] | None = None,
    ) -> None:
        if db is None:
            from .db_client import open_database
            db = open_database()
        self._db = db
        self._stale_ttl_seconds = stale_ttl_seconds
        self._user_overrides = user_overrides or {}
        self._provider_state: dict[str, dict[str, float | int]] = {}

    def _provider_state_for(self, provider: str) -> dict[str, float | int]:
        return self._provider_state.setdefault(
            provider,
            {
                "failed_refresh_count": 0,
                "last_failure_ts": 0.0,
                "cooldown_until": 0.0,
            },
        )

    def provider_state(self, provider: str) -> dict[str, float | int]:
        return dict(self._provider_state_for(provider))

    def record_refresh_failure(
        self,
        provider: str,
        *,
        backoff_seconds: int = 300,
    ) -> dict[str, float | int]:
        state = self._provider_state_for(provider)
        state["failed_refresh_count"] = int(state["failed_refresh_count"]) + 1
        state["last_failure_ts"] = time.time()
        if int(state["failed_refresh_count"]) >= 3:
            state["cooldown_until"] = float(state["last_failure_ts"]) + backoff_seconds
        return dict(state)

    def _catalog_snapshot(self, provider: str) -> tuple[int, float]:
        with self._db.conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*), COALESCE(MAX(stale_until), 0)
                FROM model_catalog
                WHERE provider = ?
                """,
                (provider,),
            ).fetchone()
        if row is None:
            return 0, 0.0
        return int(row[0]), float(row[1] or 0.0)

    def _project_provider_catalog(self, provider: CLIProvider) -> None:
        rows = self.get(provider.name)
        provider.model_catalog = rows
        models = [
            DiscoveredModel.from_dict({
                "model_id": row["model_id"],
                "display_name": row.get("display_name") or row["model_id"],
                "available": row.get("available", True),
                "deprecated": row.get("deprecated", False),
                "discovery_source": row["source"],
                "discovered_at": row.get("discovered_at", row["last_seen"]),
                "aliases": row.get("aliases", ()),
                "capabilities": row.get("capabilities", ()),
                "context_window": row.get("context_window"),
                "reasoning_levels": row.get("reasoning_levels", ()),
                "input_price_per_million": row.get("input_price_per_million"),
                "output_price_per_million": row.get("output_price_per_million"),
                "request_multiplier": row.get("request_multiplier"),
                "provider_metadata": row.get("provider_metadata", {}),
                "tier": None if row["tier"] == "unknown" else row["tier"],
                "tier_reason": row.get("tier_reason"),
                "routeable": row.get("auto_routeable", False),
            })
            for row in rows
        ]
        projected_tiers = tier_projection(models)
        allowed_tiers = (
            set(provider.allowed_auto_route_tiers)
            if provider.allowed_auto_route_tiers is not None
            else None
        )
        if allowed_tiers is not None:
            operator_pinned_tiers = {
                model.tier
                for model in models
                if model.tier_reason == "operator_pin" and model.tier is not None
            }
            effective_tiers = allowed_tiers | operator_pinned_tiers
            projected_tiers = {
                tier: model_id
                for tier, model_id in projected_tiers.items()
                if tier in effective_tiers
            }
            for row in provider.model_catalog:
                row["auto_routeable"] = bool(
                    row.get("auto_routeable", False)
                    and (
                        row.get("tier") in allowed_tiers
                        or row.get("tier_reason") == "operator_pin"
                    )
                )
            provider.cost_rank = {
                tier: rank
                for tier, rank in provider.cost_rank.items()
                if tier in effective_tiers
            }

        provider.tier_models = projected_tiers
        for tier in projected_tiers:
            provider.cost_rank.setdefault(tier, 1)

    @staticmethod
    def _parse_model_discovery_output(provider: "CLIProvider", raw: str) -> dict[str, list[str]]:
        """Parse model-discovery output as JSON or one-model-per-line text."""
        payload = raw.strip()
        if not payload:
            return {}
        if payload.startswith("["):
            parsed = json.loads(payload)
            if isinstance(parsed, list):
                # Convert list format to dict format {provider: [models]}
                return {provider.name: parsed}
        if payload.startswith("{"):
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                models = parsed.get("models")
                if isinstance(models, list):
                    return {provider.name: models}
        # Default: treat as one-model-per-line
        lines = [line.strip() for line in payload.splitlines() if line.strip()]
        return {provider.name: lines} if lines else {}

    @staticmethod
    def _command_for_provider(provider: CLIProvider) -> list[str]:
        if provider.model_discovery_cmd is None:
            return []
        if isinstance(provider.model_discovery_cmd, str):
            return shlex.split(provider.model_discovery_cmd)
        return list(provider.model_discovery_cmd)

    @staticmethod
    def _mark_provider_ready(provider: CLIProvider, *, checked_at: float) -> None:
        if provider.readiness.reason in {
            DetectReason.AUTH_FAILED,
            DetectReason.AUTH_UNKNOWN,
            DetectReason.BINARY_MISSING,
            DetectReason.ENDPOINT_UNREACHABLE,
        }:
            return
        provider.readiness = ProviderReadiness(
            routeable=True,
            reason=DetectReason.READY,
            last_checked=checked_at,
        )
        provider.detect_reason = provider.readiness.reason

    def get(self, provider: str) -> list[dict[str, Any]]:
        with self._db.conn() as conn:
            rows = conn.execute(
                """
                SELECT model_id, provider, tier, cost, last_seen, source, stale_until,
                       metadata_json
                FROM model_catalog
                WHERE provider = ?
                ORDER BY CASE tier
                    WHEN 'low' THEN 0
                    WHEN 'medium' THEN 1
                    WHEN 'high' THEN 2
                    ELSE 3
                END, model_id
                """,
                (provider,),
            ).fetchall()

        result = []
        for row in rows:
            try:
                metadata = json.loads(row[7]) if row[7] else {}
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            result.append({
                "model_id": row[0],
                "provider": row[1],
                "tier": row[2],
                "cost": row[3],
                "last_seen": row[4],
                "source": row[5],
                "stale_until": row[6],
                "auto_routeable": row[2] != "unknown",
                **metadata,
            })
        return result

    def refresh(
        self,
        provider: str,
        discovered_models: list[dict[str, Any] | str],
        *,
        source: str = "live_provider_catalog",
        successful: bool = True,
    ) -> None:
        # A successful catalog is authoritative. Bootstrap is used only when no
        # live/cache/LKG catalog exists, never as gap fill.
        if successful and discovered_models:
            models = normalize_models(provider, discovered_models, source=source)
        else:
            current = self.get(provider)
            if current:
                return
            models = bootstrap_models(provider)
        prices = _load_price_data()
        for model in models:
            price_info = prices.get(model.model_id, {})
            if model.input_price_per_million is None:
                raw_price = price_info.get("input_cost_per_token")
                if isinstance(raw_price, (int, float)):
                    model.input_price_per_million = float(raw_price) * 1_000_000
            if model.output_price_per_million is None:
                raw_price = price_info.get("output_cost_per_token")
                if isinstance(raw_price, (int, float)):
                    model.output_price_per_million = float(raw_price) * 1_000_000
        assign_provider_relative_tiers(models, pins=self._user_overrides)
        now = int(time.time())
        stale_until = now + self._stale_ttl_seconds
        model_ids = [model.model_id for model in models]

        with self._db.conn() as conn:
            conn.executemany(
                """
                INSERT INTO model_catalog
                    (model_id, provider, tier, cost, last_seen, source, stale_until, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, model_id) DO UPDATE SET
                    tier = excluded.tier,
                    cost = excluded.cost,
                    last_seen = excluded.last_seen,
                    source = excluded.source,
                    stale_until = excluded.stale_until,
                    metadata_json = excluded.metadata_json
                """,
                [
                    (
                        model.model_id,
                        provider,
                        model.tier or "unknown",
                        model.input_price_per_million,
                        now,
                        model.discovery_source,
                        stale_until,
                        json.dumps({
                            key: value
                            for key, value in model.to_dict().items()
                            if key not in {"model_id", "tier", "discovery_source"}
                        }, sort_keys=True),
                    )
                    for model in models
                ],
            )

            if model_ids:
                placeholders = ", ".join("?" for _ in model_ids)
                conn.execute(
                    f"""
                    DELETE FROM model_catalog
                    WHERE provider = ?
                      AND model_id NOT IN ({placeholders})
                    """,
                    (provider, *model_ids),
                )
            else:
                conn.execute(
                    "DELETE FROM model_catalog WHERE provider = ?",
                    (provider,),
                )

        state = self._provider_state_for(provider)
        state["failed_refresh_count"] = 0
        state["last_failure_ts"] = 0.0
        state["cooldown_until"] = 0.0

    def refresh_all(
        self,
        registry: ProviderRegistry,
        *,
        timeout: int = 15,
    ) -> dict[str, list[str]]:
        now = time.time()
        results = {
            "refreshed": [],
            "skipped": [],
            "cooldown": [],
            "failed": [],
        }

        for provider in registry.available_providers:
            count, stale_until = self._catalog_snapshot(provider.name)
            state = self._provider_state_for(provider.name)

            if float(state["cooldown_until"]) > now:
                results["cooldown"].append(provider.name)
                continue

            if count > 0 and stale_until > now:
                self._project_provider_catalog(provider)
                results["skipped"].append(provider.name)
                continue

            adapter = getattr(provider, "model_discovery_adapter", None)
            if adapter is not None:
                try:
                    live = adapter.discover_live()
                    if live is not None and live.successful and live.models:
                        self.refresh(
                            provider.name,
                            [model.to_dict() for model in live.models],
                            source=live.source,
                        )
                        self._project_provider_catalog(provider)
                        self._mark_provider_ready(provider, checked_at=now)
                        results["refreshed"].append(provider.name)
                        continue
                except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired, OSError):
                    pass
                try:
                    cached = adapter.discover_official_cache()
                    if (
                        cached is not None
                        and cached.models
                        and now - cached.discovered_at <= self._stale_ttl_seconds
                    ):
                        self.refresh(
                            provider.name,
                            [model.to_dict() for model in cached.models],
                            source=cached.source,
                        )
                        self._project_provider_catalog(provider)
                        self._mark_provider_ready(provider, checked_at=now)
                        results["refreshed"].append(provider.name)
                        continue
                except (OSError, ValueError, json.JSONDecodeError):
                    pass

            # Official CLI-owned caches sit below live discovery and above LKG.
            if provider.name == "codex" and not self._command_for_provider(provider):
                cached = load_codex_cache()
                if (
                    cached is not None
                    and cached.models
                    and now - cached.discovered_at <= self._stale_ttl_seconds
                ):
                    self.refresh(
                        provider.name,
                        [model.to_dict() for model in cached.models],
                        source=cached.source,
                    )
                    self._project_provider_catalog(provider)
                    self._mark_provider_ready(provider, checked_at=now)
                    results["refreshed"].append(provider.name)
                    continue

            command = self._command_for_provider(provider)
            if not command:
                configured_models = [
                    {
                        "model_id": model_id,
                        "provider_metadata": {"tier": tier},
                    }
                    for tier, model_id in provider.tier_models.items()
                    if isinstance(model_id, str) and model_id
                ]
                if bootstrap_models(provider.name):
                    self.refresh(provider.name, [], successful=False)
                else:
                    self.refresh(
                        provider.name,
                        configured_models,
                        source="operator_config",
                        successful=bool(configured_models),
                    )
                catalog = self.get(provider.name)
                self._project_provider_catalog(provider)
                self._mark_provider_ready(provider, checked_at=now)
                results["refreshed"].append(provider.name)
                continue

            had_catalog = count > 0

            try:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if completed.returncode != 0:
                    raise RuntimeError(
                        f"{provider.display_name}: model discovery exited {completed.returncode}"
                    )
                parser = provider.model_discovery_parser or self._parse_model_discovery_output
                discovered = parser(provider, completed.stdout)
                if isinstance(discovered, dict):
                    if any(tier in discovered for tier in ("low", "medium", "high")):
                        discovered = [
                            {"model_id": model_id, "provider_metadata": {"tier": tier}}
                            for tier in ("low", "medium", "high")
                            for model_id in discovered.get(tier, [])
                            if isinstance(model_id, str)
                        ]
                    else:
                        discovered = discovered.get(provider.name, [])
                self.refresh(provider.name, discovered, source="live_provider_catalog")
                self._project_provider_catalog(provider)
                self._mark_provider_ready(provider, checked_at=now)
                results["refreshed"].append(provider.name)
            except (
                FileNotFoundError,
                RuntimeError,
                subprocess.TimeoutExpired,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                previous_cooldown = float(state["cooldown_until"])
                state = self.record_refresh_failure(provider.name)
                if had_catalog and provider.readiness.routeable:
                    provider.readiness = ProviderReadiness(
                        routeable=True,
                        reason=DetectReason.STALE_BUT_ROUTEABLE,
                        last_checked=now,
                    )
                    provider.detect_reason = provider.readiness.reason
                elif provider.readiness.reason is DetectReason.READY:
                    provider.readiness = ProviderReadiness(
                        routeable=False,
                        reason=DetectReason.CATALOG_PENDING,
                        last_checked=now,
                    )
                    provider.detect_reason = provider.readiness.reason

                if float(state["cooldown_until"]) > previous_cooldown:
                    log.warning(
                        "%s: catalog refresh entering cooldown after repeated failure: %s",
                        provider.display_name,
                        exc,
                    )
                results["failed"].append(provider.name)

        return results

    def merge_with_static_fallback(
        self,
        provider: str,
        discovered: list[dict[str, Any] | str],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for entry in discovered:
            try:
                merged.append(_normalize_model_entry(provider, entry))
            except (TypeError, ValueError) as exc:
                log.warning("%s: skipping malformed model discovery entry: %s", provider, exc)
        return merged if merged else [model.to_dict() for model in bootstrap_models(provider)]

    def is_auto_routeable(self, model_id: str) -> bool:
        ranked = rank_models_with_price_data([{"model_id": model_id}])
        return bool(ranked and ranked[0]["auto_routeable"])
