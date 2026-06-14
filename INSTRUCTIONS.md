### Host-native execution (default for MCP host shells)

1. Call `route_task` or `plan_task` / `decompose_task`.
2. Consume `host_spawn` or `host_spawn_waves` from the response.
3. When `host_spawn_waves` or `host_execution_contract: spawn_subagents` is present, spawn one **Agent** or **Task** subagent per agent entry — do not use direct Write/Edit on planned `target_files`.
4. For lone `route_task` results without a pending handoff, direct edits are allowed when `host_native_method` is `direct_edit`.
5. Use `execute_subtask(provider_id=...)` only for utility backends when `delegation_utilities_enabled` is true.
6. `execute_swarm` defaults to `host_native` — run returned waves in the host; no subprocess fanout.
7. Host-native heuristic planning fans out **one agent per file** for webapp/fullstack intent or listed paths (`orchestrator.heuristic_intent_templates`, default true).
8. After scaffold waves, call `expand_host_plan(discovered_files=[...])` or `report_host_wave(expand_plan=true)` for additional file agents.
9. Report waves with `workspace_root` from handoff (`learning_report_contract`), per-agent `task_id`, `spawn_id`, `success`, `touched_files`, and `output_excerpt`; terminal reports must set `outcome` and check `finalize.swarm_outcome`.

Same-host `execute_subtask` returns `HostNativeRequired`.

**Dynamic Workflow emission (opt-in, claude-code only).** With `routing_policy.shells.claude-code.workflow_emit` set, fan-out plans also return `workflow_emit: true` and a `workflow_script` (contract `emit_workflow`). Prefer launching it via the **Workflow** tool (Claude Code v2.1.154+) over `host_spawn_waves`: each `agent()` routes to its Threnody tier model (vanilla Workflow runs every agent on the session model), and it runs in the background. After it returns, call `report_workflow_result(workflow_name, agents[])` so Threnody records telemetry and learns recurring shapes for export as permanent `/workflow` commands. Fall back to `host_spawn_waves` if the Workflow tool is unavailable.

# Threnody — Custom Instructions

Threnody generates AI-shell-specific instruction blocks during installation.
The generated block clearly names the shell it applies to and reflects the
current `routing_policy` in `config.yaml`.

## Routing policy

Configure coordination guard vs advisory routing without editing generated instruction files by
hand:

```yaml
routing_policy:
  mode: default # default | guarded | advisory | custom  (strict is deprecated → guarded)
  shells:
    github-copilot-cli:
      mode: advisory
    claude-code:
      mode: guarded
```

`mode: default` uses Threnody recommendations:

| Shell | Default behavior |
|---|---|
| `claude-code` | Advisory routing — `route_task` recommended but not mandatory; no PreToolUse hook unless you opt into guarded mode |
| `github-copilot-cli` | Advisory routing; direct edits are allowed by default |
| `cursor` | Advisory routing |
| `codex` | Advisory routing |

Use `mode: guarded` to require `route_task` before code edits in generated instructions for all
shells. Use `mode: advisory` to make routing non-mandatory for all shells. Use
`mode: custom` with per-shell overrides when you want mixed behavior.

**Migration:** `mode: strict` is accepted as a deprecated alias for `guarded` (a warning is logged). Re-run `./install.sh` after changing guarded/advisory to refresh managed instruction blocks and the Claude hook entry.

Per-shell profiles may set:

```yaml
routing_policy:
  mode: custom
  shells:
    github-copilot-cli:
      route_task_mandatory: true
      low_tier_execute_subtask: false
      agent_transparency_required: true
      direct_edit_hooks: false
      tier_model_mapping:
        low: gpt-5-mini
        medium: claude-sonnet-4.6
        high: claude-opus-4.6
```

`direct_edit_hooks` is only supported for shells with a real hook surface. Today
that means Claude Code. GitHub Copilot CLI receives advisory instructions by
default and does not receive Claude `PreToolUse` hook language unless a supported
hook surface is added.

## Routing exemptions

Threnody uses an exemption list, not a code-file allowlist. Built-in
exemptions cover Markdown docs (`.md`), Cursor rule docs (`.mdc`), and known AI
assistant instruction files such as `CLAUDE.md`, `GEMINI.md`, `AGENTS.md`,
`copilot-instructions.md`, `.cursorrules`, `.windsurfrules`, and `.clinerules`.
All other filetypes remain routed by default unless explicitly added under
`routing_exceptions` in `config.yaml`.

## Rendering instructions manually

The installer calls the renderer automatically. To inspect or copy a block
manually:

```bash
python3 -m shared.instructions claude-code --config ~/.local/lib/threnody/config.yaml
python3 -m shared.instructions github-copilot-cli --config ~/.local/lib/threnody/config.yaml
python3 -m shared.instructions cursor --config ~/.local/lib/threnody/config.yaml --verbatim
```

The managed block markers remain stable:

| Marker | Shell |
|---|---|
| `<!-- Threnody:claude:start -->` | Claude Code |
| `<!-- Threnody:copilot:start -->` | GitHub Copilot CLI |
| `<!-- Threnody:codex:start -->` | OpenAI Codex |
| `<!-- Threnody:junie:start -->` | JetBrains Junie |

Cursor's `.mdc` rule file is written as a standalone generated document instead
of a marked block.

## Cost discipline surfaces

- **Host hooks:** Claude guarded mode installs `shell/threnody-routing-hook.sh` (see [docs/HOOKS.md](docs/HOOKS.md)).
- **Routing trust:** run `python3 -m shared.routing_report --write-docs` for fixture accuracy stats.
- **Learned agents:** approval-gated drafts may land in the **cost_lane** when low-tier patterns recur with strong quality — prefer free/low execution metadata in drafts. Approved agents inject context during **planning** (`plan_task` / `decompose_task`); they do not auto-select host subagent personas at `route_task` time.

## Cross-session memory (cross-CLI)

All MCP hosts installed from this repo share `~/.local/lib/threnody/cache.db`. Use `memory_*` tools with:

- **`global` scope** — shared machine-wide keys (no `project_id`)
- **`project` scope** — pass a stable **absolute** project root as `project_id` so every shell hits the same namespace (avoid `"."`, which resolves per-host)
- **`task` scope** — requires explicit shared `task_id` strings

`shared/memory.canonical_project_id()` normalizes relative paths under the active workspace.

## Adaptive routing feedback

1. `route_task` returns `task_id` and records `complexity_score` in telemetry (pass `cwd` for project-local adaptive thresholds).
2. Call `record_outcome(task_id=..., outcome=...)` when work finishes.
3. Enable per-project learning: `threnody tune set learning_enabled true --project .`

## Legal and provider terms

Threnody is not affiliated with or endorsed by any AI provider. Operators are
responsible for complying with each provider's terms of service. Provider terms,
policies, and enforcement may change at any time without notice; Threnody cannot
guarantee continued compatibility with any provider's rules.

- See [docs/LEGAL.md](docs/LEGAL.md) for operator responsibilities and provider links
- Host shells execute by default; Claude Code is a router-only coordination anchor
- `execute_subtask` is utility-delegation only (opt-in OpenCode, Aider, local endpoints); host→host delegation is blocked
- Override router-only subprocess delegation via `providers.router_only_allow_execution` only when you accept provider-policy risk
- Safer routing examples live in [config.example.yaml](config.example.yaml)
