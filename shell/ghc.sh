#!/usr/bin/env bash
# Threnody — orchestrated agent ensemble
# Source this in ~/.zshrc:  source ~/.local/lib/threnody/shell/ghc.sh
#
# Every `ghcag "task"` call auto-triggers:
#   1. PLAN:       sonnet 4.6 reasons about the task, produces execution plan
#   2. EXECUTE:    spawn parallel agents at assigned model tiers (wave by wave)
#   3. SYNTHESISE: sonnet 4.6 merges results and flags conflicts
#
# No API keys — everything goes through `gh copilot`.

_ROUTER_DIR="$HOME/.local/lib/threnody"
if [[ ! -d "$_ROUTER_DIR" && -d "$HOME/.local/lib/switchyard" ]]; then
    _ROUTER_DIR="$HOME/.local/lib/switchyard"
fi
_ROUTER_CLI="python3 $_ROUTER_DIR/copilot/entry.py"

# ---------------------------------------------------------------------------
# _ghc_model_flag — cached check for --model support
# ---------------------------------------------------------------------------
_ghc_model_ok=""
_ghc_check_model() {
    if [[ -z "$_ghc_model_ok" ]]; then
        if gh copilot agent --help 2>&1 | grep -q "\-\-model" 2>/dev/null; then
            _ghc_model_ok="yes"
        else
            _ghc_model_ok="no"
        fi
    fi
}

# ---------------------------------------------------------------------------
# _ghc_call — call gh copilot with optional model injection
# ---------------------------------------------------------------------------
_ghc_call() {
    local subcommand="$1" model="$2"
    shift 2
    _ghc_check_model
    if [[ "$_ghc_model_ok" == "yes" && -n "$model" ]]; then
        gh copilot "$subcommand" --model "$model" "$@"
    else
        if [[ "$_ghc_model_ok" != "yes" && -n "$model" ]]; then
            echo "   note: --model not supported, using default" >&2
        fi
        gh copilot "$subcommand" "$@"
    fi
}

# ---------------------------------------------------------------------------
# _ghc_run_wave — execute one wave of agents in parallel
#   args: tmpdir wave_label subtask_json [subtask_json ...]
# ---------------------------------------------------------------------------
_ghc_run_wave() {
    local tmpdir="$1"
    shift 2

    local pids=() ids=()

    for st_json in "$@"; do
        local sid sdesc smodel stier
        sid=$(echo "$st_json"   | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
        sdesc=$(echo "$st_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['description'])")
        smodel=$(echo "$st_json"| python3 -c "import sys,json; print(json.load(sys.stdin)['model'])")
        stier=$(echo "$st_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['tier'])")

        echo "    #${sid} [${stier}] → ${smodel}: ${sdesc:0:72}" >&2

        local outfile="$tmpdir/agent_${sid}.out"
        (
            _ghc_call agent "$smodel" "$sdesc" > "$outfile" 2>&1
        ) &
        pids+=($!)
        ids+=("$sid")
    done

    local failed=0
    for i in "${!pids[@]}"; do
        if ! wait "${pids[$i]}"; then
            echo "    ⚠️  Agent #${ids[$i]} exited non-zero" >&2
            ((failed++))
        fi
    done
    return $failed
}

# ---------------------------------------------------------------------------
# ghc — the main entry point
#
# Usage:
#   ghc suggest "how to list files"        → simple route, single agent
#   ghc explain "what does awk do"         → simple route, single agent
#   ghc agent "implement JWT auth"         → AUTO-ORCHESTRATED ensemble
#   ghc agent -w "complex task"            → show plan only, don't execute
#   ghc agent -f "cached task"             → skip cache
#   ghc agent --no-plan "quick task"       → skip orchestration, single agent
#   ghc --stats                            → cache stats
# ---------------------------------------------------------------------------
ghc() {
    if [[ $# -eq 0 || "$1" == "-h" || "$1" == "--help" ]]; then
        cat <<'HELP'
Usage:
  ghc suggest "question"
  ghc explain "question"
  ghc agent "coding task"
  ghc agent -w "coding task"       Show plan only
  ghc agent --no-plan "task"       Skip orchestration
  ghc --stats
HELP
        return 0
    fi

    # --- Meta ---
    if [[ "$1" == "--stats" || "$1" == "-s" ]]; then
        $_ROUTER_CLI cache-stats
        return 0
    fi

    local subcommand="$1"
    shift

    # Parse flags
    local force=0 why=0 noplan=0
    local passthrough=()
    for arg in "$@"; do
        case "$arg" in
            -f|--force)   force=1 ;;
            -w|--why)     why=1 ;;
            --no-plan)    noplan=1 ;;
            *)            passthrough+=("$arg") ;;
        esac
    done

    local passthrough_count="${#passthrough[@]}"
    local task="${passthrough[$((passthrough_count - 1))]}"
    local preceding=("${passthrough[@]:0:$((passthrough_count - 1))}")

    if [[ -z "$task" ]]; then
        gh copilot "$subcommand" "$@"
        return $?
    fi

    # --- Cache check ---
    if [[ $force -eq 0 ]]; then
        local cj cf
        cj=$($_ROUTER_CLI cache-get "$task" 2>/dev/null)
        cf=$(echo "$cj" | python3 -c "import sys,json; print(json.load(sys.stdin).get('found',False))" 2>/dev/null)
        if [[ "$cf" == "True" || "$cf" == "true" ]]; then
            echo "⚡ Cache hit — no model call" >&2
            echo "$cj" | python3 -c "import sys,json; print(json.load(sys.stdin)['result'])"
            return 0
        fi
    fi

    # ─────────────────────────────────────────────────────────
    # suggest / explain → simple single-agent call
    # ─────────────────────────────────────────────────────────
    if [[ "$subcommand" != "agent" ]]; then
        local rj model reason
        rj=$($_ROUTER_CLI route "$task" 2>/dev/null)
        model=$(echo "$rj"  | python3 -c "import sys,json; print(json.load(sys.stdin)['model'])" 2>/dev/null)
        reason=$(echo "$rj" | python3 -c "import sys,json; print(json.load(sys.stdin)['reason'])" 2>/dev/null)
        echo "🔀 model=$model  ($reason)" >&2
        [[ $why -eq 1 ]] && return 0
        local result call_status
        result=$(_ghc_call "$subcommand" "$model" "${preceding[@]}" "$task")
        call_status=$?
        echo "$result"
        if [[ $call_status -eq 0 && -n "$result" ]]; then
            $_ROUTER_CLI cache-put "$task" "$result" "$model" 2>/dev/null
        fi
        return "$call_status"
    fi

    # ─────────────────────────────────────────────────────────
    # agent → ORCHESTRATED ENSEMBLE (auto on every call)
    # ─────────────────────────────────────────────────────────

    # --no-plan: skip orchestration, run single agent with heuristic routing
    if [[ $noplan -eq 1 ]]; then
        local rj model
        rj=$($_ROUTER_CLI route "$task" 2>/dev/null)
        model=$(echo "$rj" | python3 -c "import sys,json; print(json.load(sys.stdin)['model'])" 2>/dev/null)
        echo "🔀 [no-plan] model=$model" >&2
        _ghc_call agent "$model" "$task"
        return $?
    fi

    # ── STEP 1: PLAN ──────────────────────────────────────────
    echo "" >&2
    echo "🧠 Planner (sonnet 4.6) is reasoning about your task..." >&2
    echo "" >&2

    local plan_json
    plan_json=$($_ROUTER_CLI plan "$task" 2>/dev/null)

    if [[ -z "$plan_json" ]] || ! printf '%s\n' "$plan_json" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'subtasks' in d else 1)" 2>/dev/null; then
        echo "   ⚠️  Planner failed — running single agent" >&2
        _ghc_call agent "" "$task"
        return $?
    fi

    # Parse plan
    local analysis strategy total_agents num_waves
    analysis=$(echo "$plan_json"    | python3 -c "import sys,json; print(json.load(sys.stdin)['analysis'])" 2>/dev/null)
    strategy=$(echo "$plan_json"    | python3 -c "import sys,json; print(json.load(sys.stdin)['strategy'])" 2>/dev/null)
    total_agents=$(echo "$plan_json"| python3 -c "import sys,json; print(json.load(sys.stdin)['total_agents'])" 2>/dev/null)
    num_waves=$(echo "$plan_json"   | python3 -c "import sys,json; print(len(json.load(sys.stdin)['waves']))" 2>/dev/null)

    # ── Show the plan ─────────────────────────────────────────
    echo "┌──────────────────────────────────────────────────────────┐" >&2
    echo "│  🤖 ENSEMBLE: ${total_agents} agent(s), ${num_waves} wave(s), ${strategy}" >&2
    echo "│" >&2
    echo "│  ${analysis:0:56}" >&2
    echo "│" >&2

    echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
