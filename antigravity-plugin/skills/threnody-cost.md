---
name: threnody-cost
description: >-
  View token spend and cost analytics. Use /threnody-cost to see
  token usage by tier, agent, session, or time period.
---

# Threnody Cost Dashboard

Track token spend and cost optimization across sessions.

## Usage

Call `inspect_spend(since="7d")` (MCP: Threnody) to view cost analytics.

### Commands

- `/threnody-cost` — last 7 days (default)
- `/threnody-cost 30d` — last 30 days
- `/threnody-cost today` — today only
- `/threnody-cost session` — current session

### Output format

The `inspect_spend` response includes:

```json
{
  "est_cost_usd": 0.42,
  "counterfactual_usd": 12.00,
  "savings_usd": 11.58,
  "by_tier": {
    "low": {"tokens": 1200000, "cost": 0.00},
    "medium": {"tokens": 800000, "cost": 0.00},
    "high": {"tokens": 400000, "cost": 0.42}
  },
  "by_agent": {
    "threnody-agy-low": {"tokens": 1200000, "cost": 0.00},
    "threnody-agy-medium": {"tokens": 800000, "cost": 0.00},
    "threnody-agy-high": {"tokens": 400000, "cost": 0.42}
  },
  "receipts": [...]
}
```

### Interpreting results

- **est_cost_usd** — estimated actual cost based on model pricing
- **counterfactual_usd** — what it would have cost without tier routing
- **savings_usd** — money saved by routing to cheaper tiers
- **by_tier** — breakdown by complexity tier (low/medium/high)
- **by_agent** — breakdown by agent type

### Cost optimization tips

1. **Use low tier for simple tasks** — boilerplate, formatting, small edits
2. **Use medium tier for standard work** — most implementation, test generation
3. **Reserve high tier for complex work** — architecture, multi-file refactors, debugging
4. **Review the receipts** — identify agents consuming disproportionate tokens
5. **Check the savings** — tier routing should save 50-80% vs flat high-tier usage

### Related

- `threnody-routing` — automatic complexity-based tier selection
- `inspect_spend` — underlying MCP tool for cost data
- `inspect_run_receipt` — detailed receipts for specific runs
