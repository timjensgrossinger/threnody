"""Per-file x dimension review fanout for the threnody-swarm-review skill.

Called by build_heuristic_plan_payload when the task starts with the REVIEW: sentinel.
Produces a DAG plan: one subtask per (file, dimension) + a synthesis subtask.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from shared.code_intel import CodeIntel, Smell, StructuralTraits
    from shared.db import Database

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
    r"password|secret|credential|keychain|token|api[_ -]?key|rce|remote code execution|"
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
    """One review dimension, stored as parts rather than one prompt string.

    The instruction text (``title`` + ``focus`` + ``report``) is identical for every
    file in a run, while only the path varies. Keeping them separate lets the same
    dimension render two ways:

    * ``prompt_template`` — the full inline prompt, path first. Byte-for-byte the
      wording used before this split, for hosts that cannot resolve a named subagent
      definition and therefore need the instructions in the prompt itself.
    * ``stable_block`` + ``variable_line`` — instructions front-loaded and path last,
      so N agents share a cacheable prefix; and when the host *does* resolve a named
      definition, the stable half is dropped entirely because the definition already
      carries it.
    """

    key: str
    subagent_type: str
    title: str   # e.g. "Security review"
    focus: str   # what to look for; one sentence ending in a period
    report: str  # how to report findings, incl. the category vocabulary
    drop_priority: int  # higher = drop first; 0 = never drop
    reasoning_heavy: bool = False  # escalates to high tier on large files

    @property
    def prompt_template(self) -> str:
        """Full inline prompt with a ``{path}`` placeholder."""
        return f"{self.title} of {{path}}: {self.focus} {self.report}"

    @property
    def stable_block(self) -> str:
        """Path-free instruction text — constant across every cell in the run.

        ``focus`` is stored lowercase because the inline form reads
        ``"... of {path}: check for ..."``; standing alone it starts a sentence.
        """
        focus = f"{self.focus[:1].upper()}{self.focus[1:]}" if self.focus else ""
        return f"{self.title}. {focus} {self.report}"

    def variable_line(self, path: str, *, changed_lines: str = "") -> str:
        """The only per-agent-varying part of a review prompt.

        Names the dimension explicitly — a bare "Review this file: X" left a
        reviewer with no idea which dimension it was assigned even under
        ``BOILERPLATE_DEFINITION``, where the exported subagent definition
        (a separate system prompt) is the only other place that would say so —
        and, when known, the changed-line ranges so a reviewer prioritizes the
        diff without losing whole-file context (the file is still read whole
        either way: content_sha, LOC bucket, and static-recall grading are
        unaffected by this clause).
        """
        base = f"{self.title} of {path}."
        if changed_lines:
            return (
                f"{base}\nChanged lines: {changed_lines} — prioritize these; "
                "read the whole file for context."
            )
        return f"{base}\nReview the whole file."


REVIEW_DIMENSIONS: list[_Dim] = [
    _Dim(
        key="security",
        subagent_type="review-security",
        title="Security review",
        focus=(
            "check for injection (SQL, command, XSS), "
            "auth bypass, hardcoded secrets, SSRF, path traversal, weak crypto, "
            "CSRF, IDOR, insecure deserialization, and input validation gaps."
        ),
        report=(
            "Report each finding as: ⚠️ [SEVERITY] security/<category> — file:line — description (CWE-XXX), "
            "where <category> is a kebab-case vulnerability class "
            "(e.g. sql-injection, xss, path-traversal, hardcoded-secret, ssrf, weak-crypto). "
            "Output nothing if no issues found."
        ),
        drop_priority=0,
        reasoning_heavy=True,
    ),
    _Dim(
        key="logic",
        subagent_type="review-logic",
        title="Logic review",
        focus=(
            "check for off-by-one errors, wrong conditions, "
            "unreachable code, swapped arguments, missing returns, and state invariant violations."
        ),
        report=(
            "Report each finding as: ⚠️ [SEVERITY] logic/<category> — file:line — description, "
            "where <category> is a kebab-case slug (e.g. off-by-one, wrong-condition, missing-return). "
            "Output nothing if no issues found."
        ),
        drop_priority=1,
        reasoning_heavy=True,
    ),
    _Dim(
        key="edge",
        subagent_type="review-edge-cases",
        title="Edge and null case review",
        focus=(
            "check for null/None dereferences, "
            "empty collection access, division by zero, missing error handling, "
            "missing defaults, boundary conditions, and missing I/O error handling."
        ),
        report=(
            "Report each finding as: ⚠️ [SEVERITY] edge/<category> — file:line — description, "
            "where <category> is a kebab-case slug (e.g. null-deref, empty-collection, div-by-zero). "
            "Output nothing if no issues found."
        ),
        drop_priority=2,
    ),
    _Dim(
        key="types",
        subagent_type="review-types",
        title="Type safety review",
        focus=(
            "check for type mismatches, unsafe casts, "
            "generic violations, incompatible return types, and serialization/deserialization drift."
        ),
        report=(
            "Report each finding as: ⚠️ [SEVERITY] types/<category> — file:line — description, "
            "where <category> is a kebab-case slug (e.g. type-mismatch, unsafe-cast, serde-drift). "
            "Output nothing if no issues found."
        ),
        drop_priority=3,
    ),
    _Dim(
        key="performance",
        subagent_type="review-performance",
        title="Performance review",
        focus=(
            "check for O(n²) algorithms, N+1 queries, "
            "memory leaks, blocking I/O in async contexts, unbounded growth, missing pagination, "
            "and redundant calls."
        ),
        report=(
            "Report each finding as: ⚠️ [SEVERITY] performance/<category> — file:line — description, "
            "where <category> is a kebab-case slug (e.g. quadratic, n-plus-1, memory-leak, blocking-io). "
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


_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _changed_line_ranges(path: str, ref: str, project_root: str, *, max_ranges: int = 6) -> str:
    """Changed-line ranges for *path* since *ref*, formatted as ``"12-34, 56"``.

    Read from ``git diff -U0`` hunk headers on the ``+start,count`` side —
    post-change line numbers, matching what a reviewer sees when it opens the
    *current* file (the file is still read whole regardless; this only tells
    the reviewer where to look first). Returns ``""`` on any git failure, no
    diff, or a path outside the repo — the caller then omits the changed-lines
    clause entirely rather than risk pointing at the wrong lines.
    """
    from .verify import _git

    result = _git(["diff", "-U0", ref, "--", path], project_root)
    if result is None or result.returncode != 0:
        return ""
    ranges: list[tuple[int, int]] = []
    for line in result.stdout.splitlines():
        match = _HUNK_HEADER_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if count == 0:
            # A pure-deletion hunk reports the line *after* the deleted block
            # with a 0 count — nothing to highlight in the current file.
            continue
        ranges.append((start, start + count - 1))
    if not ranges:
        return ""
    labels = [f"{a}-{b}" if a != b else str(a) for a, b in ranges[:max_ranges]]
    suffix = f" (+{len(ranges) - max_ranges} more)" if len(ranges) > max_ranges else ""
    return ", ".join(labels) + suffix


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


def _structural_density(content: str, intel: "CodeIntel | None" = None) -> float:
    """Blend nesting, branch density, and definition surface into a 0.0–1.0 score.

    Pure-Python over already-read content — no disk I/O, no LLM. Lets a dense,
    deeply-nested mid-sized file out-rank a flat large one for tiering.

    When ``intel`` carries a successful AST parse, the same three quantities come
    from the parse instead of the keyword regexes — the formula, normalizers, and
    0.0–1.0 scale are identical, so ``_density_bucket`` names and therefore
    ``profile_key_for`` learning keys are unchanged. The AST simply measures
    accurately what the regexes approximate (which count keywords inside strings
    and comments), so a previously mis-bucketed file may now bucket correctly.
    """
    eloc = _effective_loc(content)
    if eloc <= 0:
        return 0.0
    if intel is not None and intel.parsed:
        defs = intel.def_count
        branches = intel.branch_count
        depth = intel.max_depth
    else:
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
    intel: "CodeIntel | None" = None  # entity/smell scan; None when unavailable
    # Coarse shape facts (annotations / loops / I/O) used to skip a dimension the file
    # cannot hold defects for. None when unavailable → nothing is skipped.
    traits: "StructuralTraits | None" = None


def estimate_review_profile(path: str, *, db: "Database | None" = None) -> ReviewProfile:
    """Return (band, has_risk, loc, density_score, ...) for path.

    Additive companion to estimate_complexity: exposes raw LOC plus a structural
    density score for per-agent tier selection while preserving the risk-bumped
    band for dimension choice. Reuses the mtime+size-keyed cached read in
    _read_file_safe, so this adds no extra disk I/O on top of estimate_complexity.

    ``db`` is optional and only enables the cross-process ``code_intel`` cache;
    without it the scan still runs against an in-process cache, so this module
    stays usable (and testable) with no database.
    """
    content = _read_file_safe(path)
    if content is None:
        # Unreadable → mid-sized default so tiering lands on medium, not low/high.
        return ReviewProfile("moderate", False, _LOC_COMPLEX)
    loc = _count_loc(content)
    band, has_risk = estimate_complexity(path)
    intel = _scan_intel(path, content, db)
    density = _structural_density(content, intel)
    concrete_high_risk = _has_concrete_high_risk_signals(content)
    return ReviewProfile(
        band, has_risk, loc, density, concrete_high_risk, intel, _scan_traits(path, content)
    )


def _scan_traits(path: str, content: str) -> "StructuralTraits | None":
    """Best-effort structural traits — a failure degrades to running every dimension."""
    try:
        from .code_intel import structural_traits

        return structural_traits(path, content)
    except Exception:  # pragma: no cover - best-effort
        log.debug("review_fanout: structural traits failed for %s", path, exc_info=True)
        return None


def _scan_intel(path: str, content: str, db: "Database | None") -> "CodeIntel | None":
    """Best-effort code_intel scan — a failure degrades to the regex heuristics."""
    try:
        from .code_intel import scan

        return scan(path, content=content, db=db)
    except Exception:  # pragma: no cover - best-effort
        log.debug("review_fanout: code_intel scan failed for %s", path, exc_info=True)
        return None


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


# Dimensions the static scanner has real detection coverage for. A clean scan is
# weak evidence for these, so a trivial file may skip them. `logic` is excluded on
# purpose: no static rule detects an off-by-one or an inverted condition, so "no
# smells" says nothing about logic and must never justify dropping that reviewer.
_STATIC_COVERAGE = frozenset({"security", "edge", "types", "performance"})


def _smells_for_profile(prof: ReviewProfile) -> "dict[str, list[Smell]]":
    """Static smells grouped by dimension, or ``{}`` when no scan is available.

    Returns ``{}`` (not None) for an unscanned profile so callers can treat the
    lookup uniformly; ``dimensions_for`` distinguishes the two cases by receiving
    None explicitly when ``prof.intel`` is absent.
    """
    if prof.intel is None:
        return {}
    from .code_intel import smells_by_dimension

    return smells_by_dimension(prof.intel.smells)


def _smell_tier_bias(dim_smells: "list[Smell]") -> int:
    """+1 when this dimension carries a high-severity static hit, else 0."""
    from .code_intel import SEVERITY_HIGH

    return 1 if any(s.severity == SEVERITY_HIGH for s in dim_smells) else 0


def _format_leads(dim_smells: "list[Smell]") -> str:
    if not dim_smells:
        return ""
    from .code_intel import format_smell_leads

    return format_smell_leads(dim_smells)


def _expected_rule_ids(dim_smells: "list[Smell]") -> list[str]:
    """High-severity rule ids for this cell — the recall ledger's expected set."""
    from .code_intel import SEVERITY_HIGH

    return sorted({s.rule_id for s in dim_smells if s.severity == SEVERITY_HIGH})


