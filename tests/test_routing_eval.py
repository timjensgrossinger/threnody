"""
Pytest suite for shared/routing_eval.py — Phase 27 eval runner.

Covers:
- _agents_to_fanout unit tests
- _compare_fixture unit tests (ordering, first-failure reasons)
- run_eval integration: PASS/SKIP/FAIL behavior, boundary skipping, exit codes
- CLI unknown --filter → exit code 1
- THRENODY_TEST_MODE set before classify
"""
import builtins
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import shared.config as config_module
import shared.routing_eval as routing_eval
from shared.routing_eval import (
    _agents_to_fanout,
    _compare_fixture,
    run_eval,
)


# ---------------------------------------------------------------------------
# Minimal fake RoutingDecision (mirrors shared/router.py dataclass)
# ---------------------------------------------------------------------------

@dataclass
class FakeDecision:
    tier: str
    score: float
    urgency_score: float = 0.0
    agents: int = 1
    reason: str = ""
    override: bool = False
    intent_modifier: float = 0.0
    matched_urgency_signals: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Unit tests: _agents_to_fanout
# ---------------------------------------------------------------------------

def test_agents_to_fanout():
    assert _agents_to_fanout(0) == "none"
    assert _agents_to_fanout(1) == "none"
    assert _agents_to_fanout(2) == "favor_parallel"
    assert _agents_to_fanout(5) == "favor_parallel"
    assert _agents_to_fanout(100) == "favor_parallel"


# ---------------------------------------------------------------------------
# Unit tests: _compare_fixture
# ---------------------------------------------------------------------------

def _base_fixture(**overrides) -> dict:
    f = {
        "id": "test-fixture",
        "category": "low_tier",
        "tags": ["stable"],
        "prompt": "Rename foo.py to bar.py",
        "expected": {
            "tier": "low",
            "score_min": 0.0,
            "score_max": 0.50,
            "urgency_expected": False,
            "fanout_expected": "none",
        },
    }
    f["expected"].update(overrides)
    return f


def test_compare_fixture_cases():
    # Case A: all fields match → pass
    fixture = _base_fixture()
    decision = FakeDecision(tier="low", score=0.25, urgency_score=0.0, agents=1)
    passed, reason = _compare_fixture(fixture, decision)
    assert passed is True
    assert reason == ""

    # Case B: tier mismatch → fail with tier reason
    fixture_b = _base_fixture(tier="high")
    decision_b = FakeDecision(tier="low", score=0.80, urgency_score=0.0, agents=1)
    passed_b, reason_b = _compare_fixture(fixture_b, decision_b)
    assert passed_b is False
    assert "tier=low" in reason_b
    assert "expected=high" in reason_b

    # Case C: score out of range → fail with score reason
    fixture_c = _base_fixture(tier="high", score_min=0.60, score_max=1.0)
    decision_c = FakeDecision(tier="high", score=0.22, urgency_score=0.0, agents=1)
    passed_c, reason_c = _compare_fixture(fixture_c, decision_c)
    assert passed_c is False
    assert "score=0.22" in reason_c
    assert "0.60" in reason_c

    # Case D: urgency mismatch → fail with urgency reason (tier+score ok)
    fixture_d = _base_fixture(tier="high", score_min=0.0, score_max=1.0, urgency_expected=True)
    decision_d = FakeDecision(tier="high", score=0.5, urgency_score=0.0, agents=1)
    passed_d, reason_d = _compare_fixture(fixture_d, decision_d)
    assert passed_d is False
    assert "urgency" in reason_d

    # Case E: fanout mismatch → fail with fanout reason (all others ok)
    fixture_e = _base_fixture(
        tier="medium", score_min=0.0, score_max=1.0,
        fanout_expected="favor_parallel",
    )
    del fixture_e["expected"]["urgency_expected"]
    decision_e = FakeDecision(tier="medium", score=0.5, urgency_score=0.0, agents=1)
    passed_e, reason_e = _compare_fixture(fixture_e, decision_e)
    assert passed_e is False
    assert "fanout=none" in reason_e
    assert "expected=favor_parallel" in reason_e


