---
name: threnody-swarm-review
description: >-
  Run a complexity-gated review swarm: fans out one agent per (file × dimension)
  — logic, security, edge/null, types, performance — then synthesizes findings
  into a ranked report. Model selection per agent routed through Threnody.
  Use when asked to "swarm review", "review with swarm", or "deep parallel review"
  of one or more files. Read-only — never writes or edits target files.
---

# Threnody swarm review

## Overview

Complexity gating prevents token blowout:

| Band | LOC | Dimensions run |
|------|-----|----------------|
| trivial | < 50 | logic, edge |
| moderate | 50–200 | logic, edge, types |
| complex | > 200 | logic, edge, types, security, performance |

Security is always added when the file contains risk signals (SQL, exec, auth, tokens, etc.).
Tier → model: trivial → low (haiku), moderate → medium (sonnet), security+complex → high (opus).

---

## Workflow

### 0. Resolve files

Use file paths from the user's message. If none provided, resolve from git:

```bash
git diff --name-only HEAD
git diff --name-only --cached
```

Filter to source files only (exclude generated files, lock files, `*.md`, `.json` schemas).

### 1. Run deterministic linters (no LLM agent)

For Python files:
```bash
ruff check <files> --output-format=concise 2>&1 || true
```

For JS/TS files:
```bash
npx eslint <files> --format=compact 2>&1 || true
```

Capture lint output — it will be injected into the synthesis agent's context.

### 2. Print transparency table

Before spawning, print:

```
File              | Dims | Tier   | Model
------------------|------|--------|-------
src/auth.py       | 5    | high   | opus
src/utils.py      | 2    | low    | haiku
```

### 3. Call execute_swarm

```python
execute_swarm(
  task="REVIEW: <space-separated file paths>",
  topology="dag",
)
```

The `REVIEW:` sentinel activates per-file × dimension fanout in the heuristic planner.
Files listed after `REVIEW:` are extracted automatically.

### 4. Execute host_spawn_waves

On `awaiting_host_execution: true` + `host_spawn_waves`:
- **All agents in a wave → one parallel message** (never sequential)
- Pass each agent its `prompt`, `model`, and `subagent_type` from the handoff
- Do **not** `Write`/`Edit` any `target_files` — review is read-only
- After each wave: `report_host_wave(swarm_id, wave, workspace_root, agents[...])`
  with `output_excerpt` = one-sentence finding summary per agent

### 5. Synthesis wave

The final wave contains a single synthesis subtask (`tier: high`). Inject:
- Linter output from step 1
- All `output_excerpt` summaries from prior waves

The synthesis agent deduplicates, ranks by severity → category, and outputs the report.

### 6. Terminal report

```python
report_host_wave(
  swarm_id="<id>",
  wave=<n>,
  workspace_root="<root>",
  terminal=True,
  outcome="accepted",
  agents=[{
    "task_id": "...",
    "spawn_id": "...",
    "success": True,
    "touched_files": [],
    "output_excerpt": "<ranked findings summary>",
  }],
)
```

Output the full ranked findings report to the user.

---

## Must

- `REVIEW:` sentinel in the task string — required for fanout to activate
- One host `Agent` per entry in `host_spawn_waves[].agents`
- Never `Write`/`Edit` target files — read-only context only
- Always call `report_host_wave` after each wave with `output_excerpt`
- Linter pass before swarm, feed results to synthesis

## Do not

- Call `execute_subtask` for same-host review agents
- Run all waves in sequence via a single agent — fan out each wave in parallel
- Spawn review agents for generated files, lock files, or binary assets
- Skip the synthesis wave — it dedupes + ranks all dimension findings

---

## Report format

The synthesis agent produces:

```
## Summary
N critical, N high, N medium, N low issues across N files.

## Findings

⚠️ [SEVERITY] category — file:line — description [(CWE-XXX)]
...
```

Severity: critical > high > medium > low
Category priority: security > logic > edge > types > performance

---

## Related skills

- `threnody-swarm` — general swarm (read+write)
- `code-review-deep` — sequential three-stage review (find → verify → rank)
- `secreview` — security review with auto-fix
