---
name: threnody-task
description: >-
  Threnody normal orchestration via plan_task, decompose_task, and fleet_plan.
  Use for multi-file tasks, wave-based host Agent execution, or /fleet planning
  without a full execute_swarm contract.
---

# Threnody task orchestration

**Execution phase** after planning — see **`threnody-plan`** for plan-only vs
plan-then-execute routing.

Use this skill for **planning + host wave execution** without the full swarm
persistence contract (`swarm_id`, budget preview, resume checkpoints).

## When to use

| Use `threnody-task` | Use `threnody-swarm` instead |
|---------------------|------------------------------|
| `plan_task` / `decompose_task` / `fleet_plan` | `execute_swarm` |
| Host runs `host_spawn_waves` | Need `swarm_id`, telemetry, resume |
| No budget preview token flow | Budget preview + `preview_token` confirm |
| Single planning pass | Coordinator star rounds (delegate mode) |

## Workflow

0. Prefer **`threnody-plan`** first unless the user already approved a wave plan.
1. **`route_task(task)`** — tier, `execution_hint`, optional single `host_spawn`.
2. **Decompose** (multi-concern work):
   - Prefer `decompose_task(task)` (alias of `plan_task`).
   - Or `plan_task(task)` directly.
3. **Read `host_spawn_waves`** from the plan response.
4. **Execute waves in order** via host `Task`/`Agent`:
   - Agents within one wave may run **in parallel**.
   - Respect wave ordering (later waves wait for earlier dependencies).
5. **Optional:** `fleet_plan(task)` when you want ready-made fleet command strings per wave.

## Rules

- Do **not** call `execute_subtask` for same-host work — use `host_spawn` entries.
- Utility delegation only when `delegation_utilities_enabled: true` (see `threnody-routing`).
- For frontend + backend + API in parallel, use the contract-first pattern in `threnody-fullstack`.

## Example

```
route_task(task="Refactor auth across services and UI")
decompose_task(task="Refactor auth across services and UI")
→ host_spawn_waves: [
     { wave: 1, parallel: false, agents: [...] },
     { wave: 2, parallel: true, agents: [...] }
   ]
→ Spawn each agent via host Task tool; wait for wave N before wave N+1.
```

## MCP tools

- `route_task`, `plan_task`, `decompose_task`, `fleet_plan`
- `validate_routing_guard` (guarded hosts before edits)
