# install.sh `--plugin-mode` Implementation Spec

**Date:** 2026-06-12
**Status:** Approved for implementation
**Inputs:** `docs/install-audit.md`, `docs/plugin-design.md`, `install.sh`
**Author:** Claude (implementation spec)

---

## 0. Problem statement

`install.sh` today runs a single, monolithic flow: provider scan → file copy →
MCP registration → shell aliases → symlinks → first-run wizard → custom
instruction sync. This is the right UX for developers who want the full CLI
surface (`ghc`, `threnody`, `threnody-watch`).

It is the wrong UX for plugin/PyPI paths, where:

- The user installed via `/plugin install threnody@threnody` or
  `claude mcp add threnody -- uvx threnody-mcp` — the MCP entry point is
  **already registered** by the plugin manifest.
- There is no shell to integrate (`ghc` is irrelevant).
- There is no TTY for the wizard.
- Modifying `.zshrc` / `.bashrc` would be intrusive and unexpected.

The `--plugin-mode` flag introduces a strict subset of the installer that is
safe to run in plugin/PyPI/headless contexts and leaves the default (`curl | bash`)
flow byte-for-byte unchanged.

---

## 1. Exact arg-parsing change

### 1.1 Where to insert (line numbers)

The installer currently has **no argument-parsing loop**. Variables are set
entirely from environment (`${THRENODY_…:-…}` expansions on lines 18–25). The
first executable code after variable declarations is the `cleanup()` function
definition (line 36) followed by the source-detection block (line 51).

**The new argument-parsing block must be inserted after line 25 (the last
`THRENODY_FORCE_PORTABLE_COPY` declaration) and before line 27 (the `TMPDIR_CLONE=""` blank/init block).**

Insertion point: between lines 25 and 27.

### 1.2 New mode-control variables

Add these five variables immediately after line 25 (still in the variable
declaration section, before `TMPDIR_CLONE`):

```bash
# ── Plugin/pip mode flags (set by --plugin-mode / --from-pip) ───────────────
THRENODY_PLUGIN_MODE="${THRENODY_PLUGIN_MODE:-0}"
THRENODY_FROM_PIP="${THRENODY_FROM_PIP:-0}"
_REGISTER_MCP_OVERRIDE=""       # empty = mode-default; "1" = force on; "0" = force off
_SYNC_INSTRUCTIONS_OVERRIDE=""  # empty = mode-default; "1" = force on; "0" = force off
```

### 1.3 Argument-parsing loop

Insert the following block immediately after the four new variable declarations
(still between original lines 25 and 27):

