#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import TGsConfig
from shared.db import Database
from shared.planner import CLIBackend, Planner, PlannerParseError


class DummyBackend(CLIBackend):
    def call(
        self, prompt: str, model: str | None = None, timeout: int = 120
    ) -> str | None:
        return None


def _planner() -> Planner:
    tempdir = tempfile.TemporaryDirectory()
    db_path = Path(tempdir.name) / "planner.db"
    planner = Planner(
        TGsConfig(db_path=db_path),
        DummyBackend(),
        Database(db_path=db_path),
    )
    planner._phase11_tempdir = tempdir
    return planner


def test_topology_default_dag() -> None:
    planner = _planner()

    plan = planner._build_plan(
        {
            "analysis": "contracts",
            "subtasks": [
                {
                    "id": 1,
                    "description": "define metadata",
                    "tier": "low",
                    "model": "low",
                    "depends_on": [],
                }
            ],
            "strategy": "parallel",
        },
        "fallback task",
    )

    assert plan.topology == "dag"
    assert plan.max_rounds == 3
    assert plan.subtasks[0].stable_id == "phase00-plan01-task01"
    serialized = planner.plan_to_dict(plan)
    assert serialized["topology"] == "dag"
    assert serialized["max_rounds"] == 3
    assert serialized["subtasks"][0]["stable_id"] == "phase00-plan01-task01"


def test_roundtrip_serialization() -> None:
    planner = _planner()
    parsed = {
        "analysis": "contracts",
        "topology": "star",
        "subtasks": [
            {
                "id": 1,
                "description": "produce artifact metadata",
                "tier": "low",
                "model": "claude-haiku-4.5",
                "provider": "Claude Code",
                "provider_id": "claude-code",
                "depends_on": [],
                "consumes": "plan-outline",
                "produces": ["typed-artifact"],
                "is_coordinator": "true",
            }
        ],
        "strategy": "parallel",
    }

    plan = planner._build_plan(parsed, "fallback task")
    assert plan.topology == "star"
    assert plan.subtasks[0].consumes == ["plan-outline"]
    assert plan.subtasks[0].produces == ["typed-artifact"]
    assert plan.subtasks[0].is_coordinator is True
    assert plan.subtasks[0].model == "claude-haiku-4.5"
    assert plan.subtasks[0].provider == "Claude Code"
    assert plan.subtasks[0].provider_id == "claude-code"

    serialized = planner.plan_to_dict(plan)
    assert serialized["topology"] == "star"
    assert serialized["max_rounds"] == 3
    assert serialized["subtasks"][0]["stable_id"] == "phase00-plan01-task01"
    assert serialized["subtasks"][0]["model"] == "claude-haiku-4.5"
    assert serialized["subtasks"][0]["provider"] == "Claude Code"
    assert serialized["subtasks"][0]["provider_id"] == "claude-code"
    assert serialized["subtasks"][0]["consumes"] == ["plan-outline"]
    assert serialized["subtasks"][0]["produces"] == ["typed-artifact"]
    assert serialized["subtasks"][0]["is_coordinator"] is True


def test_local_contradiction_parse_error() -> None:
    planner = _planner()

    with pytest.raises(PlannerParseError, match="TOPO-11-001"):
        planner._build_plan(
            {
                "subtasks": [
                    {
                        "id": 1,
                        "description": "conflicting metadata",
                        "tier": "low",
                        "model": "low",
                        "depends_on": [],
                        "consumes": ["artifact-a"],
                        "produces": ["artifact-a"],
                    }
                ],
                "strategy": "parallel",
            },
            "fallback task",
        )


# ---------------------------------------------------------------------------
# From test_planner_coordinator_validation.py
# ---------------------------------------------------------------------------

from shared.planner import ExecutionPlan, Subtask


def _planner_coord() -> Planner:
    tempdir = tempfile.TemporaryDirectory()
    db_path = Path(tempdir.name) / "planner.db"
    planner = Planner(
        TGsConfig(db_path=db_path),
        DummyBackend(),
        Database(db_path=db_path),
    )
    planner._phase13_tempdir = tempdir
    return planner


def test_single_coordinator_validation() -> None:
    planner = _planner_coord()

    plan = planner._build_plan(
        {
            "analysis": "coordinator validation",
            "subtasks": [
                {
                    "id": 1,
                    "description": "inspect prior artifacts",
                    "tier": "low",
                    "depends_on": [],
                    "is_coordinator": True,
                },
                {
                    "id": 2,
                    "description": "run worker task",
                    "tier": "low",
                    "depends_on": [1],
                },
            ],
            "strategy": "dag",
        },
        "fallback task",
    )

    assert plan.subtasks[0].is_coordinator is True


def test_duplicate_coordinators_in_wave_rejected() -> None:
    planner = _planner_coord()

    with pytest.raises(PlannerParseError, match="D-01/D-02"):
        planner._build_plan(
            {
                "analysis": "coordinator validation",
                "subtasks": [
                    {
                        "id": 1,
                        "description": "first coordinator",
                        "tier": "low",
                        "depends_on": [],
                        "is_coordinator": True,
                    },
                    {
                        "id": 2,
                        "description": "second coordinator",
                        "tier": "low",
                        "depends_on": [],
                        "is_coordinator": True,
                    },
                ],
                "strategy": "parallel",
            },
            "fallback task",
        )


# ---------------------------------------------------------------------------
# From test_planner_stable_ids.py
# ---------------------------------------------------------------------------


def _planner_stable() -> Planner:
    tempdir = tempfile.TemporaryDirectory()
    db_path = Path(tempdir.name) / "planner.db"
    planner = Planner(
        TGsConfig(db_path=db_path),
        DummyBackend(),
        Database(db_path=db_path),
    )
    planner._phase32_tempdir = tempdir
    return planner


def test_stable_ids_deterministic() -> None:
    planner = _planner_stable()
    parsed = {
        "phase_number": "32",
        "plan_number": "1",
        "analysis": "stable ids",
        "subtasks": [
            {"id": 1, "description": "define schema", "tier": "low", "model": "low"},
            {
                "id": 2,
                "description": "serialize fields",
                "tier": "medium",
                "model": "medium",
                "depends_on": [1],
            },
        ],
        "strategy": "dag",
    }

    first = planner._build_plan(parsed, "phase 32")
    second = planner._build_plan(parsed, "phase 32")

    assert [st.stable_id for st in first.subtasks] == [
        "phase32-plan01-task01",
        "phase32-plan01-task02",
    ]
    assert [st.stable_id for st in first.subtasks] == [
        st.stable_id for st in second.subtasks
    ]


def test_plan_to_dict_includes_topology_and_max_rounds() -> None:
    plan = ExecutionPlan(
        analysis="serialize",
        subtasks=[
            Subtask(
                id=1,
                stable_id="phase32-plan01-task01",
                description="define schema",
                tier="low",
                model="low",
            )
        ],
        waves=[[1]],
        total_agents=1,
        strategy="parallel",
    )

    serialized = Planner.plan_to_dict(plan)

    assert serialized["topology"] == "dag"
    assert serialized["max_rounds"] == 3
    assert serialized["subtasks"][0]["stable_id"] == "phase32-plan01-task01"