for st in plan.get('subtasks', []):
    deps = ''
    if st.get('depends_on'):
        deps = f' (after #{\"#\".join(str(d) for d in st[\"depends_on\"])})'
    desc = st.get('description', '')[:50]
    tier = st.get('tier', 'low')
    model = st.get('model', '?')
    print(f'│  #{st[\"id\"]} [{tier:6s}] → {model:20s} {desc}{deps}')
" >&2

    echo "└──────────────────────────────────────────────────────────┘" >&2

    # --why: show plan, don't execute
    if [[ $why -eq 1 ]]; then
        echo "" >&2
        echo "(--why mode — not executing)" >&2
        echo "$plan_json"
        return 0
    fi

    # ── STEP 2: EXECUTE (wave by wave) ────────────────────────
    echo "" >&2
    local tmpdir
    tmpdir=$(mktemp -d)
    local total_failed=0

    for wave_idx in $(seq 0 $((num_waves - 1))); do
        local wave_size
        wave_size=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['waves'][$wave_idx]))" 2>/dev/null)

        echo "━━━ Wave $((wave_idx + 1))/${num_waves} (${wave_size} parallel agents) ━━━" >&2

        local wave_sts=()
        for st_idx in $(seq 0 $((wave_size - 1))); do
            local stj
            stj=$(echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
wave = plan.get('waves', [])[$wave_idx]
sid = wave[$st_idx]
st = next(s for s in plan['subtasks'] if s['id'] == sid)
print(json.dumps(st))
" 2>/dev/null)
            wave_sts+=("$stj")
        done

        _ghc_run_wave "$tmpdir" "$((wave_idx + 1))" "${wave_sts[@]}"
        total_failed=$((total_failed + $?))

        echo "" >&2
    done

    # ── STEP 3: COLLECT RESULTS ───────────────────────────────
    local results_json
    results_json=$(echo "$plan_json" | TMPDIR_CTX="$tmpdir" python3 -c "
import sys, json, os
plan = json.load(sys.stdin)
tmpdir = os.environ.get('TMPDIR_CTX', '/tmp')
results = {}
for st in plan.get('subtasks', []):
    outfile = os.path.join(tmpdir, f'agent_{st[\"id\"]}.out')
    if os.path.isfile(outfile):
        with open(outfile) as f:
            results[st.get('id')] = f.read(10_485_760)
    else:
        results[st.get('id')] = '(no output)'
print(json.dumps(results))
" 2>/dev/null)

    # ── STEP 4: SYNTHESISE (if >1 agent) ─────────────────────
    if [[ "$total_agents" -gt 1 ]]; then
        echo "🧬 Synthesiser (sonnet 4.6) merging ${total_agents} agent results..." >&2
        echo "" >&2

        local synthesis
        synthesis=$($_ROUTER_CLI synthesise "$task" "$results_json" 2>/dev/null)

        local synth_text
        synth_text=$(echo "$synthesis" | python3 -c "import sys,json; print(json.load(sys.stdin).get('synthesis',''))" 2>/dev/null)

        if [[ -n "$synth_text" && "$synth_text" != "None" ]]; then
            echo "━━━ Synthesis ━━━"
            echo "$synth_text"
            echo ""
        fi
    fi

    # ── Print individual agent outputs ────────────────────────
    echo "━━━ Agent Outputs ━━━"
    echo "$plan_json" | TMPDIR_CTX="$tmpdir" python3 -c "
import sys, json, os
plan = json.load(sys.stdin)
tmpdir = os.environ.get('TMPDIR_CTX', '/tmp')
for st in plan.get('subtasks', []):
    outfile = os.path.join(tmpdir, f'agent_{st[\"id\"]}.out')
    print(f'')
    tier = st.get('tier', 'low')
    model = st.get('model', '?')
    print(f'── #{st.get(\"id\")} [{tier}→{model}]: {st.get(\"description\", \"\")[:60]}')
    if os.path.isfile(outfile):
        with open(outfile) as f:
            content = f.read(10_485_760).strip()
            print(content if content else '(empty output)')
    else:
        print('(no output file)')
"

    # Cache combined result
    if [[ $total_failed -eq 0 ]]; then
        local combined
        combined=$(echo "$results_json" | python3 -c "
import sys, json
results = json.load(sys.stdin)
print('\n'.join(results.values()))
" 2>/dev/null)
        [[ -n "$combined" ]] && $_ROUTER_CLI cache-put "$task" "$combined" "ensemble" 2>/dev/null
    fi

    rm -rf "$tmpdir"
    [[ $total_failed -eq 0 ]]
}

# ---------------------------------------------------------------------------
# ghca — manual parallel mode (explicit subtask list, each independently routed)
# ---------------------------------------------------------------------------
ghca() {
    local tasks=("$@")
    if [[ ${#tasks[@]} -eq 0 ]]; then
        echo "Usage: ghca \"subtask1\" \"subtask2\" ..." >&2
        return 1
    fi

    echo "🤖 Manual ensemble: ${#tasks[@]} agents" >&2
    local tmpdir pids=()
    tmpdir=$(mktemp -d)

    for i in "${!tasks[@]}"; do
        local task="${tasks[$i]}"
        local outfile="$tmpdir/agent_$i.out"
        (
            local rj model
            rj=$($_ROUTER_CLI route "$task" 2>/dev/null)
            model=$(echo "$rj" | python3 -c "import sys,json; print(json.load(sys.stdin)['model'])" 2>/dev/null)
            echo "  #$((i+1)) → $model: ${task:0:60}" >&2
            _ghc_call agent "$model" "$task" > "$outfile" 2>&1
        ) &
        pids+=($!)
    done

    local failed=0
    for i in "${!pids[@]}"; do
        wait "${pids[$i]}" || ((failed++))
    done

    echo "" >&2
    echo "━━━ Results ━━━"
    for i in "${!tasks[@]}"; do
        echo ""
        echo "── Agent $((i+1)): ${tasks[$i]}"
        cat "$tmpdir/agent_$i.out"
    done

    rm -rf "$tmpdir"
    [[ $failed -eq 0 ]]
}

# ---------------------------------------------------------------------------
# threnody inspect — CLI-first operator surface for readiness and task inspection
# ---------------------------------------------------------------------------
_TGS_PYTHON=""

_tgs_python() {
    if [[ -n "$_TGS_PYTHON" ]]; then
        printf '%s\n' "$_TGS_PYTHON"
        return 0
    fi

    local candidate
    for candidate in /opt/homebrew/bin/python3 "$(command -v python3 2>/dev/null)" "$(command -v python 2>/dev/null)"; do
        [[ -z "$candidate" || ! -x "$candidate" ]] && continue
        if "$candidate" - <<'PY' >/dev/null 2>&1
from dataclasses import dataclass

@dataclass(slots=True)
class _Compat:
    value: int
PY
        then
            _TGS_PYTHON="$candidate"
            printf '%s\n' "$_TGS_PYTHON"
            return 0
        fi
    done

    echo "threnody inspect: no compatible Python interpreter found (need Python 3.10+)" >&2
    return 1
}

_tgs_usage() {
    cat >&2 <<'EOF'
Usage:
  threnody inspect status [--project PATH] [--details]
  threnody inspect task <task_id> [--details]
  threnody inspect approvals [--project PATH] [--limit N] [--details]
  threnody inspect write-audit [--limit N]
  threnody inspect approvals approve <id> --project PATH --operator OP
  threnody inspect approvals reject <id> --project PATH --operator OP --reason TEXT
  threnody inspect approvals merge <id> <target_agent_id> --project PATH --operator OP [--reason TEXT]
  threnody tune show [key] --project PATH
  threnody tune set <key> <value> --project PATH [--force]
  threnody tune reset [key] --project PATH
  threnody settings
  threnody eval run [--filter CATEGORY]
  threnody eval baseline
  threnody except list
  threnody except add <type> <pattern> [--note TEXT]
  threnody except remove <type> <pattern>
  threnody db check [--db PATH]
  threnody db repair [--db PATH]
  threnody db backup [--db PATH]
  threnody db prune [--db PATH] [--keep N]

Examples:
  threnody inspect status --project .
  threnody inspect status --project . --details
  threnody inspect task execute-1234
  threnody inspect approvals --project .
  threnody inspect write-audit --limit 20
  threnody tune set concurrency_limit 5 --project .
  threnody eval run --filter low
  threnody eval baseline
  threnody except add skill "auto-time"
  threnody except add skill "tgsd-*"
  threnody except add filetype ".md"
  threnody except add project "/home/me/notes"
  threnody except list
  threnody except remove skill "auto-time"
EOF
}

_tgs_inspect_status() {
    local project="."
    local details=0
    local outcome_details=0
    local pybin=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --project)
                [[ $# -lt 2 ]] && { echo "threnody inspect status: --project requires a path" >&2; return 1; }
                project="$2"
                shift 2
                ;;
            --details)
                details=1
                shift
                ;;
            --outcome-details)
                outcome_details=1
                shift
                ;;
            -h|--help)
                _tgs_usage
                return 0
                ;;
            *)
                echo "threnody inspect status: unknown argument: $1" >&2
                _tgs_usage
                return 1
                ;;
            esac
    done

    pybin=$(_tgs_python) || return 1
    ROUTER_DIR="$_ROUTER_DIR" TGS_ACTIVE_WORKSPACE="$project" "$pybin" - "$project" "$details" "$outcome_details" <<'PY'
import json
import os
import sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
sys.path.insert(0, str(base))

import mcp_server

project = sys.argv[1] if len(sys.argv) > 1 else ""
details = sys.argv[2] == "1" if len(sys.argv) > 2 else False
outcome_details = sys.argv[3] == "1" if len(sys.argv) > 3 else False
result = mcp_server.inspect_status(project)

if details or outcome_details:
    if outcome_details:
        # Add outcome metrics to result
        try:
            outcome_result = mcp_server.handle_learning_outcome_stats({})
            if outcome_result.get("success"):
                result["outcome_metrics"] = {
                    "coverage_percentage": outcome_result.get("coverage_percentage"),
                    "total_tasks_in_window": outcome_result.get("total_tasks_in_window"),
                    "tasks_with_feedback": outcome_result.get("tasks_with_feedback"),
                    "distribution": outcome_result.get("outcome_distribution", {}),
                }
            else:
                result["outcome_metrics"] = {"error": outcome_result.get("error", "Unknown error")}
        except Exception as e:
            result["outcome_metrics"] = {"error": f"Failed to get outcome metrics: {e}"}
    
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if "error" not in result else 1)

if "error" in result:
    print(f"status error: {result.get('error', 'unknown')} - {result.get('details', '')}".rstrip(), file=sys.stderr)
    raise SystemExit(1)

readiness = result.get("readiness", {})
limits = result.get("limits", {})
summary = readiness.get("summary", {})
enabled = readiness.get("enabled") or readiness.get("enabled_features") or []
enabled_text = ",".join(enabled) if enabled else "none"
recent = result.get("recent_summary", {})
rework = result.get("rework_summary", {})
urgency_val = recent.get("max_urgency_score")
rework_count = rework.get("recent_rework_count", 0)
urgency_part = f" urgency={urgency_val:.2f}" if urgency_val is not None else ""
rework_part = f" rework={rework_count}" if rework_count else ""

print(
    "status "
    f"{result.get('project_id') or project}: "
    f"enabled={enabled_text} "
    f"pending={summary.get('pending_approval_count', 0)}"
    f"{urgency_part}"
    f"{rework_part} "
    f"concurrency={limits.get('concurrency', '?')} "
    f"budget={limits.get('budget_hard_cap_tokens', '?')} "
    f"fanout={limits.get('fanout_cap', '?')}"
)

if outcome_details:
    outcome = result.get("outcome_metrics", {})
    if "error" not in outcome:
        coverage = outcome.get("coverage_percentage")
        total = outcome.get("total_tasks_in_window", 0)
        feedback = outcome.get("tasks_with_feedback", 0)
        if coverage is not None:
            print(f"Outcome Observability (1h window):")
            print(f"  Coverage: {coverage:.1f}% ({feedback}/{total} tasks with feedback)")
            dist = outcome.get("distribution", {})
            if dist:
                print(f"  Distribution (by model):")
                for tier_model, counts in sorted(dist.items(), key=lambda x: sum(x[1].values()) if isinstance(x[1], dict) else 0, reverse=True)[:5]:
                    acc = counts.get("accepted", 0)
                    rev = counts.get("revised", 0)
                    rej = counts.get("rejected", 0)
                    work = counts.get("reworked", 0)
                    print(f"    {tier_model}: {acc} accepted, {rev} revised, {rej} rejected, {work} reworked")

print(f"details: {result.get('explainability_link', 'threnody inspect status --details')}")
PY
}

_tgs_inspect_task() {
    local task_id=""
    local details=0
    local pybin=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --details)
                details=1
                shift
                ;;
            -h|--help)
                _tgs_usage
                return 0
                ;;
            *)
                if [[ -z "$task_id" ]]; then
                    task_id="$1"
                    shift
                else
                    echo "threnody inspect task: unknown argument: $1" >&2
                    _tgs_usage
                    return 1
                fi
                ;;
        esac
    done

    if [[ -z "$task_id" ]]; then
        echo "threnody inspect task: task_id is required" >&2
        _tgs_usage
        return 1
    fi

    pybin=$(_tgs_python) || return 1
    ROUTER_DIR="$_ROUTER_DIR" "$pybin" - "$task_id" "$details" <<'PY'
