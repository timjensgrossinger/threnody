# Provider Compatibility

This table describes the shipped provider contract and the current hermetic
test coverage. It does not claim that every provider subscription is available
on every machine.

| Provider | Default Routeability | Hermetic Coverage | Live Smoke Needed |
|---|---|---|---|
| GitHub Copilot | low, medium, high | detection, command construction, isolated env, execution parsing, auth failure, generic timeout path | passed 2026-06-08 |
| Claude Code | low, medium, high | command construction, effort mapping, execution parsing, timeout, auth-status preflight, caller opt-out/preference | passed 2026-06-08 |
| Gemini CLI | low, medium, high | command construction, caller detection, entrypoint adapter, model tiering | yes |
| OpenAI Codex | low, medium, high | noninteractive command contract, output cleanup, API-key detection, login probe, quota parsing, timeout, adapter metadata | yes |
| Cursor | low, medium, high | entrypoint adapter, host instructions, MCP registration shape, caller detection, routing policy | yes |
| JetBrains Junie | medium-only by default | entrypoint adapter, medium-only model projection, caller detection, instructions, routeability diagnostics | yes |
| OpenCode | low-only by default | command construction, entrypoint adapter, caller detection, low-only projection after catalog refresh | yes |
| Aider | low, medium, high | API-key detection, missing binary, command builder, model discovery fallback, result extraction | yes |
| Amazon Q/Kiro | low, medium, high | binary selection, auth fallback/failure metadata, static models, result extraction | yes |
| Mistral Vibe | low, medium, high | command execution parsing, timeout, active model behavior, workdir cleanup, spillover routing | yes |
| Blackbox AI | low, medium, high | registry inclusion, detection and command path coverage, generic execution parsing | yes |
| Windsurf | detect-only | detect reason, non-routeable status, skipped by execution | no execution by design |
| Ollama loopback | local endpoint | metadata tiering, loopback discovery, execution fallback | optional |
| OpenAI-compatible endpoint | configured endpoint | TLS rules, credential handling, local/network scope validation, execution fallback | optional |

## Compatibility Rules

- Detection reports readiness reason separately from binary presence.
- Auth-aware installer scans report `auth_unknown` unless verification probes
  are explicitly run.
- Unavailable or deprecated catalog models fall back only within the same
  effective routeable tier.
- Operator tier pins can opt into other tiers explicitly.
- Provider output parsing must not write malformed provider responses to disk.

## Current Verification

Run:

```bash
THRENODY_TEST_MODE=1 python3 -m pytest \
  tests/test_discovery.py \
  tests/test_provider_execution.py \
  tests/test_provider_quota.py \
  tests/test_codex_contract.py \
  tests/test_model_catalog.py -q
```

The required two-provider live gate passed through the full
`handle_execute_subtask()` path with GitHub Copilot and Claude Code on
2026-06-08. Other provider live rows remain optional compatibility expansion
work for the alpha.

Current hermetic result: **298 passed, 2 live-smoke skips** on 2026-06-08.
