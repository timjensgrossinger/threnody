<!-- GSD:project-start source:PROJECT.md -->
## Project

**Threnody**

Threnody is a CLI-native orchestration layer for AI coding workflows. It routes work to the cheapest capable model across installed AI shells, decomposes complex tasks into parallel workstreams, and shares one core brain across multiple CLI entry points.

This project is now focused on making that orchestration adapt better to your coding behavior, deepen cross-shell interoperability, and split complex prompts into smarter parallel routers without losing quality.

**Core Value:** Quietly choose the cheapest cross-shell execution path that still meets the required quality bar, so the system needs less manual correction over time.

### Constraints

- **Tech stack**: Keep the current Python + shell + SQLite architecture — improvements should extend the existing shared core rather than replace it
- **Compatibility**: First-class support must cover Claude Code CLI, GitHub Copilot CLI, and Gemini CLI
- **Quality**: Cheapest execution is only acceptable when it still satisfies the task's quality requirements
- **Cost**: Token spend should be minimized under the quality bar, not optimized in isolation
- **Architecture**: Preserve the shared-brain / multiple-entry-point pattern already established in `shared/`, `copilot/`, `claude-code/`, and `mcp_server.py`
<!-- GSD:project-end -->

<!-- GSD:stack-start source:codebase/STACK.md -->
## Technology Stack

## Languages
- Python 3.10+ — entire runtime and CLI codebase. Entry points and core modules live under `mcp_server.py` and `shared/` (e.g. `shared/config.py`, `shared/planner.py`). The installer enforces Python 3.10 in `install.sh` (lines that check `python3` version).
- Bash (installer and shell wrappers) — `install.sh`, `shell/ghc.sh`.
## Runtime
- CPython (3.10+) — all modules are written for Python and use stdlib modules (`subprocess`, `sqlite3`, `logging`, `pathlib`). See `mcp_server.py` and `shared/*`.
- pip for Python dependencies (no lockfile detected). `install.sh` installs `pyyaml` via `python3 -m pip install --user pyyaml` when missing (see `install.sh`).
- No Node/npm/Cargo/Go manifests detected (no `package.json`, `pyproject.toml` or `go.mod` in the repository root).
## Frameworks & Libraries
- No web/application framework. The project is a collection of CLI tools and a stdio JSON-RPC MCP server implemented in pure Python.
- SQLite (via Python stdlib `sqlite3`) with WAL mode. Database wrapper: `shared/db.py`. Default DB path is `~/.local/lib/threnody/cache.db` (see `shared/config.py` and `config.yaml`). The repo also contains an instance `cache.db` at the repository root (development artifact).
- `pyyaml` used to load `config.yaml` via `shared/config.py::TGsConfig.from_yaml()`.
- pytest-style tests present in `tests/` (e.g. `tests/test_db.py`, `tests/test_discovery.py`). No explicit `pytest.ini` or `tox` config detected.
## Key Dependencies (observed in code)
- `subprocess` — used widely to call external AI CLIs (`gh`, `claude`, `gemini`) from `shared/discovery.py`, `shared/planner.py`, `copilot/providers.py`, `claude-code/providers.py`.
- `sqlite3` — persistence layer in `shared/db.py` (WAL enabled).
- `json`, `logging`, `re`, `hashlib`, `pathlib`, `time` — used across `shared/`.
- `pyyaml` — configuration loader used by `shared/config.py` and referenced in `install.sh`. No other third-party packages are imported.
## Build / Dev / CLI tooling
- `install.sh` — installs files to `~/.local/lib/threnody`, ensures `pyyaml` is present, performs provider discovery, registers the MCP server with host CLIs, writes provider discovery JSON (`providers.json`), and syncs managed instruction blocks into `~/.claude/CLAUDE.md` and `~/.copilot/copilot-instructions.md`. See `install.sh` for exact steps.
- Shell helpers / aliases: `shell/ghc.sh` is sourced by the installer and provides the user-facing CLI wrappers (`ghc`, `ghcs`, `ghce`, `ghcw`) plus operator controls such as `threnody inspect ...` and `threnody tune ...`.
## Execution and Entry Points
- `mcp_server.py` — main MCP server run as `python3 ~/.local/lib/threnody/mcp_server.py`. It exposes JSON-RPC tools over stdio. It is registered by `install.sh` with host CLIs (GitHub Copilot and Claude Code) for MCP usage.
- Copilot CLI entry: `copilot/entry.py` — thin CLI wrapper that bootstraps shared core with Copilot provider. Typical invocation: `gh copilot mcp add Threnody -- python3 ~/.local/lib/threnody/mcp_server.py` (see `mcp_server.py` docs and `install.sh`).
- Claude Code entry: `claude-code/entry.py` — same pattern for Claude.
## Concurrency & Parallelism
- The orchestrator (`shared/orchestrator.py`) is wave-based and runs subtasks in parallel waves using subprocess-driven CLI calls. Planner decomposes tasks into waves (`shared/planner.py::build_waves`).
- SQLite uses WAL (`PRAGMA journal_mode=WAL`) to allow concurrent reads while writes are occurring (see `shared/db.py` line executing PRAGMA).
## Observed Patterns & Examples
- Cross-provider execution: `shared/discovery.py::ProviderRegistry.execute_cheapest()` tries available CLI providers cheapest-first and returns the first successful result. It uses `CLIProvider._build_command()` to construct provider-specific commands (e.g. `['gh','copilot','--','-p', prompt, '--model', model]`).
- Plan caching: `shared/planner.py` checks `Database.plan_get()` before calling the planner LLM backend (see `Planner.plan`).
- File-writing from model outputs: `mcp_server.py::handle_execute_subtask` will call `registry.execute_cheapest(...)` and then extract code fences or first code line with `_extract_code_for_file` before writing to `target_file`.
## Platform Requirements
- Python 3.10+
- `gh` (GitHub CLI), `claude` (Claude Code CLI), and/or `gemini` CLI for full functionality; at least one is required for normal operation (installer enforces presence of at least one — see `install.sh`).
- No hosted cloud dependencies required; this is a local CLI orchestration tool that depends on local installation of external AI CLIs. The MCP server runs as a local stdio process registered with host CLIs.
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

