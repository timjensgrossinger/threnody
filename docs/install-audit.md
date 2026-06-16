# Threnody Install Surface Audit

**Date:** 2026-06-12  
**Scope:** install.sh, settings_wizard.py, mcp_server.py, shared/config.py  
**Question:** What must happen at plugin-install time vs first MCP call vs lazily?

## Executive Summary

The Threnody install process is **two-phase with lazy initialization**:

1. **Plugin-install time (install.sh):** Heavy lifting—provider discovery, dependency installation, MCP registration, shell integration, and optional first-run config
2. **First MCP call (mcp_server.py):** Lazy initialization—config loading defaults to sane values if missing, database opens on first tool invocation
3. **Lazy (settings_wizard.py):** Runs only if `config.yaml` doesn't exist AND user doesn't skip with `THRENODY_SKIP_WIZARD=1`

---

## 1. install.sh Steps (Complete List)

### Repo/Archive Detection (Lines 51–73)
- Detects if running from cloned repo, extracted archive, or standalone mode
- For standalone (curl | bash), clones repo to temp directory

### Pre-flight Checks (Lines 75–97)
1. Python 3.10+ validation (required)
2. Legacy Switchyard migration: moves `~/.local/lib/switchyard` → `~/.local/lib/threnody` if needed

### Provider Availability Scan (Lines 102–222)
- **Creates:** `PROVIDER_SCAN_JSON` temp file with live provider detection results
- **Creates:** `PROVIDER_SCAN_ENV` temp file with boolean flags (HAS_GH, HAS_CLAUDE, HAS_CODEX, etc.)
- **Calls:** `shared.discovery.installer_provider_inventory(verify_readiness=True)` to check for installed CLI tools
- **Validates:** At least one host CLI must exist (gh, claude, codex, cursor, junie, opencode) OR `THRENODY_ALLOW_NO_HOST=1`
- **Gated:** Entire install fails if no host CLIs found (unless override set)

### Dependency Installation (Lines 232–246)
- Installs `pyyaml` via pip (gated by `THRENODY_SKIP_DEPENDENCIES`)
- Installs UI deps: `rich`, `questionary` (gated by skip flag)
- **Non-fatal:** Warnings logged if pip fails; installer continues

### File Copy (Lines 248–357)
- **Preserves:** `config.yaml`, `cache.db`, `backup/` directory
- **Uses:** `rsync` (preferred) or Python fallback for portable copy
- **Backup:** Pre-install backup of existing cache.db if present
- **Syntax check:** Validates `mcp_server.py` and `shared/router.py` compile
- **Idempotent:** Overwrites all code files except config and database

### Provider Discovery Written to Disk (Lines 343–351)
- **Creates:** `$INSTALL_DIR/providers.json` with static provider inventory
  - Used by `settings_wizard.py` on first-run config
  - Used by `shared/config.py._load_providers()` if present
  - Falls back to `FALLBACK_PROVIDERS` if missing

### MCP Server Registration (Lines 359–506)
Registers MCP server entry points in host CLI config files:

| Host CLI | Config File | Gated By |
|----------|-------------|----------|
| Claude Code | `~/.claude.json` | `HAS_CLAUDE=1` |
| GitHub Copilot CLI | `~/.copilot/mcp-config.json` | Always (creates if missing) |
| OpenCode | Manual (interactive) | `HAS_OPENCODE=1` (info only) |
| Codex | `~/.codex/config.toml` | `HAS_CODEX=1` |
| Cursor | `~/.cursor/mcp.json` | `HAS_CURSOR=1` |
| Junie | `~/.junie/mcp/mcp.json` | `HAS_JUNIE=1` |

**Entry point:** `python3 <INSTALL_DIR>/mcp_server.py`

### Shell Integration (Lines 582–620)
- **Detects shell:** zsh (.zshrc) or bash (.bashrc)
- **Adds:** `source $INSTALL_DIR/shell/ghc.sh` to shell RC (idempotent)
- **Symlinks:** CLI entry points to `~/.local/bin/` (threnody, ghc, threnody-watch, switchyard, etc.)

### First-Run Configuration Wizard (Lines 622–630)
- **Condition:** Only runs if `$INSTALL_DIR/config.yaml` does NOT exist AND `THRENODY_SKIP_WIZARD != 1`
- **Calls:** `python3 shared/settings_wizard.py <INSTALL_DIR>/config.yaml`
- **Non-blocking:** Errors swallowed; can be run later via `threnody settings`

