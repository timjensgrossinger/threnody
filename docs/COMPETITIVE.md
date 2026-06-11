# Competitive positioning (operator view)

Threnody and large swarm meta-harnesses (e.g. [Ruflo](https://github.com/ruvnet/ruflo))
both coordinate multi-agent work inside AI coding CLIs. They optimize for
different goals. This document is **not** a feature parity checklist or legal
comparison — it helps operators choose scope.

## Threnody's wedge

**Spend less on the CLIs you already pay for** by routing tier, planning waves,
and executing through the **host shell** (Task/Agent) instead of subprocess
arbitrage and coordination LLM rounds.

| Dimension | Threnody | Typical large swarm platform |
|-----------|----------|------------------------------|
| Primary goal | Cost-aware routing + local coordination | Autonomous multi-agent collaboration at scale |
| Execution default | Host-native `host_spawn_waves` | Platform-owned swarm runtime + many MCP tools |
| Agent model | Tier (low/medium/high) + host models | Catalog of 100+ specialist agent types |
| Alignment | Contract artifacts + verify gates | Queen + consensus (Raft, BFT, gossip, mesh) |
| Memory | SQLite FTS + approval-gated learning | Vector DB, neural patterns, trajectory learning |
| Federation | Deferred / out of scope | Cross-machine agent comms |
| Codebase | Plain Python, ~43 MCP tools | Large plugin ecosystem |

## What Threnody deliberately does not copy

To preserve the token-cost wedge, Threnody does **not** ship by default:

- Multi-queen or peer-voting swarm consensus
- Hive-mind / mesh topologies with gossip rounds
- Cross-machine federation
- Fixed armies of named specialist agents

These patterns add **coordination LLM passes** and operational surface. They fit
platform-scale autonomy; they work against Threnody's lean meta-harness story.

## Cheap alignment (Threnody's "consensus")

Instead of voting protocols:

1. **Contract-first wave** — OpenAPI, shared types (`produces` / `consumes`)
2. **Parallel workers** — same wave after contract lands
3. **Integration wave** — wire-up, smoke tests, or operator verify gates
4. **Optional soft review** — one host integration Task agent (single medium-tier call)

Expert path: **delegate** swarm mode with **star** topology and a single
coordinator — legacy/expert only, not default.

## When to use which

| Operator need | Lean toward |
|---------------|-------------|
| Solo dev, multiple CLIs, minimize surprise billing | **Threnody** |
| Plan waves, approve before spawn, host executes | **Threnody** (`threnody-plan` skill) |
| 100+ agents, federation, enterprise hive-mind | Dedicated swarm platform |
| Autonomous loops with heavy self-learning infra | Dedicated swarm platform |

## Related docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — execution model and topologies
- [COST_SAVINGS.md](COST_SAVINGS.md) — host-native vs delegate economics
- [RELEASE_LIMITATIONS.md](RELEASE_LIMITATIONS.md) — federation deferred
