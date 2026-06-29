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

# Raw-LOC thresholds for per-agent tier selection (independent of the risk-bumped
# band used for dimension selection). Small files get a cheap low-tier reviewer;
# large reasoning-heavy dimensions escalate to high.
_LOC_LOW = 230
_LOC_HIGH = 600

# Structural-density cutoffs for tier selection. density_score (0.0–1.0) blends
# nesting depth, branch density, and definition surface — see _structural_density.
# A dense reasoning-heavy file climbs even when mid-sized; a flat large file is
# held at medium instead of auto-escalating on raw LOC alone.
_HIGH_DENSITY = 0.45
_LOW_DENSITY = 0.18

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

_CONCRETE_HIGH_RISK_SIGNALS = re.compile(
    r"(?:\b(?:rce|remote code execution|os\.system|cursor\.execute|raw_query|"
    r"shell\s*=\s*True|deseriali[sz](?:e|ation)|pickle\.loads|ssrf|"
    r"server-side request forgery|path traversal|directory traversal)\b|"
    r"\b(?:exec|eval)\s*\(|\byaml\.load\s*\()",
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
    reasoning_heavy: bool = False  # escalates to high tier on large files


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
        reasoning_heavy=True,
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
        reasoning_heavy=True,
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
        reasoning_heavy=True,
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


def _has_concrete_high_risk_signals(content: str) -> bool:
    return bool(_CONCRETE_HIGH_RISK_SIGNALS.search(content))


# Comment-only line prefixes across the common review languages. Heuristic — a
# line whose first non-space char starts one of these is treated as non-code.
_COMMENT_PREFIXES = ("#", "//", "*", "--", "/*")

# Definition / control-flow keyword signals. Language-agnostic approximations,
# not a real parser — that is the point: microsecond cost, no AST, no LLM.
_DEF_SIGNALS = re.compile(r"\b(?:def|class|function|func|fn)\b|=>")
_BRANCH_SIGNALS = re.compile(
    r"\b(?:if|elif|else|for|while|case|switch|catch|except)\b|&&|\|\||\?"
)


def _effective_loc(content: str) -> int:
    """Non-blank, non-comment-only lines — strips license headers / dead blocks."""
    n = 0
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith(_COMMENT_PREFIXES):
            continue
        n += 1
    return n


def _max_nesting_depth(content: str) -> int:
    """Approximate max nesting via indentation units and running brace balance."""
    max_indent_units = 0
    brace = 0
    max_brace = 0
    for line in content.splitlines():
        stripped = line.lstrip()
        if not stripped:
            continue
        indent = line[: len(line) - len(stripped)]
        # tab → 1 unit; 4 spaces → 1 unit (common indent widths)
        units = indent.count("\t") + (indent.count(" ") // 4)
        if units > max_indent_units:
            max_indent_units = units
        brace += stripped.count("{") - stripped.count("}")
        if brace > max_brace:
            max_brace = brace
    return max(max_indent_units, max_brace)


def _structural_density(content: str) -> float:
    """Blend nesting, branch density, and definition surface into a 0.0–1.0 score.

    Pure-Python over already-read content — no disk I/O, no AST, no LLM. Lets a
    dense, deeply-nested mid-sized file out-rank a flat large one for tiering.
    """
    eloc = _effective_loc(content)
    if eloc <= 0:
        return 0.0
    defs = len(_DEF_SIGNALS.findall(content))
    branches = len(_BRANCH_SIGNALS.findall(content))
    depth = _max_nesting_depth(content)
    depth_n = min(depth / 8.0, 1.0)
    branch_n = min((branches / eloc) / 0.4, 1.0)
    def_n = min((defs / eloc) / 0.25, 1.0)
    score = 0.5 * depth_n + 0.35 * branch_n + 0.15 * def_n
    return round(min(score, 1.0), 3)


def _task_requests_high_tier(task: str) -> bool:
    return bool(_HIGH_REVIEW_TASK_SIGNALS.search(task))


# Explicit dimension intent: the skill emits "REVIEW: [dims=performance] <paths>".
# The bracket token never matches a file pattern in extraction, so it is dropped
# from the path set and recovered here.
_REQUESTED_DIMS = re.compile(r"\[dims?=([a-z,\s/-]+)\]", re.IGNORECASE)
_DIM_ALIASES = {
    "perf": "performance",
    "sec": "security",
    "null": "edge",
    "edge-cases": "edge",
    "edgecases": "edge",
    "type": "types",
}
_DIM_KEYS = ("performance", "security", "logic", "types", "edge")


def _requested_dimensions(task: str) -> list[str]:
    """Dimensions the user explicitly asked for, in request order.

    Primary form: ``[dims=performance,security]``. Falls back to a bare keyword
    scan only when no bracket is present. Returns [] when nothing recognized.
    """
    if not isinstance(task, str) or not task:
        return []
    out: list[str] = []
    m = _REQUESTED_DIMS.search(task)
    if m:
        for tok in m.group(1).split(","):
            key = _DIM_ALIASES.get(tok.strip().lower(), tok.strip().lower())
            if key in _DIM_BY_KEY and key not in out:
                out.append(key)
        return out
    for key in _DIM_KEYS:
        if re.search(rf"\b{key}\b", task, re.IGNORECASE) and key not in out:
            out.append(key)
    return out


def strip_dims_token(task: str) -> str:
    """Remove the ``[dims=...]`` intent token so file extraction never sees it."""
    if not isinstance(task, str):
        return task
    return _REQUESTED_DIMS.sub(" ", task)


Complexity = str  # "trivial" | "moderate" | "complex"


class ReviewProfile(NamedTuple):
    band: Complexity
    has_risk: bool
    loc: int
    density_score: float = 0.0  # structural density (0.0–1.0); default keeps 3-arg back-compat
    concrete_high_risk: bool = False


def estimate_review_profile(path: str) -> ReviewProfile:
    """Return (band, has_risk, loc, density_score) for path.

    Additive companion to estimate_complexity: exposes raw LOC plus a structural
    density score for per-agent tier selection while preserving the risk-bumped
    band for dimension choice. Reuses the mtime+size-keyed cached read in
    _read_file_safe, so this adds no extra disk I/O on top of estimate_complexity.
    """
    content = _read_file_safe(path)
    if content is None:
        # Unreadable → mid-sized default so tiering lands on medium, not low/high.
        return ReviewProfile("moderate", False, _LOC_COMPLEX)
    loc = _count_loc(content)
    band, has_risk = estimate_complexity(path)
    density = _structural_density(content)
    concrete_high_risk = _has_concrete_high_risk_signals(content)
    return ReviewProfile(band, has_risk, loc, density, concrete_high_risk)


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


def dimensions_for(
    band: Complexity,
    has_risk: bool,
    requested: list[str] | None = None,
) -> list[_Dim]:
    """Dimensions to run for a given complexity band + risk flag.

    When ``requested`` names explicit dimensions, run *only* those; security is
    appended (never evicting a named dim) only when the file carries real risk
    signals. With no explicit request, fall back to band-derived selection.
    """
    if requested:
        keys = [k for k in requested if k in _DIM_BY_KEY]
        if has_risk and "security" not in keys:
            keys.append("security")
        if keys:
            return [_DIM_BY_KEY[k] for k in keys]
        # requested held only unknown keys → fall through to band logic

    if band == "trivial":
        keys = ["logic", "edge"]
    elif band == "moderate":
        keys = ["logic", "edge", "types"]
    else:  # complex
        keys = ["logic", "edge", "types", "security", "performance"]

    if has_risk and "security" not in keys:
        keys = ["security"] + keys

    return [_DIM_BY_KEY[k] for k in keys]


def _effective_drop_priority(dim: _Dim, requested_keys: set[str], has_risk: bool) -> int:
    """Per-run drop priority. Lower = more protected; dropped highest-first.

    User-requested dimensions are the most protected (-1) so they survive the
    max_agents cap even against security: security is *added* on risk (0) but
    must never evict a dimension the user explicitly asked for. Everything else
    keeps its static rank, shifted below the protected set.
    """
    if dim.key in requested_keys:
        return -1
    if dim.key == "security" and has_risk:
        return 0
    return dim.drop_priority + 1


_TIER_ORDER = ("low", "medium", "high")


def _apply_tier_bias(tier: str, bias: int) -> str:
    """Shift a tier up/down by ``bias`` steps, clamped to low..high."""
    if not bias:
        return tier
    try:
        idx = _TIER_ORDER.index(tier)
    except ValueError:
        return tier
    return _TIER_ORDER[max(0, min(len(_TIER_ORDER) - 1, idx + bias))]


def _loc_bucket(loc: int) -> str:
    if loc < _LOC_LOW:
        return "low"
    if loc > _LOC_HIGH:
        return "high"
    return "mid"


def _density_bucket(density_score: float) -> str:
    if density_score >= _HIGH_DENSITY:
        return "dense"
    if density_score < _LOW_DENSITY:
        return "flat"
    return "mid"


def profile_key_for(prof: "ReviewProfile", path: str) -> str:
    """Transferable learning key: ext|loc_bucket|density_bucket.

    Path-independent on purpose — a learned bias for ``.py|mid|dense`` applies to
    any file with that shape, including files never seen and brand-new repos.
    """
    ext = Path(path).suffix.lower() or "noext"
    return f"{ext}|{_loc_bucket(prof.loc)}|{_density_bucket(prof.density_score)}"


def tier_for(
    dim: _Dim,
    band: Complexity,
    has_risk: bool,
    *,
    loc: int | None = None,
    force_high: bool = False,
    density_score: float | None = None,
    concrete_high_risk: bool = False,
    bias: int = 0,
) -> str:
    """Routing tier for a dimension + file profile.

    Risk signals add the security dimension but do not automatically escalate to
    high. High tier is reserved for explicit deep/high-risk review requests,
    concrete exploit primitives, or genuinely large/dense reasoning-heavy files.
    When ``loc`` is given, tier on raw LOC + dimension reasoning-weight, refined
    by ``density_score``: a dense reasoning-heavy file climbs even when
    mid-sized; a flat large file is held at medium instead of escalating on raw
    LOC alone. With ``loc`` omitted the legacy 2-band behavior is preserved; with
    ``density_score`` omitted the prior LOC-only escalation is preserved
    (back-compat for both).

    ``bias`` is a learned per-profile adjustment (clamped step) applied AFTER the
    heuristic — it never overrides explicit or concrete high-risk escalation, and
    is a no-op (0) when no learning data exists, so fresh repos keep the pure
    heuristic.
    """
    if dim.key == "security" and (force_high or concrete_high_risk):
        return "high"
    have_density = density_score is not None
    if loc is None:
        if dim.key == "security" and has_risk:
            tier = "medium"
        else:
            tier = "low" if band == "trivial" else "medium"
        return _apply_tier_bias(tier, bias)
    if loc < _LOC_LOW:
        # A small but dense reasoning-heavy file earns medium over low.
        if dim.key == "security" and has_risk:
            tier = "medium"
        elif dim.reasoning_heavy and have_density and density_score >= _HIGH_DENSITY:
            tier = "medium"
        else:
            tier = "low"
    elif dim.reasoning_heavy and have_density and density_score >= _HIGH_DENSITY:
        # Dense reasoning-heavy mid-sized file escalates without needing huge LOC.
        tier = "high"
    elif loc > _LOC_HIGH and dim.reasoning_heavy:
        # Hold a genuinely flat large file at medium; otherwise escalate as before.
        tier = "medium" if (have_density and density_score < _LOW_DENSITY) else "high"
    else:
        tier = "medium"
    return _apply_tier_bias(tier, bias)


def synthesis_tier(
    requires_high: bool,
    n_cells: int = 0,
    has_high_risk_files: bool = False,
) -> str:
    """Routing tier for review synthesis.

    Scales up for explicit high-risk runs, concrete exploit primitives, or large
    finding sets — but never for ordinary security-adjacent risk words alone.
    """
    if requires_high or has_high_risk_files or n_cells >= 12:
        return "high"
    return "medium"


def build_review_subtasks(
    entries: list[tuple[str, str]],
    task: str,
    *,
    max_agents: int | None = None,
    tier_bias: dict[tuple[str, str], int] | None = None,
) -> dict:
    """Build a DAG plan dict with per-(file, dimension) subtasks + synthesis.

    entries: (path, description_hint) pairs from extract_task_file_entries.
    task: original REVIEW: ... task string.
    max_agents: hard cap; lowest-priority dimensions dropped first.
    tier_bias: optional learned {(profile_key, dimension): step} map. Looked up
        per cell (microsecond dict hit) and applied as a clamped tier shift. An
        empty/None map is a no-op — fresh repos keep the pure heuristic.
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
    requested = _requested_dimensions(task)
    requested_keys = set(requested)

    # Compute per-file (dims, profile) — profile carries raw LOC for tiering
    file_dims: list[tuple[str, list[_Dim], ReviewProfile]] = []
    for path, _ in entries:
        prof = estimate_review_profile(path)
        dims = dimensions_for(prof.band, prof.has_risk, requested=requested)
        # Only force-add security on an explicit high-tier signal, not merely
        # because the user named some other dimension.
        if task_force_high and not any(dim.key == "security" for dim in dims):
            dims = [_DIM_BY_KEY["security"]] + dims
        file_dims.append((path, dims, prof))

    # Flatten to (path, dim, profile) ordered by never-drop first (per-run priority)
    all_cells: list[tuple[str, _Dim, ReviewProfile]] = []
    for path, dims, prof in file_dims:
        for dim in sorted(
            dims, key=lambda d: _effective_drop_priority(d, requested_keys, prof.has_risk)
        ):
            all_cells.append((path, dim, prof))

    # Cap: drop highest effective-priority cells first; reserve 1 slot for synthesis
    if max_agents is not None and max_agents > 0:
        review_cap = max(1, max_agents - 1)
        if len(all_cells) > review_cap:
            by_priority = sorted(
                range(len(all_cells)),
                key=lambda i: _effective_drop_priority(
                    all_cells[i][1], requested_keys, all_cells[i][2].has_risk
                ),
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
    review_requires_high = task_force_high or any(
        prof.concrete_high_risk for _, _, prof in all_cells
    )

    for idx, (path, dim, prof) in enumerate(all_cells, start=1):
        bias = 0
        if tier_bias:
            bias = int(tier_bias.get((profile_key_for(prof, path), dim.key), 0))
        t = tier_for(
            dim,
            prof.band,
            prof.has_risk,
            loc=prof.loc,
            force_high=task_force_high,
            density_score=prof.density_score,
            concrete_high_risk=prof.concrete_high_risk,
            bias=bias,
        )
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
    reviewed_files = sorted({path for path, _, _ in all_cells})
    has_high_risk_files = any(prof.concrete_high_risk for _, _, prof in all_cells)
    subtasks.append({
        "id": synth_id,
        "description": (
            _SYNTHESIS_PROMPT
            + f"\n\nFiles reviewed: {', '.join(reviewed_files)}"
        ),
        "tier": synthesis_tier(review_requires_high, len(all_cells), has_high_risk_files),
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
    file_high_risks: list[bool] = []
    for idx, (path, _hint) in enumerate(file_entries, start=1):
        prof = estimate_review_profile(path)
        file_high_risks.append(prof.concrete_high_risk)
        tier = _fast_review_tier(prof, force_high=task_force_high)
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
    review_requires_high = task_force_high or any(file_high_risks)
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


def _fast_review_tier(prof: ReviewProfile, *, force_high: bool = False) -> str:
    """Tier for one-agent-per-file broad review.

    Broad review agents cover all dimensions, so medium is the default. Escalate
    only for explicit deep/high-risk intent, concrete exploit primitives, or
    large/dense files where a single reviewer needs extra reasoning depth.
    """
    if force_high or prof.concrete_high_risk:
        return "high"
    if prof.loc > _LOC_HIGH and prof.density_score >= _LOW_DENSITY:
        return "high"
    if prof.loc >= _LOC_LOW and prof.density_score >= _HIGH_DENSITY:
        return "high"
    return "medium"
