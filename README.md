<h1 align="center">Threnody</h1>
<h3 align="center">Lean MCP coordination for host-native swarms, receipts, replayable workflows, and approval-gated learning</h3>

<p align="center"><sub>
  host-native execution · token-savings receipts · run cards · workflow blueprints · curated task packs · cross-session memory
</sub></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License"></a>
  <a href="https://github.com/timjensgrossinger/threnody/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/timjensgrossinger/threnody/ci.yml?branch=main" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue" alt="Python">
  <img src="https://img.shields.io/badge/MCP-stdio-green" alt="MCP">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-v0.3.0--alpha.2-orange" alt="Release"></a>
</p>

<p align="center">
  <strong>Plan in Threnody.</strong> Route, decompose, and swarm via MCP.<br>
  <strong>Execute in the host.</strong> Agent/Task subagents from <code>host_spawn</code> / <code>host_spawn_waves</code>.<br>
  <strong>Keep receipts.</strong> Every route, plan, and swarm can return a token-savings receipt and exportable run card.<br>
  <strong>Replay what works.</strong> Approved agents and workflow blueprints turn recurring patterns into cheap repeatable flows.<br>
  <strong>Scale deliberately.</strong> Broad reviews can fan out one agent per file; concurrency is controlled separately from swarm size.
</p>

---

## Install

**Requires:** Python 3.10+, macOS or Linux, and at least one host AI CLI (`gh`, `claude`, `codex`, `cursor-agent`, `junie`, or `opencode`).

### Claude Code plugin marketplace (recommended)

```bash
claude plugin marketplace add timjensgrossinger/threnody
claude plugin install threnody@threnody
```

Bundles the MCP server and nine routing skills. No shell restart needed.

### MCP registry via `uvx`

Works with any MCP-aware host (Claude Code, Copilot, Codex, Cursor):

```bash
claude mcp add threnody -- uvx threnody-mcp
```

Or install directly:

```bash
pip install threnody-mcp        # then: python3 -m threnody.mcp_server
uvx threnody-mcp                # run without installing
```

On first tool call with no config present, Threnody returns setup instructions. Run `threnody settings` to complete configuration.

### Full CLI install (shell aliases + ghc wrappers)

```bash
curl -fsSL https://raw.githubusercontent.com/timjensgrossinger/threnody/main/install.sh | bash
```

Or clone and install:

```bash
git clone https://github.com/timjensgrossinger/threnody.git
cd threnody
./install.sh
```

Adds `ghc`, `ghcs`, `ghce` shell aliases and syncs routing instructions to all connected host CLIs. Restart your shell after install.

Plugin-only install (skips shell aliases):

```bash
curl -fsSL https://raw.githubusercontent.com/timjensgrossinger/threnody/main/install.sh | bash -s -- --plugin-mode
```

**Provider terms:** Threnody is not affiliated with or endorsed by any AI provider. Credentials stay in provider-native stores; you configure auth in each host CLI. See [docs/LEGAL.md](docs/LEGAL.md) for operator responsibilities.

Docs: [plugin install guide](docs/PLUGIN_INSTALL.md) · [limitations](docs/RELEASE_LIMITATIONS.md) · [legal](docs/LEGAL.md) · [architecture](docs/ARCHITECTURE.md)

---

## What is Threnody?

**Threnody** is a local-first **MCP meta-harness** for developer workflows. It is intentionally not a giant hosted swarm platform: Threnody stays small, auditable, and host-native. Register it in Claude Code, Copilot CLI, Codex, Cursor, Junie, or OpenCode — Threnody **plans and routes** in MCP; the **host shell executes** via `host_spawn` / `host_spawn_waves` (Agent or Task subagents). When a handoff includes `host_spawn_waves`, spawn subagents — do not substitute direct edits on planned files.

`execute_subtask` is **utility delegation only** (opt-in): OpenCode, Aider, and local loopback endpoints — never to other host CLIs. Same-host work returns `HostNativeRequired` with an actionable spawn payload. Claude Code is a **router-only host** by default.