### Custom Instructions Sync (Lines 632–1017)
- **Renders:** Shell-specific routing instructions based on config (or defaults if config missing)
- **Syncs to:** AI host CLI instruction files:
  - Claude Code: `~/.claude/CLAUDE.md` + `~/.claude/settings.json` (PreToolUse hook)
  - GitHub Copilot: `~/.copilot/copilot-instructions.md`
  - Codex: `~/.codex/AGENTS.md`
  - Cursor: `~/.cursor/rules/threnody.mdc`
  - Junie: `~/.junie/AGENTS.md`
- **Installs:** Tier agent templates to `~/.claude/agents`, `~/.cursor/agents`; bundled Threnody skills to `~/.claude/skills`, `~/.cursor/skills`, `~/.agents/skills`, `~/.codex/skills`; flat agent guides to `~/.copilot/agents`, `~/.config/opencode/agent`
- **Hook install:** Registers Claude PreToolUse hook for routing enforcement (if enabled in config)

---

## 2. settings_wizard.py First-Run Detection

**Entry point:** `if __name__ == "__main__"` (lines 884–895)

```python
if len(sys.argv) > 1:
    _candidate = BASE_DIR.joinpath(sys.argv[1]).resolve()
    if not str(_candidate).startswith(str(BASE_DIR.resolve())):
        print(f"Error: config path must be inside {BASE_DIR}", file=sys.stderr)
        sys.exit(2)
    _path = _candidate
sys.exit(0 if run_wizard(_path) else 1)
```

**First-run detection:**
- Called by install.sh **only if `config.yaml` doesn't exist** (line 624)
- Takes explicit path argument: `settings_wizard.py $INSTALL_DIR/config.yaml`
- **Does NOT** auto-detect missing config internally—relies on install.sh to gate it

**What it does:**
1. **Loads providers.json** (lines 103–127)—if missing, uses `FALLBACK_PROVIDERS`
2. **Interactive 4-step wizard** (if rich + questionary available) or plain text fallback:
   - Step 1: Select which providers to enable
   - Step 2: Per-caller routing restrictions (if 2+ host CLIs)
   - Step 3: Tier preferences + usage window thresholds
   - Step 3.75: Routing enforcement mode (guarded vs advisory)
   - Step 3.6: Delegation utilities (opencode, aider)
   - Step 4: Review & confirm write
3. **Writes config.yaml** with:
   - `providers.disabled` (list of excluded providers)
   - `providers.caller_allowlists` (per-caller routing allowlists)
   - `providers.preferred_routing` (tier → provider mappings)
   - `providers.usage_windows` (budget thresholds per provider)
   - `providers.delegation_utilities_enabled` (boolean)
   - `routing_policy` (enforcement mode + per-shell overrides)

**Cancellation:** Ctrl+C skips config write; user can run `threnody settings` later to re-run

---

## 3. mcp_server.py Startup & Initialization

**Entry point:** `main()` function (lines 2644–2680)

### Lazy Initialization Pattern (Lines 2020–2026, 548–681)

On **first tool invocation**, `_ensure_init()` is called to load global state:

```python
def handle_plan_task(args: dict) -> dict:
    global _config, _db, _router, _planner, _orchestrator
    if _planner is None or _db is None or _config is None:
        config, db, router, planner, orchestrator = _ensure_init()  # ← Lazy init
    else:
        config, db, router, planner, orchestrator = _config, _db, _router, _planner, _orchestrator
```

### _ensure_init() Steps (Lines 548–681)

1. **Double-checked locking** (lines 556–561) to prevent race conditions
2. **Config loading** (line 568):
   ```python
   config = TGsConfig.from_yaml()
   ```
   - **No first-run detection here**—loads from `CONFIG_YAML` path
   - If file missing: returns `TGsConfig.defaults()` (sane defaults)
   - If file exists but empty/invalid YAML: logs warning, returns defaults
   - See config.py lines 1404–1410

3. **Database initialization** (line 569):
   ```python
   db = Database(config.db_path, backup_keep=config.db_backup_keep)
   ```
   - Opens/creates SQLite cache.db
   - Creates tables on first run
   - Performs WAL mode setup

4. **Router initialization** (line 570):
   ```python
   router = TaskRouter(config)
   ```

5. **Provider registry** (lines 571–582):
   - Tries to load via `shared.discovery.get_registry()`
   - Falls back to `GhCopilotBackend()` if unavailable

6. **Planner initialization** (line 583):
   ```python
   planner = Planner(config, backend, db)
   ```

7. **Provider resolution** (lines 584–595):
   - Tries to resolve default provider from registry
   - Falls back to `CopilotProvider()` if unavailable

