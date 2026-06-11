#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Threnody installer
#
# Three modes:
#   1. Run from inside a cloned repo   → copies files to ~/.local/lib/threnody
#   2. Run from a downloaded zip/tar   → same as above
#   3. Run standalone (curl | bash)    → clones the repo first, then installs
#
# Usage:
#   git clone git@github.com:timjensgrossinger/threnody.git && cd threnody && ./install.sh
#   — or —
#   curl -fsSL https://raw.githubusercontent.com/timjensgrossinger/threnody/main/install.sh | bash
# ─────────────────────────────────────────────────────────────────────────────

INSTALL_DIR="${THRENODY_INSTALL_DIR:-${SWITCHYARD_INSTALL_DIR:-$HOME/.local/lib/threnody}}"
REPO_URL="${THRENODY_REPO_URL:-${SWITCHYARD_REPO_URL:-https://github.com/timjensgrossinger/threnody.git}}"
THRENODY_ALLOW_NO_HOST="${THRENODY_ALLOW_NO_HOST:-${SWITCHYARD_ALLOW_NO_HOST:-0}}"
THRENODY_SKIP_DEPENDENCIES="${THRENODY_SKIP_DEPENDENCIES:-${SWITCHYARD_SKIP_DEPENDENCIES:-0}}"
THRENODY_SKIP_WIZARD="${THRENODY_SKIP_WIZARD:-${SWITCHYARD_SKIP_WIZARD:-0}}"
THRENODY_TEST_FAIL_AFTER_COPY="${THRENODY_TEST_FAIL_AFTER_COPY:-${SWITCHYARD_TEST_FAIL_AFTER_COPY:-0}}"
THRENODY_PROVIDER_SCAN_TEST_MODE="${THRENODY_PROVIDER_SCAN_TEST_MODE:-${SWITCHYARD_PROVIDER_SCAN_TEST_MODE:-0}}"
THRENODY_FORCE_PORTABLE_COPY="${THRENODY_FORCE_PORTABLE_COPY:-${SWITCHYARD_FORCE_PORTABLE_COPY:-0}}"

TMPDIR_CLONE=""
PROVIDER_SCAN_JSON=""
PROVIDER_SCAN_ENV=""
INSTRUCTION_RENDER_DIR=""

info()  { echo "  ✅ $*"; }
warn()  { echo "  ⚠️  $*" >&2; }
error() { echo "  ❌ $*" >&2; exit 1; }

cleanup() {
    local path
    for path in \
        "$PROVIDER_SCAN_JSON" \
        "$PROVIDER_SCAN_ENV" \
        "$INSTRUCTION_RENDER_DIR" \
        "$TMPDIR_CLONE"
    do
        if [[ -n "$path" && -e "$path" ]]; then
            rm -rf -- "$path"
        fi
    done
}
trap cleanup EXIT INT TERM

# ── Detect source directory ──────────────────────────────────────────────────

SOURCE_DIR=""

if [[ -f "$(dirname "$0")/mcp_server.py" ]]; then
    # Running from inside the repo / extracted archive
    SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
elif [[ -f "./mcp_server.py" ]]; then
    SOURCE_DIR="$(pwd)"
else
    # Standalone mode — clone the repo first
    echo ""
    echo "📦 Threnody — standalone installer"
    echo ""

    command -v git >/dev/null 2>&1 || error "git is required but not found on PATH"

    TMPDIR_CLONE="$(mktemp -d)"
    echo "  Cloning from $REPO_URL ..."
    git clone --quiet "$REPO_URL" "$TMPDIR_CLONE/threnody" || error "Clone failed — do you have access to the repo?"
    SOURCE_DIR="$TMPDIR_CLONE/threnody"
    info "Cloned to temporary directory"
fi

# ── Pre-flight checks ───────────────────────────────────────────────────────

echo ""
echo "🔧 Threnody installer"
echo "   Source:  $SOURCE_DIR"
echo "   Target:  $INSTALL_DIR"
echo ""

LEGACY_INSTALL_DIR="$HOME/.local/lib/switchyard"
if [[ "$INSTALL_DIR" == "$HOME/.local/lib/threnody" && -d "$LEGACY_INSTALL_DIR" && ! -e "$INSTALL_DIR" ]]; then
    mv "$LEGACY_INSTALL_DIR" "$INSTALL_DIR"
    info "Migrated previous Switchyard install → $INSTALL_DIR"
fi

