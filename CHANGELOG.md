# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project uses Semantic
Versioning for public releases.

## [Unreleased]

## [0.3.0-alpha.3] - 2026-07-31

Host-native spawn manifests now account for every file they were given, and the
learning loop gains four zero-token ground-truth sources. No MCP tool was
removed; one response shape changed (see Changed).

### Added

- **`shared/verify.py`** — lint/type/test verify gate graded against the **merge
  base**: failures already red on the branch are reported as
  `preexisting_failures` and never blamed on the run; only `new_failures` can
  warn/reject. Runs in-process at host-native finalize (zero model tokens) and
  yields at most one low-tier `verify_followup`. Off by default (`verify_gate`).
- **`shared/code_intel.py`** — deterministic, LLM-free entity/smell scan (stdlib
  `ast` for Python, regex elsewhere). Backs review structural-density scoring and
  the objective static-recall grade; cached by `(path, content_sha)`.
- **`shared/review_memory.py`** — a `(file revision × dimension)` cell already
  reviewed at an equal-or-stronger tier is skipped and its findings replayed into
  synthesis; findings since fixed are injected as "already fixed". A skip needs an
  exact digest match **and** tier coverage, so depth is never traded for cost.
- **`shared/beliefs.py`** — one free repo-scoped lesson per run at finalize
  (pattern on a clean run, constraint on a failed/reworked one), stored in the
  existing `project` memory scope, injected under a hard cap.
- **`shared/ladder.py`** + `tests/ladder/L0..L6` — graded task ladder: the
  ground-truth complement to the classification-only routing eval. Operator-
  invoked (`threnody ladder run`), feeds `model_quality_events` and the
  minimum-passing-tier table.
- **`shared/hybrid_learning.py`** — the diagnose→implement tier discount is
  learned per work profile (`hybrid_tier_bias`) instead of fixed.
- **Objective quality sources** — `record_static_recall_score` (reviewer graded
  against the static pre-scan) and `record_verify_gate_score` (did the write leave
  new failures) join the free findings proxy; `threnody quality` reports
  `objective_n`/`objective_avg` so ground truth is distinguishable from inference.
- **Router duration axis** — `expected_duration_bucket` (short/medium/long) from
  structural signals, with `tests/eval/duration/` fixtures.

### Fixed

- **Files could vanish from a spawn manifest.** `choose_agent_count`'s
  `min(file_count, 4)` recommendation was applied as a hard file cap, so a 9-file
  task silently planned 4. Fanout is now one writer per named file, bounded only by
  `swarm.max_agents`, and an over-budget plan **packs** files into fewer multi-file
  owners rather than dropping them.
- **The squeeze is now reported.** `coverage.deferred` (a named file with no owner
  — a plan defect) and `coverage.packed` (budget forced merging) ride the plan
  payload and `plan_summary`; intent-template fullstack plans get the same
  accounting instead of packing silently.
- **`swarm_max_agents: -1` meant "unlimited" everywhere except the packer**, which
  read it as a cap of one agent. Any caller passing the config value collapsed a
  whole fanout into a single agent.
- **`target_files` was dropped by the planner**, so a multi-file owner reached the
  host with one target and the ownership dedupe could not fire. `Subtask` now
  carries the plural list through `plan_to_dict`, alongside `inline_files`.
- **Per-file prompt hints leaked across paths.** Clause windows now anchor on the
  full path and terminate at the next path, coupled subtasks group per directory
  instead of collapsing globally, and the ownership sentence no longer splices
  onto an unterminated clause.
- **`expand_host_plan` bypassed the containment gate and every cap.** It now runs
  `sanitize_plan_for_host` (escaped paths return as `deferred_files` and stay
  claimable) and packs to the agent cap, re-offsetting ids so appended agents
  cannot collide with already-spawned ones.
- **`workspace_root` resolved silently to the server's cwd.** Resolution happens
  once per handoff and is echoed as `workspace_root_source`
  (`argument` | `env_override` | `cwd_fallback`), with a `workspace_root_warning`
  on the fallback — target containment is checked against that root.
- **Task packs lost their preamble** once hint windows were tightened; the pack
  prefix is injected per subtask again.
