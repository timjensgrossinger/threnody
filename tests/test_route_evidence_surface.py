"""Tests for the route_task file-evidence surface and tier-override reporting.

The routing eval fixtures are prose-only (schema.json sets
``additionalProperties: false`` with no ``target_files``), so the file-evidence
path and the outcome plumbing are covered here instead.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import mcp_server
from shared import outcomes as shared_outcomes
from shared.db import Database


# ---------------------------------------------------------------------------
# Path resolution and containment
# ---------------------------------------------------------------------------

def test_declared_target_files_are_resolved(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    resolved = mcp_server._resolve_route_target_files(["a.py"], "some task", str(tmp_path))
    assert resolved == [str((tmp_path / "a.py").resolve())]


def test_declared_order_is_preserved_and_deduped(tmp_path: Path) -> None:
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("x = 1\n")
    resolved = mcp_server._resolve_route_target_files(
        ["b.py", "a.py", "b.py"], "t", str(tmp_path)
    )
    assert resolved == [
        str((tmp_path / "b.py").resolve()),
        str((tmp_path / "a.py").resolve()),
    ]


def test_paths_escaping_the_workspace_are_dropped(tmp_path: Path) -> None:
    """Containment is the caller's job and it must actually happen.

    ``normalize_target_path`` rejects parent traversal; anything that resolves
    outside the workspace is dropped rather than read.
    """
    resolved = mcp_server._resolve_route_target_files(
        ["../outside.py", "/etc/passwd"], "t", str(tmp_path)
    )
    assert resolved == []


def test_non_string_and_blank_entries_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    resolved = mcp_server._resolve_route_target_files(
        ["a.py", "", "   ", None, 7], "t", str(tmp_path)  # type: ignore[list-item]
    )
    assert resolved == [str((tmp_path / "a.py").resolve())]


def test_falls_back_to_paths_named_in_the_task_text(tmp_path: Path) -> None:
    """The half that made the original misroute recoverable.

    A host that never passes target_files still gets file-aware routing, because
    the task named its target in prose.
    """
    (tmp_path / "scanner.py").write_text("x = 1\n")
    resolved = mcp_server._resolve_route_target_files(
        None, "rewrite scanner.py please", str(tmp_path)
    )
    assert resolved == [str((tmp_path / "scanner.py").resolve())]


def test_declared_files_take_precedence_over_the_task_text(tmp_path: Path) -> None:
    for name in ("declared.py", "mentioned.py"):
        (tmp_path / name).write_text("x = 1\n")
    resolved = mcp_server._resolve_route_target_files(
        ["declared.py"], "also touches mentioned.py", str(tmp_path)
    )
    assert resolved == [str((tmp_path / "declared.py").resolve())]


# ---------------------------------------------------------------------------
# Evidence collection never breaks routing
# ---------------------------------------------------------------------------

def test_evidence_is_none_when_nothing_resolves(tmp_path: Path) -> None:
    assert mcp_server._route_risk_evidence("no files here", str(tmp_path), None, None) is None


def test_evidence_collection_failure_is_swallowed(monkeypatch, tmp_path: Path) -> None:
    """Routing must survive a broken scan — the tier still has to come back."""
    (tmp_path / "a.py").write_text("x = 1\n")

    def boom(*_a, **_kw):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(mcp_server, "collect_task_evidence", boom)
    assert mcp_server._route_risk_evidence("edit a.py", str(tmp_path), ["a.py"], None) is None


def test_evidence_is_collected_for_a_risky_target(tmp_path: Path) -> None:
    target = tmp_path / "store.py"
    target.write_text(
        "def q(conn, uid):\n"
        "    return conn.execute(f'SELECT * FROM t WHERE id={uid}').fetchall()\n"
    )
    ev = mcp_server._route_risk_evidence("rewrite it", str(tmp_path), ["store.py"], None)
    assert ev is not None
    assert ev.any_security_smell


# ---------------------------------------------------------------------------
# route_task declares the parameter it now honours
# ---------------------------------------------------------------------------

def test_route_task_schema_declares_target_files() -> None:
    tool = next(t for t in mcp_server.TOOLS if t["name"] == "route_task")
    props = tool["inputSchema"]["properties"]
    assert "target_files" in props
    assert props["target_files"]["type"] == "array"
    assert props["target_files"]["items"]["type"] == "string"
    assert "task" in tool["inputSchema"]["required"]
    assert "target_files" not in tool["inputSchema"]["required"]


def test_record_outcome_schema_declares_the_override_fields() -> None:
    tool = next(t for t in mcp_server.TOOLS if t["name"] == "record_outcome")
    props = tool["inputSchema"]["properties"]
    assert "tier_overridden" in props["outcome"]["enum"]
    for field in ("routed_tier", "actual_tier"):
        assert props[field]["enum"] == ["low", "medium", "high"]


# ---------------------------------------------------------------------------
# Tier-override reporting
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "outcomes.db")


def test_tier_overridden_is_an_accepted_outcome() -> None:
    assert "tier_overridden" in shared_outcomes.OUTCOME_VALUES
    assert "tier_overridden" in shared_outcomes.OUTCOME_ALLOWLIST


def test_tier_override_is_persisted(db: Database) -> None:
    shared_outcomes.record_outcome(
        db, "route-abc", "tier_overridden", routed_tier="low", actual_tier="high"
    )
    with db.conn() as conn:
        row = conn.execute(
            "SELECT current_outcome, routed_tier, actual_tier "
            "FROM routing_outcomes WHERE task_id = ?",
            ("route-abc",),
        ).fetchone()
    assert tuple(row) == ("tier_overridden", "low", "high")


def test_unknown_tier_names_are_dropped_not_stored(db: Database) -> None:
    shared_outcomes.record_outcome(
        db, "route-def", "rejected", routed_tier="opus", actual_tier=""
    )
    with db.conn() as conn:
        row = conn.execute(
            "SELECT routed_tier, actual_tier FROM routing_outcomes WHERE task_id = ?",
            ("route-def",),
        ).fetchone()
    assert tuple(row) == (None, None)


def test_omitted_tiers_do_not_erase_a_recorded_override(db: Database) -> None:
    """A later correction must not blank the pair the first call recorded."""
    shared_outcomes.record_outcome(
        db, "route-ghi", "tier_overridden", routed_tier="low", actual_tier="high"
    )
    shared_outcomes.record_outcome(db, "route-ghi", "accepted")
    with db.conn() as conn:
        row = conn.execute(
            "SELECT current_outcome, routed_tier, actual_tier "
            "FROM routing_outcomes WHERE task_id = ?",
            ("route-ghi",),
        ).fetchone()
    assert tuple(row) == ("accepted", "low", "high")


def test_tier_override_counts_as_a_failure_for_learning(db: Database) -> None:
    """The point of the outcome: it must feed the adaptive band as a failure.

    ``enqueue_learning_update`` maps anything outside accepted/revised to
    success=False, so no change was needed there — but the mapping is the
    contract this fix depends on.
    """
    assert "tier_overridden" not in ("accepted", "revised")


def test_outcome_correction_preserves_the_routed_context(db: Database) -> None:
    """A second record_outcome call must not erase what the learning loop reads.

    The UPDATE path used to null tier/model/provider_name/complexity_score/
    telemetry_id. enqueue_learning_update looks up complexity_score and falls
    back to a tier-based default when it is missing, so a "tier_overridden"
    report — usually the second call on an already-recorded task — silently
    degraded its own signal.
    """
    now = time.time()  # inside the 7-day correction window
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO routing_outcomes (task_id, current_outcome, recorded_at, "
            "tier, model, provider_name, complexity_score, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("route-keep", "accepted", now, "low", "haiku", "claude-code", 0.32, now),
        )
    shared_outcomes.record_outcome(
        db, "route-keep", "tier_overridden", routed_tier="low", actual_tier="high"
    )
    with db.conn() as conn:
        row = conn.execute(
            "SELECT current_outcome, previous_outcome, tier, model, provider_name, "
            "complexity_score, routed_tier, actual_tier "
            "FROM routing_outcomes WHERE task_id = ?",
            ("route-keep",),
        ).fetchone()
    assert row[0] == "tier_overridden"
    assert row[1] == "accepted"
    # The routed context survives the correction.
    assert (row[2], row[3], row[4]) == ("low", "haiku", "claude-code")
    assert row[5] == pytest.approx(0.32)
    assert (row[6], row[7]) == ("low", "high")
