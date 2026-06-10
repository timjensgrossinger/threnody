"""
threnody policy — Runtime policy management CLI (plan 12).

Subcommands:
    list               List policy files in ~/.local/lib/threnody/policies/
    show <name>        Show a policy file
    test <policy.yaml> --path <path> --tool <tool> --command <cmd> --host <host>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


POLICIES_DIR = Path.home() / ".local/lib/threnody/policies"


def _list_policies(args: argparse.Namespace) -> int:
    if not POLICIES_DIR.exists():
        print(f"No policies directory at {POLICIES_DIR}")
        return 0
    files = sorted(POLICIES_DIR.glob("*.yaml"))
    if not files:
        print("No policy files found.")
        return 0
    for f in files:
        print(f"  {f.name}")
    return 0


def _show_policy(args: argparse.Namespace) -> int:
    name = args.name
    if not name.endswith(".yaml"):
        name += ".yaml"
    policy_path = POLICIES_DIR / name
    if not policy_path.exists():
        print(f"Policy not found: {policy_path}", file=sys.stderr)
        return 1
    print(policy_path.read_text())
    return 0


def _test_policy(args: argparse.Namespace) -> int:
    """Evaluate a policy against a single test operation."""
    from shared.policy import policy_from_yaml, evaluate, OPEN_POLICY

    policy_file = args.policy_file
    if not os.path.exists(policy_file):
        print(f"Policy file not found: {policy_file}", file=sys.stderr)
        return 1

    try:
        policy = policy_from_yaml(policy_file)
    except Exception as exc:
        print(f"Failed to load policy: {exc}", file=sys.stderr)
        return 1

    results: list[dict] = []

    if args.path:
        for target in args.path:
            verdict = evaluate(policy, "file_write", target)
            results.append({"op": "file_write", "target": target,
                             "decision": verdict.decision, "reason": verdict.reason})

    if args.tool:
        for target in args.tool:
            verdict = evaluate(policy, "mcp_tool", target)
            results.append({"op": "mcp_tool", "target": target,
                             "decision": verdict.decision, "reason": verdict.reason})

    if args.command:
        for target in args.command:
            verdict = evaluate(policy, "command", target)
            results.append({"op": "command", "target": target,
                             "decision": verdict.decision, "reason": verdict.reason})

    if args.host:
        for target in args.host:
            verdict = evaluate(policy, "http_egress", target)
            results.append({"op": "http_egress", "target": target,
                             "decision": verdict.decision, "reason": verdict.reason})

    if args.secret:
        for target in args.secret:
            verdict = evaluate(policy, "secret", target)
            results.append({"op": "secret", "target": target,
                             "decision": verdict.decision, "reason": verdict.reason})

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            icon = "✓" if r["decision"] == "allow" else "✗"
            print(f"  {icon} [{r['op']}] {r['target']!r:40s} → {r['decision']}  ({r['reason']})")

    denied = any(r["decision"] == "deny" for r in results)
    return 1 if denied else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="threnody policy")
    sub = parser.add_subparsers(dest="subcmd")

    sub.add_parser("list", help="List available policy files")

    show_p = sub.add_parser("show", help="Show policy file content")
    show_p.add_argument("name", help="Policy name (without .yaml)")

    test_p = sub.add_parser("test", help="Test a policy against operations")
    test_p.add_argument("policy_file", help="Path to policy YAML file")
    test_p.add_argument("--path",    action="append", default=[], metavar="PATH")
    test_p.add_argument("--tool",    action="append", default=[], metavar="TOOL")
    test_p.add_argument("--command", action="append", default=[], metavar="CMD")
    test_p.add_argument("--host",    action="append", default=[], metavar="HOST")
    test_p.add_argument("--secret",  action="append", default=[], metavar="SECRET")
    test_p.add_argument("--json",    action="store_true")

    args = parser.parse_args(argv)
    if args.subcmd == "list":
        return _list_policies(args)
    if args.subcmd == "show":
        return _show_policy(args)
    if args.subcmd == "test":
        return _test_policy(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
