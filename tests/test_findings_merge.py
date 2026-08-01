"""Unit tests for shared/findings_merge.py — in-process review findings merge."""
from __future__ import annotations

from shared.findings_merge import (
    Finding,
    findings_path,
    merge,
    parse_findings_text,
    read_run_findings,
    render_report,
    review_meta_for,
    write_findings,
)

_RUN = "swarm-findings-merge-test"


def _cleanup(run_id: str) -> None:
    import shutil

    from shared.run_log import run_log_dir

    shutil.rmtree(run_log_dir(run_id), ignore_errors=True)


class TestParse:
    def test_canonical_line(self):
        findings = parse_findings_text(
            "⚠️ [HIGH] security/sql-injection — shared/db.py:42 — concatenated query (CWE-89)"
        )
        assert len(findings) == 1
        f = findings[0]
        assert (f.severity, f.dimension, f.category) == ("high", "security", "sql-injection")
        assert (f.path, f.line) == ("shared/db.py", 42)
        assert f.cwe == "CWE-89"
        # The CWE is captured in its own field, not left duplicated in the text.
        assert "CWE" not in f.description

    def test_tolerates_bullets_hyphens_and_case(self):
        """Models are unreliable about dashes, bullets and severity casing; none of
        that should cost a real finding."""
        text = (
            "- [medium] logic/off-by-one - shared/a.py:10 - loop bound excludes last row\n"
            "* ⚠️ [LOW] types/unsafe-cast – shared/b.py:3 – cast without a guard\n"
        )
        findings = parse_findings_text(text)
        assert [f.severity for f in findings] == ["medium", "low"]

    def test_missing_category_falls_back_to_dimension(self):
        findings = parse_findings_text("⚠️ [HIGH] security — a.py:1 — something bad")
        assert findings[0].dimension == "security"
        assert findings[0].category == ""

    def test_unknown_severity_normalizes_rather_than_dropping(self):
        findings = parse_findings_text("⚠️ [SPICY] logic/x — a.py:1 — odd severity word")
        assert findings and findings[0].severity == "medium"

    def test_noise_and_prose_skipped(self):
        text = (
            "### Findings\n"
            "No issues found.\n"
            "dim=security total=0 high=0\n"
            "some prose the agent added\n"
        )
        assert parse_findings_text(text) == []

    def test_empty_input(self):
        assert parse_findings_text("") == []
        assert parse_findings_text("   \n\n") == []


class TestMerge:
    def _f(self, severity: str, line: int, desc: str = "concatenated query") -> Finding:
        return Finding(
            dimension="security",
            category="sql-injection",
            severity=severity,
            path="shared/db.py",
            line=line,
            description=desc,
        )

    def test_dedup_is_line_independent(self):
        """Two agents reporting the same defect at different lines is one finding."""
        result = merge([self._f("medium", 42), self._f("high", 44)])
        assert result.total == 1
        assert result.kept[0].severity == "high"  # highest severity survives
        assert len(result.duplicates) == 1

    def test_distinct_findings_both_kept(self):
        result = merge([self._f("high", 1), self._f("high", 2, desc="missing timeout")])
        assert result.total == 2

    def test_ranking_severity_then_dimension(self):
        findings = [
            Finding("performance", "quadratic", "high", "a.py", 5, "nested loop"),
            Finding("security", "xss", "high", "a.py", 9, "unescaped output"),
            Finding("logic", "off-by-one", "critical", "a.py", 1, "bad bound"),
        ]
        kept = merge(findings).kept
        assert [f.severity for f in kept] == ["critical", "high", "high"]
        # Within equal severity, security outranks performance.
        assert kept[1].dimension == "security"

    def test_agreement_counts_reports(self):
        result = merge([self._f("high", 1), self._f("low", 2)])
        assert list(result.agreement.values()) == [2]

    def test_empty(self):
        result = merge([])
        assert result.total == 0 and result.counts_by_severity["high"] == 0