On Claude Code, an opt-in mode (`routing_policy.shells.claude-code.workflow_emit`) emits **tier-aware [Dynamic Workflow](https://code.claude.com/docs/en/workflows) scripts** for fan-out plans: each `agent()` routes to its Threnody tier model, where a vanilla workflow would run every agent on the session model. Recurring orchestration shapes can be approved and saved as permanent `/workflow` commands.

Search terms that describe the same project: **MCP orchestrator**, **meta-harness**, **multi-agent coding**, **swarm coordination**, **self-learning agents**, **Copilot / Claude / Codex orchestration**.

Short version: Threnody is for operators who want the benefits of multi-agent
coding without a permanent agent army, a second hosted control plane, or hidden
coordination-token drift.

---

## Claude Code and provider compliance (default posture)

With **default configuration**, Threnody matches Anthropic's intended MCP pattern for Claude Code:

| Control | Default behavior |
|---|---|
| **Execution** | Host runs work via **Agent** and direct edits — Threnody returns `host_spawn` / `host_spawn_waves`, not subprocess `claude -p` loops |
| **Router-only** | Claude Code is a coordination anchor; Threnody does not delegate to it as a subprocess backend |
| **Same-host `execute_subtask`** | Returns **`HostNativeRequired`** with an actionable spawn payload |
| **Host→host delegation** | **`HostDelegationBlocked`** — no subprocess to Copilot, Codex, Cursor, Junie, or Claude from MCP |
| **Utility delegation** | Off by default; opt-in targets OpenCode, Aider, and local loopback endpoints only |
| **Routing policy** | Advisory by default for all shells — `route_task` recommended, not mandatory; opt into `routing_policy.mode: guarded` for coordination gates ([docs/HOOKS.md](docs/HOOKS.md)) |

**Operator opt-in risk:** Enabling `providers.router_only_allow_execution` for Claude Code can subprocess the CLI. With **subscription OAuth**, that pattern is documented as **high policy risk** in [docs/LEGAL.md](docs/LEGAL.md). API-key billing is lower operational risk but still bills outside Threnody's telemetry.

Threnody documents operator responsibilities; it does not provide legal certification. Verify your auth mode and provider terms before changing defaults.

---

## Project skills

Nine repo-local skills under [`skills/`](skills/) guide MCP workflows from any connected host (Cursor, Copilot, Claude Code, Codex, etc.). Host shells discover them via repo symlinks (`.cursor/skills`, `.claude/skills`) and the skill index in [copilot-instructions.md](copilot-instructions.md).

| Skill | Use when |
|---|---|
| [threnody-plan](skills/threnody-plan/SKILL.md) | Plan-only or plan-then-execute; choose waves vs swarm |
| [threnody-routing](skills/threnody-routing/SKILL.md) | `route_task`, routing guard, host-native vs utility delegation |
| [threnody-task](skills/threnody-task/SKILL.md) | `plan_task`, `decompose_task`, `fleet_plan`, `host_spawn_waves` |
| [threnody-swarm](skills/threnody-swarm/SKILL.md) | `execute_swarm`, topology, budget preview, resume |
| [threnody-swarm-review](skills/threnody-swarm-review/SKILL.md) | Complexity-gated review swarm — one agent per file × dimension, ranked report (read-only) |
| [threnody-fast-review](skills/threnody-fast-review/SKILL.md) | Fast broad review swarm — one read-only agent per file plus synthesis |
| [threnody-workflow](skills/threnody-workflow/SKILL.md) | Consensus swarm via tier-aware Dynamic Workflows; save pre-tuned, zero-config `/workflow` commands (claude-code) |
| [threnody-fullstack](skills/threnody-fullstack/SKILL.md) | Contract-first parallel frontend + backend + API |
| [threnody-subtasks](skills/threnody-subtasks/SKILL.md) | Monitor opt-in utility `execute_subtask` runs |

---

## Why Threnody?

| | |
|---|---|
| **Plan in MCP, execute in host** | `route_task` / `plan_task` return `host_spawn` / `host_spawn_waves` — spawn Agent/Task subagents in the host shell. |
| **Learn over time** | Pattern tracking, draft agents, and an approval queue before anything goes live. |
| **Swarm when needed** | `execute_swarm` defaults to `host_native`: wave plans hand off to the host; no subprocess fanout by default. |
| **Utility delegation (opt-in)** | `execute_subtask(provider_id=…)` to OpenCode, Aider, or local endpoints only; host→host delegation is blocked. |
| **Planning mode** | `threnody-plan` skill — plan-only vs plan-then-execute; routes to waves or swarms without extra coordinator rounds. |
| **Spend discipline** | Host-native execution uses existing CLI entitlements; `cost_receipt` shows selected path, high-tier counterfactual, and skipped coordination calls. |
| **Operator receipts** | `inspect_run_receipt` exports JSON, Markdown, or HTML run cards with plan, waves, cost receipt, policy decisions, and outcome fields. |
| **Curated packs** | `list_task_packs` / `plan_task_pack` provide focused presets such as `security-review`, `test-gap`, and `release-check` without a giant agent catalog. |
| **Replay proven workflows** | `workflow_blueprint_export` saves host-native waves; `workflow_blueprint_run` replays them later without a fresh planner call. |
| **Reasonable fanout** | `swarm.max_agents: -1` means no default hard size cap; use `parallelism.max_workers` for concurrency/backpressure, or opt into a hard ceiling when needed. |

---

## Who this is for

- Developers who want MCP coordination (swarms, memory, learning) inside their existing AI CLI host
- Teams standardizing on one MCP layer across Copilot, Claude Code, Codex, or Cursor
- Operators who want local-first state, explicit provider diagnostics, and approval-gated learned agents
- Anyone who wants credentials to stay in provider-native stores — Threnody does not manage API keys

## Who this is not for

- A single chat assistant for casual coding questions — one CLI agent is enough; Threnody adds orchestration overhead
- A hosted SaaS with a support SLA — solo open-source project; GitHub issues are how support happens
- Compliance-certified agent orchestration — Threnody documents operator responsibilities; it does not ship audit-grade compliance bundles
- Non-coding LLM workflows (research, writing, data pipelines) — Threnody wraps CLI coding agents specifically
- Anyone who needs Threnody to guarantee provider ToS compliance — your deployment posture depends on which CLIs and routing patterns you enable

---

## Agents that learn — with your approval

Threnody watches recurring work patterns, drafts reusable agents when evidence is strong, and **waits for you to approve** before anything goes live.

```text
host execution → track patterns → draft agent → YOU approve → activate → auto-match future work
```

- **No auto-promotion** — drafts never become active without explicit approval
- **Conservative gates** — recurrence, quality score, and low rework must all agree before drafting
- **Project vs shared vs cost lanes** — project-specific patterns activate sooner; shared patterns need stronger evidence; **cost_lane** drafts prefer free/low-tier execution when recurrence and quality gates pass
- **Inspect everything** — `learning_agent_summary`, `learning_pattern_health`, and redacted `learning_audit_log` MCP tools

```bash
threnody inspect approvals --project .
threnody inspect approvals approve 12 --project . --operator you
```

---

## How it works

```text
Host shell (Claude / Copilot / Codex / Cursor / …)
  → route_task / plan_task   tier + host_spawn / host_spawn_waves
  → host executes            Agent or Task subagents, direct edits
  → utility delegation       execute_subtask → OpenCode / Aider / local (opt-in)
  → swarm / learning         execute_swarm (host_native default), memory_*, learning_*
```

1. **You give a task** to your MCP host shell.
2. **Threnody scores complexity** → low / medium / high tier (no extra LLM call on the hot path).
3. **`route_task` or `plan_task` returns spawn metadata** — `host_spawn` for single-agent work, `host_spawn_waves` for multi-step plans.
4. **The host runs the work** — Claude Code uses **Agent**; other shells use **Task**. Same-host `execute_subtask` returns `HostNativeRequired`.
5. **Swarms or utility delegation** — `execute_swarm` returns a host-native wave plan by default; optional `execute_subtask` only for utility backends when enabled in config.

### What leaves your machine

By default, Threnody is local-first:

- Routing state, telemetry, and caches stay in local SQLite (`~/.local/lib/threnody/`)
- The MCP server talks to your host shell over stdio — no Threnody-hosted control plane
- Outbound traffic comes from the provider CLIs you invoke (Anthropic, OpenAI, GitHub, Google, etc.)

If you route to a network LLM endpoint, re-do the network review. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/LEGAL.md](docs/LEGAL.md).

