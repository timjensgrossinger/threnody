#!/usr/bin/env python3
"""
Provider health diagnostics and bounded self-repair.

Entry points:
  threnody doctor            — diagnose all providers, exit 1 if any QUARANTINED
  threnody doctor --repair   — diagnose + run bounded self-repair
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

log = logging.getLogger(__name__)

_ROUTER_DIR = Path(__file__).parent.parent
_PROVIDERS_JSON = _ROUTER_DIR / "providers.json"
_STALE_PROVIDERS_DAYS = 7
_BACKUP_INTERVAL_S = 86400.0  # 24 h

_SUGGEST: dict[str, dict[str, str]] = {
    "github-copilot": {
        "auth_expired":    "gh auth login",
        "binary_missing":  "run install.sh",
        "quota_exceeded":  "check GitHub Copilot subscription",
    },
    "claude-code": {
        "auth_expired":    "claude login",
        "binary_missing":  "run install.sh",
        "quota_exceeded":  "check Anthropic billing",
    },
    "gemini-cli": {
        "auth_expired":    "gemini auth login  (or set GEMINI_API_KEY)",
        "binary_missing":  "run install.sh",
        "quota_exceeded":  "check Google AI quota",
    },
}
_DEFAULT_SUGGEST = {
    "auth_expired":   "re-authenticate the provider",
    "binary_missing": "run install.sh",
    "quota_exceeded": "check provider subscription/billing",
}


def _load_providers() -> list[dict]:
    try:
        data = json.loads(_PROVIDERS_JSON.read_text())
        return [p for p in data.get("providers", []) if p.get("available")]
    except Exception:
        return []


def _probe_provider(provider_name: str) -> tuple[str, bool]:
    from .resilience import AuthProbe
    ok = AuthProbe.check(provider_name)
    return provider_name, ok


def _suggest_fix(provider_name: str, category: str | None) -> str:
    cat = (category or "").lower()
    table = _SUGGEST.get(provider_name, _DEFAULT_SUGGEST)
    for key, fix in table.items():
        if key in cat:
            return fix
    if cat in _DEFAULT_SUGGEST:
        return _DEFAULT_SUGGEST[cat]
    return "—"


def diagnose(db, repair: bool = False, dry_run: bool = False) -> int:
    """Run diagnostics. Returns 0 if all healthy, 1 if any QUARANTINED."""
    providers = _load_providers()
    provider_names = [p.get("name", "") for p in providers if p.get("name")]

    # Parallel auth probes
    probe_results: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_probe_provider, name): name for name in provider_names}
        for fut in as_completed(futures):
            try:
                name, ok = fut.result()
                probe_results[name] = ok
            except Exception:
                probe_results[futures[fut]] = False

    # Health state from DB
    health_rows: dict[str, dict] = {}
    if db is not None:
        try:
            for row in db.iter_provider_health():
                pid = row.get("provider_id")
                if pid:
                    health_rows[pid] = row
        except Exception:
            pass

    # DB integrity check
    db_ok = True
    if db is not None:
        try:
            db._check_integrity_and_recover()
            db_ok = db.last_integrity_ok
        except Exception:
            db_ok = False

    # providers.json staleness
    providers_stale = False
    providers_mtime = 0.0
    if _PROVIDERS_JSON.exists():
        providers_mtime = _PROVIDERS_JSON.stat().st_mtime
        age_days = (time.time() - providers_mtime) / 86400.0
        providers_stale = age_days > _STALE_PROVIDERS_DAYS

    # Print table
    col_w = (24, 14, 18, 12, 38)
    header = f"{'PROVIDER':<{col_w[0]}}  {'STATE':<{col_w[1]}}  {'LAST FAILURE':<{col_w[2]}}  {'AUTH PROBE':<{col_w[2]}}  {'SUGGESTED FIX'}"
    print(header)
    print("-" * sum(col_w) + "-" * 8)

    any_quarantined = False
    for p in providers:
        name = p.get("name", "")
        if not name:
            continue
        display = p.get("display_name", name)
        row = health_rows.get(name, {})
        state = row.get("state", "HEALTHY")
        if state == "QUARANTINED":
            any_quarantined = True
        last_cat = row.get("last_failure_category") or "—"
        last_ts = row.get("last_failure_ts")
        last_str = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(last_ts))
            if last_ts else "—"
        )
        probe_ok = probe_results.get(name, True)
        probe_str = "ok" if probe_ok else "FAIL"
        fix = _suggest_fix(name, last_cat) if (state == "QUARANTINED" or not probe_ok) else "—"

        state_marker = {
            "HEALTHY": " ",
            "DEGRADED": "~",
            "QUARANTINED": "!",
            "PROBING": "?",
        }.get(state, " ")

        print(
            f"{state_marker}{display:<{col_w[0]-1}}  "
            f"{state:<{col_w[1]}}  "
            f"{last_str:<{col_w[2]}}  "
            f"{probe_str:<{col_w[2]}}  "
            f"{fix}"
        )

    print()

    if not db_ok:
        print("DB: integrity check FAILED — run: threnody db repair")
    elif db is not None:
        print("DB: ok")

    if providers_stale:
        import datetime
        age = datetime.datetime.fromtimestamp(providers_mtime)
        print(f"providers.json: STALE (last updated {age.strftime('%Y-%m-%d')}) — run: ./install.sh")
    else:
        print("providers.json: ok")

    if repair:
        print()
        run_self_repair(db, dry_run=dry_run)

    return 1 if (any_quarantined or not db_ok) else 0


def run_self_repair(db, dry_run: bool = False) -> None:
    """Bounded self-repair — safe, idempotent, non-interactive."""
    tag = "[dry-run] " if dry_run else ""

    # 1. DB backup if overdue
    if db is not None:
        try:
            last_backup = getattr(db, "last_backup_ts", None)
            overdue = last_backup is None or (time.time() - last_backup) > _BACKUP_INTERVAL_S
            if overdue:
                if not dry_run:
                    bp = db.backup_db()
                    print(f"{tag}repair: db backup → {bp}")
                else:
                    print(f"{tag}repair: db backup (would run)")
            else:
                print(f"{tag}repair: db backup not needed")
        except Exception as exc:
            print(f"{tag}repair: db backup failed — {exc}")

    # 2. providers.json staleness
    stale = False
    if _PROVIDERS_JSON.exists():
        age_s = time.time() - _PROVIDERS_JSON.stat().st_mtime
        stale = age_s > _STALE_PROVIDERS_DAYS * 86400.0
    if stale:
        print(f"{tag}repair: providers.json is stale — re-run install.sh to refresh")
    else:
        print(f"{tag}repair: providers.json ok")

    # 3. DB integrity — auto-recover if broken
    if db is not None:
        try:
            db._check_integrity_and_recover()
            if not db.last_integrity_ok:
                if not dry_run:
                    db._recover_db()
                    print(f"{tag}repair: db integrity failed — recovery attempted")
                else:
                    print(f"{tag}repair: db integrity failed (would recover)")
            else:
                print(f"{tag}repair: db integrity ok")
        except Exception as exc:
            print(f"{tag}repair: db integrity check error — {exc}")


def main(argv: list[str] | None = None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Threnody provider health diagnostics")
    parser.add_argument("--repair", action="store_true", help="Run bounded self-repair after diagnosis")
    parser.add_argument("--dry-run", action="store_true", help="Show repair actions without applying")
    parser.add_argument("--db", type=Path, default=None, help="Path to cache.db (optional)")
    args = parser.parse_args(argv)

    db = None
    db_path = args.db or (Path.home() / ".local/lib/threnody/cache.db")
    if db_path.exists():
        try:
            from .db import Database
            db = Database(str(db_path))
        except Exception as exc:
            print(f"warning: could not open DB — {exc}", file=sys.stderr)

    try:
        exit_code = diagnose(db, repair=args.repair, dry_run=args.dry_run)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
