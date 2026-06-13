# MCP First-Run Detection — Implementation Spec

**Date:** 2026-06-12  
**Status:** Implementation-ready  
**Inputs:** `docs/install-audit.md`, `docs/plugin-design.md`  
**Scope:** `mcp_server.py` only — no changes to `shared/` unless noted

---

## 1. Problem Statement

When Threnody reaches a user via the plugin/`uvx` path, `install.sh` never
runs. The server starts, loads `TGsConfig.defaults()`, and appears healthy — but
no host AI CLI has been confirmed, no `providers.json` exists, and routing is in
advisory mode with zero operator preferences. The user gets no signal that
setup was skipped.

The server must detect this condition non-blockingly and guide the user to run
setup, without hanging (no TTY in a stdio MCP subprocess) and without breaking
the lazy-init design that makes the server safe to use with defaults.

---

## 2. Design Decisions (from plugin-design.md §5)

- **Non-blocking.** Tool calls complete normally even when unconfigured. No
  hard gate that rejects calls.
- **Marker file.** A `.threnody-initialized` marker (mtime = first successful
  init) suppresses the hint after first use without requiring the user to create
  `config.yaml`.
- **Two surfaces.** (a) `initialize` response gets an `instructions` field;
  (b) `check_providers` gets enriched fields (`setup_required`, `config_mode`,
  `setup_hint`).
- **No wizard.** `settings_wizard.py` requires a TTY; the MCP server runs
  without one. Guidance points to `threnody settings` instead.

---

## 3. What Constitutes "Unconfigured"

`TGsConfig.defaults()` returns a perfectly usable config — routing works,
the DB is created, providers are discovered lazily. So "config file missing" is
insufficient by itself to declare the system unconfigured.

The definition is a conjunction of **three independent signals**, all of which
must be true simultaneously:

```
unconfigured := (
    not CONFIG_YAML.exists()           # (A) no operator config on disk
    AND not _INITIALIZED_MARKER.exists() # (B) first-run marker absent
    AND _count_routable_providers() == 0 # (C) zero execution-capable providers
                                         #     after lazy init
)
```

### Signal rationale

**(A) `not CONFIG_YAML.exists()`**
`CONFIG_YAML` is `~/.local/lib/threnody/config.yaml` (from `shared/config.py:33`).
If this file exists, the user or installer has already gone through first-run
configuration; no hint needed.

**(B) `not _INITIALIZED_MARKER.exists()`**
A user who installed via `install.sh` and ran `THRENODY_SKIP_WIZARD=1` will
have no `config.yaml` but should still not see the first-run nag. The marker
file (path: `~/.local/lib/threnody/.threnody-initialized`) is written after the
first successful `_ensure_init()` completion, so it is present for any non-fresh
install path. An absent marker means the server has never completed a successful
init cycle on this machine.

**(C) `_count_routable_providers() == 0`**
This is the clearest signal that no host CLI is present. `TGsConfig.defaults()`
gives a valid config, but if discovery finds zero routable providers, the server
cannot actually execute any work. This check uses the registry already loaded by
`_ensure_init()` (via `_get_registry_with_config()`) so it adds no extra
subprocess cost.

"Routable" here means `provider.routeable == True` in `to_compact_dict()` —
i.e., the binary was found AND readiness probed positive.

### Edge case: `config.yaml` exists but providers absent

If (A) is false (config file present), the full predicate is false — no hint
shown. This is intentional: a user who has gone through the wizard is presumed
to understand their setup even if all providers happen to be offline.

### Edge case: `_ensure_init()` fails

If init itself throws, the existing `-32603` error propagates as before. The
first-run check only runs *after* a successful init (see §4.2 and §4.3 below).

---

## 4. Implementation: Where and How

### 4.1 New Module-Level Constants (add near line 175 after lazy globals block)

