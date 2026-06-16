# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Full test suite
python3 -m pytest tests/ -v

# Single test file
python3 -m pytest tests/test_router.py -v

# Single test by name
python3 -m pytest tests/test_router.py::test_name -v

# Isolated from host CLIs (also set automatically by conftest fixtures)
THRENODY_TEST_MODE=1 python3 -m pytest tests/ -v

# Lightweight syntax check (used by installer)
python3 -m py_compile mcp_server.py shared/router.py
bash -n install.sh shell/*.sh

# Routing eval suite — run before changing config.yaml or eval fixtures
threnody eval run
threnody eval run --filter low,urgency
python3 -m shared.routing_eval       # repo-local fallback
python3 -m shared.routing_report --write-docs  # operator ROUTING_ACCURACY.md

# Refresh local eval baseline
threnody eval baseline
python3 -m shared.eval_baseline      # repo-local fallback

# Start the MCP server (manual testing)
python3 mcp_server.py

# Live monitoring (separate terminal)
threnody-watch

# Shell aliases (installed by install.sh — restart shell or source ~/.zshrc first)
ghc agent "implement JWT auth for the user service"  # multi-agent wave orchestration
ghcs "how to list files recursively in python"        # quick single-agent call (auto-routed)
ghce "what does awk '{print $2}' do"                  # quick explanation call
ghcw                                                  # cache stats
ghc agent -w "refactor the database layer"            # show plan only, no execution
ghc agent --no-plan "add a docstring to this function" # single agent, skip orchestration

# Provider health diagnostics and self-repair
threnody doctor                              # diagnose all providers, exit 1 if any QUARANTINED
threnody doctor --repair                     # diagnose + bounded self-repair

# Provider / readiness diagnostics
threnody inspect status --project . --details

# Operator controls
threnody inspect task <task-id>
threnody inspect approvals --project .
threnody inspect approvals approve <id> --project . --operator <name>
threnody tune show --project .
threnody tune set concurrency_limit 5 --project .
threnody tune reset concurrency_limit --project .

# DB maintenance
threnody db check [--db PATH]        # integrity check + report
threnody db repair [--db PATH]       # recover from latest timestamped backup
threnody db backup [--db PATH]       # online backup via conn.backup (keeps 3)
threnody db prune [--db PATH] [--keep N]  # rotate old backups

# Re-run installer (idempotent — updates provider registrations and shell aliases)
./install.sh
```

No `pyproject.toml`, `setup.py`, or build step — plain Python 3.10+. Core dependency is `pyyaml`; `install.sh` also installs `rich` and `questionary` for the settings wizard when available. At least one AI CLI must be installed: `gh` (GitHub Copilot), `claude` (Claude Code), `gemini`, `codex`, `cursor-agent`, `junie`, or `opencode`.

`threnody eval run` and `python3 -m shared.routing_eval` load the **installed** config from `~/.local/lib/threnody/config.yaml`, not the repo-root `config.yaml` template. The `threnody ...` commands require the shell wrapper from `install.sh`; the `python3 -m ...` forms are repo-local fallbacks.

## Architecture

**Shared brain, provider-specific entry points.** All logic lives in `shared/`; provider directories (`copilot/`, `claude-code/`, `gemini/`, `codex/`, `cursor/`, `junie/`, `mistral/`, `opencode/`, `blackbox/`, etc.) are thin wrappers that instantiate a concrete `Provider` and delegate to the shared core.

Three execution paths run concurrently:

| Path | Trigger | Role |
|---|---|---|
| **Hot** (blocking) | Every task | Route → plan → execute → return result |
| **Warm** (async bg) | After subtask completes | `shared/eval.py` — rework detection + quality scoring |
| **Cold** (periodic bg) | Telemetry accumulation | `shared/adaptive.py` — EMA threshold adjustment |

### Hot path (blocking)

```
route_task
  → shared/router.py       # keyword heuristic → tier (low/medium/high), no LLM
  → shared/discovery.py    # returns provider/model metadata for chosen tier

decompose_task / plan_task
  → shared/planner.py      # LLM-backed decomposition → ExecutionPlan with waves + topology
  → returns wave/topology metadata; caller drives execution

execute_subtask / swarm runs
  → shared/orchestrator.py # topology runner → wave-based parallel subprocess execution
  → shared/discovery.py    # ProviderRegistry picks routable delegation targets (excludes router-only hosts by default)

```

The planner is advisory — it only returns decomposition metadata. The orchestrator owns runtime behavior: execution, retries, escalation, token budgets, topology fallback, swarm, and checkpoints.

### Execution topologies

`ExecutionPlan.topology` controls which runner fires inside `Orchestrator`:

| Topology | Runner | Notes |
|---|---|---|
| `linear` | `_execute_runtime_plan` (wave loop) | Default; all others fall back here on validation failure |
| `dag` | `_execute_dag_runner` | Dependency-ordered wave execution via shared wave core |
| `hierarchical` | `_execute_hierarchical_runner` | Parent–child subtask trees |
| `star` | `_execute_star_runner` | Coordinator rounds with worker fanout; `run_coordinator_consensus` fans out persona-diverse queens (subprocess path) |

### Supporting subsystems

| Module | Role |
|---|---|
| `shared/config.py` | `TGsConfig` dataclass — all constants, YAML loading, hard tier bounds |
| `shared/db.py` | Single `Database` wrapper — SQLite WAL, 37+ tables, startup integrity check, auto-recovery, backup rotation |
| `shared/db_cli.py` | `threnody db` CLI — operator-facing check/repair/backup/prune subcommands backed by `Database` |
| `shared/adaptive.py` | EMA-based threshold learning |
| `shared/agents.py` | Learning loop: pattern tracking → draft → approval queue → registration |
| `shared/eval.py` | Background rework detection + quality eval |
| `shared/routing_eval.py` | Fixture-based routing evaluation framework |
| `shared/speculative.py` | Borderline-score speculative execution |
| `shared/context.py` | Reads source files, injects diff context into subtask prompts; write-safety boundary |
| `shared/style.py` | Per-project code style profiling; `StyleLearner` / `DecompositionPrefs` |
| `shared/discovery.py` | `ProviderRegistry` singleton — detects CLIs, router-only hosts, delegated execution |
| `shared/adapters.py` | `ProviderAdapter` versioned contract + `ExecutionResult`; secondary adapters (Blackbox, Aider, Q/Kiro) |
| `shared/swarm.py` | Swarm persistence domain helpers |
| `shared/memory.py` | Cross-session memory store |
| `shared/snapshot.py` | `FileSnapshot` — pre/post write diffing for `execute_subtask` preview gate |
| `shared/status.py` | `build_status_snapshot` — shared status builder for MCP and CLI surfaces |
| `shared/instructions.py` | Shell-specific managed instruction renderer driven by `routing_policy` |
| `shared/model_catalog.py` | Dynamic model catalog — discovers, ranks, caches live model lists |
| `shared/outcomes.py` | Outcome recording, scoring, and aggregation for routing feedback |
| `shared/settings_wizard.py` | Interactive TUI wizard for first-run and re-configuration |
| `shared/provider_factory.py` | Registry-driven resolver that maps `CLIProvider.name` → concrete `Provider` subclass for `Orchestrator` construction |
| `shared/health.py` | Provider health state machine and circuit-breaker helpers |
| `shared/resilience.py` | Error classification, retry policy, auth probing — used by discovery execute paths |
| `shared/doctor.py` | Provider health diagnostics and bounded self-repair; backs `threnody doctor [--repair]` |
| `shared/edit_blocks.py` | Aider-style SEARCH/REPLACE block parser used by `execute_subtask` `blocks` mode |
| `shared/bandit.py` | LinUCB/Thompson sampling routing policy; shadow mode by default — logs picks but executes heuristic; promote via `config.routing.bandit_mode = 'live'` |
| `shared/policy.py` | Runtime policy enforcement (`file_write`, `file_read`, `command`, `http_egress`, `mcp_tool`, `secret`); fail-closed — unrecognized op type → deny |
| `shared/quota.py` | Provider subscription quota collection and normalization |
| `shared/spend.py` | Operator-facing spend and savings snapshots from local cost telemetry |
| `shared/worktree.py` | Git worktree isolation for `execute_subtask`; leases at `~/.local/lib/threnody/worktrees/<task_id>`, released with `merge` or `discard` |
| `shared/replay.py` | Trace replay and state forking from coordinator checkpoints; requires idempotency keys on `SIDE_EFFECTING` subtasks |
| `shared/routing_hook.py` | Standalone PreToolUse routing guard bridge — no MCP stdio required; backs the Claude Code `guarded` policy hook |
| `shared/run_log.py` | Append-only per-run JSONL log under `~/.local/lib/threnody/runs/<run_id>/` — the capture plane for `batch` host-native reporting. Worker-wave learning is appended here (by the PostToolUse hook or the host) and imported once at terminal via `host_learning.import_run_log`, eliminating the per-wave `report_host_wave` round-trip. Holds an `active.json` pointer + idempotency/`runs_keep` rotation |
| `shared/learning_hook.py` | Standalone PostToolUse learning-capture bridge — appends one `run_log` line per Edit/Write with zero model tokens. Depends only on `run_log` (no MCP/DB), never blocks the tool. Backs the Claude `PostToolUse` hook when `host_native.learning_capture=hook` |
| `shared/agent_export.py` | Export approved learned agent definitions as provider-native skill files |
| `shared/host_spawn.py` | Host-native spawn contract helpers — produces `host_spawn` / `host_spawn_waves` payloads, enforces `HostNativeRequired`; honors per-subtask `subagent_type` and `read_only` overrides. `sanitize_plan_for_host()` is the workspace-containment safety gate: before host-wave OR Dynamic Workflow emission it strips `target_file`s that escape the workspace root and drops fragment/empty prompts, collapsing to a single coherent agent if nothing safe survives (records a `sanitization` report). Host-native swarm agents edit via their own native Edit/Write tools — the `execute_subtask` surgical edit modes (`rewrite`/`blocks`/`patch`) are utility-delegation-only and do not apply to host-native waves |
| `shared/review_fanout.py` | Per-file × dimension review fanout for `REVIEW:` tasks — complexity gating (trivial/moderate/complex), dimension selection, tier assignment, `build_review_subtasks()` |
| `shared/consensus.py` | Multi-queen consensus decision logic shared by both execution paths — persona selection, quorum + structural tally, judge arbitration. Owns the *decision*; orchestrator/host own *execution* (spawning queens). Pure, dependency-light, independently tested |
| `shared/workflow_emit.py` | Renders an `ExecutionPlan`/plan dict into a Claude Code **Dynamic Workflow** JS script — tier-aware per-`agent()` model routing, wave→`parallel`/synthesis mapping, dependency-result injection, structured per-agent schema. claude-code only, opt-in (`routing_policy.shells.claude-code.workflow_emit`). Additive — never replaces `host_spawn_waves` |
| `shared/workflow_export.py` | Learns + exports permanent workflows — `workflow_shape_fingerprint` (orchestration shape, prompt-independent), approval-gated `build_workflow_draft`, `tune_models_from_learning` (re-tune per-tier models from recorded outcomes), `build_workflow_doc_header` (tier→model map, persona roster, learned-agent links), and `export_workflow(tune=True)` writing pre-tuned, documented scripts to `.claude/workflows/<slug>.js` |
| `mcp_server.py` | MCP server (JSON-RPC/stdio) — lazy-init, ~42 public tools (incl. `report_workflow_result`) |
| `shell/ghc.sh` | Multi-agent shell script backing the `ghc` / `ghcs` / `ghce` aliases |
| `shell/threnody-watch` | Live monitoring daemon; run in a separate terminal alongside the MCP server |

`mcp_server.py` is ~310 KB — use `grep` rather than reading it whole. All meaningful logic is in `shared/`; the server is a thin dispatch layer.

`providers.json` in the repo root is an auto-generated readiness cache written by `install.sh` and updated at runtime. Do not edit it manually.

`shared/data/model_prices.json` is the bundled cost database used by `model_catalog.py` to rank models into tiers. It is auto-generated and should not be edited manually.

### Key abstractions

- **`Provider` (abstract)** in `shared/orchestrator.py` — implemented by each provider dir; interface: `resolve_model(tier)`, `execute(subtask, model)`, `available_tiers()`
- **`CLIBackend` (abstract)** in `shared/planner.py` — separates LLM planning calls from execution; concrete: `GhCopilotBackend`, `ClaudeCodeBackend`
- **`CLIProvider` dataclass** in `shared/discovery.py` — self-describing provider with detection, command construction, cost rank; `BUILTIN_PROVIDERS` bootstraps discovery
- **`ProviderAdapter` dataclass** in `shared/adapters.py` — versioned capability contract for secondary adapters; capabilities: `EXECUTE`, `STREAM`, `REGISTER`, `TOKEN_USAGE`

### Data layer

- Single SQLite WAL at `~/.local/lib/threnody/cache.db`
- WAL mode + `synchronous=NORMAL` is intentional — routing cache data loss on crash is acceptable; for `approval_queue` and other durable tables, durability is operator responsibility via backup rotation (configurable via `cache.backup_keep` in `config.yaml`)
- All schema changes go through `Database._init_schema()` — no ad-hoc connections elsewhere
- Tables: `cache`, `plan_cache`, `artifacts`, `telemetry`, `adaptive_thresholds`, `agent_definitions`, `agent_audit`, `style_profiles`, `project_routing`, `project_settings`, `rework_events`, `subtask_patterns`, `escalations`, `speculation_log`, `swarm_runs`, `swarm_workers`, `swarm_events`, `routing_guards`, `routing_guard_executions`, `approval_queue`, `coordinator_round_checkpoints`, `plan_revisions`, `coordinator_amendments`, `fanout_telemetry`, `preview_tokens`, `memory`, and more

## Conventions

- **Type hints**: PEP 604 `X | None` (not `Optional[X]`). All public APIs fully annotated.
- **Logging**: `log = logging.getLogger(__name__)` at module top. Never `print()` for production output. Never silent `except: pass` — always `log.debug(..., exc_info=True)`.
- **Error style**: background subsystems (planner, learner, speculative) → best-effort + fallback. Surface APIs (entry points, MCP server) → explicit raise after logging.
- **DB access**: always use `db.conn()` — never access `_db._conn` directly. WAL mode + thread-local connections require the `conn()` accessor.
- **Schema changes**: add to `Database._init_schema()` only; use existing `_ensure_*` helpers for follow-up column/index migrations.
- **Write-safety boundary**: file-targeted flows must reuse `normalize_target_path()` / `is_within_repo()` from `shared/context.py` plus the snapshot/preview helpers — never write arbitrary paths directly.
- **Preferred routing sort**: when `preferred_routing` is configured for a tier, operator preference rank is the primary sort key and overrides `cost_rank`. `preferred_routing_by_caller` applies the same ordering to one host shell/caller only. Cost rank only dominates when no matching global or caller-specific preference is set.
- **Provider metadata is a contract**: routed/executed results carry `provider`, `provider_id`, `model`, `billing_tier`, `cost_rank`, `billing_source`, and sometimes effort metadata. Tests assert these end-to-end.
- **Learning is approval-gated**: pattern tracking in `shared/agents.py` flows through draft readiness and the approval queue before local activation or cross-CLI registration. Do not bypass the approval step.
- **New provider** touches three places: `CLIBackend` in `shared/planner.py`, `Provider` implementation in the provider dir, `CLIProvider` entry in `BUILTIN_PROVIDERS` in `shared/discovery.py`.
- **LLM output parsing**: reuse `_extract_json(raw)` from `shared/planner.py` — fenced JSON first, then brace-balancing fallback.
- **Pattern utilities**: reuse `pattern_hash` / `normalize_pattern` from `shared/agents.py`.
- **Config template vs installed**: `config.yaml` in the repo root is the template only. Runtime reads `~/.local/lib/threnody/config.yaml`. `install.sh` creates the installed copy only when absent — editing the template does not overwrite an existing install.
- **Instruction surface alignment**: when routing UX, tool names, install behavior, or host-shell integration changes, update `README.md`, `INSTRUCTIONS.md`, `CLAUDE.md`, `install.sh`, `.github/copilot-instructions.md`, and `shared/instructions.py` together. `routing_policy` in `config.yaml` controls instruction enforcement: `advisory` is the default for all shells (routing recommended, not mandatory); `guarded` requires `route_task` before code edits and enables Claude PreToolUse hooks. `strict` is a deprecated alias for `guarded`.
- **`execute_subtask` surgical edit modes**: use `mode=` to control how `target_file` is written:
  - `write` (default) — model output written verbatim. Safe for new files only.
  - `rewrite` — injects current file content, asks model for complete rewrite, guards against fragments with a length-ratio check (rejects if output < 50% of original). Max file: 32 KiB.
  - `blocks` — Aider-style SEARCH/REPLACE blocks (`shared/edit_blocks.py`). Token-efficient surgical edits. Max file: 128 KiB. Falls back to `retryable=True` on parse/match failure.
  - `patch` — provider returns unified diff; applied via `apply_unified_diff()` in `shared/snapshot.py`.
  All modes use `_write_file_with_audit` as the final write primitive.
- **Routing exceptions bypass the routing guard**: use `routing_exception_add/list/remove` MCP tools (or the DB helpers) to exempt specific calls from the guard. Valid `exception_type` values: `skill`, `filetype`, `project`, `command`, `caller`, `path`. Built-in exemptions cover `.md`, `.mdc`, and known AI assistant instruction files; every other filetype remains routed by default.

## Pull requests

Use scoped Conventional Commit titles: `fix(router): …`, `feat(planner): …`, `refactor(discovery): …`. Explain behavior changes and provider-contract impacts. List exact verification commands run. Call out config, schema, security, and installer changes.

Do not commit generated or machine-specific files: `providers.json`, `cache.db`, `*.db-wal`, `*.db-shm`, backup files, status files, credentials, or paths containing local usernames.

## Testing

Tests live in `tests/test_*.py`. `tests/conftest.py` provides function-scoped fixtures for hermetic DB isolation, path validation, and provider discovery mocking — use these rather than rolling your own setup/teardown.

Use `tempfile.TemporaryDirectory()` for ephemeral DB files in tests that don't use conftest fixtures. Never share DB state between tests.

`THRENODY_TEST_MODE=1` isolates discovery/execution from host-installed CLIs. The conftest autouse fixture sets this automatically.

Routing eval fixtures live in `tests/eval/` organised by tier (`low_tier/`, `medium_tier/`, `high_tier/`, `urgency/`, `fanout/`). `schema.json` and `README.md` in that directory describe the fixture contract. When adding a new routing signal, add a matching fixture and run `threnody eval baseline` to update the baseline.

## Config

`config.yaml` controls complexity-scoring signals, tier bounds, parallelism, speculation, and per-provider effort defaults. Loaded via `TGsConfig.from_yaml()` in `shared/config.py`.

`host_native` (in `config.yaml`, `HostNativeConfig`) controls how host-native wave runs report learning:
- `report_mode: batch` (default) — host accumulates per-agent results into the `run_log` and reports **once** at terminal (`report_host_swarm_complete` → `import_run_log`), so worker-wave `report_host_wave` calls are eliminated and the hot path matches native subagent spawning. `inline` is the legacy per-wave ingest, kept as a fallback (`report_mode: inline` reverts).
- `learning_capture: hook` (default) — PostToolUse hook appends run-log lines with zero model tokens; `model` makes the host pass agents in the terminal call; `off` disables per-agent capture. Resolved **per caller** by `effective_learning_capture()`: shells in `LEARNING_HOOK_CAPABLE_SHELLS` (claude-code, codex, cursor, github-copilot-cli — install.sh registers each CLI's native post-edit hook: Claude `PostToolUse`, Codex `PostToolUse`/apply_patch, Cursor `afterFileEdit`, Copilot `postToolUse`) use `hook`; others (Junie, OpenCode) fall back to `model` so no host loses learning. The per-wave elimination applies to all CLIs regardless. The standalone `learning_hook.py` parses all four payload shapes.
- `draft_ready_mode: deferred` (default) — `check_draft_ready` (the only LLM call in learning) runs off the hot path (terminal/warm-path), never on a reporting call.
- `runs_keep` — per-run log dir rotation count. Consensus waves are always reported mid-run regardless of mode (a failed quorum spawns a judge).

## Legal and provider compliance

- Threnody is not affiliated with or endorsed by any AI provider
- Provider terms, policies, and enforcement may change at any time without notice
- Host shells execute via `host_spawn` / `host_spawn_waves` (Agent/Task); when `host_spawn_waves` is present, spawn subagents — do not substitute direct edits on planned `target_files`. `execute_subtask` is utility-delegation only (opt-in); host→host subprocess delegation is blocked
- Dynamic Workflow emission (opt-in, claude-code only): when `routing_policy.shells.claude-code.workflow_emit` is set and a plan is fan-out shaped, the response also carries `workflow_emit: true` + a `workflow_script` (contract `emit_workflow`) the host launches via the **Workflow** tool instead of `host_spawn_waves`. Each `agent()` routes to its Threnody tier model (vanilla Workflow runs all agents on the session model). The host returns the run's `agents[]` to `report_workflow_result`, which records telemetry and — after a shape recurs across successful runs — enqueues an approval-gated draft exportable (via `shared/workflow_export.py`, `tune=True`) to a permanent, re-tuned, documented `.claude/workflows/<slug>.js` command. Requires Claude Code v2.1.154+ (operator opt-in implies it). Drive the whole loop with the **`/threnody-workflow`** skill
- Multi-queen consensus over the workflow path is **hybrid by default**: workers run via `workflow_script`; consensus queens run as a separate `consensus_wave` of read-only host agents whose verdicts go through `ingest_host_wave`/`report_host_wave`. The claude-code-only opt-in `routing_policy.shells.claude-code.consensus_in_workflow` renders the persona queen phase **inside** the workflow instead; `report_workflow_result(consensus=[...])` then tallies quorum via `shared/consensus.py` and returns a lazy `consensus_followup` judge round when needed. The decision always lives in Python (`consensus.py`), never in the emitted JS
- Host-native heuristic planning (`shared/heuristic_plan.py`) fans out one agent per file for webapp/fullstack intent; tasks starting with `REVIEW:` route to `shared/review_fanout.py` for per-file × dimension review DAG. Mid-run `expand_host_plan` adds agents for discovered files. Learning ingest merges handoff snapshots in `shared/host_learning.py`
- Host-native multi-queen consensus (when `swarm.consensus.enabled` + `host_native_enabled`, star/auto topology): `execute_swarm` appends a `wave_kind=consensus` wave of read-only persona-diverse review queens after the worker waves. The host spawns them, reports JSON decisions via `report_host_wave`; `ingest_host_wave` tallies quorum and, if unresolved, returns `consensus_followup` for a single lazy judge round. Host-native queens always run on the host model (never cross providers) — diversity comes from personas, not models. Subprocess path uses `run_coordinator_consensus` and may cross providers when `consensus_cross_provider_enabled`
- Override router-only hosts via `providers.router_only_allow_execution`; see `docs/LEGAL.md`

`routing_exceptions` is an exemption list, not a code-file allowlist. Add only extra non-code surfaces there; do not enumerate code languages or config formats.

`routing_policy` controls guarded vs advisory routing instructions per shell. Default: `advisory` for all shells. To override:

```yaml
routing_policy:
  mode: custom
  shells:
    claude-code:
      mode: guarded
    github-copilot-cli:
      mode: advisory
```

Changing `routing_policy` regenerates managed instruction blocks in all host-shell config files. To preview or manually copy the generated block for a shell without running the installer:

```bash
python3 -m shared.instructions claude-code --config ~/.local/lib/threnody/config.yaml
python3 -m shared.instructions github-copilot-cli --config ~/.local/lib/threnody/config.yaml
```

See `KNOWN_BOTTLENECKS.md` for current performance constraints — notably: serial planner/synthesis stages (unchanged), configurable speculation pool (`parallelism.speculation_workers`), and warm-path eval batch parallelism (`parallelism.warm_path_workers`).
