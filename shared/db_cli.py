from __future__ import annotations

"""CLI utility for Threnody database maintenance."""

import argparse
import sys
from pathlib import Path

from .db import Database


def _backup_count(db: Database) -> int | str:
    """Number of ``.bak.*`` restore candidates beside the live DB."""
    import glob as _glob

    db_path = getattr(db, "_db_path", None)
    if db_path is None:
        return "unknown"  # RemoteDatabase / stub — no local path to scan.
    try:
        return len(_glob.glob(str(db_path) + ".bak.*"))
    except OSError:
        return "unknown"


def _newest_backup_age_hours(db: Database) -> str:
    """Age of the newest restore candidate, or a warning when there is none."""
    age_fn = getattr(db, "_newest_backup_age_s", None)
    if not callable(age_fn):
        return "unknown"
    try:
        age_s = age_fn()
    except Exception:
        return "unknown"
    if age_s is None:
        return (
            "none — a corruption would quarantine cache.db and reset every "
            "learning table (run: threnody db backup)"
        )
    return f"{age_s / 3600:.1f}"


def cmd_check(args):
    """Run integrity check on the database."""
    db_path = args.db
    db = Database(db_path)
    try:
        db._check_integrity_and_recover()
        rebuilt = db.rebuild_memory_fts()
        print(f"integrity_ok: {db.last_integrity_ok}")
        print(f"db_path: {db_path}")
        print(f"memory_fts_rows: {rebuilt}")
        # Report the restore candidate on *disk*, not `last_backup_ts` — that only
        # records a backup this process took, so it reads "never" on every healthy
        # install and made the one command an operator runs to ask "am I protected?"
        # claim there was no backup while several sat next to the DB. Mirrors
        # status._load_backup_health.
        print(f"backups_present: {_backup_count(db)}")
        print(f"newest_backup_age_hours: {_newest_backup_age_hours(db)}")
        if not db.last_integrity_ok:
            sys.exit(1)
    finally:
        db.close()


def cmd_repair(args):
    """Repair the database."""
    db_path = args.db
    db = Database(db_path)
    try:
        db._recover_db()
        print("action: repair")
        print("result: ok")
    except Exception:
        print("result: failed")
        sys.exit(1)
    finally:
        db.close()


def cmd_backup(args):
    """Backup the database."""
    db_path = args.db
    db = Database(db_path)
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
    source = Path(args.source)
    destination = Path(args.out) if args.out else source.with_suffix(source.suffix + ".salvaged")
    db = Database(args.db)
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
        db = Database(args.db)
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
        db = Database(args.db)
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

    db = Database(args.db)
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
    db_path = args.db
    keep = args.keep
    db = Database(db_path)
    try:
        db._prune_old_backups(keep=keep)
        print("action: prune")
        print(f"keep: {keep}")
        print("result: ok")
    finally:
        db.close()


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

    prune_parser = subparsers.add_parser(
        "prune", parents=[common], help="Prune old backups"
    )
    prune_parser.add_argument("--keep", type=int, default=3, help="Backups to keep")

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
    elif args.subcmd == "prune":
        cmd_prune(args)
    elif args.subcmd == "salvage":
        cmd_salvage(args)
    elif args.subcmd == "learn":
        cmd_learn(args)


if __name__ == "__main__":
    main()