```bash
# ── Argument parsing ─────────────────────────────────────────────────────────
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --plugin-mode)
            THRENODY_PLUGIN_MODE=1
            ;;
        --from-pip)
            THRENODY_FROM_PIP=1
            THRENODY_PLUGIN_MODE=1   # --from-pip implies --plugin-mode
            ;;
        --register-mcp)
            _REGISTER_MCP_OVERRIDE=1
            ;;
        --no-register-mcp)
            _REGISTER_MCP_OVERRIDE=0
            ;;
        --sync-instructions)
            _SYNC_INSTRUCTIONS_OVERRIDE=1
            ;;
        --no-sync-instructions)
            _SYNC_INSTRUCTIONS_OVERRIDE=0
            ;;
        --allow-no-host)
            THRENODY_ALLOW_NO_HOST=1
            ;;
        --skip-wizard)
            THRENODY_SKIP_WIZARD=1
            ;;
        --skip-deps)
            THRENODY_SKIP_DEPENDENCIES=1
            ;;
        --help|-h)
            cat <<'EOF'
Usage: install.sh [OPTIONS]

Options:
  --plugin-mode          Minimal install: skip shell aliases, symlinks, wizard,
                         and instruction sync. Use when already registered via
                         a plugin manifest or PyPI.
  --from-pip             Like --plugin-mode but also skips file copy (source
                         already installed via pip/uvx). Only runs provider
                         scan, providers.json write, and optionally MCP
                         registration.
  --register-mcp         Force MCP registration in host CLI configs even in
                         plugin-mode (default: skip in plugin-mode).
  --no-register-mcp      Force skip MCP registration (overrides default-on
                         behavior in normal mode; useful for re-runs).
  --sync-instructions    Force custom-instruction sync even in plugin-mode.
  --no-sync-instructions Force skip custom-instruction sync.
  --allow-no-host        Continue even if no host AI CLI is detected.
  --skip-wizard          Skip first-run configuration wizard.
  --skip-deps            Skip pip dependency installation.
  --help, -h             Show this help and exit.

Environment equivalents (all overridable):
  THRENODY_PLUGIN_MODE=1
  THRENODY_FROM_PIP=1
  THRENODY_ALLOW_NO_HOST=1
  THRENODY_SKIP_WIZARD=1
  THRENODY_SKIP_DEPENDENCIES=1
EOF
            exit 0
            ;;
        *)
            echo "  Unknown argument: $1" >&2
            echo "  Run with --help for usage." >&2
            exit 1
            ;;
    esac
    shift
done

# ── Derive DO_* control flags from mode + overrides ─────────────────────────
# These are the authoritative gate variables used throughout the script.
# Defaults: everything on in normal mode; strategic subset on in plugin-mode.

if [[ "$THRENODY_PLUGIN_MODE" = "1" ]]; then
    # Force wizard skip (no TTY in plugin contexts)
    THRENODY_SKIP_WIZARD=1

    # File copy: on by default even in plugin-mode (seeds the data dir)
    # --from-pip turns it off because the wheel already placed the files
    if [[ "$THRENODY_FROM_PIP" = "1" ]]; then
        DO_FILE_COPY=0
    else
        DO_FILE_COPY=1
    fi

    # MCP registration: OFF by default in plugin-mode
    # (plugin manifest already registered it; double-registration duplicates
    # the server entry in host CLI config files)
    DO_MCP_REGISTRATION="${_REGISTER_MCP_OVERRIDE:-0}"

    # Shell integration (.zshrc/.bashrc source line): always OFF
    DO_SHELL_INTEGRATION=0

    # Symlinks to ~/.local/bin: always OFF
    DO_SYMLINKS=0

    # Custom instruction sync: OFF by default
    DO_INSTRUCTION_SYNC="${_SYNC_INSTRUCTIONS_OVERRIDE:-0}"
else
    # Normal mode: everything on (preserves existing behavior exactly)
    DO_FILE_COPY=1
    DO_MCP_REGISTRATION="${_REGISTER_MCP_OVERRIDE:-1}"
    DO_SHELL_INTEGRATION=1
    DO_SYMLINKS=1
    DO_INSTRUCTION_SYNC="${_SYNC_INSTRUCTIONS_OVERRIDE:-1}"
fi
```

### 1.4 Complete updated variable-declaration + arg-parsing section (drop-in replacement for lines 18–26)

This is the full replacement for the top of the file from `INSTALL_DIR=` through
the end of the original variable block, ready to paste:

