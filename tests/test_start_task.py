from pathlib import Path
import mcp_server


def test_start_task_implement_heuristic(tmp_path, monkeypatch):
    # Create a simple file-scoped task context
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("def foo(): pass\n")
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())

    result = mcp_server.handle_start_task({"task": "Create src/foo.py", "mode": "implement"})
    assert isinstance(result, dict)
    assert "profile" in result
    assert "next_action" in result
    na = result.get("next_action")
    assert na and na.get("action_kind") in {"host_spawn", "planner_handoff", "investigate_complete"}


def test_start_task_investigate(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())
    res = mcp_server.handle_start_task({"task": "Inspect project", "mode": "investigate"})
    assert res.get("next_action", {}).get("action_kind") == "investigate_complete"


def test_start_task_rejects_workspace_outside_active_root(tmp_path, monkeypatch):
    active = tmp_path / "active"
    outside = tmp_path / "outside"
    active.mkdir()
    outside.mkdir()
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: active.resolve())

    result = mcp_server.handle_start_task(
        {"task": "Inspect project", "mode": "investigate", "cwd": str(outside)}
    )

    assert "within the active workspace" in result["error"]


def test_start_task_review_forces_review_fanout(tmp_path, monkeypatch):
    (tmp_path / "src.py").write_text("print('x')\n")
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())

    result = mcp_server.handle_start_task(
        {"task": "Review src.py", "mode": "review"}
    )

    assert result["next_action"]["action_reason"] == "review_fanout"
    assert result["host_spawn_waves"]


def test_start_task_rejects_non_string_task(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())

    result = mcp_server.handle_start_task({"task": 42})

    assert result == {"error": "task must be a string"}


def test_start_task_string_argument_shorthand_is_supported():
    assert mcp_server._normalize_mcp_tool_arguments("start_task", "Inspect project") == {
        "task": "Inspect project"
    }


def test_start_task_is_dispatched_as_blocking():
    assert "start_task" in mcp_server._BLOCKING_TOOLS


def test_start_task_review_drops_external_targets(tmp_path, monkeypatch):
    active = tmp_path / "active"
    outside = tmp_path / "outside.py"
    active.mkdir()
    outside.write_text("secret = True\n")
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: active.resolve())

    result = mcp_server.handle_start_task(
        {"task": f"Review {outside}", "mode": "review"}
    )

    assert result == {"error": "review targets must be inside the active workspace"}


def test_start_task_review_rejects_relative_traversal(tmp_path, monkeypatch):
    active = tmp_path / "active"
    active.mkdir()
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: active.resolve())

    result = mcp_server.handle_start_task(
        {"task": "Review ../outside.py", "mode": "review"}
    )

    assert result == {"error": "review targets must be inside the active workspace"}
