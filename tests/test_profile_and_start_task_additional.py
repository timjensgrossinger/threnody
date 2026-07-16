import subprocess
from pathlib import Path
import mcp_server
from shared.project_profile import profile_project


def test_profile_missing_manifests(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())
    profile = profile_project(tmp_path)
    assert profile.manifests == []


def test_profile_git_info(tmp_path, monkeypatch):
    # Initialize a git repo and commit
    Path(tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())
    profile = profile_project(tmp_path)
    assert profile.git is not None
    assert profile.git.root is not None
    assert profile.git.dirty is False


def test_start_task_review_returns_host_waves(tmp_path, monkeypatch):
    # Create a small repo with a file
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("print('x')\n")
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())
    res = mcp_server.handle_start_task({"task": "Review src", "mode": "review"})
    assert res.get("next_action", {}).get("action_kind") == "host_spawn"
    assert isinstance(res.get("host_spawn_waves"), list)


def test_profile_scan_is_bounded(tmp_path, monkeypatch):
    from shared import project_profile

    monkeypatch.setattr(project_profile, "MAX_SCAN_FILES", 1)
    (tmp_path / "one.txt").write_text("one")
    (tmp_path / "two.txt").write_text("two")

    profile = profile_project(tmp_path)

    assert any("scan capped" in warning for warning in profile.warnings)


def test_start_task_profile_uses_relative_public_paths(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{"name":"demo","scripts":{"test":"pytest"}}')
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())

    result = mcp_server.handle_start_task(
        {"task": "Inspect project", "mode": "investigate"}
    )

    assert result["profile"]["workspace_root"] == "."
    assert result["profile"]["manifests"][0]["path"] == "package.json"
    assert "parsed" not in result["profile"]["manifests"][0]


def test_profile_scan_is_bounded(tmp_path, monkeypatch):
    from shared import project_profile

    monkeypatch.setattr(project_profile, "MAX_SCAN_FILES", 1)
    (tmp_path / "one.txt").write_text("one")
    (tmp_path / "two.txt").write_text("two")

    profile = profile_project(tmp_path)

    assert any("scan capped" in warning for warning in profile.warnings)
