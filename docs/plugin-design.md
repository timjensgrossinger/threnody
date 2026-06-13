# Threnody Plugin Distribution Design

**Date:** 2026-06-12
**Status:** Design proposal
**Inputs:** `docs/marketplace-research.md`, `docs/install-audit.md`
**Goal:** Add an alternative "Claude plugin" install path alongside the existing
`git clone + ./install.sh` flow, without breaking the existing flow.

---

## 0. Context recap

Threnody is a **local-first Python MCP server** (`mcp_server.py`, stdio
transport) that coordinates routing/planning/swarm; the **host shell executes**.
It is targeted primarily at Claude Code but also runs under Copilot CLI, Codex,
Cursor, Junie, and OpenCode.

Two facts from the install audit shape everything below:

1. **Runtime is lazy and default-safe.** `_ensure_init()` (mcp_server.py:548)
   loads `TGsConfig.from_yaml()` which returns `TGsConfig.defaults()` when
   `config.yaml` is missing, auto-creates `cache.db`, and discovers providers
   lazily. **The server runs with zero config files present.**
2. **install.sh does the heavy lifting** — provider scan, dependency install,
   MCP registration in host CLI config files, shell aliases (`ghc`/`threnody`),
   symlinks, the first-run wizard, and custom-instruction sync.