### Honest limitations

- Threnody orchestrates tools that can execute arbitrary code with your user permissions
- Provider risk is real — routing policy reduces it, but cannot change a provider's underlying trust model or terms
- Cost rank is a routing hint, not a bill estimate
- Realistic enforcement outcome is account suspension or rate limits, not necessarily litigation

Full list: [docs/RELEASE_LIMITATIONS.md](docs/RELEASE_LIMITATIONS.md)

---

## Spend less across your CLIs

Threnody is built for operators who want **token discipline** across Copilot, Claude, Codex, Cursor, and other installed CLIs — not a second API bill from the coordination layer.

```text
route_task / plan_task  →  host_spawn / host_spawn_waves  →  host Agent/Task
execute_subtask         →  utility targets only (opt-in; host→host blocked)
execute_swarm           →  host_native default; delegate opt-in
```

1. **`route_task` / `plan_task`** classify work and return `host_spawn` metadata plus `execution_hint.economics`.
2. **Host-native first** — Agent/Task subagents and direct edits use your existing CLI entitlements; no Threnody subprocess loop for same-host work.
3. **Utility delegation (opt-in)** — `execute_subtask(provider_id=…)` to OpenCode, Aider, or local loopback endpoints when `delegation_utilities_enabled: true`.
4. **Contract-first by default, consensus opt-in** — parallel waves + verify gates out of the box; enable persona-diverse multi-queen consensus per swarm (`swarm.consensus`) when you want adversarial agreement (see [docs/COMPETITIVE.md](docs/COMPETITIVE.md)).
5. **Measure locally** — `inspect_spend`, `threnody inspect spend`, and `threnody gain` aggregate delegated-subtask savings from `cost_telemetry`.
6. **Guarded coordination (opt-in)** — set `routing_policy.mode: guarded` to require `route_task` before code edits; Claude Code can install a PreToolUse hook. See [docs/HOOKS.md](docs/HOOKS.md). Default is advisory for all shells.

