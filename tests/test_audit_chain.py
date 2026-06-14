"""Tests for plan 03 HMAC-chained audit log."""
from __future__ import annotations

import hmac as _hmac
import hashlib
import json

import pytest

from shared.db import Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "test.db")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_swarm_events_has_chain_hmac(db):
    with db.conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(swarm_events)").fetchall()}
    assert "chain_hmac" in cols


def test_agent_audit_has_chain_hmac(db):
    with db.conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_audit)").fetchall()}
    assert "chain_hmac" in cols


def test_file_writes_has_chain_hmac(db):
    with db.conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(file_writes)").fetchall()}
    assert "chain_hmac" in cols


# ---------------------------------------------------------------------------
# Audit secret
# ---------------------------------------------------------------------------

def test_audit_secret_generated_on_first_call(tmp_path):
    db = Database(tmp_path / "test.db")
    secret = db._get_audit_secret()
    assert isinstance(secret, bytes)
    assert len(secret) == 32


def test_audit_secret_persisted(tmp_path):
    db = Database(tmp_path / "test.db")
    s1 = db._get_audit_secret()
    db2 = Database(tmp_path / "test.db")
    s2 = db2._get_audit_secret()
    assert s1 == s2


def test_audit_secret_file_permissions(tmp_path):
    import stat
    db = Database(tmp_path / "test.db")
    db._get_audit_secret()
    secret_path = tmp_path / "audit_secret"
    mode = oct(stat.S_IMODE(secret_path.stat().st_mode))
    assert mode == oct(0o600), f"Expected 0o600, got {mode}"


# ---------------------------------------------------------------------------
# HMAC computation
# ---------------------------------------------------------------------------

def test_compute_chain_hmac_deterministic(db):
    secret = db._get_audit_secret()
    h1 = db._compute_chain_hmac(secret, "", "payload-a")
    h2 = db._compute_chain_hmac(secret, "", "payload-a")
    assert h1 == h2


def test_compute_chain_hmac_changes_with_prev(db):
    secret = db._get_audit_secret()
    h1 = db._compute_chain_hmac(secret, "", "payload")
    h2 = db._compute_chain_hmac(secret, "prev-hash", "payload")
    assert h1 != h2


def test_compute_chain_hmac_changes_with_payload(db):
    secret = db._get_audit_secret()
    h1 = db._compute_chain_hmac(secret, "", "payload-a")
    h2 = db._compute_chain_hmac(secret, "", "payload-b")
    assert h1 != h2


def test_compute_chain_hmac_sha256_format(db):
    secret = db._get_audit_secret()
    h = db._compute_chain_hmac(secret, "", "test")
    assert len(h) == 64  # SHA-256 hex digest
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# log_swarm_event chains correctly
# ---------------------------------------------------------------------------

def test_log_swarm_event_stores_chain_hmac(db):
    db.log_swarm_event("sw-001", "test_event", {"key": "val"})
    with db.conn() as conn:
        row = conn.execute(
            "SELECT chain_hmac FROM swarm_events WHERE swarm_id='sw-001'"
        ).fetchone()
    assert row is not None
    assert len(row[0]) == 64


def test_log_swarm_event_chain_links(db):
    db.log_swarm_event("sw-002", "evt1", {"n": 1})
    db.log_swarm_event("sw-002", "evt2", {"n": 2})
    with db.conn() as conn:
        rows = conn.execute(
            "SELECT chain_hmac, payload FROM swarm_events WHERE swarm_id='sw-002' ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    h1, p1 = rows[0]
    h2, p2 = rows[1]
    assert h1 != h2
    secret = db._get_audit_secret()
    # h2 should be hmac(secret, h1 || p2)
    expected = db._compute_chain_hmac(secret, h1, p2)
    assert h2 == expected


# ---------------------------------------------------------------------------
# record_file_write chains correctly
# ---------------------------------------------------------------------------

def test_record_file_write_stores_chain_hmac(db):
    db.record_file_write("test_scope", "key-fw-001", "/tmp/a.py", 5)
    with db.conn() as conn:
        row = conn.execute(
            "SELECT chain_hmac FROM file_writes WHERE idempotency_key='key-fw-001'"
        ).fetchone()
    assert row is not None
    assert len(row[0]) == 64


# ---------------------------------------------------------------------------
# verify_audit_chain
# ---------------------------------------------------------------------------

def test_verify_intact_chain(db):
    db.log_swarm_event("sw-003", "e1", {"x": 1})
    db.log_swarm_event("sw-003", "e2", {"x": 2})
    db.log_swarm_event("sw-003", "e3", {"x": 3})
    breaks = db.verify_audit_chain("swarm_events")
    assert breaks == []


def test_verify_detects_tampered_row(db):
    db.log_swarm_event("sw-004", "e1", {"x": 1})
    db.log_swarm_event("sw-004", "e2", {"x": 2})
    # Tamper: overwrite chain_hmac of the second row with a bad value
    with db.conn() as conn:
        conn.execute(
            "UPDATE swarm_events SET chain_hmac='deadbeefdeadbeefdeadbeefdeadbeef'"
            " WHERE swarm_id='sw-004' AND event_type='e2'"
        )
    breaks = db.verify_audit_chain("swarm_events")
    assert len(breaks) >= 1
    assert any(b.get("stored_hmac", "").startswith("deadbeef") for b in breaks)


def test_verify_unknown_table_raises(db):
    with pytest.raises(ValueError, match="Unknown audit table"):
        db.verify_audit_chain("not_a_table")


def test_verify_empty_table_is_intact(db):
    breaks = db.verify_audit_chain("file_writes")
    assert breaks == []


# ---------------------------------------------------------------------------
# agent_audit_log chains correctly
# ---------------------------------------------------------------------------

def test_agent_audit_log_stores_chain_hmac(db):
    row_id = db.agent_audit_log("agent-001", "generated", {"version": 1})
    assert row_id is not None
    with db.conn() as conn:
        row = conn.execute(
            "SELECT chain_hmac FROM agent_audit WHERE id=?", (row_id,)
        ).fetchone()
    assert row is not None
    assert len(row[0]) == 64


# ---------------------------------------------------------------------------
# Agent audit query (moved from test_agent_audit_query.py)
# ---------------------------------------------------------------------------


def test_list_agent_audit_events_is_newest_first_and_filterable(
    temp_db_fixture: Database,
) -> None:
    temp_db_fixture.agent_audit_log("agent-a", "created", {"step": 1})
    second_id = temp_db_fixture.agent_audit_log("agent-b", "approved", {"step": 2})
    third_id = temp_db_fixture.agent_audit_log("agent-a", "registered", {"step": 3})

    all_events = temp_db_fixture.list_agent_audit_events(limit=2)
    filtered = temp_db_fixture.list_agent_audit_events(agent_id="agent-a", limit=10)

    assert [event["id"] for event in all_events] == [third_id, second_id]
    assert [event["event_type"] for event in filtered] == ["registered", "created"]
    assert all(event["agent_id"] == "agent-a" for event in filtered)


def test_list_agent_audit_events_clamps_limit(temp_db_fixture: Database) -> None:
    for index in range(105):
        temp_db_fixture.agent_audit_log(
            f"agent-{index}",
            "generated",
            {"index": index},
        )

    events = temp_db_fixture.list_agent_audit_events(limit=500)

    assert len(events) == 100
