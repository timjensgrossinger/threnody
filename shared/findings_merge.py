"""Parse, dedup, rank and render review findings without an LLM.

The review fan-out already mandates one exact finding format (see
``review_fanout.REVIEW_DIMENSIONS``)::

    ⚠️ [SEVERITY] dimension/category — file:line — description (CWE-XXX)

Given that, merging N dimension agents' findings into one ranked report is a
mechanical operation: parse, drop duplicates, sort. Spending a synthesis *agent* on
it costs the excerpts of every prior agent re-sent as its context, and the same
excerpts a third time in the wave report.

This module does that merge in-process instead, and — because it parses the findings
itself — can derive the per-category ``review_meta`` breakdown that
``model_quality.record_static_recall_score`` needs but which is skipped today
whenever a host reports findings without categories.

Findings arrive by file: each review agent writes to
``<run_dir>/findings/<spawn_id>.md`` and returns only counts, so agent reports stop
accumulating in the parent conversation.

Public API
----------
    findings_dir(run_id) / findings_path(run_id, spawn_id)
    parse_findings_text(text, ...) -> list[Finding]
    read_run_findings(run_id) -> dict[str, list[Finding]]
    merge(findings) -> MergeResult
    render_report(result, reviewed_files=...) -> str
    review_meta_for(findings, ...) -> dict
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, NamedTuple

log = logging.getLogger(__name__)

FINDINGS_SUBDIR = "findings"

# Severity vocabulary, weakest to strongest. Anything unrecognized normalizes to
# "medium": dropping a finding because its severity word was odd would be worse than
# ranking it imprecisely.
SEVERITY_ORDER = ("low", "medium", "high", "critical")
_SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITY_ORDER)}
DEFAULT_SEVERITY = "medium"
# Severities that count toward ``findings_high`` in the learning ledger.
_HIGH_SEVERITIES = frozenset({"high", "critical"})

# Em dash, en dash, or one-or-more hyphens, as the field separator. Models are not
# reliable about which dash they emit, and the separator carries no meaning.
_DASH = r"(?:\s*(?:—|–|-{1,3})\s*)"

_FINDING_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?"                      # optional bullet
    r"(?:⚠️?️?\s*)?"                      # optional warning glyph
    r"\[(?P<severity>[A-Za-z]+)\]"            # [HIGH]
    r"\s*(?P<dimension>[A-Za-z_]+)"           # security
    r"(?:/(?P<category>[A-Za-z0-9_-]+))?"     # /sql-injection
    + _DASH
    + r"(?P<path>[^\s:]+?):(?P<line>\d+)"     # file:line
    + _DASH
    + r"(?P<description>.+?)\s*$",
    re.UNICODE,
)

_CWE_RE = re.compile(r"\(?(CWE-\d+)\)?", re.IGNORECASE)

# Lines an agent may emit around its findings that are not findings.
_NOISE_RE = re.compile(
    r"^\s*(?:no (?:issues|findings)|none found|clean|#{1,6}\s|dim=)",
    re.IGNORECASE,
)


class Finding(NamedTuple):
    """One parsed finding."""

    dimension: str
    category: str
    severity: str
    path: str
    line: int
    description: str
    cwe: str = ""
    source: str = ""  # spawn id of the agent that reported it

    @property
    def is_high(self) -> bool:
        return self.severity in _HIGH_SEVERITIES

    def format_line(self) -> str:
        """Render back to the canonical report line."""
        label = f"{self.dimension}/{self.category}" if self.category else self.dimension
        suffix = f" ({self.cwe})" if self.cwe else ""
        return (
            f"⚠️ [{self.severity.upper()}] {label} — "
            f"{self.path}:{self.line} — {self.description}{suffix}"
        )


@dataclass
class MergeResult:
    """Outcome of merging every agent's findings for one run."""

    kept: list[Finding] = field(default_factory=list)
    duplicates: list[Finding] = field(default_factory=list)
    # fingerprint -> number of agents that reported it (>=1)
    agreement: dict[str, int] = field(default_factory=dict)

    @property
    def counts_by_severity(self) -> dict[str, int]:
        out = {name: 0 for name in SEVERITY_ORDER}
        for finding in self.kept:
            out[finding.severity] = out.get(finding.severity, 0) + 1
        return out

    @property
    def total(self) -> int:
        return len(self.kept)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def findings_dir(run_id: str, *, create: bool = False) -> Path:
    """``<run_dir>/findings`` for *run_id*."""
    from .run_log import run_log_dir

    directory = run_log_dir(run_id, create=create) / FINDINGS_SUBDIR
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def findings_path(run_id: str, spawn_id: str) -> Path:
    """Per-agent findings file. *spawn_id* is sanitized to one path segment."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(spawn_id or "agent").strip()) or "agent"
    if safe in {".", ".."}:
        safe = "agent"
    return findings_dir(run_id) / f"{safe}.md"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _normalize_severity(raw: str) -> str:
    value = str(raw or "").strip().lower()
    return value if value in _SEVERITY_RANK else DEFAULT_SEVERITY


def parse_findings_text(
    text: str,
    *,
    default_dimension: str = "",
    source: str = "",
) -> list[Finding]:
    """Extract findings from one agent's report text.

    Unparseable lines are skipped rather than failing the batch — an agent that adds
    a prose header should not cost us its real findings. *default_dimension* is used
    only when a line omits it.
    """
    if not text or not text.strip():
        return []
    out: list[Finding] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or _NOISE_RE.match(raw_line):
            continue
        match = _FINDING_RE.match(raw_line)
        if match is None:
            continue
        description = match.group("description").strip()
        cwe_match = _CWE_RE.search(description)
        cwe = cwe_match.group(1).upper() if cwe_match else ""
        if cwe:
            # Strip the trailing CWE marker so it is not duplicated on re-render.
            description = _CWE_RE.sub("", description).strip().rstrip("—-–").strip()
        try:
            line_no = int(match.group("line"))
        except (TypeError, ValueError):
            line_no = 0
        dimension = (match.group("dimension") or default_dimension or "").strip().lower()
        out.append(
            Finding(
                dimension=dimension,
                category=(match.group("category") or "").strip().lower(),
                severity=_normalize_severity(match.group("severity")),
                path=match.group("path").strip(),
                line=line_no,
                description=description,
                cwe=cwe,
                source=source,
            )
        )
    return out


REPLAY_SOURCE = "replay"


def write_findings(run_id: str, spawn_id: str, findings: Iterable[Finding]) -> Path | None:
    """Write *findings* as a findings file for *spawn_id*. Returns the path or None.

    Used for findings that no agent in this run produced — notably cells served from
    prior-review memory. Without this, a fully-cached review run under in-process
    synthesis would report nothing at all: there is no synthesis agent to receive the
    replay block, and no agent wrote a findings file.
    """
    try:
        findings_dir(run_id, create=True)
        path = findings_path(run_id, spawn_id)
        path.write_text(
            "\n".join(f.format_line() for f in findings) + "\n", encoding="utf-8"
        )
        return path
    except Exception:
        log.debug("findings_merge: write_findings failed for %s", run_id, exc_info=True)
        return None


def read_run_findings(run_id: str) -> dict[str, list[Finding]]:
    """Read every ``<run_dir>/findings/*.md`` file as {spawn_id: findings}.

    Missing directory → empty dict, which is how a run that used the legacy
    in-conversation protocol is detected.
    """
    directory = findings_dir(run_id)
    if not directory.is_dir():
        return {}
    out: dict[str, list[Finding]] = {}
    for path in sorted(directory.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log.debug("findings_merge: unreadable findings file %s", path, exc_info=True)
            continue
        spawn_id = path.stem
        out[spawn_id] = parse_findings_text(text, source=spawn_id)
    return out


# ---------------------------------------------------------------------------
# Merge + render
# ---------------------------------------------------------------------------

def _dimension_rank(dimension: str) -> int:
    """Report order for dimensions, taken from the fan-out's own definition."""
    try:
        from .review_fanout import REVIEW_DIMENSIONS

        order = [d.key for d in REVIEW_DIMENSIONS]
    except Exception:  # pragma: no cover - import guard
        order = ["security", "logic", "edge", "types", "performance"]
    try:
        return order.index(str(dimension or "").strip().lower())
    except ValueError:
        return len(order)


def merge(findings: Iterable[Finding]) -> MergeResult:
    """Dedup by fingerprint and rank: severity desc, then dimension, then file:line.

    Deduplication keeps the highest-severity instance, matching what the LLM
    synthesis prompt asks for. The fingerprint is
    ``review_memory.finding_fingerprint`` — line-independent on purpose, so the same
    defect reported at different line numbers by two agents collapses to one.
    """
    from .review_memory import finding_fingerprint

    best: dict[str, Finding] = {}
    duplicates: list[Finding] = []
    agreement: dict[str, int] = {}

    for finding in findings:
        fingerprint = finding_fingerprint(
            finding.dimension, finding.category, finding.description
        )
        agreement[fingerprint] = agreement.get(fingerprint, 0) + 1
        incumbent = best.get(fingerprint)
        if incumbent is None:
            best[fingerprint] = finding
            continue
        if _SEVERITY_RANK[finding.severity] > _SEVERITY_RANK[incumbent.severity]:
            best[fingerprint] = finding
            duplicates.append(incumbent)
        else:
            duplicates.append(finding)

    kept = sorted(
        best.values(),
        key=lambda f: (
            -_SEVERITY_RANK[f.severity],
            _dimension_rank(f.dimension),
            f.path,
            f.line,
        ),
    )
    return MergeResult(kept=kept, duplicates=duplicates, agreement=agreement)


def render_report(
    result: MergeResult,
    *,
    reviewed_files: Iterable[str] | None = None,
) -> str:
    """Render the ranked report in the same shape the synthesis agent was asked for.

    Keeping the output shape identical matters: downstream consumers (and the
    operator) should not be able to tell whether a run merged in Python or via the
    LLM, other than by cost.
    """
    files = sorted({f for f in (reviewed_files or []) if f})
    if not result.kept:
        body = "No issues found."
        if files:
            body += f"\n\nFiles reviewed: {', '.join(files)}"
        return body

    counts = result.counts_by_severity
    summary = ", ".join(
        f"{counts.get(name, 0)} {name}"
        for name in reversed(SEVERITY_ORDER)
        if counts.get(name, 0)
    )
    file_count = len({f.path for f in result.kept})
    lines = [
        "### Summary",
        f"{summary} issues across {file_count} file(s).",
        "",
        "### Findings (ranked: critical → high → medium → low; "
        "then security > logic > edge > types > performance)",
        "",
    ]
    lines.extend(finding.format_line() for finding in result.kept)
    if result.duplicates:
        lines.append("")
        lines.append(
            f"{len(result.duplicates)} duplicate report(s) collapsed into the above."
        )
    if files:
        lines.append("")
        lines.append(f"Files reviewed: {', '.join(files)}")
    return "\n".join(lines)


def review_meta_for(
    findings: list[Finding],
    *,
    kept_fingerprints: set[str] | None = None,
) -> dict:
    """Build a ``review_meta`` payload for one agent's findings.

    Always includes ``categories``, which is the point: the static-recall scorer in
    :mod:`shared.model_quality` skips scoring entirely when a host reports findings
    without a category breakdown, so deriving it here converts a silently-dropped
    objective signal into a recorded one.
    """
    from .review_memory import finding_fingerprint

    categories: dict[str, dict[str, int | bool]] = {}
    total = 0
    high = 0
    for finding in findings:
        total += 1
        if finding.is_high:
            high += 1
        slug = (
            f"{finding.dimension}/{finding.category}"
            if finding.category
            else finding.dimension
        )
        bucket = categories.setdefault(
            slug, {"findings_total": 0, "findings_high": 0, "kept": False}
        )
        bucket["findings_total"] = int(bucket["findings_total"]) + 1
        if finding.is_high:
            bucket["findings_high"] = int(bucket["findings_high"]) + 1
        if kept_fingerprints is None:
            bucket["kept"] = True
        elif finding_fingerprint(
            finding.dimension, finding.category, finding.description
        ) in kept_fingerprints:
            bucket["kept"] = True
    return {
        "findings_total": total,
        "findings_high": high,
        "categories": categories,
        "findings": [
            {
                "category": f.category or f.dimension,
                "severity": f.severity,
                "line": f.line,
                "summary": f.description,
            }
            for f in findings
        ],
    }


__all__ = [
    "DEFAULT_SEVERITY",
    "FINDINGS_SUBDIR",
    "SEVERITY_ORDER",
    "Finding",
    "MergeResult",
    "findings_dir",
    "findings_path",
    "merge",
    "parse_findings_text",
    "read_run_findings",
    "render_report",
    "review_meta_for",
]
