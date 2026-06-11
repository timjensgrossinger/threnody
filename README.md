<p align="center">
  <img src="docs/assets/hero.svg" alt="Threnody — local-first MCP meta-harness — plan in Threnody, execute via `host_spawn` / `host_spawn_waves` in the host shell; `execute_subtask` is cross-backend only for AI coding CLIs" width="100%">
</p>

<h1 align="center">Threnody</h1>
<h3 align="center">Local-first MCP meta-harness — host executes, Threnody coordinates swarms, memory, and learning</h3>

<p align="center"><sub>
  MCP coordination · self-learning agents · swarm orchestration · cross-session memory · optional delegation
</sub></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License"></a>
  <a href="https://github.com/timjensgrossinger/threnody/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/timjensgrossinger/threnody/ci.yml?branch=main" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue" alt="Python">
  <img src="https://img.shields.io/badge/MCP-stdio-green" alt="MCP">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-v1.0.0--beta.1-orange" alt="Release"></a>
</p>

<p align="center">
  <strong>Host executes.</strong> Task tool, direct edits, host-configured local/API backends.<br>
  <strong>Threnody coordinates.</strong> Route, plan, swarm, memory, and approval-gated learning.<br>
  <strong>Delegate when needed.</strong> Optional subprocess routing to other installed CLIs.
</p>

---

## Install in 2 minutes

```bash
curl -fsSL https://raw.githubusercontent.com/timjensgrossinger/threnody/main/install.sh | bash
```

Or clone and install:

```bash
git clone https://github.com/timjensgrossinger/threnody.git
cd threnody
./install.sh
```

**Requires:** Python 3.10+, macOS or Linux, and at least one host AI CLI (`gh`, `claude`, `gemini`, `codex`, `cursor-agent`, `junie`, or `opencode`).

Restart your shell, then connect from Claude Code, Copilot CLI, Gemini, Codex, Cursor, or Junie — Threnody registers as an MCP server automatically.

**Provider terms:** Threnody is not affiliated with or endorsed by any AI provider. Credentials stay in provider-native stores; you configure auth in each host CLI. See [docs/LEGAL.md](docs/LEGAL.md) for operator responsibilities.

Docs: [limitations](docs/RELEASE_LIMITATIONS.md) · [legal](docs/LEGAL.md) · [architecture](docs/ARCHITECTURE.md)

---

## What is Threnody?

**Threnody** is a local-first **MCP meta-harness** for developer workflows. Register it in Claude Code, Copilot CLI, Gemini, Codex, Cursor, or Junie — the **host shell executes** work while Threnody **coordinates** routing, planning, swarms, cross-session memory, and approval-gated learned agents.

Optional **delegation** via `execute_subtask` routes to other installed CLIs (Copilot, Codex, Cursor, endpoints, Aider, …). Claude Code is a **router-only host** by default: a coordination anchor, not a subprocess delegation target.

Search terms that describe the same project: **MCP orchestrator**, **meta-harness**, **multi-agent coding**, **swarm coordination**, **self-learning agents**, **Copilot / Claude / Gemini orchestration**.

---

## Why Threnody?

| | |
|---|---|
| **Coordinate in the host** | `route_task` returns tier guidance and `execution_hint` — host Task tool and direct edits first. |
| **Learn over time** | Pattern tracking, draft agents, and an approval queue before anything goes live. |
| **Swarm when needed** | Decompose hard work into dependency-ordered waves with linear, DAG, hierarchical, or star topologies. |
| **Delegate optionally** | `execute_subtask` routes to other backends when you want cross-CLI execution. |
| **Spend discipline** | Tier routing, free low-tier paths, and local spend telemetry (`inspect_spend`, `threnody gain`). |

---

## Who this is for

- Developers who want MCP coordination (swarms, memory, learning) inside their existing AI CLI host
- Teams standardizing on one MCP layer across Copilot, Claude Code, Gemini, Codex, or Cursor
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
execute subtask → track patterns → draft agent → YOU approve → activate → auto-match future work
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
Host shell (Claude / Copilot / Gemini / …)
  → route_task          tier + execution_hint (host-native first)
  → host executes       Task tool, direct edits, host backends
  → optional delegate   execute_subtask → other CLIs / endpoints
  → swarm / learning    execute_swarm, memory_*, learning_*
