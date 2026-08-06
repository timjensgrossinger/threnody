"""Tests for shared/verify.py — failure parsing and merge-base baseline diffing.

The property that matters: a failure that was already red at the merge base must
never be attributed to the current run. Everything else in this module exists to
serve that.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shared import verify
from shared.config import VerifyGateConfig, VerifyGateSignalConfig


# ---------------------------------------------------------------------------
# Failure extraction
# ---------------------------------------------------------------------------


class TestExtractFailures:
    def test_pytest_failed_lines(self):
        out = verify.extract_failures("tests", (
            "FAILED tests/test_a.py::test_one - AssertionError: nope\n"
            "FAILED tests/test_b.py::test_two\n"
            "ERROR tests/test_c.py::test_three - ImportError\n"
            "2 failed, 5 passed\n"
        ))
        assert out == {
            "tests/test_a.py::test_one",
            "tests/test_b.py::test_two",
            "tests/test_c.py::test_three",
        }

    def test_pytest_clean_output(self):
        assert verify.extract_failures("tests", "5 passed in 0.1s") == set()

    def test_linter_lines_drop_line_numbers(self):
        # Identity must survive an insertion above the finding.
        before = verify.extract_failures("lint", "shared/a.py:10:5: E501 line too long")
        after = verify.extract_failures("lint", "shared/a.py:42:5: E501 line too long")
        assert before == after
        assert before == {"shared/a.py|E501|line too long"}

    def test_linter_normalizes_quoted_identifiers(self):
        a = verify.extract_failures("types", 'shared/a.py:3: error: Name "foo" is not defined')
        b = verify.extract_failures("types", 'shared/a.py:9: error: Name "bar" is not defined')
        assert a == b

    def test_distinct_codes_stay_distinct(self):
        out = verify.extract_failures("lint", (
            "a.py:1:1: E501 line too long\n"
            "a.py:2:1: F401 unused import\n"
        ))
        assert len(out) == 2

    def test_empty_text(self):
        assert verify.extract_failures("tests", "") == set()
        assert verify.extract_failures("lint", "") == set()

    def test_non_matching_noise_is_ignored(self):
        assert verify.extract_failures("lint", "All checks passed!\n") == set()


# ---------------------------------------------------------------------------
# run_signal
# ---------------------------------------------------------------------------


class TestRunSignal:
    def test_success(self):
        out = verify.run_signal(
            "tests", command="python3 -c pass", timeout_seconds=30, required=True, cwd=None
        )
        assert out.passed is True
        assert out.failures == set()

    def test_failure_captures_output(self):
        out = verify.run_signal(
            "tests",
            command='python3 -c "import sys; print(\'FAILED t.py::x\'); sys.exit(1)"',
            timeout_seconds=30,
            required=True,
            cwd=None,
        )
        assert out.passed is False
        assert out.failures == {"t.py::x"}

    def test_missing_required_command_is_unavailable(self):
        out = verify.run_signal("lint", command="", timeout_seconds=1, required=True, cwd=None)
        assert out.passed is False
        assert out.unavailable is True
        assert "unavailable" in out.error

    def test_missing_optional_command_is_skipped(self):
        out = verify.run_signal("lint", command="", timeout_seconds=1, required=False, cwd=None)
        assert out.passed is True
        assert out.skipped is True

    def test_timeout_records_seconds(self):
        out = verify.run_signal(
            "tests",
            command="python3 -c \"import time; time.sleep(5)\"",
            timeout_seconds=0.2,
            required=True,
            cwd=None,
        )
        assert out.timed_out is True
        assert out.to_dict()["timeout_seconds"] == 0.2

    def test_unparseable_command_is_an_error_not_a_crash(self):
        out = verify.run_signal(
            "lint", command='"unterminated', timeout_seconds=1, required=True, cwd=None
        )
        assert out.passed is False
        assert out.error


# ---------------------------------------------------------------------------
# Baseline against a real git repo
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "T"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "seed"], root)
    return root


class TestBaselineRef:
    def test_non_repo_has_no_baseline(self, tmp_path: Path):
        assert verify.resolve_baseline_ref(str(tmp_path)) is None

    def test_empty_project_root(self):
        assert verify.resolve_baseline_ref("") is None

    def test_single_commit_repo_has_no_parent(self, repo: Path):
        # Merge base is HEAD and there is no HEAD~1 — must report None, not guess.
        assert verify.resolve_baseline_ref(str(repo)) is None

    def test_second_commit_resolves_to_parent(self, repo: Path):
        (repo / "b.txt").write_text("b\n", encoding="utf-8")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "second"], repo)
        ref = verify.resolve_baseline_ref(str(repo))
        assert ref
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert ref != head


class TestBaselineSignals:
    def test_runs_command_at_the_baseline_revision(self, repo: Path):
        # marker.py exists only on the second commit; the baseline worktree must
        # therefore NOT see it.
        (repo / "check.py").write_text(
            "import os, sys\n"
            "sys.exit(0 if os.path.exists('marker.py') else 1)\n",
            encoding="utf-8",
        )
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "add check"], repo)
        base_ref = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        (repo / "marker.py").write_text("x = 1\n", encoding="utf-8")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "add marker"], repo)

        # At HEAD the check passes; at base_ref it fails.
        here = verify.run_signal(
            "tests", command="python3 check.py", timeout_seconds=30,
            required=True, cwd=str(repo),
        )
        assert here.passed is True
        out = verify.run_baseline_signals(
            {"tests": ("python3 check.py", 30.0)}, project_root=str(repo), ref=base_ref
        )
        assert "tests" in out  # ran successfully (and reported a failure set)

    def test_bad_ref_yields_no_baseline(self, repo: Path):
        out = verify.run_baseline_signals(
            {"tests": ("python3 -c pass", 5.0)},
            project_root=str(repo),
            ref="0000000000000000000000000000000000000000",
        )
        assert out == {}

    def test_no_signals_short_circuits(self, repo: Path):
        assert verify.run_baseline_signals({}, project_root=str(repo), ref="HEAD") == {}

    def test_leaves_no_worktree_behind(self, repo: Path):
        (repo / "b.txt").write_text("b\n", encoding="utf-8")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "second"], repo)
        ref = verify.resolve_baseline_ref(str(repo))
        verify.run_baseline_signals(
            {"tests": ("python3 -c pass", 5.0)}, project_root=str(repo), ref=str(ref)
        )
        listed = subprocess.run(
            ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert listed.count("\n") == 1  # only the main worktree remains


# ---------------------------------------------------------------------------
# End-to-end gate with baseline
# ---------------------------------------------------------------------------


def _cfg(command: str, *, required=True, mode="warn") -> VerifyGateConfig:
    return VerifyGateConfig(
        enabled=True,
        mode=mode,
        signals={"tests": VerifyGateSignalConfig(command=command, required=required,
                                                 timeout_seconds=30)},
    )


class TestGateWithBaseline:
    def test_clean_run_passes(self, repo: Path):
        report = verify.run_verify_gate(
            _cfg("python3 -c pass"), project_root=str(repo), baseline=False
        )
        assert report.verdict == verify.VERDICT_PASS
        assert report.new_failures == []

    def test_preexisting_failure_is_not_blamed_on_the_run(self, repo: Path):
        # A test that fails identically at HEAD and at the baseline.
        script = "import sys\nprint('FAILED t.py::already_broken')\nsys.exit(1)\n"
        (repo / "check.py").write_text(script, encoding="utf-8")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "broken check"], repo)
        (repo / "unrelated.txt").write_text("x\n", encoding="utf-8")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "unrelated change"], repo)

        report = verify.run_verify_gate(
            _cfg("python3 check.py", mode="block"), project_root=str(repo)
        )
        assert report.baseline_used is True
        assert report.new_failures == []
        assert report.preexisting_failures == ["tests:t.py::already_broken"]
        assert report.verdict == verify.VERDICT_PASS
        assert report.signals["tests"]["preexisting"] is True

    def test_newly_introduced_failure_is_reported(self, repo: Path):
        # Baseline reports one failure; HEAD reports that one plus a new one.
        (repo / "check.py").write_text(
            "import sys\nprint('FAILED t.py::old')\nsys.exit(1)\n", encoding="utf-8"
        )
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "one failure"], repo)
        (repo / "check.py").write_text(
            "import sys\nprint('FAILED t.py::old')\nprint('FAILED t.py::new')\nsys.exit(1)\n",
            encoding="utf-8",
        )
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "two failures"], repo)

        report = verify.run_verify_gate(
            _cfg("python3 check.py", mode="block"), project_root=str(repo)
        )
        assert report.baseline_used is True
        assert report.new_failures == ["tests:t.py::new"]
        assert report.preexisting_failures == ["tests:t.py::old"]
        assert report.verdict == verify.VERDICT_REJECTED

    def test_warn_mode_does_not_reject(self, repo: Path):
        (repo / "check.py").write_text(
            "import sys\nprint('FAILED t.py::new')\nsys.exit(1)\n", encoding="utf-8"
        )
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "c1"], repo)
        (repo / "x.txt").write_text("x", encoding="utf-8")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "c2"], repo)
        report = verify.run_verify_gate(
            _cfg("python3 check.py", mode="warn"), project_root=str(repo)
        )
        # Same failure at baseline -> pre-existing -> pass even in warn mode.
        assert report.verdict == verify.VERDICT_PASS

    def test_no_baseline_falls_back_to_plain_pass_fail(self, tmp_path: Path):
        report = verify.run_verify_gate(
            _cfg('python3 -c "import sys; sys.exit(1)"', mode="block"),
            project_root=str(tmp_path),
        )
        assert report.baseline_used is False
        assert report.verdict == verify.VERDICT_REJECTED
        assert report.new_failures == ["tests:failed"]

    def test_optional_unavailable_signal_passes(self, repo: Path):
        report = verify.run_verify_gate(
            VerifyGateConfig(
                enabled=True, mode="block",
                signals={"lint": VerifyGateSignalConfig(command="", required=False)},
            ),
            project_root=str(repo),
        )
        assert report.verdict == verify.VERDICT_PASS

    def test_required_unavailable_signal_does_not_reject(self, repo: Path):
        """A required signal with no configured/detectable command is a
        configuration gap (e.g. mypy not installed), not a regression this run
        introduced — it must not warn/reject, only show up in degraded_signals.
        """
        report = verify.run_verify_gate(
            VerifyGateConfig(
                enabled=True, mode="block",
                signals={"lint": VerifyGateSignalConfig(command="", required=True)},
            ),
            project_root=str(repo),
        )
        assert report.verdict == verify.VERDICT_PASS
        assert report.signals["lint"]["unavailable"] is True
        assert report.signals["lint"]["passed"] is False
        assert report.degraded_signals == ["lint"]

    def test_command_resolver_override(self, repo: Path):
        calls: list[str] = []

        def resolver(signal: str, root: str) -> str:
            calls.append(signal)
            return "python3 -c pass"

        report = verify.run_verify_gate(
            _cfg("auto"), project_root=str(repo), baseline=False, command_resolver=resolver
        )
        assert calls == ["tests"]
        assert report.verdict == verify.VERDICT_PASS


# ---------------------------------------------------------------------------
# Baseline cache
# ---------------------------------------------------------------------------


class TestBaselineCache:
    def test_round_trip(self):
        run_id = "test-verify-cache-1"
        verify.store_baseline_cache(run_id, "refA", {"tests": {"a::b"}})
        assert verify.load_baseline_cache(run_id, "refA") == {"tests": {"a::b"}}

    def test_ref_mismatch_is_a_miss(self):
        run_id = "test-verify-cache-2"
        verify.store_baseline_cache(run_id, "refA", {"tests": {"a::b"}})
        assert verify.load_baseline_cache(run_id, "refB") is None

    def test_missing_run_is_a_miss(self):
        assert verify.load_baseline_cache("test-verify-cache-nonexistent", "refA") is None

    def test_empty_run_id_is_a_miss(self):
        assert verify.load_baseline_cache("", "refA") is None


# ---------------------------------------------------------------------------
# Host-native gate at finalize (token-free, in-process)
# ---------------------------------------------------------------------------


class TestHostNativeGate:
    @staticmethod
    def _cfg_with_gate(command: str, *, mode="warn"):
        from shared.config import TGsConfig

        cfg = TGsConfig()
        cfg.verify_gate = VerifyGateConfig(
            enabled=True, mode=mode,
            signals={"tests": VerifyGateSignalConfig(command=command, required=True,
                                                     timeout_seconds=30)},
        )
        return cfg

    def test_disabled_gate_returns_none(self, repo: Path):
        from shared.config import TGsConfig
        from shared.host_learning import _run_host_verify_gate

        cfg = TGsConfig()
        cfg.verify_gate = VerifyGateConfig(enabled=False)
        assert _run_host_verify_gate(
            "r1", {"assigned_files": ["a.py"]}, config=cfg,
            workspace_root=str(repo), success=True,
        ) is None

    def test_no_written_files_skips_the_gate(self, repo: Path):
        from shared.host_learning import _run_host_verify_gate

        assert _run_host_verify_gate(
            "r1", {"assigned_files": []},
            config=self._cfg_with_gate("python3 -c pass"),
            workspace_root=str(repo), success=True,
        ) is None

    def test_failed_run_skips_the_gate(self, repo: Path):
        from shared.host_learning import _run_host_verify_gate

        assert _run_host_verify_gate(
            "r1", {"assigned_files": ["a.py"]},
            config=self._cfg_with_gate("python3 -c pass"),
            workspace_root=str(repo), success=False,
        ) is None

    def test_clean_run_reports_pass_without_followup(self, repo: Path):
        from shared.host_learning import _run_host_verify_gate

        report = _run_host_verify_gate(
            "r-clean", {"assigned_files": ["a.py"]},
            config=self._cfg_with_gate("python3 -c pass"),
            workspace_root=str(repo), success=True,
        )
        assert report is not None
        assert report["verdict"] == verify.VERDICT_PASS
        assert report["new_failures"] == []
        assert "followup" not in report

    def test_new_failure_yields_a_single_low_tier_followup(self, repo: Path):
        from shared.host_learning import _run_host_verify_gate

        (repo / "check.py").write_text(
            "import sys\nprint('FAILED t.py::old')\nsys.exit(1)\n", encoding="utf-8"
        )
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "one"], repo)
        (repo / "check.py").write_text(
            "import sys\nprint('FAILED t.py::old')\nprint('FAILED t.py::new')\nsys.exit(1)\n",
            encoding="utf-8",
        )
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "two"], repo)

        report = _run_host_verify_gate(
            "r-fail", {"assigned_files": ["check.py"]},
            config=self._cfg_with_gate("python3 check.py"),
            workspace_root=str(repo), success=True,
        )
        assert report is not None
        followup = report["followup"]
        assert followup["tier"] == "low"
        assert followup["read_only"] is False
        assert "t.py::new" in followup["description"]
        # The pre-existing failure must be explicitly excluded from the fix scope.
        assert "t.py::old" not in followup["description"]
        assert "already existed" in followup["description"]

    def test_promote_verify_keys_lifts_to_top_level(self):
        from shared.host_learning import _promote_verify_keys

        response = {"finalize": {"verify_report": {"verdict": "warn"},
                                 "verify_followup": {"tier": "low"}}}
        _promote_verify_keys(response)
        assert response["verify_report"] == {"verdict": "warn"}
        assert response["verify_followup"] == {"tier": "low"}

    def test_promote_verify_keys_is_a_noop_without_finalize(self):
        from shared.host_learning import _promote_verify_keys

        response = {"run_id": "x"}
        _promote_verify_keys(response)
        assert response == {"run_id": "x"}
