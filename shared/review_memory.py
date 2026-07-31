"""Prior-review persistence: skip re-reviewing unchanged code, forget nothing.

Two tables, both written off the hot path at wave finalize:

``review_scans``
    One row per ``(path, content_sha, dimension)`` that has actually been
    reviewed, including the tier it ran at. This is what makes a *skip* safe: if
    the exact file revision was already reviewed by an equal-or-stronger tier,
    re-running that agent cannot produce new information, so the cell is dropped
    from the plan and its stored findings are replayed into synthesis instead.

``review_findings``
    Per-finding lifecycle keyed by a content fingerprint rather than by revision,
    so a finding's status survives unrelated edits to the file. A finding that
    stops being reported once the file content changes is marked ``resolved``,
    and resolved fingerprints are fed back into later prompts as "already fixed —
    do not re-report", which is what stops a review swarm from rediscovering the
    same issue every run.

Everything here is best-effort: a missing table, a locked DB, or a malformed
payload degrades to "no memory" (full review, nothing skipped) and never raises
into planning or finalize.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any, Mapping, NamedTuple

if TYPE_CHECKING:
    from shared.db import Database

log = logging.getLogger(__name__)

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_WONTFIX = "wontfix"

_TIER_RANK = {"low": 0, "medium": 1, "high": 2}

# Hard cap on replayed/suppressed findings injected into a prompt, so review
# memory can never grow the prompt without bound.
MAX_CONTEXT_FINDINGS = 15


class StoredFinding(NamedTuple):
    fingerprint: str
    category: str
    severity: str
    line: int
    summary: str
    status: str


class CachedScan(NamedTuple):
    path: str
    dimension: str
    tier: str
    findings_total: int
    findings_high: int
    findings: tuple[StoredFinding, ...]
    ts: float


def finding_fingerprint(dimension: str, category: str, summary: str) -> str:
    """Stable identity for a finding, independent of line numbers.

    Line numbers shift on every unrelated edit, so they are deliberately excluded:
    a fingerprint must survive reformatting for the resolve-by-absence logic to
    mean anything. Reuses ``normalize_pattern`` from :mod:`shared.agents` (the
    repo's canonical text normalizer) to fold paths, quotes, and whitespace.
    """
    from .agents import normalize_pattern

    payload = "|".join((
        str(dimension or "").strip().lower(),
        str(category or "").strip().lower(),
        normalize_pattern(str(summary or "")),
    ))
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:24]


def tier_covers(cached_tier: str, planned_tier: str) -> bool:
    """True when a cached scan's tier is at least as strong as the planned one.

    A cell previously reviewed at ``low`` must NOT satisfy a plan that now wants
    ``high`` — the stronger reviewer may see what the cheaper one missed. Unknown
    tiers are treated as not covering, so an unrecognised value fails safe toward
    re-reviewing.
    """
    have = _TIER_RANK.get(str(cached_tier or "").strip().lower())
    want = _TIER_RANK.get(str(planned_tier or "").strip().lower())
    if have is None or want is None:
        return False
    return have >= want


def _parse_findings(raw: Any) -> list[dict[str, Any]]:
    """Normalize a host-reported ``review_meta['findings']`` list.

    Accepted per-item shape (all optional except a non-empty summary):
    ``{"category": str, "severity": str, "line": int, "summary": str}``.
    Anything unparseable is skipped rather than failing the whole batch.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        summary = str(item.get("summary") or item.get("description") or "").strip()
        if not summary:
            continue
        try:
            line = int(item.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        out.append({
            "category": str(item.get("category") or "").strip().lower(),
            "severity": str(item.get("severity") or "").strip().lower() or "medium",
            "line": line,
            "summary": summary,
        })
    return out


def record_review_scan(
    db: Database,
    *,
    path: str,
    content_sha: str,
    dimension: str,
    tier: str,
    findings_total: int,
    findings_high: int,
    findings: Any = None,
    model: str | None = None,
) -> None:
    """Persist that ``(path, content_sha, dimension)`` was reviewed, plus findings.

    Findings detail is optional: when the host reports only counts, the scan row
    still enables the skip path — it just replays a count rather than a list. When
    detail *is* reported, each finding is upserted as ``open`` and any previously
    open fingerprint that is absent from this report is marked ``resolved`` against
    the new revision.
    """
    if not path or not content_sha or not dimension:
        return
    parsed = _parse_findings(findings)
    now = time.time()
    try:
        with db.conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO review_scans "
                "(path, content_sha, dimension, tier, findings_total, findings_high, "
                " model, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    path,
                    content_sha,
                    dimension,
                    str(tier or "").strip().lower(),
                    int(findings_total or 0),
                    int(findings_high or 0),
                    (model or None),
                    now,
                ),
            )
            if not parsed:
                # No detail reported — do not touch finding lifecycle state. Absence
                # of detail is not evidence a previously open finding is fixed.
                return
            seen: set[str] = set()
            for finding in parsed:
                fp = finding_fingerprint(dimension, finding["category"], finding["summary"])
                seen.add(fp)
                existing = conn.execute(
                    "SELECT first_seen_ts, first_seen_sha FROM review_findings "
                    "WHERE path = ? AND dimension = ? AND fingerprint = ?",
                    (path, dimension, fp),
                ).fetchone()
                first_ts = float(existing[0]) if existing else now
                first_sha = (existing[1] if existing else content_sha) or content_sha
                conn.execute(
                    "INSERT OR REPLACE INTO review_findings "
                    "(path, dimension, fingerprint, category, severity, line, summary, "
                    " status, first_seen_sha, last_seen_sha, resolved_sha, "
                    " first_seen_ts, last_seen_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        path,
                        dimension,
                        fp,
                        finding["category"],
                        finding["severity"],
                        finding["line"],
                        finding["summary"],
                        STATUS_OPEN,
                        first_sha,
                        content_sha,
                        first_ts,
                        now,
                    ),
                )
            # Resolve-by-absence, but only against a *different* revision: on a
            # re-review of the identical content, a missing finding means the two
            # runs disagreed, not that anything was fixed.
            conn.execute(
                "UPDATE review_findings SET status = ?, resolved_sha = ?, last_seen_ts = ? "
                "WHERE path = ? AND dimension = ? AND status = ? "
                "  AND last_seen_sha != ? AND fingerprint NOT IN ({})".format(
                    ",".join("?" * len(seen)) if seen else "''"
                ),
                (
                    STATUS_RESOLVED,
                    content_sha,
                    now,
                    path,
                    dimension,
                    STATUS_OPEN,
                    content_sha,
                    *sorted(seen),
                ),
            )
    except Exception:  # pragma: no cover - best-effort persistence
        log.debug("review_memory: scan record failed for %s", path, exc_info=True)


