---
phase: 0-release-foundation
plan: 03
subsystem: release-documentation
tags: [mcp, documentation, providers, benchmarks, clean-install]
requires: [0-01]
provides: [factual-release-docs, clean-install-smoke-coverage]
affects: [release-foundation]
tech-stack:
  added: []
  patterns: [temporary-build-artifacts, fresh-venv-install, stdio-json-rpc-smoke]
key-files:
  created:
    - tests/test_clean_install.py
  modified:
    - README.md
    - docs/MCP_TOOLS.md
    - docs/BENCHMARKS.md
    - docs/ROUTING_QUALITY.md
    - docs/PROVIDER_COMPATIBILITY.md
    - docs/RELEASE_LIMITATIONS.md
decisions:
  - Keep install.sh as the full CLI/power-user path while documenting the packaged threnody-mcp stdio entry point.
  - Report the 53 schemas returned by tools/list and distinguish them from unpublished trace/session handlers.
  - Treat provider live smoke as an explicit machine- and entitlement-dependent gate rather than claiming it from hermetic tests.
metrics:
  duration: approximately 15 minutes
  completed: 2026-07-11
---

# Phase 0 Plan 3: Release Documentation and Clean-Install Verification Summary

Release-facing installation, MCP, benchmark, routing, provider, and limitation
claims now match the current source tree. Wheel and sdist smoke coverage builds
both formats, installs each into a fresh virtual environment, checks installed
metadata/imports, and completes the MCP `initialize` handshake without host
CLIs.

## What Changed

- Added the packaging ownership anchor and documented `uvx threnody-mcp`,
  `pip install threnody-mcp`, the `threnody-mcp` entry point, and the unchanged
  `install.sh` path in `README.md`.
- Reconciled `docs/MCP_TOOLS.md` with the 53 published schemas, host-native
  reporting tools, utility-only delegation boundaries, approval requirements,
  and the `2024-11-05` initialize response.
- Refreshed benchmark and routing-evaluation counts from the 2026-07-11
  hermetic run: 39 passing eval fixtures and 2 intentional boundary skips.
- Reclassified provider roles from the registry and bounded live-coverage
  claims to what was actually observed during this audit.
- Documented the tag-driven release workflow and retained the real alpha
  limitations around entitlements, local CLIs, Windows, sandboxing, policy,
  and live coverage.
- Added `tests/test_clean_install.py` for wheel and sdist installs, metadata,
  imports, and JSON-RPC initialization.

## Verification

- `THRENODY_TEST_MODE=1 python3 -m shared.routing_eval` — 39 passed, 0
  failed, 2 skipped.
- `UV_NO_PROGRESS=1 uv run --python 3.13 --with pytest --with build python -m
  pytest tests/test_clean_install.py -q` — 2 passed.
- `THRENODY_TEST_MODE=1 env -u COPILOT_CLI -u COPILOT_RUN_APP -u CLAUDE_CODE
  -u CLAUDE_CODE_SESSION -u OPENCODE_HOST -u OPENCODE_SESSION python3 -m
  pytest tests/ -q` — 2,165 passed, 3 skipped.
- The provider compatibility command passed 240 tests; packaging plus MCP
  protocol regressions passed 247 tests in the combined focused run.
- `python3 -m py_compile tests/test_clean_install.py` and `git diff --check`
  passed.
- The host Python is 3.14.5 while the package declares Python `<3.14`, so the
  clean-install module reports one explicit skip there rather than attempting
  an invalid install. The supported Python 3.13 run above passed both formats.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Test expectation bug] Accounted for PEP 440 metadata normalization**

- **Found during:** Clean-install smoke verification
- **Issue:** Hatchling/pip correctly report `0.3.0a2` in installed metadata,
  while the repository `VERSION` file intentionally contains
  `0.3.0-alpha.2`.
- **Fix:** The test compares normalized metadata to the raw source version
  returned by the runtime module.
- **Files modified:** `tests/test_clean_install.py`
- **Commit:** `735c44a`

**2. [Rule 3 - Environment compatibility] Made unsupported local Python
verification explicit**

- **Found during:** Clean-install smoke verification on Python 3.14.5
- **Issue:** The declared package range is `>=3.10,<3.14`; pip correctly
  rejected the artifact in the local Python 3.14 environment.
- **Fix:** The test emits an explicit module-level skip on unsupported
  interpreters, and the same test was run successfully under Python 3.13.
- **Files modified:** `tests/test_clean_install.py`
- **Commit:** `735c44a`

No manifest or release-workflow files were modified. Pre-existing dirty files
and untracked generated release artifacts were preserved.

## Known Stubs

None in the files created or modified by this plan.

## Commits

- `6d96be2` — docs(0-03): reconcile MCP and routing documentation
- `bd9c717` — docs(0-03): bound provider and release claims
- `735c44a` — test(0-03): verify clean wheel and sdist installs

## Self-Check: PASSED

- Summary file exists at `.planning/phases/0-release-foundation/0-03-SUMMARY.md`.
- All three task commits exist in repository history.
- All planned documentation files and `tests/test_clean_install.py` exist.
- Unrelated `uninstall.sh`, sandbox directories, and pre-existing release
  artifacts remain outside the plan commits.