8. **Orchestrator initialization** (lines 608–617):
   ```python
   orchestrator = Orchestrator(
       config, provider, planner, db, 
       project_root=str(_active_workspace_root()),
       ...
   )
   ```

9. **Background daemons** (lines 635–651):
   - Health probe loop (every 30s probes quarantined providers)
   - Warm path background loop (speculative execution)

10. **Shutdown handlers** (lines 653–671):
    - Registers atexit handler to close database
    - Registers SIGTERM handler (main thread only)

### Key First-Run Behaviors

- **No config file:** Uses `TGsConfig.defaults()` — all routing goes to detected host CLIs
- **Empty database:** Tables auto-created by Database class
- **Missing providers.json:** Falls back to `FALLBACK_PROVIDERS` hardcoded in shared/config.py
- **No host CLIs:** Runtime discovery via `shared.discovery` will still work (slower, best-effort)
- **Error on init:** Exception caught and re-raised; MCP client receives "Internal error" (-32603)

---

## 4. shared/config.py Config Loading (Lines 1404–1410)

```python
@classmethod
def from_yaml(cls, path: Path | None = None) -> "TGsConfig":
    """Load config from YAML file, falling back to defaults."""
    path = path or CONFIG_YAML
    if not path.exists():
        log.info("No config.yaml found, using defaults")
        return cls.defaults()
    ...
```

**Key properties of `TGsConfig.defaults()`:**

| Setting | Default | Purpose |
|---------|---------|---------|
| `providers` | Empty dict | No disabled providers, no caller allowlists, no preferences |
| `routing_policy.mode` | "default" (advisory for all shells) | Safe mode—permissive routing |
| `parallelism.enabled` | True | Wave-level parallelism on |
| `thresholds.low_max` | 0.55 | Low-medium tier boundary (adaptive) |
| `thresholds.medium_max` | 0.80 | Medium-high tier boundary (adaptive) |
| `planner_model` | "claude-sonnet-4-6" | Planning backbone |
| `code_review` | False | No auto-review |
| `escalation_retry_enabled` | True | Retry on planner errors |
| `reasoning_scoring_enabled` | True | Reasoning signal scoring on |

---

## 5. Shell-Alias/GHC Setup (Skippable in Plugin Mode)

### Always Run
- MCP server registration (host CLI config files)—**non-negotiable for plugin mode**
- Provider discovery scan—**non-negotiable** (validates host CLI availability)

### Optional/Skippable
| Step | Gated By | Purpose | Plugin-Skippable |
|------|----------|---------|------------------|
| Shell integration (.zshrc/.bashrc) | Shell detection | CLI commands (ghc, threnody) | ✅ Yes |
| Symlinks to ~/.local/bin | Hard-coded | Path convenience | ✅ Yes |
| First-run wizard | `config.yaml` missing + `THRENODY_SKIP_WIZARD` | Interactive config | ✅ Yes (run later) |
| Custom instructions sync | MCP host CLI detected | AI host-specific instructions | ✅ Mostly (but good to sync) |

---

## 6. Mandatory vs Optional for Plugin-Only Install

### MANDATORY (Must happen at plugin-install time)

1. **Provider availability scan** (install.sh lines 102–222)
   - Detects host CLI availability
   - Validates at least one host CLI exists
   - Fails if none found (unless `THRENODY_ALLOW_NO_HOST=1`)
   - Populates `providers.json` for fallback

2. **File copy** (install.sh lines 248–357)
   - Installs source code
   - Preserves existing config.yaml + cache.db
   - Validates syntax

3. **pyyaml dependency** (install.sh lines 232–239)
   - Required for config parsing
   - Semi-mandatory (installer continues if fails, but planner may crash)

4. **MCP server registration** (install.sh lines 359–506)
   - Registers entry points in host CLI configs
   - Without this, host CLI won't see the MCP server

### OPTIONAL (Can skip for pure plugin installs)

1. **UI dependencies** (rich, questionary) — graceful fallback to plain prompts
2. **Shell integration** — not needed if not using CLI commands
3. **Symlinks** — not needed if not using CLI commands  
4. **First-run wizard** — can be run later; config defaults are usable
5. **Custom instructions sync** — nice-to-have for AI instruction contextualization

---

## 7. Minimum Requirements Before First MCP Tool Call

### At Install Time
- [x] Source files copied to `$INSTALL_DIR`
- [x] `providers.json` written (provider availability snapshot)
- [x] At least one host CLI detected
- [x] MCP server registered in host CLI config
- [x] pyyaml available (or custom YAML parser fallback)

