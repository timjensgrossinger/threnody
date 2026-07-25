from __future__ import annotations

"""threnody learning report — surfaces what the router has learned."""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from .adaptive import get_band_stats
from .config import DB_PATH
from .db import Database
from .db_client import open_database

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    HAS_RICH = True
except ImportError:  # pragma: no cover
    HAS_RICH = False

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def build_learning_report(db: Database, *, window_days: int = 7) -> dict[str, Any]:
    """Aggregate all learning signals into a report dict.

    Returns a plain dict suitable for JSON serialisation or rendering.
    window_days=0 means all time.
    """
    cutoff = time.time() - window_days * 86400 if window_days > 0 else 0.0

    with db.conn() as conn:
        # --- patterns ---
        patterns = conn.execute(
            "SELECT occurrence_count, eval_quality FROM subtask_patterns WHERE last_seen >= ?",
            (cutoff,),
        ).fetchall()
        total_patterns = len(patterns)
        if total_patterns != 0:
            avg_recurrence = sum(r[0] for r in patterns) / total_patterns
        else:
            avg_recurrence = 0.0
        quality_vals = [r[1] for r in patterns if r[1] is not None]
        _nq = len(quality_vals)
        if _nq != 0:
            avg_eval_quality = sum(quality_vals) / _nq
        else:
            avg_eval_quality = 0.0

        # --- agents ---
        agents_by_state = dict(
            conn.execute(
                "SELECT COALESCE(promotion_state, 'draft'), COUNT(*) "
                "FROM agent_definitions GROUP BY promotion_state"
            ).fetchall()
        )
        pending_approvals = conn.execute(
            "SELECT COUNT(*) FROM approval_queue WHERE status = 'pending'"
        ).fetchone()[0]

        # --- routing outcomes (within window) ---
        outcome_rows = conn.execute(
            """
            SELECT tier, current_outcome, COUNT(*) as cnt
            FROM routing_outcomes
            WHERE recorded_at >= ?
            GROUP BY tier, current_outcome
            """,
            (cutoff,),
        ).fetchall()
        outcomes_by_tier: dict[str, dict[str, int]] = {}
        total_outcomes = 0
        for tier, outcome, cnt in outcome_rows:
            tier = tier or "unknown"
            outcomes_by_tier.setdefault(tier, {})
            outcomes_by_tier[tier][outcome] = cnt
            total_outcomes += cnt

        # --- rework trend (daily buckets within window) ---
        rework_rows = conn.execute(
            """
            SELECT
                CAST(ts / 86400 AS INTEGER) as day_bucket,
                COUNT(*) as tasks,
                SUM(success) as successes,
                SUM(rework_count) as rework_total
            FROM telemetry
            WHERE ts >= ?
            GROUP BY day_bucket
            ORDER BY day_bucket
            """,
            (cutoff,),
        ).fetchall()
        rework_trend = [
            {
                "day_offset": int(r[0]),
                "tasks": int(r[1]),
                "success_rate": round(r[2] / r[1], 3) if r[1] else 0.0,
                "rework_total": int(r[3] or 0),
            }
            for r in rework_rows
        ]
        overall_rework = (
            sum(r.get("rework_total", 0) for r in rework_trend)
            / max(sum(r.get("tasks", 0) for r in rework_trend), 1)
        )

    # --- adaptive bands ---
    bands = get_band_stats(db)
    band_coverage = len({b.get("band", "") for b in bands})

    return {
        "window_days": window_days,
        "generated_at": time.time(),
        "patterns": {
            "total": total_patterns,
            "avg_recurrence": round(avg_recurrence, 2),
            "avg_eval_quality": round(avg_eval_quality, 3),
        },
        "agents": {
            "by_state": agents_by_state,
            "pending_approvals": pending_approvals,
            "total_active": agents_by_state.get("active", 0),
        },
        "routing_outcomes": {
            "total": total_outcomes,
            "by_tier": outcomes_by_tier,
        },
        "rework": {
            "overall_rate": round(overall_rework, 3),
            "daily": rework_trend,
        },
        "adaptive_bands": {
            "covered": band_coverage,
            "stats": bands,
        },
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_report(report: dict[str, Any], fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps(report, indent=2)
    if fmt == "markdown":
        return _render_markdown(report)
    if HAS_RICH:
        return _render_rich(report)
    return _render_plain(report)


def _window_label(days: int) -> str:
    return f"last {days}d" if days > 0 else "all time"


def _render_plain(report: dict[str, Any]) -> str:
    lines: list[str] = []
    win = _window_label(report.get("window_days", 7))

    p = report.get("patterns", {})
    lines.append(f"=== Threnody Learning Report ({win}) ===\n")
    lines.append(f"PATTERNS EMERGED ({win})")
    lines.append(f"  total:            {p.get('total', 0)}")
    lines.append(f"  avg recurrence:   {p.get('avg_recurrence', 0)}")
    lines.append(f"  avg eval quality: {p.get('avg_eval_quality', 0)}\n")

    a = report.get("agents", {})
    lines.append("AGENTS")
    lines.append(f"  active:           {a.get('total_active', 0)}")
    lines.append(f"  pending approval: {a.get('pending_approvals', 0)}")
    for state, cnt in sorted(a.get("by_state", {}).items()):
        lines.append(f"  {state:20s}: {cnt}")
    lines.append("")

    ro = report.get("routing_outcomes", {})
    lines.append(f"ROUTING OUTCOMES ({win})  total={ro.get('total', 0)}")
    for tier, outcomes in sorted(ro.get("by_tier", {}).items()):
        total = sum(outcomes.values())
        breakdown = "  ".join(f"{k}={v}" for k, v in sorted(outcomes.items()))
        lines.append(f"  {tier:8s}  total={total:4d}  {breakdown}")
    lines.append("")

    rw = report.get("rework", {})
    lines.append(f"REWORK  overall_rate={rw.get('overall_rate', 0):.1%}")
    for day in rw.get("daily", [])[-7:]:
        lines.append(
            f"  day {day.get('day_offset', 0)}  tasks={day.get('tasks', 0):4d}  "
            f"success={day.get('success_rate', 0):.1%}  rework={day.get('rework_total', 0)}"
        )
    lines.append("")

    ab = report.get("adaptive_bands", {})
    lines.append(f"ADAPTIVE BANDS  covered={ab.get('covered', 0)}/10")

    return "\n".join(lines)


def _render_rich(report: dict[str, Any]) -> str:
    import io

    console = Console(file=io.StringIO(), width=90, highlight=False)
    win = _window_label(report.get("window_days", 7))

    # Patterns
    p = report.get("patterns", {})
    t_pat = Table(title=f"Patterns Emerged ({win})", show_header=True)
    t_pat.add_column("Metric")
    t_pat.add_column("Value", justify="right")
    t_pat.add_row("Total", str(p.get("total", 0)))
    t_pat.add_row("Avg recurrence", str(p.get("avg_recurrence", 0)))
    t_pat.add_row("Avg eval quality", str(p.get("avg_eval_quality", 0)))
    console.print(t_pat)

    # Agents
    a = report.get("agents", {})
    t_ag = Table(title="Agents", show_header=True)
    t_ag.add_column("State")
    t_ag.add_column("Count", justify="right")
    for state, cnt in sorted(a.get("by_state", {}).items()):
        t_ag.add_row(state, str(cnt))
    t_ag.add_row("[bold]pending approval[/bold]", str(a.get("pending_approvals", 0)))
    console.print(t_ag)

    # Routing outcomes
    ro = report.get("routing_outcomes", {})
    by_tier = ro.get("by_tier", {})
    all_outcome_keys = sorted({k for outcomes in by_tier.values() for k in outcomes})
    t_ro = Table(title=f"Routing Outcomes ({win})  total={ro.get('total', 0)}", show_header=True)
    t_ro.add_column("Tier")
    for k in all_outcome_keys:
        t_ro.add_column(k, justify="right")
    t_ro.add_column("Total", justify="right")
    for tier, outcomes in sorted(by_tier.items()):
        total = sum(outcomes.values())
        t_ro.add_row(tier, *[str(outcomes.get(k, 0)) for k in all_outcome_keys], str(total))
    console.print(t_ro)

    # Rework trend
    rw = report.get("rework", {})
    t_rw = Table(
        title=f"Rework Trend  overall={rw.get('overall_rate', 0):.1%}",
        show_header=True,
    )
    t_rw.add_column("Day bucket")
    t_rw.add_column("Tasks", justify="right")
    t_rw.add_column("Success", justify="right")
    t_rw.add_column("Rework", justify="right")
    for day in rw.get("daily", [])[-7:]:
        t_rw.add_row(
            str(day.get("day_offset", 0)),
            str(day.get("tasks", 0)),
            f"{day.get('success_rate', 0):.1%}",
            str(day.get("rework_total", 0)),
        )
    console.print(t_rw)

    # Adaptive bands summary
    ab = report.get("adaptive_bands", {})
    console.print(
        Panel(
            f"Adaptive band coverage: [bold]{ab.get('covered', 0)}/10[/bold]",
            title="Adaptive Learning",
        )
    )

    return console.file.getvalue()  # type: ignore[union-attr]


def _render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    win = _window_label(report.get("window_days", 7))
    lines.append(f"# Threnody Learning Report ({win})\n")

    p = report.get("patterns", {})
    lines.append("## Patterns Emerged\n")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Total | {p.get('total', 0)} |")
    lines.append(f"| Avg recurrence | {p.get('avg_recurrence', 0)} |")
    lines.append(f"| Avg eval quality | {p.get('avg_eval_quality', 0)} |")
    lines.append("")

    a = report.get("agents", {})
    lines.append("## Agents\n")
    lines.append("| State | Count |")
    lines.append("|---|---|")
    for state, cnt in sorted(a.get("by_state", {}).items()):
        lines.append(f"| {state} | {cnt} |")
    lines.append(f"| **pending approval** | {a.get('pending_approvals', 0)} |")
    lines.append("")

    ro = report.get("routing_outcomes", {})
    by_tier_md = ro.get("by_tier", {})
    all_outcome_keys = sorted({k for outcomes in by_tier_md.values() for k in outcomes})
    lines.append(f"## Routing Outcomes ({win}) — total {ro.get('total', 0)}\n")
    header = "| Tier | " + " | ".join(all_outcome_keys) + " | Total |"
    sep = "|---|" + "|".join("---" for _ in all_outcome_keys) + "|---|"
    lines.append(header)
    lines.append(sep)
    for tier, outcomes in sorted(by_tier_md.items()):
        total = sum(outcomes.values())
        row = f"| {tier} | " + " | ".join(str(outcomes.get(k, 0)) for k in all_outcome_keys) + f" | {total} |"
        lines.append(row)
    lines.append("")

    rw = report.get("rework", {})
    lines.append(f"## Rework Trend — overall {rw.get('overall_rate', 0):.1%}\n")
    lines.append("| Day | Tasks | Success | Rework |")
    lines.append("|---|---|---|---|")
    for day in rw.get("daily", [])[-7:]:
        lines.append(
            f"| {day.get('day_offset', 0)} | {day.get('tasks', 0)} | {day.get('success_rate', 0):.1%} | {day.get('rework_total', 0)} |"
        )
    lines.append("")

    ab = report.get("adaptive_bands", {})
    lines.append(f"## Adaptive Bands\n\nCoverage: **{ab.get('covered', 0)}/10**\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Threnody learning report")
    parser.add_argument(
        "--window-days",
        type=int,
        default=7,
        metavar="N",
        help="Look-back window in days (0 = all time, default 7)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "markdown"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help="Path to cache.db",
    )
    args = parser.parse_args(argv)

    db = open_database(args.db)
    try:
        report = build_learning_report(db, window_days=args.window_days)
        print(render_report(report, fmt=args.format))
    except Exception as exc:
        log.error("learning report failed: %s", exc, exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
