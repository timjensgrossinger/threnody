"""finalize_route_task: ledger rows for route_task work done by direct edits."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared import direct_edit_quality as DEQ
from shared import outcomes as O
from shared import verify as V
from shared.config import TGsConfig
from shared.db import Database
from shared.receipts import record_run_receipt

TASK = "fix the bug in app.py"


@pytest.fixture()
def cfg(tmp_path: Path) -> TGsConfig:
    c = TGsConfig(db_path=tmp_path / "deq.db")
    return replace(c, verify_gate=replace(c.verify_gate, enabled=True),
                   model_quality=replace(c.model_quality, enabled=True))


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "deq.db")


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    return root


def _seed(db: Database, root: Path, *, touch: bool = True, task: str = TASK) -> str:
    task_id = O.route_task_id(task)
    O.persist_route_telemetry(db, task_id=task_id, tier="medium", complexity_score=0.4,
                              model="routed-model", caller="claude-code")
    record_run_receipt(db, run_id=task_id, source_tool="route_task", task=task,
                       payload={}, workspace_root=str(root))
    if touch:
        db.direct_edit_touch_record(task_id=task_id, caller="claude-code",
                                    cwd=str(root), file_path=str(root / "app.py"))
    return task_id


def _fake_verify(monkeypatch: pytest.MonkeyPatch, *, new_failures: list[str] | None = None,
                 ran: list[str] | None = None) -> list[dict]:
    calls: list[dict] = []
    report = {
        "ran_signals": ["tests"] if ran is None else ran,
        "new_failures": new_failures or [],
        "preexisting_failures": ["old"],
        "baseline_used": True,
    }

    def fake_run(gate_cfg, *, project_root, baseline=True, run_id=None, command_resolver=None):
        calls.append({"root": project_root, "run_id": run_id, "resolver": command_resolver})
        return SimpleNamespace(to_dict=lambda: report)

    monkeypatch.setattr(V, "run_verify_gate", fake_run)
    return calls


def _rows(db: Database, source: str) -> list[dict]:
    with db.conn() as conn:
        cur = conn.execute(
            "SELECT model, tier, dimension, kind, run_id, task_hash, score_0_10, sample_meta "
            "FROM model_quality_events WHERE source = ?", (source,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def test_verify_and_outcome_rows(db, cfg, project, monkeypatch):
    task_id = _seed(db, project)
    calls = _fake_verify(monkeypatch, new_failures=["f1"])
    out = DEQ.finalize_route_task(db, task_id, config=cfg, outcome="accepted",
                                  task_text=TASK, caller="claude-code")
    assert out["outcome_recorded"] and out["verify_recorded"]
    assert calls and calls[0]["run_id"] is None and calls[0]["root"] == str(project)
    (v,) = _rows(db, "verify_gate")
    assert (v["model"], v["tier"], v["run_id"], v["task_hash"]) == (
        "routed-model", "medium", task_id, task_id)
    assert v["score_0_10"] == 7.5
    assert v["dimension"] and v["dimension"] != ""
    (o,) = _rows(db, "outcome")
    assert o["score_0_10"] == 10.0 and o["run_id"] == task_id and o["tier"] == "medium"
    assert "routed" in (o["sample_meta"] or "")


def test_role_and_kind_come_from_receipt_without_task_text(db, cfg, project, monkeypatch):
    # record_outcome usually arrives after the guard was replaced, so neither the
    # task text nor the guard is available: the receipt's derived labels must be.
    from shared.roles import derive_role_from_task
    from shared.task_kinds import derive_kind_from_task

    task = "fix the XSS in the comment renderer of app.py"
    task_id = _seed(db, project, task=task)
    _fake_verify(monkeypatch)
    DEQ.finalize_route_task(db, task_id, config=cfg, outcome="accepted")
    (o,) = _rows(db, "outcome")
    assert o["dimension"] == (derive_role_from_task(task) or "general").lower()
    assert o["kind"] == (derive_kind_from_task(task) or None)
    receipt = db.get_run_receipt(task_id)["receipt"]
    assert task not in str(receipt), "receipt must keep labels, never the prose"


def test_second_call_is_idempotent(db, cfg, project, monkeypatch):
    task_id = _seed(db, project)
    calls = _fake_verify(monkeypatch)
    DEQ.finalize_route_task(db, task_id, config=cfg, outcome="accepted", task_text=TASK)
    again = DEQ.finalize_route_task(db, task_id, config=cfg, outcome="accepted", task_text=TASK)
    assert len(_rows(db, "verify_gate")) == 1
    assert len(_rows(db, "outcome")) == 1
    assert again["verify_skipped"] == "already_recorded"
    assert len(calls) == 1


def test_no_touches_records_outcome_only(db, cfg, project, monkeypatch):
    task_id = _seed(db, project, touch=False)
    calls = _fake_verify(monkeypatch)
    out = DEQ.finalize_route_task(db, task_id, config=cfg, outcome="revised", task_text=TASK)
    assert out["outcome_recorded"] and not out["verify_recorded"]
    assert not calls and not _rows(db, "verify_gate")
    assert _rows(db, "outcome")[0]["score_0_10"] == 6.0


def test_tier_overridden_writes_no_outcome_row(db, cfg, project, monkeypatch):
    task_id = _seed(db, project)
    _fake_verify(monkeypatch)
    out = DEQ.finalize_route_task(db, task_id, config=cfg, outcome="tier_overridden",
                                  actual_tier="high", caller="claude-code")
    assert not out["outcome_recorded"] and out["verify_recorded"]
    assert not _rows(db, "outcome")
    assert _rows(db, "verify_gate")[0]["tier"] == "high"


def test_attribution_reported(db, cfg, project, monkeypatch):
    task_id = _seed(db, project, touch=False)
    out = DEQ.finalize_route_task(db, task_id, config=cfg, outcome="accepted",
                                  actual_model="my-model", actual_tier="high")
    assert out["attribution"] == "reported"
    row = _rows(db, "outcome")[0]
    assert row["model"] == "my-model" and row["tier"] == "high"


def test_attribution_override(db, cfg, project, monkeypatch):
    task_id = _seed(db, project, touch=False)
    import shared.host_spawn as HS
    monkeypatch.setattr(HS, "host_native_model_for_tier", lambda c, caller, tier, registry=None: f"{caller}-{tier}")
    out = DEQ.finalize_route_task(db, task_id, config=cfg, outcome="accepted", actual_tier="high")
    assert out["attribution"] == "override"
    assert _rows(db, "outcome")[0]["model"] == "claude-code-high"


def test_attribution_routed(db, cfg, project):
    task_id = _seed(db, project, touch=False)
    out = DEQ.finalize_route_task(db, task_id, config=cfg, outcome="accepted")
    assert out["attribution"] == "routed"
    assert _rows(db, "outcome")[0]["model"] == "routed-model"


def test_non_route_id_ignored(db, cfg):
    out = DEQ.finalize_route_task(db, "swarm-123", config=cfg, outcome="accepted")
    assert out["skipped"] == "not_a_route_task"
    assert not _rows(db, "outcome")


def test_verify_disabled_writes_no_verify_row(db, cfg, project, monkeypatch):
    cfg = replace(cfg, verify_gate=replace(cfg.verify_gate, enabled=False))
    task_id = _seed(db, project)
    calls = _fake_verify(monkeypatch)
    out = DEQ.finalize_route_task(db, task_id, config=cfg, outcome="accepted")
    assert out["outcome_recorded"] and not calls and not _rows(db, "verify_gate")


def test_unscorable_report_writes_nothing(db, cfg, project, monkeypatch):
    task_id = _seed(db, project)
    _fake_verify(monkeypatch, ran=[])
    out = DEQ.finalize_route_task(db, task_id, config=cfg)
    assert out["verify_skipped"] == "unscorable" and not _rows(db, "verify_gate")


def test_missing_touched_file_is_filtered(db, cfg, project, monkeypatch):
    task_id = _seed(db, project, touch=False)
    db.direct_edit_touch_record(task_id=task_id, caller="c", cwd=str(project),
                                file_path=str(project / "gone.py"))
    calls = _fake_verify(monkeypatch)
    out = DEQ.finalize_route_task(db, task_id, config=cfg)
    assert out["verify_skipped"] == "no_touched_files" and not calls


def test_schedule_finalize_never_raises(db, cfg, monkeypatch):
    import shared.eval as E
    monkeypatch.setattr(E, "_get_warm_path_executor", lambda n: (_ for _ in ()).throw(RuntimeError("x")))
    DEQ.schedule_finalize(db, "route-abc", config=cfg)


def test_touch_record_is_unique_per_file(db):
    for _ in range(3):
        db.direct_edit_touch_record(task_id="route-1", caller="c", cwd="/x", file_path="/x/a.py")
    assert db.direct_edit_touches("route-1") == ["/x/a.py"]
