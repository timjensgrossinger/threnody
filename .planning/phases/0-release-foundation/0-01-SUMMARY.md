---
phase: 0-release-foundation
plan: 01
subsystem: packaging
tags: [hatchling, wheel, sdist, mcp, versioning]
requires: []
provides: [installable-threnody-mcp, packaging-regression-tests]
affects: [release-foundation]
tech-stack:
  added: [Hatchling]
  patterns: [flat-layout packaging, import shim, metadata version fallback]
key-files:
  created:
    - pyproject.toml
    - threnody/__init__.py
    - threnody/mcp_server.py
    - tests/test_packaging.py
  modified:
    - shared/version.py
decisions:
  - Keep the existing flat runtime imports and expose the package entry point through a thin threnody shim.
  - Keep VERSION as the release source and configure Hatchling to parse its bare version content.
metrics:
  duration: approximately 15 minutes
  completed: 2026-07-11
---

# Phase 0 Plan 1: Packaging Foundation Summary

Hatchling packaging now builds `threnody-mcp` wheel and sdist artifacts with a working `threnody-mcp` stdio entry point, flat-runtime compatibility, and archive regression coverage.

## Files Changed

- Added `pyproject.toml` with dynamic `VERSION` metadata, Python/dependency constraints, UI extras, console script, and runtime/development archive exclusions.
- Added `threnody` package shims delegating to the existing top-level `mcp_server.main`.
- Added installed-metadata fallback to `shared/version.py`.
- Added hermetic packaging tests for metadata, archive contents, entry-point delegation, and source-tree version behavior.

## Tests Run

- `python3 -m py_compile shared/version.py threnody/__init__.py threnody/mcp_server.py`
- `python3 -m pytest tests/test_packaging.py -q` — 4 passed
- `python3 -m build --sdist --wheel` — built `threnody_mcp-0.3.0a2.tar.gz` and `threnody_mcp-0.3.0a2-py3-none-any.whl`

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking packaging configuration] Configured Hatchling to parse bare VERSION content**

- **Found during:** Build verification
- **Issue:** Hatchling’s default version regex expects an assignment-style file and rejected the repository’s bare `VERSION` file.
- **Fix:** Added `pattern = "(?P<version>.+)"` while retaining `VERSION` as the single source path.
- **Files modified:** `pyproject.toml`
- **Commit:** `059c084`

No unrelated files were modified; the pre-existing `uninstall.sh` change and `sandbox/` files remain dirty and untouched.

## Self-Check: PASSED

- Summary file exists.
- Task commits `059c084` and `e05cc1e` exist in repository history.
- Verification commands passed.