Workflow guide: [docs/COST_SAVINGS.md](docs/COST_SAVINGS.md)

---

## Feature highlights

| | Feature | What it does |
|---|---|---|
| 🎯 | **Tier routing** | Heuristic complexity scoring + `host_spawn` / `execution_hint` for host-native work |
| 🧠 | **Learning loop** | Pattern tracking → draft agents → approval queue → plan-time context injection for matching work |
| 🐝 | **Swarm orchestration** | `execute_swarm` returns `host_spawn_waves` by default (`awaiting_host_execution`); broad reviews can fan out one agent per file; `parallelism.max_workers` throttles concurrency separately from plan size |
| 🚀 | **Batch learning capture** | `host_native.report_mode: batch` (default) eliminates per-wave `report_host_wave` round-trips — learning is captured to a per-run JSONL log (PostToolUse hook, zero model tokens) and imported once at terminal, keeping swarms as fast as native subagent spawning. `inline` reverts to per-wave ingest |
| ⚡ | **Dynamic Workflows** | Opt-in (claude-code): fan-out plans emit a tier-aware [Workflow](https://code.claude.com/docs/en/workflows) script — each `agent()` routes to its Threnody tier model; recurring shapes export to permanent `/workflow` commands (`report_workflow_result`, `routing_policy.shells.claude-code.workflow_emit`) |
| 🧾 | **Receipts and run cards** | `cost_receipt` plus `inspect_run_receipt(format=json|markdown|html)` record plan, waves, model rationale, skipped calls, policy decisions, and outcome fields |
| 🧩 | **Task packs and blueprints** | Curated packs cover common workflows; successful host-native runs can export to workflow blueprints and replay without another planner call |
| 💾 | **Cross-session memory** | `memory_*` MCP tools backed by local SQLite — shared across all MCP hosts via `~/.local/lib/threnody/cache.db` |
| 🔌 | **MCP-native** | 40+ tools over stdio JSON-RPC; works with any MCP-compatible host shell |
| 🗳️ | **Multi-queen consensus** | Opt-in persona-diverse review queens + lazy judge arbitration (`swarm.consensus`); host-native or subprocess star |
| 🔀 | **Utility delegation** | Opt-in `execute_subtask` to OpenCode, Aider, local endpoints; host→host blocked |
| 📋 | **Planning skills** | Nine repo skills under `skills/` — start with `threnody-plan` for plan-only workflows |
| 📈 | **Adaptive thresholds** | EMA-based threshold learning from `record_outcome` (pass `task_id` from `route_task`; enable per-project learning) |
| 🛡️ | **Write safety** | Path validation, outside-workspace grant model + audit trail |
| 🔒 | **Guarded routing** | Optional coordination gate + Claude PreToolUse hooks (`routing_policy.mode: guarded`; advisory is default) |

### Cross-CLI memory

All installed MCP hosts share one SQLite store at `~/.local/lib/threnody/cache.db` (WAL). Use these conventions so Claude Code, Copilot, Cursor, and other shells read the same keys:

| Scope | When to use | Cross-CLI tip |
|---|---|---|
| `global` | Machine-wide coordination | Fully shared — no `project_id` |
| `project` | Repo-specific state | Pass a **stable absolute path** as `project_id`, not `"."` (each host resolves `"."` to its own active workspace) |
| `task` | One run or wave | Share explicit `task_id` strings across hosts |

`shared/memory.canonical_project_id()` resolves relative paths to absolute paths under the active workspace. Do not store secrets in memory — any connected host can read or overwrite keys.

### Adaptive routing

1. `route_task` returns `task_id` and persists `complexity_score` in telemetry.
2. After work completes, call `record_outcome(task_id=..., outcome=accepted|revised|rejected|reworked)`.
3. Enable learning per project: `threnody tune set learning_enabled true --project .`
4. Warm-path EMA updates apply adaptive thresholds at classify time once band and project sample gates are met (requires `cwd` on `route_task`).

---

## Supported providers

| Provider | Binary | Role | Notes |
|---|---|---|---|
| **Claude Code** | `claude` | Host (router-only) | MCP coordination anchor; executes via **Agent** / direct edits — no default subprocess delegation |
| **GitHub Copilot** | `gh` | Host | Host executes via **Task**; coordinates in MCP |
| **OpenAI Codex** | `codex` | Host | Host-native execution via Task tool |
| **Cursor** | `cursor-agent` | Host | Host-native execution via Task tool |
| **OpenCode** | `opencode` | Host + utility | Host-native when MCP host; utility delegation target when enabled |
| **JetBrains Junie** | `junie` | Host / legacy paths | Host-native when MCP host; not a default `execute_subtask` target from other hosts |
| **Aider** | `aider` | Utility | Secondary adapter; opt-in utility delegation |
| **Amazon Q / Kiro** | `q` / `kiro` | Legacy / detect | Secondary adapter; not default host→host delegation |
| **Mistral Vibe** | `vibe` | Legacy / detect | Secondary adapter |
| **Blackbox AI** | `blackbox` | Legacy / detect | When CLI installed |
| **Windsurf** | `windsurf` | detect only | Never selected for execution |

Run `threnody inspect status --project . --details` for your live provider matrix.

Full compatibility matrix: [docs/PROVIDER_COMPATIBILITY.md](docs/PROVIDER_COMPATIBILITY.md)

---

## See it in action

**1. Plan in Threnody**

```json
plan_task("add JWT auth with tests")
→ host_spawn_waves: [
    { "wave": 1, "agents": [{ "tool": "Agent", "tier": "medium", "prompt": "…", "target_files": ["src/auth.py"] }] },
    { "wave": 2, "agents": [{ "tool": "Agent", "tier": "low", "prompt": "…", "target_files": ["tests/test_auth.py"] }] }
  ]
```

**2. Execute in the host (Claude Code example)**

```
📋 Wave 1 — spawn Agent (sonnet) → src/auth.py
📋 Wave 2 — spawn Agent (haiku)  → tests/test_auth.py
```

**3. Optional utility delegation (opt-in)**

```
# Enable providers.delegation_utilities_enabled in config.yaml first
execute_subtask(prompt="…", tier="low", provider_id="opencode")
→ delegates to OpenCode utility backend

execute_subtask(prompt="…", tier="medium")  # same-host caller
→ HostNativeRequired + host_spawn payload

execute_subtask(provider_id="codex")  # always blocked
→ HostDelegationBlocked
```

---

## Shell commands

```bash
ghc agent "implement JWT auth for the user service"   # multi-agent waves
ghcs "how to list files recursively in python"        # quick routed call
threnody inspect status --project . --details       # provider readiness
threnody-watch                                      # live TUI monitor
```

Full reference: [docs/CLI.md](docs/CLI.md)

---

## Documentation

| Doc | Contents |
|---|---|
| [Plugin Install](docs/PLUGIN_INSTALL.md) | uvx, plugin marketplace, and `--plugin-mode` setup |
| [MCP Tools](docs/MCP_TOOLS.md) | All 40+ MCP tool surfaces |
| [CLI Reference](docs/CLI.md) | Shell aliases and operator commands |
| [Architecture](docs/ARCHITECTURE.md) | Trust boundaries and local-first design |
| [Configuration](config.example.yaml) | Safe starting config (copy to `~/.local/lib/threnody/config.yaml`) |
| [Model Discovery](docs/MODEL_DISCOVERY.md) | Live catalogs, tier pins, cost ranks |
| [Routing Quality](docs/ROUTING_QUALITY.md) | Eval methodology and accuracy |
| [Routing accuracy report](docs/ROUTING_ACCURACY.md) | Generated fixture stats (`python3 -m shared.routing_report --write-docs`) |
| [Host routing hooks](docs/HOOKS.md) | Claude PreToolUse guard script |
| [Release Limitations](docs/RELEASE_LIMITATIONS.md) | Beta scope, privacy, roadmap |
| [Legal and Provider Terms](docs/LEGAL.md) | Operator responsibilities and provider links |
| [Cost savings workflows](docs/COST_SAVINGS.md) | Host-native vs utility delegation and operator commands |
| [Competitive positioning](docs/COMPETITIVE.md) | Threnody vs heavy swarm platforms; contract-first alignment |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Common fixes |

---

## Beta status

Public alpha **v0.3.0-alpha.2** — MCP tool schemas may change between releases; pin a git tag for stability. See [CHANGELOG.md](CHANGELOG.md).

- macOS and Linux; `zsh` and `bash`
- Windows not supported by the installer
- Provider behavior depends on locally installed CLI versions and entitlements

---

## Running tests

```bash
THRENODY_TEST_MODE=1 python3 -m pytest tests/ -q
THRENODY_TEST_MODE=1 python3 -m shared.routing_eval
python3 scripts/check_release_archive.py
```

---

## Uninstall

```bash
~/.local/lib/threnody/uninstall.sh
~/.local/lib/threnody/uninstall.sh --purge-data
```

---

## Legal and provider terms

Threnody is an independent open-source project. It is not affiliated with,
endorsed by, or sponsored by Anthropic, OpenAI, GitHub, Google, Cursor,
JetBrains, or any other provider named in this repository.

Threnody is provided **"AS IS"** under the [Apache License 2.0](LICENSE) (no
warranty; limitation of liability). You are solely responsible for determining
whether your routing patterns comply with each provider's current terms.

**Default execution model:** Threnody plans in MCP; host shells execute via
`host_spawn` / `host_spawn_waves` (Agent or Task subagents). Same-host
`execute_subtask` returns `HostNativeRequired`. Optional utility delegation
(OpenCode, Aider, local endpoints) requires `providers.delegation_utilities_enabled: true`.
Host→host subprocess delegation is not supported.

Operator responsibilities and provider links: [docs/LEGAL.md](docs/LEGAL.md)

## License

Licensed under the [Apache License, Version 2.0](LICENSE). Third-party attributions in [NOTICE](NOTICE).

Built by [@timjensgrossinger](https://github.com/timjensgrossinger).