- Review fanout tiering is LOC + structural-density based (dense mid-size
  reasoning-heavy → high, flat large → medium) rather than keyword-driven, and
  `[dims=...]` intent is honored with drop protection.

### Changed

- **Host-native `execute_swarm` responses are compact for every run**, not only
  `REVIEW:` runs: the duplicate `plan` is replaced by `plan_summary` (which keeps
  `coverage`, `inline_files`, `sanitization`). Spawn from `host_spawn_waves`; full
  plan fidelity remains in the run receipt (`inspect_run_receipt`).

## [0.3.0-alpha.2] - 2026-06-16

Local resource-usage hardening for many concurrent host-native subagents.
Agent counts stay unlimited; these are throughput/footprint changes only —
no behavior change.

### Changed

- **Batched per-wave DB writes** — `Database.flush_host_wave_records` coalesces
  the per-agent `track_pattern` + `log_agent_result` +
  `routing_guard_record_execution` writes (≈3N auto-commits) into a single
  transaction per wave. Shared `_apply_pattern_row` / `_telemetry_columns_values`
  primitives keep rows byte-identical; `ingest_host_wave` and the workflow
  ingest path buffer then flush once.
- **Wave-scoped source cache** — `context.read_source_cached` (mtime+size keyed
  LRU) reads each source file once per fan-out wave instead of once per subtask;
  `review_fanout` reuses it. Auto-invalidates on any mid-wave write.

### Added

- **`background` config block** — tunable, disablable health-probe (default
  60 s) and warm-path (default 120 s) daemon cadence; legacy
  `resilience.health_probe_interval_s` honored as a fallback.

## [0.3.0-alpha.1] - 2026-06-14

Host-native swarm safety hardening and tier-aware Dynamic Workflow emission.

### Added

- **Dynamic Workflow emission** (claude-code, opt-in) — render an `ExecutionPlan`
  into a Claude Code Workflow JS script with tier-aware per-`agent()` model
  routing; approval-gated learning and pre-tuned permanent export to
  `.claude/workflows/<slug>.js`. New `report_workflow_result` MCP tool,
  consensus-in-workflow path, and `/threnody-workflow` skill.
- **Plan safety gate** (`sanitize_plan_for_host`) — runs before both
  `host_spawn_waves` and workflow emission: strips `target_file`s that escape
  the workspace root, drops fragment/empty prompts, prunes waves and
  `depends_on`, and collapses to a single coherent agent when nothing safe
  survives. Read-only review targets are exempt. Emits a `sanitization` report.

### Fixed

- **Heuristic planner misfire** — reject absolute/home/system-root/fragment
  paths at extraction so spurious prose slices (home dir, plan file) never
  become write targets; the single-subtask fallback fires instead.
- **`execute_swarm` `workspace_root`** — the arg now threads through into the
  handoff, routing guard, and file hints instead of always defaulting to the
  active MCP workspace root.
- `build_host_spawn_waves` warns instead of silently skipping empty prompts.

### Changed

- `Subtask.edit_mode` documented as `execute_subtask` utility-delegation only;
  host-native swarm agents edit via their own native tools.
- Test suite consolidation (fork → replay, phase15 → planner, topology-explain
  → execute-swarm).

## [0.2.0-alpha.1] - 2026-06-11

Second public alpha — host-native swarm orchestration, richer learning ingest, and
heuristic multi-file planning without external planner LLM calls for common webapp
tasks.

### Added

- **Host-native heuristic planning** — `execute_swarm` / `plan_task` fan out one host
  `Task`/`Agent` per file for webapp and fullstack intent; DAG waves for integration
  files (`index.html`, `app.js`, etc.)
- **`expand_host_plan`** MCP tool — mid-run file fanout when scaffold waves discover
  additional paths
- **`learning_report_contract`** on swarm and plan handoffs — documents required
  `report_host_wave` fields (`workspace_root`, `output_excerpt`, per-agent telemetry)
- **Richer host learning ingest** — resolves `workspace_root` from handoff meta,
  normalizes absolute/relative `touched_files`, auto-fills `output_excerpt` from disk,
  and enables rework/style detection across waves
- **CLI-neutral project skills** under `skills/` (`threnody-swarm`, `threnody-task`,
  `threnody-plan`, `threnody-fullstack`, `threnody-routing`, `threnody-subtasks`)
