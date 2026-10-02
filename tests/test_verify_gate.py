"""Tests for plan 04 janitor-style verify gate."""
from __future__ import annotations

import subprocess
from unittest.mock import patch
import pytest

from shared.config import VerifyGateConfig, VerifyGateSignalConfig
from shared.orchestrator import AgentResult, Orchestrator


def test_verify_gate_config_defaults():
    cfg = VerifyGateConfig()
    assert cfg.enabled is False
    assert cfg.mode == "warn"
    assert "tests" in cfg.signals
    assert "types" in cfg.signals
    assert "lint" in cfg.signals


def test_verify_gate_signal_config():
    s = VerifyGateSignalConfig(command="pytest", required=True, timeout_seconds=45)
    assert s.command == "pytest"
    assert s.required is True
    assert s.timeout_seconds == 45


def test_agent_result_gate_verdict_default():
    r = AgentResult(subtask_id=1, tier="low", model="haiku", output="ok", token_count=10)
    assert r.gate_verdict is None
    assert r.gate_signals is None


def test_agent_result_gate_verdict_settable():
    r = AgentResult(subtask_id=1, tier="low", model="haiku", output="ok", token_count=10)
    r.gate_verdict = "pass"
    r.gate_signals = {"tests": {"passed": True}}
    assert r.gate_verdict == "pass"


def _make_orchestrator(gate_cfg: VerifyGateConfig):
    from shared.config import TGsConfig
    cfg = TGsConfig()
    cfg.verify_gate = gate_cfg
    orch = Orchestrator.__new__(Orchestrator)
    orch._config = cfg
    orch._project_root = "/tmp"
    orch._db = None
    return orch


def _subtask(target_file=None):
    from shared.planner import Subtask
    return Subtask(id=1, description="write file", tier="low", target_file=target_file)


def _result():
    return AgentResult(subtask_id=1, tier="low", model="haiku", output="done", token_count=5)


def test_gate_disabled_no_op():
    orch = _make_orchestrator(VerifyGateConfig(enabled=False))
    r = orch._run_verify_gate(_subtask("/tmp/f.py"), _result())
    assert r.gate_verdict is None


def test_gate_no_target_file_no_op():
    orch = _make_orchestrator(VerifyGateConfig(enabled=True, mode="block"))
    r = orch._run_verify_gate(_subtask(None), _result())
    assert r.gate_verdict is None


def test_gate_passing_signal_pass_verdict():
    cfg = VerifyGateConfig(
        enabled=True, mode="warn",
        signals={"tests": VerifyGateSignalConfig(command="true", required=True)},
    )
    orch = _make_orchestrator(cfg)
    r = orch._run_verify_gate(_subtask("/tmp/f.py"), _result())
    assert r.gate_verdict == "pass"
    assert r.success is True


def test_gate_failing_required_warn_mode_not_rejected():
    cfg = VerifyGateConfig(
        enabled=True, mode="warn",
        signals={"tests": VerifyGateSignalConfig(command="false", required=True)},
    )
    orch = _make_orchestrator(cfg)
    r = orch._run_verify_gate(_subtask("/tmp/f.py"), _result())
    assert r.gate_verdict == "warn"
    assert r.success is True


def test_gate_failing_required_block_mode_rejected():
    cfg = VerifyGateConfig(
        enabled=True, mode="block",
        signals={"tests": VerifyGateSignalConfig(command="false", required=True)},
    )
    orch = _make_orchestrator(cfg)
    r = orch._run_verify_gate(_subtask("/tmp/f.py"), _result())
    assert r.gate_verdict == "rejected"
    assert r.success is False


def test_gate_failing_nonrequired_does_not_reject():
    cfg = VerifyGateConfig(
        enabled=True, mode="block",
        signals={"lint": VerifyGateSignalConfig(command="false", required=False)},
    )
    orch = _make_orchestrator(cfg)
    r = orch._run_verify_gate(_subtask("/tmp/f.py"), _result())
    assert r.gate_verdict == "pass"
    assert r.success is True