# Wildcard profile key: a tier_bias entry that applies to every file profile.
# Used by the objective model-quality loop, whose signal is per-(model, dimension)
# and therefore not tied to any one file shape.
GLOBAL_PROFILE_KEY = "*"


def _cell_tier(
    path: str,
    dim: _Dim,
    prof: ReviewProfile,
    *,
    task_force_high: bool,
    tier_bias: dict[tuple[str, str], int] | None,
) -> tuple[str, "list[Smell]"]:
    """Resolve one cell's tier and its dimension smells.

    Extracted so the review-memory skip check and the subtask builder agree on the
    planned tier — a skip decision compared against a different tier than the one
    that would actually run would be unsound.
    """
    bias = 0
    if tier_bias:
        # Two learned sources share this map: a per-profile step keyed on the
        # file's profile_key, and a model-wide step from the objective quality
        # ledger keyed on the GLOBAL_PROFILE_KEY wildcard. Their sum is clamped to
        # a single step so two independent learners cannot compound into a
        # two-tier jump; the evidence-based smell bias below is separate.
        learned = int(tier_bias.get((profile_key_for(prof, path), dim.key), 0))
        learned += int(tier_bias.get((GLOBAL_PROFILE_KEY, dim.key), 0))
        bias = max(-1, min(1, learned))
    dim_smells = _smells_for_profile(prof).get(dim.key, []) if prof.intel else []
    # A confirmed-shape static hit is concrete evidence this cell has real work to
    # do, so give it one extra step of reasoning headroom.
    bias += _smell_tier_bias(dim_smells)
    tier = tier_for(
        dim,
        prof.band,
        prof.has_risk,
        loc=prof.loc,
        force_high=task_force_high,
        density_score=prof.density_score,
        concrete_high_risk=prof.concrete_high_risk,
        bias=bias,
    )
    return tier, dim_smells


