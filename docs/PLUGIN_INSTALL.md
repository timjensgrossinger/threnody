# Threnody Plugin Installation Guide

This guide covers three ways to install Threnody: via Claude Code plugin marketplace (recommended), via PyPI/`uvx`, or via the official MCP registry.

---

## Prerequisites

- **Python 3.10+**
- **At least one host AI CLI installed:**
  - `claude` (Claude Code)
  - `gh` (GitHub Copilot CLI)
  - `codex` (Anthropic Codex)
  - `cursor-agent` (Cursor)
  - `junie` (Junie)
  - `opencode` (OpenCode)

The installer detects which CLI(s) you have; at least one must be present (or set `THRENODY_ALLOW_NO_HOST=1` to bypass this check).

---

## Installation Path 1: Claude Code Plugin Marketplace (Recommended)

This is the richest install experience: the plugin bundles the MCP server, the Threnody routing skills, and the coordination hook as a single installable unit.

### One-time: Register the marketplace

Inside a Claude Code session:

```bash
/plugin marketplace add timjensgrossinger/threnody
```

Or via CLI:

```bash
claude plugin marketplace add timjensgrossinger/threnody
```

### Install the plugin

```bash
/plugin install threnody@threnody
```

Or via CLI:

```bash
claude plugin install threnody@threnody
```

### Team setup (optional)

Drop this into your project's `.claude/settings.json` to prompt teammates to install:

```json
{
  "extraKnownMarketplaces": {
    "threnody": {
      "source": { "source": "github", "repo": "timjensgrossinger/threnody" }
    }
  },
  "enabledPlugins": { "threnody@threnody": true }
}
```

The plugin automatically registers the MCP server via `uvx threnody-mcp` and installs the Threnody skills.

---

## Installation Path 2: Official MCP Registry (PyPI + `uvx`)

Register Threnody with any MCP-aware client (Claude Desktop, Cursor, Claude Code, etc.) via the official registry.

### Minimal install

```bash
claude mcp add threnody -- uvx threnody-mcp
```

### With environment overrides (optional)

Set custom data directory or allow startup without a host CLI:

```bash
claude mcp add -e THRENODY_INSTALL_DIR="$HOME/.threnody" threnody -- uvx threnody-mcp
```

**Environment variables:**
- `THRENODY_INSTALL_DIR`: Override the data/config directory (defaults to `~/.local/lib/threnody`)
- `THRENODY_ALLOW_NO_HOST`: Set to `1` for `install.sh` to bypass its host-CLI check

This path works with any registry-aware client that supports the official MCP registry.

---

## Installation Path 3: PyPI Direct (`pip` / `uvx`)

For developers who want the MCP server without registry automation.

### Install via pip

```bash
pip install threnody-mcp
```

Or run directly via `uvx` (no installation needed):

```bash
uvx threnody-mcp
```

### Optional: Seed the data directory

After installing via PyPI, optionally run the installer to seed the on-disk data directory (`~/.local/lib/threnody`):

```bash
curl -fsSL https://threnody.dev/install.sh | bash -s -- --from-pip
```

This step:
- Validates Python 3.10+ is available
- Scans for installed host CLIs
- Writes `providers.json` (the fallback provider inventory)
- Installs bundled Threnody skills into provider-native roots for Claude Code, Cursor, Codex, Copilot CLI, and OpenCode
- Skips file copy, shell aliases, and wizard

Then register the MCP server manually in your host CLI config. For Claude Code:

```bash
claude mcp add threnody -- uvx threnody-mcp
```

---

## First-Run: What Happens

The first time you call a Threnody tool, the MCP server initializes with sensible defaults:

- **Config:** Reads `~/.local/lib/threnody/config.yaml` if present; uses defaults otherwise
- **Database:** Auto-creates `~/.local/lib/threnody/cache.db` for routing history and memory
- **Guidance:** Prints an `instructions` field telling you:
  - Routing is in **advisory** mode by default (recommended, not mandatory)
  - All detected host CLIs are enabled
  - To customize, run `threnody settings` (or call `route_task` and follow guidance)
  - If no host CLI was detected, install one before using `execute_subtask` or agent spawning
- **First task:** Call `start_task` with `mode="implement"` for the guided
  install -> project profile -> host-native next-action flow. Use
  `mode="review"` or `mode="investigate"` for read-only workflows.

