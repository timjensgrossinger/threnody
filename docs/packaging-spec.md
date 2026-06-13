# Packaging Spec: `pyproject.toml` for `threnody-mcp`

**Date:** 2026-06-12
**Status:** Implementation-ready spec
**Inputs:** `threnody.manifest.json`, `docs/plugin-design.md`, `requirements.txt`,
`shared/version.py`, `mcp_server.py` (imports + `main()`), `shared/data/model_prices.json`
**Goal:** Add `pyproject.toml` so `pip install threnody-mcp` and `uvx threnody-mcp`
both work, without disturbing the existing `install.sh` flow.

---

## 0. Pre-flight findings

Before speccing anything, reading the existing code revealed three facts that
shape every decision below.

1. **`main()` already exists** at `mcp_server.py:10552`. It reads JSON-RPC from
   stdin and never returns. No code change is needed to the entry-point
   *behavior*. The only change needed is ensuring the import path resolves once
   the code lives inside a wheel.

2. **`VERSION` is the canonical version source.** `shared/version.py` reads
   `Path(__file__).parent.parent / "VERSION"` at import time. The `VERSION` file
   currently contains `0.2.0-alpha.1` (PEP 440 normalized: `0.2.0a1`). This is
   the single source of truth — it must stay that way.

3. **`mcp_server.py` inserts `BASE` onto `sys.path`** (`sys.path.insert(0,
   str(BASE))`) so all `from shared.*`, `from copilot.*`, etc. imports resolve
   relative to the file's directory. This works perfectly at runtime today but
   becomes wrong inside a wheel, where `BASE` is a zip-extracted temp dir, not
   the install root with all sibling packages. The packaging layout must put all
   importable packages under one top-level namespace so the wheel's site-packages
   entry makes them importable without the `sys.path` hack. See §3.

---

## 1. Package name on PyPI: `threnody-mcp`

**Recommendation: `threnody-mcp` (as already decided in `threnody.manifest.json`).**

| Name | Verdict | Reasoning |
|---|---|---|
| `threnody` | Avoid | Generic name; likely squatted or confusable. Harder to defend on PyPI name collisions. |
| `threnody-mcp` | **Use** | Already in `threnody.manifest.json`. Unambiguous purpose signal. Convention-aligned: `mcp-server-git`, `mcp-server-fetch` all append a qualifier. The `-mcp` suffix makes the package discoverable in PyPI searches for MCP servers. |

The **import namespace** (what users `import` in Python code) is `threnody`
(no dash), matching the subdirectory layout specified in §3.

---

## 2. Build backend: Hatchling

**Recommendation: Hatchling (`hatchling`).**

| Backend | Verdict | Reasoning |
|---|---|---|
| `flit-core` | Avoid | Best for single-module packages where `src/__init__.py` carries `__version__`. Threnody's layout — `mcp_server.py` + multiple sub-packages (`shared/`, provider dirs, `cli/`) + a `VERSION` file at root — is multi-package and non-trivial. Flit requires `__version__` in the top-level `__init__.py` and cannot read it from an external file without plugins. |
| `setuptools` | Viable fallback | Familiar, battle-tested, but verbose. Requires manual `find_packages` or `packages` list. The `dynamic` version via `attr:` can read `__version__` from a module but not a bare `VERSION` file without a custom hook. |
| `hatchling` | **Use** | First-class support for `[tool.hatch.version] path = "VERSION"` — reads a bare version file with no plugin needed. Clean `[tool.hatch.build.targets.wheel] packages` list maps arbitrary dirs into the wheel namespace. Actively maintained, PEP 517/518/660 compliant. Used by FastAPI, Pydantic, MCP Python SDK — the exact ecosystem Threnody lives in. |

---

## 3. Package layout and the `sys.path` problem

### 3.1 Current layout (flat, path-insert)

```
/Users/tim.grossinger/.local/lib/threnody/
  mcp_server.py          # top-level, not in any package
  shared/                # sub-package
  claude-code/           # sub-package
  copilot/               # sub-package
  codex/                 # sub-package
  cursor/                # sub-package
  junie/                 # sub-package
  mistral/               # sub-package
  opencode/              # sub-package
  blackbox/              # sub-package
  cli/                   # sub-package
  VERSION
  ...
```

