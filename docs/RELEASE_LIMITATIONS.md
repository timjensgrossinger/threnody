# Release Limitations and Roadmap

## Known Alpha Limitations

- Windows is not supported by the installer or process-control helpers.
- Provider behavior depends on locally installed CLI versions and account
  entitlements.
- Live provider smoke tests are machine-specific and remain a release gate.
- MCP transport-disconnect cleanup still needs an explicit regression test.
- Branch protection and final release archive inspection are repository-hosting
  operations and cannot be completed from the local source tree alone.
- OpenCode defaults to low-only routing.
- Junie defaults to medium-only routing.
- Windsurf is detection-only.

## Honest limitations

Plain-language constraints that matter in practice:

- Threnody orchestrates CLIs that can execute arbitrary code with your user
  permissions — it is not a sandbox
- Provider risk is real; routing policy reduces it but cannot change a
  provider's underlying trust model
- Cost rank is a routing hint, not a bill estimate
- Documentation and risk tiers are planning aids, not certifications or legal
  opinions
- Solo open-source project — no vendor SLA; GitHub issues are the support channel

## Comparison Boundaries

Threnody is not positioned as a replacement for a specific AI coding tool or a
full agentic platform (for example Ruflo-style federation, vector RAG marketplaces,
web UI goal planners, or plugin ecosystems). It is a **local-first MCP meta-harness**
for operators who already use one or more AI CLIs — the host shell executes work;
Threnody coordinates routing, swarms, memory, and spend discipline.

Do not compare against audit-grade compliance orchestrators on features Threnody
does not ship (HMAC audit chains, regulatory export bundles, signed agent cards,
cross-machine federation meshes, etc.).

### Explicit non-goals (vs full meta-harness platforms)

- Agent federation across machines or trust boundaries
- Hosted web UI or GOAP-style goal planner frontends
- Plugin marketplace at platform scale (30+ plugins)
- Vector RAG / HNSW knowledge graph as a core product surface
- WASM sandbox agents or Rust-native inference engines
- Background worker daemons with dozens of auto-triggered hooks

Threnody focuses on: **local MCP coordination**, **host-native execution**,
**eval-backed tier routing**, **optional cross-CLI delegation**, and
**operator-visible spend telemetry**.

Comparisons should be limited to observable behavior:

- Local-first MCP routing across installed CLIs.
- Cost-aware tier selection and spend telemetry (`inspect_spend`, `threnody gain`).
- Provider diagnostics and explicit readiness reasons.
- Approval-gated learned agents.
- Workspace write auditing and preview handling.

Avoid unsupported market claims, provider quality rankings, or claims about
private provider quotas that are not available through stable APIs.

## Provider compliance boundaries

Threnody cannot certify routing patterns as fully compliant with every provider
terms of service. Provider terms, policies, and enforcement may change at any
time without notice; Threnody cannot guarantee continued compatibility with any
provider's rules. See [docs/LEGAL.md](LEGAL.md) for the full risk-tier guide.

- **Highest risk:** routing Claude Pro/Max subscription OAuth to `claude -p`
  subprocesses (from any host, including Claude Code when
  `router_only_allow_execution` is enabled). June 2026 Agent SDK credit caps
  may apply.
- **Lower risk (operator-accepted):** `router_only_allow_execution` with
  Claude Code configured for API-key billing — pay-per-token, not subscription
  quota. Threnody cannot detect auth mode; verify before enabling.
- **Lower risk:** routing through each host's own official CLI on its own
  subscription, local loopback LLMs, or explicitly configured HTTPS network
  endpoints you operate
- **Google models:** use Vertex AI or Google AI Studio API directly — not CLI
  subprocess delegation
- **Team use:** each operator should use their own subscriptions; do not run a
  shared routing service that executes provider CLIs on behalf of others

Do not claim affiliation with or endorsement by any AI provider.

## Privacy Model

- Threnody does not require central service credentials.
- Provider CLIs receive the prompts they are asked to execute.
- Local telemetry, routing history, learning state, and caches stay in SQLite
  unless the operator exports or prompts with them.
- Secret fields are redacted from public audit surfaces where structured data
  is returned.

## Cost-Routing Assumptions

Cost rank is a routing hint, not a bill estimator. It combines bundled defaults,
provider metadata, and operator overrides. Subscription status and provider
quota windows can change independently from Threnody.

## Roadmap

- Add explicit MCP transport-disconnect cancellation tests.
- Complete live smoke matrix for supported providers.
- Add Linux and macOS clean install/reinstall/uninstall CI jobs with real shell
  environments.
- Add a managed command for project-local OpenCode deregistration guidance.
- Publish versioned release archives after archive inspection and secret scans.
