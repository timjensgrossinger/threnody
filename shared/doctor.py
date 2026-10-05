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
}
_DEFAULT_SUGGEST = {
    "auth_expired":   "re-authenticate the provider",
    "binary_missing": "run install.sh",
    "quota_exceeded": "check provider subscription/billing",
}

# Suggested fixes for shared-WAL DB contention/corruption.
_DB_SUGGEST = {
    "db_locked":  "another Threnody MCP server is initializing; retry — it self-heals, or run `threnody doctor --repair`",
    "db_corrupt": "run `threnody db repair` (restores newest valid backup); check quarantined cache.db.corrupt.* files",
}
_QUARANTINE_KEEP = 3


def _db_contention_report(db) -> dict:
    """Inspect the DB dir for lock/quarantine evidence (no mutation)."""
    report = {"corrupt_files": [], "lock_present": False, "db_path": None}
    db_path = getattr(db, "_db_path", None)
    if db_path is None:
        return report
    import glob as _glob
    report["db_path"] = str(db_path)
    report["corrupt_files"] = sorted(_glob.glob(str(db_path) + ".corrupt.*"))
    lock_path = getattr(db, "_lock_path", None)
    report["lock_present"] = bool(lock_path and Path(lock_path).exists())
    return report


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


def _lsof(args: list[str]) -> str | None:
    """``lsof`` output, or None when lsof is not installed / fails."""
    import shutil
    import subprocess

    binary = shutil.which("lsof")
    if not binary:
        return None
    try:
        return subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.SubprocessError):
        log.debug("lsof %s failed", args, exc_info=True)
        return None


def _deleted_db_mappings(pid: int, db_name: str) -> list[str] | None:
    """Unlinked (``+L1``) cache.db* files the daemon still has open, or None if unknown.

    A daemon still mapping a deleted ``-shm``/``-wal`` is the stale-mapping
    state behind ``disk I/O error``; its watchdog should restart it within
    seconds, so seeing this persist means the watchdog is not running.
    """
    out = _lsof(["-n", "-P", "+L1", "-p", str(pid)])
    if out is None:
        return None
    return [line for line in out.splitlines() if db_name in line]


