from __future__ import annotations

"""CLI utility for Threnody database maintenance."""

import argparse
import sys
from pathlib import Path

from .db import Database


def cmd_check(args):
    """Run integrity check on the database."""
    db_path = args.db
    db = Database(db_path)
    try:
        db._check_integrity_and_recover()
        print(f"integrity_ok: {db.last_integrity_ok}")
        print(f"db_path: {db_path}")
        last_backup = db.last_backup_ts if db.last_backup_ts is not None else "never"
        print(f"last_backup: {last_backup}")
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

    subparsers = parser.add_subparsers(dest="subcmd")

    subparsers.add_parser("check", help="Run integrity check")
    subparsers.add_parser("repair", help="Repair the database")
    subparsers.add_parser("backup", help="Backup the database")

    prune_parser = subparsers.add_parser("prune", help="Prune old backups")
    prune_parser.add_argument("--keep", type=int, default=3, help="Backups to keep")

    args = parser.parse_args()

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


if __name__ == "__main__":
    main()