import json
import os
import sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
sys.path.insert(0, str(base))

import mcp_server

task_id = sys.argv[1] if len(sys.argv) > 1 else ""
details = sys.argv[2] == "1" if len(sys.argv) > 2 else False
result = mcp_server.inspect_task(task_id)

if details:
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if "error" not in result else 1)

if "error" in result:
    print(f"task error: {result.get('error', 'unknown')} - {result.get('details', '')}".rstrip(), file=sys.stderr)
    raise SystemExit(1)

subtasks = result.get("subtasks", [])
providers = sorted({str(row.get("provider") or "?") for row in subtasks})
models = sorted({str(row.get("model") or "?") for row in subtasks})
fallback = any(bool(row.get("used_fallback")) for row in subtasks)
speculation = any(bool(row.get("used_speculation")) for row in subtasks)

print(
    f"task {task_id}: "
    f"subtasks={len(subtasks)} "
    f"providers={','.join(providers) if providers else 'none'} "
    f"models={','.join(models) if models else 'none'} "
    f"fallback={'yes' if fallback else 'no'} "
    f"speculation={'yes' if speculation else 'no'}"
)
print("details: threnody inspect task <task_id> --details")
PY
}

_tgs_inspect_approvals() {
    local project="."
    local limit=25
    local details=0
    local action="list"
    local queue_id=""
    local canonical_id=""
    local operator=""
    local reason="operator-merge"
    local pybin=""

    if [[ $# -gt 0 ]]; then
        case "$1" in
            approve|reject|merge)
                action="$1"
                shift
                ;;
        esac
    fi

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --project)
                [[ $# -lt 2 ]] && { echo "threnody inspect approvals: --project requires a path" >&2; return 1; }
                project="$2"
                shift 2
                ;;
            --limit)
                [[ $# -lt 2 ]] && { echo "threnody inspect approvals: --limit requires a value" >&2; return 1; }
                limit="$2"
                shift 2
                ;;
            --operator)
                [[ $# -lt 2 ]] && { echo "threnody inspect approvals: --operator requires a value" >&2; return 1; }
                operator="$2"
                shift 2
                ;;
            --reason)
                [[ $# -lt 2 ]] && { echo "threnody inspect approvals: --reason requires a value" >&2; return 1; }
                reason="$2"
                shift 2
                ;;
            --details)
                details=1
                shift
                ;;
            -h|--help)
                _tgs_usage
                return 0
                ;;
            *)
                case "$action" in
                    approve|reject)
                        if [[ -z "$queue_id" ]]; then
                            queue_id="$1"
                            shift
                            continue
                        fi
                        ;;
                    merge)
                        if [[ -z "$queue_id" ]]; then
                            queue_id="$1"
                            shift
                            continue
                        fi
                        if [[ -z "$canonical_id" ]]; then
                            canonical_id="$1"
                            shift
                            continue
                        fi
                        ;;
                esac
                echo "threnody inspect approvals: unknown argument: $1" >&2
                _tgs_usage
                return 1
                ;;
        esac
    done

    case "$action" in
        approve|reject)
            [[ -z "$queue_id" || -z "$operator" ]] && {
                echo "threnody inspect approvals $action: queue id and --operator are required" >&2
                return 1
            }
            ;;
        merge)
            [[ -z "$queue_id" || -z "$canonical_id" || -z "$operator" ]] && {
                echo "threnody inspect approvals merge: queue id, target agent id, and --operator are required" >&2
                return 1
            }
            ;;
    esac

    pybin=$(_tgs_python) || return 1
    ROUTER_DIR="$_ROUTER_DIR" TGS_ACTIVE_WORKSPACE="$project" "$pybin" - \
        "$project" "$action" "$limit" "$details" "$queue_id" "$canonical_id" "$operator" "$reason" <<'PY'
