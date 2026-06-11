# Provider-Reported Quotas

Threnody can use provider-reported subscription quota windows when a provider
offers a documented, authenticated, machine-readable interface. It does not
scrape web pages, read credential stores, parse terminal UI rendering, or infer
subscription limits from model context windows.

## Provider support

| Provider | Status | Source |
|---|---|---|
| OpenAI Codex | Supported | `codex app-server` RPC `account/rateLimits/read` |
| Claude Code | Interactive only | Usage views and limit errors exist, but no documented structured subscription-quota command. When `router_only_allow_execution` is active on subscription auth, provider usage caps (e.g. June 2026 Agent SDK credits) may apply; Threnody does not read subscription quota programmatically |
| GitHub Copilot CLI | Interactive only | `/usage`, footer, and status-line quota displays exist; billing REST usage does not universally include allowance or remaining quota |
| Cursor | UI only | Account usage is visible in product UI; no documented structured CLI quota API |
| JetBrains Junie | UI only | IDE/Junie license UI shows remaining AI Credits and reset timing; no documented structured CLI quota command |
| OpenCode | Usage only | `opencode stats` reads local token/cost telemetry, not an upstream subscription allowance |
| Amazon Q/Kiro | Interactive only | `/usage` shows current usage and remaining credits; no documented structured quota output |
| Aider | Unsupported | Uses separately configured upstream providers |
| Mistral Vibe | Unsupported | No documented CLI subscription-quota contract used by Threnody |
| Blackbox AI | Unsupported | No documented CLI subscription-quota contract |
| Windsurf | Unsupported | No documented CLI subscription-quota contract |

Unsupported is an explicit result, not an error and not an assumed unlimited
allowance. Provider adapters handle unavailable CLIs, authentication failures,
rate limits, timeouts, and malformed responses without exposing credentials.
Interactive/UI/local-telemetry surfaces are documented in the reason field but
remain `unsupported` for automated routing until the provider publishes a
stable structured contract.

## Normalization

Each `ProviderQuotaSnapshot` records:

- provider and window name
- window duration
- used, remaining, and limit
- unit
- reset and observation timestamps
- source
- confidence and freshness

Codex currently reports `usedPercent`. Threnody stores this as
`unit: percent`, `limit: 100`, and derives the remaining percentage. It does
not invent token or request limits.

Multiple windows are retained independently. For example, a five-hour window
and a weekly window can both influence routing.

## Routing and fallback

Existing `providers.usage_windows` entries remain compatible:

```yaml
providers:
  usage_windows:
    codex:
      - hours: 5
        budget_tokens: 500000
        threshold: 0.8
        action: prefer_alternatives
      - hours: 168
        budget_tokens: 5000000
        threshold: 0.9
        action: hard_exclude
```

For each configured window, routing:

1. Uses a fresh provider-reported ratio when available.
2. Applies `prefer_alternatives`, `cost_rank_boost`, or `hard_exclude` at the
   configured threshold.
3. Falls back to Threnody token telemetry divided by `budget_tokens` when
   provider quota is unsupported, unavailable, stale, or malformed.
4. Returns no ratio when neither source is available.

`budget_tokens` remains necessary for the telemetry fallback. It is ignored
when a matching fresh provider-reported window supplies a ratio.

Model context-window utilization is session state, not subscription quota, and
is never used by this feature.

## Caching, diagnostics, and privacy

Quota adapter results are cached briefly to avoid repeatedly invoking provider
CLIs. Normalized observations are appended to the private Threnody SQLite
database for diagnostics. No access tokens, cookies, authorization headers, or
raw provider responses are persisted.

`check_providers` reports support status, source, freshness, windows, errors,
and configured threshold decisions. Routing results include `quota_rationale`.
`inspect_task` includes the latest persisted quota observation and clearly
labels it as diagnostic state rather than guaranteed route-time state.

GitHub's billing usage API may be useful for separate billing reports, but it
is not treated as subscription quota because individual, organization, and
enterprise billing scopes differ and the API does not always return an
allowance or remaining limit.
