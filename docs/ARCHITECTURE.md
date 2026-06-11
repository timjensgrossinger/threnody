# Architecture and Trust Boundaries

Threnody is a local MCP meta-harness: the host shell executes work; Threnody
coordinates routing, planning, swarms, memory, and learning.

```text
MCP host shell
  Claude Code / Copilot / Gemini / Codex / Cursor / Junie / OpenCode
        |
        | JSON-RPC over stdio
        v
Threnody mcp_server.py  (coordination layer)
        |
        +-- shared.router        task tier classification
        +-- shared.orchestrator  planning, swarm, verification gates
        +-- shared.agents        pattern learning and approval queue
        +-- shared.memory        cross-session memory store
        +-- shared.discovery     provider detection and delegated execution
        +-- shared.db            local SQLite state
        |
        +-- host-native execution (Task tool, direct edits, host backends)
        +-- optional delegation → other CLIs / loopback / network endpoints
```

## Two-path execution model

| Path | When | Mechanism |
|------|------|-----------|
| **Host-native** | Default for MCP host shells | Host Task tool, direct edits, host-configured local/API backends |
| **Delegated** | Operator routes to another backend | `execute_subtask` → Copilot, Codex, Cursor, endpoints, Aider, etc. |

Claude Code is a **router-only host** by default: Threnody registers as MCP
inside it but does not subprocess `claude` for delegated work unless the
operator opts in via `providers.router_only_allow_execution`.

`route_task` and `plan_task` return `host_spawn` / `host_spawn_waves` plus
`execution_hint` with `mode: host_native | delegate` and `delegation_targets`.
`execute_subtask` is cross-backend only for host callers (`HostNativeRequired` otherwise).
`execute_swarm` defaults to `host_native` plan handoff (`awaiting_host_execution`).

## Guarded vs advisory routing

| Mode | Meaning |
|------|---------|
| **guarded** | Require `route_task` before non-exempt code edits; Claude Code installs PreToolUse hooks by default |
| **advisory** | Recommend routing for non-trivial work; direct edits allowed without a guard |

Guarded mode is a **coordination gate**, not a delegation mandate. After routing,
follow `execution_hint` — host-native execution first; `execute_subtask` only when
delegating to another backend.

`mode: strict` in `config.yaml` is a deprecated alias for `guarded`.

## Trust Boundaries

- Host shells are trusted only as local MCP clients. Caller detection is used
  for policy selection, not authentication.
- Delegated provider CLIs execute as the current operating-system user.
- Runtime configuration lives outside the source tree in an untracked
  `config.yaml`.
- Network endpoint providers are never auto-discovered beyond loopback.
  Network endpoints must be explicitly configured and must use HTTPS.
- File writes are validated against workspace boundaries. Outside-workspace
  writes require explicit grants and are audited.
- Verification commands are operator configuration. They execute as direct
  argument lists without a shell; shell features belong in an explicit script.

## Local State

Threnody stores routing cache, telemetry, provider readiness, approval
queues, learned agents, swarm checkpoints, and memory snapshots in local SQLite.
The database is not sent to provider CLIs except through task prompts explicitly
created by the operator or the MCP host.

## Release-Sensitive Invariants

- Generated provider inventories and runtime status files are not source
  artifacts.
- Missing required verify-gate tools fail instead of passing silently.
- OpenCode is low-only by default; Junie is medium-only by default.
- Windsurf is detect-only and never selected for execution.
- `claude-code` is router-only by default for delegated execution.
