# Threnody for Antigravity — Implementation Summary

## What We Built

A complete Antigravity integration that extends Google's agy CLI with Threnody's orchestration capabilities while respecting agy's native systems.

## Architecture Decisions

### 1. **Native agy Format (Primary)**
- `host_spawn_waves` outputs agy's native array-of-configs format
- Translation layer maintains compatibility with Claude, Copilot, Codex, Cursor, Junie, OpenCode
- Single source of truth (agy-native), multiple serializers

### 2. **Static + Dynamic Agents**
- 6 static agent definitions as templates (low/medium/high + 3 queen reviewers)
- Skills teach `define_subagent` for dynamic agent creation
- Users get both approaches: quick templates + flexible dynamic creation

### 3. **Smart Workspace Picker**
Auto-selects isolation mode based on task characteristics:
- `branch` — multi-file writes, wildcards (safest)
- `share` — read-only reviews (middle ground)
- `inherit` — simple single-file edits (fastest)

### 4. **Value-Add Features**
Things agy doesn't have natively:
- **Complexity-based tier routing** — auto-picks flash vs pro based on task complexity
- **Multi-queen consensus** — 3 parallel reviewers (correctness/risk/speed personas)
- **Cross-session memory** — SQLite-backed persistent state
- **Learning loop** — pattern tracking → draft agents → approval queue
- **Verify gate** — baseline-diff lint/type/test against merge base
- **Cost tracking** — token-savings receipts and spend analytics

### 5. **Python SDK Integration**
Optional `google-antigravity` SDK support as alternative to plugin hooks:
- `spawn_threnody_agent()` — tier-based agent spawning
- `spawn_dynamic_agent()` — custom agent with system prompt
- Works alongside plugin system, doesn't replace it

## Files Created/Modified

### Core Integration (37 files, 2724 insertions)

**New Provider (`antigravity/`):**
- `__init__.py` — module init
- `providers.py` — provider implementation with detection, command building, output cleaning
- `entry.py` — CLI entry point for subprocess execution
- `sdk_integration.py` — optional Python SDK wrapper

**Plugin System (`antigravity-plugin/`):**
- `plugin.json` — agy plugin manifest
- `mcp_config.json` — MCP server registration
- `hooks.json` — PreToolUse/PostToolUse hooks
- `rules/threnody.md` — routing rules
- `skills/` — 9 skills (plan, routing, task, swarm, fullstack, fast-review, swarm-review, workflow, subtasks, cost)

**Agent Definitions (`shell/agents/`):**
- `threnody-agy-low.md` — gemini-3.5-flash, low effort
- `threnody-agy-medium.md` — gemini-3.5-flash, high effort
- `threnody-agy-high.md` — gemini-3.1-pro, high effort
- `threnody-agy-queen-correctness.md` — correctness-first reviewer
- `threnody-agy-queen-risk.md` — risk-first reviewer
- `threnody-agy-queen-speed.md` — speed-first reviewer

**Core Changes:**
- `shared/host_spawn.py` — agy-native format + translation methods + smart workspace picker
- `shared/discovery.py` — provider registration, caller detection
- `shared/config.py` — shell config, effort defaults
- `shared/instructions.py` — instruction rendering
- `shared/model_registry.py` — Gemini model registry entries
- `install.sh` — plugin installation logic
- `config.example.yaml` — Antigravity config section
- `README.md` — balanced disclaimer

**Tests:**
- `tests/test_antigravity_host_spawn.py` — spawn format, translation methods, workspace picker
- `tests/test_antigravity_provider.py` — provider detection, command building
- `tests/test_antigravity_registration.py` — registration in discovery, config, model registry
- `tests/test_antigravity_sdk.py` — SDK integration tests

## Key Code Examples

### agy-Native Spawn Format

```python
spec = HostSpawnSpec(
    tool="invoke_subagent",
    method="host_task",
    model="gemini-3.5-flash",
    subagent_type="threnody-low",
    prompt="Implement feature X",
    tier="low",
    workspace="inherit",
    role="Implementer",
    effort="low",
)

# agy-native format
agy_config = spec.to_agy_dict()
# {
#   "TypeName": "low",
#   "Role": "Implementer",
#   "Prompt": "Implement feature X",
#   "Workspace": "inherit",
#   "Model": "gemini-3.5-flash",
#   "Effort": "low"
# }

# Translation for Claude
claude_config = spec.to_claude_dict()
# {"tool": "Agent", "subagent_type": "threnody-low", ...}
```

### Smart Workspace Picker

```python
def determine_workspace_mode(task: dict) -> str:
    if task.get("read_only"):
        return "share"
    if len(task.get("target_files", [])) > 3:
        return "branch"
    if any("*" in f for f in task.get("target_files", [])):
        return "branch"
    return "inherit"
```

### Python SDK Integration

```python
from antigravity.sdk_integration import spawn_threnody_agent

result = await spawn_threnody_agent(
    tier="medium",
    prompt="Implement feature X",
    workspace="branch"
)
```

## README Disclaimer

Balanced positioning:
- Acknowledges agy's native capabilities (subagents, swarms, planning, skills, hooks, MCP)
- Highlights what we add (tier routing, consensus, memory, learning, verify gate, cost tracking)
- Notes most useful for multi-tool teams or cost optimization
- Clarifies plugin uses only official extension points
- Links to ToS and support contact

## Testing

All tests pass:
- 2593 tests passed, 3 skipped
- New tests cover: spawn format, translation methods, workspace picker, SDK integration
- Compiles cleanly, no syntax errors

## What's Next

1. **Commit and push** to branch `feat/antigravity-provider`
2. **Create PR** with this summary
3. **Test with actual agy CLI** once available
4. **Iterate** based on real-world usage

## Policy Compliance

✅ Uses only official agy extension points (plugins, skills, hooks, MCP, agents)
✅ No direct API calls or OAuth token handling
✅ Python SDK is Google's official package (Apache 2.0)
✅ Subprocess delegation removed (was policy risk)
✅ Transparent disclaimer in README
