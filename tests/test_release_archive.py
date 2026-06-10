from __future__ import annotations

import io
import tarfile
import zipfile

from scripts.check_release_archive import (
    archive_paths,
    forbidden_reason,
    inspect_paths,
)


def test_forbidden_runtime_and_secret_paths() -> None:
    assert forbidden_reason("providers.json")
    assert forbidden_reason("release/cache.db")
    assert forbidden_reason("release/.runtime/state.json")
    assert forbidden_reason("release/.aider.chat.history.md")
    assert forbidden_reason("release/._README.md")
    assert forbidden_reason("release/config.yaml.bak-20260608")
    assert forbidden_reason("../outside.txt")
    assert forbidden_reason("/absolute/path.txt")


def test_safe_source_paths() -> None:
    assert forbidden_reason("README.md") is None
    assert forbidden_reason("config.example.yaml") is None
    assert forbidden_reason("release/.env.example") is None
    assert forbidden_reason("tests/test_release_archive.py") is None


def test_inspect_paths_returns_all_findings() -> None:
    findings = inspect_paths(
        ["README.md", "cache.db", "nested/__pycache__/module.pyc"]
    )
    assert [path for path, _reason in findings] == [
        "cache.db",
        "nested/__pycache__/module.pyc",
    ]


def test_zip_archive_paths(tmp_path) -> None:
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("Threnody/README.md", "ok")
        handle.writestr("Threnody/providers.json", "{}")

    paths, link_findings = archive_paths(str(archive))

    assert link_findings == []
    assert inspect_paths(paths) == [
        ("Threnody/providers.json", "runtime, secret, or machine-specific file")
    ]


def test_tar_rejects_unsafe_symlink_target(tmp_path) -> None:
    archive = tmp_path / "release.tar"
    with tarfile.open(archive, "w") as handle:
        readme = tarfile.TarInfo("Threnody/README.md")
        payload = b"ok"
        readme.size = len(payload)
        handle.addfile(readme, io.BytesIO(payload))

        link = tarfile.TarInfo("Threnody/latest")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        handle.addfile(link)

    paths, link_findings = archive_paths(str(archive))

    assert inspect_paths(paths) == []
    assert link_findings == [
        ("Threnody/latest", "unsafe link target: ../../outside")
    ]
