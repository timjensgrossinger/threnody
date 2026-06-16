"""Tests for shared.workflow_export and the report_workflow_result handler."""
from __future__ import annotations

from pathlib import Path

import pytest

from shared.workflow_export import (
    DEFAULT_WORKFLOW_PROMOTE_THRESHOLD,
    WORKFLOW_DRAFT_KIND,
    build_workflow_doc_header,
    build_workflow_draft,
    export_workflow,
    tune_models_from_learning,
    workflow_shape_fingerprint,
)


def _plan(file_a: str = "app.py", file_b: str = "db.py") -> dict:
    return {
        "topology": "dag",
        "analysis": "review",
        "subtasks": [
            {"id": 1, "tier": "high", "subagent_type": "review-security", "read_only": True, "target_file": file_a},
            {"id": 2, "tier": "medium", "subagent_type": "review-logic", "read_only": True, "target_file": file_a},
            {"id": 3, "tier": "high", "depends_on": [1, 2], "read_only": True},
        ],
        "waves": [[1, 2], [3]],
    }


def test_fingerprint_ignores_file_paths_same_shape_collides() -> None:
    fp1 = workflow_shape_fingerprint(_plan("app.py", "db.py"))
    fp2 = workflow_shape_fingerprint(_plan("other.py", "thing.py"))
    assert fp1 == fp2  # shape identical, only file paths differ


def test_fingerprint_differs_on_shape_change() -> None:
    base = workflow_shape_fingerprint(_plan())
    altered = dict(_plan())
    altered["subtasks"] = altered["subtasks"] + [
        {"id": 4, "tier": "low", "subagent_type": "review-edge-cases", "read_only": True}
    ]
    altered["waves"] = [[1, 2, 4], [3]]
    assert workflow_shape_fingerprint(altered) != base


def test_fingerprint_requires_structure() -> None:
    with pytest.raises(ValueError):
        workflow_shape_fingerprint({"subtasks": []})


def test_build_workflow_draft_shape() -> None:
    draft = build_workflow_draft(
        name="Review App", script="export const meta = {}\n", fingerprint="abc123",
        plan_dict=_plan(), run_count=3,
    )
    assert draft["kind"] == WORKFLOW_DRAFT_KIND
    assert draft["name"] == "review-app"
    assert draft["fingerprint"] == "workflow:abc123"
    assert draft["shape"]["wave_count"] == 2
    assert draft["shape"]["agent_count"] == 3
    assert draft["run_count"] == 3


def test_export_writes_js_to_project_workflows(tmp_path: Path) -> None:
    draft = build_workflow_draft(
        name="my-flow", script="export const meta = {}\nreturn 1", fingerprint="f",
        plan_dict=_plan(), run_count=2,
    )
    res = export_workflow(draft, project_path=str(tmp_path))
    assert len(res["written"]) == 1
    written = Path(res["written"][0])
    assert written == (tmp_path / ".claude" / "workflows" / "my-flow.js").resolve()
    assert written.read_text(encoding="utf-8").endswith("\n")
    assert "export const meta" in written.read_text(encoding="utf-8")


def test_export_dry_run_writes_nothing(tmp_path: Path) -> None:
    draft = build_workflow_draft(
        name="dry", script="x", fingerprint="f", plan_dict=_plan(), run_count=2,
    )
    res = export_workflow(draft, project_path=str(tmp_path), dry_run=True)
    assert res["written"] == []
    assert res["skipped"]
    assert not (tmp_path / ".claude" / "workflows" / "dry.js").exists()


def test_export_rejects_non_workflow_draft() -> None:
    with pytest.raises(ValueError):
        export_workflow({"kind": "agent", "script": "x", "name": "n"})


def test_export_rejects_missing_script() -> None:
    with pytest.raises(ValueError):
        export_workflow({"kind": WORKFLOW_DRAFT_KIND, "name": "n"})


# ---------------------------------------------------------------------------
# report_workflow_result handler — logic via collaborator stubs
# ---------------------------------------------------------------------------


