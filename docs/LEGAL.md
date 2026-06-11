# Legal and Provider Terms

**This document is not legal advice.** It summarizes operator responsibilities
for Threnody. Provider terms, policies, and enforcement may change at any time
without notice. Consult qualified counsel before commercializing Threnody or
exposing it to many users.

## What Threnody is

Threnody is a **local-first MCP meta-harness** that:

- Runs on the operator's machine as an MCP server over stdio
- Coordinates routing, planning, swarms, memory, and learned agents
- Lets the **host shell execute** work (Task tool, direct edits, host-configured models)
- Optionally **delegates** to other installed CLIs or endpoints via `execute_subtask`

Threnody is **not** affiliated with, endorsed by, or sponsored by any AI provider
named in documentation.

## Credentials and auth

- Threnody does **not** manage provider API keys by default
- Operators configure auth in each host CLI (API key, local model, or plan-backed login)
- Credentials stay in provider-native stores; Threnody reads installed CLI state only
- Some secondary adapters (for example Aider) may use operator-supplied API keys when configured
- Threnody **cannot detect** which billing mode a CLI is using (API key vs subscription OAuth).
  Operators must verify auth and billing source before enabling subprocess overrides.

## Execution model

| Path | Description |
|------|-------------|
| **Host-native** (default) | Host shell runs Task agents, edits, or host-configured backends |
| **Utility delegation (opt-in)** | `execute_subtask` invokes OpenCode, Aider, or local loopback endpoints only |
| **Router-only hosts** | Claude Code is a coordination anchor by default — not a subprocess delegation target |

### Host-native MCP contract (Meta-harness v2)

For MCP host shells, Threnody returns `host_spawn` / `host_spawn_waves` from
`route_task`, `plan_task`, and `execute_swarm`. The host must spawn subagents
via **Agent** (Claude Code) or **Task** (other shells). Same-host
`execute_subtask` returns **`HostNativeRequired`**.

Utility delegation uses `execute_subtask(provider_id=...)` only for OpenCode, Aider,
or local endpoints when `providers.delegation_utilities_enabled` is true.
Threnody does not subprocess to other host CLIs (Copilot, Codex, Cursor, …).

Override router-only defaults with `providers.router_only_allow_execution` in
`config.yaml` only when you accept provider-policy risk. See
[config.example.yaml](../config.example.yaml).

## Provider policy risks

Threnody's default configuration coordinates in the host shell and does not spawn
`claude -p` subprocesses. Policy risk arises only when operators opt into
`router_only_allow_execution`.

| Scenario | Risk tier |
|----------|-----------|
| Default router-only (no subprocess to host CLIs) | Compliant — Threnody coordinates; host executes |
| `router_only_allow_execution: [claude-code]` + **API key** auth in Claude Code | Lower risk — pay-per-token API billing; intended override path when you accept subprocess delegation |
| `router_only_allow_execution: [claude-code]` + **Pro/Max subscription OAuth** | High risk — third-party orchestration of subscription quota; Anthropic restricted this pattern; June 2026 Agent SDK credit caps may apply |
| Google Gemini models via **Vertex AI / AI Studio API** (direct HTTP, not CLI subprocess) | Google's stated third-party integration path — use direct API calls, not CLI subprocess delegation |

Notes:

- API-key override is primarily an **operational** concern (tokens bill silently to
  your API key with no Threnody-side usage dashboard), not a compliance blocker.
- Subscription OAuth subprocess delegation can drain plan quotas and trigger
  provider enforcement. Threnody does not read Claude subscription quota
  programmatically.
- Threnody no longer supports Google Gemini CLI as a provider. Gemini CLI has
  rebranded; for Google models, use Vertex AI or Google AI Studio API directly.

## What leaves the machine

- Orchestration state, telemetry, and learning data stay in local SQLite under
  `~/.local/lib/threnody/`
- Outbound traffic comes from CLIs the operator or host invokes — not from a
  Threnody-hosted control plane
- Network LLM endpoints must be explicitly configured (HTTPS required)

## Operator responsibilities

You are responsible for:

- Complying with every provider API, plan, and usage policy that applies to your accounts
- Using only CLIs and credentials you are entitled to use
- Ensuring your organization's MCP and automation policies permit Threnody
- Keeping secrets out of tracked config files (runtime secrets belong in untracked
  `~/.local/lib/threnody/config.yaml` or provider-native stores)
- Verifying billing mode before enabling `router_only_allow_execution` overrides

Threnody orchestrates tools that can execute arbitrary code with the permissions
of the current user. Routing policy reduces risk; it does not replace reading
each provider's current terms.

## Provider links

| Provider | Terms / docs |
|----------|----------------|
| Anthropic / Claude Code | [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) |
| OpenAI / Codex | [OpenAI Terms](https://openai.com/policies/terms-of-use), [Codex CLI](https://developers.openai.com/codex/cli) |
| GitHub Copilot | [MCP in Copilot](https://docs.github.com/en/copilot/concepts/context/mcp) |
| Google (Vertex AI / AI Studio) | [Google Cloud Terms](https://cloud.google.com/terms), [Google AI Studio](https://ai.google.dev/) |
| Cursor | [Terms of Service](https://cursor.com/terms-of-service) |

## Commercial use

If you sell Threnody, offer it as a hosted service, or route provider access for
customers: get qualified legal review, use commercial API contracts where
required, and do not imply endorsement by any AI provider.

## Third-party CLI agents

Vulnerabilities, outages, and terms changes in third-party CLIs are **out of scope**
for Threnody security reports. Report those to the respective vendor.

## Related documentation

- [Architecture](ARCHITECTURE.md) — trust boundaries and execution paths
- [Release limitations](RELEASE_LIMITATIONS.md) — beta scope
- [Security policy](../SECURITY.md) — deployment and credential handling
- [Configuration template](../config.example.yaml) — delegation and override examples
