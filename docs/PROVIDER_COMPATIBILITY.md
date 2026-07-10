# Provider Compatibility

This table describes the shipped provider registry and current hermetic adapter
coverage. It does not claim that a provider subscription, login, model catalog,
or entitlement is available on every machine.

| Provider | Registry role / default routeability | Hermetic Coverage | Live Smoke Observed in This Audit |
|---|---|---|---|
| GitHub Copilot (`gh`) | Host; low, medium, high; low cost rank 0 | detection, command construction, isolated env, execution parsing, auth failure, timeout paths | Not run |
| Claude Code (`claude`) | Host and router-only by default; low, medium, high | command construction, effort mapping, execution parsing, timeout, auth-status and caller policy | Not run |
| OpenAI Codex (`codex`) | Host; low, medium, high | noninteractive command contract, output cleanup, API-key/login detection, quota, timeout, metadata | Not run |
| Cursor (`cursor-agent`) | Host; low, medium, high | adapter, instructions, MCP registration shape, caller detection, routing policy | Not run |
| JetBrains Junie (`junie`) | Host; medium-only automatic routing | adapter, medium-only projection, caller detection, instructions, diagnostics | Not run |
| OpenCode (`opencode`) | Host and default utility allowlist; low-only automatic routing | command/entrypoint, caller detection, low-only projection and catalog refresh | Not run |
| Aider (`aider`) | Non-host utility adapter; in the default utility allowlist when delegation is enabled | API-key detection, missing binary, command builder, model fallback, result extraction | Not run |
| Amazon Q/Kiro (`q`/`kiro`) | Non-host adapter; requires explicit utility allowlisting for delegation | binary selection, auth fallback/failure metadata, static models, result extraction | Not run |
| Mistral Vibe (`vibe`) | Non-host adapter; requires explicit utility allowlisting for delegation | command parsing, timeout, model behavior, workdir cleanup, spillover | Not run |
| Blackbox AI (`blackbox`) | Non-host adapter; requires explicit utility allowlisting for delegation | registry inclusion, detection/command path, generic parsing | Not run |
| Windsurf (`windsurf`) | Detect-only; never routeable | detect reason and non-routeable execution skip | Not applicable |
| Ollama loopback | Configured/local HTTP utility endpoint | metadata tiering, discovery, execution fallback | Not run |
| OpenAI-compatible endpoint | Configured HTTP utility endpoint | TLS, credential handling, scope validation, execution fallback | Not run |

## Compatibility Rules

- Detection reports readiness reason separately from binary presence.
- Auth-aware installer scans report `auth_unknown` unless verification probes
  are explicitly run.
- Unavailable or deprecated catalog models fall back only within the same
  effective routeable tier.
- Operator tier pins can opt into other tiers explicitly.
- Provider output parsing must not write malformed provider responses to disk.

## Current Verification

Run the hermetic adapter suite:

```bash
THRENODY_TEST_MODE=1 python3 -m pytest \
  tests/test_discovery.py \
  tests/test_provider_execution.py \
  tests/test_provider_quota.py \
  tests/test_codex_contract.py \
  tests/test_model_catalog.py -q
```

The command above exercises mocked/test-mode and adapter contracts; it does not
authenticate to or invoke provider CLIs. No live provider smoke was run for
this documentation refresh. Live coverage remains an operator/release gate
because it depends on local binaries, credentials, quotas, and entitlements.