```bash
INSTALL_DIR="${THRENODY_INSTALL_DIR:-${SWITCHYARD_INSTALL_DIR:-$HOME/.local/lib/threnody}}"
REPO_URL="${THRENODY_REPO_URL:-${SWITCHYARD_REPO_URL:-https://github.com/timjensgrossinger/threnody.git}}"
THRENODY_ALLOW_NO_HOST="${THRENODY_ALLOW_NO_HOST:-${SWITCHYARD_ALLOW_NO_HOST:-0}}"
THRENODY_SKIP_DEPENDENCIES="${THRENODY_SKIP_DEPENDENCIES:-${SWITCHYARD_SKIP_DEPENDENCIES:-0}}"
THRENODY_SKIP_WIZARD="${THRENODY_SKIP_WIZARD:-${SWITCHYARD_SKIP_WIZARD:-0}}"
THRENODY_TEST_FAIL_AFTER_COPY="${THRENODY_TEST_FAIL_AFTER_COPY:-${SWITCHYARD_TEST_FAIL_AFTER_COPY:-0}}"
THRENODY_PROVIDER_SCAN_TEST_MODE="${THRENODY_PROVIDER_SCAN_TEST_MODE:-${SWITCHYARD_PROVIDER_SCAN_TEST_MODE:-0}}"
THRENODY_FORCE_PORTABLE_COPY="${THRENODY_FORCE_PORTABLE_COPY:-${SWITCHYARD_FORCE_PORTABLE_COPY:-0}}"

# Plugin/pip mode flags
THRENODY_PLUGIN_MODE="${THRENODY_PLUGIN_MODE:-0}"
THRENODY_FROM_PIP="${THRENODY_FROM_PIP:-0}"
_REGISTER_MCP_OVERRIDE=""
_SYNC_INSTRUCTIONS_OVERRIDE=""

# Argument parsing
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --plugin-mode)          THRENODY_PLUGIN_MODE=1 ;;
        --from-pip)             THRENODY_FROM_PIP=1; THRENODY_PLUGIN_MODE=1 ;;
        --register-mcp)         _REGISTER_MCP_OVERRIDE=1 ;;
        --no-register-mcp)      _REGISTER_MCP_OVERRIDE=0 ;;
        --sync-instructions)    _SYNC_INSTRUCTIONS_OVERRIDE=1 ;;
        --no-sync-instructions) _SYNC_INSTRUCTIONS_OVERRIDE=0 ;;
        --allow-no-host)        THRENODY_ALLOW_NO_HOST=1 ;;
        --skip-wizard)          THRENODY_SKIP_WIZARD=1 ;;
        --skip-deps)            THRENODY_SKIP_DEPENDENCIES=1 ;;
        --help|-h)
            # (help text as above)
            exit 0 ;;
        *)
            echo "  Unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

# Derive DO_* gate flags
if [[ "$THRENODY_PLUGIN_MODE" = "1" ]]; then
    THRENODY_SKIP_WIZARD=1
    [[ "$THRENODY_FROM_PIP" = "1" ]] && DO_FILE_COPY=0 || DO_FILE_COPY=1
    DO_MCP_REGISTRATION="${_REGISTER_MCP_OVERRIDE:-0}"
    DO_SHELL_INTEGRATION=0
    DO_SYMLINKS=0
    DO_INSTRUCTION_SYNC="${_SYNC_INSTRUCTIONS_OVERRIDE:-0}"
else
    DO_FILE_COPY=1
    DO_MCP_REGISTRATION="${_REGISTER_MCP_OVERRIDE:-1}"
    DO_SHELL_INTEGRATION=1
    DO_SYMLINKS=1
    DO_INSTRUCTION_SYNC="${_SYNC_INSTRUCTIONS_OVERRIDE:-1}"
fi
```

---

## 2. Steps to SKIP in plugin-mode

All five of these steps must be wrapped in `if [[ "$DO_…" = "1" ]]; then … fi`
guards. The existing code inside each block is **unchanged**; only the outer
conditional is added.

### 2.1 Shell alias setup — source line in `.zshrc` / `.bashrc` (lines 596–610)

**Current code (lines 596–610):**
```bash
if [[ -n "$SHELL_RC" ]]; then
    if grep -qF "Threnody" "$SHELL_RC" 2>/dev/null; then
        info "Shell integration already in $SHELL_RC"
    else
        { … } >> "$SHELL_RC"
        info "Added to $SHELL_RC …"
    fi
else
    warn "Could not detect shell RC file. Add this line manually:"
    echo "       $SHELL_SOURCE"
fi
```

**Wrap with:**
```bash
if [[ "$DO_SHELL_INTEGRATION" = "1" ]]; then
    # (existing lines 596–610 unchanged)
fi
```

The `SHELL_RC` / `SHELL_SOURCE` variable declarations at lines 587–594 are
harmless to leave outside the guard since they are cheap assignments. Move them
inside the guard as well for clarity if desired.

**In plugin-mode:** the entire `echo "🐚 Shell integration"` banner and body are
skipped. No `.zshrc` / `.bashrc` modification occurs.

### 2.2 Symlinks to `~/.local/bin` (lines 612–620)

