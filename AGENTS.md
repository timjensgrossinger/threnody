# Repository Guidelines

## Project Structure & Module Organization

Threnody is a plain Python 3.10+ project with a shared core and thin provider adapters.

- `shared/`: routing, planning, orchestration, persistence, evaluation, and provider discovery.
- `codex/`, `copilot/`, `claude-code/`, `gemini/`, `cursor/`, and other provider directories: provider-specific entry points and model mappings.
- `mcp_server.py`: JSON-RPC/stdio MCP dispatch layer; keep business logic in `shared/`.
- `tests/`: pytest suite, with shared fixtures in `tests/conftest.py`.
- `shell/`: installed shell helpers and monitoring scripts.
- `config.yaml`: installation template, not the active runtime configuration.

Do not manually edit generated caches such as `providers.json` or `shared/data/model_prices.json`.

## Build, Test, and Development Commands

There is no package build step or project manifest. Use:

```bash
./install.sh
python3 -m pytest tests/ -v
THRENODY_TEST_MODE=1 python3 -m pytest tests/ -v
python3 -m pytest tests/test_router.py::test_base_score_low_tier -v
python3 -m py_compile mcp_server.py shared/router.py
python3 mcp_server.py
```

`./install.sh` installs dependencies, registrations, and shell aliases. Prefer test mode for hermetic runs that must ignore locally installed AI CLIs.

## Coding Style & Naming Conventions

Follow existing Python style: four-space indentation, `snake_case` functions and modules, `PascalCase` classes, and `UPPER_CASE` constants. Fully annotate public APIs and prefer PEP 604 unions such as `Path | None`. Use `log = logging.getLogger(__name__)` instead of production `print()` calls. Access SQLite only through `Database.conn()`, and add schema changes in `Database._init_schema()`.

## Testing Guidelines

Add focused pytest tests named `test_<behavior>.py` or `test_<behavior>()`. Reuse fixtures from `tests/conftest.py`; isolate filesystem, database, subprocess, and provider state. Run the affected test file first, then the full hermetic suite for shared or cross-provider changes. No fixed coverage threshold is documented, but regressions should include a test.

## Commit & Pull Request Guidelines

Recent history follows Conventional Commits, for example `feat(router): ...` and `fix(security): ...`. Keep commits scoped and imperative. Pull requests should explain behavior changes, list verification commands, link relevant issues, and call out configuration, schema, security, or provider-contract impacts. Include screenshots only for user-facing terminal or settings UI changes.

## Configuration & Security

Runtime configuration lives at `~/.local/lib/threnody/config.yaml`; editing the repository template does not update an existing installation. Reuse path-normalization, snapshot, and preview helpers for file writes. Never commit credentials, bearer tokens, local databases, or generated backups.
