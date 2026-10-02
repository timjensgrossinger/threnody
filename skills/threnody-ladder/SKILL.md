---
name: threnody-ladder
description: >-
  Grade which model tier of the current host (Claude Code haiku/sonnet/opus,
  Codex gpt models, ...) can actually do which kind of task, using the graded
  task ladder. Fills the minimum-passing-tier tables in `threnody quality`.
  Spends real tokens. Use when asked to benchmark, grade, or calibrate model
  tiers, or "which model is good at what".
---

# Threnody ladder (host-native)

The ladder runs graded coding cases (L0-L6) at each tier and checks the output
with a deterministic grader. On router-only hosts such as Claude Code Threnody
must not spawn the host CLI, so you spawn the agents and Threnody grades what
they return.

This spends real tokens: one agent per (case x tier).

## Process

1. **State the cost.** Compute items = cases x tiers (13 cases x 3 tiers = 39 by
   default). Tell the user and ask to confirm, or to narrow with `levels`,
   `case_ids` or `tiers`. Do not spawn before they confirm.

2. Call **`ladder_plan`** (MCP: Threnody) with optional `tiers`, `levels`,
   `case_ids`. It returns `sweep_id`, `caller`, `count` and `items[]`, each with
   `case_id`, `level`, `kind`, `tier`, `model`, `target_file`, `prompt`.

3. **Spawn one Agent per item** with `model` = `item.model`,
   `subagent_type` = `general-purpose` (or `threnody-<tier>`) and
   `prompt` = `item.prompt`, in parallel batches of at most 8. Agents must not
   write or edit files; they reply with only the complete file contents.

4. For each reply call **`ladder_grade`** with `case_id`, `tier`, `model`,
   `sweep_id` and `content` (the agent's raw reply). Keep the returned
   `passed` flag. A missing or empty reply is graded `content=""` (a failure).

5. **Summarise.** Run `threnody quality --since 1d` (or `inspect_quality`) and
   report the minimum passing tier per level and per task kind, plus any
   case that failed at every tier.

## Notes

- Do not edit `target_file` yourself and do not fix an agent's output before
  grading; the verdict is the agent's alone.
- On hosts that are not router-only, `threnody ladder run` does this directly.
