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

## Default path (always try first)

1. Call `route_task(task=...)` (MCP: Threnody).
2. Read `execution_hint.mode`:
   - `host_native` — use direct edits or host `Task`/`Agent` (`host_spawn` in response).
   - `delegate` — only when `delegation_utilities_enabled` is true and targets are utilities.
3. Follow `recommended_action` and `host_native_model` / `host_native_method`.

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

When `routing_policy` is `guarded` (default for Claude Code):

- Call `route_task` before non-exempt code edits.
- Markdown and instruction files are exempt by default.
- After routing, **host-native execution first** — the guard is coordination, not a delegation mandate.

## Related skills

- **Planning entry (start here for multi-step work):** `threnody-plan`
- Normal execution after plan: `threnody-task`
- Large parallel fanout with persistence: `threnody-swarm`
- Full-stack frontend + backend + API: `threnody-fullstack`
- Monitor utility `execute_subtask`: `threnody-subtasks`