**Current code (lines 612–620):**
```bash
mkdir -p "$HOME/.local/bin"
for entry_point in threnody-watch threnody switchyard-watch switchyard ghc; do
    if [[ -f "$INSTALL_DIR/shell/$entry_point" ]]; then
        chmod +x "$INSTALL_DIR/shell/$entry_point"
        ln -sf "$INSTALL_DIR/shell/$entry_point" "$HOME/.local/bin/$entry_point"
        info "Symlinked $entry_point → ~/.local/bin/$entry_point"
    fi
done
```

**Wrap with:**
```bash
if [[ "$DO_SYMLINKS" = "1" ]]; then
    mkdir -p "$HOME/.local/bin"
    for entry_point in threnody-watch threnody switchyard-watch switchyard ghc; do
        # (unchanged)
    done
fi
```

Note: the `mkdir -p "$HOME/.local/bin"` is inside the guard because it only
makes sense when we are creating symlinks there. The directory itself is not
needed for plugin-mode.

### 2.3 GHC wrappers / shell integration header (lines 582–586)

The section header and `SHELL_SOURCE` / `SHELL_RC` declarations:

```bash
# ── Shell integration ──────────────────────────────────────────────────────
echo ""
echo "🐚 Shell integration"

SHELL_SOURCE="source $INSTALL_DIR/shell/ghc.sh"
SHELL_RC=""

if [[ -n "${ZSH_VERSION:-}" ]] || [[ "$SHELL" == */zsh ]]; then
    SHELL_RC="$HOME/.zshrc"
elif [[ -n "${BASH_VERSION:-}" ]] || [[ "$SHELL" == */bash ]]; then
    SHELL_RC="$HOME/.bashrc"
fi
```

These lines (582–594) are all shell-integration setup. Wrap the entire block
from line 582 through line 620 inside a single `if [[ "$DO_SHELL_INTEGRATION" = "1" ]] || [[ "$DO_SYMLINKS" = "1" ]]; then` guard, since symlink creation does not need `SHELL_RC` / `SHELL_SOURCE`. Or, more cleanly, keep the section header and variable setup inside `DO_SHELL_INTEGRATION`, and put the symlink loop in a separate `DO_SYMLINKS` guard immediately after.

**Recommended clean split:**
```bash
if [[ "$DO_SHELL_INTEGRATION" = "1" ]]; then
    echo ""
    echo "🐚 Shell integration"
    # lines 587–610 (SHELL_SOURCE, SHELL_RC, RC append logic)
fi

if [[ "$DO_SYMLINKS" = "1" ]]; then
    # lines 612–620 (mkdir + symlink loop)
fi
```

### 2.4 First-run configuration wizard (lines 622–630)

**Current code (lines 622–630):**
```bash
if [[ ! -f "$INSTALL_DIR/config.yaml" && "$THRENODY_SKIP_WIZARD" != "1" ]]; then
    echo ""
    echo "  First-time setup -- configure providers and routing"
    echo "   (Ctrl+C to skip -- run 'threnody settings' anytime later)"
    echo ""
    python3 "$INSTALL_DIR/shared/settings_wizard.py" "$INSTALL_DIR/config.yaml" || true
fi
```

**No structural change needed here.** The arg-parsing block above sets
`THRENODY_SKIP_WIZARD=1` whenever `THRENODY_PLUGIN_MODE=1`, so the existing
`"$THRENODY_SKIP_WIZARD" != "1"` guard already handles this. No line-level
change is needed on lines 622–630.

However, for explicitness, add a comment:
```bash
# In plugin-mode, THRENODY_SKIP_WIZARD is forced to 1 above; this block
# runs only in normal interactive installs.
if [[ ! -f "$INSTALL_DIR/config.yaml" && "$THRENODY_SKIP_WIZARD" != "1" ]]; then
    # (unchanged)
fi
```

### 2.5 Custom instruction sync (lines 632–1017)

Covered in detail in §4 below. Short answer: **skip by default in plugin-mode**;
wrap the entire section (lines 632–1017) in `if [[ "$DO_INSTRUCTION_SYNC" = "1" ]]; then`.

---

## 3. Steps to KEEP mandatory in plugin-mode

These steps run unconditionally in both normal and plugin-mode. No gate is added
around them.

### 3.1 Python 3.10+ validation (lines 89–98)