def _stub_handler_env(monkeypatch, *, snapshot, shape_counter):
    """Patch mcp_server collaborators so the handler runs without a real DB."""
    import types
    import mcp_server

    # Fake db exposing the batched flush the handler now calls once per run.
    fake_db = types.SimpleNamespace(flush_host_wave_records=lambda **kwargs: [])
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (None, fake_db, None, None, None))

    ingested: list[dict] = []

    def fake_build(db, *, run_id, agent_spec, result, project_id=None):
        ingested.append({"spec": agent_spec, "result": result})
        return {
            "task_id": agent_spec["spawn_id"],
            "pattern_hash": "h-" + str(agent_spec.get("spawn_id")),
            "eval_quality": 1.0,
            "touched_files": [],
            "resolved_project": project_id or "p",
            "pattern_payload": {},
            "telemetry_payload": {},
        }

    monkeypatch.setattr(mcp_server, "build_host_agent_record", fake_build)
    monkeypatch.setattr(mcp_server, "check_draft_ready", lambda *a, **k: False)

    def fake_memory_get(scope, key, *a, **k):
        if key.startswith("workflow_emit:"):
            if snapshot is None:
                raise KeyError("not found")
            return {"value": snapshot}
        if key.startswith("workflow_shape:"):
            return {"value": {"count": shape_counter["count"]}}
        raise KeyError("not found")

    def fake_memory_set(scope, key, value, *a, **k):
        if key.startswith("workflow_shape:"):
            shape_counter["count"] = value["count"]
        return {"key": key}

    monkeypatch.setattr(mcp_server, "memory_get", fake_memory_get)
    monkeypatch.setattr(mcp_server, "memory_set", fake_memory_set)

    enqueued: list[dict] = []

    def fake_enqueue(project_id, draft, *, db=None, **k):
        enqueued.append(draft)
        return {"id": 42, "status": "pending"}

    monkeypatch.setattr(mcp_server, "approval_queue_enqueue", fake_enqueue)
    return ingested, enqueued


def _agents() -> list[dict]:
    return [
        {"id": "1", "label": "sec", "tier": "high", "model": "opus", "result": {"summary": "ok", "success": True}},
        {"id": "2", "label": "logic", "tier": "medium", "model": "sonnet", "result": {"summary": "ok", "findings": ["x"], "success": True}},
    ]


def test_handler_ingests_telemetry(monkeypatch) -> None:
    import mcp_server

    ingested, enqueued = _stub_handler_env(monkeypatch, snapshot=None, shape_counter={"count": 0})
    res = mcp_server.handle_report_workflow_result(
        {"workflow_name": "review-app", "agents": _agents()}
    )
    assert res["agents_ingested"] == 2
    assert res["all_success"] is True
    assert len(ingested) == 2
    # No snapshot stored → no learning draft.
    assert "workflow_draft" not in res


def test_handler_below_threshold_no_enqueue(monkeypatch) -> None:
    import mcp_server

    snap = {"script": "export const meta = {}\n", "fingerprint": "fp1", "topology": "dag", "waves": [[1, 2], [3]]}
    ingested, enqueued = _stub_handler_env(monkeypatch, snapshot=snap, shape_counter={"count": 0})
    res = mcp_server.handle_report_workflow_result(
        {"workflow_name": "review-app", "agents": _agents()}
    )
    assert res["shape_runs"] == 1  # first successful run
    assert res["workflow_draft"]["enqueued"] is False
    assert enqueued == []


def test_handler_at_threshold_enqueues_draft(monkeypatch) -> None:
    import mcp_server

    snap = {"script": "export const meta = {}\n", "fingerprint": "fp1", "topology": "dag", "waves": [[1, 2], [3]]}
    # Pre-seed counter to threshold-1 so this run crosses it.
    start = {"count": DEFAULT_WORKFLOW_PROMOTE_THRESHOLD - 1}
    ingested, enqueued = _stub_handler_env(monkeypatch, snapshot=snap, shape_counter=start)
    res = mcp_server.handle_report_workflow_result(
        {"workflow_name": "review-app", "agents": _agents()}
    )
    assert res["shape_runs"] == DEFAULT_WORKFLOW_PROMOTE_THRESHOLD
    assert res["workflow_draft"]["enqueued"] is True
    assert len(enqueued) == 1
    assert enqueued[0]["kind"] == WORKFLOW_DRAFT_KIND


def test_handler_failure_blocks_learning(monkeypatch) -> None:
    import mcp_server

    snap = {"script": "x", "fingerprint": "fp1", "topology": "dag", "waves": [[1, 2], [3]]}
    ingested, enqueued = _stub_handler_env(monkeypatch, snapshot=snap, shape_counter={"count": 5})
    agents = _agents()
    agents[0]["result"]["success"] = False
    res = mcp_server.handle_report_workflow_result(
        {"workflow_name": "review-app", "agents": agents}
    )
    assert res["all_success"] is False
    assert "workflow_draft" not in res  # failed run never promotes
    assert enqueued == []


def test_handler_rejects_bad_input(monkeypatch) -> None:
    import mcp_server

    _stub_handler_env(monkeypatch, snapshot=None, shape_counter={"count": 0})
    assert "error" in mcp_server.handle_report_workflow_result({"agents": _agents()})
    assert "error" in mcp_server.handle_report_workflow_result({"workflow_name": "x", "agents": []})


# ---------------------------------------------------------------------------
# Phase C: learning-tuned, documented export
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self, stats: dict | None = None, agents: list | None = None):
        self._stats = stats
        self._agents = agents or []

    def get_active_agents(self):
        return self._agents