def test_gate_auto_detect_no_tool_does_not_reject_required_signal():
    """A missing tool (e.g. no linter installed) is a configuration gap, not a
    regression this run introduced — it must not warn/reject on its own, and
    is surfaced separately via degraded_signals for an operator to notice.
    """
    cfg = VerifyGateConfig(
        enabled=True, mode="block",
        signals={"lint": VerifyGateSignalConfig(command="auto", required=True)},
    )
    orch = _make_orchestrator(cfg)
    with patch.object(orch, "_detect_gate_command", return_value=""):
        r = orch._run_verify_gate(_subtask("/tmp/f.py"), _result())
    assert r.gate_verdict == "pass"
    assert r.success is True
    assert r.gate_signals["lint"]["unavailable"] is True
    assert r.gate_signals["lint"]["passed"] is False
    assert r.gate_degraded_signals == ["lint"]


def test_gate_auto_detect_no_tool_skips_optional_signal():
    cfg = VerifyGateConfig(
        enabled=True, mode="block",
        signals={"lint": VerifyGateSignalConfig(command="auto", required=False)},
    )
    orch = _make_orchestrator(cfg)
    with patch.object(orch, "_detect_gate_command", return_value=""):
        r = orch._run_verify_gate(_subtask("/tmp/f.py"), _result())
    assert r.gate_verdict == "pass"
    assert r.gate_signals["lint"]["skipped"] is True


def test_gate_executes_configured_command_without_shell():
    cfg = VerifyGateConfig(
        enabled=True, mode="block",
        signals={
            "tests": VerifyGateSignalConfig(
                command="python3 -m pytest -q",
                required=True,
                timeout_seconds=45,
            ),
        },
    )
    orch = _make_orchestrator(cfg)
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="",
        stderr="",
    )
    with patch("shared.orchestrator.subprocess.run", return_value=completed) as run:
        r = orch._run_verify_gate(_subtask("/tmp/f.py"), _result())

    assert r.gate_verdict == "pass"
    run.assert_called_once_with(
        ["python3", "-m", "pytest", "-q"],
        capture_output=True,
        text=True,
        cwd="/tmp",
        timeout=45,
    )


def test_gate_required_timeout_rejects():
    cfg = VerifyGateConfig(
        enabled=True, mode="block",
        signals={
            "tests": VerifyGateSignalConfig(
                command="python3 -m pytest",
                required=True,
                timeout_seconds=1,
            ),
        },
    )
    orch = _make_orchestrator(cfg)
    timeout = subprocess.TimeoutExpired(
        cmd=["python3", "-m", "pytest"],
        timeout=1,
    )
    with patch("shared.orchestrator.subprocess.run", side_effect=timeout):
        r = orch._run_verify_gate(_subtask("/tmp/f.py"), _result())

    assert r.gate_verdict == "rejected"
    assert r.gate_signals["tests"]["timed_out"] is True
    assert r.gate_signals["tests"]["timeout_seconds"] == 1


@pytest.fixture()
def db(tmp_path):
    from shared.db import Database
    return Database(tmp_path / "test.db")


def test_routing_outcomes_has_gate_verdict_column(db):
    with db.conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(routing_outcomes)").fetchall()}
    assert "gate_verdict" in cols


def test_record_outcome_stores_gate_verdict(db):
    from shared.outcomes import record_outcome
    record_outcome(db, "task-gv-001", "accepted", gate_verdict="pass")
    with db.conn() as conn:
        row = conn.execute(
            "SELECT gate_verdict FROM routing_outcomes WHERE task_id='task-gv-001'"
        ).fetchone()
    assert row is not None and row[0] == "pass"


def test_record_outcome_gate_verdict_none_default(db):
    from shared.outcomes import record_outcome
    record_outcome(db, "task-gv-002", "accepted")
    with db.conn() as conn:
        row = conn.execute(
            "SELECT gate_verdict FROM routing_outcomes WHERE task_id='task-gv-002'"
        ).fetchone()
    assert row is not None and row[0] is None


