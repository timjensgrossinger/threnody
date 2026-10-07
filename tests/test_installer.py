from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _copy_source(target: Path) -> Path:
    shutil.copytree(
        ROOT,
        target,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            "__pycache__",
            "*.pyc",
            "cache.db*",
            ".runtime",
            "providers.json",
            "audit_secret",
            "threnody-status.json",
        ),
    )
    return target


def _installer_env(home: Path, temp_dir: Path, **overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "SHELL": "/bin/bash",
            "TMPDIR": str(temp_dir),
            "THRENODY_ALLOW_NO_HOST": "1",
            "THRENODY_SKIP_DEPENDENCIES": "1",
            "THRENODY_SKIP_WIZARD": "1",
            "THRENODY_PROVIDER_SCAN_TEST_MODE": "1",
        }
    )
    env.update(overrides)
    return env


def _run_installer(
    source: Path,
    home: Path,
    temp_dir: Path,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(source / "install.sh")],
        cwd=source,
        env=_installer_env(home, temp_dir, **overrides),
        capture_output=True,
        text=True,
        timeout=90,
    )


_REVIEW_DEFINITIONS = {
    "threnody-review-security",
    "threnody-review-logic",
    "threnody-review-edge",
    "threnody-review-types",
    "threnody-review-performance",
    "threnody-review-fast",
}


def _bundled_skill_names(source: Path) -> set[str]:
    return {path.parent.name for path in source.glob("skills/threnody-*/SKILL.md")}


def test_clean_install_with_spaces_and_portable_copy(tmp_path: Path) -> None:
    source = _copy_source(tmp_path / "source tree")
    home = tmp_path / "home with spaces"
    temp_dir = tmp_path / "temporary files"
    home.mkdir()
    temp_dir.mkdir()

    result = _run_installer(
        source,
        home,
        temp_dir,
        THRENODY_FORCE_PORTABLE_COPY="1",
    )

    assert result.returncode == 0, result.stderr
    install_dir = home / ".local/lib/threnody"
    assert (install_dir / "mcp_server.py").is_file()
    assert (install_dir / "uninstall.sh").is_file()
    assert (install_dir / "providers.json").is_file()
    assert (home / ".local/bin/threnody").is_symlink()
    assert "using portable Python copy fallback" in result.stderr
    assert not list(temp_dir.iterdir())

    skill_names = _bundled_skill_names(source)
    assert skill_names
    for target in (
        home / ".agents/skills",
        home / ".codex/skills",
        home / ".claude/skills",
        home / ".cursor/skills",
    ):
        installed = {path.parent.name for path in target.glob("threnody-*/SKILL.md")}
        # Review definitions share the threnody- prefix but are exported by
        # agent_export, not copied from skills/ — compared separately below.
        assert installed - _REVIEW_DEFINITIONS == skill_names

    for target in (
        home / ".copilot/agents",
        home / ".config/opencode/agent",
    ):
        installed = {path.stem for path in target.glob("threnody-*.md")}
        assert installed - _REVIEW_DEFINITIONS == skill_names
        assert _REVIEW_DEFINITIONS <= installed

    claude_agents = {path.stem for path in (home / ".claude/agents").glob("threnody-review-*.md")}
    assert _REVIEW_DEFINITIONS <= claude_agents
    # Claude Code also gets one effort variant per review definition.
    assert {f"{name}-high" for name in _REVIEW_DEFINITIONS} <= claude_agents


def test_reinstall_is_idempotent_and_preserves_runtime_data(tmp_path: Path) -> None:
    source = _copy_source(tmp_path / "source")
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    home.mkdir()
    temp_dir.mkdir()

    first = _run_installer(source, home, temp_dir)
    assert first.returncode == 0, first.stderr

    install_dir = home / ".local/lib/threnody"
    config = install_dir / "config.yaml"
    database = install_dir / "cache.db"
    stale = install_dir / "stale-generated-file.txt"
    config.write_text("custom: preserved\n", encoding="utf-8")
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE user_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO user_marker VALUES ('preserved')")
    stale.write_text("remove me", encoding="utf-8")

    second = _run_installer(source, home, temp_dir)

    assert second.returncode == 0, second.stderr
    assert config.read_text(encoding="utf-8") == "custom: preserved\n"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM user_marker").fetchone() == (
            "preserved",
        )
    assert not stale.exists()
    bashrc = (home / ".bashrc").read_text(encoding="utf-8")
    assert bashrc.count("source ") == 1


def test_interrupted_install_cleans_temps_and_reinstall_recovers(tmp_path: Path) -> None:
    source = _copy_source(tmp_path / "source")
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    home.mkdir()
    temp_dir.mkdir()

    failed = _run_installer(
        source,
        home,
        temp_dir,
        THRENODY_TEST_FAIL_AFTER_COPY="1",
    )

    assert failed.returncode != 0
    assert "Injected test failure" in failed.stderr
    assert not list(temp_dir.iterdir())

    recovered = _run_installer(source, home, temp_dir)
    assert recovered.returncode == 0, recovered.stderr
    assert (home / ".local/lib/threnody/mcp_server.py").is_file()


