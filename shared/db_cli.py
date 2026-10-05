from __future__ import annotations

"""CLI utility for Threnody database maintenance.

Every command opens the database through ``db_client.open_database`` — the
single-writer daemon when it is enabled — rather than constructing a direct
``Database``. A direct opener mmaps its own ``-shm`` next to the daemon's, which
is the multi-writer hazard the daemon exists to remove, and a direct ``repair``
would have to wait out the daemon's own hold on the file.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from .config import DB_BACKUP_KEEP
from .db import RECOVERY_DECLINED_IN_USE

# Outcomes of Database.repair() that mean the files were (re)written.
_REPAIR_ACTED = frozenset({"salvaged", "restored", "quarantined", "recreated"})


def _open(db_path: Path):
    from .db_client import open_database

    return open_database(Path(db_path))


def _backup_count(db) -> int | str:
    """Number of ``.bak.*`` restore candidates beside the live DB."""
    import glob as _glob

    db_path = getattr(db, "_db_path", None)
    if db_path is None:
        return "unknown"  # stub — no local path to scan.
    try:
        return len(_glob.glob(str(db_path) + ".bak.*"))
    except OSError:
        return "unknown"


def _newest_backup_age_hours(db) -> str:
    """Age of the newest restore candidate, or a warning when there is none."""
    try:
        age_s = db.newest_backup_age_s()
    except Exception:
        return "unknown"
    if age_s is None:
        return (
            "none — a corruption would quarantine cache.db and reset every "
            "learning table (run: threnody db backup)"
        )
    return f"{age_s / 3600:.1f}"


def _print_repair(result: str) -> int:
    """Print a repair outcome; returns the exit code (non-zero unless it acted)."""
    print("action: repair")
    if result in _REPAIR_ACTED:
        print(f"outcome: {result}")
        print("result: ok")
        return 0
    # Typically RECOVERY_DECLINED_IN_USE: another process held the database,
    # so nothing was touched. Saying "ok" here hid exactly that.
    print(f"result: {result}")
    if str(result).startswith(RECOVERY_DECLINED_IN_USE):
        print("hint: stop other Threnody sessions (or run `threnody db status` to see who) and retry")
    return 1


def cmd_check(args):
    """Integrity check; repairs when the check fails (as before)."""
    db = _open(args.db)
    try:
        verdict = db.check().get("integrity_ok")
        rebuilt = db.rebuild_memory_fts()
        print(f"integrity_ok: {verdict}")
        print(f"db_path: {args.db}")
        print(f"memory_fts_rows: {rebuilt}")
        # Report the restore candidate on *disk*, not `last_backup_ts` — that only
        # records a backup this process took, so it reads "never" on every healthy
        # install and made the one command an operator runs to ask "am I protected?"
        # claim there was no backup while several sat next to the DB. Mirrors
        # status._load_backup_health.
        print(f"backups_present: {_backup_count(db)}")
        print(f"newest_backup_age_hours: {_newest_backup_age_hours(db)}")
        if verdict is False:
            code = _print_repair(db.repair())
            if code:
                sys.exit(code)
    finally:
        db.close()


def cmd_repair(args):
    """Repair the database (declines, and exits 1, while it is in use)."""
    try:
        db = _open(args.db)
    except Exception as exc:
        print(f"result: failed ({exc})")
        sys.exit(1)
    try:
        code = _print_repair(db.repair())
    except Exception as exc:
        print(f"result: failed ({exc})")
        code = 1
    finally:
        db.close()
    if code:
        sys.exit(code)


def cmd_backup(args):
    """Backup the database."""
    db = _open(args.db)
    try:
        bp = db.backup_db()
        print(f"backup_path: {bp}")
        print(f"last_backup_ts: {db.last_backup_ts}")
        if bp is None:
            sys.exit(1)
    finally:
        db.close()


def cmd_salvage(args):
    """Recover readable rows out of a quarantined ``.corrupt.*`` image.

    Recovery is automatic and in-place on open; this exists for the quarantines
    already sitting on disk from before that existed (nine on this install), which
    nothing else could ever read again.
    """
    source = Path(args.source).resolve()
    destination = (
        Path(args.out).resolve() if args.out else source.with_suffix(source.suffix + ".salvaged")
    )
    db = _open(args.db)
    try:
        rows = db.salvage_file(source, destination)
        print(f"source: {source}")
        print(f"rows_recovered: {rows}")
        if rows <= 0:
            print("result: nothing recoverable")
            sys.exit(1)
        print(f"out: {destination}")
        print("result: ok")
    finally:
        db.close()


def cmd_learn(args):
    """Journal-backed learning maintenance: status / rebuild / import."""
    from . import learning_journal

    if args.action == "status":
        stats = learning_journal.stats()
        print(f"journal_root: {stats['root']}")
        print(f"shards: {len(stats['shards'])}")
        print(f"events: {stats['events']}")
        print(f"bytes: {stats['bytes']}")
        for kind, count in sorted(stats["by_kind"].items()):
            print(f"  {kind}: {count}")
        db = _open(args.db)
        try:
            for table in (
                "model_quality_events",
                "review_tier_bias",
                "review_scans",
                "review_findings",
                "hybrid_tier_bias",
                "telemetry",
            ):
                try:
                    with db.conn() as conn:
                        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception:
                    n = "n/a"
                print(f"db.{table}: {n}")
        finally:
            db.close()
        return

    if args.action == "rebuild":
        db = _open(args.db)
        try:
            counts = db.replay_learning_journal(rebuild=True)
            print("action: rebuild")
            for kind, count in sorted(counts.items()):
                print(f"  {kind}: {count}")
            print("result: ok")
        finally:
            db.close()
        return

    # import <run_id>
    from .host_learning import import_run_log
    from . import run_log

    db = _open(args.db)
    try:
        meta = run_log.read_run_meta(args.run_id) or {}
        outcome = str(meta.get("outcome") or args.outcome or "accepted")
        result = import_run_log(db, args.run_id, outcome=outcome)
        print(f"action: import\nrun_id: {args.run_id}\noutcome: {outcome}")
        print(f"result: {result}")
    finally:
        db.close()


def cmd_prune(args):
    """Prune old backups."""
    keep = args.keep
    db = _open(args.db)
    try:
        remaining = db.prune_backups(keep=keep)
        print("action: prune")
        print(f"keep: {keep}")
        print(f"remaining: {remaining}")
        print("result: ok")
    finally:
        db.close()


def _fmt_age(seconds: object) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if s >= 3600:
        return f"{s / 3600:.1f}h"
    if s >= 60:
        return f"{s / 60:.1f}m"
    return f"{s:.1f}s"


def cmd_status(args):
    """What serves the database right now. Never spawns a daemon."""
    from .config import TGsConfig
    from .db_client import probe_daemon

    socket_path = None
    try:
        cfg = TGsConfig.from_yaml()
        configured = getattr(cfg.db_daemon, "socket_path", "") or ""
        if configured and Path(cfg.db_path).resolve() == Path(args.db).resolve():
            socket_path = configured
    except Exception:
        cfg = None
    info = probe_daemon(args.db, socket_path=socket_path)
    if args.json:
        print(json.dumps(info, indent=2, default=str))
        return
    print(f"db_path: {info['db_path']}")
    print(f"socket: {info['socket_path']}")
    if not info.get("running"):
        print("daemon: not running")
        print(f"election_lock: {info.get('election_lock')}"
              + (" (a daemon is starting, or one is wedged without its socket)"
                 if info.get("election_lock") == "held" else ""))
        print(f"access_lock: {info.get('access_lock')}")
        return
    if info.get("legacy"):
        print(f"daemon: running (pid {info.get('pid')}), LEGACY protocol 1 — pre-upgrade code; "
              "stop it (SIGTERM) so the next client spawns a current one")
        return
    st = info.get("status") or {}
    print(f"daemon: running (pid {st.get('pid')}, version {st.get('version')}, "
          f"protocol {st.get('protocol')})")
    print(f"uptime: {_fmt_age(st.get('uptime_s'))} (max lifetime {_fmt_age(st.get('max_lifetime_s'))})")
    print(f"idle_for: {_fmt_age(st.get('idle_for_s'))} (idle timeout {_fmt_age(st.get('idle_timeout_s'))})")
    req = st.get("requests") or {}
    print(f"requests: fg={req.get('fg')} bg={req.get('bg')}")
    print(f"sessions_open: {st.get('sessions_open')}  clients: {st.get('clients_connected')}  "
          f"legacy_clients: {st.get('legacy_clients')}")
    for path, ids in (st.get("files") or {}).items():
        recorded, current = ids.get("recorded"), ids.get("current")
        flag = "" if recorded == current or recorded is None else "  <-- CHANGED"
        print(f"file {Path(path).name}: recorded={recorded} current={current}{flag}")
    err = st.get("last_error")
    if err:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(err.get("ts", 0)))
        print(f"last_error: {err.get('code')} at {when}: {err.get('message')}")
    else:
        print("last_error: none")
    ckpt = st.get("last_checkpoint")
    if ckpt:
        when = time.strftime("%H:%M:%S", time.localtime(ckpt.get("ts", 0)))
        print(f"last_checkpoint: {ckpt.get('mode')} at {when} → {ckpt.get('result')}")
    print(f"access_lock: {(st.get('access_lock') or {}).get('access_lock')}")
    if st.get("paused"):
        print("state: PAUSED (maintenance in progress)")
    if st.get("stopping"):
        print(f"state: stopping ({st.get('stopping')})")


def main():
    """Main CLI entry point."""
    default_db = Path.home() / ".local/lib/threnody/cache.db"

    parser = argparse.ArgumentParser(description="Threnody DB maintenance CLI")
    parser.add_argument("--db", type=Path, default=default_db, help="Path to cache.db")

    # `--db` is documented as `threnody db check [--db PATH]`, i.e. *after* the
    # subcommand, and the shell wrapper forwards it that way — but it was only
    # defined on the top-level parser, so every documented invocation exited with
    # "unrecognized arguments". Declaring it on a shared parent accepts both
    # positions.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=None, help="Path to cache.db")

    subparsers = parser.add_subparsers(dest="subcmd")

    subparsers.add_parser("check", parents=[common], help="Run integrity check")
    subparsers.add_parser("repair", parents=[common], help="Repair the database")
    subparsers.add_parser("backup", parents=[common], help="Backup the database")
    status_parser = subparsers.add_parser(
        "status", parents=[common], help="Show what serves the database (never spawns)"
    )
    status_parser.add_argument("--json", action="store_true", help="Machine-readable output")

    prune_parser = subparsers.add_parser(
        "prune", parents=[common], help="Prune old backups"
    )
    # Defaults to the retention policy itself (DB_BACKUP_KEEP); a smaller
    # literal here silently cut the documented 7-day restore window to hours.
    prune_parser.add_argument(
        "--keep", type=int, default=DB_BACKUP_KEEP, help="Backups to keep"
    )

    salvage_parser = subparsers.add_parser(
        "salvage", parents=[common],
        help="Recover rows from a quarantined .corrupt.* image",
    )
    salvage_parser.add_argument("source", help="Path to the .corrupt.* file")
    salvage_parser.add_argument("--out", default=None, help="Destination DB path")

    learn_parser = subparsers.add_parser(
        "learn", help="Journal-backed learning maintenance"
    )
    learn_sub = learn_parser.add_subparsers(dest="action", required=True)
    learn_sub.add_parser("status", parents=[common], help="Journal + table summary")
    learn_sub.add_parser("rebuild", parents=[common], help="Replay journal into DB")
    import_parser = learn_sub.add_parser(
        "import", parents=[common], help="Import one run_log by id"
    )
    import_parser.add_argument("run_id")
    import_parser.add_argument("--outcome", default=None)

    args = parser.parse_args()
    if getattr(args, "db", None) is None:
        args.db = default_db

    if not args.subcmd:
        parser.print_help()
        sys.exit(0)

    if args.subcmd == "check":
        cmd_check(args)
    elif args.subcmd == "repair":
        cmd_repair(args)
    elif args.subcmd == "backup":
        cmd_backup(args)
    elif args.subcmd == "status":
        cmd_status(args)
    elif args.subcmd == "prune":
        cmd_prune(args)
    elif args.subcmd == "salvage":
        cmd_salvage(args)
    elif args.subcmd == "learn":
        cmd_learn(args)


if __name__ == "__main__":
    main()
