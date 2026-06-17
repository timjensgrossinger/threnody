"""Unit tests for shared/review_learning.py — profile-keyed review-tier learning."""
from __future__ import annotations

import tempfile
from pathlib import Path

from shared.db import Database
from shared.review_learning import (
    DEFAULT_MIN_SAMPLES,
    load_review_tier_bias,
    record_review_tier_outcome,
)


def _db(tmp: str) -> Database:
    return Database(Path(tmp) / "rl.db")


def test_under_reviewed_profile_escalates():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        for _ in range(DEFAULT_MIN_SAMPLES + 1):
            record_review_tier_outcome(
                db, profile_key=".py|mid|dense", dimension="performance",
                tier="low", findings_high=2, findings_total=4, kept_by_synthesis=True,
            )
        assert load_review_tier_bias(db).get((".py|mid|dense", "performance")) == 1


def test_idle_high_tier_deescalates():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        for _ in range(DEFAULT_MIN_SAMPLES + 1):
            record_review_tier_outcome(
                db, profile_key=".py|high|flat", dimension="types",
                tier="high", findings_high=0, findings_total=0, kept_by_synthesis=True,
            )
        assert load_review_tier_bias(db).get((".py|high|flat", "types")) == -1


def test_below_min_samples_no_bias():
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        record_review_tier_outcome(
            db, profile_key=".go|low|mid", dimension="logic",
            tier="low", findings_high=1, findings_total=1, kept_by_synthesis=True,
        )
        assert (".go|low|mid", "logic") not in load_review_tier_bias(db)


def test_cheap_tier_clean_does_not_escalate():
    # A low-tier agent that found nothing must not push the escalate signal.
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        for _ in range(DEFAULT_MIN_SAMPLES + 1):
            record_review_tier_outcome(
                db, profile_key=".py|low|flat", dimension="edge",
                tier="low", findings_high=0, findings_total=0, kept_by_synthesis=True,
            )
        assert (".py|low|flat", "edge") not in load_review_tier_bias(db)


def test_high_findings_not_kept_does_not_escalate():
    # Findings synthesis dropped (false positives) are not under-review evidence.
    with tempfile.TemporaryDirectory() as tmp:
        db = _db(tmp)
        for _ in range(DEFAULT_MIN_SAMPLES + 1):
            record_review_tier_outcome(
                db, profile_key=".py|mid|mid", dimension="logic",
                tier="medium", findings_high=3, findings_total=3, kept_by_synthesis=False,
            )
        assert (".py|mid|mid", "logic") not in load_review_tier_bias(db)


def test_empty_db_returns_empty_map():
    with tempfile.TemporaryDirectory() as tmp:
        assert load_review_tier_bias(_db(tmp)) == {}
