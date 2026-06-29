# Competitive positioning (operator view)

Threnody and large swarm meta-harnesses (e.g. [Ruflo](https://github.com/ruvnet/ruflo))
both coordinate multi-agent work inside AI coding CLIs. They optimize for
different goals. This document is **not** a feature parity checklist or legal
comparison — it helps operators choose scope.

## Threnody's wedge

**Spend less on the CLIs you already pay for** by routing tier, planning waves,
and executing through the **host shell** (Task/Agent) instead of subprocess
arbitrage and coordination LLM rounds. Threnody should feel deliberately
different from Ruflo: smaller, auditable, receipt-driven, approval-gated, and
replayable instead of a giant platform surface.

| Dimension | Threnody | Typical large swarm platform |
|-----------|----------|------------------------------|
| Primary goal | Cost-aware routing + local coordination | Autonomous multi-agent collaboration at scale |
| Execution default | Host-native `host_spawn_waves` | Platform-owned swarm runtime + many MCP tools |
| Agent model | Tier (low/medium/high), curated task packs, host models | Catalog of 100+ specialist agent types |
| Alignment | Contract artifacts + verify gates | Queen + consensus (Raft, BFT, gossip, mesh) |
| Memory | SQLite FTS + approval-gated learning | Vector DB, neural patterns, trajectory learning |
| Federation | Deferred / out of scope | Cross-machine agent comms |
| Codebase | Plain Python, focused MCP tool layer | Large plugin ecosystem |
| Trust artifact | `cost_receipt` + `inspect_run_receipt` JSON/Markdown/HTML | Platform verification and hosted surfaces |
| Fanout control | No default hard host-native size cap; throttle with `parallelism.max_workers` | Platform-level swarm sizing and runtime limits |

## What Threnody deliberately does not copy

To preserve the token-cost wedge, Threnody does **not** ship by default:

- Multi-queen or peer-voting swarm consensus
- Hive-mind / mesh topologies with gossip rounds
- Cross-machine federation
- Fixed armies of named specialist agents
- Vector/graph memory before cited SQLite memory digests prove the need

These patterns add **coordination LLM passes** and operational surface. They fit
platform-scale autonomy; they work against Threnody's lean meta-harness story.

## New differentiators

- **Token-savings receipts**: `route_task`, `plan_task`, and host-native
  `execute_swarm` responses include `cost_receipt` with selected tier/model,
  high-tier counterfactual, savings estimate, and skipped coordination calls.
- **Operator run receipts**: `inspect_run_receipt(run_id, format=json|markdown|html)`
  exports a local trust artifact with the plan, waves, target files, learning
  contract, policy decisions, and outcome fields.
- **Curated task packs**: `list_task_packs` and `plan_task_pack` provide a small
  set of practical presets (`test-gap`, `security-review`, `docs-sync`,
  `release-check`, `frontend-smoke`, `migration-plan`) instead of a huge agent
  marketplace.
- **Workflow blueprints**: `workflow_blueprint_export` turns successful
  host-native wave plans into replayable blueprints; `workflow_blueprint_run`
  replays them without another planner call.
- **Agent-count optimizer**: `execute_swarm` request preparation records a
  recommendation (`single_agent`, `two_agent_pair`, `bounded_swarm`, or
  `review_file_sweep`) so Threnody can show when fewer agents are cheaper and
  when broad file-level review should actually scale out.
- **Fast review default**: broad code review uses `FAST_REVIEW:` — one
  read-only agent per file plus synthesis. Deep file × dimension review is
  opt-in for explicit specialist/security-critical audits.

## Cheap alignment (Threnody's "consensus")

Instead of voting protocols:

1. **Contract-first wave** — OpenAPI, shared types (`produces` / `consumes`)
2. **Parallel workers** — same wave after contract lands
3. **Integration wave** — wire-up, smoke tests, or operator verify gates
4. **Optional targeted verify** — only synthesized high/critical findings get a
   follow-up read-only verifier

Expert path: **delegate** swarm mode with **star** topology and a single
coordinator — legacy/expert only, not default.

## When to use which

| Operator need | Lean toward |
|---------------|-------------|
| Solo dev, multiple CLIs, minimize surprise billing | **Threnody** |
| Plan waves, approve before spawn, host executes | **Threnody** (`threnody-plan` skill) |
| 35-file review that should not collapse to 3 agents | **Threnody** (`threnody-fast-review`; set `parallelism.max_workers` to throttle) |
| 100+ agents, federation, enterprise hive-mind | Dedicated swarm platform |
| Autonomous loops with heavy self-learning infra | Dedicated swarm platform |

## Related docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — execution model and topologies
- [COST_SAVINGS.md](COST_SAVINGS.md) — host-native vs delegate economics
- [RELEASE_LIMITATIONS.md](RELEASE_LIMITATIONS.md) — federation deferred
- [RUFLO_HEAD_TO_HEAD.md](RUFLO_HEAD_TO_HEAD.md) — explicit Ruflo comparison