import json
import os
import sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
sys.path.insert(0, str(base))

import mcp_server

project = sys.argv[1] if len(sys.argv) > 1 else ""
action = sys.argv[2] if len(sys.argv) > 2 else ""
limit = int(sys.argv[3])
details = sys.argv[4] == "1"
queue_id = sys.argv[5]
canonical_id = sys.argv[6]
operator = sys.argv[7]
reason = sys.argv[8]

try:
    if action == "list":
        result = mcp_server.approval_queue_list(project, limit=limit)
    elif action == "approve":
        result = mcp_server.approval_queue_approve(project, queue_id, operator)
    elif action == "reject":
        result = mcp_server.approval_queue_reject(project, queue_id, operator, reason=reason)
    else:
        result = mcp_server.approval_queue_merge(project, queue_id, canonical_id, operator, reason=reason)
except Exception as exc:  # noqa: BLE001 - shell surface needs a readable CLI error
    result = {"error": "ApprovalActionError", "details": str(exc)}

if details:
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if not isinstance(result, dict) or "error" not in result else 1)

if isinstance(result, dict) and "error" in result:
    print(f"approval error: {result.get('error')} - {result.get('details', '')}".rstrip(), file=sys.stderr)
    raise SystemExit(1)

if action == "list":
    print(f"approvals {project}: {len(result)} pending")
    for item in result:
        print(
            f"#{item.get('id')} {item.get('name')} "
            f"status={item.get('status')} created={item.get('created_at')}"
        )
    raise SystemExit(0)

if result.get("warning"):
    print(f"warning: {result.get('warning')}")

if action == "approve":
    print(f"approved #{result.get('queue_id')} for {project} by {result.get('operator_id')}")
elif action == "reject":
    print(
        f"rejected #{result.get('queue_id')} for {project} by {result.get('operator_id')}: "
        f"{result.get('reason')}"
    )
else:
    print(
        f"merged #{result.get('queue_id')} into {result.get('canonical_id')} "
        f"for {project} by {result.get('operator_id')}"
    )
PY
}

_tgs_learning_summary() {
    local project="."
    local pybin=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --project)
                [[ $# -lt 2 ]] && { echo "threnody learning summary: --project requires a path" >&2; return 1; }
                project="$2"
                shift 2
                ;;
            -h|--help)
                echo "threnody learning summary [--project PATH]"
                echo ""
                echo "Display learning outcome summary over 1-hour window"
                echo "  --project PATH  use project at PATH (default: .)"
                return 0
                ;;
            *)
                echo "threnody learning summary: unknown argument: $1" >&2
                return 1
                ;;
        esac
    done

    pybin=$(_tgs_python) || return 1
    ROUTER_DIR="$_ROUTER_DIR" TGS_ACTIVE_WORKSPACE="$project" "$pybin" - <<'PY'
import json
import os
import sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))

import mcp_server

