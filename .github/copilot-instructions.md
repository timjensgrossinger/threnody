# Threnody

## Build, test, lint, and runtime commands

```bash
# Full test suite
python3 -m pytest tests/ -v

# Single test file
python3 -m pytest tests/test_router.py -v

# Single test by name
python3 -m pytest tests/test_router.py::test_base_score_low_tier -v

# Hermetic test run that ignores host-installed AI CLIs
THRENODY_TEST_MODE=1 python3 -m pytest tests/ -v

# Lightweight syntax check (used by installer)
python3 -m py_compile mcp_server.py shared/router.py

# Routing eval suite — run before changing config.yaml or eval fixtures
threnody eval run
threnody eval run --filter low,urgency
python3 -m shared.routing_eval       # repo-local fallback

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

## High-level architecture

**Shared brain, provider-specific entry points.** All logic lives in `shared/`; provider directories (`copilot/`, `claude-code/`, `gemini/`, `codex/`, `cursor/`, `junie/`, `mistral/`, `opencode/`, `blackbox/`, etc.) are thin wrappers that instantiate a concrete `Provider` and delegate to the shared core.

Three execution paths run concurrently:

| Path | Trigger | Role |
|---|---|---|
| **Hot** (blocking) | Every task | Route → plan → execute → return result |
| **Warm** (async bg) | After subtask completes | `shared/eval.py` — rework detection + quality scoring |
| **Cold** (periodic bg) | Telemetry accumulation | `shared/adaptive.py` — EMA threshold adjustment |

### Hot path (blocking)

```text
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
| `star` | `_execute_star_runner` | Coordinator rounds with worker fanout |

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
| `shared/settings_wizard.py` | Interactive first-run and reconfiguration wizard |
| `shared/provider_factory.py` | Registry-driven resolver that maps `CLIProvider.name` → concrete `Provider` subclass for `Orchestrator` construction |
| `mcp_server.py` | MCP server (JSON-RPC/stdio) — lazy-init, ~41 public tools |
| `shell/ghc.sh` | Multi-agent shell script backing the `ghc` / `ghcs` / `ghce` aliases |
| `shell/threnody-watch` | Live monitoring daemon; run in a separate terminal alongside the MCP server |

`mcp_server.py` is ~294 KB — use `grep` rather than reading it whole. All meaningful logic is in `shared/`; the server is a thin dispatch layer.

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

## Key conventions

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
- **Instruction surface alignment**: when routing UX, tool names, install behavior, or host-shell integration changes, update `README.md`, `INSTRUCTIONS.md`, `CLAUDE.md`, `install.sh`, `.github/copilot-instructions.md`, and `shared/instructions.py` together. `routing_policy` in `config.yaml` controls instruction enforcement: `guarded` (default for Claude Code) requires `route_task` before code edits; `advisory` (default for GitHub Copilot CLI) renders guidance only. `strict` is a deprecated alias for `guarded`.
- **`execute_subtask` + `target_file` writes model output verbatim**: when the model returns only a fragment, only that fragment lands in the file — the rest is lost. Safe for new files or full rewrites; dangerous for surgical edits to large existing files. Use direct file tools for targeted edits.
- **Routing exceptions bypass the routing guard**: use `routing_exception_add/list/remove` MCP tools (or the DB helpers) to exempt specific calls from the guard. Valid `exception_type` values: `skill`, `filetype`, `project`, `command`, `caller`, `path`.

## Testing

Tests live in `tests/test_*.py`. `tests/conftest.py` provides function-scoped fixtures for hermetic DB isolation, path validation, and provider discovery mocking — use these rather than rolling your own setup/teardown.

Use `tempfile.TemporaryDirectory()` for ephemeral DB files in tests that don't use conftest fixtures. Never share DB state between tests.

`THRENODY_TEST_MODE=1` isolates discovery/execution from host-installed CLIs. The conftest autouse fixture sets this automatically.

Routing eval fixtures live in `tests/eval/` organised by tier (`low_tier/`, `medium_tier/`, `high_tier/`, `urgency/`, `fanout/`). `schema.json` and `README.md` in that directory describe the fixture contract. When adding a new routing signal, add a matching fixture and run `threnody eval baseline` to update the baseline.

## Config

`config.yaml` controls complexity-scoring signals, tier bounds, parallelism, speculation, and per-provider effort defaults. Loaded via `TGsConfig.from_yaml()` in `shared/config.py`.

## Legal and provider compliance

- Threnody is not affiliated with or endorsed by any AI provider
- Provider terms, policies, and enforcement may change at any time without notice
- Host shells execute by default; utility delegation (OpenCode/Aider/local) is opt-in; host→host delegation is blocked
- Override router-only hosts via `providers.router_only_allow_execution`; see `docs/LEGAL.md`

`routing_exceptions` is an exemption list, not a code-file allowlist. Add only extra non-code surfaces; do not enumerate code languages or config formats. Built-in exemptions cover `.md`, `.mdc`, and known AI assistant instruction files; every other filetype remains routed by default unless explicitly exempted.

`routing_policy` controls guarded vs advisory routing instructions per shell. Default: `guarded` for Claude Code, `advisory` for all others. To override:

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
python3 -m shared.instructions github-copilot-cli --config ~/.local/lib/threnody/config.yaml
python3 -m shared.instructions claude-code --config ~/.local/lib/threnody/config.yaml
```

See `KNOWN_BOTTLENECKS.md` for current performance constraints — notably: serial planner/synthesis stages, single-lane speculative fallback, and sequential warm-path eval.