No configuration is required to get started — defaults are safe.

### Check setup status

Call the `check_providers` tool (or `route_task`) to see the current state:

```json
{
  "config_present": false,
  "config_mode": "defaults",
  "routing_policy": "advisory",
  "host_clis_detected": ["claude-code"],
  "setup_hint": "Running on defaults. Optional: `threnody settings` for routing/provider prefs."
}
```

---

## Customization: `threnody settings`

To configure routing behavior, provider preferences, or parallelism:

### Install the CLI (optional)

If you used the plugin or PyPI path and skipped `install.sh`, you can still use the `threnody` command by installing the full CLI:

```bash
curl -fsSL https://threnody.dev/install.sh | bash -s -- --plugin-mode
```

This seeds the on-disk installation, creates shell aliases (`ghc`, `ghcs`, `ghce`), and makes `threnody settings` available.

### Run the interactive wizard

```bash
threnody settings
```

This opens an interactive TUI where you can:
- Adjust routing tier bounds (low/medium/high)
- Set provider preferences (preferred providers for each tier)
- Configure parallelism (concurrent waves, speculation workers)
- Enable or disable optional features (speculation, learning)

Settings are saved to `~/.local/lib/threnody/config.yaml`.

### Guarded routing (advanced)

By default, routing is **advisory** (recommended, not mandatory). To enforce routing gates in Claude Code:

```bash
curl -fsSL https://threnody.dev/install.sh | bash -s -- --plugin-mode --sync-instructions
```

This writes managed routing policy blocks into your host shell instruction files, enabling the PreToolUse coordination hook when you set `routing_policy.mode: guarded` in `config.yaml`.

---

## Fallback: Full `install.sh` (Power-User Path)

If you want the complete developer experience (shell aliases, symlinks, custom instruction sync), use the standalone installer:

```bash
git clone https://github.com/timjensgrossinger/threnody.git
cd threnody
./install.sh
```

Or via curl:

```bash
curl -fsSL https://raw.githubusercontent.com/timjensgrossinger/threnody/main/install.sh | bash
```

This installs:
- Python source to `~/.local/lib/threnody/`
- Shell aliases: `ghc`, `ghcs`, `ghce`, `threnody`, `threnody-watch` to `~/.local/bin/`
- MCP server registration in Claude Code, Copilot CLI, Cursor, and other connected hosts
- Custom instruction sync (routing policy blocks in shell instruction files)
- Bundled Threnody skills in `~/.agents/skills`, `~/.codex/skills`, `~/.claude/skills`, `~/.cursor/skills`, `~/.copilot/agents`, and `~/.config/opencode/agent`
- Interactive first-run configuration wizard

After installation, restart your shell to pick up the aliases, then connect from your host CLI.

---

## Troubleshooting

### "No host CLI detected"

Install at least one of: `claude` (Claude Code), `gh` (GitHub Copilot), `codex`, `cursor-agent`, `junie`, or `opencode`.

To skip this check: set `THRENODY_ALLOW_NO_HOST=1` (or pass `--allow-no-host` to `install.sh`).

### Config not found, using defaults

This is normal. Either:
- Run `threnody settings` to create a config interactively
- Create a config manually in `~/.local/lib/threnody/config.yaml` (template available in repo as `config.example.yaml`)
- Leave it as-is; defaults are safe

### "command not found: threnody"

The `threnody` CLI command is only available if you ran the full `install.sh` (which creates symlinks). If you installed via plugin/PyPI and want the CLI:

```bash
curl -fsSL https://threnody.dev/install.sh | bash -s -- --plugin-mode
```

Or add `~/.local/lib/threnody/shell` to your `PATH`.

### Plugin marketplace not found

Ensure the marketplace is registered:

```bash
claude plugin marketplace add timjensgrossinger/threnody
```

Then install:

```bash
claude plugin install threnody@threnody
```

---

## Docs & Support

- [Architecture](../docs/ARCHITECTURE.md) — system design and execution models
- [Legal & compliance](../docs/LEGAL.md) — provider terms, auth, and operator responsibilities
- [Limitations](../docs/RELEASE_LIMITATIONS.md) — known constraints and workarounds
- [Main README](../README.md) — feature overview and quick reference
