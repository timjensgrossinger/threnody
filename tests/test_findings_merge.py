"""Unit tests for shared/findings_merge.py — in-process review findings merge."""
from __future__ import annotations

from shared.findings_merge import (
    Finding,
    findings_path,
    merge,
    parse_adjudication,
    parse_findings_text,
    read_run_findings,
    read_synthesis_adjudication,
    render_report,
    review_meta_for,
    split_adjudication_sections,
    synthesis_report_path,
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

    def test_unadjudicated_is_unknown_not_kept(self):
        """The bug this file's tri-state exists for.

        With no adjudicator, ``kept`` used to default to True, so every model in every
        run was recorded as having had all its findings accepted. That made the 3.0
        "noisy" branch of findings_to_score unreachable and made review_tier_bias
        escalate a cheap tier on any high-severity finding it claimed, real or not.
        """
        findings = parse_findings_text("⚠️ [HIGH] security/x — a.py:1 — concat")
        meta = review_meta_for(findings)
        assert meta["kept_by_synthesis"] is None
        assert meta["categories"]["security/x"]["kept"] is None

    def test_adjudicated_drop_is_recorded_as_false(self):
        from shared.review_memory import finding_fingerprint

        findings = parse_findings_text("⚠️ [HIGH] types/cast — a.py:5 — unsupported claim")
        dropped = {finding_fingerprint("types", "cast", "unsupported claim")}
        meta = review_meta_for(findings, kept_fingerprints=set(), dropped_fingerprints=dropped)
        assert meta["kept_by_synthesis"] is False
        assert meta["categories"]["types/cast"]["kept"] is False

    def test_paraphrased_finding_stays_unknown(self):
        """Fingerprints fold whitespace and paths, not wording.

        A finding the synthesis agent reworded matches neither section. Guessing
        "dropped" would penalise a model for the adjudicator's paraphrase, so it must
        read as unknown instead.
        """
        findings = parse_findings_text("⚠️ [HIGH] security/x — a.py:1 — original wording")
        meta = review_meta_for(
            findings, kept_fingerprints={"deadbeef"}, dropped_fingerprints={"cafe"}
        )
        assert meta["kept_by_synthesis"] is None

    def test_one_accepted_finding_outweighs_a_rejected_one(self):
        from shared.review_memory import finding_fingerprint

        findings = parse_findings_text(
            "⚠️ [HIGH] security/a — a.py:1 — real defect\n"
            "⚠️ [LOW] security/b — a.py:2 — bogus claim\n"
        )
        meta = review_meta_for(
            findings,
            kept_fingerprints={finding_fingerprint("security", "a", "real defect")},
            dropped_fingerprints={finding_fingerprint("security", "b", "bogus claim")},
        )
        # Agent level: it surfaced something real, so it is not scored as pure noise.
        assert meta["kept_by_synthesis"] is True
        # Category level still distinguishes the two.
        assert meta["categories"]["security/a"]["kept"] is True
        assert meta["categories"]["security/b"]["kept"] is False


class TestAdjudication:
    """The synthesis agent is the only thing in the system that judges a finding."""

    _REPORT = (
        "### Summary\n"
        "1 high, 1 medium issues across 1 file(s).\n\n"
        "### Findings\n"
        "⚠️ [HIGH] security/sql-injection — a.py:10 — raw query from user input\n"
        "⚠️ [MEDIUM] logic/off-by-one — a.py:22 — loop misses last element\n\n"
        "### Dropped\n"
        "⚠️ [HIGH] types/cast — a.py:5 — unsupported claim — no such cast exists\n"
    )

    def test_splits_kept_from_dropped(self):
        kept, dropped = parse_adjudication(self._REPORT)
        assert len(kept) == 2
        assert dropped  # candidate spellings of the one rejected finding

    def test_a_rejected_finding_matches_despite_its_appended_reason(self):
        """The prompt tells the adjudicator to append its reason after a dash.

        That lands inside the description, which is part of the fingerprint — so
        without prefix candidates a rejection would never match the agent's own
        finding and the drop would silently read as unknown.
        """
        from shared.review_memory import finding_fingerprint

        _, dropped = parse_adjudication(self._REPORT)
        assert finding_fingerprint("types", "cast", "unsupported claim") in dropped

    def test_section_split_gives_a_truthful_finding_count(self):
        kept_text, dropped_text = split_adjudication_sections(self._REPORT)
        assert len(parse_findings_text(kept_text)) == 2
        assert len(parse_findings_text(dropped_text)) == 1

    def test_no_dropped_section_is_not_a_parse_failure(self):
        """An adjudicator that rejected nothing is the common case, not an error."""
        kept, dropped = parse_adjudication(
            "### Findings\n⚠️ [HIGH] security/x — a.py:1 — real\n"
        )
        assert len(kept) == 1 and dropped == set()

    def test_empty_text_yields_two_empty_sets(self):
        assert parse_adjudication("") == (set(), set())

    def test_a_finding_in_both_sections_counts_as_kept(self):
        report = (
            "### Findings\n⚠️ [HIGH] security/x — a.py:1 — dupe\n\n"
            "### Dropped\n⚠️ [HIGH] security/x — a.py:9 — dupe\n"
        )
        kept, dropped = parse_adjudication(report)
        assert len(kept) == 1 and dropped == set()

    def test_report_lives_outside_the_findings_dir(self):
        """``read_run_findings`` globs ``findings/*.md``.

        A synthesis report stored there would be re-ingested as one more agent's
        findings and every finding would be counted twice by the merge.
        """
        try:
            synthesis_report_path(_RUN, create=True).write_text(
                self._REPORT, encoding="utf-8"
            )
            write_findings(_RUN, "1", parse_findings_text("⚠️ [LOW] logic/x — a.py:1 — m"))
            assert set(read_run_findings(_RUN)) == {"1"}
        finally:
            _cleanup(_RUN)

    def test_read_returns_none_when_no_adjudicator_ran(self):
        """None ("nobody judged") must stay distinct from "judged, kept everything"."""
        assert read_synthesis_adjudication("swarm-no-such-run") is None

    def test_read_round_trips_from_the_run_dir(self):
        try:
            synthesis_report_path(_RUN, create=True).write_text(
                self._REPORT, encoding="utf-8"
            )
            verdict = read_synthesis_adjudication(_RUN)
            assert verdict is not None and len(verdict[0]) == 2 and verdict[1]
        finally:
            _cleanup(_RUN)


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

    def test_backfill_applies_the_runs_adjudication(self):
        """End of the wire: a rejected finding must reach the learning outcome as
        False, and it must find the verdict on its own — the ingest path does not
        thread it through three signatures."""
        from shared.host_learning import _RUN_ADJUDICATION, _backfill_review_meta

        try:
            findings_path(_RUN, "7").parent.mkdir(parents=True, exist_ok=True)
            findings_path(_RUN, "7").write_text(
                "⚠️ [HIGH] security/x — a.py:1 — bogus claim\n", encoding="utf-8"
            )
            synthesis_report_path(_RUN, create=True).write_text(
                "### Findings\nNo issues found.\n\n"
                "### Dropped\n⚠️ [HIGH] security/x — a.py:1 — bogus claim — not real\n",
                encoding="utf-8",
            )
            spec = {"subagent_type": "review-security", "target_file": "a.py"}
            merged = _backfill_review_meta(_RUN, "7", spec, {"success": True})
            assert merged["review_meta"]["kept_by_synthesis"] is False
        finally:
            _RUN_ADJUDICATION.pop(_RUN, None)
            _cleanup(_RUN)

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