Unchanged. Required by `mcp_server.py` and `shared/`. Plugin install fails here
if Python is too old — correct behavior.

### 3.2 Legacy Switchyard migration (lines 83–87)

Unchanged. Cheap directory rename; idempotent; important for users who had the
pre-rename install.

### 3.3 Provider availability scan (lines 102–222)

Unchanged. This:
- Detects which host CLIs are installed (`gh`, `claude`, `codex`, `cursor`,
  `junie`, `opencode`)
- Writes `$PROVIDER_SCAN_JSON` and `$PROVIDER_SCAN_ENV` temp files
- Validates at least one host CLI exists (or `THRENODY_ALLOW_NO_HOST=1`)

**Must run in plugin-mode** because `providers.json` (written in §3.5) depends
on it, and the plugin's MCP server uses `providers.json` as the cold-start
inventory at first tool call.

### 3.4 pyyaml (and mandatory-only deps) installation (lines 232–239)

The dependency install block currently installs both mandatory (`pyyaml`) and
optional (`rich`, `questionary`) packages. In plugin-mode, only `pyyaml` is
needed; `rich`/`questionary` are wizard-only UI deps.

**Pseudocode for gating optional UI deps:**
```bash
if [[ "$THRENODY_SKIP_DEPENDENCIES" != "1" ]]; then
    pip3 install --quiet "pyyaml>=6.0,<7" ... || warn "pyyaml install failed; config parsing may be unavailable"
    # Optional UI deps — skip in plugin-mode (wizard won't run)
    if [[ "$THRENODY_PLUGIN_MODE" != "1" ]]; then
        pip3 install --quiet rich questionary ... || warn "UI deps unavailable; wizard will use plain-text fallback"
    fi
fi
```

This is a refinement on top of the existing `THRENODY_SKIP_DEPENDENCIES` gate.

### 3.5 File copy to `$INSTALL_DIR` (lines 248–357)

**Mandatory in plugin-mode when `--from-pip` is NOT set.** Gate:

```bash
if [[ "$DO_FILE_COPY" = "1" ]]; then
    # (existing lines 248–357 unchanged — rsync/Python fallback, backup, syntax check)
fi
```

When `DO_FILE_COPY=0` (i.e. `--from-pip` mode), the source files are already
present via pip's install into the venv. The file-copy block is skipped entirely.
The `INSTALL_DIR` is still used as the data directory (config, cache.db,
providers.json live there regardless of how the code was delivered).

### 3.6 Provider discovery write to disk / `providers.json` (lines 343–351)

**Mandatory in both modes.** This writes the provider inventory snapshot that
`shared/config._load_providers()` and `settings_wizard.py` use on cold start.

Even in `--from-pip` mode where file copy is skipped, `providers.json` must be
written to `$INSTALL_DIR`. The write currently happens inside the file-copy
block (around line 343). It must be **extracted** out of the `DO_FILE_COPY`
guard and run unconditionally:

```bash
if [[ "$DO_FILE_COPY" = "1" ]]; then
    # (rsync / Python fallback copy — lines 248–342)
fi

# Always write providers.json, even when file copy is skipped (--from-pip)
# (lines 343–351 — Python snippet that reads PROVIDER_SCAN_JSON and writes
#  $INSTALL_DIR/providers.json)
mkdir -p "$INSTALL_DIR"
python3 - "$INSTALL_DIR" "$PROVIDER_SCAN_JSON" <<'PY'
# (existing providers.json write snippet, unchanged)
PY
info "Written providers.json"
```

**This is the only structural change needed inside the mandatory section.**

### 3.7 MCP server registration in host CLI config files (lines 359–506)

Mandatory in **normal mode** and **opt-in in plugin-mode** via `--register-mcp`.

Wrap the entire MCP registration section:

```bash
if [[ "$DO_MCP_REGISTRATION" = "1" ]]; then
    echo ""
    echo "🔌 MCP server registration"
    # (existing lines 359–580 unchanged)
fi
```

See §4 rationale for why this defaults to OFF in plugin-mode.

---

## 4. Recommendation on custom instruction sync

**Recommendation: skip custom instruction sync by default in plugin-mode.**

### What it does (lines 632–1017)

