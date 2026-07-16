import json
from pathlib import Path

import mcp_server
from shared.project_profile import profile_project


def test_profile_detects_package_json_scripts(tmp_path, monkeypatch):
    pkg = {"name": "x", "scripts": {"build": "echo build", "test": "echo test"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    # Ensure MCP active workspace points to tmp_path for the handler
    monkeypatch.setattr(mcp_server, "_active_workspace_root", lambda: tmp_path.resolve())
    profile = profile_project(tmp_path)
    assert any(str(p.path).endswith("package.json") for p in profile.manifests)
    assert "npm run test" in profile.candidate_commands.get("test", [])
    assert "npm run build" in profile.candidate_commands.get("build", [])


def test_profile_invalid_root(tmp_path):
    # non-existent path raises InvalidProjectRoot
    from shared.project_profile import InvalidProjectRoot
    bad = tmp_path / "nope"
    try:
        profile_project(bad)
        raise AssertionError("Expected InvalidProjectRoot")
    except InvalidProjectRoot:
        pass
