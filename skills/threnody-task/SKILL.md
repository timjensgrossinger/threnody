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
5. **Reporting** — check `learning_report_contract.report_mode`:
   - **`batch` (default):** do **NOT** call `report_host_wave` per worker wave. Spawn waves; learning is captured automatically (PostToolUse hook) or passed in the single terminal call (`learning_capture=model`).
   - **`inline` (legacy):** `report_host_wave(host_run_id|plan_run_id, wave, workspace_root, agents[])` after each wave with `task_id`, `spawn_id`, `success`, `touched_files`, **`output_excerpt`**.
6. **Mid-run:** `expand_host_plan(discovered_files=[...])` when wave 1 discovers files not in the initial plan.
7. **Final:** `report_host_swarm_complete(outcome=...)` once (batch imports + finalizes). Check `finalize.swarm_outcome`.
8. **Optional:** `fleet_plan(task)` when you want ready-made fleet command strings per wave.

## Rules

- Host-native heuristic planning fans out **one agent per file** for webapp/fullstack intent or explicit paths; single-file tasks stay one agent.
- When `host_spawn_waves` or `host_execution_contract: spawn_subagents` is present, spawn one `Task`/`Agent` per agent — never use direct `Write`/`Edit` on planned `target_files`.
- Do **not** call `execute_subtask` for same-host work — use `host_spawn` entries.
- Do not follow `route_task` `direct_edit` while a plan handoff is active; use `routing_guard` and `host_run_id` from the plan response.
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
→ (batch mode, default) report once at the end:
   report_host_swarm_complete(
     host_run_id,
     outcome="accepted",
     workspace_root="<from plan handoff>",
     agents=[{                       # only when learning_capture=model
       task_id, spawn_id, success,
       touched_files: ["relative/path.py"],
       output_excerpt: "short agent summary",
     }, ...],
   )
```

## MCP tools

- `route_task`, `plan_task`, `decompose_task`, `fleet_plan`
- `report_host_wave`, `report_host_swarm_complete`, `expand_host_plan`, `inspect_swarm`
- `validate_routing_guard` (guarded hosts before edits)