```python
# ---------------------------------------------------------------------------
# First-run detection
# ---------------------------------------------------------------------------

from shared.config import BASE_DIR as _CONFIG_BASE_DIR

_INITIALIZED_MARKER: Path = _CONFIG_BASE_DIR / ".threnody-initialized"

# Tools that bypass the first-run gate (always allowed; never block).
_ALWAYS_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "check_providers",
    "inspect_status",
    "inspect_task",
    "tune_show",
    "routing_exception_list",
    "list_subtasks",
})

# Populated after first _ensure_init() succeeds; None = not yet determined.
_first_run_state: bool | None = None
_first_run_lock = threading.Lock()
```

Add these after line 199 (end of the `_subtasks_lock` declaration), before
line 202 (the `SubtaskExecutionTimeout` class).

`BASE_DIR` is already importable from `shared.config`; the import at line 47
of `mcp_server.py` imports `CONFIG_YAML` from `shared.config`, so `BASE_DIR`
is in the same module — no new dependency.

---

### 4.2 `_detect_first_run()` Helper (add after `_config_file_signature()` at line ~689)

Insert after line 688 (end of `_config_file_signature()`):

```python
def _count_routable_providers() -> int:
    """Count execution-routable providers using the in-process registry.

    Uses `_get_registry_with_config()` which was already called during
    `_ensure_init()` — no extra subprocess launch or network probe.
    Returns 0 on any error so the caller stays safe.
    """
    try:
        registry = _get_registry_with_config()
        compact = registry.to_compact_dict()
        return sum(
            1 for p in compact.get("providers", [])
            if p.get("routeable") is True
        )
    except Exception:
        log.debug("_count_routable_providers failed", exc_info=True)
        return 0


def _detect_first_run() -> bool:
    """Return True if the server is in an unconfigured first-run state.

    Predicate (all three must hold):
      A — config.yaml absent
      B — .threnody-initialized marker absent
      C — zero routable providers after init

    Result is cached in _first_run_state after the first call.
    """
    global _first_run_state
    if _first_run_state is not None:
        return _first_run_state
    with _first_run_lock:
        if _first_run_state is not None:
            return _first_run_state
        a = not CONFIG_YAML.exists()
        b = not _INITIALIZED_MARKER.exists()
        c = (_count_routable_providers() == 0) if (a and b) else False
        result = a and b and c
        _first_run_state = result
        log.debug(
            "first-run detection: config_missing=%s marker_missing=%s zero_routable=%s → %s",
            a, b, c, result,
        )
    return result


def _write_initialized_marker() -> None:
    """Write .threnody-initialized to suppress future first-run hints.

    Best-effort: logs debug on failure so a read-only install dir never
    breaks startup.
    """
    try:
        _INITIALIZED_MARKER.touch(exist_ok=True)
        log.debug("wrote initialized marker: %s", _INITIALIZED_MARKER)
    except Exception:
        log.debug("could not write initialized marker", exc_info=True)
```

---

### 4.3 Marker Write — Where to Call `_write_initialized_marker()`

The marker must be written exactly once: after the **first successful
`_ensure_init()` completion**. The right place is the end of `_ensure_init()`
at line 671 (after `_shutdown_registered = True`), inside the `needs_full_init`
branch, after all globals are assigned and background daemons started:

```python
# Existing line 671 (end of _shutdown_registered block):
                _shutdown_registered = True

            # ↓ ADD THIS BLOCK (after line 671, still inside `needs_full_init`):
            if not _INITIALIZED_MARKER.exists():
                _write_initialized_marker()
```

This placement is correct because:
- It runs exactly once (guarded by `needs_full_init`).
- All critical globals are assigned before this point.
- The marker is written before the function returns, so subsequent calls to
  `_detect_first_run()` see `_INITIALIZED_MARKER.exists() == True`.
- The `_first_run_state` cache may already be populated from a `check_providers`
  call that ran before init; the marker write is still correct because the
  predicate re-evaluation path (`_detect_first_run()`) will return the cached
  value, and the marker prevents stale state on the next server restart.

---

### 4.4 `initialize` Handler — Add `instructions` Field

**Location:** line 10453–10462.

**Change:** add a conditional `instructions` field to the `send_response` call
when first-run is detected. The detection here is intentionally **pre-init**
(no `_ensure_init()` call) and uses only signals (A) and (B) — fast path, no
provider probe.