```

1. **You give a task** to your MCP host shell.
2. **Threnody scores complexity** → low / medium / high tier (no extra LLM call on the hot path).
3. **`route_task` returns `execution_hint`** — host-native guidance by default; delegation targets when routable backends exist.
4. **Complex tasks decompose** into waves — `execute_swarm` or host Task agents; optional `execute_subtask` for cross-backend work.

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

Threnody is built for operators who want **token discipline** across Copilot, Claude, Gemini, Codex, Cursor, and other installed CLIs — not a second API bill from the coordination layer.

1. **`route_task`** classifies work and returns `execution_hint` with host-native vs delegate guidance plus `economics` (`is_free`, `cost_rank`, `cheapest_path_rationale`).
2. **Host-native first** — Task tool and direct edits use your existing CLI entitlements.
3. **Delegate only when needed** — `execute_subtask` picks the cheapest routable backend.
4. **Measure locally** — `inspect_spend`, `threnody inspect spend`, and `threnody gain` aggregate delegated-subtask savings from `cost_telemetry`.
5. **Guarded coordination (Claude Code default)** — `route_task` before code edits; PreToolUse hook blocks unclassified premium edits. See [docs/HOOKS.md](docs/HOOKS.md). Set `routing_policy.mode: advisory` to disable.

Workflow guide: [docs/COST_SAVINGS.md](docs/COST_SAVINGS.md)

---

## Feature highlights

| | Feature | What it does |
|---|---|---|
| 🎯 | **Tier routing** | Heuristic complexity scoring + `execution_hint` for host-native vs delegated work |
| 🧠 | **Learning loop** | Pattern tracking → draft agents → approval queue → auto-match future work |
| 🐝 | **Swarm orchestration** | `execute_swarm` with linear, DAG, hierarchical, and star topologies |
| 💾 | **Cross-session memory** | `memory_*` MCP tools backed by local SQLite |
| 🔌 | **MCP-native** | ~43 tools over stdio JSON-RPC; works with any MCP-compatible host shell |
| 🔀 | **Optional delegation** | `execute_subtask` to Copilot, Codex, Cursor, endpoints, Aider, … |
| 📈 | **Adaptive thresholds** | EMA-based threshold learning from routing outcomes |
| 🛡️ | **Write safety** | Path validation, outside-workspace preview gate, audit trail |
| 🔒 | **Guarded routing** | Optional coordination gate + Claude PreToolUse hooks (`routing_policy.mode: guarded`) |

---

## Supported providers

| Provider | Binary | Role | Notes |
|---|---|---|---|
| **Claude Code** | `claude` | Host (router-only) | MCP coordination anchor; host executes by default |
| **GitHub Copilot** | `gh` | Host + delegation | Core host; routable for cross-backend work |
| **OpenAI Codex** | `codex` | Host + delegation | Host shell + subprocess execution |
| **Cursor** | `cursor-agent` | Host + delegation | Host shell + subprocess execution |
| **OpenCode** | `opencode` | Delegation | Low-tier auto-route by default |
| **JetBrains Junie** | `junie` | Delegation | Medium-tier auto-route by default |
| **Aider** | `aider` | Delegation | Secondary adapter |
| **Amazon Q / Kiro** | `q` / `kiro` | Delegation | Secondary adapter |
| **Mistral Vibe** | `vibe` | Delegation | Secondary adapter |
| **Blackbox AI** | `blackbox` | Delegation | When CLI installed |
| **Windsurf** | `windsurf` | detect only | Never selected for execution |

Run `threnody inspect status --project . --details` for your live provider matrix.

Full compatibility matrix: [docs/PROVIDER_COMPATIBILITY.md](docs/PROVIDER_COMPATIBILITY.md)

---

## See it in action

**Before each wave:**

```
📋 Wave 1 — Foundation files
┌─────────┬──────┬─────────────────────┬──────────────────┬─────────────────────────────┐
│ Agent # │ Tier │ Model               │ Provider         │ Target files                │
├─────────┼──────┼─────────────────────┼──────────────────┼─────────────────────────────┤
│ 1       │ low  │ gpt-5-mini          │ GitHub Copilot   │ config.py                   │
│ 2       │ low  │ o4-mini             │ OpenAI Codex     │ models.py                   │
│ 3       │ med  │ sonnet              │ Claude Code      │ main.py                     │
└─────────┴──────┴─────────────────────┴──────────────────┴─────────────────────────────┘
```

**After all waves:**

```
📊 Build complete — 3 agents, 1 wave
   GitHub Copilot: 1 agent (gpt-5-mini, free)
   OpenAI Codex:   1 agent (o4-mini)
   Claude Code:    1 agent (sonnet, ~13k tokens)
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
| [MCP Tools](docs/MCP_TOOLS.md) | All 41 MCP tool surfaces |
| [CLI Reference](docs/CLI.md) | Shell aliases and operator commands |
| [Architecture](docs/ARCHITECTURE.md) | Trust boundaries and local-first design |
| [Configuration](config.example.yaml) | Safe starting config (copy to `~/.local/lib/threnody/config.yaml`) |
| [Model Discovery](docs/MODEL_DISCOVERY.md) | Live catalogs, tier pins, cost ranks |
| [Routing Quality](docs/ROUTING_QUALITY.md) | Eval methodology and accuracy |
| [Routing accuracy report](docs/ROUTING_ACCURACY.md) | Generated fixture stats (`python3 -m shared.routing_report --write-docs`) |
| [Host routing hooks](docs/HOOKS.md) | Claude PreToolUse guard script |
| [Release Limitations](docs/RELEASE_LIMITATIONS.md) | Beta scope, privacy, roadmap |
| [Legal and Provider Terms](docs/LEGAL.md) | Operator responsibilities and provider links |
| [Cost savings workflows](docs/COST_SAVINGS.md) | Host-native vs delegate decision tree and operator commands |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Common fixes |

---

## Beta status

Public beta **v1.0.0-beta.1** — MCP tool schemas may change between releases; pin a git tag for stability. See [CHANGELOG.md](CHANGELOG.md).

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

**Default execution model:** host shells execute via Task tool and direct edits.
Claude Code is a router-only coordination anchor — not a default
`execute_subtask` targets. Override only via `providers.router_only_allow_execution`.

Operator responsibilities and provider links: [docs/LEGAL.md](docs/LEGAL.md)

## License

Licensed under the [Apache License, Version 2.0](LICENSE). Third-party attributions in [NOTICE](NOTICE).

Built by [@timjensgrossinger](https://github.com/timjensgrossinger).
