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
_FAST_REVIEW_SENTINEL = "FAST_REVIEW:"

_LOC_TRIVIAL = 50
_LOC_COMPLEX = 200

_RISKY_EXTENSIONS = frozenset({".py", ".js", ".ts", ".go", ".rb", ".java", ".php", ".cs", ".cpp", ".c"})

_RISK_SIGNALS = re.compile(
    r"(?:\b(?:sql|subprocess|os\.system|auth(?:enticate|entication|orization)?|"
    r"crypto|cryptograph(?:y|ic)|encrypt(?:ion)?|decrypt(?:ion)?|payment|billing|card|"
    r"password|secret|credential|token|api[_ -]?key|rce|remote code execution|"
    r"cursor\.execute|raw_query|shell\s*=\s*True|deseriali[sz](?:e|ation)|"
    r"pickle\.loads|ssrf|server-side request forgery|"
    r"path traversal|directory traversal)\b|\b(?:exec|eval)\s*\(|\byaml\.load\s*\()",
    re.IGNORECASE,
)

_HIGH_REVIEW_TASK_SIGNALS = re.compile(
    r"\b(?:deep(?:\s+security)?\s+review|threat[-\s]?model(?:ing)?|"
    r"security[-\s]?critical|critical\s+security|high[-\s]?risk)\b",
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
    normalized = task.strip().upper()
    return normalized.startswith(_REVIEW_SENTINEL) or normalized.startswith(_FAST_REVIEW_SENTINEL)


def is_fast_review_intent(task: str) -> bool:
    """True for the fast one-agent-per-file review override."""
    if not isinstance(task, str):
        return False
    return task.strip().upper().startswith(_FAST_REVIEW_SENTINEL)


def _read_file_safe(path: str) -> str | None:
    # Delegate to the shared mtime+size-keyed cache so the bytes read here for
    # complexity estimation are reused by per-cell context enrichment instead
    # of being re-read from disk. max_bytes=None preserves the prior uncapped
    # read, so banding for large files is unchanged.
    from .context import read_source_cached

    return read_source_cached(Path(path), max_bytes=None)


def _count_loc(content: str) -> int:
    return sum(1 for line in content.splitlines() if line.strip())


def _has_risk_signals(content: str) -> bool:
    return bool(_RISK_SIGNALS.search(content))


def _task_requests_high_tier(task: str) -> bool:
    return bool(_HIGH_REVIEW_TASK_SIGNALS.search(task))


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


def tier_for(dim: _Dim, band: Complexity, has_risk: bool, force_high: bool = False) -> str:
    """Routing tier for a dimension + band combination."""
    if dim.key == "security" and (has_risk or force_high):
        return "high"
    if band == "trivial":
        return "low"
    return "medium"


def synthesis_tier(requires_high: bool) -> str:
    """Routing tier for review synthesis."""
    return "high" if requires_high else "medium"


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

    if is_fast_review_intent(task):
        return build_fast_review_subtasks(entries, task, max_agents=max_agents)

    task_force_high = _task_requests_high_tier(task)

    # Compute per-file (dims, band, risk)
    file_dims: list[tuple[str, list[_Dim], Complexity, bool]] = []
    for path, _ in entries:
        band, has_risk = estimate_complexity(path)
        dims = dimensions_for(band, has_risk)
        if task_force_high and not any(dim.key == "security" for dim in dims):
            dims = [_DIM_BY_KEY["security"]] + dims
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
    review_requires_high = task_force_high or any(has_risk for _, _, _, has_risk in all_cells)

    for idx, (path, dim, band, has_risk) in enumerate(all_cells, start=1):
        t = tier_for(dim, band, has_risk, force_high=task_force_high)
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
        "tier": synthesis_tier(review_requires_high),
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


def build_fast_review_subtasks(
    entries: list[tuple[str, str]],
    task: str,
    *,
    max_agents: int | None = None,
) -> dict:
    """Build one read-only review agent per file plus synthesis.

    This override trades depth for throughput: one agent owns logic, security,
    edge, type, and performance review for a single file. It is intended for
    broad review sweeps where per-file parallelism matters more than per-dimension
    depth.
    """
    file_entries = list(entries)
    dropped = 0
    if max_agents is not None and max_agents > 0:
        review_cap = max(1, max_agents - 1)
        if len(file_entries) > review_cap:
            dropped = len(file_entries) - review_cap
            file_entries = file_entries[:review_cap]

    subtasks: list[dict] = []
    review_ids: list[int] = []
    task_force_high = _task_requests_high_tier(task)
    file_risks: list[bool] = []
    for idx, (path, _hint) in enumerate(file_entries, start=1):
        band, has_risk = estimate_complexity(path)
        file_risks.append(has_risk)
        tier = "high" if has_risk or task_force_high else "medium"
        subtasks.append({
            "id": idx,
            "description": (
                f"Fast full-file review of {path}: check logic, security, edge/null cases, "
                "type safety, and performance. Report only concrete findings as: "
                "⚠️ [SEVERITY] category — file:line — description [(CWE-XXX)]. "
                "Output nothing if no issues found."
            ),
            "tier": tier,
            "target_file": path,
            "subagent_type": "review-fast-file",
            "read_only": True,
            "depends_on": [],
            "single_file_insertion": False,
        })
        review_ids.append(idx)

    synth_id = len(file_entries) + 1
    reviewed_files = [path for path, _ in file_entries]
    review_requires_high = task_force_high or any(file_risks)
    subtasks.append({
        "id": synth_id,
        "description": (
            _SYNTHESIS_PROMPT
            + f"\n\nFast review mode: one review agent per file. Files reviewed: {', '.join(reviewed_files)}"
        ),
        "tier": synthesis_tier(review_requires_high),
        "depends_on": review_ids,
        "subagent_type": "",
        "read_only": True,
    })

    return {
        "analysis": (
            f"Fast review fanout: {len(file_entries)} file agent(s) + 1 synthesis. "
            f"Dropped {dropped} file(s) due to max_agents cap. "
            "Host-native DAG. No external planner LLM was called."
        ),
        "subtasks": subtasks,
        "strategy": "dag",
        "topology": "dag",
        "review_mode": "fast_file",
        "dropped_file_count": dropped,
    }