try:
    result = mcp_server.handle_learning_outcome_stats({})
    
    if not result.get("success"):
        print("No outcome snapshot available yet (background computation initializing)")
        raise SystemExit(1)
    
    snapshot = result
    coverage = snapshot.get("coverage_percentage")
    total_tasks = snapshot.get("total_tasks_in_window", 0)
    tasks_with_feedback = snapshot.get("tasks_with_feedback", 0)
    
    print("Learning Outcome Summary (1h window)")
    print(f"Project: {os.environ.get('TGS_ACTIVE_WORKSPACE', '.')}")
    print()
    
    if coverage is not None:
        print(f"Feedback Coverage:     {coverage:.1f}% ({tasks_with_feedback}/{total_tasks} tasks)")
    else:
        print(f"Feedback Coverage:     N/A ({tasks_with_feedback}/{total_tasks} tasks)")
    
    print("Distribution by Model:")
    
    dist = snapshot.get("outcome_distribution", {})
    if not dist:
        print("  (no outcome data)")
    else:
        # Sort by total count descending
        sorted_models = sorted(dist.items(), key=lambda x: sum(x[1].values()), reverse=True)
        for tier_model, counts in sorted_models:
            acc = counts.get("accepted", 0)
            rev = counts.get("revised", 0)
            rej = counts.get("rejected", 0)
            work = counts.get("reworked", 0)
            total = acc + rev + rej + work
            acceptance = (acc / total * 100) if total > 0 else 0
            
            print(f"  {tier_model:30s} {acc:3d} acc {rev:2d} rev {rej:2d} rej {work:2d} rework  ({acceptance:.0f}% acceptance)")
    
    print()
    print("Recent Patterns:")
    
    by_confidence = {}
    for tier_model, counts in dist.items():
        total = sum(counts.values())
        if total > 0:
            acceptance = counts.get("accepted", 0) / total * 100
            by_confidence[tier_model] = acceptance
    
    if by_confidence:
        most_conf = max(by_confidence, key=by_confidence.get)
        least_conf = min(by_confidence, key=by_confidence.get)
        print(f"  Most confident model: {most_conf} ({by_confidence[most_conf]:.0f}% acceptance)")
        print(f"  Least confident model: {least_conf} ({by_confidence[least_conf]:.0f}% acceptance)")
    
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc(file=sys.stderr)
    raise SystemExit(1)
PY
}

_tgs_tune() {
    local mode="${1:-}"
    local project="."
    local force=0
    local key=""
    local value=""
    local pybin=""

    shift || true
    case "$mode" in
        show|set|reset) ;;
        *)
            echo "threnody tune: subcommand must be show, set, or reset" >&2
            _tgs_usage
            return 1
            ;;
    esac

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --project)
                [[ $# -lt 2 ]] && { echo "threnody tune: --project requires a path" >&2; return 1; }
                project="$2"
                shift 2
                ;;
            --force)
                force=1
                shift
                ;;
            -h|--help)
                _tgs_usage
                return 0
                ;;
            *)
                case "$mode" in
                    show)
                        if [[ -z "$key" ]]; then
                            key="$1"
                            shift
                            continue
                        fi
                        ;;
                    set)
                        if [[ -z "$key" ]]; then
                            key="$1"
                            shift
                            continue
                        fi
                        if [[ -z "$value" ]]; then
                            value="$1"
                            shift
                            continue
                        fi
                        ;;
                    reset)
                        if [[ -z "$key" ]]; then
                            key="$1"
                            shift
                            continue
                        fi
                        ;;
                esac
                echo "threnody tune: unknown argument: $1" >&2
                _tgs_usage
                return 1
                ;;
        esac
    done

    if [[ "$mode" == "set" && ( -z "$key" || -z "$value" ) ]]; then
        echo "threnody tune set: key and value are required" >&2
        return 1
    fi

    pybin=$(_tgs_python) || return 1
    ROUTER_DIR="$_ROUTER_DIR" TGS_ACTIVE_WORKSPACE="$project" "$pybin" - \
        "$project" "$mode" "$key" "$value" "$force" <<'PY'
import json
import os
import sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))

import mcp_server

project = sys.argv[1]
mode = sys.argv[2]
key = sys.argv[3] or None
value = sys.argv[4]
force = sys.argv[5] == "1"

if mode == "show":
    result = mcp_server.tune_show(project, key)
elif mode == "set":
    result = mcp_server.tune_set(project, key or "", value, force=force)
else:
    result = mcp_server.tune_reset(project, key)

if "error" in result:
    print(f"tune error: {result.get('error')} - {result.get('details', '')}".rstrip(), file=sys.stderr)
    raise SystemExit(1)

if mode == "show":
    settings = result.get("settings", {})
    if result.get("key"):
        print(f"{result.get('key')}={result.get('value')}")
    else:
        print(f"tune {result.get('project_id')}:")
        for field in (
            "learning_enabled",
            "concurrency_limit",
            "budget_hard_cap_tokens",
            "fanout_cap",
            "pending_approval_limit",
        ):
            print(f"  {field}={settings.get(field)}")
    raise SystemExit(0)

if result.get("warning"):
    print(f"warning: {result.get('warning')}")
if mode == "set" and not result.get("updated"):
    raise SystemExit(1)

if mode == "set":
    print(f"updated {result.get('project_id')}: {result.get('key')}={result.get('value')}")
else:
    reset_key = result.get("key") or "all"
    print(f"reset {result.get('project_id')}: {reset_key}")
PY
}

_tgs_serve() {
    local pybin
    pybin=$(_tgs_python) || return 1
    exec "$pybin" "$_ROUTER_DIR/shared/remote_server.py" "$@"
}

threnody() {
    local area="${1:-}"
    shift || true

_tgs_inspect_write_audit() {
    local limit=50
    local pybin=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --limit)
                [[ $# -lt 2 ]] && { echo "threnody inspect write-audit: --limit requires a value" >&2; return 1; }
                limit="$2"
                shift 2
                ;;
            -h|--help)
                echo "Usage: threnody inspect write-audit [--limit N]" >&2
                return 0
                ;;
            *)
                echo "threnody inspect write-audit: unknown argument: $1" >&2
                return 1
                ;;
        esac
    done

    pybin=$(_tgs_python) || return 1
    ROUTER_DIR="$_ROUTER_DIR" "$pybin" - "$limit" <<'PY'
import json, os, sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))
import mcp_server

limit = int(sys.argv[1])
result = mcp_server.handle_inspect_write_audit({"limit": limit})

if "error" in result:
    print(f"error: {result.get('error')}", file=sys.stderr)
    raise SystemExit(1)

entries = result.get("entries", [])
if not entries:
    print("write-audit: no out-of-workspace writes recorded")
    raise SystemExit(0)

import datetime
print(f"{'TIMESTAMP':<22}  {'GRANT REASON':<15}  {'TIER':<8}  {'PROVIDER':<16}  PATH")
print("-" * 90)
for e in entries:
    ts = e.get("ts", 0)
    dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "?"
    reason = e.get("grant_reason") or e.get("reason") or "?"
    tier = e.get("tier") or "?"
    provider = e.get("provider") or "?"
    fpath = e.get("path") or "?"
    print(f"{dt:<22}  {reason:<15}  {tier:<8}  {provider:<16}  {fpath}")
