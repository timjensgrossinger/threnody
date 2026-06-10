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
    "gemini-cli": "Gemini CLI",
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


def render_shell_instructions(
    config: TGsConfig,
    shell_id: str,
    *,
    verbatim: bool = False,
) -> str:
    """Render a managed Threnody instruction block for one shell."""
    profile = config.routing_policy.effective_profile(shell_id)
    label = SHELL_LABELS.get(profile.shell_id, profile.shell_id)
    lines: list[str] = []
    if not verbatim:
        lines.extend([f"## Threnody Integration for {label}", ""])
    else:
        lines.extend([f"# Threnody Integration for {label}", ""])

    lines.append(f"These instructions apply only to **{label}** (`{profile.shell_id}`).")
    lines.append("")

    if profile.route_task_mandatory:
        lines.extend(
            [
                "### Routing mode: strict",
                "",
                "ALWAYS call `route_task` before writing or editing code or other non-exempt project files.",
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
    if profile.low_tier_execute_subtask:
        lines.append("For low-tier work, use `execute_subtask` when it can safely write the whole target file.")
    else:
        lines.append("For low-tier work, `execute_subtask` is optional; use it only when it is safer or cheaper than direct editing.")

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
                "Do not bypass this hook for code edits unless the user explicitly disables strict routing.",
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
