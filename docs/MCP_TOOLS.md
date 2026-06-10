# MCP Tools Reference

Threnody exposes **43 public MCP tools** over JSON-RPC/stdio. Tests enforce that every published schema has a callable handler.

## Routing & Planning

| Tool | Description |
|---|---|
| `plan_task(task)` | Planner-based decomposition for multi-file work |
| `decompose_task(task)` | Alias for `plan_task`; preferred entry point for multi-concern tasks |
| `fleet_plan(task)` | Like decompose but returns ready-to-run parallel agent commands |
| `route_task(task)` | Classify complexity → returns `{tier, model, score, reason}` |
| `execute_subtask(prompt, tier, target_file?, effort?)` | Route prompt to cheapest CLI provider; optionally tune provider-native reasoning effort |
| `execute_swarm(task, topology?, max_agents?)` | Plan and start a bounded multi-agent swarm |
| `apply_preview(preview_token, approve)` | Approve/deny file writes outside workspace |
| `validate_routing_guard(...)` | Check whether a host edit/write is allowed by the active routing policy |

Optional `effort` on `execute_subtask` is a provider-level reasoning hint (e.g. `"low"`, `"high"`, `"max"`, `"xhigh"`). Honored by Claude Code, Codex, and Cursor only; unsupported providers reject explicit overrides.

## Cache & Task Inspection

| Tool | Description |
|---|---|
| `cache_get(task)` | Look up cached result for a task |
| `cache_put(task, result, model)` | Store result in cache |
| `cache_stats()` | Cache hit rates, entry counts, DB size |
| `inspect_task(task_id)` | Provider/model/tier telemetry and fallback flags |
| `list_subtasks()` | Active and recently completed `execute_subtask` calls |
| `stop_subtask(task_id)` | Pause a running process or cancel a task still starting |
| `resume_subtask(task_id)` | Resume a stopped provider process |
| `resume_swarm_inspect(failed_swarm_id)` | List checkpoints for a failed swarm |
| `resume_swarm_confirm(failed_swarm_id, checkpoint_index)` | Resume from a selected checkpoint |

## Provider & Project Inspection

| Tool | Description |
|---|---|
| `check_providers()` | List detected CLIs, models, cost rankings, availability |
| `inspect_status(project_id)` | Project readiness, limits, fanout state |
| `approval_queue_list(project_id)` | Pending approval queue |
| `tune_show(project_id)` | Current persisted operator tuning values |
| `inspect_write_audit(limit?)` | Recent outside-workspace write audit events |
| `routing_exception_list()` | Persisted routing bypass rules |
| `routing_exception_add(exception_type, pattern)` | Add a scoped bypass rule |
| `routing_exception_remove(exception_type, pattern)` | Remove a bypass rule |

## Learning Surfaces

| Tool | Description |
|---|---|
| `learning_agent_summary()` | Summarize learned agents by status, lane, and approval state |
| `learning_pattern_health(project_id?)` | Pattern counts, mature drafts, queue depth, active agents |
| `learning_audit_log(agent_id?, limit?)` | Filtered audit trail (secrets redacted) |
| `learning_outcome_stats()` | Recent outcome distribution and feedback coverage |
| `record_outcome(task_id, outcome, operator_id?, note?)` | Persist an explicit routed-task outcome |

## Approval and Memory

- Approval: `agent_queue_list`, `agent_queue_approve`, `agent_queue_reject`, `agent_queue_merge`, and `approval_queue_*` aliases.
- Memory: `memory_list`, `memory_get`, `memory_set`, `memory_delete`.

## Deprecated remote tools (unsupported)

`remote_dispatch` and `remote_job_status` remain in the tree for compatibility
but are **deprecated and unsupported**. Use local MCP stdio only. See
[docs/LEGAL.md](LEGAL.md).

Operator tuning: `threnody tune set|reset` via the shell wrapper.

## `execute_subtask` example

```
execute_subtask(
  prompt="Create a config.py with default constants...",
  tier="low",
  target_file="/path/to/config.py",
  prefer_free=true,
  effort="high"
)
→ Routes to cheapest provider (e.g. gpt-5-mini or gemini-flash-lite)
→ Returns: {result, provider, model, tier, fallback_used, file_written, lines_written}
```

See [CONFIGURATION.md](CONFIGURATION.md) for spillover and effort defaults.