PY
}

    case "$area" in
        inspect)
            local mode="${1:-}"
            shift || true
            case "$mode" in
                status) _tgs_inspect_status "$@" ;;
                task) _tgs_inspect_task "$@" ;;
                approvals) _tgs_inspect_approvals "$@" ;;
                write-audit) _tgs_inspect_write_audit "$@" ;;
                leases|deadletters)
                    local pybin=""
                    pybin=$(_tgs_python) || return 1
                    (cd "$_ROUTER_DIR" && "$pybin" -m cli.inspect "$subcommand" "$@")
                    return $?
                    ;;
                ""|-h|--help) _tgs_usage ;;
                *)
                    echo "threnody inspect: unknown subcommand: $mode" >&2
                    _tgs_usage
                    return 1
                    ;;
            esac
            ;;
        learning)
            local mode="${1:-}"
            shift || true
            case "$mode" in
                summary) _tgs_learning_summary "$@" ;;
                ""|-h|--help) _tgs_usage ;;
                *)
                    echo "threnody learning: subcommand must be summary" >&2
                    return 1
                    ;;
            esac
            ;;
        tune)
            _tgs_tune "$@"
            ;;
        settings)
            local pybin
            pybin=$(_tgs_python) || return 1
            "$pybin" "$_ROUTER_DIR/shared/settings_wizard.py" "$_ROUTER_DIR/config.yaml"
            ;;
        serve)
            _tgs_serve "$@"
            ;;
        eval)
            local subcmd="${1:-}"
            local pybin=""
            shift || true
            case "$subcmd" in
                run)
                    # Forward all args to the python runner; ensure test mode
                    pybin=$(_tgs_python) || return 1
                    export THRENODY_TEST_MODE=1
                    # Invoke runner module, preserving stdout/stderr and exit code
                    (
                        cd "$_ROUTER_DIR" || exit 1
                        "$pybin" -m shared.routing_eval "$@"
                    )
                    return $?
                    ;;
                baseline)
                    pybin=$(_tgs_python) || return 1
                    export THRENODY_TEST_MODE=1
                    (
                        cd "$_ROUTER_DIR" || exit 1
                        "$pybin" -m shared.eval_baseline
                    )
                    exit_code=$?
                    # Compact summary
                    ROUTER_DIR="$_ROUTER_DIR" "$pybin" - <<'PY'
import json
import os
import sys
from pathlib import Path

router_dir = os.environ.get("ROUTER_DIR", "")
if not router_dir:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    sys.exit(2)
p = Path(router_dir) / "tests/eval/baseline.json"
try:
    d=json.load(open(p))
    print(f"Baseline written: {p} | fixtures={len(d.get('fixtures',[]))} | config_hash_present={'config_hash' in d} | schema_version_present={'schema_version' in d}")
except Exception as e:
    print('Baseline capture failed:', e)
    sys.exit(2)
PY
                    summary_exit=$?
                    if [[ $exit_code -ne 0 ]]; then
                        return $exit_code
                    fi
                    return $summary_exit
                    ;;
                ""|-h|--help)
                    echo "Usage: threnody eval {run|baseline} [--filter ...]" >&2
                    return 0
                    ;;
                bandit)
                    pybin=$(_tgs_python) || return 1
                    (cd "$_ROUTER_DIR" && "$pybin" -m cli.eval bandit "$@")
                    return $?
                    ;;
                *)
                    echo "Usage: threnody eval {run|baseline|bandit} [--filter ...]" >&2
                    return 2
                    ;;
            esac
            ;;
        doctor)
            local pybin=""
            pybin=$(_tgs_python) || return 1
            (cd "$_ROUTER_DIR" && "$pybin" -m shared.doctor "$@")
            return $?
            ;;
        users)
            _tgs_users "$@"
            ;;
        except)
            _tgs_except "$@"
            ;;
        trace)
            local pybin=""
            pybin=$(_tgs_python) || return 1
            (cd "$_ROUTER_DIR" && "$pybin" -m cli.trace "$@")
            return $?
            ;;
        policy)
            local pybin=""
            pybin=$(_tgs_python) || return 1
            (cd "$_ROUTER_DIR" && "$pybin" -m cli.policy "$@")
            return $?
            ;;
        gain)
            local pybin=""
            pybin=$(_tgs_python) || return 1
            (cd "$_ROUTER_DIR" && "$pybin" -m cli.gain "$@")
            return $?
            ;;
        audit)
            local subcmd="${1:-}"
            shift || true
            local pybin=""
            pybin=$(_tgs_python) || return 1
            case "$subcmd" in
                verify|export)
                    (cd "$_ROUTER_DIR" && "$pybin" -m cli.audit "$subcmd" "$@")
                    return $?
                    ;;
                ""|-h|--help)
                    echo "Usage: threnody audit {verify|export} [--tables TABLE...] [--quiet] [--output FILE]" >&2
                    return 0
                    ;;
                *)
                    echo "threnody audit: unknown subcommand: $subcmd" >&2
                    return 1
                    ;;
            esac
            ;;
        db)
            local subcmd="${1:-}"
            shift || true
            local pybin=""
            pybin=$(_tgs_python) || return 1
            case "$subcmd" in
                check|repair|backup|prune)
                    (cd "$_ROUTER_DIR" && "$pybin" -m shared.db_cli "$subcmd" "$@")
                    return $?
                    ;;
                ""|-h|--help)
                    echo "Usage: threnody db {check|repair|backup|prune} [--db PATH] [--keep N]" >&2
                    return 0
                    ;;
                *)
                    echo "threnody db: unknown subcommand: $subcmd" >&2
                    return 1
                    ;;
            esac
            ;;
        ""|-h|--help)
            _tgs_usage
            ;;
        *)
            echo "tgs: unknown area: $area" >&2
            _tgs_usage
            return 1
            ;;
    esac
}

switchyard() {
    echo "switchyard is deprecated; use threnody" >&2
    threnody "$@"
}

# ---------------------------------------------------------------------------
# threnody except — manage routing bypass rules
# ---------------------------------------------------------------------------

_tgs_except() {
    local subcmd="${1:-}"
    shift || true

    local pybin=""
    pybin=$(_tgs_python) || return 1

    case "$subcmd" in
        list)
            ROUTER_DIR="$_ROUTER_DIR" "$pybin" - <<'PY'
import json, os, sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))
import mcp_server

result = mcp_server.handle_routing_exception_list({})
if "error" in result:
    print(f"error: {result.get('error')}: {result.get('details', '')}", file=sys.stderr)
    raise SystemExit(1)

rows = result.get("exceptions", [])
if not rows:
    print("No routing exceptions configured.")
    print("  Add one with: threnody except add <type> <pattern>")
    raise SystemExit(0)

import datetime
print(f"{'TYPE':<12}  {'PATTERN':<30}  {'NOTE':<30}  ADDED")
print("-" * 90)
for r in rows:
    ts = r.get("created_at") or 0
    dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
    note = (r.get("note") or "")[:28]
    pat  = (r.get("pattern") or "")[:28]
    typ  = (r.get("exception_type") or "")[:10]
    print(f"{typ:<12}  {pat:<30}  {note:<30}  {dt}")
PY
            ;;
        add)
            local exc_type="" pattern="" note=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --note)
                        [[ $# -lt 2 ]] && { echo "threnody except add: --note requires a value" >&2; return 1; }
                        note="$2"; shift 2 ;;
                    -h|--help)
                        cat >&2 <<'HELP'
Usage: threnody except add <type> <pattern> [--note TEXT]

Types:
  skill      Match the skill name passed to validate_routing_guard (glob ok)
  filetype   Match the file extension of the target file (e.g. .md)
  project    Match the working directory prefix (e.g. /home/me/notes)
  command    Match the tool_name (e.g. Write, Edit)
  caller     Match the resolved caller (e.g. github-copilot)
  path       Match the target file path prefix

Examples:
  threnody except add skill "auto-time"
  threnody except add skill "tgsd-*"
  threnody except add filetype ".md"
  threnody except add project "/home/me/notes"
  threnody except add caller "github-copilot"
HELP
                        return 0 ;;
                    *)
                        if [[ -z "$exc_type" ]]; then exc_type="$1"; shift
                        elif [[ -z "$pattern" ]]; then pattern="$1"; shift
                        else echo "threnody except add: unexpected argument: $1" >&2; return 1; fi ;;
                esac
            done
            [[ -z "$exc_type" ]] && { echo "threnody except add: <type> is required" >&2; return 1; }
            [[ -z "$pattern" ]] && { echo "threnody except add: <pattern> is required" >&2; return 1; }
            ROUTER_DIR="$_ROUTER_DIR" "$pybin" - "$exc_type" "$pattern" "$note" <<'PY'
