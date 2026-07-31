"""Repo-scoped beliefs: what worked here before, and what broke.

Threnody already *stores* per-project memory (:mod:`shared.memory`) but nothing
ever read it back into a prompt, so every run started from zero knowledge of the
repo. This module closes that loop.

Two kinds, both derived for free at swarm finalize — no LLM, no extra tokens:

``pattern``
    A run that succeeded cleanly (no rework, no new verify failures). Evidence
    that the approach taken here is the approach this repo accepts.
``constraint``
    A run that failed, needed rework, or left new verify failures. Evidence of a
    shape to avoid next time.

Beliefs are stored in the existing ``project`` memory scope under a ``belief:``
key prefix, so they inherit the FTS5 index, the size caps, and the operator
surfaces (``memory_list`` / ``memory_search``) for free — no new table and no
vector store. Retrieval ranks by FTS relevance blended with recency and how often
a belief has recurred, then hard-caps the injected text so a long repo history can
never crowd out the actual task.

Everything is best-effort: a missing DB or a malformed record degrades to "no
beliefs", which is exactly the fresh-repo behavior.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from shared.db import Database

log = logging.getLogger(__name__)

KIND_PATTERN = "pattern"
KIND_CONSTRAINT = "constraint"
VALID_KINDS = frozenset({KIND_PATTERN, KIND_CONSTRAINT})

BELIEF_PREFIX = "belief"
MEMORY_SCOPE = "project"

# Injection caps. Deliberately small: beliefs are supporting context, never the
# brief, and an unbounded history would dilute the task itself.
DEFAULT_MAX_INJECTED = 5
DEFAULT_MAX_CHARS = 1200
MAX_SUMMARY_CHARS = 300

# Recency half-life for ranking, in seconds (~14 days). A belief from last year
# should not outrank one from yesterday just because it matches more words.
_RECENCY_HALF_LIFE = 14 * 24 * 3600.0


class Belief(NamedTuple):
    kind: str
    summary: str
    paths: tuple[str, ...]
    hits: int
    updated_at: float
    key: str


def _belief_key(kind: str, summary: str) -> str:
    """Deterministic key so the same lesson recurring increments rather than duplicates."""
    from .agents import normalize_pattern

    digest = hashlib.sha256(
        normalize_pattern(summary).encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    return f"{BELIEF_PREFIX}:{kind}:{digest}"


def _is_belief_key(key: str) -> bool:
    return str(key or "").startswith(f"{BELIEF_PREFIX}:")


def record_belief(
    *,
    kind: str,
    summary: str,
    project_id: str,
    paths: "list[str] | tuple[str, ...]" = (),
    db: "Database | None" = None,
) -> bool:
    """Store or reinforce one belief for a project. Returns True when written.

    Recording the same lesson again bumps ``hits`` and the timestamp instead of
    creating a duplicate, so a repeatedly-hit constraint naturally outranks a
    one-off.
    """
    kind = str(kind or "").strip().lower()
    summary = " ".join(str(summary or "").split())[:MAX_SUMMARY_CHARS]
    if kind not in VALID_KINDS or not summary or not project_id:
        return False
    try:
        from .memory import MemoryNotFoundError, memory_get, memory_set

        key = _belief_key(kind, summary)
        hits = 1
        first_ts = time.time()
        try:
            existing = memory_get(MEMORY_SCOPE, key, project_id, db=db)
            value = existing.get("value")
            if isinstance(value, dict):
                hits = int(value.get("hits") or 0) + 1
                first_ts = float(value.get("first_ts") or first_ts)
        except MemoryNotFoundError:
            pass
        except Exception:
            log.debug("beliefs: existing lookup failed for %s", key, exc_info=True)
        memory_set(
            MEMORY_SCOPE,
            key,
            {
                "kind": kind,
                "summary": summary,
                "paths": [str(p) for p in paths][:20],
                "hits": hits,
                "first_ts": first_ts,
            },
            project_id,
            db=db,
        )
        return True
    except Exception:  # pragma: no cover - best-effort persistence
        log.debug("beliefs: record failed", exc_info=True)
        return False


def _belief_from_envelope(envelope: Any) -> Belief | None:
    if not isinstance(envelope, dict):
        return None
    value = envelope.get("value")
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "").strip().lower()
    summary = str(value.get("summary") or "").strip()
    if kind not in VALID_KINDS or not summary:
        return None
    raw_paths = value.get("paths")
    paths = tuple(str(p) for p in raw_paths) if isinstance(raw_paths, list) else ()
    try:
        hits = int(value.get("hits") or 1)
    except (TypeError, ValueError):
        hits = 1
    try:
        updated_at = float(envelope.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0
    return Belief(kind, summary, paths, hits, updated_at, str(envelope.get("key") or ""))


def _score(belief: Belief, *, now: float, fts_rank: float | None) -> float:
    """Blend FTS relevance, recency decay, and recurrence into one sort key.

    Higher is better. ``fts_rank`` is bm25 (lower is better) and is inverted; when
    absent, relevance contributes nothing and ordering falls back to recency and
    recurrence — which is the right default for "what should I know about this
    repo" with no specific query.
    """
    age = max(0.0, now - belief.updated_at)
    recency = 0.5 ** (age / _RECENCY_HALF_LIFE)
    recurrence = min(belief.hits / 5.0, 1.0)
    relevance = 0.0 if fts_rank is None else 1.0 / (1.0 + max(0.0, -fts_rank))
    return 0.45 * relevance + 0.35 * recency + 0.20 * recurrence


def load_beliefs(
    project_id: str,
    *,
    query: str = "",
    limit: int = DEFAULT_MAX_INJECTED,
    db: "Database | None" = None,
) -> list[Belief]:
    """Return the most relevant beliefs for a project, best first.

    With a ``query`` the FTS index narrows the candidate set; without one the whole
    belief set for the project is ranked by recency and recurrence. Constraints are
    preferred over patterns on ties, because "do not do X" prevents more damage
    than "X worked" reinforces.
    """
    if not project_id or limit < 1:
        return []
    try:
        from .memory import memory_get, memory_list, memory_search
    except Exception:  # pragma: no cover - defensive import
        return []

    ranks: dict[str, float] = {}
    keys: list[str] = []
    if query.strip():
        try:
            for hit in memory_search(
                query, scope=MEMORY_SCOPE, project_id=project_id, limit=50, db=db
            ):
                key = str(hit.get("key") or "")
                if _is_belief_key(key):
                    keys.append(key)
                    ranks[key] = float(hit.get("rank") or 0.0)
        except Exception:
            log.debug("beliefs: FTS search failed", exc_info=True)
    if not keys:
        try:
            keys = [
                str(row.get("key") or "")
                for row in memory_list(MEMORY_SCOPE, project_id, db=db)
                if _is_belief_key(str(row.get("key") or ""))
            ]
        except Exception:
            log.debug("beliefs: list failed", exc_info=True)
            return []

    now = time.time()
    scored: list[tuple[float, int, Belief]] = []
    for key in keys:
        try:
            belief = _belief_from_envelope(memory_get(MEMORY_SCOPE, key, project_id, db=db))
        except Exception:
            continue
        if belief is None:
            continue
        kind_bonus = 0 if belief.kind == KIND_CONSTRAINT else 1
        scored.append((-_score(belief, now=now, fts_rank=ranks.get(key)), kind_bonus, belief))
    scored.sort(key=lambda item: (item[0], item[1], item[2].summary))
    return [item[2] for item in scored[:limit]]


def format_belief_block(
    beliefs: "list[Belief]", *, max_chars: int = DEFAULT_MAX_CHARS
) -> str:
    """Render beliefs as a bounded prompt block, or ``""`` when there are none.

    Constraints and patterns are labelled separately and framed as prior evidence
    rather than instructions, so a stale belief cannot override what the agent
    actually finds in the code.
    """
    if not beliefs:
        return ""
    constraints = [b for b in beliefs if b.kind == KIND_CONSTRAINT]
    patterns = [b for b in beliefs if b.kind == KIND_PATTERN]
    lines: list[str] = []
    if constraints:
        lines.append("Avoid (these previously caused failures or rework here):")
        lines.extend(f"- {b.summary}" for b in constraints)
    if patterns:
        if lines:
            lines.append("")
        lines.append("Worked before in this repo:")
        lines.extend(f"- {b.summary}" for b in patterns)
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[: max(0, max_chars - 3)].rstrip() + "..."
    return (
        "\n\n## What this repo has taught us\n"
        "Prior evidence from earlier runs, not instructions — if the code "
        "contradicts a point below, trust the code and say so.\n" + body
    )


def build_belief_context(
    project_id: str,
    *,
    query: str = "",
    limit: int = DEFAULT_MAX_INJECTED,
    max_chars: int = DEFAULT_MAX_CHARS,
    db: "Database | None" = None,
) -> str:
    """Convenience wrapper: load + render in one call. Never raises."""
    try:
        return format_belief_block(
            load_beliefs(project_id, query=query, limit=limit, db=db),
            max_chars=max_chars,
        )
    except Exception:  # pragma: no cover - best-effort
        log.debug("beliefs: context build failed", exc_info=True)
        return ""


__all__ = [
    "KIND_PATTERN",
    "KIND_CONSTRAINT",
    "VALID_KINDS",
    "BELIEF_PREFIX",
    "DEFAULT_MAX_INJECTED",
    "DEFAULT_MAX_CHARS",
    "Belief",
    "record_belief",
    "load_beliefs",
    "format_belief_block",
    "build_belief_context",
]