```python
    if method == "initialize":
        global _client_name
        client_info = params.get("clientInfo", {})
        _client_name = client_info.get("name")
        log.info("MCP initialize — client: %s", _client_name)

        # Fast first-run check using only file-system signals (no DB open).
        # Signal C (zero routable providers) is not evaluated here because
        # _ensure_init() has not run yet. The check_providers tool returns
        # the full three-signal verdict once init completes.
        _precheck_first_run = (
            not CONFIG_YAML.exists() and not _INITIALIZED_MARKER.exists()
        )
        _init_response: dict = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "Threnody", "version": get_version()},
        }
        if _precheck_first_run:
            _init_response["instructions"] = (
                "Threnody is running with default settings — no config.yaml was found "
                "and first-run setup has not completed. Routing is in advisory mode; "
                "all detected host AI CLIs are enabled with no provider preferences. "
                "This is functional but unoptimized. To complete setup, run: "
                "`threnody settings` (or re-run `./install.sh` for the full wizard). "
                "If you installed via `claude mcp add` or a plugin, no install.sh step "
                "is needed — `threnody settings` is sufficient. "
                "To check what is currently configured, call the `check_providers` tool."
            )
            log.info("first-run hint included in initialize response")
        send_response(req_id, _init_response)
```

The `instructions` field is a standard MCP `initialize` response field (see MCP
spec 2024-11-05, §3.1 `InitializeResult`). Host clients surface it to the model
or user directly.

---

### 4.5 `handle_check_providers` — Enrich With First-Run Fields

**Location:** line 9718–9778.

**Change:** After the existing quota enrichment loop, append first-run metadata
to the return value. This uses the full three-signal predicate because
`_ensure_init()` has already run (it is called at line 9728).

```python
def handle_check_providers(_args: dict) -> dict:
    """Return compact, secret-safe provider diagnostics augmented with usage windows."""
    registry = _get_registry_with_config()
    base = registry.to_compact_dict()

    try:
        config, db, router, planner, orchestrator = _ensure_init()
    except Exception:
        return base

    try:
        quota_service = ProviderQuotaService(db)
        checker = ProviderUsageChecker(quota_service)
    except Exception:
        return base

    # ... existing quota loop (lines 9738–9776, unchanged) ...

    # ↓ ADD AFTER the existing quota loop (after line 9776 `prov["usage_windows"] = windows_list`):

    # First-run / setup-status enrichment
    _config_present = CONFIG_YAML.exists()
    _marker_present = _INITIALIZED_MARKER.exists()
    _routable_count = sum(
        1 for p in base.get("providers", []) if p.get("routeable") is True
    )
    _is_first_run = _detect_first_run()  # uses cached result after first call

    base["config_present"] = _config_present
    base["config_mode"] = "operator" if _config_present else "defaults"
    base["routing_policy"] = getattr(
        getattr(config, "routing_policy", None), "mode", "advisory"
    ) or "advisory"
    base["routable_provider_count"] = _routable_count
    base["setup_required"] = _is_first_run
    if _is_first_run:
        base["setup_hint"] = (
            "Threnody is running on defaults with no routable host AI CLIs detected. "
            "Run `threnody settings` to configure providers, or run `./install.sh` "
            "for the full interactive setup including shell aliases and routing hooks."
        )
        base["install_command"] = "threnody settings"
    else:
        base["setup_hint"] = (
            "Running on defaults (advisory routing, no provider preferences). "
            "Optional: run `threnody settings` to configure routing/provider preferences."
            if not _config_present
            else None
        )
        base["install_command"] = None

    return base
```

---

## 5. Response Formats

### 5.1 `initialize` Response (first-run detected)

```json
{
  "protocolVersion": "2024-11-05",
  "capabilities": { "tools": { "listChanged": false } },
  "serverInfo": { "name": "Threnody", "version": "0.2.0-alpha.1" },
  "instructions": "Threnody is running with default settings — no config.yaml was found and first-run setup has not completed. Routing is in advisory mode; all detected host AI CLIs are enabled with no provider preferences. This is functional but unoptimized. To complete setup, run: `threnody settings` (or re-run `./install.sh` for the full wizard). If you installed via `claude mcp add` or a plugin, no install.sh step is needed — `threnody settings` is sufficient. To check what is currently configured, call the `check_providers` tool."
}
```

