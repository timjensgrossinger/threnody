## Host-native MCP discipline

PreToolUse routing hooks enforce **Edit/Write** coordination only. They do not
invoke host subagents. When a host caller attempts same-host work via
`execute_subtask`, Threnody returns `HostNativeRequired` with an actionable
`host_spawn` payload — spawn the host `Agent` or `Task` tool instead.

# Host routing hooks

Threnody can enforce **guarded coordination** on code edits in Claude Code via a PreToolUse hook. The hook calls `validate_routing_guard` logic directly (SQLite + guard state) — no MCP stdio subprocess per edit.

Guarded mode requires `route_task` or `decompose_task` before non-exempt code edits; after routing, the host follows `execution_hint` (host-native first, delegate only when needed). It is not a delegation mandate.

## Claude Code (installed by default in guarded mode)

After `./install.sh`, `~/.claude/settings.json` registers:

```json
{
  "matcher": "Edit|Write",
  "hooks": [
    {
      "type": "command",
      "command": "~/.local/lib/threnody/shell/threnody-routing-hook.sh"
    }
  ]
}
```

Re-run `./install.sh` to refresh the hook path after moving the install directory.

### Enable / disable

- **Guarded (hook on):** `routing_policy.shells.claude-code.mode: guarded` in `config.yaml`, then `./install.sh`
- **Advisory (hook off):** set mode to `advisory` and re-run `./install.sh` (removes the managed hook entry)

(`mode: strict` is a deprecated alias for `guarded`.)

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| Every edit blocked | Call `route_task` or `decompose_task` first from the same project directory |
| Hook not firing | Confirm `~/.claude/settings.json` contains the managed PreToolUse entry |
| Wrong install path | Set `THRENODY_INSTALL_DIR` before `./install.sh` or edit the `command` path |
| Test hook manually | `echo '{"tool_name":"Edit","cwd":"/path/to/repo","tool_input":{"file_path":"src/foo.py"}}' \| ~/.local/lib/threnody/shell/threnody-routing-hook.sh` |

Exit codes: **0** = allow, **2** = block (Claude convention).

## Copilot CLI

GitHub Copilot CLI uses **advisory** managed instructions only. There is no supported PreToolUse equivalent today; follow `docs/COST_SAVINGS.md` for host-native vs delegate decisions.

## Cursor / Codex / other hosts

Copy the pattern: run the hook script (or `python3 -m shared.routing_hook validate --stdin`) before file writes, or rely on advisory instructions from `./install.sh`.

Manual one-liner:

```bash
export PYTHONPATH="$HOME/.local/lib/threnody"
echo '{"tool_name":"Edit","cwd":"'"$PWD"'","tool_input":{"file_path":"relative/or/abs/path.py"}}' \
  | python3 -m shared.routing_hook validate --stdin
```

## Cost rationale

Hooks prevent premium-tier direct edits on routed code paths without a prior `route_task` decision — the main bypass vector for cost discipline when instructions alone are advisory.