_DIRECTORY_TOKEN = re.compile(r"(?<![\w./-])((?:[\w.-]+/)+|\.)(?![\w.-]*\.[A-Za-z])")


def directory_targets(task: str) -> list[str]:
    """Directory-shaped targets named in a ``REVIEW:`` task.

    A trailing-slash token (``shared/``) or a bare ``.``. Deliberately not a glob
    engine: this exists so ``REVIEW: shared/`` has a defined meaning, nothing more.
    """
    if not isinstance(task, str) or not task.strip():
        return []
    body = strip_dims_token(task)
    if body.strip().lower().startswith(_REVIEW_SENTINEL):
        body = body.strip()[len(_REVIEW_SENTINEL):]
    out: list[str] = []
    seen: set[str] = set()
    for match in _DIRECTORY_TOKEN.finditer(body):
        token = match.group(1).strip()
        if not token:
            continue
        normalized = token.rstrip("/") or "."
        if normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        out.append(normalized)
    return out


def changed_files_under(
    workspace_root: str, directories: list[str]
) -> tuple[list[str], str | None]:
    """Files changed since the merge base, restricted to *directories*.

    Returns ``(paths, baseline_ref)``. An empty ref means no baseline could be
    resolved (fresh repo, root commit, not a git repo) — the caller must then not
    claim to have scoped anything.

    Each returned file is reviewed *whole*, so ``content_sha``, the LOC bucket in
    ``profile_key_for`` and static-recall grading are all identical to an unscoped
    run. This narrows *which* files are reviewed, never how much of one is read.
    """
    if not workspace_root or not directories:
        return [], None
    try:
        from pathlib import Path

        from .verify import _git, resolve_baseline_ref

        ref = resolve_baseline_ref(workspace_root)
        if not ref:
            return [], None
        args = ["diff", "--name-only", ref, "--", *directories]
        proc = _git(args, workspace_root)
        if proc is None or proc.returncode != 0:
            return [], None
        root = Path(workspace_root)
        paths: list[str] = []
        seen: set[str] = set()
        for line in (proc.stdout or "").splitlines():
            rel = line.strip()
            if not rel or rel.lower() in seen:
                continue
            candidate = root / rel
            # Deleted files show up in the diff but cannot be reviewed.
            if not candidate.is_file():
                continue
            seen.add(rel.lower())
            paths.append(str(candidate))
        return paths, ref
    except Exception:
        log.debug("review_fanout: changed-file scoping failed", exc_info=True)
        return [], None


