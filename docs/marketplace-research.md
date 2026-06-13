# MCP Marketplace & Distribution Research

Research date: 2026-06-12. Focus: distribution options for a Python MCP server
(Threnody) so users can install it without cloning a git repo and running
`install.sh`.

---

## 1. MCP Registries and Marketplaces

### 1.1 Official MCP Registry (modelcontextprotocol.io)

- **URL**: https://registry.modelcontextprotocol.io
- **GitHub**: https://github.com/modelcontextprotocol/registry
- **Status**: Public, launched preview 2025-09-08; API freeze at v0.1 (no
  breaking changes) since 2025-10-24.
- **Scale**: The registry API lets MCP clients enumerate servers like an app
  store. Glama.ai mirrors 34,000+ servers indexed from across the ecosystem.
- **Submission mechanism**: via the `mcp-publisher` CLI (see §3 below);
  servers are identified by reverse-DNS name (e.g. `io.github.tgrossinger/threnody`).
- **Official schema URL**: `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`

### 1.2 Smithery (smithery.ai)

- **URL**: https://smithery.ai
- **Scale**: ~2,000+ listings.
- **CLI**: `npm install -g smithery@latest` (requires Node 20+)
- **Distribution model**: CLI-first. Users install from Smithery by name:
  ```bash
  npx @smithery/cli install <server-name>
  # or
  smithery mcp add <server-name> --client claude
  smithery mcp add <server-name> --client claude --config '{"apiKey":"..."}'
  ```
- **Publishing**:
  ```bash
  smithery mcp publish <url-or-bundle.mcpb> -n org/server-name
  ```
- **Manifest**: `smithery.yaml` in the repo root (see §2.2).
- **Source**: https://smithery.ai/docs/concepts/cli

### 1.3 mcp.so

- **URL**: https://mcp.so
- **Scale**: 20,000+ servers indexed as of April 2026; largest public directory.
- **Submission**: Self-registration form on the site (no PR required).
- **Primary audience**: Claude Desktop and Cursor users.

### 1.4 Glama (glama.ai/mcp)

- **URL**: https://glama.ai/mcp/servers
- **Scale**: 34,000+ servers (mirrors official registry plus community).
- **Submission**: Submission form; manually reviewed; prefers production-quality
  servers with clear README and working install example.
- **Source**: https://glama.ai/blog/2026-01-24-official-mcp-registry-serverjson-requirements

### 1.5 Anthropic Plugin Marketplace (claude-plugins-official)

This is a **Claude Code plugin** marketplace, NOT an MCP server registry. It
is distinct from the MCP registry — plugins bundle MCP servers alongside
skills, hooks, and agents as a single installable unit.

- **URL**: https://claude.com/plugins
- **Automatically available** to all Claude Code users (no add command needed).
- **Curation**: Anthropic-curated; inclusion is at Anthropic's discretion.
  Third parties cannot self-submit to the official marketplace.
- **Install command** (from inside Claude Code):
  ```
  /plugin install <name>@claude-plugins-official
  ```
- **Community marketplace** (`claude-plugins-community`):
  - GitHub: `anthropics/claude-plugins-community`
  - User adds it manually: `/plugin marketplace add anthropics/claude-plugins-community`
  - Then installs: `/plugin install <name>@claude-community`
  - Accepts submissions via the in-app form (passes automated validation/safety screening).

### 1.6 Community-Curated Lists

- **punkpeye/awesome-mcp-servers** (GitHub): curated README; submit via PR.
  Supports automated agent PRs with `🤖🤖🤖` title tag.

---

## 2. Manifest / Schema Formats

### 2.1 Official MCP Registry: `server.json`

File placed in repo root and submitted via `mcp-publisher publish`.

**Root fields:**

| Field         | Type   | Required | Description                                              |
|---------------|--------|----------|----------------------------------------------------------|
| `$schema`     | string | Yes      | `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json` |
| `name`        | string | Yes      | Reverse-DNS unique ID: `io.github.tgrossinger/threnody` |
| `title`       | string | No       | Human-readable display name                              |
| `description` | string | Yes      | Short description of what the server does                |
| `version`     | string | Yes      | Semantic version: `1.0.0`                               |
| `websiteUrl`  | string | No       | Docs or setup guide URL                                  |
| `repository`  | object | No       | `{url, source, id?, subfolder?}`                         |
| `packages`    | array  | No       | Local/installable package configs (PyPI, npm, OCI, etc.) |
| `remotes`     | array  | No       | Cloud-hosted HTTP/SSE endpoints                          |
| `_meta`       | object | No       | Custom metadata under key `io.modelcontextprotocol.registry/publisher-provided` (max 4KB) |

