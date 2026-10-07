#!/usr/bin/env bash
# Claude Code Agent-tool hook — puts the routed model + reasoning effort on every
# subagent spawn and logs each spawn to logs/agent_spawns.jsonl.
# Subcommands: pre | post | subagent-start | subagent-stop | session-start.
# Never blocks: any failure prints nothing (the no-op) and exits 0.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 0
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)" || exit 0
export PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}"
export THRENODY_INSTALL_DIR="${THRENODY_INSTALL_DIR:-$INSTALL_DIR}"

python3 -m shared.agent_hook "${1:-}" 2>/dev/null || true
exit 0