- Vienna weather webapp fixture at `tests/fixtures/vienna-weather-app/` for swarm demos
- Adaptive routing threshold wiring and end-to-end route telemetry persistence

### Changed

- **Routing policy defaults:** `routing_policy.mode: default` now recommends **advisory**
  routing for all shells (including Claude Code). Guarded coordination and Claude
  PreToolUse hooks remain available via `mode: guarded` or per-shell overrides.
  Re-run `./install.sh` to refresh managed instruction blocks and hook registration.
- **`execute_swarm`** defaults to `host_native` — returns `host_spawn_waves` for host
  execution instead of subprocess orchestration; improved Cursor spawn model normalization
- **Host spawn enforcement** — plan and swarm handoffs require host `Task`/`Agent`
  subagents; direct `Write`/`Edit` on planned `target_files` is blocked during active
  handoffs
- **`report_host_wave`** documentation and skills now require `workspace_root` and
  `output_excerpt` for learning quality (server backfills when omitted)

### Fixed

- `execute_swarm` host-native response and handoff registration edge cases
- Test fixtures using fake API key patterns that triggered secret scanning

### Notes

- MCP tool schemas may change between alpha releases; pin `v0.2.0-alpha.1` for stability
- See [KNOWN_BOTTLENECKS.md](KNOWN_BOTTLENECKS.md) for documented performance limits

## [0.1.0-alpha.1] - 2026-06-11

First public **Threnody** alpha after retiring the Switchyard-branded beta. Version
line resets from `1.0.0-beta.1` to reflect the rebrand and provider-compliance
hardening (older internal tags such as `v3.2.0-alpha.1` remain in git history).

### Added

