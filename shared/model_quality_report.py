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
from shared.model_quality import build_min_passing_tier_map, build_quality_snapshot

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


def render_min_passing_tier_section(min_map: dict[str, dict[str, str]]) -> list[str]:
    """Render the graded ladder's minimum-passing-tier table.

    This is the auto-detected "which model is good at what": for each model, the
    cheapest tier observed to pass every case at a difficulty level. Empty until
    `threnody ladder run` has been executed.
    """
    if not min_map:
        return [
            "## Minimum passing tier (graded ladder)",
            "",
            "_No graded ladder results yet — run `threnody ladder run` to populate "
            "the ground-truth signal._",
            "",
        ]
    levels = sorted({lvl for per_model in min_map.values() for lvl in per_model})
    lines = [
        "## Minimum passing tier (graded ladder)",
        "",
        "Cheapest tier observed to pass **every** case at each level. Derived from "
        "graded outcomes, not hand-maintained — use it to inform `preferred_routing`.",
        "",
        "| Model | " + " | ".join(levels) + " |",
        "|---" * (len(levels) + 1) + "|",
    ]
    for model in sorted(min_map):
        cells = [min_map[model].get(level, "—") for level in levels]
        lines.append(f"| {model} | " + " | ".join(cells) + " |")
    lines.append("")
    return lines


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
    if args.json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    else:
        print(render_quality_markdown(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
