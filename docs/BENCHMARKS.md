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
```

## Current Reference Run

Measured on the release-audit machine on 2026-06-08:

| Benchmark | Result |
|---|---:|
| MCP module import and tool registry build | 0.095s, 43 tools |
| Hot-path routing classification loop | 300 tasks in 0.009s, 0.030ms/task |
| Subtask lifecycle/orchestration regression file | 24 passed in 5.69s |
| Full hermetic pytest suite | 1,781 passed, 2 skipped in 52.21s |
| Current broad suite excluding localhost socket tests | 1,772 passed, 2 skipped in 44.76s |
| Routing eval | 32 passed, 0 failed, 2 skipped |
| Routing eval focused tests | 138 passed in 0.44s |

Provider live latency is not benchmarked here because it is dominated by
external provider behavior.
