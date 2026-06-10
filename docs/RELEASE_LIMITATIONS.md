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

Threnody is not positioned as a replacement for a specific AI coding tool.
It is a local routing and orchestration layer for operators who already use one
or more AI CLIs. Do not compare against audit-grade compliance orchestrators on
features Threnody does not ship (HMAC audit chains, regulatory export bundles,
signed agent cards, etc.).

Comparisons should be limited to observable behavior:

- Local-first MCP routing across installed CLIs.
- Cost-aware tier selection.
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

- **Highest risk:** routing Claude Pro/Max subscription OAuth from a non-Claude
  host (for example Copilot, Cursor, or Codex) to `claude -p`
- **Grey zone:** Claude Code host → Claude Code subprocess on the same
  subscription; blocked by default via adapter opt-out; explicit operator
  opt-in only
- **Lower risk:** routing through each host's own official CLI, local loopback
  LLMs, or explicitly configured HTTPS network endpoints you operate
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