The custom instruction sync:
1. Renders shell-specific routing policy blocks via `shared/instructions.py`
2. Writes or patches managed sections into:
   - `~/.claude/CLAUDE.md` (Claude Code)
   - `~/.copilot/copilot-instructions.md` (GitHub Copilot CLI)
   - `~/.codex/AGENTS.md` (Codex)
   - `~/.cursor/rules/threnody.mdc` (Cursor)
   - `~/.junie/AGENTS.md` (Junie)
3. Installs tier agent templates + skill manifests to `~/.claude/agents`,
   `~/.cursor/agents`
4. Optionally registers the Claude PreToolUse routing enforcement hook in
   `~/.claude/settings.json`

### Why skip in plugin-mode

1. **The plugin manifest already handles skills.** `.claude-plugin/plugin.json`
   has `"skills": "./skills"` which delivers the six `threnody-*` skills as part
   of the plugin install. Writing skill manifests via install.sh would be
   redundant and could conflict with what the plugin manager placed.

2. **Default routing is advisory, not guarded.** `routing_policy.mode: advisory`
   (the default) does not require managed blocks in `CLAUDE.md` — the model
   operates on guidelines naturally surfaced by system prompts and tool
   descriptions. The instruction sync is critical only when `mode: guarded`
   injects a PreToolUse hook. Plugin users start advisory; they opt into guarded
   explicitly.

3. **Modifying user instruction files without consent is intrusive.** Plugin
   users expect the plugin manifest to declare its surface area. Silently patching
   `~/.claude/CLAUDE.md` is surprising behavior and may overwrite sections the
   user has customized.

4. **Re-runnable via `--sync-instructions`.** Users who want the full
   instruction sync after a plugin install can run:
   ```bash
   curl -fsSL https://threnody.dev/install.sh | bash -s -- --plugin-mode --sync-instructions
   ```
   or simply run `./install.sh --sync-instructions` from a local clone.

5. **Consistent with the `plugin.json` hook design decision.** `plugin-design.md`
   §4.2 explicitly notes the PreToolUse hook is NOT bundled by default in the
   plugin manifest because the advisory default needs no CLAUDE.md edits. The
   installer should mirror this decision.

### Implementation

Wrap the entire custom instruction section (lines 632–1017):

```bash
if [[ "$DO_INSTRUCTION_SYNC" = "1" ]]; then
    echo ""
    echo "📝 Custom instructions (shell-specific coordination policy)"
    # (existing lines 637–1017 unchanged)
else
    if [[ "$THRENODY_PLUGIN_MODE" = "1" ]]; then
        echo ""
        echo "  Skipping custom instruction sync (plugin-mode)"
        echo "  Re-run with --sync-instructions to write routing policy blocks."
    fi
fi
```

---

## 5. `--from-pip` companion flag

### Purpose