**`packages[]` item fields:**

| Field                  | Type   | Required    | Description                                           |
|------------------------|--------|-------------|-------------------------------------------------------|
| `registryType`         | string | Yes         | `pypi`, `npm`, `cargo`, `nuget`, `oci`, `mcpb`       |
| `registryBaseUrl`      | string | Conditional | Required for pypi/npm/cargo/nuget; e.g. `https://pypi.org` |
| `identifier`           | string | Yes         | Package name on the registry (e.g. `threnody-mcp`)   |
| `version`              | string | Yes         | Package version                                       |
| `transport`            | object | Yes         | `{type: "stdio"|"streamable-http"|"sse", url?}`      |
| `runtimeHint`          | string | No          | Execution method: `uvx`, `npx`, `dnx`                |
| `packageArguments`     | array  | No          | CLI args (`positional` or `named` type)               |
| `runtimeArguments`     | array  | No          | Container/runtime args                                |
| `environmentVariables` | array  | No          | `{name, description, isRequired?, isSecret?, default?, choices?}` |

**`remotes[]` item fields:**

| Field      | Type   | Required | Description                               |
|------------|--------|----------|-------------------------------------------|
| `type`     | string | Yes      | `streamable-http` or `sse`                |
| `url`      | string | Yes      | Endpoint URL; supports `{variable}` templates |
| `variables`| object | No       | Variable definitions for URL templates    |
| `headers`  | array  | No       | HTTP header specs                         |

**Submission workflow:**

```bash
# Install publisher CLI
brew install mcp-publisher

# Initialize server.json from your server dir
mcp-publisher init

# Authenticate (GitHub namespace: io.github.*)
mcp-publisher login github

# Publish
mcp-publisher publish

# Verify
curl "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.tgrossinger/threnody"
```

**Namespace ownership proofs:**
- `io.github.*` namespace: GitHub OAuth via `mcp-publisher login github`
- `com.yourcompany.*` namespace: Ed25519 keypair + DNS TXT record

**Source**: https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/server-json/generic-server-json.md

### 2.2 Smithery: `smithery.yaml`

File placed in repo root. Controls how Smithery launches the server and what
configuration it accepts from users.

```yaml
startCommand:
  type: stdio                     # always "stdio" for local servers
  configSchema:                   # JSON Schema for user-provided config
    type: object
    required: []
    properties:
      apiKey:
        type: string
        description: API key for the service
        isSecret: true
      port:
        type: number
        default: 8080
        description: Port to bind to
  commandFunction: |              # JS function returning the launch command
    (config) => ({
      command: 'uvx',
      args: ['threnody-mcp'],
      env: {
        API_KEY: config.apiKey || '',
        PORT: String(config.port)
      }
    })
runtime: python                   # optional; declares language runtime
```

**Key `commandFunction` patterns for Python:**
- Via `uvx` (recommended — zero install):
  `command: 'uvx', args: ['threnody-mcp']`
- Via `python` directly:
  `command: 'python', args: ['-m', 'threnody']`
- Via pip-installed entrypoint:
  `command: 'threnody-mcp'`

**Publishing to Smithery:**
```bash
npm install -g smithery@latest
smithery mcp publish https://your-hosted-url -n yourorg/threnody
# or bundle format:
smithery mcp publish threnody.mcpb -n yourorg/threnody
```

**Source**: https://smithery.ai/docs/concepts/cli

### 2.3 Claude Code Plugin Marketplace: `marketplace.json` + `plugin.json`

For distribution as a **Claude Code plugin** (a superset of MCP — it can
bundle the MCP server plus skills, hooks, agents):

**`.claude-plugin/marketplace.json`** (in your marketplace repo):
```json
{
  "name": "threnody-marketplace",
  "owner": { "name": "Tim Grossinger", "email": "tim.grossinger@movec.com" },
  "plugins": [
    {
      "name": "threnody",
      "source": "./plugins/threnody",
      "description": "Threnody MCP coordination server",
      "version": "1.0.0",
      "homepage": "https://github.com/your-org/threnody",
      "repository": "https://github.com/your-org/threnody",
      "license": "MIT",
      "category": "productivity",
      "tags": ["mcp", "routing", "multi-agent"],
      "mcpServers": {
        "threnody": {
          "command": "uvx",
          "args": ["threnody-mcp"]
        }
      }
    }
  ]
}
```

**`.claude-plugin/plugin.json`** (in the plugin itself):
```json
{
  "name": "threnody",
  "description": "Threnody MCP coordination server",
  "version": "1.0.0"
}
```