def test_uninstall_preserves_unrelated_configuration_and_runtime_data(
    tmp_path: Path,
) -> None:
    source = _copy_source(tmp_path / "source")
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    home.mkdir()
    temp_dir.mkdir()
    copilot_config = home / ".copilot/mcp-config.json"
    copilot_config.parent.mkdir(parents=True)
    copilot_config.write_text(
        json.dumps(
            {
                "unrelated": {"keep": True},
                "mcpServers": {
                    "Other": {
                        "command": "other",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (home / ".bashrc").write_text("export KEEP_ME=1\n", encoding="utf-8")

    installed = _run_installer(source, home, temp_dir)
    assert installed.returncode == 0, installed.stderr
    install_dir = home / ".local/lib/threnody"
    (install_dir / "config.yaml").write_text("custom: keep\n", encoding="utf-8")
    (install_dir / "cache.db").write_bytes(b"database")

    result = subprocess.run(
        ["bash", str(install_dir / "uninstall.sh")],
        env=_installer_env(home, temp_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert not install_dir.exists()
    backup_dir = home / ".local/share/threnody"
    assert (backup_dir / "config.yaml").read_text(encoding="utf-8") == "custom: keep\n"
    assert (backup_dir / "cache.db").read_bytes() == b"database"
    preserved = json.loads(copilot_config.read_text(encoding="utf-8"))
    assert preserved["unrelated"] == {"keep": True}
    assert preserved["mcpServers"] == {"Other": {"command": "other"}}
    assert (home / ".bashrc").read_text(encoding="utf-8").strip() == "export KEEP_ME=1"
    assert not (home / ".local/bin/threnody").exists()


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_uninstaller_help(flag: str) -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "uninstall.sh"), flag],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "Usage:" in result.stdout


_AGENT_HOOK_EVENTS = {
    "PreToolUse": "pre",
    "PostToolUse": "post",
    "SubagentStart": "subagent-start",
    "SubagentStop": "subagent-stop",
    "SessionStart": "session-start",
}


def _agent_hook_commands(settings: dict, event: str) -> list[str]:
    return [
        hook["command"]
        for group in settings.get("hooks", {}).get(event, [])
        for hook in group.get("hooks", [])
        if "threnody-agent-hook" in str(hook.get("command"))
    ]


def test_agent_hook_registration_is_idempotent_and_removable(tmp_path: Path) -> None:
    source = _copy_source(tmp_path / "source")
    home = tmp_path / "home with spaces"
    temp_dir = tmp_path / "tmp"
    home.mkdir()
    temp_dir.mkdir()
    settings_path = home / ".claude/settings.json"
    settings_path.parent.mkdir(parents=True)
    user_hooks = {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk-rewrite"}]},
        ],
        "SessionStart": [
            {"hooks": [{"type": "command", "command": "gsd-session-start"}]},
        ],
        "SubagentStop": [
            {"hooks": [{"type": "command", "command": "neovimagents-stop"}]},
        ],
    }
    settings_path.write_text(
        json.dumps({"model": "opus", "hooks": user_hooks}), encoding="utf-8"
    )
    claude = {"THRENODY_PROVIDER_SCAN_TEST_HOSTS": "claude-code"}

    for _ in range(2):
        result = _run_installer(source, home, temp_dir, **claude)
        assert result.returncode == 0, result.stderr

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    script = home / ".local/lib/threnody/shell/threnody-agent-hook.sh"
    for event, sub in _AGENT_HOOK_EVENTS.items():
        commands = _agent_hook_commands(settings, event)
        assert len(commands) == 1, (event, commands)
        assert commands[0] == f"'{script}' {sub}"
    pre_groups = [
        g for g in settings["hooks"]["PreToolUse"]
        if any("threnody-agent-hook" in h["command"] for h in g["hooks"])
    ]
    assert pre_groups[0]["matcher"] == "Agent"
    assert pre_groups[0]["hooks"][0]["timeout"] == 5
    # The user's own hooks survive untouched.
    assert settings["model"] == "opus"
    for event, groups in user_hooks.items():
        for group in groups:
            assert group in settings["hooks"][event]
    # Tier effort variants are pre-generated beside the tier agents.
    agents = home / ".claude/agents"
    assert (agents / "threnody-medium-high.md").is_file()
    assert "threnody:effort-variant" in (agents / "threnody-medium-high.md").read_text()

    removed = _run_installer(source, home, temp_dir, THRENODY_SKIP_AGENT_HOOK="1", **claude)
    assert removed.returncode == 0, removed.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert all(not _agent_hook_commands(settings, e) for e in _AGENT_HOOK_EVENTS)
    for event, groups in user_hooks.items():
        for group in groups:
            assert group in settings["hooks"][event]

    reinstalled = _run_installer(source, home, temp_dir, **claude)
    assert reinstalled.returncode == 0, reinstalled.stderr
    install_dir = home / ".local/lib/threnody"
    uninstalled = subprocess.run(
        ["bash", str(install_dir / "uninstall.sh")],
        env=_installer_env(home, temp_dir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert uninstalled.returncode == 0, uninstalled.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert all(not _agent_hook_commands(settings, e) for e in _AGENT_HOOK_EVENTS)
    assert settings["hooks"]["SubagentStop"] == user_hooks["SubagentStop"]
    assert settings["hooks"]["SessionStart"] == user_hooks["SessionStart"]
    assert user_hooks["PreToolUse"][0] in settings["hooks"]["PreToolUse"]
