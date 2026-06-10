#!/usr/bin/env python3
"""Reject runtime state and unsafe paths from a Threnody source archive."""
from __future__ import annotations

import argparse
import subprocess
import tarfile
import zipfile
from pathlib import PurePosixPath


FORBIDDEN_NAMES = {
    ".DS_Store",
    ".aider.chat.history.md",
    ".aider.input.history",
    ".env",
    "Thumbs.db",
    "audit_secret",
    "auth.json",
    "config.yaml",
    "credentials.json",
    "providers.json",
    "threnody-status.json",
}
FORBIDDEN_COMPONENTS = {
    ".copilot-sandbox",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".runtime",
    "__pycache__",
    "backup",
}


def forbidden_reason(raw_path: str) -> str | None:
    normalized = raw_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    parts = tuple(part for part in path.parts if part not in ("", "."))

    if path.is_absolute() or ".." in parts:
        return "unsafe absolute or parent-traversal path"
    if not parts:
        return None

    name = parts[-1]
    if any(part in FORBIDDEN_COMPONENTS for part in parts):
        return "runtime/cache directory"
    if name.startswith("._"):
        return "macOS AppleDouble metadata"
    if name in FORBIDDEN_NAMES:
        return "runtime, secret, or machine-specific file"
    if name.startswith(".env.") and name not in {".env.example", ".env.sample"}:
        return "environment secret file"
    if name.startswith(".aider.tags.cache."):
        return "Aider cache file"
    if name.endswith((".pyc", ".pyo", ".db", ".db-wal", ".db-shm")):
        return "generated cache or database file"
    if ".db.bak" in name:
        return "database backup file"
    if name.endswith(".bak") or ".bak-" in name or ".bak." in name:
        return "backup file"
    if name.endswith(".log") or ".log." in name:
        return "log file"
    return None


def inspect_paths(paths: list[str]) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for path in paths:
        reason = forbidden_reason(path)
        if reason:
            findings.append((path, reason))
    return findings


def tracked_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def archive_paths(archive: str) -> tuple[list[str], list[tuple[str, str]]]:
    paths: list[str] = []
    link_findings: list[tuple[str, str]] = []
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive, "r:*") as handle:
            for member in handle.getmembers():
                paths.append(member.name)
                if member.issym() or member.islnk():
                    reason = forbidden_reason(member.linkname)
                    if reason:
                        link_findings.append(
                            (member.name, f"unsafe link target: {member.linkname}")
                        )
        return paths, link_findings
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as handle:
            return handle.namelist(), link_findings
    raise ValueError(f"Unsupported or invalid archive: {archive}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect tracked files or a release archive for unsafe content."
    )
    parser.add_argument("--archive", help="Optional .tar, .tar.gz, or .zip archive")
    args = parser.parse_args()

    try:
        if args.archive:
            paths, findings = archive_paths(args.archive)
        else:
            paths, findings = tracked_paths(), []
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"release archive inspection failed: {exc}")
        return 2

    findings.extend(inspect_paths(paths))
    if findings:
        print("release archive inspection failed:")
        for path, reason in sorted(findings):
            print(f"  {path}: {reason}")
        return 1

    print(f"release archive inspection passed: {len(paths)} entries checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
