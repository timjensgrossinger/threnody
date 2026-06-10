"""
threnody trace — Trace replay + state forking CLI (plan 13).

Subcommands:
    show   <run_id>          Checkpoint timeline
    replay <run_id>          Replay from checkpoint [--from <id>] [--dry-run]
    fork   <run_id>          Fork from checkpoint [--from <id>] [--override key=val]
    diff   <run_a> <run_b>   Side-by-side trajectory comparison
"""
from __future__ import annotations

import argparse
import json
import sys


def _load_db():
    try:
        from shared.db import Database
        import os
        db_path = os.path.expanduser("~/.local/lib/threnody/cache.db")
        return Database(db_path)
    except Exception as exc:
        print(f"error: could not open DB: {exc}", file=sys.stderr)
        sys.exit(1)


def _engine(db=None):
    from shared.replay import ReplayEngine
    return ReplayEngine(db or _load_db())


def cmd_show(args: argparse.Namespace) -> int:
    engine = _engine()
    result = engine.show_run(args.run_id)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    print(f"\nRun: {result['run_id']}")
    print(f"  Status    : {result['status']}")
    print(f"  Topology  : {result.get('topology', 'linear')}")
    print(f"  Agents    : {result.get('effective_agents', '?')}")
    if result.get("parent_swarm_id"):
        print(f"  Forked from: {result['parent_swarm_id']}")
    cps = result.get("checkpoints", [])
    print(f"\n  Checkpoints ({len(cps)}):")
    for cp in cps:
        print(f"    [{cp['id']:4d}] round={cp['round']:3d}  rev={cp['revision']}  "
              f"verdict={cp['verdict'] or 'n/a'}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    engine = _engine()
    result = engine.execute_replay(
        args.run_id,
        from_checkpoint_id=args.from_checkpoint,
        overrides={},
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    status = result.get("status")
    plan = result.get("plan", {})
    print(f"\nReplay: {args.run_id}")
    print(f"  Status               : {status}")
    print(f"  From checkpoint      : {plan.get('from_checkpoint_id')}")
    print(f"  Subtasks to replay   : {plan.get('subtasks_to_replay', 0)}")
    print(f"  Subtasks skipped     : {plan.get('subtasks_to_skip', 0)}")
    print(f"  Approval gates       : {plan.get('approval_gates', 0)}")
    if status == "halted":
        print("\n  [HALTED] Approval required before replay can proceed.")
    return 0


def cmd_fork(args: argparse.Namespace) -> int:
    overrides: dict = {}
    for ov in (args.override or []):
        if "=" in ov:
            k, _, v = ov.partition("=")
            overrides[k.strip()] = v.strip()
    engine = _engine()
    result = engine.fork(
        args.run_id,
        from_checkpoint_id=args.from_checkpoint,
        overrides=overrides,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"\nFork: {args.run_id}")
    print(f"  Status     : {result['status']}")
    print(f"  Fork run   : {result['fork_run_id']}")
    plan = result.get("plan", {})
    print(f"  From round : {plan.get('from_round_index')}")
    if args.dry_run:
        print(f"\n  [DRY-RUN] No changes written. Add --no-dry-run to commit fork.")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    engine = _engine()
    result = engine.diff(args.run_a, args.run_b)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"\nDiff: {args.run_a}  vs  {args.run_b}")
    print(f"  Total rounds : {result['total_rounds']}")
    if result["identical"]:
        print("  Identical trajectories — no divergence.")
    else:
        print(f"  First diverge at round: {result['diverge_at_round']}")
        print(f"\n  Diverging rounds ({len(result['diffs'])}):")
        for d in result["diffs"]:
            print(f"    round={d['round']:3d}  A={d['run_a_verdict'] or 'n/a':12s}  "
                  f"B={d['run_b_verdict'] or 'n/a'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="threnody trace")
    sub = parser.add_subparsers(dest="subcmd")

    show_p = sub.add_parser("show", help="Checkpoint timeline for a run")
    show_p.add_argument("run_id")
    show_p.add_argument("--json", action="store_true")

    replay_p = sub.add_parser("replay", help="Replay from a checkpoint")
    replay_p.add_argument("run_id")
    replay_p.add_argument("--from", dest="from_checkpoint", type=int, default=None,
                          metavar="CHECKPOINT_ID")
    replay_p.add_argument("--dry-run", action="store_true", default=True)
    replay_p.add_argument("--json", action="store_true")

    fork_p = sub.add_parser("fork", help="Fork a run from a checkpoint")
    fork_p.add_argument("run_id")
    fork_p.add_argument("--from", dest="from_checkpoint", type=int, default=None,
                        metavar="CHECKPOINT_ID")
    fork_p.add_argument("--override", action="append", metavar="KEY=VAL",
                        help="Override: e.g. --override tier=high")
    fork_p.add_argument("--dry-run", action="store_true", default=False)
    fork_p.add_argument("--json", action="store_true")

    diff_p = sub.add_parser("diff", help="Side-by-side comparison of two runs")
    diff_p.add_argument("run_a")
    diff_p.add_argument("run_b")
    diff_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.subcmd == "show":
        return cmd_show(args)
    if args.subcmd == "replay":
        return cmd_replay(args)
    if args.subcmd == "fork":
        return cmd_fork(args)
    if args.subcmd == "diff":
        return cmd_diff(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
