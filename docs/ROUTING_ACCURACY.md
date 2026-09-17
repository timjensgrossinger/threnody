# Routing accuracy (operator report)

Reproducible fixture-based tier routing stats from `python3 -m shared.routing_report`.
Do not commit `tests/eval/baseline.json`; regenerate this document locally or from CI artifacts.

- **Generated:** 2026-09-03
- **Config hash:** `d1a0abe4ea35`
- **Fixtures:** 55
- **Executed accuracy:** 100.0%
- **Boundary fixtures (informational):** 2 skipped

## How to refresh

```bash
THRENODY_TEST_MODE=1 python3 -m shared.routing_report --write-docs
THRENODY_TEST_MODE=1 python3 -m shared.routing_eval
```
# Threnody Routing Eval Report

**Date:** 2026-09-03  
**Fixtures:** 55  
**Accuracy:** 96.4%  

| Status | Count |
|--------|-------|
| Pass | 53 |
| Fail | 0 |
| Skip | 2 |

## Category Accuracy

| Category | Pass | Fail | Skip | Executed Accuracy |
|----------|------|------|------|-------------------|
| duration | 6 | 0 | 0 | 100.0% |
| high_tier | 14 | 0 | 2 | 100.0% |
| low_tier | 12 | 0 | 0 | 100.0% |
| medium_tier | 18 | 0 | 0 | 100.0% |
| urgency | 3 | 0 | 0 | 100.0% |