def test_record_outcome_rejects_invalid_gate_verdict(db):
    from shared.outcomes import record_outcome
    record_outcome(db, "task-gv-003", "accepted", gate_verdict="bad_value")
    with db.conn() as conn:
        row = conn.execute(
            "SELECT gate_verdict FROM routing_outcomes WHERE task_id='task-gv-003'"
        ).fetchone()
    assert row[0] is None


def test_verify_gate_progress_callback():
    from shared.verify import run_verify_gate, SignalOutcome
    cfg = VerifyGateConfig(
        enabled=True,
        signals={
            "lint": VerifyGateSignalConfig(command="true", required=True),
            "tests": VerifyGateSignalConfig(command="true", required=True),
        },
    )
    progress_ticks = []

    def _callback(curr, total, msg):
        progress_ticks.append((curr, total, msg))

    with patch("shared.verify.run_signal", return_value=SignalOutcome(name="lint", passed=True)):
        report = run_verify_gate(
            cfg,
            project_root="/tmp",
            baseline=False,
            progress_callback=_callback,
        )
    assert report.verdict == "pass"
    assert len(progress_ticks) == 2
    assert progress_ticks[0][0] == 1
    assert progress_ticks[1][0] == 2



# ---------------------------------------------------------------------------
# Scoped verification
# ---------------------------------------------------------------------------
from shared import verify as _verify  # noqa: E402


@pytest.fixture
def scoped_repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "foo.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "bar.py").write_text("y = 1\n")
    (tmp_path / "pkg" / "notes.txt").write_text("hi\n")
    (tmp_path / "pkg" / "my mod.py").write_text("z = 1\n")
    (tmp_path / "tests" / "test_foo.py").write_text("def test_a(): pass\n")
    (tmp_path / "tests" / "test_foo_extra.py").write_text("def test_b(): pass\n")
    return tmp_path