command -v python3 >/dev/null 2>&1 || error "python3 is required but not found on PATH"

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ "$PYTHON_MAJOR" -lt 3 ]] || [[ "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 10 ]]; then
    error "Python 3.10+ required (found $PYTHON_VERSION)"
fi
info "Python $PYTHON_VERSION"

# ── Check for CLI tools ─────────────────────────────────────────────────────

PROVIDER_SCAN_JSON="$(mktemp)"
PROVIDER_SCAN_ENV="$(mktemp)"
python3 - "$SOURCE_DIR" "$PROVIDER_SCAN_JSON" "$PROVIDER_SCAN_ENV" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

source_dir = Path(sys.argv[1]).resolve()
if not source_dir.is_absolute():
    raise SystemExit(f"source_dir not absolute: {source_dir}")
json_path = Path(sys.argv[2]).resolve()
if not json_path.is_absolute():
    raise SystemExit(f"json_path not absolute: {json_path}")
env_path = Path(sys.argv[3]).resolve()
if not env_path.is_absolute():
    raise SystemExit(f"env_path not absolute: {env_path}")

sys.path.insert(0, str(source_dir))

if os.environ.get("THRENODY_PROVIDER_SCAN_TEST_MODE") == "1" or os.environ.get("SWITCHYARD_PROVIDER_SCAN_TEST_MODE") == "1":
    providers = []
else:
    from shared.discovery import installer_provider_inventory
    providers = installer_provider_inventory(verify_readiness=True)
json_path.write_text(
    json.dumps(
        {
            "providers": providers,
            "detected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        indent=2,
    ),
    encoding="utf-8",
)

by_name = {provider.get("name", ""): provider for provider in providers if provider.get("name")}

def available(name: str) -> int:
    entry = by_name.get(name, {})
    return int(bool(entry.get("available")))

host_count = sum(
    1 for provider in providers
    if provider.get("host_shell") and provider.get("available")
)

env_lines = [
    f"HAS_GH={available('github-copilot')}",
    f"HAS_CLAUDE={available('claude-code')}",
    f"HAS_CODEX={available('codex')}",
    f"HAS_CURSOR={available('cursor')}",
    f"HAS_JUNIE={available('junie')}",
    f"HAS_OPENCODE={available('opencode')}",
    f"HAS_AIDER={available('aider')}",
    f"HAS_AMAZON_Q={available('amazon-q')}",
    f"HAS_MISTRAL={available('mistral-vibe')}",
    f"HAS_BLACKBOX={available('blackbox-ai')}",
    f"HAS_WINDSURF={available('windsurf')}",
    f"HOST_COUNT={host_count}",
]
env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
PY
# shellcheck source=/dev/null
source "$PROVIDER_SCAN_ENV"
rm -f "$PROVIDER_SCAN_ENV"
PROVIDER_SCAN_ENV=""

python3 - "$PROVIDER_SCAN_JSON" <<'PY'
import json
import sys
from pathlib import Path

INSTALL_URLS = {
    "github-copilot": "https://cli.github.com",
    "claude-code": "https://docs.anthropic.com/en/docs/claude-code",
    "codex": "https://developers.openai.com/codex/",
    "cursor": "https://www.cursor.com",
    "junie": "https://junie.jetbrains.com",
    "opencode": "https://opencode.ai",
    "aider": "https://aider.chat",
    "amazon-q": "https://aws.amazon.com/q/developer/",
    "mistral-vibe": "https://docs.mistral.ai/",
    "blackbox-ai": "https://www.blackbox.ai",
    "windsurf": "https://windsurf.com",
}

_p148 = Path(sys.argv[1]).resolve()
if not _p148.is_absolute():
    raise SystemExit(f"invalid path: {_p148}")
with open(_p148, encoding="utf-8") as handle:
    payload = json.load(handle)

for provider in payload.get("providers", []):
    name = provider.get("name", "")
    display_name = provider.get("display_name", "")
    available = bool(provider.get("available", False))
    routeable = bool(provider.get("routeable", False))
    reason = provider.get("detect_reason", "")
    detected_binary = provider.get("detected_binary") or provider.get("binary", "")
    install_url = INSTALL_URLS.get(name)

    if available and routeable:
        print(f"  ✅ {display_name} found")
        continue

    if available and reason == "execution_not_supported":
        print(f"  ⚠️  {display_name} found (detect-only)")
        continue

    if available:
        print(f"  ⚠️  {display_name} found but not routeable ({reason})")
        if detected_binary != provider.get("binary", ""):
            print(f"       Detected binary: {detected_binary}")
        continue

    print(f"  ⚠️  {display_name} not found")
    if install_url:
        print(f"       Install: {install_url}")
PY

if [[ "$HOST_COUNT" -eq 0 ]]; then
    if [[ "$THRENODY_ALLOW_NO_HOST" == "1" ]]; then
        warn "No host CLI detected; continuing because THRENODY_ALLOW_NO_HOST=1"
    else
        error "At least one host CLI ('gh', 'claude', 'codex', 'cursor-agent', 'junie', or 'opencode') must be installed"
    fi
fi

# ── Install pyyaml dependency ────────────────────────────────────────────────

if [[ "$THRENODY_SKIP_DEPENDENCIES" != "1" ]] && ! python3 -c "import yaml" 2>/dev/null; then
    echo ""
    echo "  Installing pyyaml..."
    python3 -m pip install --quiet --user pyyaml || { warn "pip install pyyaml failed — install it manually"; true; }
fi
info "pyyaml available"

# ── Install wizard UI dependencies ─────────────────────────────────────────
if [[ "$THRENODY_SKIP_DEPENDENCIES" != "1" ]] && ! python3 -c "import rich, questionary" 2>/dev/null; then
    echo ""
    echo "  Installing UI dependencies (rich, questionary)..."
    python3 -m pip install --quiet --user rich questionary || { warn "pip install failed -- wizard falls back to plain prompts"; true; }
fi

# ── Copy files ───────────────────────────────────────────────────────────────

echo ""
echo "📁 Installing files..."

mkdir -p "$INSTALL_DIR"

# Backup existing DB before overwriting installation files
if [[ -f "$INSTALL_DIR/cache.db" ]]; then
    python3 - "$INSTALL_DIR" <<'PYEOF' 2>/dev/null || true
import sys
from pathlib import Path
base = Path(sys.argv[1])
sys.path.insert(0, str(base))
try:
    _home = Path.home().resolve()
    base = Path(sys.argv[1]).resolve()
    if not str(base).startswith(str(_home)):
        raise SystemExit(f"base path outside home: {base}")
    from shared.db import Database
    db = Database(base / "cache.db")
    bp = db.backup_db()
    db.close()
    if bp:
        print(f"  pre-install DB backup: {bp}")
except Exception as e:
    print(f"  pre-install DB backup skipped: {e}")
PYEOF
fi

copy_source_tree() {
    if [[ "$THRENODY_FORCE_PORTABLE_COPY" != "1" ]] && command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude='__pycache__/' \
            --exclude='.pytest_cache/' \
            --exclude='*.pyc' \
            --exclude='cache.db*' \
            --exclude='config.yaml' \
            --exclude='backup/' \
            --exclude='.git/' \
            --exclude='.DS_Store' \
            "$SOURCE_DIR/" "$INSTALL_DIR/"
        return
    fi

    warn "rsync not found; using portable Python copy fallback"
    python3 - "$SOURCE_DIR" "$INSTALL_DIR" <<'PY'
import shutil
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).resolve()
preserved_names = {"config.yaml", "cache.db", "cache.db-wal", "cache.db-shm", "backup"}
ignored_dirs = {".git", "__pycache__", ".pytest_cache"}

target.mkdir(parents=True, exist_ok=True)
for child in list(target.iterdir()):
    if child.name in preserved_names or child.name.startswith("cache.db.bak"):
        continue
    if child.is_dir() and not child.is_symlink():
        shutil.rmtree(child)
    else:
        child.unlink()

for source_path in source.rglob("*"):
    relative = source_path.relative_to(source)
    if any(part in ignored_dirs for part in relative.parts):
        continue
    if source_path.name == ".DS_Store" or source_path.suffix == ".pyc":
        continue
    if relative.as_posix() == "config.yaml" or source_path.name.startswith("cache.db"):
        continue
    if relative.parts and relative.parts[0] == "backup":
        continue
    target_path = target / relative
    if source_path.is_symlink():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.symlink_to(source_path.readlink())
    elif source_path.is_dir():
        target_path.mkdir(parents=True, exist_ok=True)
    else:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
PY
}

copy_source_tree

info "Files installed to $INSTALL_DIR"

if [[ "$THRENODY_TEST_FAIL_AFTER_COPY" == "1" ]]; then
    error "Injected test failure after source copy"
fi

# ── Provider discovery ──────────────────────────────────────────────────────

echo ""
echo "📋 Provider discovery"

cp "$PROVIDER_SCAN_JSON" "$INSTALL_DIR/providers.json"
info "Provider discovery written to $INSTALL_DIR/providers.json"
rm -f "$PROVIDER_SCAN_JSON"
PROVIDER_SCAN_JSON=""

# ── Verify install ───────────────────────────────────────────────────────────

python3 -m py_compile "$INSTALL_DIR/mcp_server.py" 2>/dev/null || error "Syntax check failed"
python3 -m py_compile "$INSTALL_DIR/shared/router.py" 2>/dev/null || error "Syntax check failed"
info "Syntax check passed"

# ── Register MCP server ─────────────────────────────────────────────────────

echo ""
echo "🔌 MCP server registration"

if [[ "$HAS_CLAUDE" -eq 1 ]]; then
    # Claude Code MCP registration via ~/.claude.json
    CLAUDE_CONFIG="$HOME/.claude.json"

    if [[ -f "$CLAUDE_CONFIG" ]]; then
        if grep -q "Threnody" "$CLAUDE_CONFIG" 2>/dev/null; then
            info "Claude Code MCP already registered"
        else
            # Add to existing config using python for safe JSON manipulation
            # shellcheck disable=SC2015
            python3 -c "
import json, sys
with open('$CLAUDE_CONFIG') as f:
    cfg = json.load(f)
mcps = cfg.setdefault('mcpServers', {})
mcps['Threnody'] = {
    'command': 'python3',
    'args': ['$INSTALL_DIR/mcp_server.py'],
    'type': 'stdio'
}
with open('$CLAUDE_CONFIG', 'w') as f:
    json.dump(cfg, f, indent=2)
" && info "Registered MCP server in Claude Code" || warn "Could not update $CLAUDE_CONFIG — register manually"
        fi
    else
        # shellcheck disable=SC2015
        python3 -c "
import json
cfg = {'mcpServers': {'Threnody': {
    'command': 'python3',
    'args': ['$INSTALL_DIR/mcp_server.py'],
    'type': 'stdio'
}}}
with open('$CLAUDE_CONFIG', 'w') as f:
    json.dump(cfg, f, indent=2)
" && info "Created $CLAUDE_CONFIG with MCP registration"
    fi
fi

# OpenCode MCP registration is currently interactive and project-scoped.
if [[ "$HAS_OPENCODE" -eq 1 ]]; then
    info "OpenCode detected — register Threnody manually inside each project with:"
    echo "       cd /path/to/project && opencode mcp add"
    echo "       Then choose 'Current project' and point it at: python3 $INSTALL_DIR/mcp_server.py"
fi

# Copilot CLI MCP registration via ~/.copilot/mcp-config.json
COPILOT_MCP_CONFIG="$HOME/.copilot/mcp-config.json"
mkdir -p "$HOME/.copilot"

if [[ -f "$COPILOT_MCP_CONFIG" ]]; then
    if grep -q "Threnody" "$COPILOT_MCP_CONFIG" 2>/dev/null; then
        info "Copilot CLI MCP already registered"
    else
        # shellcheck disable=SC2015
        python3 -c "
import json
with open('$COPILOT_MCP_CONFIG') as f:
    cfg = json.load(f)
mcps = cfg.setdefault('mcpServers', {})
mcps['Threnody'] = {
    'type': 'stdio',
    'command': 'python3',
    'args': ['$INSTALL_DIR/mcp_server.py'],
    'env': {}
}
with open('$COPILOT_MCP_CONFIG', 'w') as f:
    json.dump(cfg, f, indent=2)
" && info "Registered MCP server in Copilot CLI" || warn "Could not update $COPILOT_MCP_CONFIG — register manually"
    fi
else
    python3 -c "
import json
cfg = {'mcpServers': {'Threnody': {
    'type': 'stdio',
    'command': 'python3',
    'args': ['$INSTALL_DIR/mcp_server.py'],
    'env': {}
}}}
with open('$COPILOT_MCP_CONFIG', 'w') as f:
    json.dump(cfg, f, indent=2)
" && info "Created $COPILOT_MCP_CONFIG with MCP registration"
fi

# Codex CLI MCP registration via ~/.codex/config.toml
if [[ "$HAS_CODEX" -eq 1 ]]; then
    CODEX_CONFIG="$HOME/.codex/config.toml"
    mkdir -p "$HOME/.codex"

    if [[ -f "$CODEX_CONFIG" ]] && grep -q "Threnody" "$CODEX_CONFIG" 2>/dev/null; then
        info "Codex CLI MCP already registered"
    else
        # shellcheck disable=SC2015
        python3 - "$CODEX_CONFIG" "$INSTALL_DIR/mcp_server.py" <<'PY' \
            && info "Registered MCP server in Codex CLI" \
            || warn "Could not update $CODEX_CONFIG — register manually"
from pathlib import Path
import sys

_home = Path.home().resolve()
config_path = Path(sys.argv[1]).resolve()
if not str(config_path).startswith(str(_home)):
    raise SystemExit(f"config_path outside home: {config_path}")
mcp_server_path = sys.argv[2]
start_marker = "# Threnody:codex-mcp:start"
end_marker = "# Threnody:codex-mcp:end"
managed = (
    f"{start_marker}\n"
    "[mcp_servers.Threnody]\n"
    'command = "python3"\n'
    f'args = ["{mcp_server_path}"]\n'
    f"{end_marker}\n"
)

config_path.parent.mkdir(parents=True, exist_ok=True)
existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
start_index = existing.find(start_marker)
end_index = existing.find(end_marker, start_index + len(start_marker)) if start_index != -1 else -1

if start_index != -1 and end_index != -1:
    before = existing[:start_index].rstrip("\n")
    after = existing[end_index + len(end_marker):].lstrip("\n")
    new_content = before
    if new_content:
        new_content += "\n\n"
    new_content += managed.rstrip("\n")
    if after:
        new_content += "\n\n" + after
elif start_index != -1:
    before = existing[:start_index].rstrip("\n")
    new_content = before
    if new_content:
        new_content += "\n\n"
    new_content += managed.rstrip("\n")
else:
    stripped = existing.rstrip("\n")
    managed_stripped = managed.rstrip("\n")
    new_content = managed_stripped if not stripped else f"{stripped}\n\n{managed_stripped}"

config_path.write_text(new_content + "\n", encoding="utf-8")
PY
    fi
fi

# Cursor MCP registration via ~/.cursor/mcp.json
if [[ "$HAS_CURSOR" -eq 1 ]]; then
    CURSOR_MCP_CONFIG="$HOME/.cursor/mcp.json"
    mkdir -p "$HOME/.cursor"

    if [[ -f "$CURSOR_MCP_CONFIG" ]]; then
        if grep -q "Threnody" "$CURSOR_MCP_CONFIG" 2>/dev/null; then
            info "Cursor MCP already registered"
        else
            # shellcheck disable=SC2015
            python3 -c "
import json
with open('$CURSOR_MCP_CONFIG') as f:
    cfg = json.load(f)
mcps = cfg.setdefault('mcpServers', {})
mcps['Threnody'] = {
    'command': 'python3',
    'args': ['$INSTALL_DIR/mcp_server.py'],
    'env': {}
}
with open('$CURSOR_MCP_CONFIG', 'w') as f:
    json.dump(cfg, f, indent=2)
" && info "Registered MCP server in Cursor" || warn "Could not update $CURSOR_MCP_CONFIG — register manually"
        fi
    else
        python3 -c "
import json
cfg = {'mcpServers': {'Threnody': {
    'command': 'python3',
    'args': ['$INSTALL_DIR/mcp_server.py'],
    'env': {}
}}}
with open('$CURSOR_MCP_CONFIG', 'w') as f:
    json.dump(cfg, f, indent=2)
" && info "Created $CURSOR_MCP_CONFIG with MCP registration"
    fi
fi

# Junie MCP registration via ~/.junie/mcp/mcp.json
if [[ "$HAS_JUNIE" -eq 1 ]]; then
    JUNIE_MCP_CONFIG="$HOME/.junie/mcp/mcp.json"
    mkdir -p "$HOME/.junie/mcp"

    if [[ -f "$JUNIE_MCP_CONFIG" ]]; then
        if grep -q "Threnody" "$JUNIE_MCP_CONFIG" 2>/dev/null; then
            info "Junie MCP already registered"
        else
            # shellcheck disable=SC2015
            python3 -c "
import json
with open('$JUNIE_MCP_CONFIG') as f:
    cfg = json.load(f)
mcps = cfg.setdefault('mcpServers', {})
mcps['Threnody'] = {
    'command': 'python3',
    'args': ['$INSTALL_DIR/mcp_server.py']
}
with open('$JUNIE_MCP_CONFIG', 'w') as f:
    json.dump(cfg, f, indent=2)
" && info "Registered MCP server in Junie" || warn "Could not update $JUNIE_MCP_CONFIG — register manually"
        fi
    else
        python3 -c "
import json
cfg = {'mcpServers': {'Threnody': {
    'command': 'python3',
    'args': ['$INSTALL_DIR/mcp_server.py']
}}}
with open('$JUNIE_MCP_CONFIG', 'w') as f:
    json.dump(cfg, f, indent=2)
" && info "Created $JUNIE_MCP_CONFIG with MCP registration"
    fi
fi

# ── Shell integration ────────────────────────────────────────────────────────

echo ""
echo "🐚 Shell integration"

SHELL_SOURCE="source $INSTALL_DIR/shell/ghc.sh"
SHELL_RC=""

if [[ -n "${ZSH_VERSION:-}" ]] || [[ "$SHELL" == */zsh ]]; then
    SHELL_RC="$HOME/.zshrc"
elif [[ -n "${BASH_VERSION:-}" ]] || [[ "$SHELL" == */bash ]]; then
    SHELL_RC="$HOME/.bashrc"
fi

if [[ -n "$SHELL_RC" ]]; then
    if grep -qF "Threnody" "$SHELL_RC" 2>/dev/null; then
        info "Shell integration already in $SHELL_RC"
    else
        {
            echo ""
            echo "# Threnody — AI orchestration"
            echo "$SHELL_SOURCE"
        } >> "$SHELL_RC"
        info "Added to $SHELL_RC — restart your shell or run: source $SHELL_RC"
    fi
else
    warn "Could not detect shell RC file. Add this line manually:"
    echo "       $SHELL_SOURCE"
fi

# Symlink shell entry points to ~/.local/bin for CLI access
mkdir -p "$HOME/.local/bin"
for entry_point in threnody-watch threnody switchyard-watch switchyard ghc; do
    if [[ -f "$INSTALL_DIR/shell/$entry_point" ]]; then
        chmod +x "$INSTALL_DIR/shell/$entry_point"
        ln -sf "$INSTALL_DIR/shell/$entry_point" "$HOME/.local/bin/$entry_point"
        info "Symlinked $entry_point → ~/.local/bin/$entry_point"
    fi
done

# ── First-run configuration wizard ──────────────────────────────────────────

if [[ ! -f "$INSTALL_DIR/config.yaml" && "$THRENODY_SKIP_WIZARD" != "1" ]]; then
    echo ""
    echo "  First-time setup -- configure providers and routing"
    echo "   (Ctrl+C to skip -- run 'threnody settings' anytime later)"
    echo ""
    python3 "$INSTALL_DIR/shared/settings_wizard.py" "$INSTALL_DIR/config.yaml" || true
fi

# ── AI custom instructions ───────────────────────────────────────────────────

echo ""
echo "📝 Custom instructions (shell-specific coordination policy)"

SYNCED_CLAUDE_INSTRUCTIONS=0
SYNCED_CLAUDE_HOOKS=0
SYNCED_COPILOT_INSTRUCTIONS=0
SYNCED_CODEX_INSTRUCTIONS=0
SYNCED_CURSOR_INSTRUCTIONS=0
SYNCED_JUNIE_INSTRUCTIONS=0
INSTRUCTION_RENDER_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t threnody-instructions)"

render_instruction_artifacts() {
    (cd "$INSTALL_DIR" && python3 - "$INSTRUCTION_RENDER_DIR" "$INSTALL_DIR/config.yaml" <<'PY'
import sys
from pathlib import Path

from shared.config import TGsConfig
from shared.instructions import render_shell_instructions

out_dir = Path(sys.argv[1]).resolve()
config_path = Path(sys.argv[2]).resolve()
if not out_dir.is_absolute() or not config_path.is_absolute():
    raise SystemExit(f"paths not absolute: {out_dir}, {config_path}")
out_dir.mkdir(parents=True, exist_ok=True)
try:
    config = TGsConfig.from_yaml(config_path)
except Exception as exc:
    print(
        f"warning: invalid config at {config_path}; rendering default routing policy: {exc}",
        file=sys.stderr,
    )
    config = TGsConfig()
shells = {
    "claude-code": ("claude-code", False),
    "github-copilot-cli": ("github-copilot-cli", False),
    "codex": ("codex", False),
    "cursor": ("cursor", True),
    "junie": ("junie", False),
}
for file_stem, (shell_id, verbatim) in shells.items():
    body = render_shell_instructions(config, shell_id, verbatim=verbatim)
    (out_dir / f"{file_stem}.md").write_text(body, encoding="utf-8")

claude_profile = config.routing_policy.effective_profile("claude-code")
(out_dir / "claude-code.hook").write_text(
    "enabled\n" if claude_profile.direct_edit_hooks else "disabled\n",
    encoding="utf-8",
)
PY
    )
}

if ! render_instruction_artifacts; then
    warn "Could not render shell-specific routing instructions"
fi

render_instruction_block() {
    local shell_id="$1"
    local path="$INSTRUCTION_RENDER_DIR/$shell_id.md"
    [[ -f "$path" ]] && cat "$path"
}

routing_hooks_enabled() {
    local shell_id="$1"
    local path="$INSTRUCTION_RENDER_DIR/$shell_id.hook"
    if [[ ! -f "$path" ]]; then
        return 2
    fi
    [[ "$(cat "$path")" == "enabled" ]]
}

sync_instruction_block() {
    local target="$1"
    local block_id="$2"
    local body="$3"

    SYNC_BODY="$body" python3 - "$target" "$block_id" <<'PY'
import os
import sys
from pathlib import Path

target, block_id = sys.argv[1], sys.argv[2]
body = os.environ.get("SYNC_BODY", "").strip("\n")
if not body:
    sys.exit(0)

path = Path(target)
existing = path.read_text(encoding="utf-8") if path.exists() else ""

start = f"<!-- Threnody:{block_id}:start -->"
end = f"<!-- Threnody:{block_id}:end -->"
legacy_heading = "# Global Instructions — Threnody Integration"
start_index = existing.find(start)
end_index = existing.find(end, start_index + len(start)) if start_index != -1 else -1

managed = f"{start}\n{body}\n{end}\n"

if start_index != -1 and end_index != -1:
    before = existing[:start_index]
    after = existing[end_index + len(end):]
    before = before.rstrip("\n")
    after = after.lstrip("\n")
    new_content = before
    if new_content:
        new_content += "\n\n"
    new_content += managed.rstrip("\n")
    if after:
        new_content += "\n\n" + after
elif start_index != -1:
    before = existing[:start_index]
    before = before.rstrip("\n")
    new_content = before
    if new_content:
        new_content += "\n\n"
    new_content += managed.rstrip("\n")
elif legacy_heading in existing and existing.count(legacy_heading) == 1:
    legacy_index = existing.index(legacy_heading)
    next_top_level = existing.find("\n# ", legacy_index + len(legacy_heading))
    before = existing[:legacy_index]
    after = existing[next_top_level + 1:] if next_top_level != -1 else ""
    before = before.rstrip("\n")
    after = after.lstrip("\n")
    new_content = before
    if new_content:
        new_content += "\n\n"
    new_content += managed.rstrip("\n")
    if after:
        new_content += "\n\n" + after
elif end in existing:
    cleaned = existing.replace(end, "").rstrip("\n")
    new_content = cleaned
    if new_content:
        new_content += "\n\n"
    new_content += managed.rstrip("\n")
else:
    existing = existing.rstrip("\n")
    managed_stripped = managed.rstrip("\n")
    new_content = managed_stripped if not existing else f"{existing}\n\n{managed_stripped}"

path.write_text(new_content + "\n", encoding="utf-8")
PY
}


install_threnody_tier_agents() {
    local target_dir="$1"
    if [[ ! -d "$INSTALL_DIR/shell/agents" ]]; then
        return 0
    fi
    mkdir -p "$target_dir"
    for tier_file in "$INSTALL_DIR/shell/agents"/threnody-*.md; do
        [[ -f "$tier_file" ]] || continue
        cp "$tier_file" "$target_dir/$(basename "$tier_file")"
    done
    info "Installed Threnody tier agent templates to $target_dir"
}

write_managed_file() {
    local target="$1"
    local body="$2"

    FILE_BODY="$body" python3 - "$target" <<'PY'
import os
import sys
from pathlib import Path

target = sys.argv[1]
body = os.environ.get("FILE_BODY", "").rstrip("\n")
if not body:
    sys.exit(0)

path = Path(target)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(body + "\n", encoding="utf-8")
PY
}

# --- Claude Code instructions ---
if [[ "$HAS_CLAUDE" -eq 1 ]]; then
    CLAUDE_MD="$HOME/.claude/CLAUDE.md"
    CLAUDE_SETTINGS_JSON="$HOME/.claude/settings.json"
    mkdir -p "$(dirname "$CLAUDE_MD")"

    CLAUDE_INSTRUCTIONS=$(render_instruction_block "claude-code" 2>/dev/null)

    if [[ -n "$CLAUDE_INSTRUCTIONS" ]]; then
        sync_instruction_block "$CLAUDE_MD" "claude" "$CLAUDE_INSTRUCTIONS"
        info "Synced managed routing instructions to $CLAUDE_MD"
        SYNCED_CLAUDE_INSTRUCTIONS=1
        install_threnody_tier_agents "$HOME/.claude/agents"
    fi

    if routing_hooks_enabled "claude-code"; then
        HOOK_ACTION="install"
    else
        HOOK_STATUS=$?
        if [[ "$HOOK_STATUS" -eq 1 ]]; then
            HOOK_ACTION="remove"
        else
            warn "Could not resolve Claude routing hook policy; keeping hook enforcement enabled"
            HOOK_ACTION="install"
        fi
    fi

    HOOK_SCRIPT="$INSTALL_DIR/shell/threnody-routing-hook.sh"
    if [[ -f "$HOOK_SCRIPT" ]]; then
        chmod +x "$HOOK_SCRIPT"
    fi

    if python3 - "$CLAUDE_SETTINGS_JSON" "$HOOK_ACTION" "$HOOK_SCRIPT" <<'PY'
from pathlib import Path
import json
import sys

_home = Path.home().resolve()
path = Path(sys.argv[1]).resolve()
if not str(path).startswith(str(_home)):
    raise SystemExit(f"path outside home: {path}")
action = sys.argv[2]
hook_script = sys.argv[3]
path.parent.mkdir(parents=True, exist_ok=True)

if path.exists():
    try:
        raw = path.read_text(encoding="utf-8").strip()
        cfg = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
else:
    cfg = {}

if not isinstance(cfg, dict):
    raise SystemExit(f"Expected JSON object in {path}")

hooks = cfg.get("hooks")
if not isinstance(hooks, dict):
    hooks = {}
    cfg["hooks"] = hooks

pre_tool_use = hooks.get("PreToolUse")
if not isinstance(pre_tool_use, list):
    pre_tool_use = []

def _is_managed_routing_hook(hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    if (
        hook.get("type") == "mcp_tool"
        and hook.get("server") in {"Threnody", "TGs-router"}
        and hook.get("tool") == "validate_routing_guard"
    ):
        return True
    if hook.get("type") == "command":
        command = str(hook.get("command") or "")
        if "threnody-routing-hook" in command:
            return True
    return False


managed_entry = {
    "matcher": "Edit|Write",
    "hooks": [
        {
            "type": "command",
            "command": hook_script,
        }
    ],
}

filtered = []
for group in pre_tool_use:
    if not isinstance(group, dict):
        filtered.append(group)
        continue
    group_hooks = group.get("hooks")
    managed = False
    if isinstance(group_hooks, list):
        for hook in group_hooks:
            if _is_managed_routing_hook(hook):
                managed = True
                break
    if not managed:
        filtered.append(group)

if action == "install":
    filtered.append(managed_entry)
hooks["PreToolUse"] = filtered

if not hooks.get("PreToolUse"):
    hooks.pop("PreToolUse", None)
if not hooks:
    cfg.pop("hooks", None)

path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
PY
    then
        if [[ "$HOOK_ACTION" == "install" ]]; then
            info "Installed Claude PreToolUse routing hook in $CLAUDE_SETTINGS_JSON"
            SYNCED_CLAUDE_HOOKS=1
        else
            info "Removed managed Claude PreToolUse routing hook from $CLAUDE_SETTINGS_JSON"
        fi
    else
        warn "Could not update $CLAUDE_SETTINGS_JSON routing hook"
    fi
fi

# --- GitHub Copilot instructions ---
COPILOT_INSTRUCTIONS_MD="$HOME/.copilot/copilot-instructions.md"
LEGACY_COPILOT_INSTRUCTIONS_MD="$HOME/.github/copilot-instructions.md"
mkdir -p "$(dirname "$COPILOT_INSTRUCTIONS_MD")" "$HOME/.github"

COPILOT_INSTRUCTIONS=$(render_instruction_block "github-copilot-cli" 2>/dev/null)

if [[ -n "$COPILOT_INSTRUCTIONS" ]]; then
    sync_instruction_block "$COPILOT_INSTRUCTIONS_MD" "copilot" "$COPILOT_INSTRUCTIONS"
    info "Synced managed routing instructions to $COPILOT_INSTRUCTIONS_MD"
    SYNCED_COPILOT_INSTRUCTIONS=1
    if [[ -e "$LEGACY_COPILOT_INSTRUCTIONS_MD" ]]; then
        sync_instruction_block "$LEGACY_COPILOT_INSTRUCTIONS_MD" "copilot" "$COPILOT_INSTRUCTIONS"
        info "Synced legacy routing instructions to $LEGACY_COPILOT_INSTRUCTIONS_MD"
    fi
fi

# --- Codex instructions ---
if [[ "$HAS_CODEX" -eq 1 ]]; then
    CODEX_AGENTS_MD="$HOME/.codex/AGENTS.md"
    mkdir -p "$(dirname "$CODEX_AGENTS_MD")"

    CODEX_INSTRUCTIONS=$(render_instruction_block "codex" 2>/dev/null)

    if [[ -n "$CODEX_INSTRUCTIONS" ]]; then
        sync_instruction_block "$CODEX_AGENTS_MD" "codex" "$CODEX_INSTRUCTIONS"
        info "Synced managed routing instructions to $CODEX_AGENTS_MD"
        SYNCED_CODEX_INSTRUCTIONS=1
    fi
fi

# --- Cursor instructions ---
if [[ "$HAS_CURSOR" -eq 1 ]]; then
    CURSOR_RULE_FILE="$HOME/.cursor/rules/threnody.mdc"
    CURSOR_INSTRUCTIONS=$(render_instruction_block "cursor" 2>/dev/null)

    if [[ -n "$CURSOR_INSTRUCTIONS" ]]; then
        write_managed_file "$CURSOR_RULE_FILE" "$CURSOR_INSTRUCTIONS"
        info "Synced managed routing instructions to $CURSOR_RULE_FILE"
        SYNCED_CURSOR_INSTRUCTIONS=1
        install_threnody_tier_agents "$HOME/.cursor/agents"
    fi
fi

# --- Junie instructions ---
if [[ "$HAS_JUNIE" -eq 1 ]]; then
    JUNIE_AGENTS_MD="$HOME/.junie/AGENTS.md"
    mkdir -p "$(dirname "$JUNIE_AGENTS_MD")"

    JUNIE_INSTRUCTIONS=$(render_instruction_block "junie" 2>/dev/null)

    if [[ -n "$JUNIE_INSTRUCTIONS" ]]; then
        sync_instruction_block "$JUNIE_AGENTS_MD" "junie" "$JUNIE_INSTRUCTIONS"
        info "Synced managed routing instructions to $JUNIE_AGENTS_MD"
        SYNCED_JUNIE_INSTRUCTIONS=1
    fi
fi

info "Full instructions reference: $INSTALL_DIR/INSTRUCTIONS.md"

# Temporary files and standalone clone are removed by the EXIT trap.

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🚀 Threnody installed successfully!"
echo ""
echo "  Managed instruction files:"
if [[ "$SYNCED_CLAUDE_INSTRUCTIONS" -eq 1 ]]; then
    echo "    ~/.claude/CLAUDE.md                  Claude Code"
fi
if [[ "$SYNCED_CLAUDE_HOOKS" -eq 1 ]]; then
    echo "    ~/.claude/settings.json             Claude PreToolUse routing hook"
fi
if [[ "$SYNCED_COPILOT_INSTRUCTIONS" -eq 1 ]]; then
    echo "    ~/.copilot/copilot-instructions.md  GitHub Copilot CLI"
fi
if [[ "$SYNCED_COPILOT_INSTRUCTIONS" -eq 1 && -e "$LEGACY_COPILOT_INSTRUCTIONS_MD" ]]; then
    echo "    ~/.github/copilot-instructions.md   Legacy Copilot copy"
fi
if [[ "$SYNCED_CODEX_INSTRUCTIONS" -eq 1 ]]; then
    echo "    ~/.codex/AGENTS.md                  OpenAI Codex"
fi
if [[ "$SYNCED_CURSOR_INSTRUCTIONS" -eq 1 ]]; then
    echo "    ~/.cursor/rules/threnody.mdc      Cursor"
fi
if [[ "$SYNCED_JUNIE_INSTRUCTIONS" -eq 1 ]]; then
    echo "    ~/.junie/AGENTS.md                  JetBrains Junie"
fi
echo ""
echo "  Available commands (after shell restart):"
echo "    ghc agent \"task\"     Orchestrated multi-agent ensemble"
echo "    ghcs \"question\"      Quick suggest (routed)"
echo "    ghce \"question\"      Quick explain (routed)"
echo "    ghcw                  Cache stats"
echo "    threnody inspect status --project .      Readiness + current limits"
echo "    threnody inspect approvals --project .   Pending approval queue"
echo "    threnody tune show --project .           Persisted operator controls"
echo ""
if [[ "$HOST_COUNT" -ge 2 ]]; then
    echo "  🔗 $HOST_COUNT host CLIs detected — coordination + optional delegation enabled"
elif [[ "$HOST_COUNT" -eq 1 ]]; then
    echo "  📎 Single host CLI detected — install OpenCode/Aider or enable local endpoints for optional utility delegation"
fi
if [[ "$HAS_GH" -eq 1 ]]; then
    echo "     Host-native execution uses your CLI auth; enable delegation_utilities for OpenCode/Aider/local only"
fi
if [[ "$HAS_OPENCODE" -eq 1 ]]; then
    echo "     OpenCode is available as a low-tier host/provider via opencode/nemotron-3-super-free"
fi
echo "  Provider terms: see $INSTALL_DIR/docs/LEGAL.md"
echo "  Provider policies may change at any time; use at your own risk."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
