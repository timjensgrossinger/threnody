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
| **Utility delegation (opt-in)** | Operator enables utility targets | `execute_subtask` → OpenCode, Aider, local loopback endpoints only |

Claude Code is a **router-only host** by default: Threnody registers as MCP
inside it but does not subprocess `claude` for delegated work unless the
operator opts in via `providers.router_only_allow_execution`.

`route_task` and `plan_task` return `host_spawn` / `host_spawn_waves` plus
`execution_hint` with `mode: host_native | delegate` and `delegation_targets`.
`execute_subtask` is utility-delegation only for host callers (`HostNativeRequired` for same-host; host→host blocked).
`execute_swarm` defaults to `host_native` plan handoff (`awaiting_host_execution`).

## Orchestration surfaces

| Tool | Role | Host execution |
|------|------|----------------|
| `route_task` | Classify tier + `execution_hint` | Optional single `host_spawn` |
| `plan_task` / `decompose_task` | LLM decomposition | `host_spawn_waves` |
| `fleet_plan` | Decompose + fleet command strings | Embedded `host_spawn` per agent |
| `execute_swarm` | Swarm contract (persistence, budget, resume) | `host_spawn_waves` (default) |
| `execute_subtask` | Single utility offload (opt-in) | Blocked for same-host; utilities only |

Normal orchestration is **plan → host_spawn_waves → host Task/Agent per wave**.
Swarms add topology selection, budget preview, swarm telemetry, and optional
delegate-mode coordinator rounds.

## Swarm topologies

Threnody uses a **single coordinator** per wave when star topology is active —
not multi-queen / peer consensus.

| Topology | Behavior |
|----------|----------|
| `linear` / `dag` | Dependency-ordered waves; default runtime loop |
| `hierarchical` | Parent–child subtask trees |
| `star` | One coordinator + workers; coordinator verdict rounds (**delegate mode**) |
| `auto` | Heuristic selection from urgency/complexity |

Default for MCP hosts: `swarm.host_execution_mode: host_native` — Threnody plans;
the host shell spawns agents. Set `host_execution_mode: delegate` only for
legacy subprocess orchestrator fanout.

Project skills in `.cursor/skills/` document workflows: `threnody-plan`,
`threnody-task`, `threnody-swarm`, `threnody-fullstack`, `threnody-routing`,
`threnody-subtasks`.

## Positioning (vs heavy swarm platforms)

Threnody optimizes for **token-cheap coordination** on the CLIs you already use:

| Approach | Threnody default |
|----------|------------------|
| Alignment | **Contract-first DAG** (`consumes` / `produces`) + integration wave |
| Execution | **Host-native** `host_spawn_waves` — host Task/Agent, not subprocess fanout |
| Swarm consensus | **Not** multi-queen voting or BFT/Raft — optional single coordinator in legacy delegate mode only |
| Scale | Tier routing + waves, not 100+ fixed agent types |

See [COMPETITIVE.md](COMPETITIVE.md) for operator-oriented comparison with
large swarm meta-harnesses. Federation and hive-mind consensus remain out of
scope ([RELEASE_LIMITATIONS.md](RELEASE_LIMITATIONS.md)).

## Full-stack parallel pattern (contract-first DAG)

To build frontend, backend, and API concurrently:

1. **Wave 1:** API contract (OpenAPI, shared DTOs) — `produces: api-contract`
2. **Wave 2:** Frontend + backend **in parallel** — both `depends_on` wave 1, `consumes: api-contract`
3. **Wave 3:** Integration / smoke tests — `depends_on` wave 2 outputs

Use `topology: dag` (or `auto`) on `execute_swarm`, or ask the planner for the
same shape via `decompose_task`.

**Consensus:** no built-in multi-agent voting. Contract-first handoff is
recommended; optional coordinator star (delegate mode) or a final integration
wave replaces peer consensus. Host-native swarms rely on operator or lead-agent
review after parallel waves.

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
