#!/usr/bin/env python3
"""Tests for shared/bandit.py — persistence, training, and live gating."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.bandit import (
    FEATURE_DIM,
    BanditPolicy,
    extract_task_features,
    get_bandit_policy,
    reset_bandit_policy,
    train_from_decisions,
)
from shared.db import Database

ARMS = ["low:heuristic", "medium:heuristic", "high:heuristic"]


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_bandit_policy()
    yield
    reset_bandit_policy()


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "bandit.db")
    database._init_schema(database._get_connection())
    yield database
    database.close()


def _features() -> list[float]:
    return extract_task_features("refactor the auth module in python", "proj")


# --- persistence ---


def test_arms_round_trip_through_db(db: Database) -> None:
    """Arms must survive the process — they used to live only in memory."""
    policy = BanditPolicy(db=db, mode="shadow")
    for _ in range(3):
        policy.update("medium:heuristic", _features(), 1.0)

    reloaded = BanditPolicy(db=db, mode="shadow")
    stats = {a["arm_id"]: a for a in reloaded.arm_stats()}
    assert stats["medium:heuristic"]["n_updates"] == 3


def test_update_rejects_wrong_feature_width(db: Database) -> None:
    policy = BanditPolicy(db=db, mode="shadow")
    policy.update("low:heuristic", [0.1, 0.2], 1.0)
    assert policy.arm_stats() == []


def test_stale_feature_shape_is_ignored_not_mis_scored(db: Database) -> None:
    """A row written under a different FEATURE_DIM must not be loaded."""
    db.save_bandit_arm("low:heuristic", [[1.0]], [1.0], 99)
    policy = BanditPolicy(db=db, mode="shadow")
    assert policy.arm_stats() == []


# --- training ---


def test_train_from_decisions_replays_scored_rows(db: Database) -> None:
    feats = _features()
    db.log_routing_decision(
        task_id="route-abc",
        features=feats,
        heuristic_pick="medium:heuristic",
        bandit_pick="medium:heuristic",
        chosen="medium:heuristic",
    )
    # Unscored rows are not training data.
    assert train_from_decisions(db)["trained"] == 0

    db.update_routing_decision_outcome("route-abc", outcome_score=1.0, regret=0.0)
    assert train_from_decisions(db)["trained"] == 1

    stats = {a["arm_id"]: a for a in get_bandit_policy(db=db).arm_stats()}
    assert stats["medium:heuristic"]["n_updates"] == 1


def test_training_never_replays_a_row_twice(db: Database) -> None:
    db.log_routing_decision(
        task_id="route-abc",
        features=_features(),
        heuristic_pick="low:heuristic",
        bandit_pick="low:heuristic",
        chosen="low:heuristic",
    )
    db.update_routing_decision_outcome("route-abc", outcome_score=1.0)
    assert train_from_decisions(db)["trained"] == 1
    assert train_from_decisions(db)["trained"] == 0


# --- selection gating ---


def test_shadow_mode_never_changes_the_chosen_arm(db: Database) -> None:
    policy = BanditPolicy(db=db, mode="shadow", min_updates=0)
    decision = policy.select(_features(), ARMS, "high:heuristic")
    assert decision.chosen_arm == "high:heuristic"
    assert decision.reason == "shadow"


def test_live_falls_back_while_arms_are_untrained(db: Database) -> None:
    """An untrained arm's UCB is exploration bonus only — identical per tier."""
    policy = BanditPolicy(db=db, mode="live", min_updates=5)
    decision = policy.select(_features(), ARMS, "high:heuristic")
    assert decision.chosen_arm == "high:heuristic"
    assert decision.reason == "untrained"


def test_live_selects_once_every_arm_is_trained(db: Database) -> None:
    policy = BanditPolicy(db=db, mode="live", min_updates=2)
    feats = _features()
    # Train all arms so the gate opens; reward "low" and punish the others.
    for _ in range(2):
        policy.update("low:heuristic", feats, 1.0)
        policy.update("medium:heuristic", feats, 0.0)
        policy.update("high:heuristic", feats, 0.0)

    decision = policy.select(feats, ARMS, "high:heuristic")
    assert decision.reason == "bandit"
    assert decision.chosen_arm == "low:heuristic"
    assert decision.bandit_arm == "low:heuristic"


