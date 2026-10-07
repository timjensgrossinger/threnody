"""Effort/model selection and reporting must be visible and honest at all times.

Covers the reporting-reliability fixes: a rotating file log (failures used to be
logged at DEBUG into an invisible stderr), routing_outcomes recording which model
and effort actually ran, planned values never passing as reported ones, a lost
hook capture being reported as lost, the warm-path retry finalizing with full
context, and inspect_status surfacing what was never reported.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

import pytest

import mcp_server
from shared import host_learning as HL
from shared import logging_setup, run_log
from shared import outcomes as O
from shared.config import HostNativeConfig, TGsConfig
from shared.db import Database
from shared.status import _load_reporting_summary, build_status_snapshot


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "reporting.db")


# ---------------------------------------------------------------------------
# (a) file logging
# ---------------------------------------------------------------------------


@pytest.fixture()
def restore_logging():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_levels = {h: h.level for h in saved_handlers}
    saved_named = {name: logging.getLogger(name).level for name in ("shared", "threnody-test")}
    yield
    logging_setup.remove_file_logging()
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
    for handler, level in saved_levels.items():
        handler.setLevel(level)
    for name, level in saved_named.items():
        logging.getLogger(name).setLevel(level)


def test_file_logging_writes_info_to_threnody_log_dir(tmp_path, monkeypatch, restore_logging):
    monkeypatch.setenv(logging_setup.LOG_DIR_ENV, str(tmp_path / "logs"))
    monkeypatch.delenv(logging_setup.LOG_LEVEL_ENV, raising=False)
    stderr = logging.StreamHandler()
    logging.getLogger().addHandler(stderr)

    path = logging_setup.configure_file_logging("test", logger_names=("threnody-test",))

    assert path == tmp_path / "logs" / "threnody.log"
    logging.getLogger("shared.some_module").info("route receipt persisted")
    logging.getLogger("threnody-test").warning("caller warning")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = path.read_text(encoding="utf-8")
    assert "route receipt persisted" in text
    assert "caller warning" in text
    assert "[test]" in text
    # stderr is pinned to WARNING so the new INFO records never reach the host.
    assert stderr.level == logging.WARNING
    # Idempotent: a second call adds no second handler.
    again = logging_setup.configure_file_logging("test")
    assert again == path
    marked = [h for h in logging.getLogger().handlers if getattr(h, "_threnody_file_log", False)]
    assert len(marked) == 1
    rotating = marked[0]
    assert rotating.maxBytes == 1_000_000 and rotating.backupCount == 5


def test_file_logging_honours_level_env(tmp_path, monkeypatch, restore_logging):
    monkeypatch.setenv(logging_setup.LOG_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "debug")
    path = logging_setup.configure_file_logging("test")
    assert path is not None
    marked = [h for h in logging.getLogger().handlers if getattr(h, "_threnody_file_log", False)]
    assert marked[0].level == logging.DEBUG
    assert logging.getLogger("shared").level == logging.DEBUG


def test_file_logging_never_raises_on_unwritable_dir(tmp_path, monkeypatch, restore_logging):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(logging_setup.LOG_DIR_ENV, str(blocker / "logs"))
    assert logging_setup.configure_file_logging("test") is None
    assert not any(getattr(h, "_threnody_file_log", False) for h in logging.getLogger().handlers)


def test_file_logging_keeps_stderr_for_a_process_without_handlers(tmp_path, monkeypatch, restore_logging):
    # A hook process has no root handler; its warnings reached stderr through
    # logging.lastResort, which stops firing once the file handler exists.
    monkeypatch.setenv(logging_setup.LOG_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    assert logging_setup.configure_file_logging("hook") is not None
    stderr = [h for h in logging.getLogger().handlers if getattr(h, "_threnody_stderr_log", False)]
    assert len(stderr) == 1 and stderr[0].level == logging.WARNING
    logging_setup.remove_file_logging()
    assert logging.getLogger().handlers == []


def test_log_file_path_defaults_under_install_logs(monkeypatch):
    monkeypatch.delenv(logging_setup.LOG_DIR_ENV, raising=False)
    path = logging_setup.log_file_path()
    assert path.name == "threnody.log"
    assert path.parent.name == "logs"
    assert (path.parent.parent / "shared" / "logging_setup.py").exists()


# ---------------------------------------------------------------------------
# (b) routing_outcomes.model_used / effort_used / model_source
# ---------------------------------------------------------------------------


def test_migration_adds_model_used_columns_to_old_schema(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE routing_outcomes (
            task_id TEXT PRIMARY KEY,
            current_outcome TEXT NOT NULL,
            previous_outcome TEXT,
            recorded_at REAL NOT NULL,
            tier TEXT,
            model TEXT,
            provider_name TEXT,
            complexity_score REAL,
            telemetry_id INTEGER,
            last_modified_by TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO routing_outcomes (task_id, current_outcome, recorded_at, created_at) "
        "VALUES ('route-old', 'accepted', 1.0, 1.0)"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    with db.conn() as c:
        cols = {row[1] for row in c.execute("PRAGMA table_info(routing_outcomes)").fetchall()}
        old = c.execute(
            "SELECT current_outcome, model_used, model_source FROM routing_outcomes "
            "WHERE task_id = 'route-old'"
        ).fetchone()
    assert {"model_used", "effort_used", "model_source"} <= cols
    assert tuple(old) == ("accepted", None, None)


def _outcome_row(db: Database, task_id: str) -> dict:
    with db.conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT tier, model, routed_tier, model_used, effort_used, model_source "
            "FROM routing_outcomes WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return dict(row)


def test_record_outcome_stores_concrete_reported_model_and_effort(db):
    task_id = O.route_task_id("harden the parser")
    O.persist_route_telemetry(db, task_id=task_id, tier="low", complexity_score=0.3, model="haiku")
    O.record_outcome(db, task_id, "accepted", actual_model="opus", actual_effort="HIGH")
    row = _outcome_row(db, task_id)
    assert row["model_used"] == "claude-opus-5-5"  # alias resolved, never stored raw
    assert row["effort_used"] == "high"
    assert row["model_source"] == "reported"
    # The routed context is kept beside it, not overwritten.
    assert row["tier"] == "low" and row["model"] == "haiku"


def test_record_outcome_rejects_unknown_effort(db):
    with pytest.raises(ValueError, match="actual_effort"):
        O.record_outcome(db, O.route_task_id("t"), "accepted", actual_effort="turbo")


def test_first_insert_stores_routed_tier_and_model(db, monkeypatch):
    task_id = O.route_task_id("tidy the helper")
    O.persist_route_telemetry(
        db, task_id=task_id, tier="medium", complexity_score=0.5, model="claude-sonnet-5-5"
    )
    # Pin the INSERT itself: with the follow-up refresh disabled the row must
    # still carry the routed context (it used to be inserted as NULL).
    real_conn = db.conn

    class _SkipRefresh:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._conn = self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def execute(self, sql, params=()):
            if sql.lstrip().startswith("UPDATE routing_outcomes") and "model = COALESCE" in sql:
                return None
            return self._conn.execute(sql, params)

    monkeypatch.setattr(db, "conn", lambda: _SkipRefresh(real_conn()))
    O.record_outcome(db, task_id, "accepted")
    monkeypatch.setattr(db, "conn", real_conn)
    row = _outcome_row(db, task_id)
    assert row["tier"] == "medium"
    assert row["model"] == "claude-sonnet-5-5"
    assert row["routed_tier"] == "medium"
    assert row["model_used"] == "claude-sonnet-5-5"
    assert row["model_source"] == "routed"
    assert row["effort_used"] is None  # a routed effort is never recorded as used


def test_routed_alias_is_stored_concrete_in_model_used(db):
    task_id = O.route_task_id("routed alias")
    O.persist_route_telemetry(db, task_id=task_id, tier="high", complexity_score=0.8, model="opus")
    O.record_outcome(db, task_id, "accepted")
    row = _outcome_row(db, task_id)
    assert row["model"] == "opus"  # the routed value, verbatim
    assert (row["model_used"], row["model_source"]) == ("claude-opus-5-5", "routed")


def test_report_upgrades_routed_and_is_never_downgraded(db):
    task_id = O.route_task_id("t2")
    O.persist_route_telemetry(db, task_id=task_id, tier="low", complexity_score=0.2, model="haiku")
    O.record_outcome(db, task_id, "revised")
    assert _outcome_row(db, task_id)["model_source"] == "routed"
    O.record_outcome(db, task_id, "accepted", actual_model="sonnet", actual_effort="medium")
    row = _outcome_row(db, task_id)
    assert (row["model_used"], row["model_source"], row["effort_used"]) == (
        "claude-sonnet-5-5", "reported", "medium",
    )
    O.record_outcome(db, task_id, "accepted")
    row = _outcome_row(db, task_id)
    assert (row["model_used"], row["model_source"], row["effort_used"]) == (
        "claude-sonnet-5-5", "reported", "medium",
    )


def test_handle_record_outcome_accepts_actual_effort(db, tmp_path, monkeypatch):
    cfg = TGsConfig(db_path=tmp_path / "reporting.db")
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
    finalize_calls: list[dict] = []
    monkeypatch.setattr(
        mcp_server.shared_direct_edit_quality,
        "schedule_finalize",
        lambda _db, task_id, **kw: finalize_calls.append({"task_id": task_id, **kw}),
    )
    task_id = O.route_task_id("t3")
    result = mcp_server.handle_record_outcome({
        "task_id": task_id, "outcome": "accepted", "actual_model": "opus", "actual_effort": "xhigh",
    })
    assert result == {"stored": True, "task_id": task_id}
    row = _outcome_row(db, task_id)
    assert (row["model_used"], row["effort_used"], row["model_source"]) == (
        "claude-opus-5-5", "xhigh", "reported",
    )
    assert finalize_calls[0]["actual_effort"] == "xhigh"
    assert finalize_calls[0]["actual_model"] == "opus"

    bad = mcp_server.handle_record_outcome({"task_id": task_id, "outcome": "accepted", "actual_effort": "turbo"})
    assert bad["error"] == "invalid_request"
    schema = next(t for t in mcp_server.TOOLS if t["name"] == "record_outcome")
    assert schema["inputSchema"]["properties"]["actual_effort"]["enum"] == [
        "low", "medium", "high", "xhigh", "max",
    ]


def test_direct_edit_verify_gate_uses_reported_effort(db, tmp_path, monkeypatch):
    from shared import direct_edit_quality as DEQ

    captured: dict = {}

    def _fake_verify(_db, **kw):
        captured.update(kw)

    import dataclasses

    cfg = TGsConfig()
    cfg.verify_gate = dataclasses.replace(cfg.verify_gate, enabled=True)
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(db, "direct_edit_touches", lambda _tid: [str(target)])

    class _Report:
        def to_dict(self):
            return {"new_failures": [], "preexisting_failures": [], "ran_signals": ["lint"]}

    import shared.verify as verify_mod

    monkeypatch.setattr(verify_mod, "run_verify_gate", lambda *a, **k: _Report())
    monkeypatch.setattr(verify_mod, "verify_report_score", lambda rd: 10.0)
    summary: dict = {}
    DEQ._finalize_verify(
        db, "route-x", config=cfg, workspace_root=str(tmp_path), model="claude-opus-5-5",
        tier="high", role=None, kind=None, summary=summary,
        record_verify_gate_score=_fake_verify, effort="high", attribution="reported",
    )
    assert captured["effort"] == "high"
    assert captured["attribution"] == {"model_source": "reported"}


# ---------------------------------------------------------------------------
# (c) planned vs reported
# ---------------------------------------------------------------------------


def _handoff(db: Database, root: Path, run_id: str = "run-src") -> None:
    HL.register_host_run_handoff(
        db,
        run_id=run_id,
        host_spawn_waves=[{"wave": 1, "agents": [
            {"spawn_id": "s1", "task_id": "t-1", "tier": "low", "model": "haiku",
             "effort": "low", "prompt": "edit a.py", "target_files": ["a.py"]},
            {"spawn_id": "s2", "task_id": "t-2", "tier": "low", "model": "haiku",
             "effort": "low", "prompt": "edit b.py", "target_files": ["b.py"]},
        ]}],
        planned_subtasks=2,
        workspace_root=str(root),
    )


def test_enrich_marks_backfilled_values_as_planned():
    snap = {"model": "haiku", "effort": "low", "tier": "low"}
    planned = HL._enrich_agent_from_handoff(
        {"task_id": "t-1"},
        snapshots_by_task_id={"t-1": snap}, snapshots_by_spawn_id={},
        snapshots_by_wave_agent={}, wave_index=1, agent_index=0,
    )
    assert planned["model"] == "haiku"
    assert planned["model_source"] == "planned"
    assert planned["effort_source"] == "planned"

    reported = HL._enrich_agent_from_handoff(
        {"task_id": "t-1", "model": "opus", "effort": "high"},
        snapshots_by_task_id={"t-1": snap}, snapshots_by_spawn_id={},
        snapshots_by_wave_agent={}, wave_index=1, agent_index=0,
    )
    assert (reported["model"], reported["model_source"]) == ("opus", "reported")
    assert (reported["effort"], reported["effort_source"]) == ("high", "reported")


def test_wave_ingest_persists_the_source_marker(db, tmp_path):
    _handoff(db, tmp_path)
    result = HL.ingest_host_wave(
        db, run_id="run-src", wave_index=1, workspace_root=str(tmp_path),
        agents=[
            {"spawn_id": "s1", "task_id": "t-1", "success": True, "touched_files": ["a.py"]},
            {"spawn_id": "s2", "task_id": "t-2", "success": True, "touched_files": ["b.py"],
             "model": "claude-sonnet-5-5", "effort": "medium"},
        ],
    )
    assert result["model_attribution"] == {"planned": 1, "reported": 1}
    with db.conn() as conn:
        payload = conn.execute(
            "SELECT payload FROM swarm_events WHERE swarm_id = ? AND event_type = ?",
            ("run-src", "host_agent_complete"),
        ).fetchone()[0]
    agents = {a["task_id"]: a for a in json.loads(payload)["agents"]}
    assert agents["t-1"]["model_source"] == "planned"
    assert agents["t-1"]["effort_source"] == "planned"
    assert agents["t-2"]["model_source"] == "reported"
    assert agents["t-2"]["effort"] == "medium"


def test_review_ledger_keeps_planned_effort_out_of_the_effort_axis():
    outcome = HL._build_review_outcome(
        {"spawn_id": "s1", "target_file": "a.py", "role": "review-security",
         "subagent_type": "threnody-review-security", "model": "haiku", "effort": "low",
         "model_source": "planned", "effort_source": "planned"},
        {"review_meta": {"findings_total": 2, "findings_high": 1}},
        "low",
    )
    assert outcome is not None
    assert outcome["effort"] is None
    assert outcome["attribution"] == {"model_source": "planned", "planned_effort": "low"}


def test_verify_quality_rows_carry_model_source(db, tmp_path, monkeypatch):
    from shared import model_quality

    _handoff(db, tmp_path, run_id="run-vq")
    run_log.append_agent_record("run-vq", {"touched_files": [str(tmp_path / "a.py")]})
    run_log.append_agent_record(
        "run-vq", {"touched_files": [str(tmp_path / "b.py")], "model": "claude-opus-5-5", "effort": "high"}
    )
    calls: list[dict] = []
    monkeypatch.setattr(model_quality, "record_verify_gate_score", lambda _db, **kw: calls.append(kw))
    HL._record_verify_quality(
        db, "run-vq", {"new_failures": [], "preexisting_failures": [], "ran_signals": ["lint"],
                       "status": "pass", "ok": True},
        config=TGsConfig(), workspace_root=str(tmp_path),
    )
    assert calls
    by_model = {c["model"]: c for c in calls}
    assert by_model["haiku"]["attribution"]["model_source"] == "planned"
    assert by_model["haiku"]["effort"] is None
    assert by_model["haiku"]["attribution"]["planned_effort"] == "low"
    assert by_model["claude-opus-5-5"]["attribution"]["model_source"] == "reported"
    assert by_model["claude-opus-5-5"]["effort"] == "high"


# ---------------------------------------------------------------------------
# (d) capture lost vs deferred
# ---------------------------------------------------------------------------


def _hook_init(monkeypatch, tmp_path: Path) -> Database:
    cfg = TGsConfig(db_path=tmp_path / "hook.db")
    cfg.host_native = HostNativeConfig(report_mode="batch", learning_capture="hook")
    db = Database(db_path=tmp_path / "hook.db")
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
    monkeypatch.setattr(mcp_server, "effective_learning_capture", lambda _cfg, _caller: "hook")
    return db


def test_hook_capture_with_valid_pointer_is_deferred(monkeypatch, tmp_path):
    _hook_init(monkeypatch, tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    run_log.set_active_run("swarm-ok", workspace_root=str(root))
    result = mcp_server.handle_report_host_wave(
        {"run_id": "swarm-ok", "wave": 1, "workspace_root": str(root), "agents": []}
    )
    assert result["deferred"] is True
    assert "capture" not in result


def test_hook_capture_without_pointer_is_reported_lost(monkeypatch, tmp_path, caplog):
    _hook_init(monkeypatch, tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    with caplog.at_level(logging.WARNING):
        result = mcp_server.handle_report_host_wave({
            "run_id": "swarm-lost", "wave": 2, "workspace_root": str(root),
            "agents": [{"spawn_id": "1", "touched_files": ["a.py"], "model": "opus"}],
        })
    assert "deferred" not in result
    assert result["capture"] == "lost"
    assert "no valid active-run pointer" in result["capture_lost_reason"]
    assert result["pointer_restored"] is True
    assert run_log.get_active_run(str(root)) == "swarm-lost"
    # The report's own agents are salvaged into the run log.
    assert result["captured"] == 1
    assert [r.get("wave") for r in run_log.read_run_log("swarm-lost")] == [2]
    assert any("hook capture lost" in r.getMessage() for r in caplog.records)


def test_lost_capture_salvage_skips_what_the_run_log_already_holds(monkeypatch, tmp_path):
    _hook_init(monkeypatch, tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    # The hook captured a.py before the pointer lapsed mid-wave.
    run_log.append_agent_record("swarm-half", {"wave": 0, "touched_files": [str(root / "a.py")]})
    report = {
        "run_id": "swarm-half", "wave": 1, "workspace_root": str(root),
        "agents": [
            {"spawn_id": "1", "touched_files": ["a.py"]},
            {"spawn_id": "2", "touched_files": ["b.py"]},
        ],
    }
    first = mcp_server.handle_report_host_wave(report)
    assert first["capture"] == "lost"
    assert first["captured"] == 1  # only b.py was new
    run_log.clear_active_run("swarm-half")
    # A retried report adds nothing.
    again = mcp_server.handle_report_host_wave(report)
    assert again["captured"] == 0
    assert len(run_log.read_run_log("swarm-half")) == 2


def test_hook_capture_with_foreign_pointer_is_lost_and_not_stolen(monkeypatch, tmp_path):
    _hook_init(monkeypatch, tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    run_log.set_active_run("swarm-other", workspace_root=str(root))
    result = mcp_server.handle_report_host_wave(
        {"run_id": "swarm-mine", "wave": 1, "workspace_root": str(root), "agents": []}
    )
    assert result["capture"] == "lost"
    assert "swarm-other" in result["capture_lost_reason"]
    assert result["pointer_restored"] is False
    assert run_log.get_active_run(str(root)) == "swarm-other"


def test_hook_capture_uses_run_record_workspace_when_report_omits_it(monkeypatch, tmp_path):
    db = _hook_init(monkeypatch, tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    db.persist_swarm_run({"swarm_id": "swarm-rec", "status": "running", "workspace_root": str(root)})
    run_log.set_active_run("swarm-rec", workspace_root=str(root))
    result = mcp_server.handle_report_host_wave({"run_id": "swarm-rec", "wave": 1, "agents": []})
    assert result["deferred"] is True


# ---------------------------------------------------------------------------
# (e) warm-path retry passes full context
# ---------------------------------------------------------------------------


def _pending_terminal_run(run_id: str) -> None:
    run_log.append_agent_record(run_id, {"touched_files": ["a.py"]})
    meta = run_log.read_run_meta(run_id)
    meta["outcome"] = "accepted"
    run_log.write_run_meta(run_id, meta)


def test_warm_path_retry_passes_config_router_and_workspace(db, tmp_path, monkeypatch):
    from shared import eval as eval_mod

    _pending_terminal_run("swarm-retry")
    db.persist_swarm_run({"swarm_id": "swarm-retry", "status": "running", "workspace_root": str(tmp_path)})
    calls: list[dict] = []
    monkeypatch.setattr(HL, "import_run_log", lambda _db, rid, **kw: calls.append({"rid": rid, **kw}))
    cfg, router = TGsConfig(), object()
    result = eval_mod.run_warm_path_background_tasks(db, config=cfg, router=router)
    assert result["run_log_import"] == 1
    assert calls == [{
        "rid": "swarm-retry", "outcome": "accepted", "config": cfg, "router": router,
        "workspace_root": str(tmp_path),
    }]


def test_warm_path_retry_loads_config_when_none_is_given(db, monkeypatch):
    from shared import eval as eval_mod
    from shared import router as router_mod
    from shared.config import TGsConfig as ConfigCls

    _pending_terminal_run("swarm-retry2")
    loaded = TGsConfig()
    monkeypatch.setattr(ConfigCls, "from_yaml", classmethod(lambda cls, path=None: loaded))
    sentinel = object()
    monkeypatch.setattr(router_mod, "TaskRouter", lambda config: sentinel)
    calls: list[dict] = []
    monkeypatch.setattr(HL, "import_run_log", lambda _db, rid, **kw: calls.append(kw))
    eval_mod.run_warm_path_background_tasks(db)
    assert calls[0]["config"] is loaded
    assert calls[0]["router"] is sentinel
    assert calls[0]["workspace_root"] is None


# ---------------------------------------------------------------------------
# (f) inspect_status reporting section
# ---------------------------------------------------------------------------


def test_reporting_summary_surfaces_unreported_superseded_and_missing_models(db, tmp_path):
    now = time.time()
    for idx, age in enumerate((7200.0, 5400.0, 600.0)):
        db.persist_swarm_run({
            "swarm_id": f"swarm-wait-{idx}", "status": "awaiting_host_execution",
            "created_ts": now - age,
        })
    db.persist_swarm_run({"swarm_id": "swarm-old", "status": Database.SWARM_STATUS_SUPERSEDED})
    O.record_outcome(db, O.route_task_id("no telemetry"), "accepted")
    O.record_outcome(db, O.route_task_id("reported"), "accepted", actual_model="opus")

    summary = _load_reporting_summary(db, now=now)
    unreported = summary["unreported_swarms"]
    assert unreported["count"] == 2
    assert [entry["swarm_id"] for entry in unreported["newest"]] == ["swarm-wait-1", "swarm-wait-0"]
    assert unreported["newest"][0]["age_s"] == pytest.approx(5400.0, abs=1.0)
    assert summary["superseded"] == 1
    assert summary["outcomes_24h"] == 2
    assert summary["outcomes_missing_model_24h"] == 1
    assert summary["log_file"].endswith("threnody.log")

    snapshot = build_status_snapshot(TGsConfig(db_path=tmp_path / "reporting.db"), db, str(tmp_path))
    assert snapshot["reporting"]["unreported_swarms"]["count"] == 2
    assert snapshot["reporting"]["superseded"] == snapshot["swarm_runs"]["superseded"] == 1
    assert _load_reporting_summary(db, now=now, superseded=7)["superseded"] == 7


def test_reporting_summary_is_fail_soft():
    class _Broken:
        def conn(self):
            raise sqlite3.OperationalError("disk I/O error")

    summary = _load_reporting_summary(_Broken())  # type: ignore[arg-type]
    assert summary["unreported_swarms"]["count"] == 0
    assert summary["superseded"] == 0
    assert summary["outcomes_missing_model_24h"] is None
    assert summary["log_file"].endswith("threnody.log")


# ---------------------------------------------------------------------------
# superseded run revived by a later wave report
# ---------------------------------------------------------------------------


def test_superseded_run_flips_back_to_running_on_a_wave_report(db, tmp_path):
    _handoff(db, tmp_path, run_id="swarm-first")
    db.persist_swarm_run({
        "swarm_id": "swarm-first", "status": "awaiting_host_execution",
        "workspace_root": str(tmp_path),
    })
    db.persist_swarm_run({
        "swarm_id": "swarm-second", "status": "awaiting_host_execution",
        "workspace_root": str(tmp_path),
    })
    retired = db.supersede_idle_swarm_runs(new_swarm_id="swarm-second", workspace_root=str(tmp_path))
    assert retired["superseded"] == ["swarm-first"]

    # The host was actually still working on the first run and reports a wave.
    HL.ingest_host_wave(
        db, run_id="swarm-first", wave_index=1, workspace_root=str(tmp_path),
        agents=[{"spawn_id": "s1", "task_id": "t-1", "success": True, "touched_files": ["a.py"]}],
    )
    with db.conn() as conn:
        status, resume = conn.execute(
            "SELECT status, resume_status FROM swarm_runs WHERE swarm_id = ?", ("swarm-first",)
        ).fetchone()
    assert (status, resume) == ("running", "running")
