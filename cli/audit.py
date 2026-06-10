"""CLI for HMAC-chained audit log — `threnody audit {verify,export}`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _get_db():
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))
    from shared.db import Database
    db_path = Path.home() / ".local" / "lib" / "Threnody" / "cache.db"
    return Database(db_path)


def cmd_verify(args) -> int:
    """Walk the HMAC chain and report tampered rows. Exits non-zero on any break."""
    db = _get_db()
    tables = args.tables or ["swarm_events", "agent_audit", "file_writes"]
    any_breaks = False
    for table in tables:
        try:
            breaks = db.verify_audit_chain(table)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if breaks:
            any_breaks = True
            for b in breaks:
                row_id = b.get("id", "?")
                exp = (b.get("expected_hmac") or "")[:16]
                stored = (b.get("stored_hmac") or "")[:16]
                print(f"CHAIN BREAK [{table}] row {row_id}: expected={exp}… stored={stored}…")
        else:
            if not args.quiet:
                print(f"ok [{table}]: chain intact")
    return 1 if any_breaks else 0


def cmd_export(args) -> int:
    """Export audit rows as JSONL for offline verification."""
    db = _get_db()
    tables = args.tables or ["swarm_events", "agent_audit", "file_writes"]
    select_map = {
        "swarm_events": (
            "SELECT id, swarm_id, event_type, payload, ts, chain_hmac"
            " FROM swarm_events ORDER BY id"
        ),
        "agent_audit": (
            "SELECT id, agent_id, event_type, details_json, created_at, chain_hmac"
            " FROM agent_audit ORDER BY id"
        ),
        "file_writes": (
            "SELECT id, scope, idempotency_key, target_path, completed_at, chain_hmac"
            " FROM file_writes ORDER BY id"
        ),
    }
    col_map = {
        "swarm_events": ["id", "swarm_id", "event_type", "payload", "ts", "chain_hmac"],
        "agent_audit": ["id", "agent_id", "event_type", "details_json", "created_at", "chain_hmac"],
        "file_writes": ["id", "scope", "idempotency_key", "target_path", "completed_at", "chain_hmac"],
    }
    out = sys.stdout
    if args.output:
        out_path = Path(args.output).resolve()
        if out_path.is_symlink():
            print(f"error: refusing to write through symlink: {out_path}", file=sys.stderr)
            return 1
        out = out_path.open("w", encoding="utf-8")
    try:
        with db.conn() as conn:
            for table in tables:
                query = select_map.get(table)
                cols = col_map.get(table)
                if not query or not cols:
                    print(f"warning: unknown table {table!r}", file=sys.stderr)
                    continue
                rows = conn.execute(query).fetchall()
                for row in rows:
                    record: dict = {"_table": table}
                    record.update(zip(cols, row))
                    out.write(json.dumps(record, default=str) + "\n")
    finally:
        if args.output:
            out.close()
    if not args.quiet:
        print(f"exported to {args.output or 'stdout'}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Threnody HMAC-chained audit log")
    sub = parser.add_subparsers(dest="subcmd")

    verify_p = sub.add_parser("verify", help="Verify HMAC chain integrity")
    verify_p.add_argument(
        "--tables", nargs="*", metavar="TABLE",
        help="Tables to verify (default: swarm_events agent_audit file_writes)",
    )
    verify_p.add_argument("--quiet", action="store_true", help="Only print failures")

    export_p = sub.add_parser("export", help="Export audit log as JSONL")
    export_p.add_argument(
        "--tables", nargs="*", metavar="TABLE",
        help="Tables to export (default: all audit tables)",
    )
    export_p.add_argument("--output", "-o", metavar="FILE", help="Output file (default: stdout)")
    export_p.add_argument("--quiet", action="store_true")

    args = parser.parse_args()
    if args.subcmd == "verify":
        return cmd_verify(args)
    if args.subcmd == "export":
        return cmd_export(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