import json, os, sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))
import mcp_server

exc_type = sys.argv[1]
pattern  = sys.argv[2]
note     = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None

result = mcp_server.handle_routing_exception_add({
    "exception_type": exc_type,
    "pattern": pattern,
    "note": note,
})
if "error" in result:
    print(f"error: {result.get('error')}: {result.get('details', '')}", file=sys.stderr)
    raise SystemExit(1)
exc = result.get("exception", {})
print(f"✓ Added routing exception: type={exc.get('exception_type')!r}  pattern={exc.get('pattern')!r}")
PY
            ;;
        remove)
            local exc_type="" pattern=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    -h|--help)
                        echo "Usage: threnody except remove <type> <pattern>" >&2
                        return 0 ;;
                    *)
                        if [[ -z "$exc_type" ]]; then exc_type="$1"; shift
                        elif [[ -z "$pattern" ]]; then pattern="$1"; shift
                        else echo "threnody except remove: unexpected argument: $1" >&2; return 1; fi ;;
                esac
            done
            [[ -z "$exc_type" ]] && { echo "threnody except remove: <type> is required" >&2; return 1; }
            [[ -z "$pattern" ]] && { echo "threnody except remove: <pattern> is required" >&2; return 1; }
            ROUTER_DIR="$_ROUTER_DIR" "$pybin" - "$exc_type" "$pattern" <<'PY'
import json, os, sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))
import mcp_server

exc_type = sys.argv[1]
pattern  = sys.argv[2]

result = mcp_server.handle_routing_exception_remove({
    "exception_type": exc_type,
    "pattern": pattern,
})
if "error" in result:
    print(f"error: {result.get('error')}: {result.get('details', '')}", file=sys.stderr)
    raise SystemExit(1)
if result.get("removed"):
    print(f"✓ Removed routing exception: type={exc_type!r}  pattern={pattern!r}")
else:
    print(f"No matching exception found for type={exc_type!r}  pattern={pattern!r}")
PY
            ;;
        ""|-h|--help)
            cat >&2 <<'HELP'
Usage: threnody except <subcommand>

Subcommands:
  list                       Show all active routing exceptions
  add <type> <pattern>       Add a bypass rule (supports * globs)
  remove <type> <pattern>    Remove a bypass rule

Exception types: skill, filetype, project, command, caller, path

Examples:
  threnody except list
  threnody except add skill "auto-time"
  threnody except add skill "tgsd-*"
  threnody except add filetype ".md"
  threnody except add project "/home/me/notes"
  threnody except remove skill "auto-time"
HELP
            ;;
        *)
            echo "threnody except: unknown subcommand: $subcmd" >&2
            echo "  Use 'threnody except --help' for usage." >&2
            return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# threnody users — multi-user management for threnody serve
# ---------------------------------------------------------------------------

_tgs_users() {
    local subcmd="${1:-}"
    shift || true

    local pybin=""
    pybin=$(_tgs_python) || return 1

    case "$subcmd" in
        add)
            local username="" providers_file=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --providers)
                        [[ $# -lt 2 ]] && { echo "threnody users add: --providers requires a file path" >&2; return 1; }
                        providers_file="$2"; shift 2 ;;
                    -h|--help)
                        echo "Usage: threnody users add <username> [--providers FILE]" >&2
                        echo "  FILE: JSON file with provider credentials (see threnody users --help)" >&2
                        return 0 ;;
                    *)
                        if [[ -z "$username" ]]; then username="$1"; shift
                        else echo "threnody users add: unexpected argument: $1" >&2; return 1; fi ;;
                esac
            done
            [[ -z "$username" ]] && { echo "threnody users add: username is required" >&2; return 1; }
            ROUTER_DIR="$_ROUTER_DIR" "$pybin" - "$username" "$providers_file" <<'PY'
import json, os, secrets, sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))
from shared.db import Database

username = sys.argv[1]
providers_file = sys.argv[2] if len(sys.argv) > 2 else ""

providers_json = "{}"
if providers_file:
    try:
        providers_json = Path(providers_file).read_text()
        json.loads(providers_json)  # validate
    except Exception as e:
        print(f"error: could not read providers file: {e}", file=sys.stderr)
        raise SystemExit(1)

token = secrets.token_urlsafe(32)
admin_secret = os.environ.get("THRENODY_SERVER_TOKEN", "")
db = Database()
try:
    user_id = db.create_user(username, token, providers_json, secret=admin_secret)
except Exception as e:
    print(f"error: {e}", file=sys.stderr)
    raise SystemExit(1)

print(f"user created")
print(f"  id:       {user_id}")
print(f"  username: {username}")
print(f"  token:    {token}")
print()
if not admin_secret:
    print("WARNING: THRENODY_SERVER_TOKEN not set — token stored unhashed.")
    print("Set THRENODY_SERVER_TOKEN before starting 'threnody serve' for secure operation.")
else:
    print("Share the token with the user — it cannot be retrieved later.")
PY
            ;;
        list)
            ROUTER_DIR="$_ROUTER_DIR" "$pybin" - <<'PY'
import json, os, sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))
from shared.db import Database

db = Database()
users = db.list_users()
if not users:
    print("no users registered")
    raise SystemExit(0)

print(f"{'ID':<36}  {'USERNAME':<20}  {'ENABLED':<8}  CREATED")
print("-" * 80)
import datetime
for u in users:
    ts = u.get("created_ts") or 0
    dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
    enabled = "yes" if u.get("enabled") else "no"
    print(f"{u.get('user_id', ''):<36}  {u.get('username', ''):<20}  {enabled:<8}  {dt}")
PY
            ;;
        show)
            local target="${1:-}"
            [[ -z "$target" ]] && { echo "threnody users show: username or id required" >&2; return 1; }
            ROUTER_DIR="$_ROUTER_DIR" "$pybin" - "$target" <<'PY'
import json, os, sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))
from shared.db import Database

target = sys.argv[1]
db = Database()
user = db.get_user_by_username(target) or db.get_user_by_id(target)
if user is None:
    print(f"error: user {target!r} not found", file=sys.stderr)
    raise SystemExit(1)

import datetime
ts = user.get("created_ts") or 0
dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "?"
enabled = "yes" if user.get("enabled") else "no"
print(f"id:         {user.get('user_id', '')}")
print(f"username:   {user.get('username', '')}")
print(f"enabled:    {enabled}")
print(f"created:    {dt}")
raw = user.get("providers_json") or "{}"
try:
    providers = list(json.loads(raw).keys())
    print(f"providers:  {', '.join(providers) if providers else '(none)'}")
except Exception:
    print(f"providers:  (unparseable)")
PY
            ;;
        token)
            local target="${1:-}"
            [[ -z "$target" ]] && { echo "threnody users token: username or id required" >&2; return 1; }
            ROUTER_DIR="$_ROUTER_DIR" "$pybin" - "$target" <<'PY'