### 5.2 `initialize` Response (configured or marker present)

```json
{
  "protocolVersion": "2024-11-05",
  "capabilities": { "tools": { "listChanged": false } },
  "serverInfo": { "name": "Threnody", "version": "0.2.0-alpha.1" }
}
```

No `instructions` key present. Clients that check for the key will see absence.

### 5.3 `check_providers` Response (first-run, unconfigured)

```json
{
  "providers": [...],
  "config_present": false,
  "config_mode": "defaults",
  "routing_policy": "advisory",
  "routable_provider_count": 0,
  "setup_required": true,
  "setup_hint": "Threnody is running on defaults with no routable host AI CLIs detected. Run `threnody settings` to configure providers, or run `./install.sh` for the full interactive setup including shell aliases and routing hooks.",
  "install_command": "threnody settings"
}
```

### 5.4 `check_providers` Response (configured)

```json
{
  "providers": [...],
  "config_present": true,
  "config_mode": "operator",
  "routing_policy": "advisory",
  "routable_provider_count": 2,
  "setup_required": false,
  "setup_hint": null,
  "install_command": null
}
```

### 5.5 `check_providers` Response (defaults but providers present)

```json
{
  "providers": [...],
  "config_present": false,
  "config_mode": "defaults",
  "routing_policy": "advisory",
  "routable_provider_count": 1,
  "setup_required": false,
  "setup_hint": "Running on defaults (advisory routing, no provider preferences). Optional: run `threnody settings` to configure routing/provider preferences.",
  "install_command": null
}
```

This case (`setup_required: false` with `config_mode: "defaults"`) is the normal
plugin-installed-and-working state — one provider found, wizard not run, marker
written after init.

---

## 6. Tool Gating — Which Tools Are Blocked

**Decision: No hard blocking.** All tools remain callable regardless of
first-run state. The design doc (`plugin-design.md §5`) explicitly states the
pattern is "detect-and-guide, non-blocking" — the lazy, default-safe runtime
is a feature, not a bug.

The `_ALWAYS_ALLOWED_TOOLS` frozenset (§4.1) is used only for documentation
and potential soft-warning logic; it does not gate tool execution.

### Rationale

`handle_route_task` and `handle_plan_task` both call `_ensure_init()` internally.
If init fails (e.g., truly zero CLIs found and a hard exception is raised), the
existing `-32603` error path handles it. If init succeeds — even on defaults —
the tools work. A hard gate at the dispatch layer would be a regression from the
current robustness posture.

The `_ALWAYS_ALLOWED_TOOLS` set documents the tools that **must never** be
gated even if someone adds soft warnings later:
- `check_providers` — the setup diagnostic tool itself
- `inspect_status` — operator visibility
- `inspect_task` / `tune_show` — read-only operator tools
- `routing_exception_list` — routing query
- `list_subtasks` — active task monitor

If a future soft-warning decorator is added to certain tools (e.g., prepending
a warning to `route_task` output when `setup_required` is true), it must skip
tools in `_ALWAYS_ALLOWED_TOOLS` and must not raise or block.

---

## 7. Marker File vs. Config File Check

**Use `.threnody-initialized` as the primary sentinel, with `config.yaml`
as a secondary signal.**

| Signal | Path | Meaning |
|--------|------|---------|
| `config.yaml` | `BASE_DIR / "config.yaml"` | User/wizard has written operator config |
| `.threnody-initialized` | `BASE_DIR / ".threnody-initialized"` | Server has completed at least one successful init cycle on this machine |

### Why not just check `config.yaml`?

`TGsConfig.defaults()` explicitly handles the missing-config case and returns
a valid config. So `config.yaml` missing is normal, not pathological. The
wizard is optional; many users will use defaults forever. A user who ran
`install.sh --plugin-mode` (which may seed a minimal config file) would suppress
the hint via (A). But a user who runs `uvx threnody-mcp` cold — with no
`install.sh` — will have neither file until init completes.

