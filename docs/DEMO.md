# Scripted Demo

This demo uses only local commands and does not require credentials until the
final optional live provider step.

## 1. Inspect Available Providers

```bash
python3 mcp_server.py
```

From an MCP host, call:

```text
check_providers()
inspect_status(project_id=".")
```

Confirm that detected providers report routeability and precise failure
reasons.

## 2. Route a Simple Task

```text
route_task("rename this helper and update one docstring")
```

Expected: low tier.

## 3. Route a Medium Auth Task

```text
route_task("Implement authentication middleware across auth.py service.py cli.py")
```

Expected: medium tier. Routine authentication implementation must not be forced
to high tier, but it must not be low.

## 4. Route a High-Risk Review

```text
route_task("Perform a deep security review of the admin platform and identify mitigations")
```

Expected: high tier. Generic "security review" scores naturally; explicit deep
or security-critical review remains high.

## 5. Execute a File-Writing Subtask

```text
execute_subtask(
  prompt="Write a small Python module with one constant.",
  tier="low",
  target_file="/path/to/workspace/generated_demo.py"
)
```

Expected: provider, model, file path, diff summary, and write audit data.

## 6. Optional Live Smoke

Run one `execute_subtask` through each available provider family before a
public release. At least two independent host providers must pass.