def _review_pattern_hash(dim_key: str) -> str:
    """Prompt-independent pattern key for a review dimension.

    Deliberately hashes a canonical token rather than the prompt: review prompts
    change wording as the boilerplate mode changes, and the learning tables must not
    see that as a new kind of work.
    """
    try:
        from .agents import pattern_hash

        return pattern_hash(f"review:{dim_key}")
    except Exception:
        log.debug("review_fanout: pattern_hash unavailable", exc_info=True)
        return ""


def _load_config(config: "Any | None" = None) -> "Any | None":
    """Return *config* or a freshly loaded one. ``None`` on any failure.

    ``TGsConfig.from_yaml()`` re-reads and re-parses config.yaml on every call, so a
    fan-out resolves it once here and threads the result.
    """
    if config is not None:
        return config
    try:
        from .config import TGsConfig

        return TGsConfig.from_yaml()
    except Exception:
        log.debug("review_fanout: config load failed", exc_info=True)
        return None


def _prompt_economy(
    caller: str | None, config: "Any | None" = None
) -> tuple[str, int]:
    """Resolve (boilerplate_mode, prompt_char_budget) for this run.

    Best-effort: any failure falls back to the legacy prompt with no budget, because
    a config read must never be what stops a review from being planned.
    """
    from .prompt_budget import BOILERPLATE_LEGACY

    if caller is None or config is None:
        return BOILERPLATE_LEGACY, 0
    try:
        from .prompt_budget import boilerplate_mode, effective_budget

        return boilerplate_mode(config, caller), effective_budget(config, caller)
    except Exception:
        log.debug("review_fanout: prompt economy resolution failed", exc_info=True)
        return BOILERPLATE_LEGACY, 0


def resolve_synthesis_mode(
    config: "Any | None",
    *,
    cells: int,
    files: int,
) -> str:
    """Decide whether findings are merged in Python or by a synthesis agent.

    What the LLM synthesis agent adds over a mechanical dedup-and-sort is
    *correlation*: noticing that findings from different dimensions or different files
    describe one root cause. That value scales with the breadth of the merge, so
    breadth is what ``auto`` measures — the number of (file × dimension) cells and the
    number of files. A narrow run has nothing to correlate and the merge is pure
    bookkeeping; a broad one is where the judgment earns its cost.

    Deliberately not keyed on the static pre-scan's expected-finding count: that
    number is almost always small, so it would resolve to ``python`` nearly always
    and make ``auto`` a misleading name for "python".
    """
    if config is None:
        return "llm"
    mode = str(getattr(config, "review_synthesis_mode", "auto") or "auto").lower()
    if mode in {"python", "llm"}:
        return mode
    max_cells = int(getattr(config, "review_synthesis_python_max_cells", 6) or 6)
    max_files = int(getattr(config, "review_synthesis_python_max_files", 2) or 2)
    if cells <= max_cells and files <= max_files:
        return "python"
    return "llm"


