# Routing Quality

## Current Result

Evaluation date: 2026-06-08

Command:

```bash
THRENODY_TEST_MODE=1 python3 -m shared.routing_eval
```

Result:

| Category | Pass | Fail | Skip | Executed Accuracy |
|---|---:|---:|---:|---:|
| Low tier | 10 | 0 | 0 | 100% |
| Medium tier | 11 | 0 | 0 | 100% |
| High tier | 8 | 0 | 2 | 100% |
| Urgency | 3 | 0 | 0 | 100% |
| **Total** | **32** | **0** | **2** | **100%** |

The two skipped high-tier fixtures are intentionally marked boundary fixtures.
Including skips in the denominator gives 94.1% corpus completion.

## Corrected Failure Pattern

The previous result was 21 passed, 11 failed, and 2 skipped. Every failure
over-routed routine authentication or authorization implementation to high tier
because those generic terms were hard high-tier overrides.

The correction:

- Removed generic `authentication` and `authorization` hard overrides.
- Retained a medium floor so auth work cannot route to low.
- Retained high overrides for security review, threat modeling, OAuth, SSO,
  RBAC, vulnerabilities, cryptography, compliance, architecture, and system
  design.

## Safety Checks

All stable fixtures for security review, migration, distributed systems,
database rollout, concurrency, production incidents, rollback planning, and
swarm topology pass after the change.

See `tests/eval/` for the fixture corpus and `docs/ROUTING_EVAL.md` for the
evaluation methodology.