def _pids_with_db_open(db_path: Path) -> dict[int, str] | None:
    """pid → command for every process holding cache.db / -wal / -shm open."""
    paths = [str(p) for p in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")) if p.exists()]
    if not paths:
        return {}
    out = _lsof(["-n", "-P", "-F", "pc", *paths])
    if out is None:
        return None
    pids: dict[int, str] = {}
    current: int | None = None
    for line in out.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current = int(line[1:])
            pids.setdefault(current, "")
        elif line.startswith("c") and current is not None:
            pids[current] = line[1:]
    return pids


def _daemon_health_check(db) -> list[str]:
    """Report the single-writer daemon's state; returns the warnings it printed."""
    warnings: list[str] = []
    try:
        from .config import TGsConfig
        cfg = TGsConfig.from_yaml()
    except Exception:
        return warnings
    daemon_cfg = getattr(cfg, "db_daemon", None)
    if not daemon_cfg or not getattr(daemon_cfg, "enabled", False):
        print("DB daemon: disabled (every process opens the DB directly)")
        return warnings
    db_path = Path(str(getattr(db, "_db_path", None) or getattr(cfg, "db_path", "")))
    from .db_client import probe_daemon

    configured = getattr(daemon_cfg, "socket_path", "") or None
    info = probe_daemon(db_path, socket_path=configured)

    def warn(msg: str) -> None:
        warnings.append(msg)
        print(f"DB daemon: WARNING — {msg}")

    pid = info.get("pid")
    if not info.get("running"):
        print(
            f"DB daemon: ENABLED, not running ({info['socket_path']}) — will spawn on next "
            f"foreground use; election lock {info.get('election_lock')}, "
            f"access lock {info.get('access_lock')}"
        )
        if info.get("election_lock") == "held":
            warn("election lock is held but nothing answers on the socket "
                 "(a daemon starting, or one wedged without its socket)")
    elif info.get("legacy"):
        warn(f"pid {pid} speaks the legacy protocol (pre-upgrade code) — stop it with "
             "SIGTERM so the next client spawns a current daemon")
    else:
        st = info.get("status") or {}
        print(
            f"DB daemon: running (pid {pid}, version {st.get('version')}, protocol "
            f"{st.get('protocol')}, up {float(st.get('uptime_s') or 0) / 3600:.1f}h, "
            f"{st.get('sessions_open')} open session(s))"
        )
        if st.get("legacy_clients"):
            warn(f"{st['legacy_clients']} connected client(s) speak the legacy protocol — "
                 "restart those Claude Code / MCP sessions to load current code")
        for path, ids in (st.get("files") or {}).items():
            if ids.get("recorded") and ids.get("recorded") != ids.get("current"):
                warn(f"{Path(path).name} changed underneath the daemon "
                     "(it should restart itself within seconds)")
        err = st.get("last_error")
        if err:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(err.get("ts", 0)))
            print(f"DB daemon: last error {err.get('code')} at {when}: {err.get('message')}")
    if pid:
        deleted = _deleted_db_mappings(int(pid), db_path.name)
        if deleted is None:
            print("DB daemon: lsof not available — skipped the deleted-mapping check")
        elif deleted:
            warn(f"pid {pid} still has {len(deleted)} deleted {db_path.name}* file(s) open "
                 "(stale mapping → disk I/O errors); restart it")
    holders = _pids_with_db_open(db_path)
    if holders is not None and len(holders) > 1:
        listing = ", ".join(f"{p} ({c or '?'})" for p, c in sorted(holders.items()))
        warn(f"{len(holders)} processes have {db_path.name} open directly: {listing} — "
             "with the daemon on, only the daemon should")
    return warnings


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

    # DB integrity check — diagnosis only; `--repair` is what may fix it.
    db_ok = True
    if db is not None:
        try:
            db_ok = db.check().get("integrity_ok") is not False
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
        print(f"  → {_DB_SUGGEST['db_corrupt']}")
    elif db is not None:
        print("DB: ok")

    # DB contention / quarantine evidence (shared WAL across concurrent MCP servers)
    if db is not None:
        rep = _db_contention_report(db)
        corrupt_files = rep.get("corrupt_files") or []
        if corrupt_files:
            print(
                f"DB: {len(corrupt_files)} quarantined corrupt DB file(s) present "
                f"(e.g. {os.path.basename(corrupt_files[-1])})"
            )
            print(f"  → {_DB_SUGGEST['db_corrupt']}")
        if rep.get("lock_present"):
            print("DB: recovery lock file present (normal during concurrent init)")
        snapshot_fn = getattr(db, "db_health_snapshot", None)
        if callable(snapshot_fn):
            try:
                snapshot = snapshot_fn()
            except Exception:
                snapshot = {}
            detected_ts = (snapshot or {}).get("corruption_detected_ts")
            if detected_ts:
                import datetime as _dt

                when = _dt.datetime.fromtimestamp(detected_ts).strftime("%Y-%m-%d %H:%M")
                recovered = " (recovered)" if snapshot.get("recovered_this_session") else ""
                print(f"DB: corruption detected this session at {when}{recovered}")

    # Single-writer daemon health (only when opted in)
    _daemon_health_check(db)

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

    # 3. DB integrity — recover if broken (gated by the access lock: declines
    #    while another process has the database open)
    if db is not None:
        try:
            if db.check().get("integrity_ok") is False:
                if not dry_run:
                    outcome = db.repair()
                    print(f"{tag}repair: db integrity failed — recovery: {outcome}")
                else:
                    print(f"{tag}repair: db integrity failed (would recover)")
            else:
                print(f"{tag}repair: db integrity ok")
        except Exception as exc:
            print(f"{tag}repair: db integrity check error — {exc}")

    # 4. Prune old quarantined corrupt DBs (keep the most recent few for forensics)
    if db is not None:
        try:
            import glob as _glob
            db_path = getattr(db, "_db_path", None)
            quarantines = sorted(_glob.glob(str(db_path) + ".corrupt.*")) if db_path else []
            excess = quarantines[:-_QUARANTINE_KEEP] if len(quarantines) > _QUARANTINE_KEEP else []
            if excess:
                if not dry_run:
                    for path in excess:
                        try:
                            os.unlink(path)
                        except Exception:
                            log.debug("could not prune quarantine %s", path, exc_info=True)
                    print(f"{tag}repair: pruned {len(excess)} old quarantined corrupt DB(s)")
                else:
                    print(f"{tag}repair: would prune {len(excess)} old quarantined corrupt DB(s)")
            else:
                print(f"{tag}repair: quarantine files ok ({len(quarantines)} kept)")
        except Exception as exc:
            print(f"{tag}repair: quarantine prune error — {exc}")


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
            from .db_client import open_database
            db = open_database(db_path)
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