`mcp_server.py` does `sys.path.insert(0, str(BASE))` so `from shared.config
import ...` and `from copilot.providers import CopilotProvider` resolve. When
running from the source tree, `BASE` is the repo root and this works.

Inside a wheel, the extracted layout is flat inside site-packages. The
`sys.path` trick will still work there — `BASE` resolves to the package dir,
and sibling packages are in the same directory. **No layout change is strictly
required.**

However, the entry point `threnody.mcp_server:main` requires `mcp_server` to
be importable as `threnody.mcp_server`. That means we need a `threnody`
top-level package.

### 3.2 Recommended wheel layout

Use Hatchling's `packages` mapping to collect the flat source tree into a
`threnody` namespace inside the wheel:

```toml
[tool.hatch.build.targets.wheel]
packages = [
  "shared",
  "claude-code",
  "copilot",
  "codex",
  "cursor",
  "junie",
  "mistral",
  "opencode",
  "blackbox",
  "cli",
]
```

And separately, map `mcp_server.py` as a module inside a synthetic `threnody`
package. The cleanest approach that avoids restructuring the source tree is:

**Add a minimal `threnody/__init__.py` shim at the repo root** that Hatchling
includes as the `threnody` package, with `mcp_server.py` symlinked or copied
into it. However, symlinks in sdists are fragile across platforms.

**Better approach (zero source restructuring):** Use Hatchling's `force-include`
to place `mcp_server.py` at `threnody/mcp_server.py` in the wheel, and create
a `threnody/__init__.py` in the source tree (it can be empty or import
`__version__`):

```toml
[tool.hatch.build.targets.wheel]
packages = ["threnody"]      # picks up threnody/__init__.py

[tool.hatch.build.targets.wheel.force-include]
"mcp_server.py" = "threnody/mcp_server.py"
"shared"        = "threnody/shared"
"claude-code"   = "threnody/claude_code"   # note: dashes → underscores
"copilot"       = "threnody/copilot"
"codex"         = "threnody/codex"
"cursor"        = "threnody/cursor"
"junie"         = "threnody/junie"
"mistral"       = "threnody/mistral"
"opencode"      = "threnody/opencode"
"blackbox"      = "threnody/blackbox"
"cli"           = "threnody/cli"
"shared/data"   = "threnody/shared/data"
```

**Problem with this approach:** `mcp_server.py` imports `from shared.config
import ...` and `from copilot.providers import ...` using the *flat* names.
Inside the wheel, these would need to be `from threnody.shared.config import
...` and `from threnody.copilot.providers import ...`. That requires modifying
every import in `mcp_server.py` and all provider modules — a large, risky
change.

### 3.3 Recommended approach: preserve flat layout, add thin shim

Keep the flat source layout unchanged. Add a `threnody/` directory at the repo
root containing only:

```
threnody/
  __init__.py      # empty or just __version__ = ...
  mcp_server.py    # one-liner shim: from mcp_server import main (see below)
```

The shim `threnody/mcp_server.py`:

```python
"""Entry point shim — delegates to the top-level mcp_server module."""
from __future__ import annotations
import importlib.util
import os
import sys
from pathlib import Path

# When running from a wheel, the flat packages (shared/, copilot/, etc.)
# are installed as top-level packages in site-packages. Insert the package
# base into sys.path so the flat imports in the real mcp_server.py resolve.
_pkg_root = Path(__file__).resolve().parent.parent
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from mcp_server import main  # noqa: E402  (after sys.path fixup)

__all__ = ["main"]
```

This keeps all existing flat imports working without touching a single line of
the real `mcp_server.py`, and gives Hatchling a clean `packages = ["threnody"]`
configuration.

**Required source change:** create `threnody/__init__.py` and
`threnody/mcp_server.py` (the shim above). No other source changes.

### 3.4 `shared/version.py` path fixup

`shared/version.py` computes:

```python
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
```

From source tree: `shared/version.py` → `.parent.parent` = repo root → `VERSION` ✓

From wheel: `site-packages/shared/version.py` → `.parent.parent` = `site-packages/`
→ no `VERSION` there. **This will break at runtime in the wheel.**

