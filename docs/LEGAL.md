# Legal and Provider Terms

**This document is not legal advice.** It summarizes practical compliance
boundaries for Threnody operators. Provider terms, policies, and enforcement may
change at any time without notice; Threnody cannot guarantee continued
compatibility with any provider's rules. Consult qualified counsel before
commercializing routing or exposing Threnody to many users.

## What Threnody is

Threnody is a **local-first MCP orchestrator** that:

- Runs on the operator's machine
- Invokes **official AI CLIs** the operator has already installed and authenticated
- Routes work across providers by tier, cost rank, and operator configuration
- Stores telemetry and state in local SQLite

Threnody is **not**:

- Affiliated with, endorsed by, or sponsored by Anthropic, OpenAI, GitHub,
  Google, Cursor, JetBrains, or any other AI provider named in documentation
- A reseller or proxy of provider subscriptions
- A substitute for reading each provider's current terms of service

## Operator responsibilities

You are responsible for:

- Complying with every provider subscription, API, and usage policy that applies
  to your accounts
- Using only CLIs and credentials you are entitled to use
- Ensuring your organization's MCP, automation, and data policies permit Threnody
- Keeping secrets out of tracked config files (`config.example.yaml` is a template
  only; runtime secrets belong in untracked `~/.local/lib/threnody/config.yaml`
  or provider-native credential stores)

Threnody does not manage provider API keys by default. Some secondary adapters
(for example Aider) may use operator-supplied API keys when configured.

## Routing risk tiers

Threnody cannot certify any routing pattern as "fully legal." The table below
describes **practical risk posture** for common setups.

| Tier | Pattern | Typical posture |
|------|---------|-----------------|
| **Green** | Host shell executes through its own official CLI | Lower risk — documented MCP/automation surfaces |
| **Green** | Any host → local loopback LLM (Ollama on `127.0.0.1`) | Lower risk — self-hosted inference |
| **Yellow** | Cross-provider routing among Copilot, Codex, Cursor, Gemini | Moderate — use official CLIs; respect org policies |
| **Yellow** | Any host → network LLM endpoint you operate (HTTPS, explicit config) | Moderate — your infrastructure, your policy |
| **Red** | Copilot, Cursor, Codex, or other non-Claude host → `claude -p` on Pro/Max OAuth | **Highest provider risk** — Anthropic prohibits routing consumer subscription OAuth through third-party products |
| **Grey** | Claude Code host → `claude -p` subprocess (same subscription) | **Grey** — blocked by default in Threnody; explicit opt-in only |

### Highest-risk pattern: non-Claude host → Claude Code

When Copilot, Cursor, or Codex is the MCP host and Threnody routes execution to
`claude -p`, the work runs through a **third-party orchestrator** using the
operator's Claude subscription credentials.

Anthropic states that OAuth for Free, Pro, and Max plans is intended for
ordinary use of Claude Code and other native Anthropic applications, and that
developers must not route those credentials through third-party products on
behalf of users.

Reference: [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)

**Recommended mitigation for teams using Copilot, Cursor, or Codex as hosts:**

- Prefer Copilot, Codex, Cursor, Gemini, or local LLMs for routed execution
- Use `preferred_routing_by_caller` and/or `caller_allowlists` to avoid
  selecting `claude-code` from non-Claude hosts (see `config.example.yaml`)
- For automated Claude-tier work from those hosts, use **API-key-backed** paths
  (for example Aider with `ANTHROPIC_API_KEY` or a configured HTTPS endpoint),
  not `claude -p` OAuth subprocess routing

### Grey pattern: Claude Code host → Claude Code

Routing from Claude Code back to `claude -p` on the **same subscription** is
less clearly prohibited than cross-provider Claude routing, but still uncertain:

- Threnody remains a third-party MCP product, not a native Anthropic application
- Nested automated `claude -p` calls may exceed "ordinary individual use"
- Duplicate processes can consume quota twice

**Threnody blocks this by default** via the `claude-code` adapter opt-out in
`shared/discovery.py`. Operators may override only through explicit
`preferred_routing_by_caller` configuration. That override carries Anthropic
ToS risk; prefer API keys for automated cross-session Claude routing.

**Lower-risk Claude Code usage:** keep Threnody as an MCP server inside Claude
Code and route to **other** providers (Copilot, Codex, local LLMs). That does
not route Claude subscription credentials through a third party.

## Per-provider notes

### Anthropic / Claude Code

- Consumer Terms apply to Free, Pro, and Max OAuth usage
- Automated third-party orchestration of subscription OAuth is the main enforcement target
- API key usage through Claude Console follows separate commercial terms
- Links: [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance), [Consumer Terms](https://www.anthropic.com/legal/consumer-terms)

### OpenAI / Codex

- Codex CLI is open source (Apache 2.0) with documented MCP and `codex exec` surfaces
- ChatGPT-plan OAuth through modified or wrapped clients remains a grey area; prefer the official `codex` binary
- Links: [Codex CLI](https://developers.openai.com/codex/cli), [OpenAI Terms](https://openai.com/policies/terms-of-use)

### GitHub Copilot

- Copilot CLI explicitly supports custom MCP servers
- Enterprise and organization policies may disable MCP or specific features
- Links: [About MCP in Copilot](https://docs.github.com/en/copilot/concepts/context/mcp), [Copilot policies](https://docs.github.com/en/copilot/reference/copilot-behavior-control-management)

### Cursor

- Cursor CLI documents headless automation, MCP, and scripting patterns
- Uses the operator's Cursor subscription quota
- Link: [Cursor CLI](https://cursor.com/cli)

### Local and network LLM endpoints

Threnody can route to:

- **Loopback** Ollama or OpenAI-compatible endpoints (`scope: local`)
- **Network** endpoints you configure explicitly (`scope: network`, HTTPS required)

These paths do not invoke provider subscription OAuth. Legal risk is limited to
your own infrastructure policy and any upstream model license terms.

Network endpoints are never auto-discovered beyond loopback. See
[docs/ARCHITECTURE.md](ARCHITECTURE.md) and [docs/MODEL_DISCOVERY.md](MODEL_DISCOVERY.md).

## Internal team guidance

For open-source distribution with free internal team use:

- Each operator should use **their own** provider subscriptions or API keys
- Do not operate a shared credential pool or central routing service that runs
  provider CLIs on behalf of others
- Confirm org MCP and automation policies before installing Threnody in CI or on
  shared machines
- Treat account suspension or rate limiting as the realistic enforcement outcome,
  not necessarily litigation

## Deprecated remote HTTP server

`threnody serve`, `remote_dispatch`, and `remote_job_status` are **deprecated
and unsupported** in this release line. They remain in the tree for compatibility
but are not documented as a supported operator surface.

Exposing Threnody over HTTP increases the risk of operating routing "on behalf
of users" and expands the attack surface. Use local MCP stdio only.

## Commercial use warning

If you later sell Threnody, offer it as a hosted service, or route provider
access for customers:

- Get qualified legal review
- Use provider API keys and commercial contracts, not consumer subscription OAuth
- Do not imply endorsement by any AI provider

## Related documentation

- [Release limitations](RELEASE_LIMITATIONS.md) — beta scope and comparison boundaries
- [Architecture](ARCHITECTURE.md) — trust boundaries and local-first design
- [Security policy](../SECURITY.md) — deployment and credential handling
- [Configuration template](../config.example.yaml) — safer routing examples
