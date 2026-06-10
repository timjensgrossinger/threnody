# Architecture and Trust Boundaries

Threnody is a local MCP server that routes work to AI CLIs already installed
on the operator's machine.

```text
MCP host shell
  Claude Code / Copilot / Gemini / Codex / Cursor / Junie / OpenCode
        |
        | JSON-RPC over stdio
        v
Threnody mcp_server.py
        |
        +-- shared.router        task tier classification
        +-- shared.discovery     provider detection and execution
        +-- shared.orchestrator  planning, swarm, and verification gates
        +-- shared.db            local SQLite state
        |
        +-- local provider CLIs and loopback endpoints
```

## Trust Boundaries

- Host shells are trusted only as local MCP clients. Caller detection is used
  for anti-recursion and policy selection, not authentication.
- Provider CLIs execute as the current operating-system user.
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
queues, learned agents, and memory snapshots in local SQLite. The database is
not sent to provider CLIs except through task prompts explicitly created by the
operator or the MCP host.

## Release-Sensitive Invariants

- Generated provider inventories and runtime status files are not source
  artifacts.
- Missing required verify-gate tools fail instead of passing silently.
- OpenCode is low-only by default; Junie is medium-only by default.
- Windsurf is detect-only and never selected for execution.
