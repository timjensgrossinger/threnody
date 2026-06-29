---
name: threnody-fullstack
description: >-
  Build frontend, backend, and API in parallel with Threnody using contract-first
  DAG waves. Use for full-stack app scaffolding, OpenAPI-first development,
  parallel UI and service agents, or integration smoke tests after fanout.
---

# Threnody full-stack parallel build

Threnody has no dedicated "app builder" mode. Use **contract-first DAG planning**
so frontend and backend agents align before parallel execution.

## Mandatory: spawn subagents

Full-stack orchestration uses the same contract as `threnody-task` and
`threnody-swarm`:

- **Spawn one host `Task`/`Agent` per subtask** in each wave.
- **Never** implement wave work yourself with Write/Edit — even for low-tier integration subtasks.
- Expect `host_execution_contract: spawn_subagents` on plan/swarm responses.

## Recommended wave shape

```text
Wave 1: Define API contract
  - OpenAPI spec, shared DTOs, route table
  - produces: api-contract (openapi.yaml, shared types)

Wave 2: Parallel implementation (depends_on wave 1)
  - Backend service/API handlers     consumes: api-contract
  - Frontend UI + client SDK         consumes: api-contract

Wave 3: Integration
  - Wire frontend to backend, smoke tests, fix drift
  - depends_on: backend + frontend subtasks
```

Frontend and backend run **in the same parallel wave** only after the contract
lands — not before.

## Which MCP entry point?

| Goal | Tool | Topology |
|------|------|----------|
| Plan once, host executes | `decompose_task` or `plan_task` | Ask planner for DAG + `depends_on` |
| Persistence, budget, resume | `execute_swarm` | `topology: dag` or `auto` |

Both return **`host_spawn_waves`** with `spawn_subagents` contract. Spawn one host
`Task`/`Agent` per subtask per wave — parallel wave 2 agents in one message.

Host-native heuristic planning also recognizes **fullstack/openapi** intent and
builds a contract-first DAG automatically. After wave 1, use `expand_host_plan`
if additional files were discovered.

## Prompt the planner explicitly

Include in the task string:

- "Contract-first: wave 1 OpenAPI + shared types"
- "Wave 2: parallel frontend and backend consuming the contract"
- "Wave 3: integration and smoke tests"
- "Use depends_on / consumes / produces artifact names"

Example task for `execute_swarm`:

```
Build a todo app: wave 1 OpenAPI + shared types; wave 2 parallel React frontend
and FastAPI backend both consuming the contract; wave 3 integration tests and
wire-up. topology dag.
```

## Do we need consensus?

**No multi-agent voting consensus is built in.**

| Approach | Role |
|----------|------|
| **Contract-first DAG** | **Recommended** — shared spec aligns workers |
| **Integration wave** | Closest hard check — run tests after parallel work |
| **Host lead review** | Default for host-native — you merge after spawned agents finish |
| **Star + delegate mode** | Optional single coordinator verdict rounds (expert/legacy) |

Use coordinator **star** topology only in **delegate** swarm mode when you
want Threnody to auto-amend the plan when workers disagree. Default host-native
swarms rely on contract + integration subtask + human/agent review.

For cost control, treat the contract wave and integration wave as the default
alignment mechanism. Do not add multi-queen consensus or high-tier review unless
the user explicitly asks for deep/security-critical validation.

## Pitfalls

- Parallel frontend/backend **without** a contract wave → API drift and merge conflicts.
- Expecting Threnody to auto-merge conflicting edits — add wave 3 integration.
- Using `execute_subtask` for host work — spawn `host_spawn_waves` instead.
- Skipping Task/Agent spawn and editing files directly — defeats orchestration.

## Related skills

- Planning entry: `threnody-plan`
- Normal execution: `threnody-task`
- `threnody-swarm` — `execute_swarm`, topology, budget preview
- `threnody-routing` — host-native vs utility delegation (direct edit only without orchestration skills)
