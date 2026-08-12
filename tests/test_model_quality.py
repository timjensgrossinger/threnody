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
    # unadjudicated (None) is NOT a rejection: nothing judged these findings, so they
    # score as yield. Only an explicit False is evidence of noise.
    assert mq.findings_to_score(findings_high=1, findings_total=1, kept_by_synthesis=None) == 10.0
    assert mq.findings_to_score(findings_high=0, findings_total=1, kept_by_synthesis=None) == 7.0


def test_unadjudicated_rows_are_tagged(db: Database) -> None:
    """Without this flag the ledger cannot tell "a judge accepted it" from "no judge
    ran", which is how the proxy came to report a precision it never measured."""
    import json

    for kept in (None, True, False):
        mq.record_findings_score(
            db, model="m", effort=None, dimension="security",
            sub_dimension=f"security/{kept}",
            findings_high=1, findings_total=1, kept_by_synthesis=kept,
        )
    with db.conn() as conn:
        rows = conn.execute(
            "SELECT sub_dimension, score_0_10, sample_meta FROM model_quality_events"
        ).fetchall()
    by_sub = {sub: (score, json.loads(meta)) for sub, score, meta in rows}
    assert by_sub["security/None"][1]["adjudicated"] is False
    assert by_sub["security/True"][1]["adjudicated"] is True
    assert by_sub["security/False"][1]["adjudicated"] is True
    # The noise branch is now reachable — it never fired before adjudication existed.
    assert by_sub["security/False"][0] == 3.0


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


# ---------------------------------------------------------------------------
# static_recall — graded against the deterministic code_intel pre-scan
# ---------------------------------------------------------------------------

class TestStaticRecall:
    def test_no_expectation_yields_no_signal(self):
        from shared.model_quality import static_recall_to_score

        assert static_recall_to_score(
            expected_rules=[], reported_categories=["security/xss"], findings_total=1
        ) is None

    def test_full_recall_scores_ten(self):
        from shared.model_quality import static_recall_to_score

        score, meta = static_recall_to_score(
            expected_rules=["sql_interpolation"],
            reported_categories=["security/sql-injection"],
            findings_total=1,
        )
        assert score == 10.0
        assert meta["matched"] == 1
        assert meta["missed"] == []
        assert meta["recall"] == 1.0

    def test_missed_expectation_scores_zero(self):
        from shared.model_quality import static_recall_to_score

        score, meta = static_recall_to_score(
            expected_rules=["eval_exec"], reported_categories=[], findings_total=0
        )
        assert score == 0.0
        assert meta["missed"] == ["eval_exec"]

    def test_partial_recall_is_proportional(self):
        from shared.model_quality import static_recall_to_score

        score, meta = static_recall_to_score(
            expected_rules=["eval_exec", "os_system"],
            reported_categories=["security/eval-injection"],
            findings_total=1,
        )
        assert meta["recall"] == 0.5
        assert score == 5.0

    def test_extra_findings_penalty_is_bounded(self):
        from shared.model_quality import static_recall_to_score

        score, meta = static_recall_to_score(
            expected_rules=["eval_exec"],
            reported_categories=["security/eval-injection"],
            findings_total=40,
        )
        assert meta["extra_findings"] == 39
        # Recall stays perfect; noise shaves at most 2 points, never inverts it.
        assert score == 8.0

    def test_score_never_negative(self):
        from shared.model_quality import static_recall_to_score

        score, _ = static_recall_to_score(
            expected_rules=["eval_exec"], reported_categories=[], findings_total=30
        )
        assert score == 0.0

    def test_record_writes_event_with_new_source(self, tmp_path):
        from shared.db import Database
        from shared.model_quality import SOURCE_STATIC_RECALL, record_static_recall_score

        db = Database(db_path=tmp_path / "cache.db")
        record_static_recall_score(
            db,
            model="haiku",
            effort="low",
            dimension="security",
            expected_rules=["os_system"],
            reported_categories=["security/command-injection"],
            findings_total=1,
            run_id="run-1",
        )
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT model, dimension, source, score_0_10 FROM model_quality_events"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][2] == SOURCE_STATIC_RECALL
        assert rows[0][0] == "haiku"

    def test_record_is_noop_without_expectation(self, tmp_path):
        from shared.db import Database
        from shared.model_quality import record_static_recall_score

        db = Database(db_path=tmp_path / "cache.db")
        record_static_recall_score(
            db,
            model="haiku",
            effort=None,
            dimension="logic",
            expected_rules=[],
            reported_categories=["logic/off-by-one"],
            findings_total=1,
        )
        with db.conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM model_quality_events").fetchone()[0]
        assert n == 0

    def test_snapshot_separates_objective_sources(self, tmp_path):
        from shared.db import Database
        from shared.model_quality import (
            build_quality_snapshot,
            record_judge_score,
            record_static_recall_score,
        )

        db = Database(db_path=tmp_path / "cache.db")
        record_static_recall_score(
            db,
            model="haiku",
            effort=None,
            dimension="security",
            expected_rules=["os_system"],
            reported_categories=["security/command-injection"],
            findings_total=1,
        )
        record_judge_score(db, model="haiku", effort=None, score_0_10=4.0)
        snap = build_quality_snapshot(db, since="all")
        by_dim = {r["dimension"]: r for r in snap["rows"]}
        assert by_dim["security"]["objective_n"] == 1
        assert by_dim["security"]["objective_avg"] == 10.0
        # The judge row is a proxy source and must not enter the objective column.
        assert by_dim["general"]["objective_n"] == 0
        assert by_dim["general"]["objective_avg"] is None


