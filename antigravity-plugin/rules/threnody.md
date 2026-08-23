# Threnody for Antigravity — Rules

## Overview

Threnody is a meta-harness that coordinates tier routing, planning, and multi-agent orchestration via MCP. You (Antigravity/`agy`) are the execution host. Threnody plans and routes; you execute.

## Default path

1. Call `route_task(task=...)` for heuristic complexity scoring.
2. Read `execution_hint.mode`:
   - `host_native` — spawn subagents via `invoke_subagent` using the tier agent definitions.
   - `delegate` — only when explicitly enabled.
3. Follow `recommended_action` and `host_native_model`.

## Subagent spawning

Use `invoke_subagent` with the appropriate tier agent:
- **Low tier** → `threnody-agy-low` (gemini-3.7-flash, low effort)
- **Medium tier** → `threnody-agy-medium` (gemini-3.7-flash, high effort)
- **High tier** → `threnody-agy-high` (gemini-3.1-pro, high effort)

## Consensus review (when enabled)

For swarm/review tasks, spawn 3 queen reviewers in parallel:
- `threnody-agy-queen-correctness` — correctness-first stance
- `threnody-agy-queen-risk` — risk-first stance
- `threnody-agy-queen-speed` — speed-first stance

If 2+ agree, ship. If all 3 disagree, escalate to a Gemini Pro judge.

## Multi-file work

Use `plan_task` or `decompose_task` to break work into waves. Spawn one agent per file per wave. Report completion via `report_host_swarm_complete`.

## Learning capture

PostToolUse hooks log run data for pattern tracking. Do not disable — this feeds the learning loop.

## Cross-session memory

Use `memory_*` MCP tools for cross-session state. Scope: `global`, `project` (absolute path), or `task`.
