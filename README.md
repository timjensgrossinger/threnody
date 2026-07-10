<!-- mcp-name: io.github.timjensgrossinger/threnody -->

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
  <strong>Plan in Threnody</strong> — route, decompose, swarm via MCP.<br>
  <strong>Execute in the host</strong> — Agent/Task subagents from <code>host_spawn</code> / <code>host_spawn_waves</code>.<br>
  <strong>Keep receipts</strong> — every route, plan, and swarm can return a token-savings receipt and exportable run card.<br>
  <strong>Replay what works</strong> — approved agents and workflow blueprints turn recurring patterns into cheap repeatable flows.
</p>

---

## What is Threnody?

A local-first **MCP meta-harness** for developer workflows — small, auditable, and host-native, not a hosted swarm platform. Register it in Claude Code, Copilot CLI, Codex, Cursor, Junie, or OpenCode: Threnody **plans and routes** in MCP; the **host shell executes** via `host_spawn` / `host_spawn_waves` (Agent or Task subagents). When a handoff includes `host_spawn_waves`, spawn subagents — do not substitute direct edits on planned files.

`execute_subtask` is **utility delegation only** (opt-in): OpenCode, Aider, and local loopback endpoints — never other host CLIs. Same-host work returns `HostNativeRequired` with a spawn payload; host→host delegation returns `HostDelegationBlocked`. Claude Code is a **router-only host** by default.