class TestRender:
    def test_no_findings(self):
        text = render_report(merge([]), reviewed_files=["a.py"])
        assert text.startswith("No issues found.")
        assert "a.py" in text

    def test_report_shape_matches_synthesis_contract(self):
        findings = parse_findings_text(
            "⚠️ [CRITICAL] logic/wrong-condition — a.py:1 — inverted auth check"
        )
        text = render_report(merge(findings), reviewed_files=["a.py"])
        assert "### Summary" in text
        assert "### Findings (ranked:" in text
        assert "⚠️ [CRITICAL] logic/wrong-condition — a.py:1 — inverted auth check" in text

    def test_duplicate_count_is_disclosed(self):
        f = Finding("security", "xss", "high", "a.py", 1, "unescaped output")
        text = render_report(merge([f, f]))
        assert "1 duplicate report(s) collapsed" in text


class TestReviewMeta:
    def test_categories_always_present(self):
        """The reason to parse findings ourselves: record_static_recall_score skips
        scoring entirely when a host reports findings without categories."""
        findings = parse_findings_text(
            "⚠️ [HIGH] security/sql-injection — a.py:1 — concat\n"
            "⚠️ [LOW] security/weak-crypto — a.py:2 — md5\n"
        )
        meta = review_meta_for(findings)
        assert meta["findings_total"] == 2
        assert meta["findings_high"] == 1
        assert set(meta["categories"]) == {"security/sql-injection", "security/weak-crypto"}
        assert meta["categories"]["security/sql-injection"]["findings_high"] == 1

    def test_critical_counts_as_high(self):
        findings = parse_findings_text("⚠️ [CRITICAL] logic/x — a.py:1 — boom")
        assert review_meta_for(findings)["findings_high"] == 1

    def test_empty_findings_is_empty_meta(self):
        meta = review_meta_for([])
        assert meta["findings_total"] == 0 and meta["categories"] == {}


class TestRunFiles:
    def test_write_then_read_round_trip(self):
        try:
            f = Finding("security", "xss", "high", "a.py", 7, "unescaped output")
            assert write_findings(_RUN, "replay", [f]) is not None
            per_agent = read_run_findings(_RUN)
            assert set(per_agent) == {"replay"}
            assert per_agent["replay"][0].description == "unescaped output"
        finally:
            _cleanup(_RUN)

    def test_missing_dir_reads_empty(self):
        """Empty means "this run used the legacy in-conversation protocol"."""
        assert read_run_findings("swarm-does-not-exist-at-all") == {}

    def test_spawn_id_cannot_escape_the_run_dir(self):
        path = findings_path(_RUN, "../../etc/passwd")
        assert path.parent.name == "findings"
        assert "etc" not in [p.name for p in path.parents]


class TestReviewMetaBackfill:
    """The findings-file protocol removes review_meta from the host's report, so
    learning must be reconstructed from the file — otherwise review_tier_bias,
    prior-review memory and the quality ledger all silently go quiet."""

    def test_backfill_from_findings_file(self):
        from shared.host_learning import _backfill_review_meta

        try:
            findings_path(_RUN, "7").parent.mkdir(parents=True, exist_ok=True)
            findings_path(_RUN, "7").write_text(
                "⚠️ [HIGH] security/sql-injection — a.py:1 — concat\n"
                "⚠️ [LOW] security/weak-crypto — a.py:2 — md5\n",
                encoding="utf-8",
            )
            spec = {"subagent_type": "review-security", "target_file": "a.py"}
            merged = _backfill_review_meta(_RUN, "7", spec, {"success": True})
            meta = merged["review_meta"]
            assert meta["findings_total"] == 2
            assert meta["findings_high"] == 1
            # Categories are the whole point — static recall scoring is skipped without them.
            assert "security/sql-injection" in meta["categories"]
        finally:
            _cleanup(_RUN)

    def test_host_reported_meta_wins(self):
        from shared.host_learning import _backfill_review_meta

        original = {"success": True, "review_meta": {"findings_total": 9}}
        spec = {"subagent_type": "review-security"}
        assert _backfill_review_meta(_RUN, "7", spec, original) is original

    def test_non_review_agent_untouched(self):
        from shared.host_learning import _backfill_review_meta

        result = {"success": True}
        assert (
            _backfill_review_meta(_RUN, "1", {"subagent_type": "threnody-low"}, result)
            is result
        )

    def test_missing_file_is_not_an_error(self):
        from shared.host_learning import _backfill_review_meta

        result = {"success": True}
        spec = {"subagent_type": "review-security"}
        assert _backfill_review_meta("swarm-nope-nothing-here", "3", spec, result) is result
