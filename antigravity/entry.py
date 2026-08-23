#!/usr/bin/env python3
"""
Threnody Antigravity CLI entry point.

Host-native only — no subprocess delegation. Execution is handled by the
agy plugin system (skills, hooks, MCP, agents) rather than subprocess invocation.

This entry point is retained for backward compatibility and diagnostic use only.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from shared.config import TGsConfig
from shared.router import TaskRouter
from shared.db import Database
from shared.db_client import open_database

log = logging.getLogger(__name__)


def _init() -> tuple[TGsConfig, Database, TaskRouter]:
    """Bootstrap core components."""
    config = TGsConfig.from_yaml()
    db = open_database(config.db_path, config=config)
    router = TaskRouter(config)
    return config, db, router


def cmd_route(task: str) -> None:
    config, db, router = _init()
    decision = router.classify(task)
    cached = db.cache_get(task)
    print(json.dumps({
        "tier": decision.tier,
        "score": decision.score,
        "reason": decision.reason,
        "agents": decision.agents,
        "cache_hit": cached is not None,
        "note": "Host-native execution only — use agy plugin for subagent spawning",
    }))


def cmd_cache_get(task: str) -> None:
    _config, db, _router = _init()
    hit = db.cache_get(task)
    if hit:
        result, model = hit
        print(json.dumps({"found": True, "result": result, "model": model}))
    else:
        print(json.dumps({"found": False}))


def cmd_cache_put(task: str, result: str, model: str) -> None:
    _config, db, _router = _init()
    db.cache_put(task, result, model)
    print(json.dumps({"stored": True}))


def cmd_cache_stats() -> None:
    _config, db, _router = _init()
    print(json.dumps(db.cache_stats(), indent=2))



def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    args = sys.argv[1:]
    if not args:
        print(
            "Usage: entry.py <command> [args...]\n\n"
            "Commands:\n"
            "  route <task>                 Heuristic classification (instant)\n"
            "  cache-get <task>             Look up cached result\n"
            "  cache-put <task> <r> <m>     Store result\n"
            "  cache-stats                  Print cache statistics\n",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = args[0]
    try:
        if cmd == "route" and len(args) >= 2:
            cmd_route(" ".join(args[1:]))
        elif cmd == "cache-get" and len(args) >= 2:
            cmd_cache_get(" ".join(args[1:]))
        elif cmd == "cache-put" and len(args) == 4:
            cmd_cache_put(args[1], args[2], args[3])
        elif cmd == "cache-stats":
            cmd_cache_stats()
        else:
            print(f"Unknown command or wrong args: {args}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        log.exception("Unhandled error")
        print(json.dumps({"error": f"{type(e).__name__}: operation failed"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