import json, os, secrets, sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))
from shared.db import Database

target = sys.argv[1]
admin_secret = os.environ.get("THRENODY_SERVER_TOKEN", "")
db = Database()
user = db.get_user_by_username(target) or db.get_user_by_id(target)
if user is None:
    print(f"error: user {target!r} not found", file=sys.stderr)
    raise SystemExit(1)

new_token = secrets.token_urlsafe(32)
db.update_user_token_hmac(user.get("user_id", ""), new_token, secret=admin_secret)
print(f"new token for {user.get('username', target)}: {new_token}")
if not admin_secret:
    print("WARNING: THRENODY_SERVER_TOKEN not set — token stored unhashed.")
else:
    print("Previous token is now invalid. Share this token with the user.")
PY
            ;;
        disable)
            local target="${1:-}"
            [[ -z "$target" ]] && { echo "threnody users disable: username or id required" >&2; return 1; }
            ROUTER_DIR="$_ROUTER_DIR" "$pybin" - "$target" "0" <<'PY'
import os, sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))
from shared.db import Database

target, flag = sys.argv[1], sys.argv[2]
db = Database()
user = db.get_user_by_username(target) or db.get_user_by_id(target)
if user is None:
    print(f"error: user {target!r} not found", file=sys.stderr)
    raise SystemExit(1)
db.set_user_enabled(user.get("user_id", ""), flag == "1")
action = "enabled" if flag == "1" else "disabled"
print(f"user {user.get('username', target)} {action}")
PY
            ;;
        enable)
            local target="${1:-}"
            [[ -z "$target" ]] && { echo "threnody users enable: username or id required" >&2; return 1; }
            ROUTER_DIR="$_ROUTER_DIR" "$pybin" - "$target" "1" <<'PY'
import os, sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))
from shared.db import Database

target, flag = sys.argv[1], sys.argv[2]
db = Database()
user = db.get_user_by_username(target) or db.get_user_by_id(target)
if user is None:
    print(f"error: user {target!r} not found", file=sys.stderr)
    raise SystemExit(1)
db.set_user_enabled(user.get("user_id", ""), flag == "1")
action = "enabled" if flag == "1" else "disabled"
print(f"user {user.get('username', target)} {action}")
PY
            ;;
        remove)
            local target="${1:-}"
            [[ -z "$target" ]] && { echo "threnody users remove: username or id required" >&2; return 1; }
            ROUTER_DIR="$_ROUTER_DIR" "$pybin" - "$target" <<'PY'
import os, sys
from pathlib import Path

base = Path(os.environ.get("ROUTER_DIR", "")).expanduser().resolve()
if not base.name:
    print("error: ROUTER_DIR not set", file=sys.stderr)
    raise SystemExit(1)
sys.path.insert(0, str(base))
from shared.db import Database

target = sys.argv[1]
db = Database()
user = db.get_user_by_username(target) or db.get_user_by_id(target)
if user is None:
    print(f"error: user {target!r} not found", file=sys.stderr)
    raise SystemExit(1)
db.delete_user(user.get("user_id", ""))
print(f"user {user.get('username', target)} removed")
PY
            ;;
        -h|--help|"")
            cat >&2 <<'HELP'
Usage: threnody users <subcommand> [args]

Manage users for the threnody serve remote server.

Subcommands:
  add <username> [--providers FILE]   Register a new user, print their token
  list                                List all registered users
  show <username|id>                  Show details for a user
  token <username|id>                 Rotate and print a new token for a user
  disable <username|id>               Disable a user (token rejected until re-enabled)
  enable <username|id>                Re-enable a disabled user
  remove <username|id>                Permanently remove a user

Provider credentials file format (--providers FILE):
  {
    "copilot": { "auth_files": { "auth.json": "<json content>" } },
    "claude":  { "env": { "ANTHROPIC_API_KEY": "sk-..." } },
    "gemini":  { "env": { "GEMINI_API_KEY": "AIza..." } },
    "codex":   { "env": { "OPENAI_API_KEY": "sk-..." } }
  }

HELP
            ;;
        *)
            echo "threnody users: unknown subcommand: $subcmd" >&2
            echo "Run 'threnody users --help' for usage." >&2
            return 1
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------
alias ghcs="ghc suggest"
alias ghce="ghc explain"
alias ghcag="ghc agent"
alias ghcw="ghc --stats"

# ---------------------------------------------------------------------------
# ? — natural language shorthand
# ---------------------------------------------------------------------------
if [[ -n "${ZSH_VERSION:-}" ]]; then
    eval '
    function ? {
        if [[ $# -eq 0 ]]; then
            echo "Usage: ? <natural language prompt>" >&2
            return 1
        fi

        local prompt="$*"
        local first
        first=$(printf "%s\n" "$prompt" | awk "{print tolower(\$1)}")

        case "$first" in
            explain|what|how|why|when|where|which|who|does|is|are|can|should|will)
                ghc explain "$prompt"
                ;;
            *)
                ghc agent "$prompt"
                ;;
        esac
    }
    '
fi

# ---------------------------------------------------------------------------
# Natural language detection — preexec hook (zsh only)
# ---------------------------------------------------------------------------
GHC_HOOK="${GHC_HOOK:-1}"

_GHC_TRIGGER_WORDS=(
    implement add fix refactor redesign
    design architect create build generate
    explain how what why when where which
    migrate convert integrate deploy
    write update remove delete rename
    optimise optimize improve extend
    document review analyse analyze
    debug trace profile benchmark
)

_ghc_is_natural_language() {
    local cmd="$1"
    [[ "$GHC_HOOK" == "0" ]] && return 1
    command -v gh &>/dev/null || return 1

    local first_char="${cmd:0:1}"
    case "$first_char" in
        [./~\$#0-9-]) return 1 ;;
    esac

    [[ "$cmd" == \#* ]] && return 1

    case "$cmd" in
        *\|*|*">"*|*"<"*|*"&"*|*";"*|*'`'*|*'$'*|*"("*|*")"*) return 1 ;;
    esac

    local first_word word_count
    first_word=$(echo "$cmd" | awk '{print tolower($1)}')
    word_count=$(echo "$cmd" | wc -w | tr -d ' ')

    case "$first_word" in
        cp|mv|rm|mkdir|cd|ls|cat|grep|find|chmod|chown|echo|source|export|git|python3|pip|brew|npm|cargo|rustc) return 1 ;;
    esac

    local w
    for w in "${_GHC_TRIGGER_WORDS[@]}"; do
        if [[ "$first_word" == "$w" ]]; then
            [[ $word_count -ge 2 ]] && return 0 || return 1
        fi
    done

    [[ $word_count -lt 3 ]] && return 1
    command -v "$first_word" &>/dev/null 2>&1 && return 1
    type "$first_word" &>/dev/null 2>&1 && return 1

    return 0
}

if [[ -n "$ZSH_VERSION" ]]; then
    _ghc_preexec() {
        local cmd="$1"
        [[ ${#cmd} -gt 500 ]] && return
        if _ghc_is_natural_language "$cmd"; then
            echo "" >&2
            echo "💬 Natural language detected — routing to agent ensemble..." >&2
            ghc agent "$cmd"
        fi
    }

    autoload -Uz add-zsh-hook 2>/dev/null
    if declare -f add-zsh-hook &>/dev/null; then
        add-zsh-hook preexec _ghc_preexec
    else
        preexec_functions+=(_ghc_preexec)
    fi
fi
