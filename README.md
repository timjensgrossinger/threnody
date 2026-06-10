<h1 align="center">Threnody</h1>
<h3 align="center">Multi-agent MCP orchestrator &amp; LLM router for AI coding CLIs — Copilot, Claude Code, Gemini, Cursor, Codex, and more</h3>

<p align="center"><sub>
  AI agent orchestration · MCP server · model routing · cost-aware tier routing · wave-based parallel execution
</sub></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License"></a>
  <a href="https://github.com/timjensgrossinger/threnody/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/timjensgrossinger/threnody/ci.yml?branch=main" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue" alt="Python">
  <img src="https://img.shields.io/badge/MCP-stdio-green" alt="MCP">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-v1.0.0--beta.1-orange" alt="Release"></a>
</p>

<p align="center">
  <strong>No Threnody-managed API keys.</strong> Runs through CLI subscriptions you already have.<br>
  <strong>Cost-aware.</strong> Boilerplate on free models; hard problems on premium tiers.<br>
  <strong>Parallel.</strong> Multi-file work decomposes into dependency-ordered waves.
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

**Provider terms:** Threnody is not affiliated with or endorsed by any AI provider. You are responsible for complying with each provider's terms of service. Provider terms, policies, and enforcement may change at any time without notice. See [docs/LEGAL.md](docs/LEGAL.md) before routing across subscriptions.

---

## What is Threnody?

**Threnody** is a local-first **AI agent orchestration** layer and **MCP server** for developer workflows. It acts as an **LLM router** and **multi-agent CLI** coordinator: score task complexity, pick the cheapest authenticated provider per tier, decompose hard work into parallel waves, and expose ~43 MCP tools to any compatible host shell.

Search terms that describe the same project: **model routing**, **agent router**, **MCP orchestrator**, **multi-agent coding**, **CLI model selection**, **cost-aware AI routing**, **Copilot / Claude / Gemini orchestration**.

---

## Why Threnody?

| | |
|---|---|
| **Save money** | Route simple edits to free/low-tier models. Reserve opus/sonnet-class models for work that needs reasoning. |
| **Use what you have** | Works with GitHub Copilot, Claude Code, Gemini CLI, Codex, Cursor, Junie, OpenCode, Aider, Amazon Q/Kiro, and more — pick the cheapest authenticated CLI per task. |
| **See everything** | Every wave shows agent, tier, model, provider, and target files before and after execution. |

---

## Agents that learn — with your approval

Threnody watches recurring work patterns, drafts reusable agents when evidence is strong, and **waits for you to approve** before anything goes live.

```text
execute subtask → track patterns → draft agent → YOU approve → activate → auto-match future work
```

- **No auto-promotion** — drafts never become active without explicit approval
- **Conservative gates** — recurrence, quality score, and low rework must all agree before drafting
- **Project vs shared lanes** — project-specific patterns activate sooner; shared patterns need stronger evidence
- **Inspect everything** — `learning_agent_summary`, `learning_pattern_health`, and redacted `learning_audit_log` MCP tools

```bash
threnody inspect approvals --project .
threnody inspect approvals approve 12 --project . --operator you
```

---

## How it works

1. **You give a task** to Copilot CLI, Claude Code, Gemini, or another MCP host.
2. **Threnody scores complexity** → low / medium / high tier (no extra LLM call on the hot path).
3. **Discovery picks the cheapest** authenticated provider for that tier (excludes the caller to prevent recursion).
4. **Complex tasks decompose** into waves — independent subtasks run in parallel, dependents wait for prior waves.

---

## Feature highlights

| | Feature | What it does |
|---|---|---|
| 🎯 | **Tier routing** | Heuristic complexity scoring + intent modifiers (`quick` → cheaper, `production` → higher quality) |
| 🔍 | **Live discovery** | Scans installed CLIs, checks auth, ranks models by bundled cost data, caches in SQLite |
| 🌊 | **Wave orchestration** | `decompose_task` → parallel waves → integration verify; linear, DAG, hierarchical, and star topologies |
| 🔌 | **MCP-native** | ~43 tools over stdio JSON-RPC; works with any MCP-compatible host shell |
| 🧠 | **Warm-path eval** | Background rework detection and quality scoring after subtasks complete |
| 📈 | **Adaptive thresholds** | EMA-based threshold learning from routing outcomes |
| 🛡️ | **Write safety** | Path validation, outside-workspace preview gate, audit trail |
| 👁️ | **Operator CLI** | `threnody inspect`, `threnody tune`, `threnody doctor`, `threnody-watch` |

---

## Supported providers

| Provider | Binary | Routeable | Notes |
|---|---|---|---|
| **GitHub Copilot** | `gh` | ✅ | Core host; includes free `gpt-5-mini` low tier |
| **Claude Code** | `claude` | ✅ | haiku / sonnet / opus |
| **Gemini CLI** | `gemini` | ✅ | flash-lite / flash / pro |
| **OpenCode** | `opencode` | ✅ | Low-tier auto-route by default |
| **OpenAI Codex** | `codex` | ✅ | Host shell + execution |
| **Cursor** | `cursor-agent` | ✅ | Host shell + execution |
| **JetBrains Junie** | `junie` | ✅ | Medium-tier auto-route by default |
| **Aider** | `aider` | ✅ | Secondary adapter |
| **Amazon Q / Kiro** | `q` / `kiro` | ✅ | Secondary adapter |
| **Mistral Vibe** | `vibe` | ✅ | Secondary adapter |
| **Blackbox AI** | `blackbox` | ✅ | When CLI installed |
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
│ 2       │ low  │ gemini-2.5-flash-lite│ Gemini CLI      │ models.py                   │
│ 3       │ med  │ sonnet              │ Claude Code      │ main.py                     │
└─────────┴──────┴─────────────────────┴──────────────────┴─────────────────────────────┘
```

**After all waves:**

```
📊 Build complete — 3 agents, 1 wave
   GitHub Copilot: 1 agent (gpt-5-mini, free)
   Claude Code:    1 agent (sonnet, ~13k tokens)
   Gemini CLI:     1 agent (flash-lite, free)
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
| [MCP Tools](docs/MCP_TOOLS.md) | All 43 MCP tool surfaces |
| [CLI Reference](docs/CLI.md) | Shell aliases and operator commands |
| [Architecture](docs/ARCHITECTURE.md) | Trust boundaries and local-first design |
| [Configuration](config.example.yaml) | Safe starting config (copy to `~/.local/lib/threnody/config.yaml`) |
| [Model Discovery](docs/MODEL_DISCOVERY.md) | Live catalogs, tier pins, cost ranks |
| [Routing Quality](docs/ROUTING_QUALITY.md) | Eval methodology and accuracy |
| [Release Limitations](docs/RELEASE_LIMITATIONS.md) | Beta scope, privacy, roadmap |
| [Legal and Provider Terms](docs/LEGAL.md) | ToS risk tiers, routing guidance, team use |
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

- Use at your own risk and in compliance with each provider's terms of service
- Provider terms, policies, and enforcement may change at any time without
  notice; Threnody cannot guarantee continued compatibility with any provider's
  rules
- Cross-routing Claude Pro/Max subscription OAuth from non-Claude hosts carries
  the highest provider-policy risk; see [docs/LEGAL.md](docs/LEGAL.md)
- Claude Code → Claude Code routing is blocked by default; explicit opt-in only

## License

Licensed under the [Apache License, Version 2.0](LICENSE). Third-party attributions in [NOTICE](NOTICE).

Built by [@timjensgrossinger](https://github.com/timjensgrossinger).