# Dimensions a file can be proven not to need, and the trait that proves it.
# Only these two: a file with no annotations genuinely has no type contract to check,
# and one with no loops and no I/O has no hot path. There is no equivalent proof for
# logic, edge cases, or security — any file can hold those.
_TRAIT_GATED_DIMENSIONS = ("types", "performance")


def _gate_dimensions_structurally(
    dims: list[_Dim], traits: "StructuralTraits | None", requested_keys: set[str]
) -> list[_Dim]:
    """Drop dimensions whose class of defect the file structurally cannot hold.

    Skips nothing unless the traits came from a real AST parse (``parsed``): on the
    regex fallback the answers are guesses, and a guess must never remove a reviewer.
    Never drops an explicitly requested dimension, and never touches ``security`` —
    which is not trait-gated at all.
    """
    if traits is None or not traits.parsed:
        return dims
    dropped: list[str] = []
    kept: list[_Dim] = []
    for dim in dims:
        if dim.key in requested_keys or dim.key not in _TRAIT_GATED_DIMENSIONS:
            kept.append(dim)
            continue
        if dim.key == "types" and not traits.has_annotations:
            dropped.append(dim.key)
            continue
        if dim.key == "performance" and not (traits.has_loops or traits.has_io):
            dropped.append(dim.key)
            continue
        kept.append(dim)
    if dropped:
        log.debug("review_fanout: structural gating dropped %s", ", ".join(dropped))
    # Never return an empty dimension set — that would silently drop the file.
    return kept or dims


def _cell_description(
    path: str,
    dim: _Dim,
    dim_smells: "list[Smell]",
    db: "Database | None",
    *,
    mode: str,
    budget: int,
    changed_lines: str = "",
) -> str:
    """Render one review cell's prompt under the resolved boilerplate *mode*.

    ``legacy`` reproduces the pre-split string exactly — same wording, same
    concatenation, no separator changes — so opting out is a true no-op. That
    contract means ``changed_lines`` is deliberately not applied there.
    """
    from .prompt_budget import (
        BOILERPLATE_DEFINITION,
        BOILERPLATE_LEGACY,
        render,
    )

    leads = _format_leads(dim_smells)
    resolved = _format_resolved(db, path, dim.key)

    if mode == BOILERPLATE_LEGACY and budget <= 0:
        return dim.prompt_template.format(path=path) + leads + resolved

    if mode == BOILERPLATE_DEFINITION:
        # The exported subagent definition already carries title/focus/report.
        stable: list[str] = []
        variable = [dim.variable_line(path, changed_lines=changed_lines), leads, resolved]
    elif mode == BOILERPLATE_LEGACY:
        # Budget-only pass: keep the legacy wording, cap the appended blocks.
        stable = [dim.prompt_template.format(path=path)]
        variable = [leads, resolved]
    else:
        stable = [dim.stable_block]
        variable = [dim.variable_line(path, changed_lines=changed_lines), leads, resolved]
    return render(stable=stable, variable=variable, budget=budget).text


def _format_resolved(db: "Database | None", path: str, dimension: str) -> str:
    """Prompt block suppressing findings already reported here and since fixed."""
    if db is None:
        return ""
    try:
        from .review_memory import format_resolved_block, load_resolved_findings

        return format_resolved_block(load_resolved_findings(db, path, dimension))
    except Exception:  # pragma: no cover - best-effort
        log.debug("review_fanout: resolved-findings read failed", exc_info=True)
        return ""


def _replayed_finding_records(replayed: "list[Any]") -> list[dict[str, Any]]:
    """Flatten prior-review scans into plain finding records.

    Structured rather than pre-rendered so the consumer owns the formatting, and so a
    record with a missing field is skipped instead of producing a malformed line.
    """
    out: list[dict[str, Any]] = []
    for scan in replayed or []:
        path = str(getattr(scan, "path", "") or "").strip()
        dimension = str(getattr(scan, "dimension", "") or "").strip()
        if not path or not dimension:
            continue
        for finding in getattr(scan, "findings", ()) or ():
            summary = str(getattr(finding, "summary", "") or "").strip()
            if not summary:
                continue
            try:
                line = int(getattr(finding, "line", 0) or 0)
            except (TypeError, ValueError):
                line = 0
            out.append({
                "dimension": dimension,
                "category": str(getattr(finding, "category", "") or "").strip().lower(),
                "severity": str(getattr(finding, "severity", "") or "").strip().lower(),
                "path": path,
                "line": line,
                "summary": summary,
            })
    return out


