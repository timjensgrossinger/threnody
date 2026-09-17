"""Tests for tier-override detection.

`tier_overridden` is the strongest signal the router can receive — a human or a
host disagreeing with a routing decision — but it only accumulates if something
reports it. Relying on an agent to remember is the same failure mode that left
the adaptive bands at zero samples, so these cover the paths that report it
without cooperation.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

import mcp_server
from shared import host_learning as HL
from shared import outcomes as O
from shared.config import TGsConfig
from shared.db import Database
from shared.routing_hook import _emit_hook_result, _resolve_record_only


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "overrides.db")


# ---------------------------------------------------------------------------
# routed_tier is derived, never demanded
# ---------------------------------------------------------------------------

def test_routed_tier_is_backfilled_from_telemetry(db: Database) -> None:
    """A host reporting an override says what it ran, not what was recommended.

    route_task already persisted its tier via persist_route_telemetry, so
    requiring the caller to repeat it would be asking for data we hold.
    """
    task_id = O.route_task_id("tidy the helper")
    O.persist_route_telemetry(db, task_id=task_id, tier="low", complexity_score=0.22,
                              model="haiku", caller="claude-code")
    O.record_outcome(db, task_id, "tier_overridden", actual_tier="high")
    with db.conn() as conn:
        row = conn.execute(
            "SELECT routed_tier, actual_tier, tier, complexity_score "
            "FROM routing_outcomes WHERE task_id = ?", (task_id,)
        ).fetchone()
    assert tuple(row) == ("low", "high", "low", pytest.approx(0.22))


def test_an_explicit_routed_tier_beats_the_derived_one(db: Database) -> None:
    task_id = O.route_task_id("t")
    O.persist_route_telemetry(db, task_id=task_id, tier="low", complexity_score=0.2)
    O.record_outcome(db, task_id, "tier_overridden", routed_tier="medium", actual_tier="high")
    with db.conn() as conn:
        row = conn.execute(
            "SELECT routed_tier FROM routing_outcomes WHERE task_id = ?", (task_id,)
        ).fetchone()
    assert row[0] == "medium"


# ---------------------------------------------------------------------------
# Wave ingest: planned tier vs the tier actually run
# ---------------------------------------------------------------------------

def _wave(tier: str) -> list[dict]:
    return [{"wave": 1, "agents": [{
        "spawn_id": "s1", "task_id": "t-1", "tier": tier, "model": "haiku",
        "prompt": "tidy the helper", "target_files": ["a.py"],
    }]}]


def _ingest(db: Database, root: Path, reported: dict) -> dict:
    HL.register_host_run_handoff(db, run_id="run-1", host_spawn_waves=_wave("low"),
                                 planned_subtasks=1, workspace_root=str(root))
    agent = {"spawn_id": "s1", "task_id": "t-1", "status": "ok", "touched_files": ["a.py"]}
    agent.update(reported)
    return HL.ingest_host_wave(db, run_id="run-1", wave_index=1, agents=[agent],
                               workspace_root=str(root))


def test_a_wave_run_at_a_different_tier_records_an_override(db: Database, tmp_path: Path) -> None:
    result = _ingest(db, tmp_path, {"tier": "high", "model": "opus"})
    assert result.get("tier_overrides") == 1
    with db.conn() as conn:
        row = conn.execute(
            "SELECT current_outcome, routed_tier, actual_tier FROM routing_outcomes"
        ).fetchone()
    assert tuple(row) == ("tier_overridden", "low", "high")


@pytest.mark.parametrize(
    "reported",
    [
        {"tier": "low"},        # matches the plan
        {},                     # host reported no tier at all
        {"tier": ""},           # empty is not a disagreement
        {"tier": "   "},
    ],
)
def test_no_override_is_recorded_without_a_real_disagreement(
    db: Database, tmp_path: Path, reported: dict
) -> None:
    result = _ingest(db, tmp_path, reported)
    assert not result.get("tier_overrides")
    with db.conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM routing_outcomes").fetchone()[0] == 0


def test_tier_case_is_not_a_disagreement(db: Database, tmp_path: Path) -> None:
    assert not _ingest(db, tmp_path, {"tier": "LOW"}).get("tier_overrides")


# ---------------------------------------------------------------------------
# Guard denials
# ---------------------------------------------------------------------------

def test_a_denial_with_a_routed_guard_records_the_override(db: Database, tmp_path: Path) -> None:
    """Editing directly against a routed plan is a disagreement worth learning from."""
    proj = mcp_server._routing_guard_cwd(str(tmp_path))
    (Path(proj) / "a.py").write_text("x = 1\n")
    task = "tidy the helper"
    O.persist_route_telemetry(db, task_id=O.route_task_id(task), tier="low",
                              complexity_score=0.22, caller="claude-code")
    db.routing_guard_put(caller="claude-code", cwd=proj,
                         mode=mcp_server.ROUTING_GUARD_MODE_ROUTED_PLAN,
                         tier="low", task_text=task)
    result = mcp_server._validate_routing_guard(
        db, caller="claude-code", cwd=proj,
        target_file=str(Path(proj) / "a.py"), tool_name="Edit",
    )
    assert result["valid"] is False
    with db.conn() as conn:
        row = conn.execute(
            "SELECT task_id, current_outcome, routed_tier FROM routing_outcomes"
        ).fetchone()
    # routing_guards stores task_text, not an id; the join is route_task_id().
    assert tuple(row) == (O.route_task_id(task), "tier_overridden", "low")


def test_a_denial_with_no_guard_records_nothing(db: Database, tmp_path: Path) -> None:
    """Unrouted work has no routed decision to correct — do not invent one."""
    proj = mcp_server._routing_guard_cwd(str(tmp_path))
    (Path(proj) / "a.py").write_text("x = 1\n")
    result = mcp_server._validate_routing_guard(
        db, caller="claude-code", cwd=proj,
        target_file=str(Path(proj) / "a.py"), tool_name="Edit",
    )
    assert result["valid"] is False
    with db.conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM routing_outcomes").fetchone()[0] == 0


def test_recording_failure_never_breaks_validation(db: Database, monkeypatch) -> None:
    """The guard must keep answering even if the learning write fails."""
    def boom(*_a, **_kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr(mcp_server.shared_outcomes, "record_outcome", boom)
    mcp_server._record_guard_override(
        db, {"routing_guard": {"tier": "low", "task_text": "t"}, "reason": "r"}
    )  # must not raise


# ---------------------------------------------------------------------------
# Hook mode
# ---------------------------------------------------------------------------

def test_record_only_never_blocks() -> None:
    denial = {"valid": False, "reason": "no routing decision"}
    assert _emit_hook_result(denial, record_only=True) == 0
    assert _emit_hook_result(denial, record_only=False) == 2


def test_allowed_edits_pass_in_both_modes() -> None:
    ok = {"valid": True, "reason": "exempt"}
    assert _emit_hook_result(ok, record_only=True) == 0
    assert _emit_hook_result(ok, record_only=False) == 0


def _profile(tmp_path: Path, body: str):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(body)
    return TGsConfig.from_yaml(cfg).routing_policy.effective_profile("claude-code")


def test_advisory_resolves_to_record_not_off(tmp_path: Path) -> None:
    """The gap this closes: advisory used to install no hook at all."""
    profile = _profile(tmp_path, "")
    assert profile.direct_edit_hook_mode == "record"
    assert profile.direct_edit_hook_installed is True
    assert profile.direct_edit_hooks is False, "record must not imply enforcement"


def test_guarded_resolves_to_enforce(tmp_path: Path) -> None:
    profile = _profile(
        tmp_path,
        "routing_policy:\n  mode: custom\n  shells:\n    claude-code:\n      mode: guarded\n",
    )
    assert profile.direct_edit_hook_mode == "enforce"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("off", "off"),
        ('"off"', "off"),      # quoted
        ("record", "record"),
        ("enforce", "enforce"),
        ("on", "enforce"),     # YAML 1.1 boolean
        ("sometimes", "record"),  # unrecognised -> derive
    ],
)
def test_explicit_hook_mode_is_honoured(
    tmp_path: Path, caplog, value: str, expected: str
) -> None:
    """`direct_edit_hook_mode: off` must actually disable the hook.

    YAML 1.1 resolves bare `off`/`on` to booleans, so the string check alone
    silently ignored an explicit opt-out.
    """
    with caplog.at_level(logging.WARNING):
        profile = _profile(
            tmp_path,
            "routing_policy:\n  mode: custom\n  shells:\n    claude-code:\n"
            f"      direct_edit_hook_mode: {value}\n",
        )
    assert profile.direct_edit_hook_mode == expected


def test_a_shell_with_no_hook_support_resolves_to_off(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    policy = TGsConfig.from_yaml(cfg).routing_policy
    assert policy.effective_profile("junie").direct_edit_hook_mode == "off"
    assert policy.effective_profile("junie").direct_edit_hook_installed is False


def test_unresolvable_policy_falls_back_to_not_blocking(monkeypatch) -> None:
    """A hook that wrongly denies is worse than one that wrongly allows."""
    monkeypatch.setattr(
        "shared.config.TGsConfig.from_yaml",
        staticmethod(lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom"))),
    )
    assert _resolve_record_only("claude-code") is True