# ---------------------------------------------------------------------------
# role-as-dimension for non-review sources (write-path coverage)
# ---------------------------------------------------------------------------

class TestRoleAsDimension:
    def test_verify_gate_role_becomes_dimension(self, db: Database) -> None:
        mq.record_verify_gate_score(
            db, model="opus", effort="high", score_0_10=10.0, role="Implementer",
        )
        with db.conn() as conn:
            row = conn.execute(
                "SELECT dimension, sub_dimension, source FROM model_quality_events"
            ).fetchone()
        assert row == ("implementer", "verify", mq.SOURCE_VERIFY_GATE)

    def test_verify_gate_no_role_falls_back_to_general(self, db: Database) -> None:
        mq.record_verify_gate_score(db, model="opus", effort="high", score_0_10=10.0)
        with db.conn() as conn:
            dimension = conn.execute(
                "SELECT dimension FROM model_quality_events"
            ).fetchone()[0]
        assert dimension == mq.DIMENSION_GENERAL

    def test_verify_gate_blank_role_falls_back_to_general(self, db: Database) -> None:
        mq.record_verify_gate_score(db, model="opus", effort="high", score_0_10=10.0, role="   ")
        with db.conn() as conn:
            dimension = conn.execute(
                "SELECT dimension FROM model_quality_events"
            ).fetchone()[0]
        assert dimension == mq.DIMENSION_GENERAL

    def test_judge_role_overrides_dimension(self, db: Database) -> None:
        mq.record_judge_score(
            db, model="sonnet", effort="medium", score_0_10=7.0,
            dimension="general", role="Debugger",
        )
        with db.conn() as conn:
            dimension = conn.execute(
                "SELECT dimension FROM model_quality_events"
            ).fetchone()[0]
        assert dimension == "debugger"

    def test_judge_no_role_keeps_explicit_dimension(self, db: Database) -> None:
        mq.record_judge_score(
            db, model="sonnet", effort="medium", score_0_10=7.0, dimension="security",
        )
        with db.conn() as conn:
            dimension = conn.execute(
                "SELECT dimension FROM model_quality_events"
            ).fetchone()[0]
        assert dimension == "security"


# ---------------------------------------------------------------------------
# by-role facet: join must actually match (was task_hash=task_hash, always empty)
# ---------------------------------------------------------------------------

class TestByRoleFacet:
    def test_by_role_facet_matches_on_run_and_model(self, db: Database) -> None:
        mq.record_verify_gate_score(
            db, model="opus", effort="high", score_0_10=9.0, role="Implementer",
            run_id="run-xyz",
        )
        db.log_agent_result(
            session_id="run-xyz",
            task_hash="some-task-id",
            agent_id=1,
            tier="high",
            model="opus",
            role="Implementer",
        )
        snap = mq.build_quality_snapshot(db, since="all", by_role=True)
        by_role = {(r["role"], r["model"]): r for r in snap["by_role"]}
        assert ("Implementer", "opus") in by_role
        assert by_role[("Implementer", "opus")]["n"] == 1

    def test_by_role_facet_empty_when_telemetry_role_unset(self, db: Database) -> None:
        mq.record_verify_gate_score(
            db, model="opus", effort="high", score_0_10=9.0, role="Implementer",
            run_id="run-xyz",
        )
        db.log_agent_result(
            session_id="run-xyz",
            task_hash="some-task-id",
            agent_id=1,
            tier="high",
            model="opus",
        )
        snap = mq.build_quality_snapshot(db, since="all", by_role=True)
        assert snap["by_role"] == []

    def test_by_role_facet_does_not_match_on_task_hash_alone(self, db: Database) -> None:
        # Regression guard: quality events key task_hash on pattern_hash, telemetry
        # keys it on task_id. A join on task_hash alone must never match even
        # when both values coincidentally look similar, or the old bug is back.
        mq.record_verify_gate_score(
            db, model="opus", effort="high", score_0_10=9.0, role="Implementer",
            task_hash="shared-value", run_id="run-a",
        )
        db.log_agent_result(
            session_id="run-b",  # different run -> must not match
            task_hash="shared-value",
            agent_id=1,
            tier="high",
            model="opus",
            role="Implementer",
        )
        snap = mq.build_quality_snapshot(db, since="all", by_role=True)
        assert snap["by_role"] == []
