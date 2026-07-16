#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${THRENODY_INSTALL_DIR:-${SWITCHYARD_INSTALL_DIR:-$HOME/.local/lib/threnody}}"
DATA_BACKUP_DIR="${THRENODY_DATA_BACKUP_DIR:-${SWITCHYARD_DATA_BACKUP_DIR:-$HOME/.local/share/threnody}}"
PURGE_DATA=0

info() { echo "  ✅ $*"; }
warn() { echo "  ⚠️  $*" >&2; }

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh [--purge-data]

Removes Threnody code, registrations, symlinks, hooks, and managed
instruction blocks. By default, config.yaml and cache.db* are preserved under
~/.local/share/threnody/. Use --purge-data to remove them instead.
EOF
}

case "${1:-}" in
    "")
        ;;
    --purge-data)
        PURGE_DATA=1
        ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

echo ""
echo "🧹 Threnody uninstaller"
echo "   Install: $INSTALL_DIR"
echo ""

python3 - "$HOME" "$INSTALL_DIR" <<'PY'
import json
import sys
from pathlib import Path

home = Path(sys.argv[1]).resolve()
install_dir = Path(sys.argv[2]).resolve()


def within_home(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved != home and home not in resolved.parents:
        raise SystemExit(f"Refusing to edit path outside HOME: {resolved}")
    return resolved


def remove_json_mcp(path_value: str) -> None:
    path = within_home(Path(path_value))
    if not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8").strip()
        data = json.loads(raw) if raw else {}
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return
    changed = False
    if "Threnody" in servers:
        servers.pop("Threnody", None)
        changed = True
    if "TGs-router" in servers:
        servers.pop("TGs-router", None)
        changed = True
    if not servers:
        data.pop("mcpServers", None)
    if not changed:
        return
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def remove_managed_block(path_value: str, block_id: str) -> None:
    path = within_home(Path(path_value))
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    start = f"<!-- Threnody:{block_id}:start -->"
    end = f"<!-- Threnody:{block_id}:end -->"
    start_index = text.find(start)
    if start_index == -1:
        return
    end_index = text.find(end, start_index + len(start))
    if end_index == -1:
        return
    before = text[:start_index].rstrip()
    after = text[end_index + len(end):].lstrip()
    updated = before
    if before and after:
        updated += "\n\n"
    updated += after
    path.write_text(updated.rstrip() + ("\n" if updated.strip() else ""), encoding="utf-8")


def remove_all_managed_blocks(path_value: str, label: str, block_id: str) -> None:
    remove_managed_block(path_value, block_id)
    path = within_home(Path(path_value))
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    start = f"<!-- {label}:{block_id}:start -->"
    end = f"<!-- {label}:{block_id}:end -->"
    while True:
        start_index = text.find(start)
        if start_index == -1:
            break
        end_index = text.find(end, start_index + len(start))
        if end_index == -1:
            break
        before = text[:start_index].rstrip()
        after = text[end_index + len(end):].lstrip()
        text = before
        if before and after:
            text += "\n\n"
        text += after
    path.write_text(text.rstrip() + ("\n" if text.strip() else ""), encoding="utf-8")


def remove_codex_mcp(path_value: str) -> None:
    path = within_home(Path(path_value))
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    for start, end in (
        ("# Threnody:codex-mcp:start", "# Threnody:codex-mcp:end"),
        ("# TGs-router:codex-mcp:start", "# TGs-router:codex-mcp:end"),
    ):
        start_index = text.find(start)
        if start_index != -1:
            end_index = text.find(end, start_index + len(start))
            if end_index != -1:
                before = text[:start_index].rstrip()
                after = text[end_index + len(end):].lstrip()
                text = before
                if before and after:
                    text += "\n\n"
                text += after
    lines = text.splitlines()
    filtered: list[str] = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if stripped in {'[mcp_servers."TGs-router"]', "[mcp_servers.TGs-router]", '[projects."/Users/tim/.local/lib/TGs-router"]'}:
            skip = True
            continue
        if skip and stripped.startswith("[") and stripped.endswith("]"):
            skip = False
        if not skip:
            filtered.append(line)
    updated = "\n".join(filtered).rstrip()
    path.write_text(updated + ("\n" if updated else ""), encoding="utf-8")


def remove_cursor_legacy_rule(path_value: str) -> None:
    path = within_home(Path(path_value))
    if path.exists():
        path.unlink()


def remove_claude_hook(path_value: str) -> None:
    path = within_home(Path(path_value))
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    groups = hooks.get("PreToolUse")
    if not isinstance(groups, list):
        return
    filtered = []
    for group in groups:
        group_hooks = group.get("hooks") if isinstance(group, dict) else None
        is_managed = False
        for hook in (group_hooks if isinstance(group_hooks, list) else []):
            if not isinstance(hook, dict):
                continue
            if (
                hook.get("type") == "mcp_tool"
                and hook.get("server") in {"Threnody", "TGs-router"}
                and hook.get("tool") == "validate_routing_guard"
            ):
                is_managed = True
            command = str(hook.get("command") or "")
            if hook.get("type") == "command" and "threnody-routing-hook" in command:
                is_managed = True
            if is_managed:
                break
        if not is_managed:
            filtered.append(group)
    if filtered:
        hooks["PreToolUse"] = filtered
    else:
        hooks.pop("PreToolUse", None)
    if not hooks:
        data.pop("hooks", None)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def remove_shell_lines(path_value: str) -> None:
    path = within_home(Path(path_value))
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    source = f"source {install_dir}/shell/ghc.sh"
    filtered = []
    for line in lines:
        if (
            line == source
            or "TGs-router/shell/ghc.sh" in line
            or line in {"# Threnody — AI orchestration", "# TGs-router — AI orchestration"}
        ):
            continue
        filtered.append(line)
    path.write_text("\n".join(filtered).rstrip() + ("\n" if filtered else ""), encoding="utf-8")


remove_json_mcp(str(home / ".claude.json"))
remove_json_mcp(str(home / ".copilot/mcp-config.json"))
remove_json_mcp(str(home / ".gemini/settings.json"))
remove_json_mcp(str(home / ".cursor/mcp.json"))
remove_json_mcp(str(home / ".junie/mcp/mcp.json"))
remove_codex_mcp(str(home / ".codex/config.toml"))
remove_claude_hook(str(home / ".claude/settings.json"))

for path, block_id in (
    (home / ".claude/CLAUDE.md", "claude"),
    (home / ".copilot/copilot-instructions.md", "copilot"),
    (home / ".github/copilot-instructions.md", "copilot"),
    (home / ".gemini/GEMINI.md", "gemini"),
    (home / ".codex/AGENTS.md", "codex"),
    (home / ".junie/AGENTS.md", "junie"),
):
    remove_all_managed_blocks(str(path), "Threnody", block_id)
    remove_all_managed_blocks(str(path), "TGs-router", block_id)

cursor_rule = within_home(home / ".cursor/rules/threnody.mdc")
if cursor_rule.exists():
    cursor_rule.unlink()
remove_cursor_legacy_rule(str(home / ".cursor/rules/tgs-router.mdc"))

remove_shell_lines(str(home / ".zshrc"))
remove_shell_lines(str(home / ".bashrc"))
PY

for entry_point in threnody-watch threnody ghc; do
    link="$HOME/.local/bin/$entry_point"
    if [[ -L "$link" ]]; then
        target="$(readlink "$link")"
        if [[ "$target" == "$INSTALL_DIR"/shell/* ]]; then
            rm -f -- "$link"
            info "Removed ~/.local/bin/$entry_point"
        fi
    fi
done

if [[ -d "$INSTALL_DIR" ]]; then
    if [[ "$PURGE_DATA" -eq 0 ]]; then
        mkdir -p "$DATA_BACKUP_DIR"
        for item in config.yaml cache.db cache.db-wal cache.db-shm backup; do
            if [[ -e "$INSTALL_DIR/$item" ]]; then
                rm -rf -- "${DATA_BACKUP_DIR:?}/$item"
                mv -- "$INSTALL_DIR/$item" "$DATA_BACKUP_DIR/$item"
            fi
        done
        while IFS= read -r backup_file; do
            mv -- "$backup_file" "$DATA_BACKUP_DIR/$(basename "$backup_file")"
        done < <(find "$INSTALL_DIR" -maxdepth 1 -name 'cache.db.bak*' -type f -print)
        info "Preserved runtime data in $DATA_BACKUP_DIR"
    fi
    rm -rf -- "$INSTALL_DIR"
    info "Removed $INSTALL_DIR"
fi

warn "Project-local opencode.json registrations must be removed from each project manually."
echo ""
info "Threnody uninstall complete"