def test_compare_fixture_ordering_first_fail():
    """Tier failure is reported even when score also fails (first-failure only)."""
    fixture = _base_fixture(tier="high", score_min=0.80, score_max=1.0)
    decision = FakeDecision(tier="low", score=0.1)  # both tier + score fail
    passed, reason = _compare_fixture(fixture, decision)
    assert passed is False
    # Should report tier failure, not score failure
    assert "tier=low" in reason
    assert "score" not in reason


# ---------------------------------------------------------------------------
# Integration tests: run_eval behavior
# ---------------------------------------------------------------------------

class _StubRouter:
    """Stub router whose classify returns a passing FakeDecision for each sample fixture."""

    _TABLE = {
        # Maps prompt substring → FakeDecision that passes the sample fixture
        "Rename utils.py": FakeDecision(tier="low", score=0.25, agents=1),
        "Refactor the authentication module": FakeDecision(tier="medium", score=0.55, agents=2),
        "Write an Ansible playbook to deploy nginx": FakeDecision(tier="high", score=0.80, agents=1),
        "prod is down": FakeDecision(tier="high", score=0.80, urgency_score=0.9, agents=1),
    }

    def __init__(self, config, db=None):
        pass

    def classify(self, prompt, project_path=None):
        for key, decision in self._TABLE.items():
            if key.lower() in prompt.lower():
                return decision
        return FakeDecision(tier="low", score=0.25)


@pytest.fixture(autouse=True)
def _reset_taskrouter(monkeypatch):
    """Reset _TaskRouter after each test so lazy import does not leak between tests."""
    monkeypatch.setattr(routing_eval, "_TaskRouter", None)


def test_run_eval_sample_fixtures_pass(monkeypatch, capsys):
    sample_fixtures = [
        {
            "id": "sample-low",
            "category": "low_tier",
            "tags": ["stable"],
            "prompt": "Rename utils.py to helpers.py",
            "expected": {
                "tier": "low",
                "score_min": 0.0,
                "score_max": 0.5,
                "urgency_expected": False,
                "fanout_expected": "none",
            },
            "_source": "low_tier/sample_low.json",
        },
        {
            "id": "sample-medium",
            "category": "medium_tier",
            "tags": ["stable"],
            "prompt": "Refactor the authentication module across auth.py service.py cli.py",
            "expected": {
                "tier": "medium",
                "score_min": 0.5,
                "score_max": 0.7,
                "urgency_expected": False,
                "fanout_expected": "favor_parallel",
            },
            "_source": "medium_tier/sample_medium.json",
        },
        {
            "id": "sample-high",
            "category": "high_tier",
            "tags": ["stable"],
            "prompt": "Write an Ansible playbook to deploy nginx across two hosts",
            "expected": {
                "tier": "high",
                "score_min": 0.7,
                "score_max": 0.9,
                "urgency_expected": False,
                "fanout_expected": "none",
            },
            "_source": "high_tier/sample_high.json",
        },
        {
            "id": "sample-urgency",
            "category": "urgency",
            "tags": ["stable"],
            "prompt": "prod is down and customers cannot log in",
            "expected": {
                "tier": "high",
                "score_min": 0.7,
                "score_max": 0.9,
                "urgency_expected": True,
                "fanout_expected": "none",
            },
            "_source": "urgency/sample_urgency.json",
        },
    ]
    monkeypatch.setattr(routing_eval, "_TaskRouter", _StubRouter)
    monkeypatch.setattr(routing_eval, "load_fixtures", lambda category=None: sample_fixtures)
    code = run_eval(None)
    captured = capsys.readouterr()
    out = captured.out
    assert code == 0, f"Expected exit 0, got {code}. Output:\n{out}"
    # Each sample fixture should produce a PASS line.
    assert out.count("PASS") == 4
    assert "Failed:  0" in out
    assert "Skipped: 0" in out
    assert "Passed:  4" in out