**Fix required in `shared/version.py`:**

```python
"""Release version — single source of truth for Threnody."""
from __future__ import annotations

from pathlib import Path
import importlib.resources

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def get_version() -> str:
    """Return the current release version string."""
    # Source-tree path (install.sh / editable installs)
    if _VERSION_FILE.exists():
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    # Wheel path: read from package metadata
    try:
        from importlib.metadata import version
        return version("threnody-mcp")
    except Exception:
        return "0.0.0+unknown"


__version__ = get_version()
```

This is the only required change to existing source files.

---

## 4. Files to include and exclude

### Include in sdist and wheel

| Path | Notes |
|---|---|
| `mcp_server.py` | Core server — included via `force-include` into `threnody/` |
| `shared/` | Entire directory, including `shared/data/model_prices.json` |
| `claude-code/` | Provider adapter |
| `copilot/` | Provider adapter |
| `codex/` | Provider adapter |
| `cursor/` | Provider adapter |
| `junie/` | Provider adapter |
| `mistral/` | Provider adapter |
| `opencode/` | Provider adapter |
| `blackbox/` | Provider adapter |
| `cli/` | `threnody` CLI surface (`audit.py`, `gain.py`, `inspect.py`, etc.) |
| `threnody/__init__.py` | New shim package (created per §3.3) |
| `threnody/mcp_server.py` | New entry-point shim (created per §3.3) |
| `config.example.yaml` | Useful reference; does not contain secrets |
| `VERSION` | Required by `shared/version.py` in source-tree installs; included as package data |
| `README.md` | PyPI long description; must contain `mcp-name: io.github.timjensgrossinger/threnody` ownership line |
| `LICENSE` | Apache-2.0 text |
| `NOTICE` | Attribution notices |

### Exclude from sdist and wheel

| Path | Reason |
|---|---|
| `tests/` | Dev-only; never ship in a distribution wheel |
| `sandbox/` | Dev experimentation; not user-facing |
| `docs/` | Docs belong on the web (GitHub Pages / README), not in the wheel |
| `scripts/` | Build/publish tooling; dev-only |
| `shell/` | Shell aliases (`ghc.sh`, `threnody-watch`) — these are `install.sh` territory, not the wheel |
| `skills/` | Bundled via the Claude plugin manifest, not via PyPI |
| `*.db`, `*.db-wal`, `*.db-shm` | Runtime state — never distribute |
| `providers.json` | Machine-local provider scan output — never distribute |
| `config.yaml` | Per-machine config — never distribute (`.gitignore` already excludes it) |
| `cache.db` | Runtime DB — never distribute |
| `install.sh`, `uninstall.sh`, `sectest.sh` | Shell scripts for the non-PyPI path |
| `.claude/`, `.cursor/`, `.github/` | Development tool config |
| `copilot-instructions.md`, `CLAUDE.md`, `AGENTS.md` | Host-shell integration files |
| `INSTRUCTIONS.md` | Rendered by `shared/instructions.py` at runtime |
| `threnody.manifest.json` | Build input artifact, not user-facing |
| `*.py[cod]`, `__pycache__/` | Compiled artifacts |
| `.planning/` | Internal planning notes |
| `backup/` | Operator backups |

---

## 5. Optional dependency groups

```toml
[project.optional-dependencies]
ui = [
    "rich>=13.0,<15",
    "questionary>=2.0,<3",
]
dev = [
    "pytest>=7.0",
    "pyyaml>=6.0,<7",   # already a runtime dep, listed here for dev clarity
]
```

Notes:

- `rich` and `questionary` are used only by `shared/settings_wizard.py` and
  `shared/doctor.py` TUI output. The server imports them with `try/except
  ImportError` guards already in place (confirmed by `requirements.txt` listing
  them as optional extras in the manifest). The `ui` extra name matches the
  `wizard` group name from `threnody.manifest.json`'s `dependencies.optional`
  — rename to `wizard` is an option, but `ui` is more conventional on PyPI.
- `uvx threnody-mcp[ui]` installs the wizard dependencies for users who want
  `threnody settings` to have the full TUI.
- No `test` extra is needed in the published package — `pytest` is a dev
  concern only.

---