def test_partial_training_still_blocks_live(db: Database) -> None:
    policy = BanditPolicy(db=db, mode="live", min_updates=2)
    feats = _features()
    for _ in range(2):
        policy.update("low:heuristic", feats, 1.0)
    # medium/high are still cold -> the gate stays shut.
    assert policy.select(feats, ARMS, "high:heuristic").reason == "untrained"


def test_empty_arm_list_is_safe(db: Database) -> None:
    policy = BanditPolicy(db=db, mode="live", min_updates=0)
    decision = policy.select(_features(), [], "medium:heuristic")
    assert decision.chosen_arm == "medium:heuristic"
    assert decision.reason == "no_arms"


# --- singleton ---


def test_training_does_not_demote_a_live_policy(db: Database) -> None:
    """train_from_decisions must not reconfigure the policy it borrows."""
    live = get_bandit_policy(db=db, mode="live", min_updates=3)
    assert live.mode == "live"

    db.log_routing_decision(
        task_id="route-abc",
        features=_features(),
        heuristic_pick="low:heuristic",
        bandit_pick="low:heuristic",
        chosen="low:heuristic",
    )
    db.update_routing_decision_outcome("route-abc", outcome_score=1.0)
    train_from_decisions(db)

    assert get_bandit_policy().mode == "live"
    assert get_bandit_policy()._min_updates == 3


def test_singleton_reconfigures_instead_of_freezing_first_caller(db: Database) -> None:
    """The old singleton kept whichever db/mode arrived first, silently."""
    first = get_bandit_policy(db=None, mode="shadow")
    second = get_bandit_policy(db=db, mode="live", min_updates=7)
    assert first is second
    assert second.mode == "live"
    assert second._db is db
    assert second._min_updates == 7


def test_feature_vector_width_matches_model_dim() -> None:
    assert len(_features()) == FEATURE_DIM


# --- the loop end to end ---


def test_route_then_outcome_scores_the_decision_row(db: Database) -> None:
    """classify() -> outcome must leave a row with a NON-NULL outcome_score.

    This never once happened: the decision was logged under a fresh uuid4 while
    the outcome was written against a run_id, so the two could not join and the
    bandit had no training data at all.
    """
    from shared.config import TGsConfig
    from shared.outcomes import route_task_id
    from shared.router import TaskRouter

    task = "refactor the python auth module across several files"
    router = TaskRouter(TGsConfig(), db=db)
    router.classify(task, project_path="/tmp/proj")

    with db.conn() as conn:
        total, scored = conn.execute(
            "SELECT COUNT(*), COUNT(outcome_score) FROM routing_decisions"
        ).fetchone()
    assert total == 1 and scored == 0

    # The outcome writer resolves the same stable key from the task text.
    db.update_routing_decision_outcome(route_task_id(task), outcome_score=1.0, regret=0.0)

    with db.conn() as conn:
        total, scored = conn.execute(
            "SELECT COUNT(*), COUNT(outcome_score) FROM routing_decisions"
        ).fetchone()
    assert (total, scored) == (1, 1)

    # And that scored row is now training data.
    assert train_from_decisions(db)["trained"] == 1


def test_live_bandit_cannot_undercut_the_security_floor(db: Database) -> None:
    """A learned policy may disagree with the score, never with a safety floor."""
    from shared.config import TGsConfig
    from shared.router import TaskRouter

    cfg = TGsConfig()
    cfg.routing.bandit_mode = "live"
    cfg.routing.bandit_min_updates = 1
    router = TaskRouter(cfg, db=db)

    # Train every arm so the live gate opens, with "low" the clear winner.
    policy = get_bandit_policy(db=db, mode="live", min_updates=1)
    feats = _features()
    for _ in range(3):
        policy.update("low:heuristic", feats, 1.0)
        policy.update("medium:heuristic", feats, 0.0)
        policy.update("high:heuristic", feats, 0.0)

    decision = router.classify("rotate the auth credential signing secret")
    assert decision.tier != "low", (
        f"bandit dropped security-floored work to {decision.tier}"
    )


def test_shadow_mode_classify_does_not_change_the_tier(db: Database) -> None:
    from shared.config import TGsConfig
    from shared.router import TaskRouter

    cfg = TGsConfig()
    cfg.routing.bandit_mode = "shadow"
    router = TaskRouter(cfg, db=db)
    baseline = TaskRouter(TGsConfig(), db=None).classify("add a docstring")
    assert router.classify("add a docstring").tier == baseline.tier
