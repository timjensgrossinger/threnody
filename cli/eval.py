"""
threnody eval bandit — Contextual bandit shadow win-rate reporter (plan 11).

Usage:
    threnody eval bandit [--last N] [--since HOURS_AGO] [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time


def _load_db():
    try:
        from shared.db import Database
        import os
        db_path = os.path.expanduser("~/.local/lib/threnody/cache.db")
        return Database(db_path)
    except Exception as exc:
        print(f"error: could not open DB: {exc}", file=sys.stderr)
        sys.exit(1)


def _wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def cmd_bandit(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="threnody eval bandit",
        description="Contextual bandit shadow win-rate vs heuristic",
    )
    parser.add_argument("--last", type=int, default=500, metavar="N",
                        help="Number of recent decisions to analyse (default 500)")
    parser.add_argument("--since", type=float, default=0.0, metavar="HOURS",
                        help="Only include decisions from last HOURS hours")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args(argv)

    since_ts = 0.0
    if args.since > 0:
        since_ts = time.time() - args.since * 3600

    db = _load_db()
    rows = db.get_bandit_summary(limit=args.last, since_ts=since_ts)

    if not rows:
        if args.json:
            print(json.dumps({"error": "no decisions recorded"}))
        else:
            print("No routing decisions recorded yet.")
            print("Bandit shadow mode logs a decision on every route_task call once active.")
        return 0

    total = len(rows)
    # Decisions where bandit agreed with heuristic
    agreements = sum(1 for r in rows if r["bandit_pick"] == r["heuristic_pick"])
    # Decisions where bandit would have diverged
    divergences = total - agreements

    # Win rate: for scored decisions, did bandit pick have better outcome?
    scored = [r for r in rows if r["outcome_score"] is not None]
    bandit_wins = 0
    heuristic_wins = 0
    for r in scored:
        if r["bandit_pick"] != r["heuristic_pick"]:
            # Counterfactual: we can't know bandit outcome for unchosen arm,
            # but we can track regret as a proxy.
            regret = r.get("regret") or 0.0
            if regret < 0:
                bandit_wins += 1
            elif regret > 0:
                heuristic_wins += 1

    total_regret = sum((r.get("regret") or 0.0) for r in scored if r.get("regret") is not None)
    mean_outcome = (
        sum(r["outcome_score"] for r in scored) / len(scored) if scored else None
    )

    # Wilson CI for agreement rate
    ci_lo, ci_hi = _wilson_ci(agreements, total)

    summary = {
        "total_decisions": total,
        "scored_decisions": len(scored),
        "bandit_agrees_heuristic": agreements,
        "bandit_diverges": divergences,
        "agreement_rate": round(agreements / total, 4) if total else 0.0,
        "agreement_ci_95": [round(ci_lo, 4), round(ci_hi, 4)],
        "bandit_wins_on_diverge": bandit_wins,
        "heuristic_wins_on_diverge": heuristic_wins,
        "total_regret": round(total_regret, 4),
        "mean_outcome_score": round(mean_outcome, 4) if mean_outcome is not None else None,
        "mode": "shadow",
        "note": (
            "Shadow mode: heuristic always executed. "
            "Bandit pick is counterfactual. Regret = heuristic_score - bandit_expected."
        ),
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"\nContextual Bandit Shadow Report ({total} decisions)")
        print("=" * 52)
        print(f"  Bandit agrees with heuristic : {agreements}/{total} "
              f"({summary['agreement_rate']:.1%})  95% CI [{ci_lo:.1%}, {ci_hi:.1%}]")
        print(f"  Bandit diverges              : {divergences}")
        if scored:
            print(f"  Scored decisions             : {len(scored)}")
            print(f"  Mean outcome score           : {mean_outcome:.3f}")
            print(f"  Total regret (sum)           : {total_regret:.3f}")
            if bandit_wins + heuristic_wins > 0:
                print(f"  On diverge — bandit wins     : {bandit_wins}")
                print(f"  On diverge — heuristic wins  : {heuristic_wins}")
        print(f"\n  Mode: {summary['mode']} (set config.routing.bandit_mode=live to promote)")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="threnody eval", add_help=False)
    parser.add_argument("subcommand", nargs="?", default="bandit")
    args, rest = parser.parse_known_args(argv)
    if args.subcommand == "bandit":
        return cmd_bandit(rest)
    print(f"Unknown subcommand: {args.subcommand!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