## 6. `install.sh` vs post-install script

**Decision: `install.sh` remains the primary install for power users. No
post-install script should be added to `pyproject.toml`.**

### Reasoning

| Approach | Assessment |
|---|---|
| Post-install script in pyproject.toml (`[project.scripts]` hook or `hatch` post-install) | PyPI wheels do not support post-install hooks. `pip` and `uvx` run no arbitrary code after wheel installation. Any post-install hook mechanism (`setup.py install_scripts`, deprecated) is explicitly not supported by modern build backends including Hatchling. |
| Separate `threnody-install` console script | Could shell out to `install.sh`, but breaks on Windows and adds complexity. The `uvx` runtime context has no TTY for the wizard. |
| `install.sh` unchanged, remains the power-user path | **Correct.** Documented in `docs/plugin-design.md` §3.3 and §6: `install.sh --plugin-mode` is the bridge for PyPI users who want on-disk config seeding. The wheel itself runs correctly with zero setup (defaults-safe, lazy init). |

The wheel is intentionally self-contained and zero-config: `_ensure_init()`
auto-creates `cache.db` and uses `TGsConfig.defaults()` when `config.yaml` is
absent. Users who want shell aliases, the interactive wizard, or
custom-instruction sync run `install.sh` or `install.sh --plugin-mode`
explicitly. This is documented in the first-run `initialize` response (per
`docs/plugin-design.md` §5).

**`install.sh` changes needed for PyPI co-existence** (additive only):
- Add `--plugin-mode` flag as specced in `docs/plugin-design.md` §6.
- Add a check: if running inside a virtualenv/uvx environment (`sys.prefix !=
  sys.base_prefix`), emit a note that `pip install threnody-mcp` is an
  alternative.

---

## 7. Version management

**Single source of truth: `VERSION` file.**

The chain is:

```
VERSION                      ← edited by humans / release scripts
  ↓ read by
shared/version.py            ← provides get_version() / __version__
  ↓ read by
pyproject.toml               ← [tool.hatch.version] path = "VERSION"
  ↓ stamped into
wheel metadata (PKG-INFO)    ← importlib.metadata.version("threnody-mcp")
  ↓ fallback read by
shared/version.py (wheel)    ← when VERSION file is not present on-disk
```

### `pyproject.toml` version stanza

```toml
[tool.hatch.version]
path = "VERSION"
```

Hatchling's built-in `version` source supports a `path` key that reads the
version from an arbitrary file. It expects the file to contain a bare PEP 440
version string — `0.2.0a1` — or a string that it can normalize. The `VERSION`
file currently contains `0.2.0-alpha.1`, which Hatchling normalizes to `0.2.0a1`
automatically.

**Do not use `[tool.hatch.version] source = "vcs"` (git tags)**: the install
dir is not a git repo at runtime, and `uvx` runs from a cached wheel where git
history is absent.

### Version bump workflow

1. Edit `VERSION` → `0.2.1` (or `0.3.0a1`, etc.)
2. `python3 -m shared.version` sanity-check (optional)
3. Update `threnody.manifest.json` `version` + `pypi_version` fields
4. Run `scripts/build-manifests.py` to regenerate `server.json` and plugin
   manifests
5. `git tag v0.2.1 && git push --tags`
6. `python3 -m build && twine upload dist/*` (or GitHub Actions publish workflow)

**No hardcoded version anywhere in `pyproject.toml`.**

---

## 8. Entry point confirmation

`main()` exists at `mcp_server.py:10552`:

```python
def main() -> None:
    log.info("Threnody MCP server %s — cross-provider orchestrator", get_version())
    ...
    for line in sys.stdin:
        ...
    # runs until stdin closes
```

The console script entry point:

```toml
[project.scripts]
threnody-mcp = "threnody.mcp_server:main"
```

resolves via the shim at `threnody/mcp_server.py` (§3.3), which re-exports
`main` from the real `mcp_server.py` after fixing up `sys.path`. No behavioral
change to `main()` is needed.

Additionally expose the `threnody` CLI surface (currently invoked as
`python3 -m cli.*` or via `install.sh` symlinks):

```toml
[project.scripts]
threnody-mcp  = "threnody.mcp_server:main"
threnody      = "threnody.cli.inspect:main"   # if cli/inspect.py has main()
```

