# Ruflo head-to-head

Ruflo and Threnody are both agent meta-harnesses, but they should not converge
on the same shape. Ruflo's public README positions it as a broad platform for
Claude Code and Codex with large agent catalogs, plugins, memory, federation,
and hosted UI surfaces. Threnody's best wedge is narrower and more operator
controlled: local MCP coordination, host-native execution, explicit receipts,
approval-gated learning, and replayable workflows that do not require a fresh
planner call every time.

Sources checked for this positioning:

- Ruflo README: <https://github.com/ruvnet/ruflo>
- Ruflo STATUS: <https://raw.githubusercontent.com/ruvnet/ruflo/main/docs/STATUS.md>
- MCP specification repository: <https://github.com/modelcontextprotocol/modelcontextprotocol>
- MCP Workflow Engine paper: <https://arxiv.org/abs/2605.00827>
- BenchAgent paper: <https://arxiv.org/abs/2606.05670>

## Comparison

| Area | Ruflo | Threnody direction |
|---|---|---|
| Scale | Broad platform with large agent/plugin surface | Stay smaller, inspectable, and local-first |
| Execution | Swarm/platform runtime | Host-native `host_spawn_waves` by default |
| Cost | Broad autonomy can add coordination calls | Receipts show selected path, counterfactual, and skipped calls |
| Learning | Rich memory and trajectory claims | SQLite learning with approval queue before activation |
| Memory | Vector/graph-style ecosystem direction | FTS memory first; cite and export before vector RAG |
| UI | Hosted web/goal/agent surfaces | Local HTML run cards and MCP-readable receipts |
| Verification | Platform verification story | Exportable JSON/Markdown/HTML run receipts |
| Swarm sizing | Platform/runtime-defined swarm limits | No default host-native hard cap; throttle concurrency separately |

## Threnody bets

1. **Token-savings receipts**: prove why a route/plan/swarm chose fewer agents or
   a lower tier.
2. **Operator run receipts**: export the exact plan, waves, learning contract,
   policy decisions, approvals, verification fields, and outcome.
3. **Curated task packs**: add practical breadth through six explicit packs, not
   through 100+ permanent agent definitions.
4. **MCP workflow blueprints**: reason once, replay successful host-native wave
   plans later without a fresh planner call.
5. **Agent-count optimizer**: default toward one agent or a pair unless task
   signals justify a swarm; broad review can intentionally fan out one read-only
   agent per file plus synthesis.
6. **Interactive run cards**: start as local HTML from `inspect_run_receipt`; keep
   the receipt schema simple enough for future MCP App-style hosts.
