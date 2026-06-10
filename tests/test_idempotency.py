"""Tests for plan 01 idempotency + attempt keys."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from shared.db import Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "test.db"
    d = Database(db_path)
    yield d


# ---------------------------------------------------------------------------
# _ensure_idempotency_schema — table presence
# ---------------------------------------------------------------------------

def test_file_writes_table_created(db):
    with db.conn() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "file_writes" in tables
    assert "idempotency_attempts" in tables


def test_idempotency_columns_on_swarm_events(db):
    with db.conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(swarm_events)").fetchall()}
    assert "idempotency_key" in cols
    assert "attempt" in cols
    assert "first_seen_at" in cols
    assert "last_attempt_at" in cols


def test_idempotency_columns_on_approval_queue(db):
    with db.conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(approval_queue)").fetchall()}
    assert "idempotency_key" in cols
    assert "attempt" in cols


def test_idempotency_columns_on_coordinator_round_checkpoints(db):
    with db.conn() as conn:
        cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(coordinator_round_checkpoints)"
            ).fetchall()
        }
    assert "idempotency_key" in cols
    assert "attempt" in cols


# ---------------------------------------------------------------------------
# claim_attempt
# ---------------------------------------------------------------------------

def test_claim_attempt_first_call_returns_zero_not_completed(db):
    attempt_n, already_done = db.claim_attempt("test_scope", "key-001")
    assert attempt_n == 0
    assert already_done is False


def test_claim_attempt_increments_on_repeated_calls(db):
    db.claim_attempt("test_scope", "key-002")
    attempt_n, already_done = db.claim_attempt("test_scope", "key-002")
    assert attempt_n == 1
    assert already_done is False


def test_claim_attempt_returns_already_completed_after_record_file_write(db):
    db.record_file_write("test_scope", "key-003", "/tmp/foo.py", 10)
    attempt_n, already_done = db.claim_attempt("test_scope", "key-003")
    assert already_done is True
    assert attempt_n == 0


def test_claim_attempt_different_scopes_independent(db):
    _, done_a = db.claim_attempt("scope_a", "shared-key")
    _, done_b = db.claim_attempt("scope_b", "shared-key")
    assert done_a is False
    assert done_b is False


# ---------------------------------------------------------------------------
# record_file_write / get_file_write
# ---------------------------------------------------------------------------

def test_record_and_get_file_write(db):
    db.record_file_write("fw_scope", "key-100", "/tmp/out.py", 42)
    result = db.get_file_write("fw_scope", "key-100")
    assert result is not None
    assert result["target_path"] == "/tmp/out.py"
    assert result["lines_written"] == 42
    assert isinstance(result["completed_at"], float)


def test_get_file_write_returns_none_for_missing(db):
    result = db.get_file_write("fw_scope", "nonexistent-key")
    assert result is None


def test_record_file_write_idempotent_on_duplicate(db):
    db.record_file_write("fw_scope", "key-101", "/tmp/a.py", 5)
    db.record_file_write("fw_scope", "key-101", "/tmp/a.py", 99)  # duplicate — ignored
    result = db.get_file_write("fw_scope", "key-101")
    assert result is not None
    assert result["lines_written"] == 5  # first write wins


def test_record_file_write_without_lines(db):
    db.record_file_write("fw_scope", "key-102", "/tmp/b.py")
    result = db.get_file_write("fw_scope", "key-102")
    assert result is not None
    assert result["lines_written"] is None


# ---------------------------------------------------------------------------
# Replay: same key → cached result, no re-write
# ---------------------------------------------------------------------------

def test_write_file_idempotency_via_db_helpers(tmp_path, db):
    """Simulate the idempotency contract: second write with same key is a no-op."""
    target = tmp_path / "output.py"
    idem_key = "plan01-replay-test"

    # First write
    target.write_text("content v1")
    db.record_file_write("file_writes", idem_key, str(target), 1)

    # Simulate second write attempt: get_file_write returns cached result
    cached = db.get_file_write("file_writes", idem_key)
    assert cached is not None
    assert cached["target_path"] == str(target)

    # File should still have v1 (caller would skip the write on cache hit)
    assert target.read_text() == "content v1"


def test_attempt_count_reflects_retries(db):
    """claim_attempt tracks how many times we've tried (0-based)."""
    key = "retry-key-001"
    scope = "orchestrator"

    n0, _ = db.claim_attempt(scope, key)
    n1, _ = db.claim_attempt(scope, key)
    n2, _ = db.claim_attempt(scope, key)

    assert n0 == 0
    assert n1 == 1
    assert n2 == 2