**Reserved marketplace names** (cannot be used by third parties):
`claude-code-marketplace`, `claude-code-plugins`, `claude-plugins-official`,
`claude-plugins-community`, `claude-community`, `anthropic-marketplace`,
`anthropic-plugins`, `agent-skills`, `anthropic-agent-skills`, etc.

**Source**: https://code.claude.com/docs/en/plugin-marketplaces

---

## 3. How `claude mcp add` Works

`claude mcp add` does NOT support installing by registry name. It only supports
direct command specification or URL. There is no `--from-registry` flag.

### Supported transports

**HTTP (hosted server):**
```bash
claude mcp add --transport http <name> <url>
claude mcp add --transport http --header "Authorization: Bearer $TOKEN" <name> <url>
```

**stdio (local subprocess):**
```bash
claude mcp add <name> -- <command> [args...]
claude mcp add -e API_KEY=xxx <name> -- uvx threnody-mcp
```

Note: `--transport sse` is deprecated as of early 2026 in favor of `--transport http`.

### Scopes

```bash
--scope local    # default: you only, current project
--scope user     # you only, all projects
--scope project  # everyone who clones the project (.mcp.json in repo root)
```

### Config file format (`.mcp.json` or `~/.claude.json`)

```json
{
  "mcpServers": {
    "threnody": {
      "type": "stdio",
      "command": "uvx",
      "args": ["threnody-mcp"],
      "env": { "THRENODY_API_KEY": "..." }
    }
  }
}
```

### Registry integration: none in `claude mcp add`

Claude Code has no built-in registry-lookup in `claude mcp add`. Registry
discovery happens through:
1. Smithery CLI: `smithery mcp add threnody --client claude` (writes to
   Claude Desktop config, not Claude Code CLI config — separate apps)
2. Plugin system: `/plugin install threnody@your-marketplace` inside a Claude
   Code session — this is the richer path (see §5 below).
3. Manual: users copy the `claude mcp add` command from your README.

### Source: https://code.claude.com/docs/en/cli-reference, https://code.claude.com/docs/en/mcp-quickstart

---

## 4. Python / pip / uvx Distribution

### uvx (recommended)

`uvx` (from the `uv` package manager) runs a PyPI package as a one-shot
subprocess, creating a temporary isolated venv. No explicit install step.

```bash
# User runs this once to register the server
claude mcp add threnody -- uvx threnody-mcp

# Or with env vars
claude mcp add -e THRENODY_KEY=xxx threnody -- uvx threnody-mcp
```

This is the **de facto standard** for Python MCP servers. All official
Anthropic Python MCP packages (e.g. `mcp-server-git`, `mcp-server-fetch`) use
this pattern.

### pip (alternative)

```bash
pip install threnody-mcp
claude mcp add threnody -- threnody-mcp        # if entrypoint defined in pyproject.toml
# or
claude mcp add threnody -- python -m threnody
```

### PyPI package requirements for the official MCP registry

The `server.json` entry for a PyPI-distributed server must include:
- `registryType: "pypi"`
- `registryBaseUrl: "https://pypi.org"`
- `identifier: "threnody-mcp"` (package name on PyPI)
- `version: "1.0.0"`
- `transport: {type: "stdio"}`
- `runtimeHint: "uvx"` (tells clients how to launch it)

The PyPI package README must contain the server's registry name for ownership
verification: `mcp-name: io.github.tgrossinger/threnody`

### Official Anthropic guidance

Anthropic recommends `uvx` in official docs and uses it for all reference
servers. The MCP Python SDK (`pip install mcp`, version 1.27.2+ as of 2026)
is the foundation; servers implementing it are automatically compatible with
the full client ecosystem.

**Source**: https://pypi.org/project/mcp/, https://github.com/modelcontextprotocol/servers

---

## 5. Claude Code Plugin Path (Recommended for Rich Integration)

The plugin system is the most capable distribution path for Claude Code
specifically, because a plugin can bundle the MCP server configuration with
skills (slash commands), hooks, and agents in a single install.

### User install flow (two steps, then done)

```
# Step 1: add your marketplace (one-time)
/plugin marketplace add your-org/threnody-marketplace

# Step 2: install the plugin
/plugin install threnody@threnody-marketplace
```

Or from CLI:
```bash
claude plugin install threnody@threnody-marketplace
```

### Automated distribution via `.claude/settings.json` (team repos)

