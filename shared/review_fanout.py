"""Per-file x dimension review fanout for the threnody-swarm-review skill.

Called by build_heuristic_plan_payload when the task starts with the REVIEW: sentinel.
Produces a DAG plan: one subtask per (file, dimension) + a synthesis subtask.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

_REVIEW_SENTINEL = "REVIEW:"

_LOC_TRIVIAL = 50
_LOC_COMPLEX = 200

_RISKY_EXTENSIONS = frozenset({".py", ".js", ".ts", ".go", ".rb", ".java", ".php", ".cs", ".cpp", ".c"})

_RISK_SIGNALS = re.compile(
    r"\b(?:sql|exec\s*\(|eval\s*\(|subprocess|os\.system|auth(?:enticate)?|"
    r"password|secret|credential|token|query|cursor\.execute|raw_query|shell\s*=\s*True|"
    r"pickle\.loads|yaml\.load\s*\()\b",
    re.IGNORECASE,
)


class _Dim(NamedTuple):
    key: str
    subagent_type: str
    prompt_template: str
    drop_priority: int  # higher = drop first; 0 = never drop


REVIEW_DIMENSIONS: list[_Dim] = [
    _Dim(
        key="security",
        subagent_type="review-security",
        prompt_template=(
            "Security review of {path}: check for injection (SQL, command, XSS), "
            "auth bypass, hardcoded secrets, SSRF, path traversal, weak crypto, "
            "CSRF, IDOR, insecure deserialization, and input validation gaps. "
            "Report each finding as: ⚠️ [SEVERITY] security — file:line — description (CWE-XXX). "
            "Output nothing if no issues found."
        ),
        drop_priority=0,
    ),
    _Dim(
        key="logic",
        subagent_type="review-logic",
        prompt_template=(
            "Logic review of {path}: check for off-by-one errors, wrong conditions, "
            "unreachable code, swapped arguments, missing returns, and state invariant violations. "
            "Report each finding as: ⚠️ [SEVERITY] logic — file:line — description. "
            "Output nothing if no issues found."
        ),
        drop_priority=1,
    ),
    _Dim(
        key="edge",
        subagent_type="review-edge-cases",
        prompt_template=(
            "Edge and null case review of {path}: check for null/None dereferences, "
            "empty collection access, division by zero, missing error handling, "
            "missing defaults, boundary conditions, and missing I/O error handling. "
            "Report each finding as: ⚠️ [SEVERITY] edge — file:line — description. "
            "Output nothing if no issues found."
        ),
        drop_priority=2,
    ),
    _Dim(
        key="types",
        subagent_type="review-types",
        prompt_template=(
            "Type safety review of {path}: check for type mismatches, unsafe casts, "
            "generic violations, incompatible return types, and serialization/deserialization drift. "
            "Report each finding as: ⚠️ [SEVERITY] types — file:line — description. "
            "Output nothing if no issues found."
        ),
        drop_priority=3,
    ),
    _Dim(
        key="performance",
        subagent_type="review-performance",
        prompt_template=(
            "Performance review of {path}: check for O(n²) algorithms, N+1 queries, "
            "memory leaks, blocking I/O in async contexts, unbounded growth, missing pagination, "
            "and redundant calls. "
            "Report each finding as: ⚠️ [SEVERITY] performance — file:line — description. "
            "Output nothing if no issues found."
        ),
        drop_priority=4,
    ),
]

_DIM_BY_KEY: dict[str, _Dim] = {d.key: d for d in REVIEW_DIMENSIONS}

_SYNTHESIS_PROMPT = """\
You are the synthesis agent for a multi-dimension code review swarm.
Your context contains output_excerpt summaries from each review agent that ran in prior waves.
Collect all reported findings and produce a unified ranked report.

## Format

### Summary
N critical, N high, N medium, N low issues across N files.

### Findings (ranked: critical → high → medium → low; then security > logic > edge > types > performance)

⚠️ [SEVERITY] category — file:line — description [(CWE-XXX)]

