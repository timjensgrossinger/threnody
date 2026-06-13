from __future__ import annotations

"""Tests for host-native multi-queen consensus: shared tally/personas, the
consensus wave builder, and the ingest → judge → learning flow."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.bandit import get_bandit_policy
from shared.config import TGsConfig
from shared.consensus import (
    QUEEN_PERSONAS,
    build_judge_prompt,
    build_queen_prompt,
    consensus_tally,
    parse_judge_decision,
    persona_id_from_spawn_id,
    select_personas,
)
from shared.db import Database
from shared.host_learning import (
    ingest_host_wave,
    inspect_host_swarm,
    record_consensus_handoff,
    register_host_run_handoff,
)
from shared.host_spawn import build_consensus_wave


def _host_native_cfg(*, host_native: bool = True, queens: int = 3, quorum: int = 2) -> TGsConfig:
    cfg = TGsConfig()
    cfg.consensus_enabled = True
    cfg.consensus_host_native_enabled = host_native
    cfg.consensus_queens = queens
    cfg.consensus_quorum = quorum
    cfg.consensus_queen_tier = "low"
    cfg.consensus_judge_tier = "low"
    cfg.consensus_judge_enabled = True
    return cfg


def _proposal(verdict: str, persona: str, *, next_work=None, amendment=None) -> dict:
    return {
        "verdict": verdict,
        "amendment": amendment,
        "next_work": next_work,
        "synthesis": {},
        "persona": persona,
    }


# ---------------------------------------------------------------------------
# Personas + prompts
# ---------------------------------------------------------------------------

def test_select_personas_clamped_and_distinct() -> None:
    personas = select_personas(3, None)
    assert len(personas) == 3
    ids = [p["id"] for p in personas]
    assert len(set(ids)) == 3
    # clamp to 2..3
    assert len(select_personas(1, None)) == 2
    assert len(select_personas(99, None)) == 3


def test_select_personas_config_override() -> None:
    cfg = TGsConfig()
    cfg.consensus_personas = ["risk-first", "speed-first"]
    personas = select_personas(3, cfg)
    assert [p["id"] for p in personas] == ["risk-first", "speed-first"]


def test_build_queen_prompt_injects_distinct_stances() -> None:
    base = "Review the work."
    prompts = {build_queen_prompt(base, p) for p in QUEEN_PERSONAS}
    # Each persona produces a distinct prompt and all contain the base task.
    assert len(prompts) == len(QUEEN_PERSONAS)
    assert all(base in p for p in prompts)


def test_persona_id_from_spawn_id() -> None:
    assert persona_id_from_spawn_id("queen-risk-first") == "risk-first"
    assert persona_id_from_spawn_id("risk-first") == "risk-first"
    assert persona_id_from_spawn_id("queen-unknown") is None
    assert persona_id_from_spawn_id(None) is None


# ---------------------------------------------------------------------------
# consensus_tally
# ---------------------------------------------------------------------------

def test_tally_degraded_when_no_valid() -> None:
    tally = consensus_tally([_proposal("fallback", "a"), _proposal("fallback", "b")], quorum=2)
    assert tally.degraded is True
    assert tally.valid_count == 0
    assert tally.winner is None


def test_tally_single_valid_no_judge() -> None:
    tally = consensus_tally([_proposal("complete", "a"), _proposal("fallback", "b")], quorum=2)
    assert tally.valid_count == 1
    assert tally.judge_needed is False
    assert tally.winner_persona == "a"


def test_tally_full_agreement() -> None:
    tally = consensus_tally(
        [_proposal("complete", "a"), _proposal("complete", "b"), _proposal("complete", "c")],
        quorum=2,
    )
    assert tally.agreement is True
    assert tally.quorum is True
    assert tally.judge_needed is False


def test_tally_quorum_hit_without_unanimity() -> None:
    # 2 of 3 share the same full decision; the third diverges → quorum, no judge.
    tally = consensus_tally(
        [
            _proposal("complete", "a", next_work=None),
            _proposal("complete", "b", next_work=None),
            _proposal("another-pass", "c", next_work={"focus": "x"}),
        ],
        quorum=2,
    )
    assert tally.quorum is True
    assert tally.judge_needed is False
    assert tally.winner_persona in {"a", "b"}


def test_tally_no_quorum_needs_judge() -> None:
    tally = consensus_tally(
        [
            _proposal("complete", "a", next_work={"focus": "A"}),
            _proposal("another-pass", "b", next_work={"focus": "B"}),
        ],
        quorum=2,
    )
    assert tally.judge_needed is True
    assert tally.winner is None


# ---------------------------------------------------------------------------
# judge parsing
# ---------------------------------------------------------------------------

def test_parse_judge_decision_valid() -> None:
    valid = [_proposal("complete", "a"), _proposal("another-pass", "b")]
    idx, used = parse_judge_decision('{"selected": 1, "reason": "x"}', valid)
    assert (idx, used) == (1, True)


def test_parse_judge_decision_garbage_falls_back_to_complete() -> None:
    valid = [_proposal("another-pass", "a"), _proposal("complete", "b")]
    idx, used = parse_judge_decision("not json", valid)
    assert idx == 1  # first 'complete'
    assert used is False


def test_build_judge_prompt_annotates_personas() -> None:
    valid = [_proposal("complete", "risk-first"), _proposal("another-pass", "speed-first")]
    prompt = build_judge_prompt(valid)
    assert "risk-first" in prompt
    assert "speed-first" in prompt


# ---------------------------------------------------------------------------
# build_consensus_wave
# ---------------------------------------------------------------------------

def test_consensus_wave_emitted_for_host_when_enabled() -> None:
    cfg = _host_native_cfg()
    wave = build_consensus_wave(
        config=cfg, caller="claude-code", task_text="do the thing", wave_index=3
    )
    assert wave is not None
    assert wave["wave_kind"] == "consensus"
    assert len(wave["agents"]) == 3
    # persona-diverse prompts, all read-only host_task, no cross-provider override.
    prompts = {a["prompt"] for a in wave["agents"]}
    assert len(prompts) == 3
    assert all(a["method"] == "host_task" for a in wave["agents"])
    assert all("delegation" not in a for a in wave["agents"])


def test_consensus_wave_none_when_host_native_disabled() -> None:
    cfg = _host_native_cfg(host_native=False)
    assert build_consensus_wave(
        config=cfg, caller="claude-code", task_text="x", wave_index=2
    ) is None


def test_consensus_wave_none_for_non_host_caller() -> None:
    cfg = _host_native_cfg()
    # aider is a delegation utility, not a host shell → no host-native consensus wave.
    assert build_consensus_wave(
        config=cfg, caller="aider", task_text="x", wave_index=2
    ) is None


def test_consensus_wave_no_cross_provider_even_with_flag_when_delegation_off() -> None:
    cfg = _host_native_cfg()
    cfg.consensus_cross_provider_enabled = True
    cfg.delegation_utilities_enabled = False
    wave = build_consensus_wave(
        config=cfg, caller="claude-code", task_text="x", wave_index=2
    )
    assert wave is not None
    # Host-native queens never carry a delegation/provider override.
    assert all("delegation" not in a and "provider_id" not in a for a in wave["agents"])


# ---------------------------------------------------------------------------
# ingest flow: quorum, judge follow-up, learning
# ---------------------------------------------------------------------------

def _setup_run(db: Database, cfg: TGsConfig, run_id: str) -> None:
    db.persist_swarm_run(
        {
            "swarm_id": run_id,
            "status": "awaiting_host_execution",
            "topology": "star",
            "resume_status": "awaiting_host_execution",
        }
    )
    register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=[
            {"wave": 1, "agents": [{"id": "w1", "caller": "claude-code", "prompt": "work"}]}
        ],
        planned_subtasks=1,
        workspace_root=None,
        project_id="proj-consensus",
        topology="star",
        task_hint="do the thing",
    )
    record_consensus_handoff(
        db, run_id, wave_index=2, personas=["correctness-first", "risk-first", "speed-first"],
        queen_tier="low",
    )


def _queen_agent(persona: str, verdict: str, next_work=None) -> dict:
    import json
    return {
        "spawn_id": f"queen-{persona}",
        "persona": persona,
        "success": True,
        "touched_files": [],
        "output_excerpt": json.dumps(
            {"verdict": verdict, "amendment": None, "next_work": next_work, "synthesis": {}}
        ),
    }


def test_ingest_consensus_quorum_resolves_without_judge(temp_db_fixture: Database) -> None:
    cfg = _host_native_cfg()
    run_id = "cons-quorum"
    _setup_run(temp_db_fixture, cfg, run_id)

    # worker wave
    ingest_host_wave(
        temp_db_fixture, run_id=run_id, wave_index=1,
        agents=[{"spawn_id": "w1", "success": True, "touched_files": [], "output_excerpt": "done"}],
        config=cfg,
    )
    # consensus wave — all complete → quorum
    resp = ingest_host_wave(
        temp_db_fixture, run_id=run_id, wave_index=2,
        agents=[
            _queen_agent("correctness-first", "complete"),
            _queen_agent("risk-first", "complete"),
            _queen_agent("speed-first", "complete"),
        ],
        config=cfg,
    )
    assert "consensus" in resp
    assert resp["consensus"]["resolved"] is True
    assert resp["consensus"]["quorum"] is True
    assert resp["consensus"]["judge_used"] is False
    assert "consensus_followup" not in resp


def test_ingest_consensus_no_quorum_requests_judge(temp_db_fixture: Database) -> None:
    cfg = _host_native_cfg()
    run_id = "cons-judge"
    _setup_run(temp_db_fixture, cfg, run_id)

    resp = ingest_host_wave(
        temp_db_fixture, run_id=run_id, wave_index=2,
        agents=[
            _queen_agent("correctness-first", "complete", next_work={"f": "A"}),
            _queen_agent("risk-first", "another-pass", next_work={"f": "B"}),
            _queen_agent("speed-first", "complete", next_work={"f": "C"}),
        ],
        config=cfg,
    )
    assert "consensus_followup" in resp
    followup = resp["consensus_followup"]
    assert followup["expects_wave"] == 3
    assert followup["host_spawn"]["wave_kind"] == "consensus_judge"

    # judge round resolves the winner
    import json
    resp2 = ingest_host_wave(
        temp_db_fixture, run_id=run_id, wave_index=3,
        agents=[{
            "spawn_id": "consensus-judge", "success": True, "touched_files": [],
            "output_excerpt": json.dumps({"selected": 1, "reason": "risk matters"}),
        }],
        config=cfg,
    )
    assert resp2["consensus"]["resolved"] is True
    assert resp2["consensus"]["judge_used"] is True


def test_ingest_consensus_learning_rewards_winner_persona(temp_db_fixture: Database) -> None:
    cfg = _host_native_cfg()
    run_id = "cons-learn"
    _setup_run(temp_db_fixture, cfg, run_id)
    router = SimpleNamespace(
        is_learning_enabled=lambda pid: True,
        learn_project_routing=lambda *a, **k: None,
        learn_time_pattern=lambda *a, **k: None,
    )
    # consensus wave (quorum) + terminal finalize in one report.
    ingest_host_wave(
        temp_db_fixture, run_id=run_id, wave_index=2,
        agents=[
            _queen_agent("correctness-first", "complete"),
            _queen_agent("risk-first", "complete"),
            _queen_agent("speed-first", "complete"),
        ],
        config=cfg, router=router, terminal=True, outcome="accepted",
    )
    persona_arms = [
        a for a in get_bandit_policy(temp_db_fixture).arm_stats()
        if ":persona:" in a["arm_id"]
    ]
    assert persona_arms, "expected a persona bandit arm to be rewarded"
    assert any(a["n_updates"] >= 1 for a in persona_arms)

    # inspect surfaces the consensus section
    snap = inspect_host_swarm(temp_db_fixture, run_id)
    assert snap is not None
    assert snap.get("consensus", {}).get("resolved") is True