The plugin path must reproduce only what is **mandatory** (the audit's §6) and
deliberately skip the rest (aliases, symlinks, ghc).

---

## 1. Distribution target

**Decision: Both — PyPI/`uvx` as the primary runtime artifact, self-hosted
`install.sh` retained as the power-user / no-PyPI fallback.**

### Rationale

| Option | Verdict | Why |
|---|---|---|
| Self-hosted only (status quo) | Keep as fallback | Already works; needed for air-gapped/dev installs and for the rich shell-alias UX. But `curl \| bash` is a high-friction, low-trust entry point and is invisible to every registry. |
| PyPI + `uvx` (standard) | **Adopt as primary** | This is the de-facto standard for Python MCP servers (`mcp-server-git`, `mcp-server-fetch` all use it). `uvx threnody-mcp` gives zero-install, isolated-venv execution and is the **only** runtime form that the official MCP registry, Smithery, and the Claude Code plugin marketplace can all reference. |
| Both | **Chosen** | PyPI/`uvx` unlocks every registry channel with one artifact; `install.sh` stays for users who want shell aliases, custom-instruction sync, or who cannot reach PyPI. |

### What "both" requires us to build

- A **`pyproject.toml`** that packages `mcp_server.py` + `shared/` + provider
  dirs into a wheel named `threnody-mcp`, exposing a console entry point:
  ```toml
  [project.scripts]
  threnody-mcp = "threnody.mcp_server:main"
  ```
  (Today there is no `pyproject.toml` — the audit notes "plain Python 3.10+".
  This is the one genuinely new build artifact the plugin path needs.)
- Package layout: move/alias the top-level modules under a `threnody/` import
  package, or use a `[tool.setuptools] py-modules`/`packages` mapping so
  `threnody.mcp_server:main` resolves. `main()` already exists
  (mcp_server.py:10552) and reads JSON-RPC from stdin — no code change needed
  to the entry behavior.
- Runtime deps come straight from `requirements.txt`: `pyyaml>=6.0,<7`
  (mandatory), `rich`/`questionary` (optional extras — wizard only).
- The PyPI long-description (README) **must** contain the registry ownership
  line: `mcp-name: io.github.timjensgrossinger/threnody`.

> Note on namespace: research uses `io.github.tgrossinger/*`, but the repo and
> CI badges are under `github.com/timjensgrossinger/threnody`. The registry
> reverse-DNS name must match the GitHub org/user that authenticates via
> `mcp-publisher login github`. **This design uses
> `io.github.timjensgrossinger/threnody`** — adjust if the publishing account
> differs.

---

## 2. Marketplace strategy

**Decision: target three channels, in this priority order.**

### Priority 1 — Claude Code plugin marketplace (richest UX, primary audience)

Threnody's primary audience is Claude Code users, and a plugin can bundle the
MCP server config **plus** the six repo-local skills (`skills/threnody-*`) and
the routing hook as a single installable unit. This is strictly better UX than
a bare MCP server for our target user. We self-host a marketplace repo (or ship
`.claude-plugin/` in the main repo) — third parties cannot self-submit to
`claude-plugins-official`, and `claude-plugins-community` is a slower,
review-gated path we pursue opportunistically later.

### Priority 2 — Official MCP registry (broadest reach, all MCP clients)

Once `threnody-mcp` is on PyPI, publishing `server.json` via `mcp-publisher`
makes Threnody discoverable to **every** registry-aware MCP client (Cursor,
Claude Desktop, Glama's 34k-server mirror, etc.), not just Claude Code. Low
incremental effort after PyPI exists.

### Priority 3 — Smithery + passive directories (mcp.so, Glama)

`smithery.yaml` gives a CLI-driven install for Claude Desktop users.
mcp.so/Glama are self-service listing forms that link back to our install
command — near-zero effort, discovery-only value. Do these last.

### Ordering justification

1. Plugin marketplace first — it is the only channel that delivers skills +
   hook + MCP as one unit to our core audience and requires **no external
   approval**.
2. MCP registry second — it depends on PyPI being live, and once it is, the
   marginal cost is one `server.json` + one publish command.
3. Smithery/directories third — lowest value-per-effort, targets a different
   app (Claude Desktop), and can be added anytime.

---

## 3. Install UX for each path

### 3.1 Official MCP registry path (PyPI + `uvx`)

What the user types (after we have published to PyPI + registry):

```bash
# Minimal
claude mcp add threnody -- uvx threnody-mcp

# With a non-default install dir or routing override (optional)
claude mcp add -e THRENODY_INSTALL_DIR="$HOME/.threnody" threnody -- uvx threnody-mcp
```

Registry-aware clients (Cursor, Claude Desktop) auto-discover the listing and
present a one-click "Install" that writes the same `mcpServers` stanza.

There is **no** shell-alias / `ghc` / symlink step on this path. First run is
handled by the detection logic in §5.

### 3.2 Claude Code plugin path (recommended)

What the user types inside a Claude Code session:

```text
# One-time: register our marketplace
/plugin marketplace add timjensgrossinger/threnody

# Install the plugin (pulls MCP config + skills + hook)
/plugin install threnody@threnody
```

Or from the CLI:

```bash
claude plugin install threnody@threnody
```

Team-repo automation (optional) — drop into a project `.claude/settings.json`
to prompt teammates automatically:

```json
{
  "extraKnownMarketplaces": {
    "threnody": { "source": { "source": "github", "repo": "timjensgrossinger/threnody" } }
  },
  "enabledPlugins": { "threnody@threnody": true }
}
```

The plugin's `mcpServers` block runs `uvx threnody-mcp`, so PyPI is the runtime
substrate here too. First run is handled by §5.

### 3.3 Fallback: existing `install.sh` (unchanged)

```bash
curl -fsSL https://raw.githubusercontent.com/timjensgrossinger/threnody/main/install.sh | bash
# or
git clone https://github.com/timjensgrossinger/threnody.git && cd threnody && ./install.sh
```

This remains the **only** path that installs shell aliases (`ghc`, `ghcs`,
`ghce`), CLI symlinks, the interactive wizard, and custom-instruction sync.
It is unchanged by this design except for the new `--plugin-mode` branch (§6),
which is additive.

### Channel comparison

| Channel | User action | Skills/hook bundled | Aliases/CLI | Runtime |
|---|---|---|---|---|
| Claude Code plugin | `/plugin install threnody@threnody` | Yes | No | `uvx threnody-mcp` |
| MCP registry / PyPI | `claude mcp add threnody -- uvx threnody-mcp` | No | No | `uvx threnody-mcp` |
| `install.sh` (fallback) | `curl … \| bash` | Synced to host | Yes | `python3 …/mcp_server.py` |

---

## 4. Plugin manifest files

All field values below are concrete for Threnody. Source of truth is
`threnody.manifest.json` (File 2); the values here mirror it.

### 4.1 `server.json` — official MCP registry

Placed in repo root; published with `mcp-publisher publish`.

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.timjensgrossinger/threnody",
  "title": "Threnody",
  "description": "Local-first MCP meta-harness: routes, plans, and swarms multi-agent coding work while the host shell executes.",
  "version": "0.2.0-alpha.1",
  "websiteUrl": "https://github.com/timjensgrossinger/threnody",
  "repository": {
    "url": "https://github.com/timjensgrossinger/threnody",
    "source": "github"
  },
  "packages": [
    {
      "registryType": "pypi",
      "registryBaseUrl": "https://pypi.org",
      "identifier": "threnody-mcp",
      "version": "0.2.0a1",
      "transport": { "type": "stdio" },
      "runtimeHint": "uvx",
      "environmentVariables": [
        {
          "name": "THRENODY_INSTALL_DIR",
          "description": "Override the data/config directory (defaults to ~/.local/lib/threnody).",
          "isRequired": false
        },
        {
          "name": "THRENODY_ALLOW_NO_HOST",
          "description": "Allow startup with no host AI CLI detected.",
          "isRequired": false,
          "default": "0"
        }
      ]
    }
  ],
  "_meta": {
    "io.modelcontextprotocol.registry/publisher-provided": {
      "tags": ["mcp", "routing", "multi-agent", "orchestration", "swarm"],
      "license": "Apache-2.0"
    }
  }
}
```

Notes:
- PyPI version `0.2.0a1` is the PEP 440 normalization of `0.2.0-alpha.1`. The
  `server.json` top-level `version` uses the semver display form; the
  `packages[].version` must match the **published PyPI** string.
- No `remotes[]` — Threnody is stdio-local only.
- The PyPI README must carry `mcp-name: io.github.timjensgrossinger/threnody`
  for ownership verification.

### 4.2 `.claude-plugin/plugin.json` — the plugin itself

```json
{
  "name": "threnody",
  "description": "Local-first MCP meta-harness — host executes, Threnody coordinates swarms, memory, and learning.",
  "version": "0.2.0-alpha.1",
  "author": { "name": "Tim Grossinger", "email": "tim.grossinger@movec.com" },
  "homepage": "https://github.com/timjensgrossinger/threnody",
  "license": "Apache-2.0",
  "mcpServers": {
    "threnody": {
      "command": "uvx",
      "args": ["threnody-mcp"]
    }
  },
  "skills": "./skills"
}
```

Notes:
- `mcpServers.threnody` mirrors the registry/PyPI runtime (`uvx threnody-mcp`)
  so all channels share one runtime substrate.
- `skills: "./skills"` bundles the six repo-local `threnody-*` skills so the
  plugin install delivers them too — the headline advantage over a bare MCP
  registration.
- The routing PreToolUse hook is **not** bundled by default: it only matters
  under `routing_policy.mode: guarded`, and the default posture is advisory.
  If we later ship a guarded variant, add a `hooks` key pointing at a hook
  script — kept out of the default plugin to honor the advisory default.

### 4.3 `.claude-plugin/marketplace.json` — the marketplace

Shipped in the **same repo root** so `/plugin marketplace add
timjensgrossinger/threnody` resolves it; the plugin source is the repo itself.

```json
{
  "name": "threnody",
  "owner": { "name": "Tim Grossinger", "email": "tim.grossinger@movec.com" },
  "metadata": {
    "description": "Threnody — MCP meta-harness for multi-agent coding.",
    "version": "1.0.0"
  },
  "plugins": [
    {
      "name": "threnody",
      "source": ".",
      "description": "Local-first MCP meta-harness — host executes, Threnody coordinates swarms, memory, and learning.",
      "version": "0.2.0-alpha.1",
      "homepage": "https://github.com/timjensgrossinger/threnody",
      "repository": "https://github.com/timjensgrossinger/threnody",
      "license": "Apache-2.0",
      "category": "productivity",
      "tags": ["mcp", "routing", "multi-agent", "orchestration", "swarm"],
      "mcpServers": {
        "threnody": { "command": "uvx", "args": ["threnody-mcp"] }
      }
    }
  ]
}
```

Notes:
- `"source": "."` means the plugin lives at the marketplace repo root (where
  `.claude-plugin/plugin.json` is). If we later split into a dedicated
  marketplace repo, switch to
  `{"source": "github", "repo": "timjensgrossinger/threnody"}`.
- `name: "threnody"` for the marketplace is safe — it is **not** on the
  reserved list (`claude-plugins-official`, `claude-community`, etc.).

### 4.4 `smithery.yaml` — Smithery

```yaml
startCommand:
  type: stdio
  configSchema:
    type: object
    required: []
    properties:
      installDir:
        type: string
        description: "Override data/config directory (THRENODY_INSTALL_DIR)."
      allowNoHost:
        type: boolean
        default: false
        description: "Allow startup with no host AI CLI detected."
  commandFunction: |
    (config) => ({
      command: 'uvx',
      args: ['threnody-mcp'],
      env: {
        ...(config.installDir ? { THRENODY_INSTALL_DIR: config.installDir } : {}),
        THRENODY_ALLOW_NO_HOST: config.allowNoHost ? '1' : '0'
      }
    })
runtime: python
```

---

## 5. First-run UX via the plugin path

### Problem

On the plugin/`uvx` path, **install.sh never runs**. So there is no shell-alias
setup, no wizard, no custom-instruction sync, and `config.yaml` is absent. The
server still works (defaults), but the user gets none of the guidance that
`install.sh` normally prints, and may not realize routing is in **advisory**
mode with **no provider preferences** set.

### Design: detect-and-guide, non-blocking

Two complementary surfaces — neither blocks tool execution (the lazy,
default-safe runtime is a feature we preserve):

**(a) `initialize`-time setup hint (passive, always shown once).**
The `initialize` handler (mcp_server.py:10453) currently returns only
`serverInfo`. Add an `instructions` string to the response when first-run is
detected. MCP clients surface server `instructions` to the model/user, so this
is the natural channel.

Detection predicate (cheap, no DB open) — reuse `_config_file_signature()`
(mcp_server.py:684) plus a marker file:

```text
first_run := (not CONFIG_YAML.exists()) AND (not <install_dir>/.threnody-initialized exists)
```

When `first_run` is true, return:

```json
{
  "protocolVersion": "2024-11-05",
  "capabilities": { "tools": { "listChanged": false } },
  "serverInfo": { "name": "Threnody", "version": "<v>" },
  "instructions": "Threnody is running with default settings (advisory routing, no provider preferences, all detected host CLIs enabled). This is fine for most users. To customize routing/providers, run `threnody settings` after installing the CLI, or call the `route_task` tool and follow its guidance. No host AI CLI was detected — install one (claude, gh, codex, cursor-agent, junie, opencode) for execution handoff."
}
```

The "no host CLI detected" clause is conditional on a fast availability probe
(reuse `shared.discovery` provider inventory; cache-backed, best-effort).

**(b) `setup_status` tool / `check_providers` enrichment (active, on demand).**
Add (or extend the existing `check_providers`) a lightweight response field
that the model can surface when the user asks "is Threnody set up?":

```json
{
  "config_present": false,
  "config_mode": "defaults",
  "routing_policy": "advisory",
  "host_clis_detected": ["claude-code"],
  "setup_hint": "Running on defaults. Optional: `threnody settings` for routing/provider prefs. Plugin installs skip shell aliases (ghc/ghcs) by design.",
  "install_path": "plugin"
}
```

**(c) Marker file write.** After the **first successful tool call** completes,
`_ensure_init()` (or its caller) writes `<install_dir>/.threnody-initialized`
(empty, mtime = first-run timestamp). This suppresses the `instructions` hint
on subsequent sessions without requiring the user to create `config.yaml`.
Writing must be best-effort (`log.debug(..., exc_info=True)` on failure) so a
read-only install dir never breaks startup.

### Why not auto-run the wizard?

The wizard (`settings_wizard.py`) is interactive (rich/questionary) and assumes
a TTY. The MCP server runs as a stdio subprocess with **no TTY** — launching it
would hang. Guidance-via-`instructions` is the correct non-interactive
equivalent; the user runs `threnody settings` later if they want it.

---

## 6. What `install.sh --plugin-mode` must do

**Decision: add a `--plugin-mode` flag (and `THRENODY_PLUGIN_MODE=1` env
equivalent) that runs the mandatory subset and skips host-shell convenience.**

This flag is invoked by the PyPI package's post-install path **only when the
user explicitly wants the on-disk install dir seeded** (e.g. to use
`threnody settings` later). The `uvx` runtime itself does **not** require
install.sh — it runs from the wheel. `--plugin-mode` is the bridge for users
who installed via plugin/PyPI but then want the local config surface.

### MUST do (mandatory subset, per audit §6)

| Step | install.sh section | Why mandatory |
|---|---|---|
| Python 3.10+ check | pre-flight (75–97) | Hard requirement |
| Provider availability scan | 102–222 | Validates ≥1 host CLI; writes `providers.json` (fallback inventory) |
| `pyyaml` install | 232–239 | Config parsing; `uvx` already vendors it via the wheel deps, so this is a no-op when run inside the venv |
| File copy to `$INSTALL_DIR` | 248–357 | Seeds the on-disk data dir (config/db live here even for `uvx` runtime, via `THRENODY_INSTALL_DIR`) |
| `providers.json` write | 343–351 | Provider inventory snapshot |
| Seed default `config.yaml` | new | Write a minimal `config.yaml` from `config.example.yaml` so first-run hint is suppressed cleanly (optional — defaults work without it) |

Provider-scan gating still applies: fail unless ≥1 host CLI OR
`THRENODY_ALLOW_NO_HOST=1`.

### MUST skip

| Step | install.sh section | Why skip |
|---|---|---|
| Shell integration (`source ghc.sh` in `.zshrc`/`.bashrc`) | 582–620 | Plugin users do not use `ghc`/`ghcs`/`ghce`; modifying RC files is intrusive |
| Symlinks to `~/.local/bin` (`threnody`, `ghc`, `threnody-watch`, …) | 582–620 | CLI convenience only; not needed for MCP |
| First-run wizard | 622–630 | No TTY in plugin context; guidance moves to §5 |

### CONDITIONAL (flag-controlled sub-options)

| Step | Default in `--plugin-mode` | Override |
|---|---|---|
| MCP server registration in host CLI configs (359–506) | **Skip** — the plugin's `mcpServers` block already registers it; double-registration would duplicate the server | `--plugin-mode --register-mcp` to force, for PyPI-only (non-plugin) users |
| Custom-instruction sync (632–1017) | **Skip** — plugin ships skills; advisory default needs no CLAUDE.md edits | `--plugin-mode --sync-instructions` for users who want guarded routing instructions written |

### Sketch of the flag plumbing (additive, install.sh top)

```bash
PLUGIN_MODE="${THRENODY_PLUGIN_MODE:-0}"
REGISTER_MCP_OVERRIDE=""      # empty = default-by-mode
SYNC_INSTRUCTIONS_OVERRIDE=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --plugin-mode)        PLUGIN_MODE=1 ;;
    --register-mcp)       REGISTER_MCP_OVERRIDE=1 ;;
    --sync-instructions)  SYNC_INSTRUCTIONS_OVERRIDE=1 ;;
    *) error "Unknown argument: $1" ;;
  esac
  shift