Deduplicate: if the same issue appears in multiple dimension reviews, keep the highest severity instance.
Output "No issues found." if all dimension agents reported clean.
"""


def is_review_intent(task: str) -> bool:
    """True when the task carries the REVIEW: sentinel injected by the skill."""
    if not isinstance(task, str):
        return False
    return task.strip().upper().startswith(_REVIEW_SENTINEL)


def _read_file_safe(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _count_loc(content: str) -> int:
    return sum(1 for line in content.splitlines() if line.strip())


def _has_risk_signals(content: str) -> bool:
    return bool(_RISK_SIGNALS.search(content))


Complexity = str  # "trivial" | "moderate" | "complex"


def estimate_complexity(path: str) -> tuple[Complexity, bool]:
    """Return (band, has_risk) for path.

    band: "trivial" | "moderate" | "complex"
    has_risk: True when content contains known security-risk patterns.
    Unreadable files default to ("moderate", False).
    """
    content = _read_file_safe(path)
    if content is None:
        return "moderate", False

    loc = _count_loc(content)
    risk = _has_risk_signals(content)
    risky_ext = Path(path).suffix.lower() in _RISKY_EXTENSIONS

    if loc < _LOC_TRIVIAL:
        band: Complexity = "trivial"
    elif loc > _LOC_COMPLEX:
        band = "complex"
    else:
        band = "moderate"

    # Bump band when risk signals or risky extension present
    if risk or risky_ext:
        if band == "trivial":
            band = "moderate"
        elif band == "moderate":
            band = "complex"

    return band, risk


def dimensions_for(band: Complexity, has_risk: bool) -> list[_Dim]:
    """Dimensions to run for a given complexity band + risk flag."""
    if band == "trivial":
        keys = ["logic", "edge"]
    elif band == "moderate":
        keys = ["logic", "edge", "types"]
    else:  # complex
        keys = ["logic", "edge", "types", "security", "performance"]

    if has_risk and "security" not in keys:
        keys = ["security"] + keys

    return [_DIM_BY_KEY[k] for k in keys]


def tier_for(dim: _Dim, band: Complexity, has_risk: bool) -> str:
    """Routing tier for a dimension + band combination."""
    if dim.key == "security" and (band == "complex" or has_risk):
        return "high"
    if band == "trivial":
        return "low"
    return "medium"


def build_review_subtasks(
    entries: list[tuple[str, str]],
    task: str,
    *,
    max_agents: int | None = None,
) -> dict:
    """Build a DAG plan dict with per-(file, dimension) subtasks + synthesis.

    entries: (path, description_hint) pairs from extract_task_file_entries.
    task: original REVIEW: ... task string.
    max_agents: hard cap; lowest-priority dimensions dropped first.
    """
    if not entries:
        return {
            "analysis": "Review fanout: no files found in task.",
            "subtasks": [
                {
                    "id": 1,
                    "description": task.strip(),
                    "tier": "medium",
                    "depends_on": [],
                }
            ],
            "strategy": "sequential",
            "topology": "linear",
        }

    # Compute per-file (dims, band, risk)
    file_dims: list[tuple[str, list[_Dim], Complexity, bool]] = []
    for path, _ in entries:
        band, has_risk = estimate_complexity(path)
        dims = dimensions_for(band, has_risk)
        file_dims.append((path, dims, band, has_risk))

    # Flatten to (path, dim, band, has_risk) ordered by never-drop first
    all_cells: list[tuple[str, _Dim, Complexity, bool]] = []
    for path, dims, band, has_risk in file_dims:
        for dim in sorted(dims, key=lambda d: d.drop_priority):
            all_cells.append((path, dim, band, has_risk))

    # Cap: drop highest drop_priority cells first; reserve 1 slot for synthesis
    if max_agents is not None and max_agents > 0:
        review_cap = max(1, max_agents - 1)
        if len(all_cells) > review_cap:
            by_priority = sorted(
                range(len(all_cells)),
                key=lambda i: all_cells[i][1].drop_priority,
                reverse=True,
            )
            n_drop = len(all_cells) - review_cap
            drop_indices = set(by_priority[:n_drop])
            dropped_labels = [
                f"{all_cells[i][0]}:{all_cells[i][1].key}" for i in by_priority[:n_drop]
            ]
            log.info(
                "review_fanout: max_agents=%d — dropping %d dimension(s): %s",
                max_agents,
                n_drop,
                ", ".join(dropped_labels),
            )
            all_cells = [c for i, c in enumerate(all_cells) if i not in drop_indices]

    subtasks: list[dict] = []
    review_ids: list[int] = []
    for idx, (path, dim, band, has_risk) in enumerate(all_cells, start=1):
        t = tier_for(dim, band, has_risk)
        subtasks.append({
            "id": idx,
            "description": dim.prompt_template.format(path=path),
            "tier": t,
            "target_file": path,
            "subagent_type": dim.subagent_type,
            "read_only": True,
            "depends_on": [],
            "single_file_insertion": False,
        })
        review_ids.append(idx)

    synth_id = len(all_cells) + 1
    reviewed_files = sorted({path for path, _, _, _ in all_cells})
    subtasks.append({
        "id": synth_id,
        "description": (
            _SYNTHESIS_PROMPT
            + f"\n\nFiles reviewed: {', '.join(reviewed_files)}"
        ),
        "tier": "high",
        "depends_on": review_ids,
        "subagent_type": "",  # empty → resolved to threnody-high by tier in host_spawn
        "read_only": True,
    })

    n_files = len(entries)
    n_dims = len(all_cells)
    return {
        "analysis": (
            f"Review fanout: {n_files} file(s), {n_dims} dimension agent(s) + 1 synthesis. "
            "Host-native DAG. No external planner LLM was called."
        ),
        "subtasks": subtasks,
        "strategy": "dag",
        "topology": "dag",
    }
