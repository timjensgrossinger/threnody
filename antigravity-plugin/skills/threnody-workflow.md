---
name: threnody-workflow
description: >-
  Run a Threnody swarm with multi-queen consensus and learning THROUGH the Claude
  Code Dynamic Workflow feature, then save the run as a permanent, pre-tuned,
  zero-config /workflow command for coworkers. Use when asked to run a workflow,
  workflow-swarm, tier-aware workflow, or to save/share a reusable multi-agent
  workflow. Claude Code only.
---

# Threnody workflow orchestration

Runs fan-out work as a tier-aware **Dynamic Workflow** (each `agent()` on its
Threnody tier model, not one session model), with multi-queen consensus and
learning — then lets you **save a permanent, documented, zero-config `/<slug>`**
command teammates run with no setup.

This is Claude Code-only. Other hosts use `host_spawn_waves` through
`threnody-swarm`; all shared behavior must work there first.

## Fast-start contract

Workflow-emitting skills must produce a runnable `workflow_script` quickly:
target **under 5 seconds** to handoff and **under 30 seconds** to first worker
spawn. The emitted script must start same-wave workers in a batch, for example
with `parallel([...])`, before waiting on the wave barrier.

Do not block initial workflow emission on optional refinement, consensus,
learning aggregation, or permanent-workflow export. Run those after the first
worker wave has started or after the workflow returns.

For review workflows, use the nested cheap shape: the Workflow script performs
medium-tier orchestration, launches file reviewers in parallel, and returns a
compact synthesis to the main session. Keep high-tier judgment for explicit
deep/security-critical review or validated high/critical findings.

## Prerequisite (claude-code only)

This path needs `routing_policy.shells.claude-code.workflow_emit: true` in
`~/.local/lib/threnody/config.yaml`. If it is off (or the host is not Claude Code),
the response will **not** include `workflow_script` — fall back to **`/threnody-swarm`**.
Requires Claude Code **v2.1.154+** (Workflow tool).

## Workflow

1. `execute_swarm(task, topology="star"|"auto")`.
2. Inspect the response:
   - **`workflow_emit: true` + `workflow_script`** — the tier-aware script. Launch it
     via the **Workflow** tool (paste/run `workflow_script`). It runs in the background,
     keeps intermediate results out of your context, and routes each agent to its model.
     Same-wave agents must be represented as a batch in the script, not as a
     sequential loop.
   - No `workflow_script` — emission is off; use **`/threnody-swarm`** instead.
3. When the workflow returns, call
   **`report_workflow_result(workflow_name, agents)`** with the `agents` array the
   workflow returned (`report_workflow_result` records per-agent learning telemetry).
4. **Consensus:**
   - **Hybrid (default):** the response also has a **`consensus_wave`**. After the
     workflow finishes, spawn each read-only queen in it as a host `Agent` (one parallel
     message), then `report_host_wave(swarm_id, wave, workspace_root, agents=[...])` with
     each queen's JSON verdict as `output_excerpt`. If the response returns
     `consensus_followup`, spawn the single judge Agent it provides and report again.
   - **Opt-in (`consensus_in_workflow`):** queens run **inside** the workflow; pass the
     workflow's returned `consensus` array to `report_workflow_result(..., consensus=[...])`.
     If it returns `consensus_followup`, spawn one judge Agent and re-call with
     `consensus` set to just the selected queen's proposal.
   - For ordinary reviews, prefer a targeted verifier pass for synthesized
     `HIGH`/`CRITICAL` findings instead of running consensus over every file.
5. **Save a permanent workflow (the payoff):** once a shape recurs across successful runs,
   `report_workflow_result` returns `workflow_draft.enqueued: true`. Then:
   - `approval_queue_approve(<queue_id>, operator=<you>)` to approve the learned workflow.
   - Export it with tuning + documentation:
     ```bash
     python3 -c "import shared.workflow_export as wx, shared.db as d; \
       wx.export_workflow(<approved_draft_dict>, project_path='.', db=d.Database(), tune=True)"
     ```
     This re-tunes per-tier models from recorded outcomes ("what model did task X well"),
     writes a documented header (tier→model map, persona roster + roles, related learned
     agents), and saves `.claude/workflows/<slug>.js`.
   - **Commit `.claude/workflows/<slug>.js` to the repo** → teammates run `/<slug>` with
     zero config.

## Must

- Launch the **`workflow_script`** via the Workflow tool — do not hand-spawn its worker
  agents as host Tasks (that defeats the background + tier-routing benefit).
- Launch the script immediately once returned; refinement, consensus, learning,
  and export are post-first-spawn work.
- Always `report_workflow_result` after the run so telemetry + shape learning happen.
- Hybrid consensus queens are **read-only** — never let them write files.
- Saving a permanent workflow is **approval-gated** — approve the draft before export.

## Do not

- Use this skill when `workflow_emit` is off or the host is not Claude Code → use
  **`/threnody-swarm`** (host_spawn_waves).
- Hand-edit a saved `.claude/workflows/*.js` — re-run and re-export to change it.
- Call `execute_subtask` for same-host agents.
- Treat Workflow support as portable across hosts.

## Relationship to other skills

- **`/threnody-swarm`** — host_spawn_waves swarm (all hosts; no Workflow tool).
- **`/threnody-swarm-review`** — review fanout (works with this skill's emit path too).
- **`/threnody-routing`** — routing + host-native contract basics.