@pytest.fixture
def all_tools(monkeypatch):
    monkeypatch.setattr(_verify.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_scoped_tests_maps_module_to_tests(scoped_repo, all_tools):
    cmd = _verify.scoped_gate_command("tests", str(scoped_repo), ["pkg/foo.py"])
    assert cmd == "python3 -m pytest --tb=no -q tests/test_foo.py tests/test_foo_extra.py"


def test_scoped_tests_test_file_itself(scoped_repo, all_tools):
    cmd = _verify.scoped_gate_command(
        "tests", str(scoped_repo), [str(scoped_repo / "tests" / "test_foo.py")]
    )
    assert cmd.endswith("tests/test_foo.py")


def test_scoped_tests_no_matching_tests(scoped_repo, all_tools):
    assert _verify.scoped_gate_command("tests", str(scoped_repo), ["pkg/bar.py"]) == ""


def test_scoped_non_py_returns_empty(scoped_repo, all_tools):
    for sig in ("lint", "types", "tests"):
        assert _verify.scoped_gate_command(sig, str(scoped_repo), ["pkg/notes.txt"]) == ""


def test_scoped_drops_outside_and_missing(scoped_repo, all_tools, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "o.py"
    outside.write_text("a = 1\n")
    cmd = _verify.scoped_gate_command(
        "lint", str(scoped_repo), [str(outside), "pkg/missing.py", "pkg/bar.py"]
    )
    assert cmd == "ruff check pkg/bar.py"
    assert _verify.scoped_gate_command("lint", str(scoped_repo), [str(outside)]) == ""


def test_scoped_lint_types_and_quoting(scoped_repo, all_tools):
    import shlex

    cmd = _verify.scoped_gate_command("types", str(scoped_repo), ["pkg/my mod.py"])
    assert shlex.split(cmd) == ["mypy", "pkg/my mod.py"]


def test_scoped_fallback_tools(scoped_repo, monkeypatch):
    monkeypatch.setattr(
        _verify.shutil, "which", lambda n: "/x" if n in {"flake8", "pyright"} else None
    )
    assert _verify.scoped_gate_command("lint", str(scoped_repo), ["pkg/foo.py"]).startswith("flake8 ")
    assert _verify.scoped_gate_command("types", str(scoped_repo), ["pkg/foo.py"]).startswith("pyright ")


def test_scoped_tool_missing(scoped_repo, monkeypatch):
    monkeypatch.setattr(_verify.shutil, "which", lambda n: None)
    for sig in ("lint", "types", "tests"):
        assert _verify.scoped_gate_command(sig, str(scoped_repo), ["pkg/foo.py"]) == ""


def test_scoped_tests_capped(tmp_path, all_tools):
    (tmp_path / "tests").mkdir()
    files = []
    for i in range(30):
        p = tmp_path / "tests" / f"test_m{i:02d}.py"
        p.write_text("")
        files.append(f"tests/test_m{i:02d}.py")
    cmd = _verify.scoped_gate_command("tests", str(tmp_path), files)
    assert cmd.count("tests/test_m") == 20


def _gate_cfg():
    return VerifyGateConfig(
        enabled=True,
        signals={
            "lint": VerifyGateSignalConfig(command="auto", required=False),
            "tests": VerifyGateSignalConfig(command="auto", required=True),
        },
    )


def test_scoped_resolver_through_gate_and_ran_signals(scoped_repo, all_tools, monkeypatch):
    seen: list[str] = []

    def fake_run(args, **kw):
        seen.append(" ".join(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(_verify.subprocess, "run", fake_run)
    report = _verify.run_verify_gate(
        _gate_cfg(),
        project_root=str(scoped_repo),
        baseline=False,
        command_resolver=_verify.scoped_resolver(["pkg/foo.py"]),
    )
    assert report.ran_signals == ["lint", "tests"]
    assert report.to_dict()["ran_signals"] == ["lint", "tests"]
    assert any("tests/test_foo.py" in c for c in seen)


def test_ran_signals_excludes_unavailable(scoped_repo, all_tools, monkeypatch):
    monkeypatch.setattr(
        _verify.subprocess, "run", lambda a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    report = _verify.run_verify_gate(
        _gate_cfg(),
        project_root=str(scoped_repo),
        baseline=False,
        command_resolver=_verify.scoped_resolver(["pkg/bar.py"]),  # no tests -> ""
    )
    assert report.ran_signals == ["lint"]
    assert "tests" in report.degraded_signals


def test_ran_signals_excludes_timeout(scoped_repo, all_tools, monkeypatch):
    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(_verify.subprocess, "run", boom)
    report = _verify.run_verify_gate(
        _gate_cfg(),
        project_root=str(scoped_repo),
        baseline=False,
        command_resolver=_verify.scoped_resolver(["pkg/foo.py"]),
    )
    assert report.ran_signals == []


def test_verify_report_score_cases():
    score = _verify.verify_report_score
    assert score({"ran_signals": [], "new_failures": []}) is None
    assert score({"ran_signals": ["lint"], "new_failures": []}) == 10.0
    assert score({"ran_signals": ["lint"], "new_failures": ["a", "b"], "baseline_used": True}) == 5.0
    assert score({"ran_signals": ["lint"], "new_failures": ["a"] * 9, "baseline_used": True}) == 0.0
    assert score({"ran_signals": ["lint"], "new_failures": ["a"], "baseline_used": False}) is None
    # degraded signal alone no longer suppresses scoring
    assert score({"ran_signals": ["lint"], "degraded_signals": ["types"]}) == 10.0


def test_verify_report_score_legacy_report():
    assert _verify.verify_report_score({"signals": {"lint": {"passed": True}}}) == 10.0
    assert _verify.verify_report_score(
        {"signals": {"lint": {"passed": True, "unavailable": True, "skipped": True}}}
    ) is None
    assert _verify.verify_report_score({"signals": {"t": {"timed_out": True}}}) is None


def test_verify_gate_scope_config(tmp_path):
    from shared.config import TGsConfig

    assert VerifyGateConfig().scope == "changed"

    def load(body: str):
        p = tmp_path / "c.yaml"
        p.write_text(body)
        return TGsConfig.from_yaml(p).verify_gate.scope

    assert load("verify_gate:\n  enabled: true\n") == "changed"
    assert load("verify_gate:\n  scope: full\n") == "full"
    assert load("verify_gate:\n  scope: bogus\n") == "changed"
