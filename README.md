<p align="center">
  <img src="docs/assets/hero.svg" alt="Threnody — local-first MCP meta-harness for AI coding CLIs" width="100%">
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
  <strong>Plan in Threnody.</strong> Route, decompose, and swarm via MCP.<br>
  <strong>Execute in the host.</strong> Agent/Task subagents from <code>host_spawn</code> / <code>host_spawn_waves</code>.<br>
  <strong>Delegate cross-backend only.</strong> <code>execute_subtask(provider_id=…)</code> when another CLI should run the work.
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

**Requires:** Python 3.10+, macOS or Linux, and at least one host AI CLI (`gh`, `claude`, `codex`, `cursor-agent`, `junie`, or `opencode`).

Restart your shell, then connect from Claude Code, Copilot CLI, Codex, Cursor, Junie, or OpenCode — Threnody registers as an MCP server automatically.

**Provider terms:** Threnody is not affiliated with or endorsed by any AI provider. Credentials stay in provider-native stores; you configure auth in each host CLI. See [docs/LEGAL.md](docs/LEGAL.md) for operator responsibilities.

Docs: [limitations](docs/RELEASE_LIMITATIONS.md) · [legal](docs/LEGAL.md) · [architecture](docs/ARCHITECTURE.md)

---

## What is Threnody?

**Threnody** is a local-first **MCP meta-harness** for developer workflows. Register it in Claude Code, Copilot CLI, Codex, Cursor, Junie, or OpenCode — Threnody **plans and routes** in MCP; the **host shell executes** via `host_spawn` / `host_spawn_waves` (Agent or Task subagents, direct edits).

`execute_subtask` is **utility delegation only** (opt-in): OpenCode, Aider, and local loopback endpoints — never to other host CLIs. Same-host work returns `HostNativeRequired` with an actionable spawn payload. Claude Code is a **router-only host** by default.

Search terms that describe the same project: **MCP orchestrator**, **meta-harness**, **multi-agent coding**, **swarm coordination**, **self-learning agents**, **Copilot / Claude / Codex orchestration**.

---

## Why Threnody?

| | |
|---|---|
| **Plan in MCP, execute in host** | `route_task` / `plan_task` return `host_spawn` / `host_spawn_waves` — spawn Agent/Task subagents in the host shell. |
| **Learn over time** | Pattern tracking, draft agents, and an approval queue before anything goes live. |
| **Swarm when needed** | `execute_swarm` defaults to `host_native`: wave plans hand off to the host; no subprocess fanout by default. |
| **Utility delegation (opt-in)** | `execute_subtask(provider_id=…)` to OpenCode, Aider, or local endpoints only; host→host delegation is blocked. |
| **Spend discipline** | Host-native execution uses existing CLI entitlements; local telemetry via `inspect_spend` and `threnody gain`. |

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
  → cross-backend only       execute_subtask(provider_id=…) → other CLIs
  → swarm / learning         execute_swarm (host_native default), memory_*, learning_*
```

1. **You give a task** to your MCP host shell.
2. **Threnody scores complexity** → low / medium / high tier (no extra LLM call on the hot path).
3. **`route_task` or `plan_task` returns spawn metadata** — `host_spawn` for single-agent work, `host_spawn_waves` for multi-step plans.
4. **The host runs the work** — Claude Code uses **Agent**; other shells use **Task**. Same-host `execute_subtask` returns `HostNativeRequired`.
5. **Cross-backend or swarms** — explicit `provider_id` for another CLI; `execute_swarm` returns a host-native wave plan by default.

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
execute_subtask         →  cross-backend only (explicit provider_id)
execute_swarm           →  host_native default; delegate opt-in
```

1. **`route_task` / `plan_task`** classify work and return `host_spawn` metadata plus `execution_hint.economics`.
2. **Host-native first** — Agent/Task subagents and direct edits use your existing CLI entitlements; no Threnody subprocess loop for same-host work.
3. **Cross-backend only** — `execute_subtask(provider_id=…)` when another CLI should run the work.
4. **Measure locally** — `inspect_spend`, `threnody inspect spend`, and `threnody gain` aggregate delegated-subtask savings from `cost_telemetry`.
5. **Guarded coordination (Claude Code default)** — `route_task` before code edits; PreToolUse hook blocks unclassified premium edits. See [docs/HOOKS.md](docs/HOOKS.md). Set `routing_policy.mode: advisory` to disable.

Workflow guide: [docs/COST_SAVINGS.md](docs/COST_SAVINGS.md)

---

## Feature highlights

| | Feature | What it does |
|---|---|---|
| 🎯 | **Tier routing** | Heuristic complexity scoring + `host_spawn` / `execution_hint` for host-native work |
| 🧠 | **Learning loop** | Pattern tracking → draft agents → approval queue → auto-match future work |
| 🐝 | **Swarm orchestration** | `execute_swarm` returns `host_spawn_waves` by default (`awaiting_host_execution`) |
| 💾 | **Cross-session memory** | `memory_*` MCP tools backed by local SQLite |
| 🔌 | **MCP-native** | ~43 tools over stdio JSON-RPC; works with any MCP-compatible host shell |
| 🔀 | **Cross-backend delegation** | `execute_subtask(provider_id=…)` to Copilot, Codex, Cursor, endpoints, Aider, … |
| 📈 | **Adaptive thresholds** | EMA-based threshold learning from routing outcomes |
| 🛡️ | **Write safety** | Path validation, outside-workspace preview gate, audit trail |
| 🔒 | **Guarded routing** | Optional coordination gate + Claude PreToolUse hooks (`routing_policy.mode: guarded`) |

---

## Supported providers

| Provider | Binary | Role | Notes |
|---|---|---|---|
| **Claude Code** | `claude` | Host (router-only) | MCP coordination anchor; executes via **Agent** / direct edits |
| **GitHub Copilot** | `gh` | Host | Host executes via **Task**; coordinates in MCP |
| **OpenAI Codex** | `codex` | Host | Host-native execution via Task tool |
| **Cursor** | `cursor-agent` | Host | Host-native execution via Task tool |
| **OpenCode** | `opencode` | Host + utility | Host-native when MCP host; utility delegation target when enabled |
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

**Default execution model:** Threnody plans in MCP; host shells execute via
`host_spawn` / `host_spawn_waves` (Agent or Task subagents). Same-host
`execute_subtask` returns `HostNativeRequired`. Optional utility delegation
(OpenCode, Aider, local endpoints) requires `providers.delegation_utilities_enabled: true`.
Host→host subprocess delegation is not supported.

Operator responsibilities and provider links: [docs/LEGAL.md](docs/LEGAL.md)

## License

Licensed under the [Apache License, Version 2.0](LICENSE). Third-party attributions in [NOTICE](NOTICE).

Built by [@timjensgrossinger](https://github.com/timjensgrossinger).
