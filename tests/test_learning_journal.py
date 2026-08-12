"""Durability tests for the append-only learning journal.

The property under test is the one that motivated the module: a corrupt
``cache.db`` must cost a rebuild, not the accumulated learning. Nine images were
quarantined on the reference install in five weeks, and each quarantine reset
``model_quality_events``, ``review_tier_bias`` and every other learning table to
zero with no way back.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import pytest

from shared import learning_journal, model_quality as mq, review_learning as rl
from shared.db import Database


@pytest.fixture
def journal_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the journal at a scratch dir — never the operator's real one."""
    root = tmp_path / "journal"
    monkeypatch.setattr(learning_journal, "JOURNAL_ROOT", root)
    return root


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(db_path=tmp_path / "cache.db")
    yield database
    database.close()


def _seed(database: Database, n: int = 6) -> None:
    for i in range(n):
        mq.record_verify_gate_score(
            database,
            model="claude-opus-4-6",
            effort=None,
            score_0_10=10.0,
            role="Implementer",
            run_id=f"run-{i}",
            spawn_id=str(i),
            tier="high",
            profile_key=".py|mid|dense",
        )
        rl.record_review_tier_outcome(
            database,
            profile_key=".py|low|flat",
            dimension="security",
            tier="low",
            findings_high=1,
            findings_total=2,
            kept_by_synthesis=True,
            run_id=f"run-{i}",
            spawn_id=str(i),
        )


def _counts(database: Database) -> tuple[int, int, int]:
    with database.conn() as conn:
        quality = conn.execute("SELECT COUNT(*) FROM model_quality_events").fetchone()[0]
        rows = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(sample_count), 0) FROM review_tier_bias"
        ).fetchone()
    return quality, rows[0], rows[1]


def test_events_are_journaled_before_the_db_write(journal_root, db) -> None:
    _seed(db, n=2)
    stats = learning_journal.stats()
    assert stats["by_kind"]["model_quality"] == 2
    assert stats["by_kind"]["review_tier_outcome"] == 2


def test_journal_survives_a_truncated_trailing_line(journal_root, db) -> None:
    """A crash mid-append must not make the whole journal unreadable."""
    _seed(db, n=2)
    shard = next(iter(sorted(journal_root.glob("*.jsonl"))))
    with shard.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "model_quality", "ts": 1.0, "mod')
    assert len(list(learning_journal.iter_events())) == 4


def test_replay_into_a_live_db_is_idempotent(journal_root, db) -> None:
    """The warm-path executor retries a terminal report that failed partway.

    Before the event_id key, every row the failed attempt had already written was
    counted a second time on that retry.
    """
    _seed(db)
    before = _counts(db)
    db.replay_learning_journal(rebuild=False)
    db.replay_learning_journal(rebuild=False)
    assert _counts(db) == before


def test_incremental_replay_skips_the_ema_kinds(journal_root, db) -> None:
    """An EMA is order-dependent; re-applying one twice moves the average twice."""
    _seed(db)
    counts = learning_journal.replay(db, rebuild=False)
    assert counts.get("skipped_ema") == 6
    assert "review_tier_outcome" not in counts


def test_distinct_unaddressable_events_are_not_deduplicated(journal_root, db) -> None:
    """Two real observations must never collapse into one.

    An event with no run/spawn/task id is not addressable, so "the same event
    twice" is not a meaningful statement about it — deduping on content would
    discard genuine data rather than prevent a double count.
    """
    for _ in range(2):
        mq.record_findings_score(
            db,
            model="claude-opus",
            effort="high",
            dimension="security",
            findings_high=1,
            findings_total=1,
            kept_by_synthesis=True,
        )
    with db.conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_quality_events").fetchone()[0] == 2


def test_quality_rows_carry_the_join_axes(journal_root, db) -> None:
    """tier + profile_key are what make "which model, at which tier, on which
    shape of file" answerable; the ledger previously stored neither."""
    _seed(db, n=1)
    with db.conn() as conn:
        row = conn.execute(
            "SELECT tier, profile_key, dimension, spawn_id FROM model_quality_events"
        ).fetchone()
    assert row == ("high", ".py|mid|dense", "implementer", "0")


def test_quarantine_and_recreate_is_rebuilt_from_the_journal(
    journal_root, tmp_path: Path
) -> None:
    """The end-to-end guarantee: an unsalvageable image costs a rebuild, not data.

    Salvage and the backup restore are both disabled here so the worst case runs:
    quarantine the file, recreate it empty, and rebuild from the journal alone.
    """
    db_path = tmp_path / "cache.db"
    database = Database(db_path=db_path)
    _seed(database)
    before = _counts(database)
    assert before[0] == 6 and before[2] == 6
    database.close()

    for backup in glob.glob(str(db_path) + ".bak.*"):
        Path(backup).unlink()
    with open(db_path, "r+b") as fh:
        fh.seek(4096)
        fh.write(b"\xde\xad\xbe\xef" * 512)

    original = Database._salvage_db
    try:
        Database._salvage_db = lambda self: False  # type: ignore[method-assign]
        recovered = Database(db_path=db_path)
    finally:
        Database._salvage_db = original  # type: ignore[method-assign]

    assert glob.glob(str(db_path) + ".corrupt.*"), "expected a quarantined image"
    assert _counts(recovered) == before
    recovered.close()


