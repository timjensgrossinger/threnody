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