## Summary
## Language & Typing
- Python 3.10+ is used: files use modern type hints (PEP 604 union syntax `X | None`) and dataclasses. See `shared/config.py` (`TGsConfig`, `ThresholdConfig`) and `shared/planner.py` (`Subtask`, `ExecutionPlan`).
- All public function signatures include type annotations where feasible. Example: `def cache_get(self, task: str) -> tuple[str, str] | None:` in `shared/db.py`.
- Use explicit concrete return types rather than `Any`. Prefer `dict[str, Any]` only when necessary (see `TGsConfig.to_legacy_dict` in `shared/config.py`).
- New modules must include full type annotations for public APIs. Use `X | None` instead of `Optional[X]`.
## File & Module Organization
- Core reusable logic lives under `shared/`. Example modules:
- Entry points and provider wrappers live in their own directories: `copilot/` and `claude-code/`.
- Put shared domain logic under `shared/` and CLI / provider adapters under top-level directories named for the provider (`copilot/`, `claude-code/`).
- Keep tests in `tests/` (current pattern) rather than co-locating with modules.
## Naming Conventions
- Files: `snake_case.py` (e.g., `shared/speculative.py`, `shared/adaptive.py`).
- Modules and packages: lower-case singular nouns or domain names (e.g., `shared.router`, `shared.planner`).
- Classes: CapWords / PascalCase dataclasses and classes (e.g., `TGsConfig`, `TaskRouter`, `ExecutionPlan`).
- Functions: snake_case (e.g., `plan_get`, `build_waves`, `_extract_json`).
- Constants: UPPER_SNAKE (e.g., `PLAN_CACHE_TTL_HOURS`, `SPECULATION_MARGIN`).
- Private functions: single leading underscore for module-private helpers (e.g., `_extract_json`, `_plan_key`).
## Patterns and Idioms (with examples)
- Dataclasses for structured domain objects: `@dataclass` used in `shared/planner.py` for `Subtask`, `ExecutionPlan` and `TokenEstimate`.
- Single Database wrapper: `shared/db.py` exposes a `Database` class that owns sqlite3 connection and schema. Reuse a single instance where persistence is needed.
- Logging: each module calls `logging.getLogger(__name__)` at the top (e.g., `log = logging.getLogger(__name__)` in `shared/*`). Use `log.debug`, `log.info`, `log.warning`.
- Error handling style:
- CLI backends are abstracted behind `CLIBackend` in `shared/planner.py`. Concrete adapters (`GhCopilotBackend`, `ClaudeCodeBackend`) implement `call(prompt, model, timeout)`.
- JSON extraction from LLM output uses a robust helper `_extract_json(raw: str) -> dict | None` in `shared/planner.py`. Follow the same pattern for any LLM output parsing (try fenced JSON first, then search for braces and balance them).
- When integrating a provider, implement the `CLIBackend` interface and place the adapter in the provider directory (e.g., `copilot/providers.py`).
- Use `Database` for persisted counters, thresholds and caches — do not open ad-hoc sqlite connections in multiple places.
- Log at appropriate levels and never swallow exceptions silently; always log `exc_info=True` for debugging on unexpected errors.
## Helper & Utility Reuse
- Style learning helpers: `shared/style.py` exposes `StyleLearner`, `DecompositionPrefs`, and lower-level detectors (`_detect_naming`, `_detect_type_hints`) which are reused by `shared/planner.py` (preamble injection). Reuse these functions rather than re-implementing heuristics.
- Pattern normalization and hashing exist in `shared/agents.py` (used heavily by the emergent agent system). Reuse `pattern_hash` and `normalize_pattern` for any pattern comparisons.
- Database schema lives in `shared/db.py`. Any new persisted table should be added through `_init_schema()` to centralize migration behavior.
## Error Handling
- Common pattern: "best-effort and continue". Many functions catch broad exceptions and log, then fall back to defaults. Example: `TaskRouter._get_thresholds()` will attempt to compute adaptive thresholds via `shared/adaptive.py` and on any exception return static thresholds with a debug log.
- When writing new code, prefer explicit fallbacks that produce a reasonable default rather than raising, for background/background-adjacent components (planners, learners, speculative executor).
- For surface APIs (entry points and server), prefer explicit error return or raise after logging — callers are expected to handle errors.
- Avoid silent `except: pass`. If you must swallow an exception, call `log.debug(..., exc_info=True)` so diagnostics are available.
- Database operations should be best-effort but commit explicitly and surface failure via logs.
## Docstrings & Comments
- Public modules and classes include module-level docstrings and descriptive comments. Follow the existing style (compact module docstring explaining purpose) as in `shared/db.py` and `shared/planner.py`.
- Functions include short triple-quoted docstrings explaining purpose and return types. Keep docstrings focused on behavior and edge-cases; do not repeat argument names.
## Testing Conventions (overview)
- Tests live in `tests/` with file names `tests/test_*.py`. Tests import modules by inserting `sys.path` to the repository root. See `tests/test_planner.py` (line 8): `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`.
- Tests use `tempfile` and ephemeral sqlite DB files for isolation (see `tests/test_db.py` and many agent/style tests that use `tempfile.TemporaryDirectory()`).
## Code Review Checklist (prescriptive)
- New public functions and classes have type annotations and docstrings.
- Logging uses module logger (`logging.getLogger(__name__)`). Do not use `print()` for production logging.
- Reuse `Database` for persistent state and register schema changes in `shared/db.py` only.
- Add unit tests to `tests/` that follow the established patterns: use `tempfile` for DBs, simple in-file mocks where needed (see `tests/test_planner.py` MockBackend), and keep tests independent.
- Maintain backward-compatible config parsing in `shared/config.py` by reading YAML via `TGsConfig.from_yaml`.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

