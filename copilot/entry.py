#!/usr/bin/env python3
"""
Threnody Copilot CLI entry point.

Thin wrapper that initialises the shared core with the Copilot provider
and exposes CLI commands. Called by the shell wrapper (ghc.sh).

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
from shared.planner import Planner, GhCopilotBackend, ProviderAgnosticBackend
from shared.orchestrator import Orchestrator
from shared.db import Database
from shared.discovery import get_registry
from copilot.providers import CopilotProvider
from copilot.providers_legacy import adapter_from_legacy

log = logging.getLogger(__name__)


def _resolve_provider() -> CopilotProvider:
    registry = get_registry()
    registry.register_adapter(adapter_from_legacy())
    adapter = registry.resolve_adapter("copilot", ProviderCapability.EXECUTE)
    if adapter is None:
        log.warning("Copilot adapter unavailable; falling back to direct provider")
        return CopilotProvider()
    try:
        provider = adapter.invoke("build_provider")
    except Exception:
        log.warning("Copilot adapter build_provider failed; falling back", exc_info=True)
        return CopilotProvider()
    return provider if isinstance(provider, CopilotProvider) else CopilotProvider()


def _init() -> tuple[TGsConfig, Database, TaskRouter, Planner, Orchestrator]:
    """Bootstrap all components."""
    config = TGsConfig.from_yaml()
    db = Database(config.db_path)
    router = TaskRouter(config)
    try:
        backend: GhCopilotBackend | ProviderAgnosticBackend = ProviderAgnosticBackend(get_registry())
    except Exception:
        log.warning("ProviderRegistry unavailable for planner, falling back to GhCopilotBackend")
        backend = GhCopilotBackend()
    planner = Planner(config, backend, db)
    provider = _resolve_provider()
    orchestrator = Orchestrator(config, provider, planner, db)
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
    provider = CopilotProvider()
    model = provider.resolve_model(decision.tier)
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


def cmd_cache_get(task: str) -> None:
    db = Database()
    hit = db.cache_get(task)
    if hit:
        result, model = hit
        print(json.dumps({"found": True, "result": result, "model": model}))
    else:
        print(json.dumps({"found": False}))


def cmd_cache_put(task: str, result: str, model: str) -> None:
    db = Database()
    db.cache_put(task, result, model)
    print(json.dumps({"stored": True}))


def cmd_cache_stats() -> None:
    db = Database()
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