def test_tune_models_from_learning_swaps_best(monkeypatch) -> None:
    # high:opus 1/4 accepted; high:sonnet 4/4 accepted → tune high→sonnet.
    stats = {
        "outcome_distribution": {
            "high:opus": {"accepted": 1, "rejected": 3},
            "high:sonnet": {"accepted": 4},
            "low:haiku": {"accepted": 5},
        }
    }
    import shared.workflow_export as wx

    monkeypatch.setattr(
        wx, "tune_models_from_learning", wx.tune_models_from_learning
    )  # keep real fn
    monkeypatch.setattr(
        "shared.memory.memory_get",
        lambda scope, key, *a, **k: {"value": stats},
    )
    overrides = tune_models_from_learning(_FakeDB(), {"high": "opus", "low": "haiku"})
    assert overrides == {"high": "sonnet"}  # low unchanged (already best)


def test_tune_models_empty_without_min_samples(monkeypatch) -> None:
    stats = {"outcome_distribution": {"high:sonnet": {"accepted": 1}}}  # < min_samples
    monkeypatch.setattr(
        "shared.memory.memory_get", lambda scope, key, *a, **k: {"value": stats}
    )
    assert tune_models_from_learning(_FakeDB(), {"high": "opus"}) == {}


def test_doc_header_documents_everything() -> None:
    draft = build_workflow_draft(
        name="review-app", script="x", fingerprint="f",
        plan_dict={"topology": "dag", "waves": [[1, 2], [3]], "analysis": "review fanout"},
        run_count=4, tier_models={"high": "opus", "low": "haiku"},
        personas=["correctness-first", "risk-first"],
    )
    header = build_workflow_doc_header(
        draft, tuning={"high": "sonnet"}, learned_agents=["py-refactorer"]
    )
    assert "/review-app" in header
    assert "4 successful run" in header
    assert "high" in header and "sonnet" in header and "re-tuned" in header
    assert "correctness-first" in header and "Correctness-first" in header
    assert "py-refactorer" in header


def test_export_applies_tuning_and_header(tmp_path) -> None:
    draft = build_workflow_draft(
        name="tuned", script='a {"model": "opus"} b\n', fingerprint="f",
        plan_dict={"topology": "dag", "waves": [[1, 2]], "analysis": "x"},
        run_count=3, tier_models={"high": "opus"}, personas=["correctness-first"],
    )
    db = _FakeDB(agents=[{"name": "spec-agent"}])
    import shared.workflow_export as wx
    # Force tuning high→sonnet.
    orig = wx.tune_models_from_learning
    wx.tune_models_from_learning = lambda _db, tm, **k: {"high": "sonnet"}
    try:
        res = export_workflow(draft, project_path=str(tmp_path), db=db, tune=True)
    finally:
        wx.tune_models_from_learning = orig
    written = (tmp_path / ".claude" / "workflows" / "tuned.js").read_text(encoding="utf-8")
    assert '"model": "sonnet"' in written and '"model": "opus"' not in written
    assert "Threnody learned workflow" in written  # header present
    assert "spec-agent" in written  # learned agent linked


def test_handler_consensus_quorum(monkeypatch) -> None:
    import mcp_server

    _stub_handler_env(monkeypatch, snapshot=None, shape_counter={"count": 0})
    monkeypatch.setattr(mcp_server, "record_consensus_learning", lambda *a, **k: None)
    consensus = [
        {"persona": "correctness-first", "result": {"verdict": "complete", "amendment": None, "next_work": None}},
        {"persona": "risk-first", "result": {"verdict": "complete", "amendment": None, "next_work": None}},
    ]
    res = mcp_server.handle_report_workflow_result(
        {"workflow_name": "wf", "agents": _agents(), "consensus": consensus}
    )
    assert res["consensus"]["resolved"] is True
    assert res["consensus"]["verdict"] == "complete"
    assert "consensus_followup" not in res


def test_handler_consensus_judge_followup(monkeypatch) -> None:
    import mcp_server

    _stub_handler_env(monkeypatch, snapshot=None, shape_counter={"count": 0})
    # Divergent verdicts → no quorum → judge followup.
    consensus = [
        {"persona": "correctness-first", "result": {"verdict": "another-pass", "next_work": {"reason": "a"}}},
        {"persona": "speed-first", "result": {"verdict": "complete", "amendment": None, "next_work": None}},
    ]
    res = mcp_server.handle_report_workflow_result(
        {"workflow_name": "wf", "agents": _agents(), "consensus": consensus}
    )
    assert res.get("consensus_followup", {}).get("judge_needed") is True
    assert "judge_prompt" in res["consensus_followup"]
