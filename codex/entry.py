#!/usr/bin/env python3
"""
Threnody Codex CLI entry point (Wave 2 — Legacy Compatibility Wrapper).

Thin wrapper that initialises the shared core with the Codex provider
and exposes CLI commands. Follows copilot/entry.py pattern with registry fallback.

Per D-11 (both registry/MCP path and thin entry-point pattern) and D-12 (graceful degradation).

Commands:
  plan <task>               Decompose via planner LLM
  synthesise <task> <json>  Merge agent results
  route <task>              Heuristic classification (instant)
  cache-get <task>          Look up cached result
  cache-put <task> <r> <m>  Store result
  cache-stats               Print cache statistics
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Ensure shared/ is importable
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from shared.config import TGsConfig
from shared.adapters import ProviderCapability
from shared.router import TaskRouter
from shared.planner import Planner, ProviderAgnosticBackend
from shared.orchestrator import Orchestrator
from shared.db import Database
from shared.discovery import get_registry
from codex.providers import CODEX_TIER_MAP, CodexProvider
from codex.providers_legacy import adapter_from_legacy

log = logging.getLogger(__name__)


def _resolve_provider():
    """Resolve Codex provider via registry.

    Per D-12 pattern: registry lookup first, returns None when unavailable.
    Resolution failures are logged at DEBUG so stderr stays clean for JSON callers.
    """
    try:
        registry = get_registry()
        registry.register_adapter(adapter_from_legacy())
        adapter = registry.resolve_adapter("codex", ProviderCapability.EXECUTE)
        if adapter is not None:
            try:
                provider = adapter.invoke("build_provider")
                log.info("Codex adapter resolved from registry")
                return provider
            except Exception as e:
                log.debug("Codex adapter invoke failed: %s", e)
    except Exception as e:
        log.debug("Codex registry resolution failed: %s", e)

    log.debug("Codex provider unavailable; continuing without provider")
    return None


def _init() -> tuple[TGsConfig, Database, TaskRouter, Planner, Orchestrator]:
    """Bootstrap all components."""
    config = TGsConfig.from_yaml()
    db = Database(config.db_path)
    router = TaskRouter(config)
    registry = get_registry()
    planner = Planner(
        config,
        ProviderAgnosticBackend(registry, caller="codex"),
        db,
    )
    provider = _resolve_provider() or CodexProvider()
    orchestrator = Orchestrator(
        config,
        provider,
        planner,
        db,
        caller="codex",
    )
    return config, db, router, planner, orchestrator


def cmd_plan(task: str) -> None:
    config, db, router, planner, orchestrator = _init()
    plan = planner.plan(task)
    print(json.dumps(planner.plan_to_dict(plan)))


def cmd_synthesise(task: str, results_json: str) -> None:
    config, db, router, planner, orchestrator = _init()
    results = json.loads(results_json)
    synthesis = orchestrator.synthesise(task, results)
    if synthesis:
        print(json.dumps({"synthesis": synthesis}))
    else:
        print(json.dumps({"synthesis": None, "error": "synthesis call failed"}))


def cmd_route(task: str) -> None:
    config, db, router, planner, orchestrator = _init()
    decision = router.classify(task)
    provider = _resolve_provider()
    model = (
        provider.resolve_model(decision.tier)
        if provider
        else CODEX_TIER_MAP["low"]
    )
    cached = db.cache_get(task)
    print(json.dumps({
        "tier": decision.tier,
        "model": model,
        "score": decision.score,
        "reason": decision.reason,
        "agents": decision.agents,
        "cache_hit": cached is not None,
        "override": decision.override,
        "intent_modifier": decision.intent_modifier,
    }))


def _open_db() -> Database:
    """Load config and return a Database at the configured path.

    Raises on config/DB init failure so main()'s exception handler
    returns a JSON error with a nonzero exit code.
    """
    config = TGsConfig.from_yaml()
    return Database(config.db_path)


def cmd_cache_get(task: str) -> None:
    db = _open_db()
    hit = db.cache_get(task)
    if hit:
        result, model = hit
        print(json.dumps({"found": True, "result": result, "model": model}))
    else:
        print(json.dumps({"found": False}))


def cmd_cache_put(task: str, result: str, model: str) -> None:
    db = _open_db()
    db.cache_put(task, result, model)
    print(json.dumps({"stored": True}))


def cmd_cache_stats() -> None:
    db = _open_db()
    print(json.dumps(db.cache_stats(), indent=2))


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    args = sys.argv[1:]
    if not args:
        print(
            "Usage: entry.py <command> [args...]\n\n"
            "Commands:\n"
            "  plan <task>                  Decompose task via planner LLM\n"
            "  synthesise <task> <json>     Merge agent results via planner LLM\n"
            "  route <task>                 Heuristic classification (instant)\n"
            "  cache-get <task>             Look up cached result\n"
            "  cache-put <task> <r> <m>     Store result\n"
            "  cache-stats                  Print cache statistics\n",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = args[0]
    try:
        if cmd == "plan" and len(args) >= 2:
            cmd_plan(" ".join(args[1:]))
        elif cmd == "synthesise" and len(args) >= 3:
            cmd_synthesise(args[1], args[2])
        elif cmd == "route" and len(args) >= 2:
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
