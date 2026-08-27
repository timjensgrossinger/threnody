"""Operator-facing model-quality ledger report — CLI + MODEL_QUALITY.md writer.

Backs ``threnody quality`` and ``python3 -m shared.model_quality_report``. Renders
the granular ``(model x effort x dimension x sub_dimension) -> score`` ledger built
by :func:`shared.model_quality.build_quality_snapshot`.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from shared.config import TGsConfig
from shared.db import Database
from shared.model_quality import (
    build_min_passing_tier_by_kind,
    build_min_passing_tier_map,
    build_quality_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOC_PATH = REPO_ROOT / "docs" / "MODEL_QUALITY.md"


def _fmt_effort(effort: str | None) -> str:
    return effort or "—"


def _fmt_dim(dimension: str, sub_dimension: str | None) -> str:
    return f"{dimension}/{sub_dimension}" if sub_dimension else dimension


def render_quality_markdown(snapshot: dict[str, Any]) -> str:
    """Render the ledger snapshot as an operator markdown document."""
    rows = snapshot.get("rows") or []
    header = [
        "# Model quality ledger (operator report)",
        "",
        "Per-`(model × effort × dimension)` quality scores (0–10) from "
        "`python3 -m shared.model_quality_report`.",
        "",
        f"- **Generated:** {date.today().isoformat()}",
        f"- **Window:** {snapshot.get('window', 'n/a')}",
        f"- **Scored outputs:** {snapshot.get('scored_outputs', snapshot.get('event_count', 0))}",
        f"- **Ledger events:** {snapshot.get('event_count', 0)} _(incl. per-category drill-downs)_",
        "",
    ]
    if not rows:
        header.append("_No quality events recorded in this window yet._")
        header.append("")
        header.append(
            "Review and task runs populate the free signals automatically. To seed the "
            "ground-truth signal (and the minimum-passing-tier table), run the graded "
            "ladder — it spends real tokens."
        )
        header.append("")
        header.append("## How to refresh")
        header.append("")
        header.append("```bash")
        header.append("threnody quality --since 7d")
        header.append("threnody ladder run --tier low,medium,high")
        header.append("python3 -m shared.model_quality_report --write-docs")
        header.append("```")
        return "\n".join(header) + "\n"

    table = [
        "| Model | Effort | Dimension | Score | n | Objective | Obj.score | Findings | "
        "Unadj. | Judge | Esc.rate |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        table.append(
            "| {model} | {effort} | {dim} | {score:.2f} | {n} | {on} | {oscore} | "
            "{fn} | {un} | {jn} | {esc:.2f} |".format(
                model=row.get("model", "?"),
                effort=_fmt_effort(row.get("effort")),
                dim=_fmt_dim(str(row.get("dimension", "?")), row.get("sub_dimension")),
                score=float(row.get("avg_score") or 0.0),
                n=int(row.get("n") or 0),
                on=int(row.get("objective_n") or 0),
                oscore=(
                    f"{float(row['objective_avg']):.2f}"
                    if row.get("objective_avg") is not None
                    else "—"
                ),
                fn=int(row.get("findings_n") or 0),
                un=int(row.get("unadjudicated_n") or 0),
                jn=int(row.get("judge_n") or 0),
                esc=float(row.get("escalation_rate") or 0.0),
            )
        )
    footer = [
        "",
        f"> {snapshot.get('disclaimer', '')}",
        "",
    ]
    footer.extend(render_min_passing_tier_section(snapshot.get("min_passing_tier") or {}))
    footer.extend(
        render_competence_by_kind_section(snapshot.get("competence_by_kind") or {})
    )
    footer.extend(
        render_staleness_section(
            list(snapshot.get("stale_tiers") or []),
            dict(snapshot.get("tier_models") or {}),
        )
    )
    footer.extend([
        "## How to refresh",
        "",
        "```bash",
        "threnody quality --since 7d",
        "threnody ladder run --tier low,medium,high   # refresh the graded ground truth",
        "python3 -m shared.model_quality_report --write-docs",
        "```",
    ])
    return "\n".join(header + table + footer) + "\n"


def _render_min_tier_table(
    min_map: dict[str, dict[str, str]], *, title: str, blurb: str
) -> list[str]:
    """Shared renderer for a ``{model: {group: tier}}`` competence table."""
    if not min_map:
        return [
            f"## {title}",
            "",
            "_No graded ladder results yet — run `threnody ladder run` to populate "
            "the ground-truth signal._",
            "",
        ]
    groups = sorted({g for per_model in min_map.values() for g in per_model})
    lines = [
        f"## {title}",
        "",
        blurb,
        "",
        "| Model | " + " | ".join(groups) + " |",
        "|---" * (len(groups) + 1) + "|",
    ]
    for model in sorted(min_map):
        cells = [min_map[model].get(g, "—") for g in groups]
        lines.append(f"| {model} | " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def render_min_passing_tier_section(min_map: dict[str, dict[str, str]]) -> list[str]:
    """Render the graded ladder's minimum-passing-tier table (difficulty axis).

    For each model, the cheapest tier observed to pass every case at a difficulty
    level. Empty until `threnody ladder run` has been executed.
    """
    return _render_min_tier_table(
        min_map,
        title="Minimum passing tier by difficulty (graded ladder)",
        blurb=(
            "Cheapest tier observed to pass **every** case at each level. Derived from "
            "graded outcomes, not hand-maintained — use it to inform `preferred_routing`. "
            "This is the *how hard* axis; see the next table for *what about*."
        ),
    )


def render_competence_by_kind_section(kind_map: dict[str, dict[str, str]]) -> list[str]:
    """Render per-task-kind competence — the "good at what" table.

    The difficulty axis cannot answer "is this model good at fixing XSS" or "can
    the cheap tier handle boilerplate", because a level says how hard a case is and
    not what it is about. This one keys on each case's declared ``kind``.
    """
    return _render_min_tier_table(
        kind_map,
        title="Competence by task kind (graded ladder)",
        blurb=(
            "Cheapest tier observed to pass **every** graded case of each task kind. "
            "This is the direct answer to \"which model is good at what\": read a row as "
            "\"this model handles this kind of work from this tier upward\". A dash means "
            "no tier swept every case of that kind — not that the model failed."
        ),
    )


def _attach_staleness(snapshot: dict[str, Any], db: Database) -> None:
    """Add ``stale_tiers``/``tier_models`` to *snapshot*, best-effort.

    Read-only and non-fatal: a report must still render when provider discovery is
    unavailable (headless, no CLIs installed), so a failure leaves the keys absent
    and the staleness section simply does not appear.
    """
    try:
        from shared.ladder import TIERS, _current_tier_models, stale_tiers

        mapping = _current_tier_models(TIERS)
        if not mapping:
            return
        snapshot["tier_models"] = mapping
        snapshot["stale_tiers"] = stale_tiers(db, mapping)
    except Exception:
        pass


def render_staleness_section(stale: list[str], mapping: dict[str, str]) -> list[str]:
    """Warn when a tier's current model is not the one its results were graded on.

    Without this the two competence tables read as current when they may describe a
    model the tier no longer resolves to — which is worse than having no data,
    because it looks authoritative.
    """
    if not stale:
        return []
    detail = ", ".join(f"`{t}` → `{mapping.get(t, '?')}`" for t in stale)
    return [
        "## ⚠️ Stale graded evidence",
        "",
        f"These tiers now resolve to a model that has **no graded results of its own**: {detail}.",
        "",
        "The competence tables above still describe whichever model the tier used to "
        "resolve to, so treat those rows as historical. Re-grade just the affected "
        "tiers with:",
        "",
        "```bash",
        "threnody ladder run --stale",
        "```",
        "",
    ]


def _open_db(db_path: Path | None, config: TGsConfig | None = None) -> Database:
    # Route through open_database so, when the single-writer daemon owns the DB,
    # this read goes over the socket instead of opening a competing second-process
    # connection (which would re-add the multi-process -shm mmap the daemon avoids).
    from shared.db_client import open_database

    return open_database(db_path, config=config)


def write_quality_doc(
    path: Path | None = None,
    *,
    since: str = "7d",
    db_path: Path | None = None,
    config: TGsConfig | None = None,
) -> dict[str, Any]:
    db = _open_db(db_path, config)
    snapshot = build_quality_snapshot(db, since=since, config=config)
    snapshot["min_passing_tier"] = build_min_passing_tier_map(db, since=since)
    snapshot["competence_by_kind"] = build_min_passing_tier_by_kind(db, since=since)
    _attach_staleness(snapshot, db)
    target = path or DEFAULT_DOC_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_quality_markdown(snapshot), encoding="utf-8")
    snapshot["written_to"] = str(target)
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Threnody model quality ledger report")
    parser.add_argument("--since", default="7d", help="Window: 7d | 24h | all (default 7d)")
    parser.add_argument("--db", type=Path, default=None, help="Path to cache.db (default: installed)")
    parser.add_argument("--json", action="store_true", help="Print JSON snapshot to stdout")
    parser.add_argument("--write-docs", action="store_true", help=f"Write markdown to {DEFAULT_DOC_PATH}")
    parser.add_argument("--output", type=Path, default=DEFAULT_DOC_PATH, help="Output path for --write-docs")
    args = parser.parse_args(argv)

    try:
        config = TGsConfig.from_yaml()
    except Exception:
        config = None

    if args.write_docs:
        snapshot = write_quality_doc(
            args.output, since=args.since, db_path=args.db, config=config
        )
        if args.json:
            print(json.dumps(snapshot, indent=2, sort_keys=True))
        else:
            print(f"wrote {snapshot['written_to']}")
        return 0

    db = _open_db(args.db, config)
    snapshot = build_quality_snapshot(db, since=args.since, config=config)
    snapshot["min_passing_tier"] = build_min_passing_tier_map(db, since=args.since)
    snapshot["competence_by_kind"] = build_min_passing_tier_by_kind(db, since=args.since)
    _attach_staleness(snapshot, db)
    if args.json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    else:
        print(render_quality_markdown(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
