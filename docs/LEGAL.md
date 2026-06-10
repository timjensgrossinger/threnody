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

## Execution model

| Path | Description |
|------|-------------|
| **Host-native** (default) | Host shell runs Task agents, edits, or host-configured backends |
| **Delegated** | `execute_subtask` invokes other routable CLIs or configured endpoints |
| **Router-only hosts** | Claude Code and Gemini CLI are coordination anchors by default — not subprocess delegation targets |

Override router-only defaults with `providers.router_only_allow_execution` in
`config.yaml` only when you accept provider-policy risk. See
[config.example.yaml](../config.example.yaml).

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

Threnody orchestrates tools that can execute arbitrary code with the permissions
of the current user. Routing policy reduces risk; it does not replace reading
each provider's current terms.

## Provider links

| Provider | Terms / docs |
|----------|----------------|
| Anthropic / Claude Code | [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) |
| OpenAI / Codex | [OpenAI Terms](https://openai.com/policies/terms-of-use), [Codex CLI](https://developers.openai.com/codex/cli) |
| GitHub Copilot | [MCP in Copilot](https://docs.github.com/en/copilot/concepts/context/mcp) |
| Google / Gemini CLI | [Gemini CLI ToS](https://geminicli.com/docs/resources/tos-privacy/) |
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
