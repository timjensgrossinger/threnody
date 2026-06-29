---
name: threnody-fast-review
description: >-
  Fast broad code review swarm: one read-only host agent per file, then one
  synthesis agent. Use when asked for a fast swarm review, one-agent-per-file
  review, broad review sweep, ordinary swarm review, or when reviewing many
  files where coverage and speed matter more than per-dimension depth.
---

# Threnody fast review

## Purpose

Use this when a review spans many files and should not collapse into a tiny
number of agents. This mode trades the deeper `file × dimension` review swarm
for faster file-level parallelism:

```text
wave 1: one read-only review agent per file
wave 2: one synthesis agent
```

This is the **default for broad swarm review**. Use `threnody-swarm-review` only
when the user explicitly asks for deep review, security-critical audit,
threat-modeling, or a named specialist dimension.

Tiering is medium by default. Ordinary risk words such as auth, token, or secret
add security attention inside the file reviewer but do not automatically force
high tier. High tier is reserved for explicit deep/security-critical wording,
concrete exploit primitives, or large/dense files.

The global `swarm.max_agents` cap still applies. If the requested count is
clamped, report the `requested_vs_effective_agent_count` field and tell the
operator which files were not reviewed.

## Workflow

### 0. Resolve files

Use file paths from the user's message. If none are provided, resolve changed
source files:

```bash
git diff --name-only HEAD
git diff --name-only --cached
```

Filter out generated files, lock files, binary assets, Markdown docs, and large
schemas unless the user explicitly asks to review them.

### 1. Run cheap deterministic checks

Run relevant linters or type checks first when available. Keep failures as
context for synthesis; do not stop the swarm solely because a linter found
findings.

### 2. Call execute_swarm

Use the `FAST_REVIEW:` sentinel and request one agent per file plus one synthesis
agent:

```python
execute_swarm(
  task="FAST_REVIEW: <space-separated file paths>",
  topology="dag",
  max_agents=<number_of_files + 1>,
)
```

The sentinel activates fast file-level review in the heuristic planner.

### 3. Execute host_spawn_waves

On `awaiting_host_execution: true` + `host_spawn_waves`:

- Spawn every wave-1 agent in parallel.
- Pass each agent its `prompt`, `model`, `tier`, `subagent_type`, and target file.
- Keep review read-only. Never `Write` or `Edit` reviewed files.
- Run the synthesis wave after all file agents finish.

### 4. Report

Use `report_host_swarm_complete` once at terminal in batch mode, or
`report_host_wave` per wave in inline mode, following `learning_report_contract`.

Before final reporting, run a cheap targeted verifier only for synthesized
`HIGH` or `CRITICAL` findings. Do not run a second full review swarm; verify the
specific finding with file:line grounding and mark it `valid`, `false_positive`,
or `needs_more_evidence`.

## Must

- Use `FAST_REVIEW:` exactly.
- Set `max_agents` to `number_of_files + 1` unless the user asks for a lower cap.
- Report clamping if effective agents are lower than requested agents.
- Preserve read-only behavior.

## Do not

- Use this for a deep security-critical audit where per-dimension specialists
  are needed. Use `threnody-swarm-review` instead.
- Collapse a large review into one general agent unless the user explicitly asks.
- Escalate every file to high tier just because it mentions auth, token, or
  credentials.
