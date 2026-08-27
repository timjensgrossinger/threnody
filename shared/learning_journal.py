"""
Append-only durable journal for everything the learning loops accumulate.

SQLite is the query surface, not the system of record. This install has
quarantined nine corrupt ``cache.db`` images since June; ``_recover_db_locked``
can only restore a backup up to ``backup_interval_hours`` old or quarantine the
file and recreate it empty, and an empty recreate resets ``review_tier_bias``,
``model_quality_events``, ``project_routing``, ``subtask_patterns`` and every
other accumulated table at once. Worse, the terminal learning report is the
single write-heaviest operation in the system, so the moment a run is most
likely to hit a malformed image is exactly the moment its learning would be
lost.

So the ordering is inverted: a learning event is durably appended here **first**,
then written to the DB. If the DB write fails — or the whole file is later
quarantined — ``replay()`` rebuilds the tables from this log. The journal is
plain JSONL under

    ~/.local/lib/threnody/journal/<YYYY-MM>.jsonl

appended with ``O_APPEND`` and fsynced, never touched by the DB backup rotation,
and never pruned automatically.

Two properties make replay safe to run at any time, any number of times:

* every event carries a deterministic ``event_id`` derived from its identity
  fields, so re-appending the same logical event twice is detectable; and
* every registered replay handler is an idempotent upsert keyed on that id.

That second property also fixes a live double-count: the warm-path executor
retries a run whose terminal ``report_host_swarm_complete`` failed partway, and
before this every row the failed attempt had already written was counted twice.

This module deliberately depends on ``config`` for the base path and nothing
else at import time. DB access is imported lazily inside ``replay`` so the write
path stays usable from contexts (hooks, one-shot CLIs) that must not open the
database.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .config import BASE_DIR

log = logging.getLogger(__name__)

JOURNAL_ROOT = BASE_DIR / "journal"

# Env override for the journal root, honoured at *call* time.
#
# ``BASE_DIR`` is resolved from ``THRENODY_INSTALL_DIR`` when ``config`` is
# imported, which is far too early for a test to influence: by the time a test
# body runs, ``shared.config`` is long since imported. That is not a theoretical
# gap — it is how the reference install's entire learning ledger came to be test
# fixtures. Every recorder appends here *before* its DB write, so a test that
# correctly isolates its ``Database`` to a tmp_path still wrote its fixtures into
# the operator's real journal, and the next ``replay_learning_journal(rebuild=True)``
# faithfully reconstructed them into the live tables.
#
# Resolving at call time closes that. ``JOURNAL_ROOT`` stays a writable module
# attribute so the existing ``monkeypatch.setattr(learning_journal,
# "JOURNAL_ROOT", ...)`` form keeps working unchanged.
_JOURNAL_ROOT_ENV = "THRENODY_JOURNAL_ROOT"
_DEFAULT_JOURNAL_ROOT = JOURNAL_ROOT


def journal_root() -> Path:
    """Resolve the journal root now, not at import time.

    Precedence, most specific first:

    1. ``JOURNAL_ROOT`` if it has been reassigned away from its import-time
       default — an explicit in-process override (a test monkeypatching the
       attribute) is the most specific statement of intent there is, so it must
       win over an ambient env var set by a broader fixture.
    2. ``THRENODY_JOURNAL_ROOT``, which also reaches subprocesses.
    3. The import-time default under ``BASE_DIR``.

    Never cached — a caller may redirect it between calls.
    """
    if JOURNAL_ROOT != _DEFAULT_JOURNAL_ROOT:
        return JOURNAL_ROOT
    override = os.environ.get(_JOURNAL_ROOT_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return JOURNAL_ROOT


# Event kinds. Adding one is additive: an unknown kind is skipped by replay and
# counted under `unknown`, never an error — an older install must be able to read
# a journal written by a newer one.
KIND_HANDOFF_AGENT = "handoff_agent"
KIND_MODEL_QUALITY = "model_quality"
KIND_REVIEW_TIER = "review_tier_outcome"
KIND_HYBRID = "hybrid_outcome"

# Identity fields per kind: what makes two appends "the same logical event".
# Chosen so a replay of the same run can never inflate a counter, while two
# genuinely distinct observations stay distinct.
_IDENTITY: dict[str, tuple[str, ...]] = {
    KIND_HANDOFF_AGENT: ("run_id", "spawn_id", "task_id"),
    KIND_MODEL_QUALITY: (
        "run_id",
        "spawn_id",
        "source",
        "model",
        "dimension",
        "sub_dimension",
        "task_hash",
        # Tier is a genuine distinguishing axis, not decoration: the same model can
        # be graded at two tiers in one run (a ladder sweep does exactly that, and
        # two tiers can resolve to the same model). Without it those are one event
        # and ``ON CONFLICT DO NOTHING`` silently keeps only the first — which would
        # leave ``build_min_passing_tier_map`` looking at a partial sweep and, by
        # its all-or-nothing rule, crediting no tier at all.
        "tier",
    ),
    KIND_REVIEW_TIER: ("run_id", "spawn_id", "profile_key", "dimension", "tier"),
    KIND_HYBRID: ("run_id", "profile_key", "delta"),
}


# At least one of these must be present for an event to be *addressable*, i.e.
# for "the same event seen twice" to be a meaningful statement. Without any of
# them, two genuinely distinct observations of the same model on the same
# dimension are indistinguishable, and deduping them would silently discard real
# data — the opposite of the problem idempotency exists to solve.
_ADDRESSABLE_FIELDS = ("run_id", "spawn_id", "task_hash")

# Keys the record envelope owns. A payload must never use them — see append().
_RESERVED_RECORD_KEYS = ("event_id", "kind", "ts")
_unaddressable_counter = itertools.count()


def event_id(kind: str, payload: Mapping[str, Any]) -> str:
    """Deterministic id for *payload* under *kind*'s identity fields.

    Falls back to hashing the whole payload for an unregistered kind, so a new
    event type is still idempotent before its identity is pinned down.

    An event carrying no addressable identity gets a unique id instead of a
    content hash: it can still be replayed, it just cannot be deduplicated,
    which is the correct trade when the alternative is dropping a real
    observation.
    """
    fields = _IDENTITY.get(kind)
    if fields:
        basis = "|".join(f"{f}={payload.get(f)!r}" for f in fields)
    else:
        basis = json.dumps(payload, sort_keys=True, default=str)
    if not any(str(payload.get(f) or "").strip() for f in _ADDRESSABLE_FIELDS):
        basis = f"{basis}|unaddressable={time.time_ns()}-{next(_unaddressable_counter)}"
    return hashlib.sha256(f"{kind}|{basis}".encode("utf-8")).hexdigest()[:32]


def journal_path(ts: float | None = None) -> Path:
    """Month-sharded journal file. Sharding keeps any single file scannable."""
    stamp = time.strftime("%Y-%m", time.localtime(ts if ts is not None else time.time()))
    return journal_root() / f"{stamp}.jsonl"


def append(kind: str, payload: Mapping[str, Any], *, ts: float | None = None) -> str:
    """Durably append one learning event and return its ``event_id``.

    Best-effort by contract: a journal failure must never break the caller's
    real work, so every error is logged and swallowed. The return value is still
    the event id, so the caller can stamp its DB row with it either way.
    """
    body = dict(payload)
    # The record shape is {"event_id", "kind", "ts", **body}, so a payload key with
    # one of those names silently wins over the real value — and a clobbered `kind`
    # makes the event replay as `unknown`, i.e. unrecoverable. Drop and log rather
    # than corrupt the record; a payload cannot legitimately own these names.
    shadowed = [k for k in _RESERVED_RECORD_KEYS if k in body]
    if shadowed:
        log.warning(
            "learning_journal: payload for kind=%s shadows reserved key(s) %s — dropping them",
            kind, ", ".join(sorted(shadowed)),
        )
        for key in shadowed:
            body.pop(key, None)
    when = float(ts if ts is not None else time.time())
    eid = event_id(kind, body)
    record = {"event_id": eid, "kind": kind, "ts": when, **body}
    line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
    try:
        journal_root().mkdir(parents=True, exist_ok=True)
        path = journal_path(when)
        # O_APPEND makes concurrent writers safe for lines under PIPE_BUF, and
        # the fsync is the whole point: an event that is not on disk before the
        # DB write cannot rebuild that write after a crash.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        log.debug("learning_journal: append failed for kind=%s", kind, exc_info=True)
    return eid


def iter_events(since: float | None = None) -> Iterator[dict[str, Any]]:
    """Yield journal events in file order, oldest shard first.

    A truncated trailing line (crash mid-append) is skipped, matching
    ``run_log.read_run_log``'s tolerance — a partial record must never make the
    whole journal unreadable.
    """
    root = journal_root()
    if not root.exists():
        return
    for path in sorted(root.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        log.debug("learning_journal: skipping malformed line in %s", path)
                        continue
                    if not isinstance(event, dict):
                        continue
                    if since is not None and float(event.get("ts") or 0.0) < since:
                        continue
                    yield event
        except OSError:
            log.debug("learning_journal: unreadable shard %s", path, exc_info=True)


# --- replay ---------------------------------------------------------------

_HANDLERS: dict[str, Callable[[Any, dict[str, Any]], None]] = {}
# Kinds whose DB representation is an order-dependent EMA, not a keyed row.
# Re-applying one twice moves the average twice, so they are replayed ONLY in a
# full rebuild, which resets the target table first and re-applies the whole
# ordered observation stream. Journaling them is still what makes that rebuild
# possible at all — the raw observations exist nowhere else once the EMA has
# absorbed them.
_EMA_KINDS = frozenset({KIND_REVIEW_TIER, KIND_HYBRID})
_RESET_TABLES = {KIND_REVIEW_TIER: "review_tier_bias", KIND_HYBRID: "hybrid_tier_bias"}


def register_handler(kind: str, fn: Callable[[Any, dict[str, Any]], None]) -> None:
    """Register the DB writer that reconstitutes *kind*."""
    _HANDLERS[kind] = fn


def _install_default_handlers() -> None:
    """Bind handlers lazily so importing this module never imports the DB layer."""
    if _HANDLERS:
        return
    from . import journal_replay  # noqa: F401  (registers on import)


def replay(
    db: Any, *, since: float | None = None, rebuild: bool = False
) -> dict[str, int]:
    """Reconstitute DB state from the journal.

    ``rebuild=False`` (the default, and what auto-recovery uses) replays only the
    kinds whose DB write is an idempotent keyed upsert, so it is safe to run at
    any time against a database that may already hold some of the rows.

    ``rebuild=True`` additionally resets and recomputes the EMA-backed bias
    tables from the full ordered observation stream. Use it after a quarantine —
    it is the only mode that restores ``review_tier_bias`` / ``hybrid_tier_bias``,
    and the only one that must not run against a live populated DB.

    Returns per-kind counts plus ``unknown`` and ``failed``. A handler that
    raises is counted and skipped: one bad event must not abort a rebuild that
    is, by definition, running because something already went wrong.
    """
    _install_default_handlers()
    counts: dict[str, int] = {}
    if rebuild:
        for table in _RESET_TABLES.values():
            try:
                with db.conn() as conn:
                    conn.execute(f"DELETE FROM {table}")
            except Exception:
                log.debug("learning_journal: could not reset %s", table, exc_info=True)
    # Sort by timestamp: EMA replay is order-dependent, and month shards only
    # give file-order, which is not the same thing across a shard boundary.
    events = sorted(iter_events(since=since), key=lambda e: float(e.get("ts") or 0.0))
    for event in events:
        kind = str(event.get("kind") or "")
        if kind in _EMA_KINDS and not rebuild:
            counts["skipped_ema"] = counts.get("skipped_ema", 0) + 1
            continue
        handler = _HANDLERS.get(kind)
        if handler is None:
            counts["unknown"] = counts.get("unknown", 0) + 1
            continue
        try:
            handler(db, event)
        except Exception:
            log.debug("learning_journal: replay failed for %s", kind, exc_info=True)
            counts["failed"] = counts.get("failed", 0) + 1
            continue
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def stats() -> dict[str, Any]:
    """Operator summary: shard count, bytes, event count, newest timestamp."""
    root = journal_root()
    shards = sorted(root.glob("*.jsonl")) if root.exists() else []
    total_bytes = 0
    events = 0
    newest = 0.0
    by_kind: dict[str, int] = {}
    for path in shards:
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
    for event in iter_events():
        events += 1
        kind = str(event.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        newest = max(newest, float(event.get("ts") or 0.0))
    return {
        "root": str(root),
        "shards": [p.name for p in shards],
        "bytes": total_bytes,
        "events": events,
        "by_kind": by_kind,
        "newest_ts": newest or None,
    }