When Threnody has been installed via `pip install threnody-mcp` or `uvx
threnody-mcp`, the Python source files are already present inside the venv (or
uvx's managed cache). Running `install.sh --from-pip` seeds the **data
directory** (`$INSTALL_DIR`) without redundantly re-copying the code files.

### What `--from-pip` does (derives from `--plugin-mode`, plus skips file copy)

```
--from-pip implies --plugin-mode
```

| Step | `--plugin-mode` | `--plugin-mode --from-pip` |
|---|---|---|
| Python version check | Run | Run |
| Legacy migration | Run | Run |
| Provider availability scan | Run | Run |
| pyyaml install | Run (mandatory only) | Run |
| File copy (`rsync`/Python fallback) | Run (`DO_FILE_COPY=1`) | **Skip** (`DO_FILE_COPY=0`) |
| providers.json write | Run | Run |
| MCP registration | Skip (default) | Skip (default) |
| Shell integration | Skip | Skip |
| Symlinks | Skip | Skip |
| Wizard | Skip | Skip |
| Custom instruction sync | Skip (default) | Skip (default) |

### Source path behavior in `--from-pip` mode

When `DO_FILE_COPY=0`, `SOURCE_DIR` is still detected at the top of the script
(the repo/archive/standalone detection at lines 51–73 runs unconditionally). In
`--from-pip` mode, source detection finding no `mcp_server.py` is not an error
— the user is not providing source, only invoking the installer for its data-dir
setup logic.

Add a guard after the source-detection block:

```bash
# When --from-pip is set, source detection failures are non-fatal
# (the PyPI wheel provides the runtime; install.sh only seeds the data dir)
if [[ -z "$SOURCE_DIR" && "$THRENODY_FROM_PIP" != "1" ]]; then
    error "Cannot locate Threnody source directory. Run from inside the repo or use the curl installer."
fi
if [[ -z "$SOURCE_DIR" && "$THRENODY_FROM_PIP" = "1" ]]; then
    warn "Source directory not found — running in data-dir-only mode (--from-pip)."
    SOURCE_DIR=""   # provider scan uses $INSTALL_DIR as fallback; file copy is skipped
fi
```

The provider scan (lines 102–222) already uses `sys.argv[1]` (the source dir)
to import `shared.discovery`. In `--from-pip` mode, pass `$INSTALL_DIR` as the
source path instead, since the pip-installed package lives there (or in a venv
that can be resolved from it). Add this conditional substitution immediately
before the provider scan:

```bash
# In --from-pip mode, the importable source is the pip-installed package,
# not a repo checkout. Pass INSTALL_DIR as the scan root; shared/discovery.py
# will be importable from the venv's site-packages.
_SCAN_SOURCE_DIR="${SOURCE_DIR:-$INSTALL_DIR}"
python3 - "$_SCAN_SOURCE_DIR" "$PROVIDER_SCAN_JSON" "$PROVIDER_SCAN_ENV" <<'PY'
# (existing provider scan script — no change needed internally)
PY
```

### `--from-pip` one-liner

```bash
curl -fsSL https://threnody.dev/install.sh | bash -s -- --from-pip
```

This is the recommended post-`pip install threnody-mcp` setup command. It seeds
`~/.local/lib/threnody/providers.json`, validates host CLI availability, and
prints a summary without touching shell config or symlinks.

---

## 6. How the curl-pipe-bash one-liner changes for plugin-mode

### Standard plugin-mode (file copy included)

For users who want to add the on-disk data directory after a plugin install
(e.g., so they can run `threnody settings` or `threnody doctor` later):

```bash
curl -fsSL https://threnody.dev/install.sh | bash -s -- --plugin-mode
```

The `bash -s --` idiom passes flags to the piped script. The `--` separates
`bash` flags from script arguments.

### PyPI / pip path (no file copy)

```bash
curl -fsSL https://threnody.dev/install.sh | bash -s -- --from-pip
```

### Plugin-mode with forced MCP re-registration

For PyPI-path users who are **not** using the plugin manifest (e.g., running
`uvx threnody-mcp` directly via `claude mcp add`) and need install.sh to register
the MCP entry point:

```bash
curl -fsSL https://threnody.dev/install.sh | bash -s -- --plugin-mode --register-mcp
```

### Plugin-mode with instruction sync enabled

For users who want routing policy blocks written into their host CLI instruction
files (e.g., to adopt `guarded` routing enforcement):

```bash
curl -fsSL https://threnody.dev/install.sh | bash -s -- --plugin-mode --sync-instructions
```

### Env-var equivalent (for CI/automation)

```bash
curl -fsSL https://threnody.dev/install.sh | THRENODY_PLUGIN_MODE=1 bash
curl -fsSL https://threnody.dev/install.sh | THRENODY_FROM_PIP=1 bash
```

### Why `bash -s -- <flags>` and not `bash <(curl …) <flags>`

The `curl | bash` form does not support passing arguments to the script
directly. The POSIX-portable idiom is `bash -s -- <args>` where `-s` tells bash
to read from stdin and `--` ends bash's own option parsing, causing remaining
arguments to be forwarded as `$1`, `$2`, etc. to the script. This is the form
used by e.g. Homebrew, rustup, and Bun installers.

The `<(curl …)` form (process substitution) is bash-specific and fails on zsh
when curl is still running — avoid it for a public one-liner.

---

## 7. Summary of all install.sh changes

### New / modified lines (delta from current install.sh)

| Change | Location | Type |
|---|---|---|
| Add `THRENODY_PLUGIN_MODE` / `THRENODY_FROM_PIP` / `_*_OVERRIDE` vars | After line 25 | Insert |
| Add `while [[ "$#" -gt 0 ]]` arg-parsing loop + `DO_*` derivation | After line 25 | Insert |
| Update comment block at top (`# Usage:`) | Lines 11–16 | Edit |
| Add `--from-pip` source-dir guard after standalone detection | After line 73 | Insert |
| Add `_SCAN_SOURCE_DIR` substitution before provider scan | Before line 104 | Insert |
| Extract `providers.json` write out of file-copy block | Lines 343–351 | Refactor |
| Wrap file-copy block in `DO_FILE_COPY` guard | Lines 248–357 | Wrap |
| Wrap MCP registration section in `DO_MCP_REGISTRATION` guard | Lines 359–580 | Wrap |
| Gate optional UI deps (`rich`, `questionary`) on `! PLUGIN_MODE` | Lines 240–246 | Edit |
| Wrap shell integration section in `DO_SHELL_INTEGRATION` guard | Lines 582–610 | Wrap |
| Wrap symlink loop in `DO_SYMLINKS` guard | Lines 612–620 | Wrap |
| Add comment on wizard skip behavior | Line 624 | Edit |
| Wrap custom instruction sync in `DO_INSTRUCTION_SYNC` guard | Lines 632–1017 | Wrap |

### Lines that are byte-for-byte unchanged in normal mode

Every line of the existing script that runs today under `curl | bash` continues
to run identically when no flags are passed. The `DO_*` variables all default to
`1` when `THRENODY_PLUGIN_MODE=0`, so all existing wrapping `if` guards evaluate
true and the existing code executes unchanged.

---

## 8. Test matrix

| Invocation | DO_FILE_COPY | DO_MCP | DO_SHELL | DO_SYMLINKS | DO_WIZARD | DO_INSTRUCTIONS |
|---|---|---|---|---|---|---|
| `./install.sh` (default) | 1 | 1 | 1 | 1 | (config-gated) | 1 |
| `--plugin-mode` | 1 | 0 | 0 | 0 | 0 | 0 |
| `--plugin-mode --register-mcp` | 1 | 1 | 0 | 0 | 0 | 0 |
| `--plugin-mode --sync-instructions` | 1 | 0 | 0 | 0 | 0 | 1 |
| `--from-pip` | 0 | 0 | 0 | 0 | 0 | 0 |
| `--from-pip --register-mcp` | 0 | 1 | 0 | 0 | 0 | 0 |
| `THRENODY_PLUGIN_MODE=1` (env) | 1 | 0 | 0 | 0 | 0 | 0 |
| `--skip-wizard` only | 1 | 1 | 1 | 1 | 0 | 1 |

---

## 9. Open questions / deferred decisions

1. **`providers.json` extraction from the file-copy block.** The exact line
   numbers where the providers.json Python snippet sits inside the file-copy
   section need to be confirmed before implementation (the audit says ~343–351
   but the full install.sh was not read past line 680 for this spec). Confirm
   with `grep -n "providers.json" install.sh` before implementing §3.6.

2. **`--from-pip` source-dir fallback for provider scan.** The existing scan
   script imports `shared.discovery` via `sys.path.insert(0, source_dir)`. When
   `source_dir` is the `uvx` venv path, this may not be the right importable
   location. A safer fallback for `--from-pip` is to call
   `python3 -c "import threnody.shared.discovery; …"` directly (leveraging the
   pip-installed package) instead of path-inserting a checkout dir. Defer to
   implementation.

3. **Whether to seed a default `config.yaml` in plugin-mode.** The plugin-design
   doc mentions optionally seeding a minimal config from `config.example.yaml`
   to suppress the first-run hint cleanly. This spec defers that decision;
   `mcp_server.py` defaults are safe without it.

4. **`threnody settings` accessibility in `--from-pip` mode.** When file copy is
   skipped and symlinks are not created, the user cannot run `threnody settings`
   unless they add `~/.local/lib/threnody/shell/threnody` to their PATH manually.
   Consider whether `--from-pip` should print a note about this.
