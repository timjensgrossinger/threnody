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

**Tier → model is chosen per agent from raw file size × dimension reasoning-weight** (no LLM call — pure heuristic, keeps fast-start):

| File size (LOC) | edge / types (light) | logic / performance / security (reasoning-heavy) |
|-----------------|----------------------|--------------------------------------------------|
| small `< 230`   | low → haiku          | low → haiku                                      |
| mid `230–600`   | medium → sonnet      | medium → sonnet                                  |
| large `> 600`   | medium → sonnet      | **high → opus**                                  |
| security + risk | —                    | **high → opus** (any size)                        |

Synthesis scales with the run: `high → opus` when ≥12 review agents or any risky
file present, else `medium → sonnet`. Never below medium (dedup/ranking is
reasoning-heavy). This mix — haiku on small files, opus on large reasoning-heavy
ones — is automatic; you do not hardcode a tier.

---

## Fast-start contract

Review swarm paths must return `host_spawn_waves` quickly: target **under 5
seconds** to handoff and **under 30 seconds** to first review-agent spawn. Cheap
review tiers are the default; high tier is reserved for risk/deep review.

Do not block first spawn on optional refinement, consensus, learning
aggregation, or deep planner calls. Run deterministic lint/context collection
only when cheap, then start the first review wave.

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

**Explicit dimension intent.** When the user names the review focus (e.g. "review
for performance"), emit it so the swarm runs *only* those dimensions instead of
the full band set:

```python
execute_swarm(
  task="REVIEW: [dims=performance] <space-separated file paths>",
  topology="dag",
)
```

`[dims=...]` is comma-separated; accepted keys: `performance, security, logic,
types, edge` (aliases `perf, sec, type, null`). Named dimensions are
drop-protected under the agent cap; `security` is still *added* (never evicting a
named dimension) when a file has real risk signals. With no `[dims=...]`, the
band-based default set runs.

**Response is a compact spawn manifest.** For review runs the response carries
`host_spawn_waves` (the lean per-agent spawn list) plus a small `plan_summary` —
the heavy duplicate `plan` and any `workflow_script` are omitted so the host
reads it in one chunk and hits the <20s first-spawn target. Full plan fidelity is
still recorded server-side (`inspect_run_receipt`). Spawn directly from
`host_spawn_waves`; do not expect a full `plan` object.

### 4. Execute host_spawn_waves

On `awaiting_host_execution: true` + `host_spawn_waves`:
- **All agents in a wave → one parallel batch/message before waiting** (never sequential)
- Pass each agent its `prompt`, `model`, and `subagent_type` from the handoff
- Do **not** `Write`/`Edit` any `target_files` — review is read-only
- **Reporting** (see `learning_report_contract.report_mode`): in `batch` mode (default) do **not** call `report_host_wave` per wave — hold each agent's `output_excerpt` in your own context for the synthesis wave, and report once at terminal. In `inline` mode call `report_host_wave` after each wave.

### 5. Synthesis wave

The final wave contains a single synthesis subtask (`tier` auto-scaled: `high` for
≥12 review agents or any risky file, else `medium`). Inject:
- Linter output from step 1
- All `output_excerpt` summaries from prior waves — **all of them**

The synthesis agent deduplicates, ranks by severity → category, and outputs the report.

> Do **not** pre-filter or drop low-severity findings before synthesis to save
> time — that loses findings. Synthesis input lives in your context (batch mode),
> not the swarm payload; latency is addressed by the compact response (step 3) and
> cheaper per-agent tiers, never by trimming findings.

### 6. Terminal report

```python
report_host_swarm_complete(
  swarm_id="<id>",
  outcome="accepted",
  workspace_root="<root>",
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
- Report learning once at terminal (`batch`, default) or after each wave (`inline`); always include `output_excerpt`
- Linter pass before swarm, feed results to synthesis

## Do not

- Call `execute_subtask` for same-host review agents
- Run all waves in sequence via a single agent — fan out each wave in parallel
- Start one same-wave review agent and wait before starting the next
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
