---
name: threnody-plan
description: >-
  Threnody planning mode for normal orchestration and swarms. Use when user
  asks to plan, decompose, show waves, dry run, preview swarm, or before
  multi-file work. Supports plan-only (stop for approval) or plan-then-execute.
---

# Threnody planning mode

Unified **planning entry point** for normal orchestration (`plan_task`) and
swarms (`execute_swarm`). Detect plan-only vs plan-then-execute from user wording.

## Trigger phrases

**Plan-only:** "plan only", "dry run", "show waves", "preview the swarm",
"don't execute yet", "what agents would you spawn"

**Plan-then-execute:** default when user asks to build/implement/refactor without
plan-only qualifiers

**Always plan first:** multi-file, multi-concern, or parallel agent work

Host-native plans auto-fan out to **one agent per file** when intent implies a webapp/fullstack stack (`heuristic_intent_templates`). Use `expand_host_plan` after scaffold waves if more files appear.

## Fast-start contract

Any plan path that emits agents must return a spawnable `host_spawn_waves`
handoff quickly: target **under 5 seconds** to handoff and **under 30 seconds**
to first host spawn. Keep optional LLM refinement, consensus, detailed receipts,
and learning aggregation off the first-spawn path.

When executing a returned plan, spawn **all agents in the same wave as one
batch** before waiting at the wave barrier. Never serialize agents inside a
single wave.

## Workflow

### 1. Classify

Call **`route_task(task=...)`** (MCP: Threnody).

### 2. Choose planning tool

| Need | Tool |
|------|------|
| Standard multi-step work, no swarm persistence | `decompose_task` (preferred) or `plan_task` |
| Budget cap, `swarm_id`, topology, resume | `execute_swarm` (host-native default) |
| Frontend + backend + API in parallel | Same tools + contract-first prompt (see `threnody-fullstack`) |

**Swarm signals:** large fanout, explicit budget/resume, topology choice, user says "swarm"

**Task signals:** refactor across files, fleet waves, no persistence requirement

### 3. Present wave plan

Use this template:

```markdown
## Threnody plan

- **Tier:** {tier from route_task}
- **Mode:** {plan_task | execute_swarm}
- **Topology:** {dag | star | hierarchical | auto | linear} (swarm only)
- **Host execution:** host_native (default)

| Wave | Parallel | Agents | Tier | Summary |
|------|----------|--------|------|---------|
| 1 | no | 1 | medium | Define API contract |
| 2 | yes | 2 | medium | Backend + frontend |
| 3 | no | 1 | low | Integration smoke tests |

**Estimated cost:** {from swarm cost_estimate if present}
```

Include `consumes` / `produces` / `depends_on` when the plan exposes them.

### 4. Branch on plan-only

**Plan-only** (user said dry run / plan only / don't execute):

- Stop after the wave table.
- Do **not** spawn host Task/Agent or call `execute_subtask`.
- Ask: "Approve this wave plan?"
- On approval → hand off to `threnody-task` or `threnody-swarm` for execution.

**Plan-then-execute** (default):

- Show a **brief** wave summary (can be shorter than full table).
- Immediately spawn `host_spawn_waves` via `threnody-task` or `threnody-swarm` — the orchestrator does **not** implement subtasks with direct edits.
- For every wave, start every same-wave agent first, then wait for that wave's
  results before advancing to dependent waves.
- **Reporting:** in `batch` mode (default, see `learning_report_contract.report_mode`) report once at terminal via `report_host_swarm_complete` — no per-worker-wave round-trip. In `inline` mode call `report_host_wave` after each wave. Use `inspect_swarm` to confirm status transitions.

## Full-stack prompt boilerplate

When planning frontend + backend + API work, append to the task:

```
Contract-first: wave 1 OpenAPI + shared types (produces api-contract);
wave 2 parallel frontend and backend (consumes api-contract);
wave 3 integration tests and wire-up. Use depends_on and topology dag.
```

See **`threnody-fullstack`** for details.

## Token cost

- Prefer **host-native** plans (`host_spawn_waves`) — one planner call, host Task/Agent bills once.
- Avoid **delegate swarm + star coordinator rounds** unless expert — each round adds LLM synthesis cost.
- Alignment = **contract artifacts + verify gates**, not multi-agent voting consensus.

## Execution handoff

| Planned with | Execute via |
|--------------|-------------|
| `decompose_task` / `plan_task` | `threnody-task` |
| `execute_swarm` | `threnody-swarm` |

## Related skills

- Entry routing: `threnody-routing`
- Normal execution: `threnody-task`
- Swarm execution: `threnody-swarm`
- Full-stack parallel: `threnody-fullstack`
- Utility subtask monitoring: `threnody-subtasks`
