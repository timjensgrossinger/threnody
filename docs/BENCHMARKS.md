# Benchmarks

Benchmarks are intended to catch release regressions, not to compare providers
whose latency depends on network, subscription, quota, and model load.

## Local Commands

```bash
time python3 - <<'PY'
import mcp_server
print(len(mcp_server.TOOLS))
PY

time THRENODY_TEST_MODE=1 python3 -m shared.routing_eval

time THRENODY_TEST_MODE=1 python3 -m pytest tests/test_parallel_execution.py -q
time THRENODY_TEST_MODE=1 python3 -m pytest tests/test_clean_install.py -q
```

## Current Reference Run

Measured on the release-audit machine on 2026-07-11. Provider latency and
network-backed provider calls are intentionally excluded.

| Benchmark | Result |
|---|---:|
| MCP module import and tool registry build | 0.098s, 53 published tools |
| Hot-path routing classification loop | 300 tasks in 0.009s, 0.030ms/task |
| Parallel execution regression file | 25 passed in 6.03s |
| Provider compatibility focused tests | 240 passed in 9.57s |
| Provider/packaging/MCP focused tests | 247 passed in 12.70s |
| Full hermetic pytest suite | 2,165 passed, 3 skipped in 85.35s |
| Clean wheel and sdist installs (Python 3.13) | 2 passed in 30.80s |
| Routing eval | 39 passed, 0 failed, 2 skipped |

Provider live latency is not benchmarked here because it is dominated by
external provider behavior.
