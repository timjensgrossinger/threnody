# MCP Tools Reference

Threnody exposes **43 public MCP tools** over JSON-RPC/stdio. Tests enforce that every published schema has a callable handler.

Tools are grouped by role: **coordination** (plan and route), **delegation** (optional subprocess to utility backends only), **learning**, **memory**, and **operator** surfaces.

## Coordination

Plan, classify, and orchestrate work. Prefer host-native execution using
`route_task` → `host_spawn` / `host_spawn_waves` before optional utility delegation.

| Tool | Description |
|---|---|
| `route_task(task)` | Classify complexity → `{tier, model, execution_hint, host_spawn?}`; includes `host_native_model`, `host_native_method`, and `mode: host_native \| delegate` |
| `plan_task(task)` | Planner-based decomposition; returns `host_spawn_waves` for host execution |
| `decompose_task(task)` | Alias for `plan_task`; preferred entry point for multi-concern tasks |
| `fleet_plan(task)` | Like decompose but returns fleet waves with embedded `host_spawn` per agent |
| `execute_swarm(task, topology?, max_agents?)` | Plan swarm; default `host_native` returns `host_spawn_waves` without subprocess fanout |
| `validate_routing_guard(...)` | Check whether a host edit/write is allowed by the active routing policy |
| `apply_preview(preview_token, approve)` | Approve/deny file writes outside workspace |

### Normal orchestration (`plan_task` / `decompose_task`)

1. Call `decompose_task(task)` (preferred) or `plan_task(task)`.
2. Read `host_spawn_waves` — ordered waves of host `Task`/`Agent` spawn payloads.
3. Execute wave 1, then wave 2, etc.; agents within a wave may run in parallel.
4. Do not use `execute_subtask` for same-host work.

`fleet_plan(task)` returns the same plan plus ready-made fleet command strings per wave.

See project skill `.cursor/skills/threnody-task/SKILL.md`.

### `execute_swarm`

Default (**host-native**): returns `awaiting_host_execution: true`, `swarm_id`,
`host_spawn_waves`, and cost estimate — no subprocess fanout. Execute each wave
via the host Agent/Task tool.

| Parameter | Notes |
|-----------|-------|
| `topology` | `auto`, `dag`, `hierarchical`, or `star` |
| `max_agents` | Cap fanout (see `swarm.max_agents` in config) |
| `budget_limit` | Triggers `preview_token` confirmation when estimate exceeds limit |

**Delegate mode** (`swarm.host_execution_mode: delegate`): background orchestrator
subprocess with coordinator rounds on star topology. Resume via
`resume_swarm_inspect` / `resume_swarm_confirm`.

Full-stack parallel frontend/backend/API: use contract-first DAG waves — see
`.cursor/skills/threnody-fullstack/SKILL.md` and [ARCHITECTURE.md](ARCHITECTURE.md).

## Delegation

Optional subprocess routing to **utility backends only** when
`providers.delegation_utilities_enabled` is true (OpenCode, Aider, local loopback
endpoints). Threnody never subprocesses to another host CLI (Copilot, Codex,
Cursor, Junie, Claude Code). Same-host MCP shells receive `HostNativeRequired`
for same-host targets.

| Tool | Description |
|---|---|
| `execute_subtask(prompt, tier, provider_id?, target_file?, effort?)` | Utility delegation only when opt-in enabled; host CLI targets are hard-rejected |

Optional `effort` is a provider-level reasoning hint (e.g. `"low"`, `"high"`, `"max"`, `"xhigh"`). Honored by supported utility backends when explicitly delegated; unsupported providers reject explicit overrides.

### `execute_subtask` example

```
execute_subtask(
  prompt="Create a config.py with default constants...",
  tier="low",
  target_file="/path/to/config.py",
  provider_id="aider",
  effort="high"
)
→ Requires providers.delegation_utilities_enabled: true
→ Delegates to a utility backend (e.g. opencode, aider, ollama/local endpoint)
→ Returns: {result, provider, model, tier, fallback_used, file_written, lines_written}
```

See [config.example.yaml](../config.example.yaml) and [INSTRUCTIONS.md](../INSTRUCTIONS.md) for `delegation_utilities_enabled`, `delegation_utilities`, `routing_policy`, `execute_subtask_guard_strict`, `low_tier_execute_subtask`, effort defaults, and router-only overrides.

## Learning

| Tool | Description |
|---|---|
| `learning_agent_summary()` | Summarize learned agents by status, lane, and approval state |
| `learning_pattern_health(project_id?)` | Pattern counts, mature drafts, queue depth, active agents |
| `learning_audit_log(agent_id?, limit?)` | Filtered audit trail (secrets redacted) |
| `learning_outcome_stats()` | Recent outcome distribution and feedback coverage |
| `record_outcome(task_id, outcome, operator_id?, note?)` | Persist an explicit routed-task outcome |

Approval workflow: `agent_queue_list`, `agent_queue_approve`, `agent_queue_reject`, `agent_queue_merge`, and `approval_queue_*` aliases.

## Memory

| Tool | Description |
|---|---|
| `memory_list` | List memory entries by scope |
| `memory_get` | Read a memory entry |
| `memory_set` | Store a memory entry |
| `memory_delete` | Delete a memory entry |
| `memory_search(query, scope?, project_id?, limit?)` | FTS5 search across memory values (no embeddings) |

## Cache and task inspection

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

## Operator

| Tool | Description |
|---|---|
| `inspect_spend(since?)` | Aggregated spend/savings from delegated subtasks; includes `usage_state` when windows configured |
| `check_providers()` | List detected CLIs, models, router-only vs delegation flags |
| `inspect_status(project_id)` | Project readiness, limits, fanout state |
| `approval_queue_list(project_id)` | Pending approval queue |
| `tune_show(project_id)` | Current persisted operator tuning values |
| `inspect_write_audit(limit?)` | Recent outside-workspace write audit events |
| `routing_exception_list()` | Persisted routing bypass rules |
| `routing_exception_add(exception_type, pattern)` | Add a scoped bypass rule |
| `routing_exception_remove(exception_type, pattern)` | Remove a bypass rule |

Shell wrapper: `threnody tune set|reset`, `threnody inspect`, `threnody doctor`.
