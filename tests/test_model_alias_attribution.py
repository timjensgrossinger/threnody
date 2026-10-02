"""Claude Code alias -> concrete model resolution for quality-ledger attribution.

conftest scrubs ``ANTHROPIC_*`` model vars and disables the real
``~/.claude/settings.json``; tests here pass explicit ``env`` / ``settings_path``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared import learning_journal, model_quality as mq, model_registry
from shared.db import Database
from shared.model_registry import CLAUDE_ALIAS_TABLE, resolve_model_alias


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(db_path=tmp_path / "cache.db")
    yield database
    database.close()


def _rows(database: Database) -> list[tuple]:
    with database.conn() as conn:
        return conn.execute(
            "SELECT model, sample_meta, event_id FROM model_quality_events ORDER BY id"
        ).fetchall()


# ---------------------------------------------------------------------------
# resolve_model_alias
# ---------------------------------------------------------------------------


def test_alias_table_is_the_fallback() -> None:
    assert resolve_model_alias("claude-code", "sonnet", env={}, settings_path=None) == (
        CLAUDE_ALIAS_TABLE["sonnet"], "alias_table",
    )
    # Unknown provider: the bare aliases are Claude Code's vocabulary.
    assert resolve_model_alias(None, "Opus", env={}, settings_path=None)[1] == "alias_table"


def test_concrete_and_foreign_ids_pass_through() -> None:
    assert resolve_model_alias(None, "claude-sonnet-5-5", env={}, settings_path=None) == (
        "claude-sonnet-5-5", "reported",
    )
    assert resolve_model_alias(None, "gpt-6-luna", env={}, settings_path=None) == (
        "gpt-6-luna", "reported",
    )
    # Another provider's "opus" is that provider's id, not Claude's alias.
    assert resolve_model_alias("cursor", "opus", env={}, settings_path=None) == ("opus", "reported")
    assert resolve_model_alias(None, "", env={}, settings_path=None) == ("", "unresolved")


def test_per_alias_env_override_wins() -> None:
    env = {
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5-20260601",
        "ANTHROPIC_MODEL": "claude-sonnet-9",
    }
    assert resolve_model_alias(None, "sonnet", env=env, settings_path=None) == (
        "claude-sonnet-5-20260601", "env:ANTHROPIC_DEFAULT_SONNET_MODEL",
    )


def test_anthropic_model_counts_only_for_the_same_family() -> None:
    env = {"ANTHROPIC_MODEL": "claude-opus-5-1"}
    assert resolve_model_alias(None, "opus", env=env, settings_path=None) == (
        "claude-opus-5-1", "env:ANTHROPIC_MODEL",
    )
    assert resolve_model_alias(None, "haiku", env=env, settings_path=None)[1] == "alias_table"
    # An alias value carries no version and is not evidence.
    assert resolve_model_alias(None, "opus", env={"ANTHROPIC_MODEL": "opus"}, settings_path=None)[1] == "alias_table"


def test_settings_env_and_model(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "model": "claude-opus-5-2[1m]",
        "env": {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-6"},
    }), encoding="utf-8")
    assert resolve_model_alias(None, "haiku", env={}, settings_path=settings) == (
        "claude-haiku-4-6", "settings:env.ANTHROPIC_DEFAULT_HAIKU_MODEL",
    )
    assert resolve_model_alias(None, "opus", env={}, settings_path=settings) == (
        "claude-opus-5-2", "settings:model",
    )
    # Shell env outranks the settings file.
    assert resolve_model_alias(
        None, "haiku", env={"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-x"}, settings_path=settings,
    )[0] == "claude-haiku-x"


def test_bracketed_context_suffix_is_still_the_alias() -> None:
    assert resolve_model_alias(None, "sonnet[1m]", env={}, settings_path=None) == (
        CLAUDE_ALIAS_TABLE["sonnet"], "alias_table",
    )


def test_unreadable_settings_fall_back_to_table(tmp_path: Path) -> None:
    broken = tmp_path / "settings.json"
    broken.write_text("{not json", encoding="utf-8")
    assert resolve_model_alias(None, "opus", env={}, settings_path=broken)[1] == "alias_table"


# ---------------------------------------------------------------------------
# Ledger write path
# ---------------------------------------------------------------------------


def test_ledger_stores_concrete_id_and_keeps_alias_in_sample_meta(db: Database) -> None:
    mq.record_verify_gate_score(
        db, model="sonnet", effort="medium", score_0_10=10.0, run_id="r1", spawn_id="1",
    )
    (model, meta, _event_id), = _rows(db)
    assert model == CLAUDE_ALIAS_TABLE["sonnet"]
    meta = json.loads(meta)
    assert meta["model_alias"] == "sonnet"
    assert meta["model_resolution"] == "alias_table"


def test_env_override_reaches_the_ledger(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-opus-5-9")
    mq.record_verify_gate_score(db, model="opus", effort=None, score_0_10=7.5, run_id="r1")
    (model, meta, _), = _rows(db)
    assert model == "claude-opus-5-9"
    assert json.loads(meta)["model_resolution"] == "env:ANTHROPIC_DEFAULT_OPUS_MODEL"


def test_concrete_report_adds_no_alias_meta(db: Database) -> None:
    mq.record_verify_gate_score(
        db, model="claude-sonnet-5-5", effort=None, score_0_10=10.0, run_id="r1",
    )
    (model, meta, _), = _rows(db)
    assert model == "claude-sonnet-5-5"
    assert meta is None or "model_alias" not in json.loads(meta)


def test_alias_and_concrete_report_of_one_event_are_one_row(db: Database) -> None:
    common = dict(effort=None, score_0_10=10.0, run_id="r1", spawn_id="3", tier="medium")
    mq.record_verify_gate_score(db, model="sonnet", **common)
    mq.record_verify_gate_score(db, model=CLAUDE_ALIAS_TABLE["sonnet"], **common)
    assert len(_rows(db)) == 1


def test_replay_is_deterministic_after_the_alias_table_moves(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mq.record_verify_gate_score(
        db, model="sonnet", effort=None, score_0_10=10.0, run_id="r1", spawn_id="1",
    )
    (model, _meta, event_id), = _rows(db)

    # Claude Code moves the alias; a replay must restore what was graded, not
    # re-resolve against the new table.
    monkeypatch.setitem(model_registry.CLAUDE_ALIAS_TABLE, "sonnet", "claude-sonnet-6")
    rebuilt = Database(db_path=tmp_path / "rebuilt.db")
    try:
        learning_journal.replay(rebuilt)
        learning_journal.replay(rebuilt)  # idempotent
        assert [(r[0], r[2]) for r in _rows(rebuilt)] == [(model, event_id)]
    finally:
        rebuilt.close()


def test_ledger_model_id_matches_what_is_stored(db: Database) -> None:
    mq.record_verify_gate_score(db, model="haiku", effort=None, score_0_10=9.0, run_id="r1")
    (model, _, _), = _rows(db)
    assert mq.ledger_model_id("haiku") == model
    assert mq.ledger_model_id(None) == mq.MODEL_UNRESOLVED
