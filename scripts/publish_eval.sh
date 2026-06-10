#!/usr/bin/env bash
set -euo pipefail

# publish_eval.sh — generate and upload routing eval report.
# Usage: ./scripts/publish_eval.sh [--output-dir DIR]

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="${1:-${REPO_DIR}/docs}"

echo "Running routing eval..."
cd "$REPO_DIR"
THRENODY_TEST_MODE=1 python3 -m shared.routing_eval --report html > "${OUTPUT_DIR}/eval_report.html"
THRENODY_TEST_MODE=1 python3 -m shared.routing_eval --report markdown > "${OUTPUT_DIR}/EVAL_REPORT.md"

echo "Reports written to ${OUTPUT_DIR}/"
echo "  eval_report.html"
echo "  EVAL_REPORT.md"

# Extract accuracy line for README badge update
ACCURACY=$(THRENODY_TEST_MODE=1 python3 - <<'PYEOF'
import sys
sys.path.insert(0, ".")
from shared.routing_eval import run_eval, load_fixtures
import os
os.environ["THRENODY_TEST_MODE"] = "1"
out = run_eval(return_results=True)
if isinstance(out, dict):
    results = out.get("result", [])
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    print(f"{passed}/{total} ({100*passed//max(total,1)}%)")
PYEOF
)
echo "Routing accuracy: ${ACCURACY}"
