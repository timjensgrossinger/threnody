# Known Bottlenecks

## Current throughput bottlenecks

1. **Provider round-trip latency** — planner, subtask execution, and synthesis still rely on blocking CLI subprocess calls, so process startup and remote model latency are now the dominant cost.
2. **Fast-start critical path** — agent-emitting skills should return `host_spawn_waves` or `workflow_script` in under 5 seconds and reach first host spawn in under 30 seconds. Old behavior that blocks on rich planner calls, consensus, learning aggregation, or sequential same-wave spawn can miss this target before any worker starts.
3. **Serial planner and synthesis stages** — wave parallelism only speeds up the middle of execution when planning happens once up front and synthesis happens once at the end.
4. **Speculative fallback pool** — partially addressed: `parallelism.speculation_workers` (default `1`) scales the higher-tier speculation pool; planner/synthesis remain serial.
5. **Warm-path eval batching** — partially addressed: `parallelism.warm_path_workers` (default `2`) parallelizes rework eval prompts inside each warm-path batch.

## Previously documented (now configurable)

- Single-lane speculative fallback → set `parallelism.speculation_workers` to `2`–`4` for borderline-heavy waves (opt-in; some providers may not be thread-safe).
- Sequential warm-path eval per batch → raise `parallelism.warm_path_workers` up to `8`.

## Not primary bottlenecks right now

- **SQLite hot path** — WAL mode + `conn()` accessor throughout hot modules; direct `_db._conn` usage eliminated from shared modules.
- **Approval queue and inspect flows** — secondary to planner/provider latency today, but the list/audit path is unpaginated and could backlog at high queue volume.

## Auto-detection status

The previously tracked auto-detection defects are covered by regression tests:

- Binary-only installer scans report auth-aware providers as `auth_unknown`
  instead of routeable.
- Verified installer scans use provider readiness probes and preserve precise
  failure reasons.
- MCP `clientInfo` mapping is centralized in `shared.discovery`.
- Environment markers use consistent truthy parsing.
- Tests cover Copilot, Claude, Gemini, Codex, Cursor, Junie, OpenCode, conflict
  precedence, and transport/parent-process fallback.
