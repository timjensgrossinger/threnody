#!/usr/bin/env bash
# Claude Code PostToolUse hook — captures host-native wave learning to the run
# log without MCP stdio. Never blocks (always exits 0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}"

# Tolerate any failure — a PostToolUse hook must not break the tool.
python3 -m shared.learning_hook capture --stdin --hook-response || printf '{"continue":true,"suppressOutput":true}\n'
exit 0
