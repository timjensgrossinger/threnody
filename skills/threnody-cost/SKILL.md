---
name: threnody-cost
description: >-
  View cost/spend dashboard with role, tier, and provider breakdowns from
  Threnody's local cost telemetry. Use when asked about cost, spend, budget,
  tokens used, savings, or how much a task/period cost.
---

# Threnody cost dashboard

`inspect_spend` returns aggregated cost telemetry from local SQLite.

All data is local — no API calls. Costs are estimates based on bundled model
price hints and token counts, not provider invoices.

## When to use

- User asks "how much did X cost?"
- User wants to see spend trends, savings, token usage
- User wants to understand tier utilization or free-tier ratio
- User wants to see cost breakdown by role, tier, or provider

## Process

1. Call **`inspect_spend`** (MCP: Threnody) with `since` parameter.

   Supported windows: `24h`, `7d` (default), `30d`, `all`.

2. Format output as table:

```
THRENODY COST — last 7d
─────────────────────────
Subtasks:   47         Est cost:    $0.34
Free:       23%        Savings:     $2.81 (vs all-high-tier)

BY ROLE (when present)
  Implementer   28 tasks   $0.18   54% of spend
  Reviewer      12 tasks   $0.09   26%
  Architect      4 tasks   $0.05   15%
  Tester         3 tasks   $0.02   5%

BY TIER
  low            31 tasks   $0.04
  medium         12 tasks   $0.18
  high            4 tasks   $0.12

BY PROVIDER
  claude         38 tasks   $0.27
  openai          9 tasks   $0.07

TIPS
  - Free-tier utilization low (23%) → enable more free-tier providers
  - Reviewer rework rate high → consider higher tier for review tasks
```

3. Each row in breakdown: role/tier/provider, task count, estimated cost,
   percentage of total spend.

4. If `by_role` is empty (no role-tagged tasks yet), skip BY ROLE section.

5. Add contextual tips based on data:

   - `free_subtask_pct < 30%` → "Enable more free-tier providers to cut spend"
   - `savings_usd > 0` → "Threnody saved you $X vs all-high-tier routing"
   - Any role with high rework rate → "Consider higher tier for {role} tasks"

## Interpretation

- **est_cost_usd**: estimated cost based on bundled model price hints × token
  counts. Not a provider invoice.
- **counterfactual_cost_usd**: what the same work would cost if all tasks ran on
  high-tier model.
- **savings_usd**: counterfactual - est = dollars saved by tier routing.
- **free_subtask_pct**: percentage of subtasks that cost $0 (free-tier providers).

## Disclaimer

Cost estimates use bundled model price hints and token estimates. They are
approximations for operator awareness, not accounting-grade billing.

## Canonical location

Project skill: `skills/threnody-cost/` in the Threnody repo.