Check whether each `cli/*.py` module exposes a `main()` before adding its
entry point; the spec below includes only `threnody-mcp` as the required entry
point since the `cli/` modules are primarily used via `threnody` shell wrapper
from `install.sh`.

---

## 9. Complete proposed `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "threnody-mcp"
dynamic = ["version"]
description = "Local-first MCP meta-harness: routes, plans, and swarms multi-agent coding work while the host shell executes via Agent/Task subagents."
readme = "README.md"
license = { text = "Apache-2.0" }
authors = [
    { name = "Tim Grossinger", email = "tim.grossinger@movec.com" },
]
keywords = ["mcp", "routing", "multi-agent", "orchestration", "swarm", "claude", "copilot", "ai"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: Apache Software License",
    "Operating System :: OS Independent",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Topic :: Software Development :: Libraries :: Python Modules",
    "Topic :: Scientific/Engineering :: Artificial Intelligence",
]
requires-python = ">=3.10,<3.14"
dependencies = [
    "pyyaml>=6.0,<7",
]

[project.optional-dependencies]
ui = [
    "rich>=13.0,<15",
    "questionary>=2.0,<3",
]
dev = [
    "pytest>=7.0",
    "build>=1.0",
    "twine>=5.0",
]

[project.scripts]
threnody-mcp = "threnody.mcp_server:main"

[project.urls]
Homepage      = "https://github.com/timjensgrossinger/threnody"
Repository    = "https://github.com/timjensgrossinger/threnody"
Documentation = "https://github.com/timjensgrossinger/threnody#readme"
Changelog     = "https://github.com/timjensgrossinger/threnody/blob/main/CHANGELOG.md"
"Bug Tracker" = "https://github.com/timjensgrossinger/threnody/issues"


# ---------------------------------------------------------------------------
# Version: read from VERSION file (single source of truth)
# ---------------------------------------------------------------------------
[tool.hatch.version]
path = "VERSION"


# ---------------------------------------------------------------------------
# Wheel contents
# ---------------------------------------------------------------------------
[tool.hatch.build.targets.wheel]
# The threnody/ shim package (threnody/__init__.py + threnody/mcp_server.py)
# is the primary package Hatchling collects via standard package discovery.
packages = ["threnody"]

[tool.hatch.build.targets.wheel.force-include]
# Flat provider sub-packages and shared core — placed as top-level packages
# in site-packages so mcp_server.py's existing flat imports continue to work.
"shared"      = "shared"
"claude-code" = "claude_code"   # PEP 8: dashes become underscores in import names
"copilot"     = "copilot"
"codex"       = "codex"
"cursor"      = "cursor"
"junie"       = "junie"
"mistral"     = "mistral"
"opencode"    = "opencode"
"blackbox"    = "blackbox"
"cli"         = "cli"
# Real server module alongside the shim for the sys.path re-export to find
"mcp_server.py" = "mcp_server.py"
# Package data
"shared/data" = "shared/data"
"VERSION"     = "VERSION"
"config.example.yaml" = "config.example.yaml"


# ---------------------------------------------------------------------------
# sdist contents
# ---------------------------------------------------------------------------
[tool.hatch.build.targets.sdist]
include = [
    "threnody/",
    "shared/",
    "claude-code/",
    "copilot/",
    "codex/",
    "cursor/",
    "junie/",
    "mistral/",
    "opencode/",
    "blackbox/",
    "cli/",
    "mcp_server.py",
    "shared/data/model_prices.json",
    "config.example.yaml",
    "VERSION",
    "README.md",
    "LICENSE",
    "NOTICE",
    "AUTHORS",
    "CHANGELOG.md",
    "requirements.txt",
    "threnody.manifest.json",
]
exclude = [
    "tests/",
    "sandbox/",
    "docs/",
    "scripts/",
    "shell/",
    "skills/",
    ".claude/",
    ".cursor/",
    ".github/",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.db.bak*",
    "providers.json",
    "config.yaml",
    "install.sh",
    "uninstall.sh",
    "sectest.sh",
    "copilot-instructions.md",
    "CLAUDE.md",
    "AGENTS.md",
    "INSTRUCTIONS.md",
    "__pycache__/",
    "*.pyc",
    ".planning/",
    "backup/",
    ".runtime/",
    "audit_secret",
    "cache.db",
]
```

---

## 10. New files to create

| File | Content | Notes |
|---|---|---|
| `pyproject.toml` | The complete stanza from §9 | Primary deliverable |
| `threnody/__init__.py` | `"""Threnody MCP meta-harness."""` + `from shared.version import __version__` | Creates the `threnody` namespace package |
| `threnody/mcp_server.py` | Entry-point shim (see §3.3) | Fixes `sys.path` then re-exports `main` |

Modify:

| File | Change | Notes |
|---|---|---|
| `shared/version.py` | Add `importlib.metadata` fallback (see §3.4) | Required for wheel installs where `VERSION` file is not on the Python path |

---

## 11. Build and publish workflow

```bash
# Install build tools (one-time)
pip install build twine

