from __future__ import annotations

"""Tests for the granular model-quality ledger (shared/model_quality.py),
findings integration in host_learning, and the warm-path judge in eval.py."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.config import TGsConfig
from shared.db import Database
from shared import model_quality as mq


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "mq.db")


# ---------------------------------------------------------------------------
# findings -> 0-10 heuristic
# ---------------------------------------------------------------------------

def test_findings_to_score_precision_proxy() -> None:
    # kept + high severity -> top score
    assert mq.findings_to_score(findings_high=2, findings_total=3, kept_by_synthesis=True) == 10.0
    # kept + only low severity -> mid-high
    assert mq.findings_to_score(findings_high=0, findings_total=2, kept_by_synthesis=True) == 7.0
    # dropped by synthesis -> noise / low precision
    assert mq.findings_to_score(findings_high=3, findings_total=3, kept_by_synthesis=False) == 3.0
    # no findings -> no signal (None, so the ledger is not diluted)
    assert mq.findings_to_score(findings_high=0, findings_total=0, kept_by_synthesis=True) is None


# ---------------------------------------------------------------------------
# writers + snapshot
# ---------------------------------------------------------------------------

def test_record_findings_score_skips_no_signal(db: Database) -> None:
    mq.record_findings_score(
        db, model="m", effort=None, dimension="security",
        findings_high=0, findings_total=0, kept_by_synthesis=True,
    )
    assert mq.build_quality_snapshot(db, since="all")["event_count"] == 0


def test_record_and_aggregate(db: Database) -> None:
    mq.record_findings_score(
        db, model="claude-opus", effort="high", dimension="security",
        sub_dimension="sql-injection",
        findings_high=2, findings_total=2, kept_by_synthesis=True,
    )
    mq.record_findings_score(
        db, model="claude-opus", effort="high", dimension="security",
        sub_dimension="sql-injection",
        findings_high=0, findings_total=1, kept_by_synthesis=True,
    )  # 7.0 -> avg with 10.0 = 8.5
    mq.record_judge_score(db, model="claude-sonnet", effort="medium", score_0_10=8.0)

    snap = mq.build_quality_snapshot(db, since="all")
    assert snap["initialized"] is True
    assert snap["event_count"] == 3
    rows = {(r["model"], r["dimension"], r["sub_dimension"]): r for r in snap["rows"]}
    opus = rows[("claude-opus", "security", "sql-injection")]
    assert opus["n"] == 2
    assert opus["avg_score"] == 8.5
    assert opus["findings_n"] == 2 and opus["judge_n"] == 0
    sonnet = rows[("claude-sonnet", "general", None)]
    assert sonnet["judge_n"] == 1 and sonnet["avg_score"] == 8.0


def test_scored_outputs_excludes_subdimension_drilldowns(db: Database) -> None:
    # One top-level dimension score + one per-category drill-down describe the SAME
    # reviewed output. event_count counts both ledger rows; scored_outputs must not
    # double-count the drill-down (F7).
    mq.record_findings_score(
        db, model="opus", effort="high", dimension="security",
        findings_high=1, findings_total=1, kept_by_synthesis=True,
    )
    mq.record_findings_score(
        db, model="opus", effort="high", dimension="security",
        sub_dimension="sql-injection",
        findings_high=1, findings_total=1, kept_by_synthesis=True,
    )
    snap = mq.build_quality_snapshot(db, since="all")
    assert snap["event_count"] == 2       # raw ledger rows (incl. drill-down)
    assert snap["scored_outputs"] == 1    # distinct scored outputs (top-level only)


def test_score_clamped_to_10(db: Database) -> None:
    mq.record_judge_score(db, model="m", effort=None, score_0_10=99.0)
    assert mq.build_quality_snapshot(db, since="all")["rows"][0]["avg_score"] == 10.0


def test_unresolved_model_bucketed(db: Database) -> None:
    mq.record_findings_score(
        db, model=None, effort=None, dimension="logic",
        findings_high=1, findings_total=1, kept_by_synthesis=True,
    )
    row = mq.build_quality_snapshot(db, since="all")["rows"][0]
    assert row["model"] == mq.MODEL_UNRESOLVED


def test_empty_snapshot_is_clean(db: Database) -> None:
    snap = mq.build_quality_snapshot(db, since="24h")
    assert snap["initialized"] is False
    assert snap["rows"] == []
    assert snap["event_count"] == 0


def test_escalation_rate_join(db: Database) -> None:
    mq.record_findings_score(
        db, model="claude-opus", effort="high", dimension="security",
        findings_high=1, findings_total=1, kept_by_synthesis=True,
    )
    # 1 escalation away from opus/high, 0 final-model executions -> rate 1.0
    db.log_escalation(
        task_hash="t", agent_id=1, from_tier="high", to_tier="high",
        token_count=9, ceiling=5, from_model="claude-opus", to_model="claude-opus",
        effort="high", reason="token_ceiling",
    )
    row = mq.build_quality_snapshot(db, since="all")["rows"][0]
    assert row["escalation_rate"] == 1.0


def test_unknown_source_rejected(db: Database) -> None:
    mq._write_event(
        db, model="m", effort=None, dimension="x", sub_dimension=None,
        score_0_10=5.0, source="bogus",
    )
    assert mq.build_quality_snapshot(db, since="all")["event_count"] == 0


# ---------------------------------------------------------------------------
# host_learning findings integration (sub-dimension categories + gating)
# ---------------------------------------------------------------------------

def test_build_review_outcome_carries_model_and_categories() -> None:
    from shared.host_learning import _build_review_outcome

    spec = {"subagent_type": "review-security", "target_file": "a.py",
            "model": "claude-opus", "effort": "high"}
    result = {"review_meta": {
        "findings_total": 3, "findings_high": 2, "kept_by_synthesis": True,
        "categories": {"sql-injection": {"findings_total": 2, "findings_high": 2, "kept": True}},
    }}
    outcome = _build_review_outcome(spec, result, "high")
    assert outcome is not None
    assert outcome["model"] == "claude-opus"
    assert outcome["effort"] == "high"
    assert "sql-injection" in outcome["categories"]


def test_record_review_outcome_writes_ledger_when_enabled(db: Database) -> None:
    from shared.host_learning import _record_review_outcome

    cfg = TGsConfig.defaults()
    outcome = {
        "target_file": "auth.py", "dimension": "security", "tier": "high",
        "model": "claude-opus", "effort": "high",
        "findings_total": 2, "findings_high": 2, "kept_by_synthesis": True,
        "categories": {"sql-injection": {"findings_total": 2, "findings_high": 2, "kept": True}},
        "run_id": "r1", "task_hash": "h1",
    }
    _record_review_outcome(db, outcome, cfg)
    rows = mq.build_quality_snapshot(db, since="all")["rows"]
    dims = {r["sub_dimension"] for r in rows}
    assert None in dims  # top-level security
    assert "sql-injection" in dims


def test_record_review_outcome_gated_off(db: Database) -> None:
    from shared.host_learning import _record_review_outcome

    cfg = TGsConfig.defaults()
    cfg.model_quality.findings_enabled = False
    outcome = {
        "target_file": "auth.py", "dimension": "security", "tier": "high",
        "model": "claude-opus", "effort": "high",
        "findings_total": 2, "findings_high": 2, "kept_by_synthesis": True,
        "categories": {}, "run_id": "r1", "task_hash": "h1",
    }
    _record_review_outcome(db, outcome, cfg)
    assert mq.build_quality_snapshot(db, since="all")["event_count"] == 0


# ---------------------------------------------------------------------------
# warm-path judge (eval.py)
# ---------------------------------------------------------------------------

def test_judge_parse() -> None:
    from shared.eval import _parse_judge_score

    assert _parse_judge_score('{"score": 9, "reason": "good"}') == (9.0, "good")
    assert _parse_judge_score('noise\n{"score": 20, "reason": "x"}')[0] == 10.0  # clamp
    assert _parse_judge_score("no json") is None
    assert _parse_judge_score(None) is None


def test_judge_one_writes_ledger(db: Database) -> None:
    from shared.eval import BackgroundEvaluator

    cfg = TGsConfig.defaults()
    seen = {}

    def fake_cli(prompt: str, model: str, timeout: int) -> str:
        seen["model"] = model
        return '{"score": 7.5, "reason": "ok"}'

    ev = BackgroundEvaluator(db=db, config=cfg, cli_call=fake_cli)
    score = ev._judge_one(output="def f(): return 1", model="claude-sonnet", effort="medium")
    assert score == 7.5
    assert seen["model"] == cfg.model_quality.judge_model  # judge ran on judge model
    row = mq.build_quality_snapshot(db, since="all")["rows"][0]
    assert row["model"] == "claude-sonnet"  # ledger attributes the SCORED model
    assert row["judge_n"] == 1


def test_spawn_judge_respects_opt_out(db: Database) -> None:
    from shared.eval import BackgroundEvaluator

    cfg = TGsConfig.defaults()
    cfg.model_quality.judge_enabled = False
    ev = BackgroundEvaluator(db=db, config=cfg, cli_call=lambda *a: '{"score": 5}')
    assert ev.spawn_judge(output="x", model="m", effort=None) is None


def test_spawn_judge_no_backend_is_noop(db: Database) -> None:
    from shared.eval import BackgroundEvaluator

    cfg = TGsConfig.defaults()
    ev = BackgroundEvaluator(db=db, config=cfg, cli_call=None)
    assert ev.spawn_judge(output="x", model="m", effort=None) is None
