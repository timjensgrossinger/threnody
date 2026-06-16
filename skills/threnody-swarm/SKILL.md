---
name: threnody-swarm
description: >-
  Start and run Threnody execute_swarm with host-native host_spawn_waves,
  topology selection (dag/star/hierarchical/auto), budget preview, and resume.
  Use when user asks to swarm, fan out agents, or run multi-agent waves with
  swarm_id persistence.
---

# Threnody swarm orchestration

## Default: host-native swarms

For MCP host callers (Claude, Copilot, Cursor, Codex, etc.), `execute_swarm`
defaults to **`host_native`**:

1. Threnody plans the task.
2. Returns `awaiting_host_execution: true` + `host_spawn_waves`.
3. **You** spawn host `Task`/`Agent` per wave — Threnody does not subprocess.

This path is **unaffected** by utility-only delegation rules.

## Workflow

0. If not already planned, follow **`threnody-plan`** (plan-only swarm preview stops before spawn).
1. Optionally `route_task` for tier context.
2. **`execute_swarm(task, topology?, max_agents?, budget_limit?)`**
3. Handle response:
   - **`awaiting_host_execution` + `host_spawn_waves`** — execute waves via host agents.
   - **`preview: true` + `preview_token`** — cost over budget; confirm then re-call with token.
   - **`started: true`** (delegate mode only) — Threnody subprocess orchestrator running.
   - Check `learning_report_contract.report_mode` (`batch` default, or `inline`).
4. **Reporting — depends on `report_mode`:**
   - **`batch` (default):** Do **NOT** call `report_host_wave` for plain worker waves. Just spawn each wave natively. Per-agent learning is captured automatically (PostToolUse hook) or, when `learning_capture=model`, by passing the agents to the single terminal call. This is the fast path — no per-wave MCP round-trip.
   - **`inline` (legacy):** call `report_host_wave` after **each** wave with `workspace_root` and per-agent results (`task_id`, `spawn_id`, `success`, `touched_files`, **`output_excerpt`**).
5. **Mid-run expansion** (both modes): after scaffold/contract waves, call `expand_host_plan(discovered_files=[...])` to spawn additional file-scoped agents.
6. **Terminal:** call `report_host_swarm_complete(outcome=accepted|revised|reworked|rejected)` once at the end (in `batch` mode this imports the whole run and finalizes). In `inline` mode you may instead set `terminal=true` on the last `report_host_wave`. Verify `finalize.swarm_outcome.stored`.
7. Monitor:
   - Host-native: `inspect_swarm` for status; optional `inspect_status`.
   - Delegate: `list_subtasks`, `resume_swarm_inspect`, `resume_swarm_confirm`.

## Must (when awaiting_host_execution)

- Spawn **one** host `Task`/`Agent` per entry in `host_spawn_waves[].agents` — all agents in a wave in **one parallel message**.
- Pass each agent its `prompt`, `target_files`, `model`, and `subagent_type` from the handoff.
- Do **not** use `Write`/`Edit` yourself on any `target_files` from the plan.
- Do not follow `route_task`'s `direct_edit` hint while a handoff is active (`execution_hint.active_handoff` or pending `host_spawn_waves`).
- In **`batch`** mode report only once at terminal (plus consensus/expand). In **`inline`** mode report after each wave.

## Consensus waves

A wave with `wave_kind: "consensus"` is **always** reported via `report_host_wave` even in batch mode — a failed quorum spawns a judge mid-run, so the decision can't wait for terminal. Follow any returned `consensus_followup` (spawn the judge, report its verdict), then proceed to the terminal call.

## Terminal report example

```python
report_host_swarm_complete(
  swarm_id="<from handoff>",
  outcome="accepted",
  workspace_root="<workspace_root from handoff>",
  # batch + learning_capture=model: pass the final wave's agents here.
  # batch + learning_capture=hook (default): agents already captured; omit.
  agents=[{
    "task_id": "swarm-...:2",
    "spawn_id": "<host-agent-id>",
    "success": True,
    "touched_files": ["styles.css"],
    "output_excerpt": "Created card layout CSS with loading/error states",
  }],
)
```

Check `learning_enrichment` in the response when the server auto-fills excerpts from disk.

## Topology

| Value | Use when |
|-------|----------|
| `auto` | Let Threnody pick from urgency/complexity heuristics |
| `dag` | Explicit `depends_on` chains (recommended for full-stack) |
| `hierarchical` | Parent/child subtask trees |
| `star` | One **coordinator** + workers; reconciliation rounds (**delegate mode only**) |

**Not multi-queen:** at most one coordinator subtask per wave. Star topology uses a single coordinator with verdicts `complete` | `another-pass` | `fallback` — not peer voting.

## Delegate mode (legacy/expert)

Override only when intentional (`~/.local/lib/threnody/config.yaml`):

```yaml
swarm:
  host_execution_mode: delegate  # default for hosts is host_native
```

Delegate mode subprocesses via the orchestrator. Higher billing and policy surface.

## Full-stack parallel work

For frontend + backend + API simultaneously, see **`threnody-fullstack`** — contract-first DAG waves, integration subtask, optional coordinator star in delegate mode.

## Do not

- Call `execute_subtask` for same-host swarm agents.
- Substitute direct writes for swarm agents, even for low-tier or trivial tasks.
- Assume Threnody merges conflicting parallel edits — include an integration wave or review yourself.
