---
name: threnody-subtasks
description: >-
  Monitor and control Threnody utility execute_subtask calls via list_subtasks,
  stop_subtask, and resume_subtask. Use when tracking delegated utility work
  (OpenCode, Aider, local endpoints) — not host-native swarms.
---

# Threnody subtask monitoring

`list_subtasks` tracks **`execute_subtask`** utility delegation only.

Host-native swarms and `host_spawn_waves` do **not** appear here — track those
by wave completion in the host shell.

## When to use

- User enabled `providers.delegation_utilities_enabled: true`
- Work was delegated via `execute_subtask` to OpenCode, Aider, or a local endpoint
- Need to pause/resume a running utility subprocess

## Process

1. Call **`list_subtasks`** (MCP: Threnody).

2. Format **Running Active** (`active_count` total):

For each group in `active_groups`:

Parallel wave (`parallel: true`):

```
WAVE: wave-1  (3 running in parallel)
  - abc123  [low]  gpt-5-mini    12.3s  -> config.py
  - def456  [low]  gpt-5-mini    12.1s  -> models.py
```

Single task:

```
abc123  [med]  claude-sonnet-4.6   4.2s  -> main.py
```

Each row: `task_id` (first 6 chars), `[tier]`, model or `resolving`, elapsed seconds, `target_file` or prompt excerpt (60 chars).

If `active_count == 0`: *No subtasks currently running.*

3. Format **Recently Completed** (last 10):

```
abc123  DONE   [low]  gpt-5-mini / Aider  28.1s  -> config.py
def456  FAILED [low]  unknown             31.0s  -> models.py
```

Group consecutive entries with the same `wave_id` under a WAVE label.

4. Footer: `X active / Y recent this session`

5. Tip when idle:

```
For live monitoring, run `threnody-watch` in another terminal tab.
```

## Control

- **`stop_subtask(task_id)`** — pause running utility subprocess
- **`resume_subtask(task_id)`** — resume paused subprocess

## Canonical location

Project skill: `.cursor/skills/threnody-subtasks/` in the Threnody repo.
Supersedes the personal `router-tasks` skill (TGs-router naming).