def test_boundary_fixture_skipped(monkeypatch, capsys):
    boundary_fixtures = [
        {
            "id": "risky-boundary",
            "category": "high_tier",
            "tags": ["boundary", "stable"],
            "prompt": "Rewrite the kernel",
            "expected": {"tier": "high", "score_min": 0.6, "score_max": 1.0},
            "_source": "high_tier/risky_boundary.json",
        }
    ]
    monkeypatch.setattr(routing_eval, "load_fixtures", lambda category=None: boundary_fixtures)
    monkeypatch.setattr(routing_eval, "_TaskRouter", _StubRouter)

    code = run_eval(None)
    captured = capsys.readouterr()
    out = captured.out

    assert "SKIP  [high_tier] risky-boundary   (boundary fixture)" in out
    assert code == 0


def test_run_eval_stable_fail_returns_exit_1(monkeypatch, capsys):
    """A stable fixture that fails should return exit code 1."""
    failing_fixtures = [
        {
            "id": "expected-high",
            "category": "high_tier",
            "tags": ["stable"],
            "prompt": "Complex architecture change",
            "expected": {"tier": "high", "score_min": 0.7, "score_max": 1.0},
            "_source": "high_tier/expected_high.json",
        }
    ]
    monkeypatch.setattr(routing_eval, "load_fixtures", lambda category=None: failing_fixtures)

    class LowStubRouter:
        def __init__(self, config, db=None):
            pass
        def classify(self, prompt, project_path=None):
            return FakeDecision(tier="low", score=0.1)

    monkeypatch.setattr(routing_eval, "_TaskRouter", LowStubRouter)

    code = run_eval(None)
    captured = capsys.readouterr()
    out = captured.out

    assert code == 1
    assert "FAIL" in out
    assert "Failed:  1" in out


def test_run_eval_invalid_fixture_fails_without_classify(monkeypatch, capsys):
    classify_calls: list[str] = []
    invalid_fixtures = [
        {
            "id": "invalid-fixture",
            "category": "low_tier",
            "tags": ["stable"],
            "expected": {"tier": "low", "score_min": 0.0, "score_max": 1.0},
            "_source": "low_tier/invalid_fixture.json",
        }
    ]
    monkeypatch.setattr(routing_eval, "load_fixtures", lambda category=None: invalid_fixtures)

    class CountingRouter:
        def __init__(self, config, db=None):
            pass

        def classify(self, prompt, project_path=None):
            classify_calls.append(prompt)
            return FakeDecision(tier="low", score=0.25)

    monkeypatch.setattr(routing_eval, "_TaskRouter", CountingRouter)

    code = run_eval(None)
    out = capsys.readouterr().out

    assert code == 1
    assert "FAIL  [low_tier] invalid-fixture" in out
    assert "invalid fixture (low_tier/invalid_fixture.json): missing required field: 'prompt'" in out
    assert not classify_calls


def test_run_eval_classify_exception_becomes_fixture_failure(monkeypatch, capsys):
    fixtures = [
        {
            "id": "classify-boom",
            "category": "low_tier",
            "tags": ["stable"],
            "prompt": "Rename utils.py to helpers.py",
            "expected": {"tier": "low", "score_min": 0.0, "score_max": 1.0},
            "_source": "low_tier/classify_boom.json",
        }
    ]
    monkeypatch.setattr(routing_eval, "load_fixtures", lambda category=None: fixtures)

    class ExplodingRouter:
        def __init__(self, config, db=None):
            pass

        def classify(self, prompt, project_path=None):
            raise RuntimeError("router exploded")

    monkeypatch.setattr(routing_eval, "_TaskRouter", ExplodingRouter)

    code = run_eval(None)
    out = capsys.readouterr().out

    assert code == 1
    assert "FAIL  [low_tier] classify-boom   classify error: RuntimeError: router exploded" in out
    assert "Failed:  1" in out