def load_cached_scan(
    db: Database, path: str, content_sha: str, dimension: str
) -> CachedScan | None:
    """Return the stored scan for this exact file revision + dimension, or None."""
    if not path or not content_sha or not dimension:
        return None
    try:
        with db.conn() as conn:
            row = conn.execute(
                "SELECT tier, findings_total, findings_high, ts FROM review_scans "
                "WHERE path = ? AND content_sha = ? AND dimension = ?",
                (path, content_sha, dimension),
            ).fetchone()
            if row is None:
                return None
            found = conn.execute(
                # Explicit severity rank: ORDER BY severity would sort the text
                # alphabetically ('high' < 'low' < 'medium'), burying the worst
                # findings below the cap.
                "SELECT fingerprint, category, severity, line, summary, status "
                "FROM review_findings WHERE path = ? AND dimension = ? AND status = ? "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                "  WHEN 'medium' THEN 2 ELSE 3 END, line LIMIT ?",
                (path, dimension, STATUS_OPEN, MAX_CONTEXT_FINDINGS),
            ).fetchall()
    except Exception:  # pragma: no cover - best-effort read
        log.debug("review_memory: cached scan read failed for %s", path, exc_info=True)
        return None
    return CachedScan(
        path=path,
        dimension=dimension,
        tier=str(row[0] or ""),
        findings_total=int(row[1] or 0),
        findings_high=int(row[2] or 0),
        findings=tuple(
            StoredFinding(str(f[0]), str(f[1] or ""), str(f[2] or ""), int(f[3] or 0), str(f[4] or ""), str(f[5] or ""))
            for f in found
        ),
        ts=float(row[3] or 0.0),
    )


def load_resolved_findings(
    db: Database, path: str, dimension: str, *, limit: int = MAX_CONTEXT_FINDINGS
) -> list[StoredFinding]:
    """Findings previously reported for this file+dimension and since fixed."""
    if not path or not dimension:
        return []
    try:
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT fingerprint, category, severity, line, summary, status "
                "FROM review_findings WHERE path = ? AND dimension = ? AND status = ? "
                "ORDER BY last_seen_ts DESC LIMIT ?",
                (path, dimension, STATUS_RESOLVED, max(1, int(limit))),
            ).fetchall()
    except Exception:  # pragma: no cover - best-effort read
        log.debug("review_memory: resolved read failed for %s", path, exc_info=True)
        return []
    return [
        StoredFinding(str(r[0]), str(r[1] or ""), str(r[2] or ""), int(r[3] or 0), str(r[4] or ""), str(r[5] or ""))
        for r in rows
    ]


def format_resolved_block(findings: "list[StoredFinding]") -> str:
    """Prompt block listing already-fixed findings to suppress."""
    if not findings:
        return ""
    lines = [f"- [{f.severity}] {f.category or 'finding'}: {f.summary}" for f in findings]
    return (
        "\n\nPreviously reported here and since FIXED — do not report these again "
        "unless the defect has genuinely returned:\n" + "\n".join(lines)
    )


def format_replay_block(scans: "list[CachedScan]") -> str:
    """Synthesis block describing cells skipped because the revision was cached."""
    if not scans:
        return ""
    lines: list[str] = []
    for scan in scans:
        if scan.findings:
            for f in scan.findings:
                lines.append(
                    f"- {scan.path} [{f.severity}] {scan.dimension}/{f.category or 'finding'}"
                    f" — line {f.line} — {f.summary}"
                )
        elif scan.findings_total:
            lines.append(
                f"- {scan.path} {scan.dimension}: {scan.findings_total} finding(s) "
                f"({scan.findings_high} high) recorded previously, detail not stored"
            )
        else:
            lines.append(f"- {scan.path} {scan.dimension}: previously reviewed, clean")
    return (
        "\n\nCarried over from a prior review of these exact file revisions (no agent "
        "was re-run because the content is unchanged). Treat these as first-class "
        "findings in the report:\n" + "\n".join(lines)
    )


__all__ = [
    "STATUS_OPEN",
    "STATUS_RESOLVED",
    "STATUS_WONTFIX",
    "MAX_CONTEXT_FINDINGS",
    "StoredFinding",
    "CachedScan",
    "finding_fingerprint",
    "tier_covers",
    "record_review_scan",
    "load_cached_scan",
    "load_resolved_findings",
    "format_resolved_block",
    "format_replay_block",
]
