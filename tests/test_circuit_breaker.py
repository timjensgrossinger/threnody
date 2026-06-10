#!/usr/bin/env python3
"""Tests for shared/health.py — provider health state machine and circuit breaker."""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("THRENODY_TEST_MODE", "1")

from shared.db import Database
from shared.health import (
    DEGRADED,
    HEALTHY,
    PROBING,
    QUARANTINED,
    _FAILURE_THRESHOLD,
    is_available,
    record_probe_result,
    record_provider_failure,
    record_provider_success,
)


def _make_db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Database(Path(f.name))


def test_new_provider_is_available():
    db = _make_db()
    try:
        assert is_available(db, "new-provider") is True
    finally:
        db.close()


def test_first_failure_degrades():
    db = _make_db()
    try:
        record_provider_failure(db, "p1", "unknown")
        row = db.get_provider_health("p1")
        assert row is not None
        assert row["state"] == DEGRADED
        assert row["consecutive_failures"] == 1
    finally:
        db.close()


def test_threshold_failures_quarantines():
    db = _make_db()
    try:
        for _ in range(_FAILURE_THRESHOLD):
            record_provider_failure(db, "p1", "unknown")
        row = db.get_provider_health("p1")
        assert row["state"] == QUARANTINED
        assert row["quarantine_until_ts"] is not None
    finally:
        db.close()


def test_quarantined_provider_not_available():
    db = _make_db()
    try:
        for _ in range(_FAILURE_THRESHOLD):
            record_provider_failure(db, "p1", "unknown")
        assert is_available(db, "p1") is False
    finally:
        db.close()


def test_terminal_category_quarantines_immediately():
    db = _make_db()
    try:
        record_provider_failure(db, "p1", "auth_expired")
        row = db.get_provider_health("p1")
        assert row["state"] == QUARANTINED
    finally:
        db.close()


def test_cooldown_elapsed_transitions_to_probing():
    db = _make_db()
    try:
        for _ in range(_FAILURE_THRESHOLD):
            record_provider_failure(db, "p1", "unknown")
        # Manually set quarantine to past
        with db.conn() as conn:
            conn.execute(
                "UPDATE provider_health SET quarantine_until_ts = ? WHERE provider_id = ?",
                (time.time() - 1.0, "p1"),
            )
        assert is_available(db, "p1") is True
        row = db.get_provider_health("p1")
        assert row["state"] == PROBING
    finally:
        db.close()


def test_probe_success_transitions_to_healthy():
    db = _make_db()
    try:
        for _ in range(_FAILURE_THRESHOLD):
            record_provider_failure(db, "p1", "unknown")
        db.update_provider_health_state("p1", PROBING)
        record_probe_result(db, "p1", ok=True)
        row = db.get_provider_health("p1")
        assert row["state"] == HEALTHY
        assert row["consecutive_failures"] == 0
    finally:
        db.close()


def test_probe_failure_re_quarantines():
    db = _make_db()
    try:
        for _ in range(_FAILURE_THRESHOLD):
            record_provider_failure(db, "p1", "unknown")
        db.update_provider_health_state("p1", PROBING)
        record_probe_result(db, "p1", ok=False)
        row = db.get_provider_health("p1")
        assert row["state"] == QUARANTINED
        assert row["quarantine_until_ts"] is not None
    finally:
        db.close()


def test_success_resets_to_healthy():
    db = _make_db()
    try:
        record_provider_failure(db, "p1", "unknown")
        record_provider_failure(db, "p1", "unknown")
        record_provider_success(db, "p1")
        row = db.get_provider_health("p1")
        assert row["state"] == HEALTHY
        assert row["consecutive_failures"] == 0
    finally:
        db.close()


def test_exponential_cooldown_increases():
    db = _make_db()
    try:
        # First quarantine
        for _ in range(_FAILURE_THRESHOLD):
            record_provider_failure(db, "p1", "unknown")
        row1 = db.get_provider_health("p1")
        until1 = row1["quarantine_until_ts"]

        # Simulate probe fail to get second quarantine with longer cooldown
        db.update_provider_health_state("p1", PROBING)
        record_probe_result(db, "p1", ok=False)
        row2 = db.get_provider_health("p1")
        until2 = row2["quarantine_until_ts"]
        assert until2 > until1
    finally:
        db.close()


def test_iter_provider_health_returns_all():
    db = _make_db()
    try:
        record_provider_failure(db, "p1", "unknown")
        record_provider_failure(db, "p2", "auth_expired")
        rows = db.iter_provider_health()
        ids = {r["provider_id"] for r in rows}
        assert "p1" in ids
        assert "p2" in ids
    finally:
        db.close()