done

# In plugin mode, default these OFF unless explicitly overridden:
if [ "$PLUGIN_MODE" = "1" ]; then
  : "${THRENODY_SKIP_WIZARD:=1}"
  DO_SHELL_INTEGRATION=0
  DO_SYMLINKS=0
  DO_MCP_REGISTRATION="${REGISTER_MCP_OVERRIDE:-0}"
  DO_INSTRUCTION_SYNC="${SYNC_INSTRUCTIONS_OVERRIDE:-0}"
fi
```

Each existing block is then wrapped in an `if [ "$DO_… " = "1" ]` guard
(currently they always run). This is the only intrusive change to install.sh,
and it is gated entirely behind `--plugin-mode`, so the default `curl | bash`
flow is byte-for-byte unchanged.

---

## 7. Build pipeline (one source of truth → three manifests)

`threnody.manifest.json` (File 2) is the canonical input. A small build script
(`scripts/build-manifests.py`, future work) reads it and emits:

- `server.json` (registry) — maps `entry_command`/`runtime_hint` →
  `packages[].runtimeHint`, `config_schema.env` → `environmentVariables[]`.
- `.claude-plugin/plugin.json` + `marketplace.json` — maps `entry_command` →
  `mcpServers.threnody`, `tags`/`license`/`author` straight through.
- `smithery.yaml` — maps `config_schema` → `configSchema`, `entry_command` →
  `commandFunction`.

This keeps name/version/description/tags consistent across channels and means a
version bump touches one file.

---

## 8. Decision summary

1. **Distribution target:** Both — PyPI/`uvx` primary (`threnody-mcp`),
   `install.sh` retained as fallback. Requires a new `pyproject.toml`.
2. **Marketplace order:** (1) Claude Code plugin marketplace, (2) official MCP
   registry, (3) Smithery + mcp.so/Glama.
3. **Install UX:** plugin → `/plugin install threnody@threnody`; registry →
   `claude mcp add threnody -- uvx threnody-mcp`; fallback → `curl | bash`.
4. **Manifests:** `server.json`, `.claude-plugin/plugin.json` +
   `marketplace.json`, `smithery.yaml` — all driven from
   `threnody.manifest.json`.
5. **First-run UX:** non-blocking `initialize` `instructions` hint +
   `setup_status` field + `.threnody-initialized` marker; never auto-run the
   TTY wizard.
6. **`install.sh --plugin-mode`:** run Python check + provider scan + file copy
   + `providers.json`; skip aliases, symlinks, wizard; MCP-registration and
   instruction-sync off by default with `--register-mcp` / `--sync-instructions`
   overrides.
