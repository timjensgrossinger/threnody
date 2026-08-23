---
name: threnody-routing
description: >-
  Threnody MCP routing and host-native execution. Use when route_task,
  routing guard, execution_hint, host_spawn, HostNativeRequired, or
  utility-only delegation (delegation_utilities_enabled) is involved.
---

# Threnody routing

Threnody is a **meta-harness**: the MCP host shell executes work; Threnody
coordinates routing, planning, and optional utility delegation.

The portable contract is `host_spawn` / `host_spawn_waves`. Claude Code Dynamic
Workflow emission is an optional Claude-only optimization; do not depend on it
for behavior that must work across Codex, Cursor, Copilot, Junie, or OpenCode.

## Default path (always try first)

1. Call `route_task(task=...)` (MCP: Threnody).
2. Read `execution_hint.mode`:
   - `host_native` — use host `Task`/`Agent` (`host_spawn` in response).
   - `delegate` — only when `delegation_utilities_enabled` is true and targets are utilities.
3. Follow `recommended_action` and `host_native_model` / `host_native_method`.

## Handoff precedence

When a prior `plan_task`, `fleet_plan`, or `execute_swarm` response includes `host_spawn_waves` or `host_execution_contract: spawn_subagents`, **always spawn** per agent — `host_native_method: direct_edit` does not apply.

`direct_edit` is only for lone `route_task` results with no pending handoff (`execution_hint.active_handoff` is not set).

## Utility-only delegation

`execute_subtask` is **not** general cross-CLI orchestration.

| Condition | Result |
|-----------|--------|
| `delegation_utilities_enabled: false` | No utility delegation; host-native only |
| Enabled + `provider_id` in allowlist | OpenCode, Aider, local loopback endpoints |
| Host CLI as target (Copilot, Codex, Cursor, Junie, Claude) | **Hard blocked** — use `host_spawn` |
| Same-host caller + same provider | `HostNativeRequired` + `host_spawn` payload |

Config (installed `~/.local/lib/threnody/config.yaml`):

```yaml
providers:
  delegation_utilities_enabled: false
  delegation_utilities:
    - opencode
    - aider
```

## Routing guard (guarded mode)

When `routing_policy` is `guarded` (opt-in via `mode: guarded` or per-shell override):

- Call `route_task` before non-exempt code edits.
- Markdown and instruction files are exempt by default.
- After routing, **host-native execution first** — the guard is coordination, not a delegation mandate.

## Antigravity execution

When the caller is `antigravity`, the `host_spawn_waves` response uses agy's native format:

```json
{
  "Subagents": [{
    "TypeName": "low",
    "Role": "Implementer",
    "Prompt": "Implement feature X...",
    "Workspace": "inherit",
    "Model": "gemini-3.5-flash",
    "Effort": "low"
  }]
}
```

Pass this directly to `invoke_subagent`. The workspace isolation mode is auto-selected:

- `branch` — isolated git branch (multi-file writes)
- `share` — light worktree (read-only reviews)
- `inherit` — shared workspace (simple edits)

For specialized tasks, use `define_subagent` to create agents on-the-fly, then invoke them. See `threnody-plan` for details.

## Learning-triggered agent suggestions

The learning loop tracks patterns across sessions. When it identifies a recurring pattern (10+ matches), it suggests spawning a specialized agent:

```
Threnody detected you've done 12 API migrations across 8 sessions.
Spawn an `api-migration-agent` with the learned pattern? (y/n)
```

The suggested agent uses the learned pattern as its system prompt and is optimized for that specific task type.

### Checking for suggestions

Call `learning_pattern_health()` (MCP: Threnody) to view:

- **mature_patterns** — patterns ready for drafting (10+ matches)
- **draft_proposals** — agents awaiting approval
- **active_agents** — currently active learned agents

### Approving suggestions

When a draft agent is approved:

1. It's added to `~/.gemini/config/agents/` as a static agent definition
2. It can be invoked via `invoke_subagent(agent_name="learned-agent-name")`
3. It appears in the `/agents` panel

### Managing learned agents

- Use `/agents` to view all agents (including learned ones)
- Learned agents are marked with `source: "learning"` in their metadata
- To disable a learned agent, remove it from `~/.gemini/config/agents/`

### Cost benefits

Learned agents:
- Use the optimal tier for their specific task type
- Have specialized prompts that reduce token waste
- Accumulate context over time, improving efficiency

Check cost savings via `threnody-cost` to see the impact of learned agents.

## Related skills

- **Planning entry (start here for multi-step work):** `threnody-plan`
- Normal execution after plan: `threnody-task`
- Large parallel fanout with persistence: `threnody-swarm`
- Full-stack frontend + backend + API: `threnody-fullstack`
- Monitor utility `execute_subtask`: `threnody-subtasks`
