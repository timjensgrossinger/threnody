---
gsd_state_version: 1.0
milestone: v2.0
milestone_name: First-Class Swarm Topologies
status: Ready for milestone verification
last_updated: "2026-07-11T00:54:00+02:00"
last_activity: 2026-07-11 — Completed release-foundation plan 0-03
progress:
  total_phases: 11
  completed_phases: 11
  total_plans: 29
  completed_plans: 29
  percent: 100
---

# STATE — TGs-router (Project Memory)

## Project Reference

- Project: TGs-router
- Core value: Quietly choose the cheapest cross-shell execution path that still meets the required quality bar
- Current focus: v2.0 milestone closeout

## Current Position

Phase: 0-release-foundation
Plan: 03 complete
Status: Ready for milestone verification
Last activity: 2026-07-11 — Completed release-foundation plan 0-03

## Performance Metrics

- Total plans completed: 34
- Latest completed plan: 37-04
- Latest milestone close: v1.9 archived

## Accumulated Context

### Roadmap Evolution

- Phase 40.1 inserted after Phase 40: Wire execute_swarm runtime handoff and resume execution (URGENT)
- Decisions carried into this milestone:
  - No new Python dependencies; reuse existing SQLite-backed learning state where possible.
  - Memory CRUD uses explicit scopes and hard-delete semantics.
  - Status surfaces must degrade gracefully with empty/missing DB and share data-loading logic between CLI and MCP.
  - Handler extraction is deferred until operator surfaces and contracts are settled.
  - Phase 17 shipped `agent_queue_*` as the public MCP approval-queue family, preserved `approval_queue_*` as compatibility aliases, and requires explicit `operator_id` for mutating actions.
  - Phase 18 planning converged on an explicit-scope memory CRUD surface backed by a small additive SQLite schema extension rather than overloading unrelated tables.
  - Phase 18 shipped `memory_*` MCP tools backed by `shared/memory.py`, with compact list metadata, explicit not-found semantics, and structured JSON value support.
  - Phase 19 kept `shared/status.py` as the shared data-loading seam for both MCP and CLI status surfaces.
  - Phase 19 reports `rework_summary` as a global count because `rework_events` has no `project_path` column.
  - Phase 19 made `conservative_defaults` conditional on blank `project_id` instead of hardcoding `true`.
  - v1.8 shipped `record_outcome` MCP tool, outcome-driven EMA updates, and `learning_outcome_stats` / `learning_pattern_health` observability surfaces.
  - Eval fixtures must use `TGSROUTER_TEST_MODE` throughout; no real CLI calls.
  - `tests/eval/baseline.json` is machine-specific and must be gitignored.
  - Eval/test config loading may fall back to defaults only in test mode when YAML support is unavailable.
  - Eval runner failures should surface as controlled FAIL or ERROR output instead of uncaught crashes.

- Open questions: None

- Blockers: None

## Session Continuity

- Stopped At: Completed 0-release-foundation/0-03; reconciled release docs and clean-install verification
- Resume: Run milestone verification for release foundation
- Last milestone completed: v1.9 (archived)

---
*State recorded after completing Phase 40.*

## Operator Next Steps

- Start the next milestone with /gsd-new-milestone