The marker file is the right signal because:
1. It represents machine-level state (init succeeded), not configuration state.
2. It is written automatically after the first successful `_ensure_init()` — no
   user action required to suppress the hint after first use.
3. It allows the signal to be cleared by deleting the marker (useful for
   testing, or if a user wants to reset the hint intentionally).
4. It does not conflate "using defaults" with "broken" — a configured user still
   gets the marker written.

### Why not a DB flag?

The DB is created inside `_ensure_init()`. The `initialize` handler (§4.4)
intentionally runs the fast pre-check before any `_ensure_init()` call to avoid
blocking the `initialize` response. The marker file can be stat-checked in ~1 µs
with no DB open.

---

## 8. Interaction With `TGsConfig.defaults()`

`TGsConfig.defaults()` (line 1398–1402) returns a `TGsConfig` instance with:
- `providers` = empty dict (no disabled, no allowlists, no preferences)
- `routing_policy.mode` = "default" → resolves to "advisory"
- All thresholds clamped to defaults

This is a **usable config**. The first-run predicate therefore cannot use
"config returns defaults" as its criterion — that is true for every missing
config, including normal plugin installs that have been running for months.

The connection between defaults and first-run is:
- `defaults()` is what `from_yaml()` returns when `config.yaml` is absent.
- `config.yaml` absent is signal (A) in the predicate.
- But (A) alone is insufficient — signal (B) (marker absent) distinguishes a
  brand-new cold start from a healthy system that simply never needed the wizard.
- Signal (C) (zero routable providers) is the confirmation that something is
  actually wrong, not just uncustomized.

In practice, a typical plugin installation flow resolves all three signals:
1. `uvx threnody-mcp` starts. (A) true, (B) true.
2. `_ensure_init()` runs. `_get_registry_with_config()` discovers `claude`
   binary. `_count_routable_providers()` returns 1. (C) false.
3. Full predicate = false. No hint shown. Marker written. System works.

The hint only fires when no CLI binary is found at all — the truly broken case.

---

## 9. Summary of Changes by Line Number

| Change | Location | Lines |
|--------|----------|-------|
| Add `_INITIALIZED_MARKER`, `_ALWAYS_ALLOWED_TOOLS`, `_first_run_state`, `_first_run_lock` globals | After line 199 | Insert ~6 lines |
| Add `_count_routable_providers()`, `_detect_first_run()`, `_write_initialized_marker()` | After line 688 (end of `_config_file_signature()`) | Insert ~55 lines |
| Call `_write_initialized_marker()` at end of `needs_full_init` block | Line 671 (after `_shutdown_registered = True`) | Insert ~3 lines |
| Enrich `initialize` response with `instructions` when pre-check fires | Lines 10453–10462 | Replace send_response block (~12 lines) |
| Enrich `handle_check_providers` return with first-run fields | Lines 9776–9778 (after quota loop) | Insert ~25 lines |

Total: approximately 100 lines added, 0 lines deleted from existing logic.

---

## 10. Testing Considerations

Tests should cover four matrix cells:

| `config.yaml` | Marker | Routable providers | Expected `setup_required` |
|---------------|--------|-------------------|--------------------------|
| absent | absent | 0 | `true` |
| absent | absent | ≥1 | `false` |
| absent | present | any | `false` |
| present | any | any | `false` |

Test the `initialize` handler pre-check separately (only uses signals A+B, no
provider count). Assert `instructions` key present/absent in the response dict.

Use `tempfile.TemporaryDirectory()` for the `BASE_DIR` override in tests, or
monkeypatch `_INITIALIZED_MARKER` and `CONFIG_YAML` directly.

The `_first_run_state` cache must be reset between test cases:
```python
import mcp_server
mcp_server._first_run_state = None
```

`THRENODY_TEST_MODE=1` (set by conftest autouse fixture) isolates provider
discovery — `_count_routable_providers()` will return 0 in test mode unless the
fixture explicitly seeds a routable provider, which makes the zero-provider case
easy to test without requiring an actual CLI binary.