def _format_replay(replayed: "list[Any]") -> str:
    """Synthesis block carrying findings from cells served out of memory."""
    if not replayed:
        return ""
    try:
        from .review_memory import format_replay_block

        return format_replay_block(replayed)
    except Exception:  # pragma: no cover - best-effort
        log.debug("review_fanout: replay block render failed", exc_info=True)
        return ""


def _review_memory_enabled() -> bool:
    """Config gate for prior-review memory. Fail-safe → enabled."""
    try:
        from .config import TGsConfig

        return bool(getattr(TGsConfig.from_yaml(), "review_memory_enabled", True))
    except Exception:  # pragma: no cover - config read is best-effort
        return True


def _apply_review_memory(
    all_cells: list[tuple[str, _Dim, ReviewProfile]],
    db: "Database | None",
    *,
    task_force_high: bool,
    tier_bias: dict[tuple[str, str], int] | None,
) -> tuple[list[tuple[str, _Dim, ReviewProfile]], list[Any]]:
    """Split cells into (to-run, replayed-from-memory).

    A cell is only skipped when the stored scan covers the *same* content digest
    AND ran at an equal-or-stronger tier, so cheap prior coverage never satisfies a
    plan that has since escalated. Without a DB, without a scan digest, or with the
    feature disabled, every cell runs — identical to pre-Phase-2 behavior.
    """
    if db is None or not all_cells or not _review_memory_enabled():
        return all_cells, []
    try:
        from .review_memory import load_cached_scan, tier_covers
    except Exception:  # pragma: no cover - defensive import
        return all_cells, []

    keep: list[tuple[str, _Dim, ReviewProfile]] = []
    replayed: list[Any] = []
    for path, dim, prof in all_cells:
        sha = prof.intel.content_sha if prof.intel else ""
        if not sha:
            keep.append((path, dim, prof))
            continue
        planned_tier, _ = _cell_tier(
            path, dim, prof, task_force_high=task_force_high, tier_bias=tier_bias
        )
        cached = load_cached_scan(db, path, sha, dim.key)
        if cached is not None and tier_covers(cached.tier, planned_tier):
            replayed.append(cached)
        else:
            keep.append((path, dim, prof))
    if replayed:
        log.info(
            "review_fanout: %d cell(s) served from prior-review memory (unchanged revision)",
            len(replayed),
        )
    # Never return an empty plan: if literally everything was cached, keep the
    # synthesis agent so the replayed findings are still reported to the user.
    return keep, replayed