## Pattern Overview
- **Shared brain, swappable providers** — All entry points share the same `shared/` core. Provider-specific code maps tier labels (low/medium/high) to concrete models and CLI commands.
- **Three-layer execution model** — Hot path (blocking user-facing work), Warm path (async background quality evaluation), Cold path (threshold adjustment from telemetry).
- **No HTTP APIs** — All LLM interactions go through subprocess calls to installed CLI tools (`gh copilot`, `claude`, `gemini`). Zero API keys.
- **Keyword-based routing, LLM-based planning** — The router is a fast heuristic (no LLM call). The planner calls an LLM to decompose complex tasks into dependency-ordered subtask waves.
- **Cross-provider execution** — A `ProviderRegistry` singleton discovers installed CLI tools at startup and routes each subtask to the cheapest available provider, with automatic fallback.
## Layers
- Purpose: Define all tunables, hard bounds, constants, and YAML-loading logic
- Location: `shared/config.py`, `config.yaml`
- Contains: `TGsConfig` dataclass, `ThresholdConfig`, `SubtaskTemplate` definitions, token ceilings, intent signal weights
- Depends on: `pyyaml`
- Used by: Every other module (router, planner, orchestrator, adaptive, speculative, eval)
- Purpose: Classify task complexity into tiers (low/medium/high) without an LLM call — instant, deterministic
- Location: `shared/router.py`
- Contains: `TaskRouter` class, `RoutingDecision` dataclass, keyword override checks, complexity scoring, intent modifier computation, project/time-based modifier learning
- Depends on: `shared/config.py`, `shared/db.py`, `shared/adaptive.py`
- Used by: `mcp_server.py`, `copilot/entry.py`, `claude-code/entry.py`, `shared/orchestrator.py`
- Purpose: Decompose complex tasks into ordered subtask DAGs using an LLM backend
- Location: `shared/planner.py`
- Contains: `Planner` class, `CLIBackend` abstract class + `GhCopilotBackend` / `ClaudeCodeBackend`, `Subtask` and `ExecutionPlan` dataclasses, `build_waves()` topological sort, `match_template()` for template bypass
- Depends on: `shared/config.py`, `shared/db.py`, `shared/agents.py`, `shared/style.py`
- Used by: `shared/orchestrator.py`, `mcp_server.py`, entry points
- Purpose: Execute subtask waves via providers, enforce kill switches, detect rework, run background evaluation
- Location: `shared/orchestrator.py`
- Contains: `Orchestrator` class, `Provider` abstract class, `AgentResult` dataclass, wave execution logic, synthesis, fleet formatting
- Depends on: `shared/planner.py`, `shared/config.py`, `shared/db.py`, `shared/eval.py`, `shared/context.py`, `shared/speculative.py`
- Used by: `mcp_server.py`, entry points
- Purpose: Map tier labels to CLI commands + model names for each AI tool
- Location: `copilot/providers.py`, `claude-code/providers.py`
- Contains: `CopilotProvider` (implements `Provider`), `ClaudeCodeProvider` (implements `Provider`), tier→model maps, cross-routing detection, budget awareness (Claude)
- Depends on: `shared/orchestrator.Provider`, `shared/discovery.py`
- Used by: Entry points, `mcp_server.py`
- Purpose: Universal cross-provider execution bridge — detect installed CLIs, route to cheapest available
- Location: `shared/discovery.py`
- Contains: `CLIProvider` dataclass, `ProviderRegistry` singleton, `BUILTIN_PROVIDERS` list (github-copilot, claude-code, gemini-cli), output cleaning regexes, caller auto-detection, sandbox isolation for subprocess calls
- Depends on: None (self-contained — no imports from other Threnody modules)
- Used by: `mcp_server.py`, `copilot/providers.py`, `claude-code/providers.py`
- Purpose: SQLite WAL database for caching, telemetry, thresholds, agent definitions, style profiles
- Location: `shared/db.py`
- Contains: `Database` class, 10 tables (cache, plan_cache, telemetry, adaptive_thresholds, agent_definitions, style_profiles, project_routing, rework_events, subtask_patterns, escalations), structural hashing
- Depends on: `shared/config.py` (paths and TTLs)
- Used by: Nearly every module
- Purpose: Adaptive threshold adjustment, agent emergence, style learning, rework detection
- Location: `shared/adaptive.py`, `shared/agents.py`, `shared/style.py`, `shared/eval.py`
- Contains: EMA-based threshold computation, emergent agent creation/dedup/matching, code style profiling, scope-aware rework classification, background evaluator
- Depends on: `shared/db.py`, `shared/config.py`
- Used by: `shared/router.py` (adaptive), `shared/planner.py` (agents, style), `shared/orchestrator.py` (eval)
- Purpose: Read relevant source code from disk and inject into agent prompts
- Location: `shared/context.py`
- Contains: `FileReference` dataclass, `extract_references()`, `read_file_context()`, `enrich_subtask()`, function boundary detection, path traversal guards
- Depends on: `shared/config.py`
- Used by: `shared/orchestrator.py` (enriches subtasks before execution)
- Purpose: Run borderline-score subtasks on both tiers simultaneously, accept the cheaper result if it passes quality checks
- Location: `shared/speculative.py`
- Contains: `SpeculativeExecutor` class, `is_borderline()`, `check_output_quality()`, `SpeculativeResult` dataclass
- Depends on: `shared/orchestrator.Provider`, `shared/config.py`, `shared/db.py`
- Used by: `shared/orchestrator.py`
## Data Flow
- All persistent state lives in SQLite WAL at `~/.local/lib/threnody/cache.db`
- Tables: result cache, plan cache, telemetry, adaptive thresholds, agent definitions, style profiles, project routing profiles, rework events, subtask patterns, escalations, speculation_log
- Concurrent reads from hot/warm/cold paths supported via WAL mode
- Lazy globals in `mcp_server.py` — components initialized on first tool call
## Key Abstractions
- Purpose: Decouple tier→model resolution and CLI execution from orchestration logic
- Defined in: `shared/orchestrator.py` (`Provider` abstract class)
- Implemented by: `copilot/providers.py` (`CopilotProvider`), `claude-code/providers.py` (`ClaudeCodeProvider`)
- Pattern: Abstract base class with `resolve_model(tier)`, `execute(subtask, model)`, `available_tiers()`
- Purpose: Abstract the LLM call for planning (different from Provider — planning is text-in/text-out, not subtask execution)
- Defined in: `shared/planner.py` (`CLIBackend` abstract class)
- Implemented by: `GhCopilotBackend`, `ClaudeCodeBackend`
- Pattern: `call(prompt, model, timeout) → str | None` via subprocess
- Purpose: Self-describing provider with detection, command construction, and execution
- Defined in: `shared/discovery.py` (`CLIProvider` dataclass)
- Instances: `BUILTIN_PROVIDERS` list — github-copilot, claude-code, gemini-cli
- Pattern: Each provider defines `name`, `binary`, `tier_models`, `cost_rank`, `detect_cmd`; `ProviderRegistry` auto-discovers which are installed
- Three tiers: `low`, `medium`, `high`
- Tier boundaries are adaptive (EMA) with hard-coded floors/ceilings that can never collapse
- Hard bounds in `shared/config.py`: low_max ∈ [0.50, 0.75], medium_max ∈ [0.75, 0.95]
- `ExecutionPlan` dataclass in `shared/planner.py`: analysis string, list of `Subtask`, dependency waves, strategy (parallel/sequential/dag), token estimates
- Subtasks are grouped into waves via `build_waves()` topological sort
- Serialized via `Planner.plan_to_dict()` for caching and JSON responses
## Entry Points
- Location: `mcp_server.py` (668 lines)
- Triggers: Registered as MCP server via `gh copilot mcp add` or `claude mcp add`; receives JSON-RPC over stdin/stdout
- Responsibilities: Lazy-init shared core, dispatch tool calls to handlers, MCP protocol management (initialize, tools/list, tools/call, ping)
- Tools: `plan_task`, `decompose_task`, `fleet_plan`, `route_task`, `cache_get`, `cache_put`, `cache_stats`, `execute_subtask`, `apply_preview`, `inspect_task`, `inspect_status`, `approval_queue_list`, `tune_show`, `check_providers`
- Location: `copilot/entry.py` (145 lines)
- Triggers: Called by `shell/ghc.sh` via `python3 copilot/entry.py <command> <args>`
- Responsibilities: Bootstrap shared core with `CopilotProvider` + `GhCopilotBackend`, dispatch CLI commands (plan, synthesise, route, cache-get, cache-put, cache-stats)
- Location: `claude-code/entry.py` (149 lines)
- Triggers: Called directly or via Claude Code custom instructions
- Responsibilities: Same as Copilot entry but with `ClaudeCodeProvider` + `ClaudeCodeBackend`
- Location: `shell/ghc.sh`
- Triggers: Sourced in `~/.zshrc`; user runs `ghc agent "task"`, `ghcs "question"`, `ghce "explain"`, or `threnody inspect status --project .`
- Responsibilities: Shell-level orchestration — calls `copilot/entry.py plan`, spawns parallel agents per wave using `_ghc_run_wave()`, calls synthesize, prints transparency tables, and exposes `threnody inspect ...` / `threnody tune ...` operator controls
- Location: `install.sh`
- Triggers: Manual run or `curl | bash`
- Responsibilities: Copy files to `~/.local/lib/threnody`, install pyyaml, register MCP server, append shell source to `~/.zshrc`, and sync managed custom-instruction blocks for Claude and Copilot
## Error Handling
- **Planner failures** → single-agent fallback (`Planner._single_agent_fallback()` returns one medium-tier subtask)
- **Provider failures** → next-cheapest provider via `ProviderRegistry.execute_cheapest()` fallback chain
- **Speculative execution failures** → fall back to normal execution path (try/except wraps entire speculative block)
- **MCP tool errors** → return JSON `{"error": ...}` with `isError: True` in response; never crash the server
- **CLI subprocess failures** → `FileNotFoundError` and `TimeoutExpired` caught per provider; return `None`
- **Kill switch** → when agent output exceeds token ceiling, flag `escalated=True` on `AgentResult`, log to escalations table
- **Adaptive threshold failures** → fall back to static thresholds from config
## Cross-Cutting Concerns
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

No project skills found. Add skills to any of: `.github/skills/`, `.agents/skills/`, `.cursor/skills/`, or `.github/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