### At First MCP Call Time (auto-initialized by _ensure_init())
- [x] `config.yaml` — **optional**; defaults used if missing
- [x] `cache.db` — **auto-created** if missing
- [x] Provider registry — **discovered lazily**; falls back if unavailable
- [x] Host CLI availability — **re-probed** if providers.json stale

### Never Needed Before First Call
- Shell integration
- CLI symlinks
- UI dependencies
- Custom instructions sync

---

## 8. Timeline Example: First Install → First Use

```
1. User: curl -fsSL https://... | bash
   ↓
2. install.sh runs:
   a. Detects repo/archive/standalone
   b. Validates Python 3.10+
   c. Scans providers → providers.json
   d. Installs pyyaml
   e. Copies files
   f. Registers MCP in ~/.claude.json (if Claude Code found)
   g. Adds to ~/.zshrc / ~/.bashrc
   h. Runs settings_wizard.py → config.yaml (unless skipped)
   i. Syncs custom instructions → ~/.claude/CLAUDE.md
   ↓
3. User restarts shell (or source ~/.zshrc)
   ↓
4. User opens Claude Code, enables Threnody MCP
   ↓
5. Claude Code calls mcp_server.py plan_task
   ↓
6. mcp_server.py → _ensure_init() → _ensure_init()
   a. Loads config.yaml (or defaults)
   b. Opens cache.db (or creates)
   c. Initializes Database, Router, Planner, Orchestrator
   d. Starts background daemons
   ↓
7. First plan returned to Claude Code
```

---

## 9. Dependency Graph

```
install.sh (no MCP server needed)
├── Detects Python 3.10+
├── Detects host CLI (gh, claude, codex, cursor, junie, opencode)
├── Installs pyyaml (optional if manual install of yaml)
├── Copies source files
├── Writes providers.json
└── Registers MCP in host CLI configs

mcp_server.py on first call (lazy initialization)
├── TGsConfig.from_yaml()  ← config.yaml optional; defaults if missing
│   ├── Reads providers.json (fallback to FALLBACK_PROVIDERS)
│   ├── Parses config.yaml (or returns TGsConfig.defaults())
│   └── Validates routing_policy, thresholds, per-caller settings
├── Database(cache.db)  ← auto-created if missing
├── TaskRouter(config)
├── ProviderRegistry.get() ← lazy provider discovery
├── Planner(config, backend, db)
└── Orchestrator(config, provider, planner, db)

settings_wizard.py (optional; on first-run if config missing)
├── Loads providers.json (or FALLBACK_PROVIDERS)
└── Writes config.yaml with routing preferences
```

---

## 10. Summary Table: Install-Time vs MCP-Time vs Lazy

| Activity | When | Blocking | Optional | Notes |
|----------|------|----------|----------|-------|
| Repo detection | Install | No | N/A | Standalone vs cloned |
| Python validation | Install | Yes | N/A | Must be 3.10+ |
| Host CLI scan | Install | Yes | N/A | Must find ≥1 CLI |
| pyyaml install | Install | No | Partial | Config parsing needs it |
| File copy | Install | Yes | N/A | Core code |
| providers.json write | Install | Yes | N/A | Provider inventory |
| MCP registration | Install | Yes | N/A | Host CLI integration |
| Shell integration | Install | No | Yes | CLI convenience only |
| First-run wizard | Install | No | Yes | Can run later |
| Config validation | MCP-first-call | No | Yes | Defaults used if missing |
| DB initialization | MCP-first-call | Yes | N/A | Auto-created on open |
| Provider registry | MCP-first-call | No | Partial | Lazy discovery fallback |
| Background daemons | MCP-first-call | No | N/A | Health probes + warm path |

---

## Conclusion

Threnody's install surface cleanly separates **plugin-install concerns** from **runtime concerns**:

- **install.sh** handles discovery, dependency injection, and registration (everything needed to make MCP discoverable by host CLIs)
- **mcp_server.py** defers config/db init until first tool call (lazy, safe defaults)
- **settings_wizard.py** is optional; runs only if config missing and user doesn't skip

This design allows **plugin-mode installation** to be very lightweight (just copy files, register MCP) while still providing **full interactive setup** for users who want custom routing/provider preferences.

For a **pure plugin-only install**, these steps are sufficient:
1. Run install.sh with `THRENODY_SKIP_WIZARD=1`
2. Ensure at least one host CLI is installed (or use `THRENODY_ALLOW_NO_HOST=1`)
3. MCP server is ready on first call (config defaults to all providers, advisory routing)
