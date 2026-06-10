"""CLI for cost savings dashboard — `threnody gain`."""
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


def _parse_window(since: str) -> float:
    """Parse '7d', '30d', '24h' into a since_ts float."""
    now = time.time()
    since = since.strip().lower()
    if since.endswith("d"):
        days = float(since[:-1])
        return now - days * 86400
    if since.endswith("h"):
        hours = float(since[:-1])
        return now - hours * 3600
    return 0.0  # all time


def _avg_compression_ratio(db, since_ts: float, group_col: str) -> dict[str, float | None]:
    """Return avg context_compression_ratio per group key, skipping NULLs."""
    try:
        with db.conn() as conn:
            rows = conn.execute(
                f"SELECT {group_col}, AVG(context_compression_ratio)"
                " FROM cost_telemetry"
                " WHERE ts >= ? AND context_compression_ratio IS NOT NULL"
                f" GROUP BY {group_col}",
                (since_ts,),
            ).fetchall()
        return {r[0]: r[1] for r in rows if r[0] is not None}
    except Exception:
        return {}


def _print_table(rows: list[dict], key_col: str, compression: dict | None = None) -> None:
    if not rows:
        print("No cost telemetry found for this window.")
        return
    show_compression = bool(compression)
    cols = [key_col, "subtask_count", "est_cost_usd", "counterfactual_cost_usd", "savings_usd"]
    if show_compression:
        cols.append("ctx_compression")
    if show_compression and compression:
        for row in rows:
            ratio = compression.get(row.get(key_col))
            row["ctx_compression"] = f"{ratio:.2f}" if ratio is not None else "-"
    widths = {c: max(len(c), max((len(str(r.get(c, ""))) for r in rows), default=0)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for row in rows:
        line = "  ".join(str(row.get(c, "")).ljust(widths[c]) for c in cols)
        print(line)
    total_est = sum(float(r.get("est_cost_usd") or 0) for r in rows)
    total_cf = sum(float(r.get("counterfactual_cost_usd") or 0) for r in rows)
    total_savings = total_cf - total_est
    print("-" * len(header))
    print(f"  Total savings: ${total_savings:.4f}  (est=${total_est:.4f}  counterfactual=${total_cf:.4f})")


def cmd_gain(args) -> int:
    db = _get_db()
    since_ts = _parse_window(args.since)
    group_col = "tier"
    if args.by == "provider":
        group_col = "provider_id"
    elif args.by == "model":
        group_col = "model"

    rows = db.get_cost_summary(since_ts=since_ts, group_by=group_col)
    compression = _avg_compression_ratio(db, since_ts, group_col)

    if args.json:
        if compression:
            for row in rows:
                key = row.get(group_col)
                row["ctx_compression_ratio"] = compression.get(key)
        print(json.dumps(rows, indent=2))
        return 0

    if args.history:
        with db.conn() as conn:
            hist_rows = conn.execute(
                "SELECT task_id, tier, provider_id, model, input_tokens, output_tokens,"
                " est_cost_usd, counterfactual_cost_usd, ts"
                " FROM cost_telemetry WHERE ts >= ? ORDER BY ts DESC LIMIT 100",
                (since_ts,),
            ).fetchall()
        for r in hist_rows:
            ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(r[8]))
            savings = (r[7] or 0.0) - (r[6] or 0.0)
            print(f"{ts_str}  {r[0][:24]:24s}  {r[1]:8s}  {r[2]:20s}  est=${r[6]:.6f}  saved=${savings:.6f}")
        return 0

    _print_table(rows, group_col, compression=compression)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Threnody cost savings dashboard")
    parser.add_argument("--since", default="7d", help="Time window (e.g. 7d, 30d, 24h)")
    parser.add_argument("--by", choices=["tier", "provider", "model"], default="tier")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--history", action="store_true", help="Show per-run rows")
    args = parser.parse_args()
    return cmd_gain(args)


if __name__ == "__main__":
    sys.exit(main())
