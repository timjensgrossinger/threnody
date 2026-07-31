"""Tests for the hybrid diagnose->implement split and its learned tier delta.

Two invariants matter most:
  1. The split emits TIERS only — never a model name. Model resolution must stay
     with host_spawn/preferred_routing, so a role->model map can never creep in.
  2. It never fires where it would be redundant or harmful (already-sequenced
     plans, urgent work, read-only cells, unbounded fan-out).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shared.config import HybridConfig
from shared.heuristic_plan import (
    apply_hybrid_split,
    build_heuristic_plan_payload,
    hybrid_profile_key,
)
from shared.hybrid_learning import load_hybrid_delta_bias, record_hybrid_outcome
from shared.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "cache.db")


def _payload(*subtasks, **extra):
    base = {
        "analysis": "test plan",
        "subtasks": list(subtasks),
        "strategy": "parallel",
        "topology": "linear",
    }
    base.update(extra)
    return base


def _writer(sid=1, tier="high", path="svc.py", **extra):
    st = {
        "id": sid,
        "description": f"Update {path}",
        "tier": tier,
        "target_file": path,
        "depends_on": [],
    }
    st.update(extra)
    return st


def _cfg(**kw):
    cfg = HybridConfig()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


# ---------------------------------------------------------------------------
# When the split applies
# ---------------------------------------------------------------------------


class TestSplitApplies:
    def test_high_tier_writer_gets_diagnosis(self):
        out = apply_hybrid_split(_payload(_writer()), task="refactor it", config=_cfg())
        assert "hybrid_split" in out
        diagnose = out["subtasks"][0]
        implement = out["subtasks"][1]
        assert diagnose["wave_kind"] == "diagnose"
        assert diagnose["tier"] == "high"
        assert diagnose["read_only"] is True
        assert diagnose["depends_on"] == []
        assert implement["wave_kind"] == "implement"
        assert implement["tier"] == "medium"
        assert implement["depends_on"] == [diagnose["id"]]

    def test_topology_and_strategy_become_dag(self):
        out = apply_hybrid_split(_payload(_writer()), task="t", config=_cfg())
        assert out["strategy"] == "dag"
        assert out["topology"] == "dag"

    def test_diagnosis_id_does_not_collide(self):
        out = apply_hybrid_split(
            _payload(_writer(sid=1), _writer(sid=7, path="b.py")), task="t", config=_cfg()
        )
        ids = [st["id"] for st in out["subtasks"]]
        assert len(ids) == len(set(ids))
        assert out["hybrid_split"]["diagnose_id"] == 8

    def test_prompt_names_task_and_files(self):
        out = apply_hybrid_split(
            _payload(_writer(path="auth/mw.py")), task="add token rotation", config=_cfg()
        )
        desc = out["subtasks"][0]["description"]
        assert "add token rotation" in desc
        assert "auth/mw.py" in desc
        assert "READ-ONLY" in desc
        assert "## Change spec" in desc

    def test_implementer_told_to_follow_spec(self):
        out = apply_hybrid_split(_payload(_writer()), task="t", config=_cfg())
        assert "change-spec" in out["subtasks"][1]["description"]

    def test_emits_no_model_names(self):
        # Guard the core differentiator: tiers only, never a hardcoded model.
        out = apply_hybrid_split(_payload(_writer()), task="t", config=_cfg())
        for st in out["subtasks"]:
            assert "model" not in st
        blob = repr(out).lower()
        for banned in ("haiku", "sonnet", "opus", "gpt-", "gemini", "claude-"):
            assert banned not in blob

    def test_multiple_writers_share_one_diagnosis(self):
        out = apply_hybrid_split(
            _payload(_writer(sid=1, path="a.py"), _writer(sid=2, path="b.py")),
            task="t",
            config=_cfg(),
        )
        diagnose_id = out["hybrid_split"]["diagnose_id"]
        writers = [st for st in out["subtasks"] if st.get("wave_kind") == "implement"]
        assert len(writers) == 2
        assert all(st["depends_on"] == [diagnose_id] for st in writers)
        assert out["hybrid_split"]["files"] == ["a.py", "b.py"]

    def test_delta_minus_two_reaches_low(self):
        out = apply_hybrid_split(
            _payload(_writer()), task="t", config=_cfg(implement_tier_delta=-2)
        )
        assert out["subtasks"][1]["tier"] == "low"

    def test_medium_min_tier_is_supported(self):
        out = apply_hybrid_split(
            _payload(_writer(tier="medium")), task="t", config=_cfg(min_tier="medium")
        )
        assert out["hybrid_split"]["implement_tier"] == "low"


# ---------------------------------------------------------------------------
# When the split must NOT apply
# ---------------------------------------------------------------------------


class TestSplitSuppressed:
    def test_disabled(self):
        payload = _payload(_writer())
        assert apply_hybrid_split(payload, task="t", config=_cfg(enabled=False)) is payload

    def test_no_high_tier_writer(self):
        payload = _payload(_writer(tier="low"), _writer(sid=2, tier="medium", path="b.py"))
        assert apply_hybrid_split(payload, task="t", config=_cfg()) is payload

    def test_read_only_cells_never_split(self):
        payload = _payload(_writer(read_only=True))
        assert apply_hybrid_split(payload, task="t", config=_cfg()) is payload

    def test_subtask_without_target_file(self):
        payload = _payload({"id": 1, "description": "think", "tier": "high", "depends_on": []})
        assert apply_hybrid_split(payload, task="t", config=_cfg()) is payload

    def test_already_sequenced_plan_is_left_alone(self):
        # A contract-first / integration DAG already paid for the upfront reasoning.
        payload = _payload(
            {"id": 1, "description": "contract", "tier": "high", "target_file": "api.py",
             "depends_on": []},
            _writer(sid=2, path="impl.py", depends_on=[1]),
        )
        assert apply_hybrid_split(payload, task="t", config=_cfg()) is payload

    def test_urgent_work_skips_the_hop(self):
        payload = _payload(_writer())
        assert apply_hybrid_split(
            payload, task="t", urgency_score=0.9, config=_cfg()
        ) is payload

    def test_urgency_below_threshold_still_splits(self):
        out = apply_hybrid_split(
            _payload(_writer()), task="t", urgency_score=0.2, config=_cfg()
        )
        assert "hybrid_split" in out

    def test_short_duration_skips_the_hop(self):
        payload = _payload(_writer())
        assert apply_hybrid_split(
            payload, task="t", duration_bucket="short", config=_cfg()
        ) is payload

    def test_long_duration_splits(self):
        out = apply_hybrid_split(
            _payload(_writer()), task="t", duration_bucket="long", config=_cfg()
        )
        assert "hybrid_split" in out

    def test_too_many_files(self):
        writers = [_writer(sid=i, path=f"f{i}.py") for i in range(1, 12)]
        payload = _payload(*writers)
        assert apply_hybrid_split(payload, task="t", config=_cfg(max_files=8)) is payload

    def test_empty_plan(self):
        payload = _payload()
        assert apply_hybrid_split(payload, task="t", config=_cfg()) is payload

    def test_no_config_is_a_noop(self):
        payload = _payload(_writer())
        # config=None falls through to the live config loader; force the disabled
        # path explicitly so this test does not depend on the installed file.
        assert apply_hybrid_split(payload, task="t", config=_cfg(enabled=False)) is payload

    def test_zero_delta_would_be_pure_overhead(self):
        # Clamped by config loading, but guard the function directly too.
        payload = _payload(_writer())
        assert apply_hybrid_split(
            payload, task="t", config=_cfg(implement_tier_delta=0)
        ) is payload


# ---------------------------------------------------------------------------
# Profile key
# ---------------------------------------------------------------------------


class TestProfileKey:
    def test_transferable_across_paths(self):
        a = hybrid_profile_key(["src/one.py"], "high")
        b = hybrid_profile_key(["other/dir/two.py"], "high")
        assert a == b == ".py|single|high"

    def test_buckets_by_file_count(self):
        assert hybrid_profile_key(["a.py"], "high").endswith("single|high")
        assert hybrid_profile_key(["a.py", "b.py"], "high").endswith("few|high")
        assert hybrid_profile_key([f"f{i}.py" for i in range(6)], "high").endswith("many|high")

    def test_mixed_extensions(self):
        assert hybrid_profile_key(["a.py", "b.ts"], "high").startswith("mixed|")

    def test_extensionless(self):
        assert hybrid_profile_key(["Makefile"], "high").startswith("noext|")


# ---------------------------------------------------------------------------
# Learned delta
# ---------------------------------------------------------------------------


class TestHybridLearning:
    def test_empty_table_yields_no_bias(self, db: Database):
        assert load_hybrid_delta_bias(db) == {}

    def test_below_min_samples_is_ignored(self, db: Database):
        record_hybrid_outcome(db, profile_key=".py|single|high", delta=-1, clean=False)
        assert load_hybrid_delta_bias(db) == {}

    def test_repeated_rework_shrinks_the_discount(self, db: Database):
        for _ in range(30):
            record_hybrid_outcome(db, profile_key=".py|single|high", delta=-1, clean=False)
        assert load_hybrid_delta_bias(db)[".py|single|high"] == 1

    def test_repeated_clean_deepens_the_discount(self, db: Database):
        for _ in range(60):
            record_hybrid_outcome(db, profile_key=".py|single|high", delta=-1, clean=True)
        assert load_hybrid_delta_bias(db)[".py|single|high"] == -1

    def test_empty_profile_key_ignored(self, db: Database):
        record_hybrid_outcome(db, profile_key="", delta=-1, clean=True)
        with db.conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM hybrid_tier_bias").fetchone()[0] == 0

    def test_sample_count_accumulates_per_delta(self, db: Database):
        record_hybrid_outcome(db, profile_key="k", delta=-1, clean=True)
        record_hybrid_outcome(db, profile_key="k", delta=-1, clean=True)
        record_hybrid_outcome(db, profile_key="k", delta=-2, clean=False)
        with db.conn() as conn:
            rows = dict(
                conn.execute(
                    "SELECT delta, sample_count FROM hybrid_tier_bias WHERE profile_key='k'"
                ).fetchall()
            )
        assert rows == {-1: 2, -2: 1}

    def test_bias_shrinks_delta_in_plan(self, db: Database, monkeypatch):
        # A profile with a rework history must plan a shallower discount.
        for _ in range(30):
            record_hybrid_outcome(db, profile_key=".py|single|high", delta=-2, clean=False)
        monkeypatch.setattr("shared.heuristic_plan._intel_db", lambda: db)
        out = apply_hybrid_split(
            _payload(_writer()), task="t", config=_cfg(implement_tier_delta=-2)
        )
        assert out["hybrid_split"]["delta"] == -1
        assert out["subtasks"][1]["tier"] == "medium"

    def test_bias_is_clamped_to_valid_range(self, db: Database, monkeypatch):
        for _ in range(60):
            record_hybrid_outcome(db, profile_key=".py|single|high", delta=-1, clean=True)
        monkeypatch.setattr("shared.heuristic_plan._intel_db", lambda: db)
        out = apply_hybrid_split(
            _payload(_writer()), task="t", config=_cfg(implement_tier_delta=-2)
        )
        # -2 deepened by -1 must clamp at -2, not run off to -3.
        assert out["hybrid_split"]["delta"] == -2

    def test_learning_disabled_ignores_bias(self, db: Database, monkeypatch):
        for _ in range(30):
            record_hybrid_outcome(db, profile_key=".py|single|high", delta=-1, clean=False)
        monkeypatch.setattr("shared.heuristic_plan._intel_db", lambda: db)
        out = apply_hybrid_split(
            _payload(_writer()), task="t", config=_cfg(learning_enabled=False)
        )
        assert out["hybrid_split"]["delta"] == -1


# ---------------------------------------------------------------------------
# Integration through the real plan builder
# ---------------------------------------------------------------------------


class TestPlanIntegration:
    # These prompts must be genuinely complex: the duration axis (Phase 6)
    # suppresses the split for 'short' work, so a one-line edit forced to
    # default_tier="high" correctly does NOT split.
    def test_high_default_tier_plan_splits(self):
        payload = build_heuristic_plan_payload(
            "Refactor the token rotation architecture in auth/rotate.py", default_tier="high"
        )
        assert "hybrid_split" in payload
        kinds = [st.get("wave_kind") for st in payload["subtasks"]]
        assert kinds == ["diagnose", "implement"]

    def test_waves_order_diagnosis_first(self):
        from shared.planner import Subtask, build_waves

        payload = build_heuristic_plan_payload(
            "Refactor the token rotation architecture in auth/rotate.py", default_tier="high"
        )
        subtasks = [
            Subtask(
                id=st["id"], description=st["description"], tier=st["tier"],
                depends_on=list(st.get("depends_on") or []),
            )
            for st in payload["subtasks"]
        ]
        waves = build_waves(subtasks)
        by_id = {st["id"]: st for st in payload["subtasks"]}
        assert len(waves) == 2
        assert [by_id[i]["wave_kind"] for i in waves[0]] == ["diagnose"]
        assert [by_id[i]["wave_kind"] for i in waves[1]] == ["implement"]

    def test_low_tier_plan_is_untouched(self):
        payload = build_heuristic_plan_payload(
            "Add a docstring to utils/strings.py", default_tier="low"
        )
        assert "hybrid_split" not in payload
        assert all(st.get("wave_kind") is None for st in payload["subtasks"])

    def test_review_plans_are_never_split(self, tmp_path: Path):
        # REVIEW: fanout is read-only end to end; a diagnosis would be nonsense.
        f = tmp_path / "svc.py"
        f.write_text("import os\ndef r(c):\n    os.system(c)\n", encoding="utf-8")
        payload = build_heuristic_plan_payload(
            f"REVIEW: {f}", default_tier="high"
        )
        assert "hybrid_split" not in payload
