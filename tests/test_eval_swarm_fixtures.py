#!/usr/bin/env python3
"""Swarm-focused eval gating tests for Phase 37."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import shared.routing_eval as routing_eval


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


class _SwarmEvalRouter:
    _TABLE = {
        "Production incident blocking release today": FakeDecision(
            tier="medium",
            score=0.6,
            urgency_score=0.9,
            agents=2,
        ),
        "Design a hierarchical swarm plan": FakeDecision(
            tier="high",
            score=0.9,
            urgency_score=0.0,
            agents=1,
        ),
        "Design a system plan for a budget-limited swarm execution": FakeDecision(
            tier="high",
            score=0.9,
            urgency_score=0.0,
            agents=1,
        ),
        "Implement authentication middleware across auth_login.py": FakeDecision(
            tier="medium",
            score=0.68,
            urgency_score=0.0,
            agents=2,
        ),
    }

    def __init__(self, config, db=None):
        pass

    def classify(self, prompt, project_path=None):
        for needle, decision in self._TABLE.items():
            if needle in prompt:
                return decision
        raise AssertionError(f"unexpected prompt in swarm eval test: {prompt}")


FIXTURE_FILES = [
    ROOT / "tests" / "eval" / "fixtures_swarm_star.json",
    ROOT / "tests" / "eval" / "fixtures_swarm_hierarchical.json",
    ROOT / "tests" / "eval" / "fixtures_swarm_budget_gate.json",
    ROOT / "tests" / "eval" / "fixtures_swarm_dag_fallback.json",
]


def test_fixtures_loadable() -> None:
    for path in FIXTURE_FILES:
        fixture = routing_eval.load_fixture(path)
        assert fixture["test_mode"] == "THRENODY_TEST_MODE"
        assert fixture["tags"] == ["stable", "swarm"]
        assert isinstance(fixture["simulated_result"], dict)


def test_swarm_fixtures_have_no_regressions(monkeypatch) -> None:
    monkeypatch.setattr(routing_eval, "_TaskRouter", _SwarmEvalRouter)
    baseline_path = ROOT / "tests" / "eval" / "baseline.json"

    for path in FIXTURE_FILES:
        fixture = routing_eval.load_fixture(path)
        result = routing_eval.run_eval(
            fixtures=[fixture],
            baseline_path=baseline_path,
            return_results=True,
            retry_once=True,
        )
        assert set(result.keys()) == {"result", "regressions", "exit_code"}
        assert result["exit_code"] == 0
        assert result["regressions"] == []
        assert len(result["result"]) == 1
        actual = result["result"][0]
        assert actual["status"] == "pass"
        assert actual["result"]["swarm"]["topology"] == fixture["simulated_result"]["topology"]
        compatibility = actual["result"]["swarm"]["compatibility"]
        assert "plan_task" in compatibility
        assert "fleet_plan" in compatibility
        assert "execute_subtask" in compatibility