def test_run_eval_malformed_decision_becomes_fixture_failure(monkeypatch, capsys):
    fixtures = [
        {
            "id": "bad-decision",
            "category": "low_tier",
            "tags": ["stable"],
            "prompt": "Rename utils.py to helpers.py",
            "expected": {"tier": "low", "score_min": 0.0, "score_max": 1.0},
            "_source": "low_tier/bad_decision.json",
        }
    ]
    monkeypatch.setattr(routing_eval, "load_fixtures", lambda category=None: fixtures)

    class MalformedRouter:
        def __init__(self, config, db=None):
            pass

        def classify(self, prompt, project_path=None):
            return None

    monkeypatch.setattr(routing_eval, "_TaskRouter", MalformedRouter)

    code = run_eval(None)
    out = capsys.readouterr().out

    assert code == 1
    assert "FAIL  [low_tier] bad-decision   malformed decision: missing attributes: tier, score, urgency_score, agents" in out
    assert "Failed:  1" in out


def test_run_eval_decision_attribute_access_failure_becomes_fixture_failure(monkeypatch, capsys):
    fixtures = [
        {
            "id": "bad-attribute-access",
            "category": "low_tier",
            "tags": ["stable"],
            "prompt": "Rename utils.py to helpers.py",
            "expected": {"tier": "low", "score_min": 0.0, "score_max": 1.0},
            "_source": "low_tier/bad_attribute_access.json",
        }
    ]
    monkeypatch.setattr(routing_eval, "load_fixtures", lambda category=None: fixtures)

    class RaisingDecision:
        def __getattr__(self, name):
            raise RuntimeError(f"cannot read {name}")

    class RaisingRouter:
        def __init__(self, config, db=None):
            pass

        def classify(self, prompt, project_path=None):
            return RaisingDecision()

    monkeypatch.setattr(routing_eval, "_TaskRouter", RaisingRouter)

    code = run_eval(None)
    out = capsys.readouterr().out

    assert code == 1
    assert "FAIL  [low_tier] bad-attribute-access   malformed decision: attribute access failed for tier: RuntimeError: cannot read tier" in out
    assert "Failed:  1" in out


def test_run_eval_router_constructor_failure_returns_exit_1(monkeypatch, capsys):
    class ExplodingRouter:
        def __init__(self, config, db=None):
            raise RuntimeError("constructor exploded")

    monkeypatch.setattr(routing_eval, "_TaskRouter", ExplodingRouter)

    code = run_eval(None)
    out = capsys.readouterr().out

    assert code == 1
    assert "ERROR: failed to initialize eval router: RuntimeError: constructor exploded" in out


def test_run_eval_router_import_failure_returns_exit_1(monkeypatch, capsys):
    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "shared.router":
            raise ImportError("router import exploded")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(routing_eval, "_TaskRouter", None)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    code = run_eval(None)
    out = capsys.readouterr().out

    assert code == 1
    assert "ERROR: failed to initialize eval router: ImportError: router import exploded" in out


def test_run_eval_fixture_loading_failure_returns_exit_1(monkeypatch, capsys):
    class SafeRouter:
        def __init__(self, config, db=None):
            pass

    def exploding_load(category=None):
        raise TypeError("fixture load exploded")

    monkeypatch.setattr(routing_eval, "_TaskRouter", SafeRouter)
    monkeypatch.setattr(routing_eval, "load_fixtures", exploding_load)

    code = run_eval(None)
    out = capsys.readouterr().out

    assert code == 1
    assert "ERROR: failed to load eval fixtures: TypeError: fixture load exploded" in out


def test_run_eval_non_list_fixtures_return_exit_1(monkeypatch, capsys):
    class SafeRouter:
        def __init__(self, config, db=None):
            pass

    monkeypatch.setattr(routing_eval, "_TaskRouter", SafeRouter)
    monkeypatch.setattr(routing_eval, "load_fixtures", lambda category=None: None)

    code = run_eval(None)
    out = capsys.readouterr().out

    assert code == 1
    assert "ERROR: failed to load eval fixtures: TypeError: expected list, got NoneType" in out


def test_run_eval_non_dict_fixture_becomes_failure(monkeypatch, capsys):
    class SafeRouter:
        def __init__(self, config, db=None):
            pass

    monkeypatch.setattr(routing_eval, "_TaskRouter", SafeRouter)
    monkeypatch.setattr(routing_eval, "load_fixtures", lambda category=None: ["bad-fixture"])

    code = run_eval(None)
    out = capsys.readouterr().out

    assert code == 1
    assert "FAIL  [unknown] unknown   invalid fixture entry: expected dict, got str" in out
    assert "Failed:  1" in out