- [docs/LEGAL.md](docs/LEGAL.md) — operator responsibilities and provider compliance boundaries
- Router-only defaults for Claude Code and Gemini CLI (coordination anchors, not default delegation targets)
- Visual README with architecture, routing, wave, and learning-loop diagrams (`docs/assets/`)
- Reference docs: [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md), [docs/CLI.md](docs/CLI.md), [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- `shared/env.py` — centralized env resolution with deprecated prefix fallbacks
- Legacy CLI wrappers: `switchyard`, `switchyard-watch` → `threnody`, `threnody-watch`
- Installer migrates `~/.local/lib/switchyard` → `~/.local/lib/threnody` when present
- README discoverability section (MCP / LLM router / multi-agent search terms)

### Changed

- **Utility-only delegation:** `providers.delegation_utilities_enabled` (default `false`) gates `execute_subtask` to utility backends only (OpenCode, Aider, local loopback endpoints). Host CLI subprocess delegation (Copilot → Codex, Aider → Copilot, etc.) is blocked and cannot be overridden by `caller_allowlists` or `preferred_routing_by_caller`. Settings wizard warns on legacy host allowlists; re-run `./install.sh` for updated installer hints.
- **Routing policy:** `guarded` replaces `strict` as the canonical coordination-gate mode (`strict` remains a deprecated alias with a log warning). Guarded profiles no longer imply `low_tier_execute_subtask`; host-native execution is the default after `route_task`. Re-run `./install.sh` to refresh managed instruction blocks and Claude hooks.
- **Routing guards:** low-tier host callers issue `direct` guards after `route_task` (not `execute_subtask`). Delegate low-tier guards remain for non-host callers or explicit `low_tier_execute_subtask` opt-in. `execute_subtask_guard_strict` defaults to `false`.
- **`route_task` hints:** `execution_hint` now includes `host_native_model` and `host_native_method`; per-shell `tier_model_mapping` defaults come from `bootstrap_tier_map`.
- **Rebrand:** Switchyard → **Threnody** — install path (`~/.local/lib/threnody`), MCP name, CLI (`threnody`), env prefix (`THRENODY_*`)
- Public repository: `timjensgrossinger/threnody`
- `switchyard` / `SWITCHYARD_*` deprecated for one beta cycle (wrappers and env fallbacks remain)
- Prior beta shipped as Switchyard (`timjensgrossinger/switchyard`); `TGSROUTER_*` still accepted where documented

### Notes

- MCP tool schemas may change between alpha releases; pin a git tag for stability
- See [KNOWN_BOTTLENECKS.md](KNOWN_BOTTLENECKS.md) for documented performance limits

## [1.0.0-beta.1] - 2026-06-10

### Retired

- GitHub release and tag **removed** on 2026-06-11 — Switchyard branding and missing
  provider-compliance documentation. Do not use; install `v0.2.0-alpha.1` or later.

### Added

- Apache License 2.0 with `NOTICE` for third-party attributions
- `VERSION` file and `shared/version.py` as single source of truth for MCP serverInfo
- Routing eval fixture alignment for low-tier override and urgency scoring behavior
- Deterministic routing eval via default config in `THRENODY_TEST_MODE` (ignores local `config.yaml`)

### Changed

- Public beta release: repository metadata, README status, and license updated for OSS
- Removed internal `.planning/` artifacts from version control
- Hardened `.gitignore` for secrets, keys, and environment files
- Routing eval CI workflow now fails correctly on fixture regressions (`pipefail`)

### Notes

- MCP tool schemas may change between beta releases; pin a git tag for stability
- See [KNOWN_BOTTLENECKS.md](KNOWN_BOTTLENECKS.md) for documented performance limits

## [v3.2.0-alpha.1] - 2026-06-08

### Added

- Explicit provider auto-route tier policies preserved through live catalog
  refresh.
- Persisted learning audit-log inspection with filtering and secret redaction.
- Required verify-gate failure semantics and per-signal timeouts.
- Explicit subtask lifecycle states and pre-PID cancellation.
- Public security, contribution, configuration, and CI documentation.
- MIT license, SECURITY.md, CONTRIBUTING.md.
- GitHub Actions CI: Python 3.10–3.13 matrix, ShellCheck, Gitleaks, archive
  inspection, and installer smoke tests.
- Managed uninstaller (`uninstall.sh`).
- Release docs: ARCHITECTURE.md, BENCHMARKS.md, DEMO.md,
  PROVIDER_COMPATIBILITY.md, ROUTING_QUALITY.md, RELEASE_LIMITATIONS.md.

### Fixed

- OpenCode and Junie no longer gain unintended routing tiers after discovery.
- Provider startup now shares the task execution deadline.
- Post-registration early returns no longer leave active subtasks orphaned.
- Patch mode now validates its target path.
- Rewrite length-guard rejection no longer calls a missing database method.
- Concurrent SQLite schema initialization is now race-free.
- Claude model IDs updated to stable `haiku`/`sonnet`/`opus` aliases.
- Claude auth preflight uses `claude auth status`; quarantine clears on
  fresh successful probe.

### Security

- Routing eval accuracy: 100% on 32 fixtures (2 intentional boundary skips).
- Verify gate: missing required tools now fail explicitly (no silent pass).
- Archive: 559 entries, no secrets, runtime state, or generated files.

## [1.9] - 2026-06-08

- Last internal milestone before the public release hardening cycle.

[Unreleased]: https://github.com/timjensgrossinger/threnody/compare/v0.3.0-alpha.3...HEAD
[0.3.0-alpha.3]: https://github.com/timjensgrossinger/threnody/compare/v0.3.0-alpha.2...v0.3.0-alpha.3
[0.3.0-alpha.2]: https://github.com/timjensgrossinger/threnody/compare/v0.3.0-alpha.1...v0.3.0-alpha.2
[0.3.0-alpha.1]: https://github.com/timjensgrossinger/threnody/compare/v0.2.0-alpha.1...v0.3.0-alpha.1
[0.2.0-alpha.1]: https://github.com/timjensgrossinger/threnody/releases/tag/v0.2.0-alpha.1
[0.1.0-alpha.1]: https://github.com/timjensgrossinger/threnody/releases/tag/v0.1.0-alpha.1
[1.0.0-beta.1]: https://github.com/timjensgrossinger/threnody/commit/4fbf8629301a7a557e9cc16ace00e9e85f9495a8
[v3.2.0-alpha.1]: https://github.com/timjensgrossinger/threnody/compare/v1.9...v3.2.0-alpha.1
[1.9]: https://github.com/timjensgrossinger/threnody/releases/tag/v1.9
