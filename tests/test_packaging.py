"""Regression tests for the installable Threnody distribution."""

from __future__ import annotations

import importlib
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import tomllib


ROOT = Path(__file__).resolve().parent.parent


def _project_metadata() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]


def test_project_metadata_and_entry_point() -> None:
    project = _project_metadata()

    assert project["name"] == "threnody-mcp"
    assert project["requires-python"] == ">=3.10,<3.14"
    assert project["dependencies"] == ["pyyaml>=6.0,<7"]
    assert project["optional-dependencies"]["ui"] == [
        "rich>=13.0,<15",
        "questionary>=2.0,<3",
    ]
    assert project["scripts"]["threnody-mcp"] == "threnody.mcp_server:main"

    version_config = tomllib.load((ROOT / "pyproject.toml").open("rb"))
    assert version_config["tool"]["hatch"]["version"]["path"] == "VERSION"


def test_distribution_contains_only_runtime_paths(tmp_path: Path) -> None:
    output_dir = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--wheel",
            "--outdir",
            str(output_dir),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    required = {
        "threnody/__init__.py",
        "threnody/mcp_server.py",
        "mcp_server.py",
        "shared/version.py",
        "shared/data/model_prices.json",
    }
    forbidden_fragments = (
        "tests/",
        "sandbox/",
        "docs/",
        "scripts/",
        "shell/",
        "skills/",
        ".planning/",
        "__pycache__/",
        ".db",
        ".runtime/",
        "providers.json",
        "audit_secret",
    )

    wheel = next(output_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        paths = set(archive.namelist())
    assert required <= paths
    assert not any(
        any(fragment in path for fragment in forbidden_fragments) for path in paths
    )

    sdist = next(output_dir.glob("*.tar.gz"))
    with tarfile.open(sdist) as archive:
        paths = {member.name.split("/", 1)[1] for member in archive.getmembers()}
    assert required <= paths
    assert not any(
        any(fragment in path for fragment in forbidden_fragments) for path in paths
    )


def test_entry_point_shim_delegates_to_existing_server() -> None:
    server = importlib.import_module("mcp_server")
    shim = importlib.import_module("threnody.mcp_server")

    assert shim.main is server.main


def test_source_tree_version_matches_version_file() -> None:
    version = importlib.import_module("shared.version")

    assert version.get_version() == (ROOT / "VERSION").read_text(encoding="utf-8").strip()
