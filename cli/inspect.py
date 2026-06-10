"""CLI for operator inspection — leases and dead letters (`threnody inspect leases|deadletters`)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _get_db():
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))
    from shared.db import Database
    db_path = Path.home() / ".local" / "lib" / "Threnody" / "cache.db"
    return Database(db_path)


def cmd_leases(args) -> int:
    db = _get_db()
    now = time.time()
    with db.conn() as conn:
        rows = conn.execute(
            "SELECT task_id, worker_id, acquired_at, expires_at, last_heartbeat,"
            " attempt, status FROM worker_leases ORDER BY acquired_at DESC LIMIT 100"
        ).fetchall()
    if not rows:
        print("No worker leases found.")
        return 0
    print(f"{'task_id':36s}  {'worker_id':20s}  {'status':8s}  {'attempt':7s}  {'expires_in':12s}")
    print("-" * 90)
    for r in rows:
        task_id, worker_id, acquired_at, expires_at, last_hb, attempt, status = r
        expires_in = f"{expires_at - now:.0f}s" if expires_at > now else "EXPIRED"
        print(f"{str(task_id)[:36]:36s}  {str(worker_id)[:20]:20s}  {status:8s}  {attempt:7d}  {expires_in:12s}")
    return 0


def cmd_deadletters(args) -> int:
    db = _get_db()
    if args.replay:
        ok = db.replay_dead_letter(args.replay)
        if ok:
            print(f"Replayed: {args.replay}")
        else:
            print(f"Not found in dead_letters: {args.replay}")
            return 1
        return 0

    entries = db.get_dead_letters(limit=args.limit)
    if not entries:
        print("Dead letter queue is empty.")
        return 0

    if args.json:
        print(json.dumps(entries, indent=2))
        return 0

    print(f"{'task_id':36s}  {'attempts':8s}  {'last_error':50s}")
    print("-" * 100)
    for e in entries:
        err = str(e.get("last_error", ""))[:50]
        print(f"{str(e['task_id'])[:36]:36s}  {e['attempt_count']:8d}  {err:50s}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Threnody operator inspection CLI")
    sub = parser.add_subparsers(dest="subcmd")

    sub.add_parser("leases", help="Show active and expired worker leases")

    dl_p = sub.add_parser("deadletters", help="Show or replay dead letter queue")
    dl_p.add_argument("--replay", metavar="TASK_ID", help="Re-queue a dead letter task")
    dl_p.add_argument("--limit", type=int, default=50, help="Max rows (default: 50)")
    dl_p.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args()
    if args.subcmd == "leases":
        return cmd_leases(args)
    if args.subcmd == "deadletters":
        return cmd_deadletters(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
