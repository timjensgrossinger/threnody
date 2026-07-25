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
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shared.config import TGsConfig
    from shared.db import Database

log = logging.getLogger(__name__)

SOURCE_FINDINGS = "findings"
SOURCE_JUDGE = "judge"
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
    kept_by_synthesis: bool,
) -> float | None:
    """Map review findings to a 0-10 quality score (precision proxy).

    Returns ``None`` when there is no signal (no findings at all) so the ledger is
    not diluted by "clean" reviews — absence of findings is ambiguous (thorough
    model on clean code, or a model that simply found nothing). The score rewards
    findings the synthesis KEPT (true positives) and penalises findings it dropped
    (noise / false positives). Heuristic and intentionally coarse — tunable.

    Validity caveat: because clean reviews earn no signal and any dropped finding
    scores low (even a correct high-severity one synthesis chose to drop), this
    signal skews toward models that surface *kept* findings on dirty code. Treat it
    as a RELATIVE learning signal, not an absolute quality benchmark (see the
    snapshot ``disclaimer``).
    """
    if findings_total <= 0:
        return None
    if not kept_by_synthesis:
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
) -> None:
    """Insert one ledger event. Best-effort — never raises into caller paths."""
    if source not in (SOURCE_FINDINGS, SOURCE_JUDGE):
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
    try:
        with db.conn() as conn:
            conn.execute(
                "INSERT INTO model_quality_events "
                "(model, effort, dimension, sub_dimension, score_0_10, source, "
                "sample_meta, task_hash, run_id, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _normalize_model(model),
                    (effort or None),
                    str(dimension or DIMENSION_GENERAL),
                    (sub_dimension or None),
                    score,
                    source,
                    meta_json,
                    (task_hash or None),
                    (run_id or None),
                    time.time(),
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
    kept_by_synthesis: bool,
    sub_dimension: str | None = None,
    task_hash: str | None = None,
    run_id: str | None = None,
) -> None:
    """Score one review agent's findings and record it (source='findings').

    No-op when the findings carry no signal (see :func:`findings_to_score`).
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
            "kept_by_synthesis": bool(kept_by_synthesis),
        },
        task_hash=task_hash,
        run_id=run_id,
    )


def record_judge_score(
    db: Database,
    *,
    model: str | None,
    effort: str | None,
    score_0_10: float,
    reason: str | None = None,
    dimension: str = DIMENSION_GENERAL,
    sub_dimension: str | None = None,
    task_hash: str | None = None,
    run_id: str | None = None,
) -> None:
    """Record one warm-path judge score (source='judge')."""
    _write_event(
        db,
        model=model,
        effort=effort,
        dimension=dimension,
        sub_dimension=sub_dimension,
        score_0_10=score_0_10,
        source=SOURCE_JUDGE,
        sample_meta={"reason": reason} if reason else None,
        task_hash=task_hash,
        run_id=run_id,
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
) -> dict[str, Any]:
    """Return the aggregated model-quality ledger for operator/MCP/doc surfaces.

    Groups ``model_quality_events`` by ``(model, effort, dimension, sub_dimension)``
    with mean score, sample count, and per-source breakdown, joined with an
    approximate escalation rate per ``(model, effort)``. Empty-safe on a fresh DB.
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
                "SUM(CASE WHEN source='judge' THEN 1 ELSE 0 END) "
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
            "escalation_rate": esc_rates.get((str(model), effort), 0.0),
        })

    return {
        "window": window_label,
        "since_ts": since_ts,
        "initialized": bool(rows_out),
        "event_count": total_events,
        "scored_outputs": scored_outputs,
        "rows": rows_out,
        "disclaimer": (
            "score_0_10 blends free review-findings precision (source='findings') "
            "and an opt-out LLM judge (source='judge'); it is a relative learning "
            "signal, not an absolute model benchmark. escalation_rate is approximate."
        ),
        "cli_hint": f"threnody quality --since {window_label}",
    }


__all__ = [
    "SOURCE_FINDINGS",
    "SOURCE_JUDGE",
    "MODEL_UNRESOLVED",
    "DIMENSION_GENERAL",
    "parse_quality_window",
    "findings_to_score",
    "record_findings_score",
    "record_judge_score",
    "build_quality_snapshot",
]
