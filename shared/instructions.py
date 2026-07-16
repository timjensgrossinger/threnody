#!/usr/bin/env python3
"""Render shell-specific Threnody managed instruction blocks."""
from __future__ import annotations

import argparse
from pathlib import Path
from textwrap import dedent

from .config import CONFIG_YAML, ShellRoutingProfile, TGsConfig


SHELL_LABELS = {
    "claude-code": "Claude Code",
    "github-copilot-cli": "GitHub Copilot CLI",
    "cursor": "Cursor",
    "codex": "OpenAI Codex",
    "junie": "JetBrains Junie",
    "opencode": "OpenCode",
}


def _tier_mapping_table(profile: ShellRoutingProfile) -> str:
    rows = ["| Tier | Default model |", "|---|---|"]
    for tier in ("low", "medium", "high"):
        model = profile.tier_model_mapping.get(tier, "router-selected default")
        rows.append(f"| `{tier}` | `{model}` |")
    return "\n".join(rows)


def _format_patterns(patterns: list[str]) -> str:
    if not patterns:
        return "`none`"
    return ", ".join(f"`{pattern}`" for pattern in patterns)


def _render_claude_pointer_block(
    config: TGsConfig,
    profile: "ShellRoutingProfile",
    *,
    verbatim: bool = False,
) -> str:
    """Instruction block for Claude Code with inline orchestration contract."""
    heading = "# Threnody Integration for Claude Code" if verbatim else "## Threnody Integration for Claude Code"
    mode = "guarded" if profile.route_task_mandatory else "advisory"
    lines: list[str] = [heading, ""]
    lines.append("These instructions apply only to **Claude Code** (`claude-code`).")
    lines.append("")
    lines.append(f"### Routing mode: {mode}")
    lines.append("")
    if profile.route_task_mandatory:
        lines.append(
            "Call `route_task` before writing or editing any non-exempt file. "
            "A managed `PreToolUse` hook enforces this — it calls `validate_routing_guard` on every `Edit`/`Write`."
        )
    else:
        lines.append("`route_task` is recommended before non-trivial edits but not enforced.")
    lines.append("")

    lines.extend([
        "### Orchestration",
        "",
        "Start ordinary work with `start_task` when you want a guided next action; use the expert routing tools directly when you need their full contract.",
        "Always route work through Threnody so each task runs on the correct tier model.",
        "Every code task — including post-plan execution — follows this contract:",
        "",
        "| Task scope | Entry point | Method |",
        "|------------|-------------|--------|",
        "| Single file / one concern | `route_task` | direct edit with returned tier model |",
        "| Multi-file / multi-concern | `decompose_task` or `plan_task` | spawn wave agents per `host_spawn_waves` |",
        "| Large parallel / swarm | `execute_swarm` | `/threnody-swarm` skill |",
        "| Fullstack (fe + be + api) | `fleet_plan` | `/threnody-fullstack` skill |",
        "",
        "Typed subagents per tier (Claude Code only):",
        "",
        "| Tier | Subagent type | Default model |",
        "|------|---------------|---------------|",
        "| low | `threnody-low` | haiku |",
        "| medium | `threnody-medium` | sonnet |",
        "| high | `threnody-high` | opus |",
        "",
        "Learning reporting follows `learning_report_contract.report_mode`. In `batch` mode (default) do "
        "NOT call `report_host_wave` per worker wave — capture is automatic (PostToolUse hook) or passed in "
        "the single terminal call; report once via `report_host_swarm_complete(outcome=...)`. In `inline` mode "
        "call `report_host_wave` after each wave with `workspace_root`, per-agent results, and `output_excerpt`. "
        "Consensus waves (`wave_kind=consensus`) are always reported mid-run in both modes.",
        "",
        "### Multi-queen consensus",
        "",
        "`consensus_enabled: true` in config activates parallel coordinators for subprocess swarms "
        "(star topology). For host-native Claude Code waves, the equivalent is fanning out 2–3 independent "
        "`Plan` agents for complex architectural decisions, then synthesizing before committing.",
        "",
    ])

    if getattr(profile, "workflow_emit", False):
        lines.extend([
            "### Dynamic Workflow emission (opt-in)",
            "",
            "When a fan-out plan response includes `workflow_emit: true`, prefer launching its "
            "`workflow_script` via the **Workflow** tool (Claude Code v2.1.154+) instead of "
            "spawning `host_spawn_waves`. The script is tier-aware — each `agent()` routes to its "
            "Threnody tier model, where Workflow's default would otherwise run every agent on the "
            "session model — and runs in the background, keeping intermediate results out of context.",
            "After it returns, call `report_workflow_result(workflow_name, agents[])` with the "
            "returned `agents` array so Threnody records learning telemetry. If the Workflow tool is "
            "unavailable, fall back to `host_spawn_waves`.",
            "Use the **`/threnody-workflow`** skill to run a consensus swarm over this path and save a "
            "pre-tuned, documented, zero-config `/<slug>` command for coworkers. Multi-queen consensus is "
            "hybrid by default (queens as host agents); set `consensus_in_workflow: true` to render queens "
            "inside the workflow.",
            "",
        ])

    if not profile.route_task_mandatory:
        lines.extend([
            "### Guarded mode",
            "",
            "To add `PreToolUse` hook enforcement (hard-blocks `Edit`/`Write` without prior `route_task`), "
            "set `routing_policy.shells.claude-code.mode: guarded` in config.yaml and re-run `./install.sh`.",
            "",
        ])

    lines.extend([
        "### Skills",
        "",
        "Full routing contracts, execution patterns, and host-native details are in the installed Threnody skills:",
        "`/threnody-routing` · `/threnody-plan` · `/threnody-task` · "
        "`/threnody-subtasks` · `/threnody-swarm` · `/threnody-workflow` · `/threnody-fullstack`",
        "",
        "Run `/threnody-routing` first if you are unfamiliar with Threnody.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def render_shell_instructions(
    config: TGsConfig,
    shell_id: str,
    *,
    verbatim: bool = False,
) -> str:
    """Render a managed Threnody instruction block for one shell."""
    profile = config.routing_policy.effective_profile(shell_id)
    if profile.shell_id == "claude-code":
        return _render_claude_pointer_block(config, profile, verbatim=verbatim)
    label = SHELL_LABELS.get(profile.shell_id, profile.shell_id)
    lines: list[str] = []
    if not verbatim:
        lines.extend([f"## Threnody Integration for {label}", ""])
    else:
        lines.extend([f"# Threnody Integration for {label}", ""])

    lines.append(f"These instructions apply only to **{label}** (`{profile.shell_id}`).")
    lines.append("")
    lines.extend(
        [
            "### Threnody role: meta-harness",
            "",
            "Threnody is a local MCP coordination layer — **the host shell executes work** "
            "(Task tool, direct edits, host-configured backends).",
            "Use coordination tools first: `start_task`, `route_task`, `plan_task`, `execute_swarm`, `memory_*`, `learning_*`.",
            "`execute_subtask` is **utility delegation only** (opt-in OpenCode, Aider, local endpoints) — "
            "not for host→host subprocess routing (Copilot, Codex, Cursor, Junie, Claude). "
            "Same-host work uses `host_spawn` / Agent or Task.",
        ]
    )
    lines.append("")

    if profile.route_task_mandatory:
        lines.extend(
            [
                "### Routing mode: guarded",
                "",
                "ALWAYS call `route_task` or `decompose_task` before writing or editing code or other non-exempt project files.",
                "After routing, follow `execution_hint` — host-native execution first (Task tool, direct edits); "
                "use `execute_subtask` only for utility targets in `delegation_targets` when `providers.delegation_utilities_enabled` is true.",
                "If `route_task` returns a routing guard, follow that guard before using direct edit/write tools.",
            ]
        )
    else:
        lines.extend(
            [
                "### Routing mode: advisory",
                "",
                "`route_task` is recommended for non-trivial non-exempt changes, but it is not mandatory before edits in this shell.",
                "You may edit directly when that is simpler or when shell/tooling support does not enforce routing guards.",
                "When you do route, follow `execution_hint` — host-native first; utility delegation only when enabled.",
            ]
        )

    lines.extend(
        [
            "",
            "### Routing exemptions",
            "",
            "Do not call `route_task` solely for files covered by routing exemptions.",
            f"Default exempt filetypes: {_format_patterns(config.routing_exceptions.filetypes)}.",
            f"Default exempt paths/patterns: {_format_patterns(config.routing_exceptions.paths)}.",
            "All other filetypes remain routed by default; do not maintain a code-language allowlist.",
            "Do NOT call `routing_exception_add` for default exempt filetypes or paths — write directly.",
        ]
    )

    lines.append("")
    lines.append(
        "For low-tier work without an active host_spawn_waves handoff, prefer direct edits or the host "
        "subagent tool from `host_spawn`; do not use `execute_subtask` to route between host CLIs."
    )

    host_tool = "Agent" if profile.shell_id == "claude-code" else "Task"
    lines.extend(
        [
            "",
            "### Host-native execution contract",
            "",
            "After `route_task`, `plan_task`, or `fleet_plan`, consume `host_spawn` / `host_spawn_waves` from the MCP response.",
            f"When `host_spawn_waves` or `host_execution_contract: spawn_subagents` is present, spawn each wave with the host `{host_tool}` tool — do **not** use direct Write/Edit on planned `target_files`.",
            f"For lone `route_task` results with no pending handoff, direct edits are allowed when `host_native_method` is `direct_edit`.",
            "Do **not** call `execute_subtask` for same-host work — Threnody returns `HostNativeRequired` with an actionable `host_spawn` payload.",
            "Use `execute_subtask(provider_id=...)` only for utility backends in `delegation_targets` when `providers.delegation_utilities_enabled` is true.",
            "Host→host `execute_subtask` (Copilot, Codex, Cursor, Junie, Claude) returns `HostDelegationBlocked`.",
            "`execute_swarm` defaults to `host_native`: execute `host_spawn_waves` in the host shell; Threnody persists the plan as `awaiting_host_execution` without subprocess fanout.",
            "Heuristic planning fans out **one host agent per file** when task intent implies multiple files (webapp, html/css/js, fullstack) or when paths are listed.",
            "After scaffold/contract waves, call `expand_host_plan` with `discovered_files` or use `report_host_wave(expand_plan=true, discovered_files=[...])` to spawn remaining file agents.",
            "Reporting follows `learning_report_contract.report_mode`. `batch` (default): do NOT call `report_host_wave` per worker wave — capture is automatic (PostToolUse learning hook) or passed in the single terminal call; report once. `inline`: call `report_host_wave` after each wave with `workspace_root`, per-agent results (`task_id`, `spawn_id`, `success`, `touched_files`, `output_excerpt`). Consensus waves are always reported mid-run.",
            "Terminalize with `report_host_swarm_complete(outcome=accepted|revised|reworked|rejected)` (batch imports the run log + finalizes), or set `terminal=true` on the last `report_host_wave` in inline mode. Check `finalize.swarm_outcome` and `swarm_outcome_error`.",
            "Use `inspect_swarm` to verify run status (`awaiting_host_execution` → `running` → `completed`).",
        ]
    )
    if profile.shell_id == "claude-code":
        lines.extend(
            [
                "",
                "Claude Code uses the **`Agent`** tool for medium/high subtasks (`Task` is an alias when available).",
                "Subprocess `claude -p` via `execute_subtask` requires `providers.router_only_allow_execution` and carries Anthropic subscription/OAuth policy risk — see docs/LEGAL.md.",
            ]
        )

    lines.append("")
    if profile.agent_transparency_required:
        lines.extend(
            [
                "Agent transparency is required before routed waves.",
                "Single-agent task: one-liner `→ #1 {tier}/{model} · {method} · {file}`.",
                "Multi-agent wave (2+ agents): full table with Agent#, Tier, Model, Method, Target.",
                "After completion: summarize model, provider, files touched.",
            ]
        )
    else:
        lines.append("Agent transparency tables are optional unless the user asks for routed wave details.")

    lines.extend(["", "### Default tier-to-model guidance", "", _tier_mapping_table(profile), ""])

    if profile.direct_edit_hooks:
        lines.extend(
            [
                "### Direct edit/write hook enforcement",
                "",
                f"{label} should enforce direct `Edit`/`Write` calls with a `PreToolUse` hook.",
                "The managed hook calls Threnody `validate_routing_guard` with `target_file`, `tool_name`, and `cwd`.",
                "Do not bypass this hook for code edits unless the user explicitly disables guarded routing.",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render Threnody instructions for one AI shell")
    parser.add_argument("shell", help="Shell id, e.g. claude-code or github-copilot-cli")
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_YAML,
        help=f"Path to config.yaml (default: {CONFIG_YAML})",
    )
    parser.add_argument(
        "--verbatim",
        action="store_true",
        help="Render a self-contained block suitable for files without managed markers",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = TGsConfig.from_yaml(args.config)
    print(render_shell_instructions(config, args.shell, verbatim=args.verbatim), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