def dimensions_for(
    band: Complexity,
    has_risk: bool,
    requested: list[str] | None = None,
    smells: "dict[str, list[Smell]] | None" = None,
) -> list[_Dim]:
    """Dimensions to run for a given complexity band + risk flag.

    When ``requested`` names explicit dimensions, run *only* those; security is
    appended (never evicting a named dim) only when the file carries real risk
    signals. With no explicit request, fall back to band-derived selection.

    ``smells`` is the static pre-scan grouped by dimension. It refines the band
    choice in two directions: a dimension holding a high-severity smell is added
    even when the band would not have selected it, and a ``trivial`` file with a
    clean scan may skip the dimensions the scanner actually covers
    (:data:`_STATIC_COVERAGE`). Explicit ``requested`` dimensions are never
    dropped, and security survives whenever ``has_risk``.
    """
    from .code_intel import SEVERITY_HIGH

    def _has_high(key: str) -> bool:
        return any(s.severity == SEVERITY_HIGH for s in (smells or {}).get(key, ()))

    if requested:
        keys = [k for k in requested if k in _DIM_BY_KEY]
        if has_risk and "security" not in keys:
            keys.append("security")
        if keys:
            # Static high-severity evidence outside the requested set still earns a
            # reviewer — a confirmed exploit primitive should not go unreviewed
            # because the operator named a different dimension.
            for key in _DIM_KEYS:
                if key not in keys and _has_high(key):
                    keys.append(key)
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

    if smells is not None:
        for key in _DIM_KEYS:
            if key not in keys and _has_high(key):
                keys.append(key)
        if band == "trivial":
            keys = [
                k
                for k in keys
                if k not in _STATIC_COVERAGE
                or smells.get(k)
                or (k == "security" and has_risk)
            ]

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
    db: "Database | None" = None,
    caller: str | None = None,
    config: "Any | None" = None,
) -> dict:
    """Build a DAG plan dict with per-(file, dimension) subtasks + synthesis.

    entries: (path, description_hint) pairs from extract_task_file_entries.
    task: original REVIEW: ... task string.
    max_agents: hard cap; lowest-priority dimensions dropped first.
    tier_bias: optional learned {(profile_key, dimension): step} map. Looked up
        per cell (microsecond dict hit) and applied as a clamped tier shift. An
        empty/None map is a no-op — fresh repos keep the pure heuristic.
    db: optional Database, used only for the cross-process code_intel scan cache.
    caller: host shell id, used to resolve prompt-economy capabilities. ``None``
        keeps the pre-split prompt wording exactly.
    config: optional pre-loaded TGsConfig, so a fan-out resolves prompt economy
        once instead of re-reading config.yaml per cell.
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
    # Prompt economy resolved once per run, not per cell — from_yaml() re-reads and
    # re-parses config.yaml on every call.
    cfg = _load_config(config)
    boilerplate, prompt_char_budget = _prompt_economy(caller, cfg)
    structural_gating = bool(
        getattr(cfg, "review_structural_dim_gating", False) if cfg is not None else False
    )

    # Resolved once per run, not per cell — same convention as heuristic_plan's
    # _changed_file_entries (git command cost is real; the ref never changes
    # mid-run). A cache miss (no merge base, not a git repo) means every cell's
    # variable_line simply omits the changed-lines clause and still reviews the
    # whole file — never a hard failure.
    _diff_ref: str | None = None
    _diff_ref_resolved = False
    _changed_lines_cache: dict[str, str] = {}

    def _changed_lines_for(path: str) -> str:
        nonlocal _diff_ref, _diff_ref_resolved
        if not _diff_ref_resolved:
            _diff_ref_resolved = True
            try:
                from .verify import resolve_baseline_ref

                _diff_ref = resolve_baseline_ref(str(Path.cwd()))
            except Exception:
                log.debug("review_fanout: baseline ref resolution failed", exc_info=True)
                _diff_ref = None
        if not _diff_ref:
            return ""
        if path not in _changed_lines_cache:
            try:
                _changed_lines_cache[path] = _changed_line_ranges(
                    path, _diff_ref, str(Path.cwd())
                )
            except Exception:
                log.debug("review_fanout: changed-line lookup failed for %s", path, exc_info=True)
                _changed_lines_cache[path] = ""
        return _changed_lines_cache[path]

    # Compute per-file (dims, profile) — profile carries raw LOC for tiering
    file_dims: list[tuple[str, list[_Dim], ReviewProfile]] = []
    # What the band/smell engine actually wanted to run per file, captured before
    # prior-review memory or the max_agents cap can shrink it — the only way a
    # caller can tell "planned 3 of 15" apart from "planned 3, this file only had 3".
    expected_by_file: dict[str, list[str]] = {}
    for path, _ in entries:
        prof = estimate_review_profile(path, db=db)
        dims = dimensions_for(
            prof.band,
            prof.has_risk,
            requested=requested,
            smells=_smells_for_profile(prof),
        )
        if structural_gating:
            dims = _gate_dimensions_structurally(dims, prof.traits, requested_keys)
        # Only force-add security on an explicit high-tier signal, not merely
        # because the user named some other dimension.
        if task_force_high and not any(dim.key == "security" for dim in dims):
            dims = [_DIM_BY_KEY["security"]] + dims
        expected_by_file[path] = [d.key for d in dims]
        file_dims.append((path, dims, prof))

    # Flatten to (path, dim, profile) ordered by never-drop first (per-run priority)
    all_cells: list[tuple[str, _Dim, ReviewProfile]] = []
    for path, dims, prof in file_dims:
        for dim in sorted(
            dims, key=lambda d: _effective_drop_priority(d, requested_keys, prof.has_risk)
        ):
            all_cells.append((path, dim, prof))

    # Prior-review memory: drop cells whose exact file revision was already
    # reviewed at an equal-or-stronger tier, and replay their stored findings into
    # synthesis instead of re-spawning the agent.
    all_cells, replayed = _apply_review_memory(
        all_cells, db, task_force_high=task_force_high, tier_bias=tier_bias
    )

    # Resolve the synthesis mode BEFORE the agent cap, from the review as requested.
    # The cap and the mode are otherwise circular: the cap must know whether to reserve
    # a slot for a synthesis agent, while `auto` keys on how many cells survive it.
    # Deciding on pre-cap breadth breaks that, and is the more faithful reading anyway —
    # how broad a review *is* should not change because a budget trimmed it.
    synthesis_mode = resolve_synthesis_mode(
        cfg,
        cells=len(all_cells),
        files=len({path for path, _, _ in all_cells} | {s.path for s in replayed}),
    )

    # A one-agent budget cannot hold both a reviewer and a synthesis agent: the cap
    # floors at one cell and the synthesis agent was then appended regardless, so the
    # plan overran the budget. Spend the slot on the review and merge in-process.
    if max_agents is not None and 0 < max_agents <= 1 and all_cells:
        synthesis_mode = "python"

    # Cap: drop highest effective-priority cells first, reserving a slot for the
    # synthesis agent only when one will actually be planned. Under python synthesis
    # the merge is in-process, so reserving a slot would silently cost a review cell.
    dropped_labels: list[str] = []
    if max_agents is not None and max_agents > 0:
        review_cap = max_agents if synthesis_mode == "python" else max(1, max_agents - 1)
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
        t, dim_smells = _cell_tier(
            path, dim, prof, task_force_high=task_force_high, tier_bias=tier_bias
        )
        subtasks.append({
            "id": idx,
            "description": _cell_description(
                path, dim, dim_smells, db,
                mode=boilerplate, budget=prompt_char_budget,
                changed_lines=_changed_lines_for(path),
            ),
            "tier": t,
            "target_file": path,
            "subagent_type": dim.subagent_type,
            "read_only": True,
            "depends_on": [],
            "single_file_insertion": False,
            # Pin the learning key to the dimension, not the rendered prompt.
            # `agents.normalize_pattern` strips paths but keeps prose, so without
            # this a prompt-wording change would mint a new pattern_hash and orphan
            # every accumulated `subtask_patterns` row for this dimension.
            "pattern_hash": _review_pattern_hash(dim.key),
            # Consumed by host_learning to score static recall for this cell.
            # Absent/empty means "no static expectation" — never a zero score.
            "review_dimension": dim.key,
            "expected_rules": _expected_rule_ids(dim_smells),
            "content_sha": prof.intel.content_sha if prof.intel else "",
        })
        review_ids.append(idx)

    synth_id = len(all_cells) + 1
    reviewed_files = sorted(
        {path for path, _, _ in all_cells} | {scan.path for scan in replayed}
    )
    has_high_risk_files = any(prof.concrete_high_risk for _, _, prof in all_cells)
    if not all_cells and replayed:
        # Every cell came out of prior-review memory, so no agent will run. The
        # in-process merge only happens at wave finalize, which nothing would reach
        # with an empty plan — so the synthesis agent stays, as the one agent whose
        # job is to surface the carried-over findings.
        synthesis_mode = "llm"
    # Cells served from prior-review memory have findings but no agent. Under `llm`
    # they ride into the synthesis prompt via _format_replay; under `python` they must
    # be handed to the in-process merge, or those findings would go unreported.
    replayed_findings = (
        _replayed_finding_records(replayed) if synthesis_mode == "python" else []
    )
    if synthesis_mode != "python":
        subtasks.append({
            "id": synth_id,
            "description": (
                _SYNTHESIS_PROMPT
                + f"\n\nFiles reviewed: {', '.join(reviewed_files)}"
                + _format_replay(replayed)
            ),
            "tier": synthesis_tier(review_requires_high, len(all_cells), has_high_risk_files),
            "depends_on": review_ids,
            "subagent_type": "",  # empty → resolved to threnody-high by tier in host_spawn
            "read_only": True,
        })

    n_files = len(entries)
    n_dims = len(all_cells)
    total_expected = sum(len(v) for v in expected_by_file.values())
    cached_note = (
        f" {len(replayed)} cell(s) served from prior-review memory (unchanged revision)."
        if replayed
        else ""
    )
    synthesis_note = (
        " Findings are merged in-process (no synthesis agent)."
        if synthesis_mode == "python"
        else " + 1 synthesis."
    )
    skipped_prior_review = [f"{r.path}:{r.dimension}" for r in replayed]
    # A max_agents cap that dropped real coverage must say so in the sentence a
    # caller is most likely to actually read — a coverage dict nobody looks at is
    # the same silent under-review this exists to prevent.
    if dropped_labels:
        coverage_clause = (
            f"{n_dims} of {total_expected} dimension agent(s) — "
            f"{len(dropped_labels)} dropped by max_agents={max_agents}."
        )
    else:
        coverage_clause = f"{n_dims} dimension agent(s)."
    planned_by_file: dict[str, list[str]] = {}
    for path, dim, _ in all_cells:
        planned_by_file.setdefault(path, []).append(dim.key)
    return {
        "analysis": (
            f"Review fanout: {n_files} file(s), {coverage_clause}"
            f"{synthesis_note}"
            f"{cached_note} Host-native DAG. No external planner LLM was called."
        ),
        "subtasks": subtasks,
        "strategy": "dag",
        "topology": "dag",
        "cached_cell_count": len(replayed),
        "synthesis_mode": synthesis_mode,
        "reviewed_files": reviewed_files,
        "replayed_findings": replayed_findings,
        "coverage": {
            "files": [path for path, _ in entries],
            "dimensions_expected": expected_by_file,
            "dimensions_planned": planned_by_file,
            "dropped_cells": dropped_labels,
            "skipped_prior_review": skipped_prior_review,
        },
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