On Claude Code, an opt-in mode (`routing_policy.shells.claude-code.workflow_emit`) emits **tier-aware [Dynamic Workflow](https://code.claude.com/docs/en/workflows) scripts** for fan-out plans — each `agent()` routes to its Threnody tier model, where a vanilla workflow runs every agent on the session model. Recurring shapes can be approved and saved as permanent `/workflow` commands.

For operators who want multi-agent coding without a permanent agent army, a second hosted control plane, or hidden coordination-token drift.

---

## Install

**Requires:** Python 3.10+ and macOS or Linux for the supported installation
paths. The full `install.sh` workflow expects at least one host AI CLI (`gh`,
`claude`, `codex`, `cursor-agent`, `junie`, or `opencode`); the packaged MCP
server can start without one for setup and diagnostics.

**Claude Code plugin marketplace** (recommended) — bundles the MCP server and nine routing skills, no shell restart:

```bash
claude plugin marketplace add timjensgrossinger/threnody
claude plugin install threnody@threnody
```

**MCP package via `uvx` or `pip`** — works with any MCP-aware host:

```bash
claude mcp add threnody -- uvx threnody-mcp
pip install threnody-mcp
threnody-mcp
```

The package entry point is a local stdio server. Set
`THRENODY_ALLOW_NO_HOST=1` when deliberately starting it on a machine with no
host CLI.

**Full CLI install** (adds `ghc`/`ghcs`/`ghce` aliases, syncs routing
instructions; restart shell after):

```bash
curl -fsSL https://raw.githubusercontent.com/timjensgrossinger/threnody/main/install.sh | bash
# plugin-only (skips shell aliases): ... | bash -s -- --plugin-mode
```

On first tool call with no config, Threnody returns setup instructions. Run `threnody settings` to finish.

**Provider terms:** Threnody is not affiliated with or endorsed by any AI provider. Credentials stay in provider-native stores; configure auth in each host CLI. See [docs/LEGAL.md](docs/LEGAL.md).

Docs: [plugin install](docs/PLUGIN_INSTALL.md) · [limitations](docs/RELEASE_LIMITATIONS.md) · [legal](docs/LEGAL.md) · [architecture](docs/ARCHITECTURE.md)

---

## Compliance posture (defaults)

With default config, Threnody matches Anthropic's intended MCP pattern for Claude Code:

| Control | Default behavior |
|---|---|
| **Execution** | Host runs work via **Agent** and direct edits — Threnody returns `host_spawn` / `host_spawn_waves`, not subprocess loops |
| **Router-only** | Claude Code is a coordination anchor; not a subprocess backend |
| **Same-host `execute_subtask`** | Returns `HostNativeRequired` with a spawn payload |
| **Host→host delegation** | `HostDelegationBlocked` — no subprocess to other CLIs from MCP |
| **Utility delegation** | Off by default; opt-in targets OpenCode, Aider, and local endpoints only |
| **Routing policy** | Advisory by default — `route_task` recommended, not mandatory; opt into `guarded` for coordination gates ([docs/HOOKS.md](docs/HOOKS.md)) |

**Operator opt-in risk:** Enabling `providers.router_only_allow_execution` can subprocess Claude Code. With subscription OAuth that pattern is documented as **high policy risk** in [docs/LEGAL.md](docs/LEGAL.md). Verify your auth mode and provider terms before changing defaults.

---

## How it works

```text
Host shell (Claude / Copilot / Codex / Cursor / …)
  → route_task / plan_task   tier + host_spawn / host_spawn_waves
  → host executes            Agent or Task subagents, direct edits
  → utility delegation       execute_subtask → OpenCode / Aider / local (opt-in)
  → swarm / learning         execute_swarm (host_native default), memory_*, learning_*
```

1. You give a task to your MCP host shell.
2. Threnody scores complexity → low / medium / high tier (no extra LLM call on the hot path).
3. `route_task` / `plan_task` return spawn metadata — `host_spawn` for single-agent, `host_spawn_waves` for multi-step plans.
4. The host runs the work — Claude Code uses **Agent**; other shells use **Task**.
5. Swarms or utility delegation — `execute_swarm` returns a host-native wave plan by default; `execute_subtask` only for utility backends when enabled.

**Local-first:** routing state, telemetry, and caches stay in local SQLite (`~/.local/lib/threnody/`); the MCP server talks to your host over stdio — no Threnody-hosted control plane. Outbound traffic comes only from the provider CLIs you invoke.

---

## Features

| | Feature | What it does |
|---|---|---|
| 🎯 | **Tier routing** | Heuristic complexity scoring + `host_spawn` / `execution_hint` for host-native work |
| 🧠 | **Learning loop** | Pattern tracking → draft agents → approval queue → plan-time context injection. No auto-promotion; conservative recurrence/quality/rework gates |
| 🐝 | **Swarm orchestration** | `execute_swarm` returns `host_spawn_waves` by default; broad reviews default to one agent per file, deep review opts into file × dimension fanout; `parallelism.max_workers` throttles concurrency separately from plan size |
| ⚡ | **Dynamic Workflows** | Opt-in (claude-code): fan-out plans emit a tier-aware [Workflow](https://code.claude.com/docs/en/workflows) script; recurring shapes export to permanent `/workflow` commands |
| 🧾 | **Receipts and run cards** | `cost_receipt` + `inspect_run_receipt(format=json\|markdown\|html)` record plan, waves, model rationale, skipped calls, policy decisions, outcomes |
| 🧩 | **Task packs and blueprints** | Curated packs (`security-review`, `test-gap`, `release-check`); successful runs export to replayable workflow blueprints |
| 💾 | **Cross-session memory** | `memory_*` MCP tools backed by local SQLite, shared across all MCP hosts |
| 🔌 | **MCP-native** | 53 published tools over stdio JSON-RPC; works with any MCP-compatible host |
| 📈 | **Adaptive thresholds** | EMA threshold learning from `record_outcome` (per-project, opt-in) |
| 🛡️ | **Write safety** | Path validation, outside-workspace grant model + audit trail |
| 🔒 | **Guarded routing** | Optional coordination gate + Claude PreToolUse hooks (`routing_policy.mode: guarded`) |

**Cross-CLI memory:** all hosts share one SQLite store at `~/.local/lib/threnody/cache.db`. Use `global` (no `project_id`), `project` (pass a **stable absolute path**, not `"."`), or `task` (explicit `task_id`) scopes. Do not store secrets — any connected host can read keys.

**Adaptive routing:** `route_task` returns a `task_id`; after work, call `record_outcome(task_id=…, outcome=accepted|revised|rejected|reworked)`. Enable per project with `threnody tune set learning_enabled true --project .`.

---

## Project skills

Nine repo-local skills under [`skills/`](skills/) guide MCP workflows from any host. `install.sh` installs them into provider-native roots (directory-style for Claude Code / Cursor / Codex; flat markdown for Copilot CLI / OpenCode).

| Skill | Use when |
|---|---|
| [threnody-plan](skills/threnody-plan/SKILL.md) | Plan-only or plan-then-execute; waves vs swarm |
| [threnody-routing](skills/threnody-routing/SKILL.md) | `route_task`, routing guard, host-native vs utility delegation |
| [threnody-task](skills/threnody-task/SKILL.md) | `plan_task`, `decompose_task`, `fleet_plan`, `host_spawn_waves` |
| [threnody-swarm](skills/threnody-swarm/SKILL.md) | `execute_swarm`, topology, budget preview, resume |
| [threnody-fast-review](skills/threnody-fast-review/SKILL.md) | Default broad review swarm — one read-only agent per file plus synthesis |
| [threnody-swarm-review](skills/threnody-swarm-review/SKILL.md) | Deep review swarm — file × dimension fanout, optional `[dims=...]`, ranked report |
| [threnody-workflow](skills/threnody-workflow/SKILL.md) | Claude Code tier-aware Dynamic Workflows; save `/workflow` commands |
| [threnody-fullstack](skills/threnody-fullstack/SKILL.md) | Contract-first parallel frontend + backend + API |
| [threnody-subtasks](skills/threnody-subtasks/SKILL.md) | Monitor opt-in utility `execute_subtask` runs |

---

## Supported providers

| Provider | Binary | Role |
|---|---|---|
| **Claude Code** | `claude` | Host (router-only) — executes via Agent / direct edits |
| **GitHub Copilot** | `gh` | Host — executes via Task |
| **OpenAI Codex** | `codex` | Host — Task execution |
| **Cursor** | `cursor-agent` | Host — Task execution |
| **OpenCode** | `opencode` | Host + utility delegation target |
| **JetBrains Junie** | `junie` | Host / legacy paths |
| **Aider** | `aider` | Utility (opt-in delegation) |
| **Amazon Q / Kiro · Mistral Vibe · Blackbox** | `q`/`kiro`/`vibe`/`blackbox` | Secondary adapters / detect |
| **Windsurf** | `windsurf` | Detect only — never executes |

Live matrix: `threnody inspect status --project . --details`. Full table: [docs/PROVIDER_COMPATIBILITY.md](docs/PROVIDER_COMPATIBILITY.md)

---

## See it in action

```json
plan_task("add JWT auth with tests")
→ host_spawn_waves: [
    { "wave": 1, "agents": [{ "tool": "Agent", "tier": "medium", "target_files": ["src/auth.py"] }] },
    { "wave": 2, "agents": [{ "tool": "Agent", "tier": "low", "target_files": ["tests/test_auth.py"] }] }
  ]
```

```text
📋 Wave 1 — spawn Agent (sonnet) → src/auth.py
📋 Wave 2 — spawn Agent (haiku)  → tests/test_auth.py
```

Optional utility delegation (opt-in via `providers.delegation_utilities_enabled`):

```text
execute_subtask(prompt="…", tier="low", provider_id="opencode")  → OpenCode utility backend
execute_subtask(prompt="…", tier="medium")                       → HostNativeRequired + spawn payload
execute_subtask(provider_id="codex")                             → HostDelegationBlocked
```

### Shell commands

```bash
ghc agent "implement JWT auth for the user service"   # multi-agent waves
ghcs "how to list files recursively in python"        # quick routed call
threnody inspect status --project . --details         # provider readiness
threnody-watch                                        # live TUI monitor
```

Full reference: [docs/CLI.md](docs/CLI.md)

---

## Documentation

| Doc | Contents |
|---|---|
| [Plugin Install](docs/PLUGIN_INSTALL.md) | uvx, plugin marketplace, `--plugin-mode` |
| [MCP Tools](docs/MCP_TOOLS.md) | All 40+ MCP tool surfaces |
| [CLI Reference](docs/CLI.md) | Shell aliases and operator commands |
| [Architecture](docs/ARCHITECTURE.md) | Trust boundaries and local-first design |
| [Configuration](config.example.yaml) | Safe starting config |
| [Routing Quality](docs/ROUTING_QUALITY.md) | Eval methodology and accuracy |
| [Host routing hooks](docs/HOOKS.md) | Claude PreToolUse guard script |
| [Cost savings](docs/COST_SAVINGS.md) | Host-native vs utility delegation |
| [Competitive positioning](docs/COMPETITIVE.md) | Threnody vs heavy swarm platforms |
| [Release Limitations](docs/RELEASE_LIMITATIONS.md) | Beta scope, privacy, roadmap |
| [Legal and Provider Terms](docs/LEGAL.md) | Operator responsibilities |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Common fixes |

---

## Beta status

Public alpha **v0.3.0-alpha.2** — MCP tool schemas may change between releases; pin a git tag for stability. macOS and Linux (`zsh`/`bash`); Windows not supported by the installer. See [CHANGELOG.md](CHANGELOG.md).

## Running tests

```bash
THRENODY_TEST_MODE=1 python3 -m pytest tests/ -q
THRENODY_TEST_MODE=1 python3 -m shared.routing_eval
python3 scripts/check_release_archive.py
```

## Uninstall

```bash
~/.local/lib/threnody/uninstall.sh [--purge-data]
```

---

## Legal

Threnody is an independent open-source project, not affiliated with or endorsed by Anthropic, OpenAI, GitHub, Google, Cursor, JetBrains, or any other provider named here. Provided **"AS IS"** under the [Apache License 2.0](LICENSE) (no warranty). You are solely responsible for determining whether your routing patterns comply with each provider's current terms.

Operator responsibilities: [docs/LEGAL.md](docs/LEGAL.md) · Third-party attributions: [NOTICE](NOTICE)

Built by [@timjensgrossinger](https://github.com/timjensgrossinger).
