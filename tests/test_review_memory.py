"""Tests for shared/review_memory.py — prior-review skip/replay and finding lifecycle.

The load-bearing property is that a skip is only ever taken when it cannot lose
coverage: same content digest AND an equal-or-stronger prior tier.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shared import code_intel as ci
from shared import review_memory as rm
from shared.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "cache.db")


@pytest.fixture(autouse=True)
def _clear_intel():
    ci.clear_intel_cache()
    yield
    ci.clear_intel_cache()


def _finding(summary="os.system with concatenated input", category="command-injection",
             severity="high", line=3):
    return {"category": category, "severity": severity, "line": line, "summary": summary}


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_stable_for_same_finding(self):
        a = rm.finding_fingerprint("security", "sql-injection", "Unbound query on line 12")
        b = rm.finding_fingerprint("security", "sql-injection", "Unbound query on line 12")
        assert a == b

    def test_differs_by_dimension_and_category(self):
        base = rm.finding_fingerprint("security", "sql-injection", "x")
        assert base != rm.finding_fingerprint("logic", "sql-injection", "x")
        assert base != rm.finding_fingerprint("security", "xss", "x")

    def test_ignores_case_and_whitespace(self):
        a = rm.finding_fingerprint("security", "sqli", "Unbound   query")
        b = rm.finding_fingerprint("SECURITY", " SQLI ", "unbound query")
        assert a == b

    def test_line_number_not_part_of_identity(self):
        # Findings are stored with a line, but identity must survive a shifted line
        # or resolve-by-absence would fire on every reformat.
        a = rm.finding_fingerprint("edge", "null-deref", "value may be None")
        b = rm.finding_fingerprint("edge", "null-deref", "value may be None")
        assert a == b


# ---------------------------------------------------------------------------
# tier_covers — the skip safety gate
# ---------------------------------------------------------------------------


class TestTierCovers:
    @pytest.mark.parametrize("cached,planned,expected", [
        ("high", "high", True),
        ("high", "low", True),
        ("medium", "low", True),
        ("medium", "medium", True),
        ("low", "low", True),
        ("low", "medium", False),
        ("low", "high", False),
        ("medium", "high", False),
    ])
    def test_rank_comparison(self, cached, planned, expected):
        assert rm.tier_covers(cached, planned) is expected

    @pytest.mark.parametrize("cached,planned", [
        ("", "high"), ("bogus", "low"), ("low", ""), ("low", "bogus"), (None, "low"),
    ])
    def test_unknown_tiers_fail_safe_to_rerun(self, cached, planned):
        assert rm.tier_covers(cached, planned) is False


# ---------------------------------------------------------------------------
# record / load round trip
# ---------------------------------------------------------------------------


class TestRecordAndLoad:
    def test_scan_round_trip(self, db: Database):
        rm.record_review_scan(
            db, path="a.py", content_sha="sha1", dimension="security", tier="medium",
            findings_total=1, findings_high=1, findings=[_finding()], model="haiku",
        )
        cached = rm.load_cached_scan(db, "a.py", "sha1", "security")
        assert cached is not None
        assert cached.tier == "medium"
        assert cached.findings_total == 1
        assert len(cached.findings) == 1
        assert cached.findings[0].category == "command-injection"
        assert cached.findings[0].status == rm.STATUS_OPEN

    def test_clean_scan_is_recorded(self, db: Database):
        # A clean review must still be cached, or clean files re-review forever.
        rm.record_review_scan(
            db, path="a.py", content_sha="sha1", dimension="logic", tier="low",
            findings_total=0, findings_high=0,
        )
        cached = rm.load_cached_scan(db, "a.py", "sha1", "logic")
        assert cached is not None
        assert cached.findings_total == 0
        assert cached.findings == ()

    def test_miss_on_different_sha(self, db: Database):
        rm.record_review_scan(
            db, path="a.py", content_sha="sha1", dimension="logic", tier="low",
            findings_total=0, findings_high=0,
        )
        assert rm.load_cached_scan(db, "a.py", "sha2", "logic") is None

    def test_miss_on_different_dimension(self, db: Database):
        rm.record_review_scan(
            db, path="a.py", content_sha="sha1", dimension="logic", tier="low",
            findings_total=0, findings_high=0,
        )
        assert rm.load_cached_scan(db, "a.py", "sha1", "security") is None

    def test_empty_identifiers_are_ignored(self, db: Database):
        rm.record_review_scan(
            db, path="", content_sha="sha1", dimension="logic", tier="low",
            findings_total=0, findings_high=0,
        )
        assert rm.load_cached_scan(db, "", "sha1", "logic") is None
        assert rm.load_cached_scan(db, "a.py", "", "logic") is None

    def test_rescan_same_revision_updates_in_place(self, db: Database):
        for tier in ("low", "high"):
            rm.record_review_scan(
                db, path="a.py", content_sha="sha1", dimension="logic", tier=tier,
                findings_total=0, findings_high=0,
            )
        cached = rm.load_cached_scan(db, "a.py", "sha1", "logic")
        assert cached is not None and cached.tier == "high"
        with db.conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM review_scans").fetchone()[0]
        assert n == 1

    def test_severity_ordering_puts_high_first(self, db: Database):
        rm.record_review_scan(
            db, path="a.py", content_sha="sha1", dimension="security", tier="high",
            findings_total=2, findings_high=1,
            findings=[
                _finding(summary="minor thing", category="info", severity="low", line=1),
                _finding(summary="rce here", category="rce", severity="high", line=9),
            ],
        )
        cached = rm.load_cached_scan(db, "a.py", "sha1", "security")
        assert cached is not None
        assert [f.severity for f in cached.findings] == ["high", "low"]


# ---------------------------------------------------------------------------
# Finding lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_resolved_when_absent_after_edit(self, db: Database):
        rm.record_review_scan(
            db, path="a.py", content_sha="sha1", dimension="security", tier="high",
            findings_total=1, findings_high=1, findings=[_finding()],
        )
        # New revision, finding no longer reported (something else was).
        rm.record_review_scan(
            db, path="a.py", content_sha="sha2", dimension="security", tier="high",
            findings_total=1, findings_high=0,
            findings=[_finding(summary="different issue", category="weak-crypto",
                               severity="medium")],
        )
        resolved = rm.load_resolved_findings(db, "a.py", "security")
        assert [f.category for f in resolved] == ["command-injection"]

    def test_not_resolved_on_same_revision(self, db: Database):
        # Two runs over identical content disagreeing is NOT evidence of a fix.
        rm.record_review_scan(
            db, path="a.py", content_sha="sha1", dimension="security", tier="high",
            findings_total=1, findings_high=1, findings=[_finding()],
        )
        rm.record_review_scan(
            db, path="a.py", content_sha="sha1", dimension="security", tier="high",
            findings_total=1, findings_high=0,
            findings=[_finding(summary="other", category="xss", severity="medium")],
        )
        assert rm.load_resolved_findings(db, "a.py", "security") == []

    def test_counts_only_report_leaves_lifecycle_untouched(self, db: Database):
        rm.record_review_scan(
            db, path="a.py", content_sha="sha1", dimension="security", tier="high",
            findings_total=1, findings_high=1, findings=[_finding()],
        )
        # A later revision reporting only counts must not resolve anything —
        # absence of detail is not evidence of a fix.
        rm.record_review_scan(
            db, path="a.py", content_sha="sha2", dimension="security", tier="high",
            findings_total=1, findings_high=1,
        )
        assert rm.load_resolved_findings(db, "a.py", "security") == []

    def test_still_reported_stays_open(self, db: Database):
        for sha in ("sha1", "sha2"):
            rm.record_review_scan(
                db, path="a.py", content_sha=sha, dimension="security", tier="high",
                findings_total=1, findings_high=1, findings=[_finding()],
            )
        assert rm.load_resolved_findings(db, "a.py", "security") == []
        cached = rm.load_cached_scan(db, "a.py", "sha2", "security")
        assert cached is not None and cached.findings[0].status == rm.STATUS_OPEN

    def test_first_seen_preserved_across_revisions(self, db: Database):
        rm.record_review_scan(
            db, path="a.py", content_sha="sha1", dimension="security", tier="high",
            findings_total=1, findings_high=1, findings=[_finding()],
        )
        with db.conn() as conn:
            first = conn.execute(
                "SELECT first_seen_ts, first_seen_sha FROM review_findings"
            ).fetchone()
        rm.record_review_scan(
            db, path="a.py", content_sha="sha2", dimension="security", tier="high",
            findings_total=1, findings_high=1, findings=[_finding()],
        )
        with db.conn() as conn:
            again = conn.execute(
                "SELECT first_seen_ts, first_seen_sha, last_seen_sha FROM review_findings"
            ).fetchone()
        assert again[0] == first[0]
        assert again[1] == "sha1"
        assert again[2] == "sha2"

    def test_malformed_findings_are_skipped_not_fatal(self, db: Database):
        rm.record_review_scan(
            db, path="a.py", content_sha="sha1", dimension="logic", tier="low",
            findings_total=3, findings_high=0,
            findings=["not a mapping", {"summary": ""}, {"no_summary": 1}, _finding()],
        )
        cached = rm.load_cached_scan(db, "a.py", "sha1", "logic")
        assert cached is not None
        assert len(cached.findings) == 1

    def test_non_list_findings_tolerated(self, db: Database):
        rm.record_review_scan(
            db, path="a.py", content_sha="sha1", dimension="logic", tier="low",
            findings_total=0, findings_high=0, findings="oops",
        )
        assert rm.load_cached_scan(db, "a.py", "sha1", "logic") is not None


# ---------------------------------------------------------------------------
# Prompt blocks
# ---------------------------------------------------------------------------


class TestPromptBlocks:
    def test_empty_blocks_render_nothing(self):
        assert rm.format_resolved_block([]) == ""
        assert rm.format_replay_block([]) == ""

    def test_resolved_block_instructs_suppression(self):
        f = rm.StoredFinding("fp", "sql-injection", "high", 5, "unbound query", "resolved")
        block = rm.format_resolved_block([f])
        assert "since FIXED" in block
        assert "unbound query" in block

    def test_replay_block_lists_findings(self):
        scan = rm.CachedScan(
            path="a.py", dimension="security", tier="high", findings_total=1,
            findings_high=1,
            findings=(rm.StoredFinding("fp", "rce", "high", 9, "eval on input", "open"),),
            ts=1.0,
        )
        block = rm.format_replay_block([scan])
        assert "Carried over from a prior review" in block
        assert "eval on input" in block

    def test_replay_block_notes_clean_and_detailless_scans(self):
        clean = rm.CachedScan("a.py", "logic", "low", 0, 0, (), 1.0)
        countless = rm.CachedScan("b.py", "logic", "low", 4, 2, (), 1.0)
        block = rm.format_replay_block([clean, countless])
        assert "previously reviewed, clean" in block
        assert "detail not stored" in block


# ---------------------------------------------------------------------------
# Integration with build_review_subtasks
# ---------------------------------------------------------------------------


class TestPlanIntegration:
    @staticmethod
    def _src(tmp_path: Path) -> str:
        f = tmp_path / "svc.py"
        f.write_text("import os\ndef run(c):\n    os.system('rm ' + c)\n", encoding="utf-8")
        return str(f)

    def test_second_run_on_unchanged_file_skips_cells(self, db: Database, tmp_path: Path):
        from shared.review_fanout import build_review_subtasks

        path = self._src(tmp_path)
        task = f"REVIEW: {path}"
        first = build_review_subtasks([(path, "")], task, db=db)
        cells = [s for s in first["subtasks"] if s.get("review_dimension")]
        assert cells and first["cached_cell_count"] == 0

        sha = ci.scan(path, db=db).content_sha
        for cell in cells:
            rm.record_review_scan(
                db, path=path, content_sha=sha, dimension=cell["review_dimension"],
                tier=cell["tier"], findings_total=1, findings_high=1,
                findings=[_finding()],
            )

        second = build_review_subtasks([(path, "")], task, db=db)
        assert [s for s in second["subtasks"] if s.get("review_dimension")] == []
        assert second["cached_cell_count"] == len(cells)
        # Synthesis survives so the carried-over findings still reach the report.
        synth = [s for s in second["subtasks"] if not s.get("review_dimension")]
        assert len(synth) == 1
        assert "Carried over from a prior review" in synth[0]["description"]
        assert path in synth[0]["description"]

    def test_weaker_prior_tier_does_not_satisfy_stronger_plan(
        self, db: Database, tmp_path: Path
    ):
        from shared.review_fanout import build_review_subtasks

        path = self._src(tmp_path)
        task = f"REVIEW: {path}"
        first = build_review_subtasks([(path, "")], task, db=db)
        cells = [s for s in first["subtasks"] if s.get("review_dimension")]
        sha = ci.scan(path, db=db).content_sha
        # Record every cell as having run at 'low', below what the plan wants.
        for cell in cells:
            rm.record_review_scan(
                db, path=path, content_sha=sha, dimension=cell["review_dimension"],
                tier="low", findings_total=0, findings_high=0,
            )
        second = build_review_subtasks([(path, "")], task, db=db)
        rerun = [s for s in second["subtasks"] if s.get("review_dimension")]
        # Every cell the plan tiers above 'low' must be re-run.
        assert any(s["tier"] in ("medium", "high") for s in rerun)
        assert len(rerun) == sum(1 for c in cells if c["tier"] != "low")

    def test_edit_invalidates_the_skip(self, db: Database, tmp_path: Path):
        from shared.review_fanout import build_review_subtasks

        path = self._src(tmp_path)
        task = f"REVIEW: {path}"
        first = build_review_subtasks([(path, "")], task, db=db)
        cells = [s for s in first["subtasks"] if s.get("review_dimension")]
        sha = ci.scan(path, db=db).content_sha
        for cell in cells:
            rm.record_review_scan(
                db, path=path, content_sha=sha, dimension=cell["review_dimension"],
                tier=cell["tier"], findings_total=0, findings_high=0,
            )
        Path(path).write_text(
            "import subprocess\ndef run(c):\n    subprocess.run(['rm', c])\n",
            encoding="utf-8",
        )
        ci.clear_intel_cache()
        third = build_review_subtasks([(path, "")], task, db=db)
        assert third["cached_cell_count"] == 0
        assert [s for s in third["subtasks"] if s.get("review_dimension")]

    def test_no_db_disables_memory(self, tmp_path: Path):
        from shared.review_fanout import build_review_subtasks

        path = self._src(tmp_path)
        plan = build_review_subtasks([(path, "")], f"REVIEW: {path}")
        assert plan["cached_cell_count"] == 0

    def test_resolved_findings_injected_into_prompt(self, db: Database, tmp_path: Path):
        from shared.review_fanout import build_review_subtasks

        path = self._src(tmp_path)
        # A security finding recorded then fixed across two revisions.
        rm.record_review_scan(
            db, path=path, content_sha="old1", dimension="security", tier="high",
            findings_total=1, findings_high=1, findings=[_finding()],
        )
        rm.record_review_scan(
            db, path=path, content_sha="old2", dimension="security", tier="high",
            findings_total=1, findings_high=0,
            findings=[_finding(summary="other", category="xss", severity="medium")],
        )
        plan = build_review_subtasks([(path, "")], f"REVIEW: {path}", db=db)
        sec = [s for s in plan["subtasks"] if s.get("review_dimension") == "security"]
        assert sec, "security cell should run — current revision was never scanned"
        assert "since FIXED" in sec[0]["description"]
        assert "command-injection" in sec[0]["description"]
