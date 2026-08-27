"""Granular per-(model x effort x dimension) quality ledger.

Records a 0-10 quality score per scored agent output into ``model_quality_events``
from two sources and aggregates them at read time (mirrors ``shared/spend.py``):

* ``findings`` — free, derived from read-only ``REVIEW:`` agent findings at wave
  finalize. A precision proxy: findings the synthesis kept score high, findings it
  dropped (noise / false positives) score low. Never spends tokens, never on the
  spawn/hot path.
* ``judge`` — an opt-out warm-path LLM judge (``shared/eval.py``) that scores general
  task output 0-10. Runs only on the existing warm-path executor, so it adds zero
  latency to tasks or swarms.

Everything here is best-effort: writers never raise into the finalize/warm paths,
and ``build_quality_snapshot`` returns a well-formed empty snapshot on a fresh DB so
the CLI / MCP / docs surfaces render cleanly with n=0.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shared.config import TGsConfig
    from shared.db import Database

log = logging.getLogger(__name__)

SOURCE_FINDINGS = "findings"
SOURCE_JUDGE = "judge"
# Objective sources — graded against something deterministic rather than against
# another model's judgement. `static_recall` compares a reviewer's reported
# findings to the high-severity static pre-scan set; `verify_gate` records whether
# a write-path run left new test/type/lint failures; `ladder` records a graded
# benchmark pass/fail. Kept distinct from the two proxy sources so operators can
# tell ground truth from inference.
SOURCE_STATIC_RECALL = "static_recall"
SOURCE_VERIFY_GATE = "verify_gate"
SOURCE_LADDER = "ladder"

VALID_SOURCES = frozenset({
    SOURCE_FINDINGS,
    SOURCE_JUDGE,
    SOURCE_STATIC_RECALL,
    SOURCE_VERIFY_GATE,
    SOURCE_LADDER,
})
OBJECTIVE_SOURCES = frozenset({SOURCE_STATIC_RECALL, SOURCE_VERIFY_GATE, SOURCE_LADDER})
# Bucket used when the host did not resolve a concrete model for an agent (e.g.
# host-native review agents whose tier -> model resolution was unavailable). Kept
# explicit so a real model is never falsely credited/blamed.
MODEL_UNRESOLVED = "host-native"
# Sentinel dimension for non-review (judge) scores that have no review dimension.
DIMENSION_GENERAL = "general"

_DEFAULT_WINDOW = "7d"


def parse_quality_window(since: str) -> tuple[float, str]:
    """Parse window strings like ``7d`` / ``24h`` / ``all`` into ``(since_ts, label)``.

    Delegates to :func:`shared.spend.parse_spend_window` so the two operator
    surfaces share identical window semantics.
    """
    from .spend import parse_spend_window

    return parse_spend_window(since or _DEFAULT_WINDOW)


def findings_to_score(
    *,
    findings_high: int,
    findings_total: int,
    kept_by_synthesis: bool | None,
) -> float | None:
    """Map review findings to a 0-10 quality score (precision proxy).

    Returns ``None`` when there is no signal (no findings at all) so the ledger is
    not diluted by "clean" reviews — absence of findings is ambiguous (thorough
    model on clean code, or a model that simply found nothing). The score rewards
    findings the synthesis KEPT (true positives) and penalises findings it dropped
    (noise / false positives). Heuristic and intentionally coarse — tunable.

    ``kept_by_synthesis`` is tri-state, and ``None`` — no adjudicator ran — also
    returns ``None``. Nobody judged those findings, so there is no evidence about
    precision, only about yield; scoring them anyway is what made 7.0 the modal
    value of the entire ledger and made ``avg_score`` report a precision that was
    never measured. This does NOT starve the review-learning loop:
    ``review_learning.record_review_tier_outcome`` reads ``findings_high`` and the
    tri-state directly and keeps its own EMA, independent of this function. And
    because ``findings`` is excluded from ``OBJECTIVE_SOURCES``, no routing
    decision loses an input either.

    Validity caveat: because clean reviews earn no signal and any dropped finding
    scores low (even a correct high-severity one synthesis chose to drop), this
    signal skews toward models that surface *kept* findings on dirty code. Treat it
    as a RELATIVE learning signal, not an absolute quality benchmark (see the
    snapshot ``disclaimer``).
    """
    if findings_total <= 0:
        return None
    if kept_by_synthesis is None:
        return None  # no adjudicator ran -> yield only, not precision evidence
    if kept_by_synthesis is False:
        return 3.0  # findings dropped by synthesis -> noisy / low precision
    if findings_high > 0:
        return 10.0  # kept a real high-severity finding
    return 7.0  # kept lower-severity findings


def _normalize_model(model: str | None) -> str:
    m = (model or "").strip()
    return m or MODEL_UNRESOLVED


def _write_event(
    db: Database,
    *,
    model: str | None,
    effort: str | None,
    dimension: str,
    sub_dimension: str | None,
    score_0_10: float,
    source: str,
    sample_meta: dict[str, Any] | None = None,
    task_hash: str | None = None,
    run_id: str | None = None,
    tier: str | None = None,
    profile_key: str | None = None,
    spawn_id: str | None = None,
    kind: str | None = None,
    journal: bool = True,
    event_id: str | None = None,
    ts: float | None = None,
) -> None:
    """Insert one ledger event. Best-effort — never raises into caller paths.

    The event is appended to the durable journal *before* the DB write, so a
    malformed image (nine quarantines on this install since June) costs a replay
    rather than the data. ``journal=False`` is for the replay path itself, which
    is reading the journal and must not write back into it.
    """
    if source not in VALID_SOURCES:
        log.debug("model_quality: refusing unknown source %r", source)
        return
    try:
        score = max(0.0, min(10.0, float(score_0_10)))
    except (TypeError, ValueError):
        log.debug("model_quality: bad score %r", score_0_10)
        return
    meta_json: str | None = None
    if sample_meta is not None:
        try:
            meta_json = json.dumps(sample_meta, sort_keys=True)
        except (TypeError, ValueError):
            meta_json = None

    row = {
        "model": _normalize_model(model),
        "effort": (effort or None),
        "dimension": str(dimension or DIMENSION_GENERAL),
        "sub_dimension": (sub_dimension or None),
        "score_0_10": score,
        "source": source,
        "sample_meta": meta_json,
        "task_hash": (task_hash or None),
        "run_id": (run_id or None),
        "tier": (tier or None),
        "profile_key": (profile_key or None),
        "spawn_id": (spawn_id or None),
        # NOT "kind": the journal record is {"event_id", "kind", "ts", **body} and
        # its `kind` is the EVENT kind ("model_quality"). A payload key of the same
        # name silently overwrote it, so every quality event replayed as `unknown`
        # and could not be restored after a recovery.
        "task_kind": (kind or None),
    }
    when = float(ts) if ts is not None else time.time()
    if journal:
        from .learning_journal import KIND_MODEL_QUALITY, append

        event_id = append(KIND_MODEL_QUALITY, row, ts=when)
    write_quality_row(db, row, event_id=event_id, ts=when)


def write_quality_row(
    db: Database,
    row: dict[str, Any],
    *,
    event_id: str | None,
    ts: float,
) -> None:
    """Idempotent insert of one prepared ledger row.

    Shared by the live path and the journal replay. ``ON CONFLICT DO NOTHING``
    over the unique ``event_id`` index is what makes replaying a run — or the
    warm-path executor retrying a terminal report that failed partway — leave the
    counts unchanged instead of doubling them.
    """
    try:
        with db.conn() as conn:
            conn.execute(
                "INSERT INTO model_quality_events "
                "(model, effort, dimension, sub_dimension, score_0_10, source, "
                "sample_meta, task_hash, run_id, tier, profile_key, spawn_id, "
                "kind, event_id, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                # The uniqueness is a PARTIAL index (legacy rows carry no
                # event_id and must not all collide on NULL), and SQLite only
                # accepts a partial index as a conflict target when the clause
                # repeats its predicate verbatim. Without the WHERE this raises
                # "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE
                # constraint" — swallowed by the best-effort except below, so
                # every ledger write would silently vanish.
                "ON CONFLICT (event_id) WHERE event_id IS NOT NULL DO NOTHING",
                (
                    row.get("model"),
                    row.get("effort"),
                    row.get("dimension"),
                    row.get("sub_dimension"),
                    row.get("score_0_10"),
                    row.get("source"),
                    row.get("sample_meta"),
                    row.get("task_hash"),
                    row.get("run_id"),
                    row.get("tier"),
                    row.get("profile_key"),
                    row.get("spawn_id"),
                    row.get("task_kind"),
                    event_id,
                    ts,
                ),
            )
    except Exception:  # pragma: no cover - best-effort
        log.debug("model_quality: event insert failed", exc_info=True)


def record_findings_score(
    db: Database,
    *,
    model: str | None,
    effort: str | None,
    dimension: str,
    findings_high: int,
    findings_total: int,
    kept_by_synthesis: bool | None,
    sub_dimension: str | None = None,
    task_hash: str | None = None,
    run_id: str | None = None,
    tier: str | None = None,
    profile_key: str | None = None,
    spawn_id: str | None = None,
) -> None:
    """Score one review agent's findings and record it (source='findings').

    No-op when the findings carry no signal (see :func:`findings_to_score`).

    ``kept_by_synthesis=None`` means nothing adjudicated these findings, and no row
    is written at all — see :func:`findings_to_score`. Yield without a verdict is
    not precision evidence, and recording it is how the proxy came to report a
    precision it had never measured. Rows that ARE written still carry
    ``sample_meta.adjudicated`` so a reader can tell "a judge accepted this" from
    "a judge rejected this".
    """
    score = findings_to_score(
        findings_high=findings_high,
        findings_total=findings_total,
        kept_by_synthesis=kept_by_synthesis,
    )
    if score is None:
        return
    _write_event(
        db,
        model=model,
        effort=effort,
        dimension=dimension,
        sub_dimension=sub_dimension,
        score_0_10=score,
        source=SOURCE_FINDINGS,
        sample_meta={
            "findings_high": int(findings_high),
            "findings_total": int(findings_total),
            "kept_by_synthesis": (
                None if kept_by_synthesis is None else bool(kept_by_synthesis)
            ),
            "adjudicated": kept_by_synthesis is not None,
        },
        task_hash=task_hash,
        run_id=run_id,
        tier=tier,
        profile_key=profile_key,
        spawn_id=spawn_id,
    )


def static_recall_to_score(
    *,
    expected_rules: "list[str] | tuple[str, ...]",
    reported_categories: "list[str] | tuple[str, ...]",
    findings_total: int,
) -> tuple[float, dict[str, Any]] | None:
    """Map reviewer output against the static pre-scan set to a 0-10 score.

    Unlike :func:`findings_to_score` this is measured against something objective:
    the high-severity smells :mod:`shared.code_intel` found deterministically in
    the same file. Recall is the share of those the reviewer reported; the
    remainder of the score penalises unexplained extra findings only mildly,
    because a reviewer legitimately finds real defects no static rule covers.

    Returns ``None`` when there is no expectation to measure against (empty
    ``expected_rules``) so a clean file never dilutes the ledger.
    """
    from .code_intel import rule_aliases

    expected = {str(r) for r in expected_rules if r}
    if not expected:
        return None
    # Reviewers report `dimension/category` kebab-case slugs; rule ids are
    # snake_case and often use different words for the same defect. Match through
    # code_intel.rule_aliases, comparing both whole slugs and individual tokens.
    reported_slugs: set[str] = set()
    reported_tokens: set[str] = set()
    for raw in reported_categories:
        if not raw:
            continue
        cat = str(raw).strip().lower()
        reported_slugs.add(cat)
        reported_slugs.add(cat.rsplit("/", 1)[-1])
        for part in re.split(r"[/_\-\s]+", cat):
            if len(part) > 2:
                reported_tokens.add(part)
    hits = 0
    missed: list[str] = []
    for rule in sorted(expected):
        aliases = rule_aliases(rule)
        matched = any(
            alias in reported_slugs
            or any(alias in slug for slug in reported_slugs)
            or alias in reported_tokens
            for alias in aliases
        )
        if matched:
            hits += 1
        else:
            missed.append(rule)
    recall = hits / len(expected)
    extras = max(0, int(findings_total) - hits)
    # Recall carries the score; unmatched extra findings shave at most 2 points.
    noise_penalty = min(2.0, 0.25 * extras)
    score = max(0.0, min(10.0, round(10.0 * recall - noise_penalty, 2)))
    meta = {
        "expected": sorted(expected),
        "matched": hits,
        "missed": missed,
        "recall": round(recall, 3),
        "extra_findings": extras,
    }
    return score, meta


def record_static_recall_score(
    db: Database,
    *,
    model: str | None,
    effort: str | None,
    dimension: str,
    expected_rules: "list[str] | tuple[str, ...]",
    reported_categories: "list[str] | tuple[str, ...]",
    findings_total: int,
    sub_dimension: str | None = None,
    task_hash: str | None = None,
    run_id: str | None = None,
    tier: str | None = None,
    profile_key: str | None = None,
    spawn_id: str | None = None,
) -> None:
    """Score a reviewer against the static pre-scan (source='static_recall').

    No-op when the file carried no high-severity static expectation.
    """
    scored = static_recall_to_score(
        expected_rules=expected_rules,
        reported_categories=reported_categories,
        findings_total=findings_total,
    )
    if scored is None:
        return
    score, meta = scored
    _write_event(
        db,
        model=model,
        effort=effort,
        dimension=dimension,
        sub_dimension=sub_dimension,
        score_0_10=score,
        source=SOURCE_STATIC_RECALL,
        sample_meta=meta,
        task_hash=task_hash,
        run_id=run_id,
        tier=tier,
        profile_key=profile_key,
        spawn_id=spawn_id,
    )


def record_verify_gate_score(
    db: Database,
    *,
    model: str | None,
    effort: str | None,
    score_0_10: float,
    new_failure_count: int = 0,
    preexisting_count: int = 0,
    role: str | None = None,
    task_hash: str | None = None,
    run_id: str | None = None,
    tier: str | None = None,
    profile_key: str | None = None,
    spawn_id: str | None = None,
) -> None:
    """Record a verify-gate outcome (source='verify_gate').

    Objective: the written code either passed lint/types/tests or introduced new
    failures relative to the merge base. ``preexisting_count`` is stored for
    context only and never affects the score. ``role`` (the semantic role of the
    agent that wrote the code — Implementer, Debugger, etc., see ``shared/roles.py``)
    becomes the dimension when known, so non-review work gets a real axis instead
    of one flat ``general`` bucket; falls back to ``general`` when the role is
    unresolved.
    """
    _write_event(
        db,
        model=model,
        effort=effort,
        dimension=role.strip().lower() if role and role.strip() else DIMENSION_GENERAL,
        sub_dimension="verify",
        score_0_10=score_0_10,
        source=SOURCE_VERIFY_GATE,
        sample_meta={
            "new_failures": int(new_failure_count),
            "preexisting_failures": int(preexisting_count),
        },
        task_hash=task_hash,
        run_id=run_id,
        tier=tier,
        profile_key=profile_key,
        spawn_id=spawn_id,
    )


def record_ladder_score(
    db: Database,
    *,
    model: str | None,
    effort: str | None,
    level_label: str,
    passed: bool,
    tier: str | None = None,
    case_id: str | None = None,
    run_id: str | None = None,
    profile_key: str | None = None,
    spawn_id: str | None = None,
    kind: str | None = None,
) -> None:
    """Record one graded ladder verdict (source='ladder').

    Pass/fail only — a benchmark that partially credits broken code is not ground
    truth. ``sub_dimension`` is the level label (``L0``..``L6``) so
    ``build_quality_snapshot`` groups a model's results per difficulty rung.

    ``tier`` and ``case_id`` are written to real columns, not only into
    ``sample_meta``: together with ``run_id`` they are what make one graded verdict
    distinguishable from another. A sweep grades every case at every tier, so
    without them two cases at the same level — or two tiers that resolve to the
    same model — share an ``event_id`` and the second is discarded by
    ``ON CONFLICT DO NOTHING``. ``build_min_passing_tier_map`` credits a tier only
    when it swept *every* case at a level, so a silent drop does not merely lose a
    row, it withholds the level entirely.
    """
    _write_event(
        db,
        model=model,
        effort=effort,
        dimension=DIMENSION_GENERAL,
        sub_dimension=str(level_label or "L?"),
        score_0_10=10.0 if passed else 0.0,
        source=SOURCE_LADDER,
        sample_meta={
            "passed": bool(passed),
            "tier": (tier or None),
            "case_id": (case_id or None),
        },
        # The case is the task being graded, so it belongs in task_hash — which is
        # part of the journal's identity tuple for this kind.
        task_hash=(case_id or None),
        run_id=run_id,
        tier=(tier or None),
        profile_key=profile_key,
        spawn_id=spawn_id,
        # `dimension` stays DIMENSION_GENERAL for backwards compatibility; `kind`
        # is the axis a consumer can actually match on.
        kind=kind,
    )


def _min_passing_tier_grouped(
    db: Database, *, group_column: str, since: str
) -> dict[str, dict[str, str]]:
    """``{model: {group: cheapest_passing_tier}}`` over ladder events.

    *group_column* is the axis to bucket by — ``sub_dimension`` for the difficulty
    level (``L0``..``L6``) or ``kind`` for the task kind. The all-or-nothing rule is
    the point: a tier is credited for a group only when it passed *every* case
    attempted in it, so one lucky pass is never evidence.

    ``tier``/``case_id`` are read from their real columns with a ``sample_meta``
    fallback, so rows written before those columns existed still count.
    """
    if group_column not in ("sub_dimension", "kind"):
        raise ValueError(f"unsupported group column: {group_column!r}")
    since_ts, _ = parse_quality_window(since)
    attempted: dict[tuple[str, str, str], set[str]] = {}
    passed: dict[tuple[str, str, str], set[str]] = {}
    try:
        with db.conn() as conn:
            rows = conn.execute(
                f"SELECT model, {group_column}, sample_meta, score_0_10, tier, task_hash "
                "FROM model_quality_events "
                f"WHERE source = ? AND ts >= ? AND {group_column} IS NOT NULL",
                (SOURCE_LADDER, since_ts),
            ).fetchall()
    except Exception:  # pragma: no cover - best-effort read
        log.debug("model_quality: ladder query failed", exc_info=True)
        return {}
    for model, group, meta_json, score, tier_col, task_hash in rows:
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except (TypeError, ValueError):
            meta = {}
        tier = str(tier_col or meta.get("tier") or "")
        case_id = str(task_hash or meta.get("case_id") or "")
        if not tier or not case_id:
            continue
        key = (str(model), str(group), tier)
        attempted.setdefault(key, set()).add(case_id)
        if float(score or 0.0) >= 10.0:
            passed.setdefault(key, set()).add(case_id)

    out: dict[str, dict[str, str]] = {}
    tier_order = ("low", "medium", "high")
    models = {m for m, _, _ in attempted}
    groups = {g for _, g, _ in attempted}
    for model in sorted(models):
        for group in sorted(groups):
            for tier in tier_order:
                key = (model, group, tier)
                if key not in attempted:
                    continue
                if passed.get(key, set()) == attempted[key]:
                    out.setdefault(model, {})[group] = tier
                    break
    return out


def graded_models_by_tier(
    db: Database, *, since: str = "all"
) -> dict[str, set[str]]:
    """``{tier: {models graded at that tier}}`` from ladder events.

    The ledger already records which model produced each graded verdict, so
    "which model was this tier's evidence collected on" needs no extra storage.
    That is what makes staleness detectable: a tier whose current model is not
    among the graded ones has no ground truth that applies to it any more, and
    `preferred_routing` advice derived from the old model is misleading rather
    than merely out of date.
    """
    since_ts, _ = parse_quality_window(since)
    out: dict[str, set[str]] = {}
    try:
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT tier, model, sample_meta FROM model_quality_events "
                "WHERE source = ? AND ts >= ?",
                (SOURCE_LADDER, since_ts),
            ).fetchall()
    except Exception:  # pragma: no cover - best-effort read
        log.debug("graded_models_by_tier failed", exc_info=True)
        return {}
    for tier_col, model, meta_json in rows:
        tier = str(tier_col or "")
        if not tier:
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except (TypeError, ValueError):
                meta = {}
            tier = str(meta.get("tier") or "")
        if not tier or not model:
            continue
        out.setdefault(tier, set()).add(str(model))
    return out


def build_min_passing_tier_map(
    db: Database, *, since: str = "all"
) -> dict[str, dict[str, str]]:
    """Return ``{model: {level_label: cheapest_passing_tier}}`` from ladder events.

    Difficulty axis: how *hard* a case a model can carry at a given tier.
    """
    return _min_passing_tier_grouped(db, group_column="sub_dimension", since=since)


def build_min_passing_tier_by_kind(
    db: Database, *, since: str = "all"
) -> dict[str, dict[str, str]]:
    """Return ``{model: {kind: cheapest_passing_tier}}`` from ladder events.

    Subject-matter axis, and the one that answers the question the level axis
    cannot: "is this model good at fixing XSS" / "can the cheap tier handle
    boilerplate". Populated only for cases whose ``case.json`` declares a ``kind``.
    """
    return _min_passing_tier_grouped(db, group_column="kind", since=since)


def record_judge_score(
    db: Database,
    *,
    model: str | None,
    effort: str | None,
    score_0_10: float,
    reason: str | None = None,
    dimension: str = DIMENSION_GENERAL,
    sub_dimension: str | None = None,
    role: str | None = None,
    task_hash: str | None = None,
    run_id: str | None = None,
    tier: str | None = None,
    profile_key: str | None = None,
    spawn_id: str | None = None,
) -> None:
    """Record one warm-path judge score (source='judge').

    ``role`` (see ``shared/roles.py``), when the caller has one, overrides
    ``dimension`` the same way ``record_verify_gate_score`` does — a real axis
    for non-review work instead of a flat ``general`` bucket. Omitted by
    callers that don't yet track a role, so existing behavior is unchanged.
    """
    _write_event(
        db,
        model=model,
        effort=effort,
        dimension=role.strip().lower() if role and role.strip() else dimension,
        sub_dimension=sub_dimension,
        score_0_10=score_0_10,
        source=SOURCE_JUDGE,
        sample_meta={"reason": reason} if reason else None,
        task_hash=task_hash,
        run_id=run_id,
        tier=tier,
        profile_key=profile_key,
        spawn_id=spawn_id,
    )


def _escalation_rate_map(db: Database, since_ts: float) -> dict[tuple[str, str | None], float]:
    """Return ``{(from_model, effort): approx_escalation_rate}``.

    Rate = escalations away from the model / (final-model executions + escalations
    away). Approximate — telemetry records the FINAL model of a run while
    ``escalations`` records the model escalated away from, so the denominator is a
    proxy for "times this model was the starting model". Bounded to [0, 1].
    """
    esc: dict[tuple[str, str | None], int] = {}
    execs: dict[tuple[str, str | None], int] = {}
    try:
        with db.conn() as conn:
            for from_model, effort, count in conn.execute(
                "SELECT from_model, effort, COUNT(*) FROM escalations "
                "WHERE ts >= ? AND from_model IS NOT NULL "
                "GROUP BY from_model, effort",
                (since_ts,),
            ).fetchall():
                esc[(str(from_model), effort)] = int(count or 0)
            for model, effort, count in conn.execute(
                "SELECT model, effort, COUNT(*) FROM telemetry "
                "WHERE ts >= ? AND model IS NOT NULL "
                "GROUP BY model, effort",
                (since_ts,),
            ).fetchall():
                execs[(str(model), effort)] = int(count or 0)
    except Exception:  # pragma: no cover - best-effort read
        log.debug("model_quality: escalation-rate query failed", exc_info=True)
        return {}
    out: dict[tuple[str, str | None], float] = {}
    for key, esc_count in esc.items():
        denom = execs.get(key, 0) + esc_count
        out[key] = round(esc_count / denom, 4) if denom else 0.0
    return out


def build_quality_snapshot(
    db: Database,
    *,
    since: str = _DEFAULT_WINDOW,
    config: "TGsConfig | None" = None,
    by_role: bool = False,
) -> dict[str, Any]:
    """Return the aggregated model-quality ledger for operator/MCP/doc surfaces.

    Groups ``model_quality_events`` by ``(model, effort, dimension, sub_dimension)``
    with mean score, sample count, and per-source breakdown, joined with an
    approximate escalation rate per ``(model, effort)``. Empty-safe on a fresh DB.

    When ``by_role=True``, additionally returns a ``by_role`` facet joining with
    ``telemetry.role`` to show per-role quality breakdown.
    """
    since_ts, window_label = parse_quality_window(since)
    rows_out: list[dict[str, Any]] = []
    total_events = 0
    # Distinct scored outputs = top-level rows only. Per-category (sub_dimension)
    # rows are drill-downs of the SAME reviewed output, so counting them here would
    # double-count; they still contribute their own aggregated row below.
    scored_outputs = 0
    try:
        with db.conn() as conn:
            grouped = conn.execute(
                "SELECT model, effort, dimension, sub_dimension, "
                "COUNT(*), AVG(score_0_10), MIN(score_0_10), MAX(score_0_10), "
                "SUM(CASE WHEN source='findings' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN source='judge' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN source IN ('static_recall', 'verify_gate', 'ladder') "
                "THEN 1 ELSE 0 END), "
                "AVG(CASE WHEN source IN ('static_recall', 'verify_gate', 'ladder') "
                "THEN score_0_10 END), "
                # Findings rows nothing adjudicated. They measure yield, not
                # precision: no judge ever saw them, so they cannot score below 7.
                "SUM(CASE WHEN source='findings' "
                "AND json_extract(sample_meta, '$.adjudicated') IS NOT 1 "
                "THEN 1 ELSE 0 END) "
                "FROM model_quality_events WHERE ts >= ? "
                "GROUP BY model, effort, dimension, sub_dimension "
                "ORDER BY model, dimension, sub_dimension",
                (since_ts,),
            ).fetchall()
    except Exception:  # pragma: no cover - best-effort read
        log.debug("model_quality: snapshot query failed", exc_info=True)
        grouped = []

    esc_rates = _escalation_rate_map(db, since_ts)
    for (
        model,
        effort,
        dimension,
        sub_dimension,
        n,
        avg_score,
        min_score,
        max_score,
        findings_n,
        judge_n,
        objective_n,
        objective_avg,
        unadjudicated_n,
    ) in grouped:
        n = int(n or 0)
        total_events += n
        if sub_dimension is None:
            scored_outputs += n
        rows_out.append({
            "model": model,
            "effort": effort,
            "dimension": dimension,
            "sub_dimension": sub_dimension,
            "n": n,
            "avg_score": round(float(avg_score or 0.0), 2),
            "min_score": round(float(min_score or 0.0), 2),
            "max_score": round(float(max_score or 0.0), 2),
            "findings_n": int(findings_n or 0),
            "judge_n": int(judge_n or 0),
            # Ground-truth subset: scored against static analysis, a verify gate,
            # or a graded ladder rather than another model's judgement.
            "objective_n": int(objective_n or 0),
            "objective_avg": (
                round(float(objective_avg), 2) if objective_avg is not None else None
            ),
            # Of findings_n, how many no adjudicator judged. A row where this equals
            # findings_n reports how much a model found, never how much of it was real.
            "unadjudicated_n": int(unadjudicated_n or 0),
            "escalation_rate": esc_rates.get((str(model), effort), 0.0),
        })

    return {
        "window": window_label,
        "since_ts": since_ts,
        "initialized": bool(rows_out),
        "event_count": total_events,
        "scored_outputs": scored_outputs,
        "rows": rows_out,
        "by_role": _build_by_role_facet(db, since_ts) if by_role else None,
        "disclaimer": (
            "score_0_10 blends free review-findings precision (source='findings') "
            "and an opt-out LLM judge (source='judge'); those two are relative "
            "learning signals, not an absolute model benchmark. objective_avg covers "
            "only the ground-truth sources (static_recall, verify_gate, ladder) and "
            "is the column to trust when objective_n is non-zero. unadjudicated_n "
            "counts findings rows no synthesis agent judged — those measure yield, "
            "not precision, and cannot score below 7. escalation_rate is approximate."
        ),
        "cli_hint": f"threnody quality --since {window_label}",
    }


def _build_by_role_facet(db: Database, since_ts: float) -> list[dict[str, Any]]:
    """Per-role quality breakdown via JOIN on telemetry.role.

    Joins model_quality_events with telemetry on (run, model) and time window
    to surface quality by semantic role (Implementer, Reviewer, etc.). Not
    ``task_hash`` — telemetry writes ``task_hash=task_id`` while quality events
    write ``task_hash=pattern_hash``; those are different values for the same
    agent, so that join could never match a row.
    """
    try:
        with db.conn() as conn:
            rows = conn.execute(
                """
                SELECT t.role, m.model, m.effort,
                       COUNT(*), AVG(m.score_0_10),
                       SUM(CASE WHEN m.source IN ('static_recall','verify_gate','ladder')
                                THEN 1 ELSE 0 END),
                       AVG(CASE WHEN m.source IN ('static_recall','verify_gate','ladder')
                                THEN m.score_0_10 END)
                FROM model_quality_events m
                JOIN telemetry t ON t.session_id = m.run_id AND t.model = m.model
                WHERE m.ts >= ? AND t.role IS NOT NULL AND t.role != ''
                GROUP BY t.role, m.model, m.effort
                ORDER BY t.role, m.model
                """,
                (since_ts,),
            ).fetchall()
    except Exception:
        log.debug("model_quality: by_role facet query failed", exc_info=True)
        return []
    return [
        {
            "role": r[0],
            "model": r[1],
            "effort": r[2],
            "n": int(r[3] or 0),
            "avg_score": round(float(r[4] or 0.0), 2),
            "objective_n": int(r[5] or 0),
            "objective_avg": round(float(r[6]), 2) if r[6] is not None else None,
        }
        for r in rows
    ]


__all__ = [
    "SOURCE_FINDINGS",
    "SOURCE_JUDGE",
    "SOURCE_STATIC_RECALL",
    "SOURCE_VERIFY_GATE",
    "SOURCE_LADDER",
    "VALID_SOURCES",
    "OBJECTIVE_SOURCES",
    "MODEL_UNRESOLVED",
    "DIMENSION_GENERAL",
    "parse_quality_window",
    "findings_to_score",
    "static_recall_to_score",
    "record_findings_score",
    "record_static_recall_score",
    "record_verify_gate_score",
    "record_ladder_score",
    "build_min_passing_tier_map",
    "record_judge_score",
    "build_quality_snapshot",
]