# Build sdist + wheel
python3 -m build

# Check the distributions
twine check dist/*

# Upload to PyPI (requires API token)
twine upload dist/*

# Verify uvx works
uvx threnody-mcp --help   # will fail gracefully (no --help flag); verify it prints MCP usage
echo '{"jsonrpc":"2.0","method":"initialize","params":{},"id":1}' | uvx threnody-mcp
```

For `uvx` caching: `uvx` caches wheels in `~/.cache/uv/`. Re-run with `uvx
--no-cache threnody-mcp` to pick up a freshly published version.

---

## 12. `README.md` requirement

The PyPI long-description (the full `README.md`) must contain the ownership
anchor for the official MCP registry's verification step:

```
mcp-name: io.github.timjensgrossinger/threnody
```

This line should appear in a comment or metadata block that does not disrupt
the human-readable README. Recommended placement: in the HTML comment block at
the very top of `README.md`:

```markdown
<!-- mcp-name: io.github.timjensgrossinger/threnody -->
```

---

## 13. `claude-code` directory name collision

The `claude-code/` directory has a hyphen. Python module names cannot contain
hyphens. The `force-include` mapping above renames it to `claude_code` in the
wheel. **However**, `mcp_server.py` does not appear to import from `claude-code/`
directly (it imports from `copilot.providers`, `shared.*`, etc. — `claude-code/`
contains `entry.py` and `providers.py` used by the orchestrator). Verify with:

```bash
grep -rn "from claude.code\|from claude_code\|import claude" \
  /Users/tim.grossinger/.local/lib/threnody/mcp_server.py \
  /Users/tim.grossinger/.local/lib/threnody/shared/
```

If any import uses `claude_code` (underscore), the `force-include` rename
handles it. If `shared/provider_factory.py` or `shared/discovery.py` uses
`importlib.import_module("claude-code.entry")` (with hyphen), those calls will
break in the wheel and must be patched to use `claude_code` (underscore). This
is a pre-publish validation step.

---

## 14. Pre-publish checklist

- [ ] Create `threnody/__init__.py` and `threnody/mcp_server.py` shim
- [ ] Patch `shared/version.py` with `importlib.metadata` fallback
- [ ] Verify `README.md` contains `<!-- mcp-name: io.github.timjensgrossinger/threnody -->`
- [ ] Confirm `claude-code` import names (§13) — fix any `importlib.import_module` calls
- [ ] Run `python3 -m build` and inspect `dist/threnody_mcp-0.2.0a1-py3-none-any.whl` contents with `unzip -l`
- [ ] Run `pip install dist/threnody_mcp-0.2.0a1-py3-none-any.whl` in a fresh venv, verify `threnody-mcp` script is on `PATH`
- [ ] Run `echo '{"jsonrpc":"2.0","method":"initialize","params":{},"id":1}' | threnody-mcp` and confirm a valid JSON-RPC response
- [ ] Run `uvx --no-cache threnody-mcp` (after PyPI upload) and confirm startup
- [ ] Confirm `get_version()` returns `0.2.0a1` both from wheel and source tree
- [ ] Run `python3 -m pytest tests/ -v` against source tree (wheel packaging does not affect tests)
- [ ] Bump `threnody.manifest.json` `pypi_version` to match before upload