Add to your project's `.claude/settings.json` to prompt team members
automatically:
```json
{
  "extraKnownMarketplaces": {
    "threnody": {
      "source": { "source": "github", "repo": "your-org/threnody-marketplace" }
    }
  },
  "enabledPlugins": {
    "threnody@threnody-marketplace": true
  }
}
```

### Plugin source options for marketplace.json

Plugins can be fetched from:
- Relative path (same repo): `"./plugins/threnody"`
- GitHub: `{"source": "github", "repo": "your-org/threnody"}`
- Git URL: `{"source": "url", "url": "https://gitlab.com/..."}`
- Git subdirectory: `{"source": "git-subdir", "url": "...", "path": "plugins/threnody"}`
- npm: `{"source": "npm", "package": "@your-org/threnody-plugin"}`

No PyPI/pip source type exists in the plugin system (that's only the MCP registry path).

---

## 6. Recommended Distribution Strategy for Threnody

Given Threnody is a Python MCP server targeting Claude Code users specifically:

### Option A: PyPI + official MCP registry (broadest reach)

1. Publish to PyPI as `threnody-mcp` with an `uvx`-runnable entrypoint.
2. Add `mcp-name: io.github.tgrossinger/threnody` to README.
3. Create `server.json` and publish via `mcp-publisher publish`.
4. List on mcp.so (self-registration form) and submit to Glama.

**User install:**
```bash
claude mcp add -e THRENODY_API_KEY=xxx threnody -- uvx threnody-mcp
```

### Option B: Claude Code plugin marketplace (Claude Code-specific, richest UX)

1. Create a plugin repo with `marketplace.json` + `plugin.json`.
2. Bundle MCP server config + any Threnody-specific skills or hooks.
3. Host at `your-org/threnody-marketplace` on GitHub.

**User install:**
```
/plugin install threnody@threnody-marketplace
```

### Option C: Smithery listing

1. Add `smithery.yaml` to the Threnody repo root.
2. Publish: `smithery mcp publish https://your-url -n yourorg/threnody`.

**User install:**
```bash
npx @smithery/cli install threnody --client claude
```

Note: Smithery targets Claude Desktop (separate app from Claude Code CLI) more
than Claude Code CLI directly.

### Summary comparison

| Channel                  | Reach          | UX for user                           | Python native? | Effort  |
|--------------------------|----------------|---------------------------------------|----------------|---------|
| PyPI + `uvx` + README    | All MCP clients | `claude mcp add ... -- uvx threnody-mcp` | Yes          | Low     |
| Official MCP registry    | All MCP clients | Auto-discovered by registry-aware clients | Yes         | Medium  |
| Claude Code plugin        | Claude Code only | `/plugin install threnody@marketplace` | Bundled       | Medium  |
| Smithery                 | Claude Desktop+ | `smithery mcp add threnody --client claude` | Via YAML  | Low     |
| mcp.so / Glama listing   | Discovery only | Links to your README install command  | N/A           | Very low |

---

## Sources

- [Official MCP registry GitHub](https://github.com/modelcontextprotocol/registry)
- [server.json schema spec (raw)](https://raw.githubusercontent.com/modelcontextprotocol/registry/refs/heads/main/docs/reference/server-json/generic-server-json.md)
- [Publish Your MCP Server (mcp registry guide)](https://modelcontextprotocol.info/tools/registry/publishing/)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Claude Code MCP quickstart](https://code.claude.com/docs/en/mcp-quickstart)
- [Claude Code: Connect to tools via MCP](https://code.claude.com/docs/en/mcp)
- [Claude Code: Discover and install plugins](https://code.claude.com/docs/en/discover-plugins)
- [Claude Code: Create and distribute a plugin marketplace](https://code.claude.com/docs/en/plugin-marketplaces)
- [Smithery CLI docs](https://smithery.ai/docs/concepts/cli)
- [Smithery publish quickstart](https://smithery.ai/docs/build/getting-started)
- [MCP Python SDK on PyPI](https://pypi.org/project/mcp/)
- [mcp-server-git on PyPI (Anthropic reference)](https://pypi.org/project/mcp-server-git/)
- [Glama: Official MCP registry server.json requirements](https://glama.ai/blog/2026-01-24-official-mcp-registry-serverjson-requirements)
- [MCP registries overview 2026](https://roxyapi.com/blogs/mcp-registries-where-to-list-your-server-2026)
- [Smithery CLI GitHub](https://github.com/smithery-ai/cli)
- [smithery.yaml example (kirill-markin)](https://github.com/kirill-markin/example-mcp-server/blob/main/smithery.yaml)
- [smithery.yaml example (mcp-memory-service)](https://github.com/doobidoo/mcp-memory-service/blob/main/smithery.yaml)