def test_cli_unknown_filter_exits_1():
    result = subprocess.run(
        [sys.executable, "-m", "shared.routing_eval", "--filter", "not-a-filter"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "ERROR: unknown filter value: 'not-a-filter'" in combined


def test_tgsrouter_test_mode_set_before_classify(monkeypatch, capsys):
    """Verifies THRENODY_TEST_MODE == '1' is set when classify is called."""
    test_mode_verified = []

    class TestModeCheckRouter:
        def __init__(self, config, db=None):
            pass

        def classify(self, prompt, project_path=None):
            test_mode_verified.append(os.environ.get("THRENODY_TEST_MODE"))
            return FakeDecision(tier="low", score=0.25)

    seed_fixtures = [
        {
            "id": "check-mode",
            "category": "low_tier",
            "tags": ["stable"],
            "prompt": "Rename foo.py",
            "expected": {"tier": "low", "score_min": 0.0, "score_max": 1.0},
            "_source": "low_tier/check_mode.json",
        }
    ]
    monkeypatch.setattr(routing_eval, "load_fixtures", lambda category=None: seed_fixtures)
    monkeypatch.setattr(routing_eval, "_TaskRouter", TestModeCheckRouter)

    run_eval(None)

    assert test_mode_verified, "classify was never called"
    assert all(v == "1" for v in test_mode_verified), (
        f"THRENODY_TEST_MODE not set correctly: {test_mode_verified}"
    )


def test_unknown_filter_value_returns_exit_1(monkeypatch, capsys):
    """run_eval() called programmatically with unknown filter → exit code 1."""
    monkeypatch.setattr(routing_eval, "_TaskRouter", _StubRouter)
    code = run_eval(filter_categories=["not-a-filter"])
    out = capsys.readouterr().out
    assert code == 1
    assert "ERROR: unknown filter value: 'not-a-filter'" in out


def test_filter_alias_low(monkeypatch, capsys):
    """'low' filter alias resolves to low_tier fixtures only."""
    seen_categories = []

    def fake_load(category=None):
        seen_categories.append(category)
        return []

    monkeypatch.setattr(routing_eval, "load_fixtures", fake_load)
    monkeypatch.setattr(routing_eval, "_TaskRouter", _StubRouter)

    run_eval(filter_categories=["low"])
    assert "low_tier" in seen_categories


def test_filter_alias_med(monkeypatch, capsys):
    """'med' and 'medium' filter aliases resolve to medium_tier."""
    for alias in ("med", "medium"):
        seen_categories = []

        def fake_load(category=None, _acc=seen_categories):
            _acc.append(category)
            return []

        monkeypatch.setattr(routing_eval, "load_fixtures", fake_load)
        monkeypatch.setattr(routing_eval, "_TaskRouter", _StubRouter)

        run_eval(filter_categories=[alias])
        assert "medium_tier" in seen_categories, f"alias '{alias}' did not resolve"


def test_run_eval_tolerates_missing_yaml_support_in_test_mode(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("planner_model: custom-model\n", encoding="utf-8")

    fixtures = [
        {
            "id": "sample-low",
            "category": "low_tier",
            "tags": ["stable"],
            "prompt": "Rename utils.py to helpers.py",
            "expected": {
                "tier": "low",
                "score_min": 0.0,
                "score_max": 0.5,
                "urgency_expected": False,
                "fanout_expected": "none",
            },
            "_source": "low_tier/sample_low.json",
        }
    ]

    monkeypatch.setattr(config_module, "CONFIG_YAML", config_path)
    monkeypatch.setattr(config_module, "yaml", None)
    monkeypatch.setattr(routing_eval, "load_fixtures", lambda category=None: fixtures)
    monkeypatch.setattr(routing_eval, "_TaskRouter", _StubRouter)

    code = run_eval(None)
    out = capsys.readouterr().out

    assert code == 0
    assert "Passed:  1" in out
