from __future__ import annotations

import logging
import threading
import time
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shared.db import Database

log = logging.getLogger(__name__)


def _tier_from_cost(_input_cost_per_token: float) -> str:
    """Deprecated compatibility helper; global prices no longer assign tiers."""
    return "unknown"


class CatalogRefresher:
    SOURCE_NAME = "litellm_refresh"
    PROVIDER_NAME = "litellm_global"
    LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
    TTL_SECONDS = 7 * 24 * 3600
    _FETCH_TIMEOUT = 10

    def __init__(self):
        self._refresh_lock = threading.Lock()
        self._refresh_in_flight: bool = False

    def refresh_if_stale(self, db: "Database") -> None:
        with self._refresh_lock:
            if self._refresh_in_flight:
                return
            now = int(time.time())
            try:
                with db.conn() as conn:
                    row = conn.execute(
                        "SELECT 1 FROM model_catalog WHERE source = ? AND stale_until > ? LIMIT 1",
                        (self.SOURCE_NAME, now),
                    ).fetchone()
            except Exception:
                log.debug("catalog_refresh: freshness check failed", exc_info=True)
                row = None
            if row is not None:
                return
            self._refresh_in_flight = True
        threading.Thread(target=self._run, args=(db,), daemon=True).start()

    def _run(self, db: "Database") -> None:
        try:
            self._do_refresh(db)
        except Exception:
            log.debug("catalog_refresh: _do_refresh raised unexpectedly", exc_info=True)
        finally:
            self._refresh_in_flight = False

    def _do_refresh(self, db: "Database") -> None:
        try:
            req = urllib.request.Request(
                self.LITELLM_URL,
                headers={"User-Agent": "Threnody/catalog-refresh"},
            )
            with urllib.request.urlopen(req, timeout=self._FETCH_TIMEOUT) as resp:
                raw_bytes = resp.read(10 * 1024 * 1024)
        except Exception:
            log.debug("catalog_refresh: HTTP fetch failed", exc_info=True)
            return

        try:
            import json
            data = json.loads(raw_bytes)
        except Exception:
            log.debug("catalog_refresh: JSON parse failed", exc_info=True)
            return
        if not isinstance(data, dict):
            log.debug("catalog_refresh: unexpected top-level type %s", type(data))
            return

        now = int(time.time())
        stale_until = now + self.TTL_SECONDS
        rows: list[tuple] = []
        for model_id, info in data.items():
            if not isinstance(info, dict):
                continue
            raw_cost = info.get("input_cost_per_token")
            if not isinstance(raw_cost, (int, float)):
                continue
            input_cost = float(raw_cost)
            cost_per_million = input_cost * 1_000_000
            rows.append((
                str(model_id),
                self.PROVIDER_NAME,
                "unknown",
                cost_per_million,
                now,
                self.SOURCE_NAME,
                stale_until,
                self.LITELLM_URL,
            ))

        if not rows:
            log.debug("catalog_refresh: no valid rows parsed from litellm response")
            return

        try:
            with db.conn() as conn:
                conn.executemany(
                    """
                    INSERT INTO model_catalog
                        (model_id, provider, tier, cost, last_seen, source, stale_until, url_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, model_id) DO UPDATE SET
                        tier       = excluded.tier,
                        cost       = excluded.cost,
                        last_seen  = excluded.last_seen,
                        source     = excluded.source,
                        stale_until = excluded.stale_until,
                        url_source = excluded.url_source
                    """,
                    rows,
                )
            log.debug("catalog_refresh: upserted %d rows from litellm", len(rows))
        except Exception:
            log.debug("catalog_refresh: DB upsert failed", exc_info=True)

    def get_tier_for_model(self, model_id: str, db: "Database") -> str | None:
        """Compatibility API; global price data no longer assigns route tiers."""
        now = int(time.time())
        try:
            with db.conn() as conn:
                row = conn.execute(
                    "SELECT tier FROM model_catalog WHERE source = ? AND model_id = ? AND stale_until > ?",
                    (self.SOURCE_NAME, model_id, now),
                ).fetchone()
        except Exception:
            log.debug("catalog_refresh: get_tier_for_model query failed", exc_info=True)
            return None
        if row is None:
            return None
        tier = str(row[0])
        return None if tier == "unknown" else tier


refresher = CatalogRefresher()