def test_salvage_recovers_in_place_without_claiming_a_quarantine(
    journal_root, tmp_path: Path
) -> None:
    """A successful salvage must not leave a `.corrupt.*` file.

    That name means "quarantined, data not recovered" to `threnody db check`, the
    status snapshot and _corruption_forensics — reporting it after a clean
    salvage would claim a data loss that did not happen.
    """
    db_path = tmp_path / "cache.db"
    database = Database(db_path=db_path)
    _seed(database)
    before = _counts(database)
    database.close()

    with open(db_path, "r+b") as fh:
        fh.seek(4096)
        fh.write(b"\xde\xad\xbe\xef" * 512)

    recovered = Database(db_path=db_path)
    if recovered._salvaged_this_session:
        assert not glob.glob(str(db_path) + ".corrupt.*")
        assert glob.glob(str(db_path) + ".presalvage.*")
        assert _counts(recovered) == before
    recovered.close()


def test_rebuild_preserves_an_unadjudicated_verdict(journal_root, tmp_path: Path) -> None:
    """`kept_by_synthesis=None` must survive a journal rebuild.

    The replay handler coerced it with ``bool(...)``, which turns "no adjudicator
    judged this" into "synthesis rejected this" — inverting the observation and
    moving the EMA the opposite way from the live write on every rebuilt run.
    """
    db_path = tmp_path / "cache.db"
    live = Database(db_path=db_path)
    for i in range(4):
        rl.record_review_tier_outcome(
            live,
            profile_key=".py|low|flat",
            dimension="security",
            tier="low",
            findings_high=1,
            findings_total=1,
            kept_by_synthesis=None,
            run_id=f"run-{i}",
            spawn_id=str(i),
        )
    with live.conn() as conn:
        expected = conn.execute(
            "SELECT escalate_ema, sample_count FROM review_tier_bias"
        ).fetchone()
    assert expected[0] > 0.0, "an unadjudicated high finding should move the EMA"
    live.close()

    rebuilt = Database(db_path=tmp_path / "rebuilt.db")
    rebuilt.replay_learning_journal(rebuild=True)
    with rebuilt.conn() as conn:
        actual = conn.execute(
            "SELECT escalate_ema, sample_count FROM review_tier_bias"
        ).fetchone()
    rebuilt.close()
    assert actual == expected


def test_unknown_event_kind_is_counted_not_fatal(journal_root, db) -> None:
    """An older install must be able to read a newer install's journal."""
    learning_journal.append("some_future_kind", {"run_id": "r1", "x": 1})
    counts = learning_journal.replay(db, rebuild=False)
    assert counts.get("unknown") == 1


def test_append_never_raises_when_the_journal_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Journaling is best-effort: it must never break the caller's real work."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(learning_journal, "JOURNAL_ROOT", blocker / "journal")
    eid = learning_journal.append("model_quality", {"run_id": "r", "model": "m"})
    assert isinstance(eid, str) and eid


def test_stats_is_empty_safe(journal_root) -> None:
    stats = learning_journal.stats()
    assert stats["events"] == 0
    assert stats["by_kind"] == {}
    assert stats["newest_ts"] is None


def test_review_quality_rows_carry_tier_and_profile_key(
    journal_root, db, tmp_path: Path
) -> None:
    """The review path must populate the join axes, not just the write path.

    `tier` and `profile_key` were added to the ledger so that "which model, at
    which tier, on which shape of file" becomes answerable — review_tier_bias
    holds the profile with no model, and the quality ledger held the model with
    no profile, so the two could never be joined. Threading the kwargs through
    `model_quality` is not enough: the review call sites in `host_learning` have
    to pass them, and they silently did not, leaving every review row NULL on
    both columns.
    """
    from shared.config import TGsConfig
    from shared.host_learning import _record_review_outcome

    target = tmp_path / "auth_token.py"
    target.write_text(
        "import subprocess\n"
        "def run(c):\n"
        "    return subprocess.run(c, shell=True)\n"
        + "\n".join(f"def f{i}(x):\n    return x" for i in range(60)),
        encoding="utf-8",
    )

    _record_review_outcome(
        db,
        {
            "target_file": str(target),
            "dimension": "security",
            "tier": "high",
            "spawn_id": "7",
            "model": "opus",
            "effort": None,
            "findings_total": 1,
            "findings_high": 1,
            "kept_by_synthesis": True,
            "categories": {"security/command-injection": {
                "findings_total": 1, "findings_high": 1, "kept": True,
            }},
            "findings": None,
            "run_id": "swarm-axes",
            "task_hash": "abc123",
        },
        TGsConfig(),
    )

    with db.conn() as conn:
        rows = conn.execute(
            "SELECT source, model, tier, profile_key, spawn_id "
            "FROM model_quality_events ORDER BY source"
        ).fetchall()
    assert rows, "review path wrote no ledger rows"
    for source, model, tier, profile_key, spawn_id in rows:
        assert model == "opus", source
        assert tier == "high", f"{source} row lost the tier"
        assert profile_key and profile_key.startswith(".py|"), f"{source}: {profile_key}"
        assert spawn_id == "7", source
    # The objective source is the one routing bias reads — it must be present.
    assert "static_recall" in {r[0] for r in rows}
