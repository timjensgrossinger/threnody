"""Tests for shared/beliefs.py — repo-scoped belief capture and injection.

Closes the loop that previously stored project memory and never read it back.
Key properties: recurring lessons reinforce rather than duplicate, constraints
outrank patterns, injected text is hard-capped, and a fresh repo injects nothing.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from shared import beliefs as bl
from shared.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "cache.db")


@pytest.fixture
def project(tmp_path: Path) -> str:
    root = tmp_path / "proj"
    root.mkdir()
    return str(root.resolve())


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class TestRecordBelief:
    def test_records_pattern_and_constraint(self, db: Database, project: str):
        assert bl.record_belief(
            kind="pattern", summary="did X cleanly", project_id=project, db=db
        )
        assert bl.record_belief(
            kind="constraint", summary="did Y badly", project_id=project, db=db
        )
        kinds = {b.kind for b in bl.load_beliefs(project, db=db)}
        assert kinds == {"pattern", "constraint"}

    @pytest.mark.parametrize("kind", ["", "bogus", "PATTERNS", None])
    def test_invalid_kind_rejected(self, db: Database, project: str, kind):
        assert bl.record_belief(kind=kind, summary="x y z", project_id=project, db=db) is False

    def test_empty_summary_rejected(self, db: Database, project: str):
        assert bl.record_belief(kind="pattern", summary="   ", project_id=project, db=db) is False

    def test_empty_project_rejected(self, db: Database):
        assert bl.record_belief(kind="pattern", summary="x", project_id="", db=db) is False

    def test_kind_is_case_insensitive(self, db: Database, project: str):
        assert bl.record_belief(
            kind="Pattern", summary="mixed case kind", project_id=project, db=db
        )

    def test_recurrence_reinforces_instead_of_duplicating(self, db: Database, project: str):
        for _ in range(3):
            bl.record_belief(
                kind="constraint", summary="the same lesson again", project_id=project, db=db
            )
        found = bl.load_beliefs(project, db=db)
        assert len(found) == 1
        assert found[0].hits == 3

    def test_recurrence_survives_wording_noise(self, db: Database, project: str):
        # normalize_pattern folds quotes/paths/whitespace, so trivially reworded
        # repeats of the same lesson must still collapse to one belief.
        bl.record_belief(
            kind="constraint", summary="failed   editing   auth.py", project_id=project, db=db
        )
        bl.record_belief(
            kind="constraint", summary="failed editing auth.py", project_id=project, db=db
        )
        assert len(bl.load_beliefs(project, db=db)) == 1

    def test_summary_is_length_capped(self, db: Database, project: str):
        bl.record_belief(
            kind="pattern", summary="w " * 900, project_id=project, db=db
        )
        found = bl.load_beliefs(project, db=db)
        assert len(found[0].summary) <= bl.MAX_SUMMARY_CHARS

    def test_paths_are_stored_and_bounded(self, db: Database, project: str):
        bl.record_belief(
            kind="pattern", summary="many files",
            project_id=project, paths=[f"f{i}.py" for i in range(40)], db=db,
        )
        assert len(bl.load_beliefs(project, db=db)[0].paths) == 20

    def test_projects_are_isolated(self, db: Database, tmp_path: Path):
        a = str((tmp_path / "a").resolve())
        b = str((tmp_path / "b").resolve())
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        bl.record_belief(kind="pattern", summary="only in a", project_id=a, db=db)
        assert len(bl.load_beliefs(a, db=db)) == 1
        assert bl.load_beliefs(b, db=db) == []


# ---------------------------------------------------------------------------
# Loading / ranking
# ---------------------------------------------------------------------------


class TestLoadBeliefs:
    def test_fresh_repo_returns_nothing(self, db: Database, project: str):
        assert bl.load_beliefs(project, db=db) == []

    def test_empty_project_id_returns_nothing(self, db: Database):
        assert bl.load_beliefs("", db=db) == []

    def test_limit_is_respected(self, db: Database, project: str):
        for i in range(8):
            bl.record_belief(kind="pattern", summary=f"lesson number {i}", project_id=project, db=db)
        assert len(bl.load_beliefs(project, limit=3, db=db)) == 3

    def test_zero_limit_returns_nothing(self, db: Database, project: str):
        bl.record_belief(kind="pattern", summary="x y z", project_id=project, db=db)
        assert bl.load_beliefs(project, limit=0, db=db) == []

    def test_constraint_outranks_pattern_on_a_tie(self, db: Database, project: str):
        bl.record_belief(kind="pattern", summary="tie breaker case", project_id=project, db=db)
        bl.record_belief(kind="constraint", summary="tie breaker case", project_id=project, db=db)
        assert bl.load_beliefs(project, db=db)[0].kind == "constraint"

    def test_recurring_belief_outranks_one_off(self, db: Database, project: str):
        bl.record_belief(kind="pattern", summary="seen once only", project_id=project, db=db)
        for _ in range(6):
            bl.record_belief(kind="pattern", summary="seen many times", project_id=project, db=db)
        assert "many" in bl.load_beliefs(project, db=db)[0].summary

    def test_query_path_returns_relevant_belief(self, db: Database, project: str):
        bl.record_belief(
            kind="constraint", summary="payment retry loop deadlocked", project_id=project, db=db
        )
        bl.record_belief(
            kind="pattern", summary="documentation update went fine", project_id=project, db=db
        )
        found = bl.load_beliefs(project, query="payment retry", db=db)
        assert any("payment" in b.summary for b in found)

    def test_unmatched_query_falls_back_to_full_set(self, db: Database, project: str):
        bl.record_belief(kind="pattern", summary="something unrelated", project_id=project, db=db)
        # No FTS hit for this query, but the repo still has a lesson worth showing.
        assert bl.load_beliefs(project, query="zzzznomatch", db=db)

    def test_recency_decay_prefers_newer(self, db: Database, project: str):
        old = bl.Belief("pattern", "old lesson", (), 1, time.time() - 400 * 24 * 3600, "k1")
        new = bl.Belief("pattern", "new lesson", (), 1, time.time(), "k2")
        now = time.time()
        assert bl._score(new, now=now, fts_rank=None) > bl._score(old, now=now, fts_rank=None)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestFormatBlock:
    def test_empty_renders_nothing(self):
        assert bl.format_belief_block([]) == ""

    def test_labels_both_sections(self):
        block = bl.format_belief_block([
            bl.Belief("constraint", "avoid this", (), 1, time.time(), "k1"),
            bl.Belief("pattern", "do this", (), 1, time.time(), "k2"),
        ])
        assert "Avoid (these previously caused failures or rework here):" in block
        assert "Worked before in this repo:" in block
        assert "avoid this" in block
        assert "do this" in block

    def test_framed_as_evidence_not_instruction(self):
        block = bl.format_belief_block(
            [bl.Belief("pattern", "x y z", (), 1, time.time(), "k")]
        )
        assert "not instructions" in block
        assert "trust the code" in block

    def test_hard_char_cap_is_enforced(self):
        many = [
            bl.Belief("pattern", f"lesson {i} " + "x" * 200, (), 1, time.time(), f"k{i}")
            for i in range(20)
        ]
        block = bl.format_belief_block(many, max_chars=300)
        assert block.endswith("...")
        # Header is outside the body budget; body itself must be capped.
        assert len(block) < 700

    def test_constraint_only(self):
        block = bl.format_belief_block(
            [bl.Belief("constraint", "only avoid", (), 1, time.time(), "k")]
        )
        assert "Worked before" not in block


class TestBuildBeliefContext:
    def test_fresh_repo_is_empty_string(self, db: Database, project: str):
        assert bl.build_belief_context(project, db=db) == ""

    def test_returns_rendered_block(self, db: Database, project: str):
        bl.record_belief(kind="constraint", summary="watch out here", project_id=project, db=db)
        out = bl.build_belief_context(project, db=db)
        assert "watch out here" in out

    def test_never_raises_on_bad_input(self):
        assert bl.build_belief_context("/nonexistent/zzz", db=None) == ""


# ---------------------------------------------------------------------------
# Injection into subtask enrichment
# ---------------------------------------------------------------------------


class TestContextInjection:
    def test_block_injected_into_enriched_subtask(self, db: Database, project: str):
        from shared.context import enrich_subtask
        from shared.planner import Subtask

        bl.record_belief(
            kind="constraint",
            summary="the retry loop here deadlocks under load",
            project_id=project,
            db=db,
        )
        subtask = Subtask(id=1, description="Fix the retry loop", tier="medium")
        enriched = enrich_subtask(subtask, project, db=db)
        assert "deadlocks under load" in enriched.description
        assert "What this repo has taught us" in enriched.description

    def test_fresh_repo_leaves_subtask_untouched(self, db: Database, project: str):
        from shared.context import enrich_subtask
        from shared.planner import Subtask

        subtask = Subtask(id=1, description="Do a thing", tier="medium")
        assert enrich_subtask(subtask, project, db=db) is subtask

    def test_no_project_root_injects_nothing(self, db: Database):
        from shared.context import build_belief_block

        assert build_belief_block(None, db=db) == ""

    def test_disabled_config_injects_nothing(self, db: Database, project: str, monkeypatch):
        from shared import context
        from shared.config import BeliefsConfig, TGsConfig

        bl.record_belief(kind="pattern", summary="should not appear", project_id=project, db=db)
        cfg = TGsConfig()
        cfg.beliefs = BeliefsConfig(enabled=True, inject_enabled=False)
        monkeypatch.setattr(TGsConfig, "from_yaml", classmethod(lambda cls, *a, **k: cfg))
        assert context.build_belief_block(project, db=db) == ""


# ---------------------------------------------------------------------------
# Capture at finalize
# ---------------------------------------------------------------------------


class TestFinalizeCapture:
    def test_clean_run_records_a_pattern(self, db: Database, project: str):
        from shared.host_learning import _record_run_belief

        _record_run_belief(
            db,
            {"task_hint": "add JWT middleware", "assigned_files": ["auth/mw.py"]},
            project_id=project, success=True, rework_events=[], verify_report=None, config=None,
        )
        found = bl.load_beliefs(project, db=db)
        assert len(found) == 1
        assert found[0].kind == "pattern"
        assert "add JWT middleware" in found[0].summary
        assert "auth/mw.py" in found[0].summary

    def test_failed_run_records_a_constraint(self, db: Database, project: str):
        from shared.host_learning import _record_run_belief

        _record_run_belief(
            db, {"task_hint": "refactor payments", "assigned_files": []},
            project_id=project, success=False, rework_events=[], verify_report=None, config=None,
        )
        found = bl.load_beliefs(project, db=db)
        assert found[0].kind == "constraint"
        assert "did not complete successfully" in found[0].summary

    def test_reworked_run_records_a_constraint(self, db: Database, project: str):
        from shared.host_learning import _record_run_belief

        _record_run_belief(
            db, {"task_hint": "touch the parser", "assigned_files": []},
            project_id=project, success=True, rework_events=[{"path": "p.py"}],
            verify_report=None, config=None,
        )
        found = bl.load_beliefs(project, db=db)
        assert found[0].kind == "constraint"
        assert "rework pass" in found[0].summary

    def test_verify_dirty_run_records_a_constraint(self, db: Database, project: str):
        from shared.host_learning import _record_run_belief

        _record_run_belief(
            db, {"task_hint": "change the schema", "assigned_files": []},
            project_id=project, success=True, rework_events=[],
            verify_report={"new_failures": ["tests:a::b", "tests:c::d"]}, config=None,
        )
        found = bl.load_beliefs(project, db=db)
        assert found[0].kind == "constraint"
        assert "2 new verify failure" in found[0].summary

    def test_no_task_hint_records_nothing(self, db: Database, project: str):
        from shared.host_learning import _record_run_belief

        _record_run_belief(
            db, {"task_hint": "", "assigned_files": ["a.py"]},
            project_id=project, success=True, rework_events=[], verify_report=None, config=None,
        )
        assert bl.load_beliefs(project, db=db) == []

    def test_capture_disabled_records_nothing(self, db: Database, project: str):
        from shared.config import BeliefsConfig, TGsConfig
        from shared.host_learning import _record_run_belief

        cfg = TGsConfig()
        cfg.beliefs = BeliefsConfig(enabled=True, capture_enabled=False)
        _record_run_belief(
            db, {"task_hint": "do a thing", "assigned_files": []},
            project_id=project, success=True, rework_events=[], verify_report=None, config=cfg,
        )
        assert bl.load_beliefs(project, db=db) == []

    def test_default_project_fallback_is_not_written(self, db: Database):
        # finalize falls back to "default-project" with no resolved workspace root;
        # pooling unrelated repos' lessons there would inject them everywhere.
        from shared.host_learning import _record_run_belief

        _record_run_belief(
            db, {"task_hint": "do a thing", "assigned_files": ["a.py"]},
            project_id="default-project", success=True, rework_events=[],
            verify_report=None, config=None,
        )
        assert bl.load_beliefs("default-project", db=db) == []
