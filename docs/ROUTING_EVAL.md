# Threnody Routing Eval

Reproducible benchmark for Threnody's cost-tier routing decisions.

## Methodology

The eval suite (`shared/routing_eval.py`) runs each fixture through the pure-heuristic router (`shared/router.py`) and checks the result against the fixture's expected tier. No LLM calls are made — the router is keyword + signal scoring only.

### Fixture taxonomy

| Category | Directory | Description |
|----------|-----------|-------------|
| `low_tier` | `tests/eval/low_tier/` | Tasks that should route to `low` |
| `medium_tier` | `tests/eval/medium_tier/` | Tasks that should route to `medium` |
| `high_tier` | `tests/eval/high_tier/` | Tasks that should route to `high` |
| `urgency` | `tests/eval/urgency/` | Urgency override signals |
| `fanout` | `tests/eval/fanout/` | Fan-out topology triggers |
| `foreach` | `tests/eval/foreach/` | Dynamic for_each node triggers |

### Fixture schema

Each fixture is a JSON file with at minimum:

```json
{
  "id": "unique-fixture-id",
  "task": "task description to route",
  "expected_tier": "low",
  "category": "low_tier"
}
```

Optional fields: `notes`, `expected_topology`, `expect_urgency`.

### Scoring

- **Pass**: routed tier matches `expected_tier` exactly.
- **Fail**: wrong tier — counts against accuracy.
- **Skip**: fixture lacks `expected_tier` or is explicitly marked skip.

Accuracy = `pass / (pass + fail)`.

## Running the eval

```bash
# Full suite
threnody eval run

# With report
threnody eval run --report markdown > docs/EVAL_REPORT.md
threnody eval run --report html > docs/eval_report.html

# Filter by category
threnody eval run --filter low,urgency

# Update baseline (run after intentional routing changes)
threnody eval baseline
python3 -m shared.eval_baseline
```

## CI integration

The `.github/workflows/routing-eval.yml` workflow runs nightly on `main` and on every PR. It fails if accuracy regresses more than 2 percentage points from the baseline without an explicit baseline update commit.

## Interpretation

The eval measures **routing heuristic accuracy** — whether the scoring model correctly classifies a task's complexity. It does not measure output quality, LLM performance, or real-world usefulness.

High accuracy (>90%) means the keyword/signal scoring correctly identifies task tier, which determines which provider and model get used, which directly affects cost.

## Extending the suite

1. Add a `.json` file to the appropriate `tests/eval/<category>/` directory.
2. Run `threnody eval run --filter <category>` to verify it passes.
3. Run `threnody eval baseline` to update the baseline if you changed routing logic.
