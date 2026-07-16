"""
shared/project_profile.py — Read-only project profiler and public contracts.

Provides typed dataclasses for ProjectProfile, StartTaskRequest, and
StartTaskResponse and a safe, bounded, read-only project profiling function
(profile_project) that detects manifests, package managers, candidate commands,
and lightweight git state. Does not execute builds/tests/lints or launch
providers. Enforces workspace containment and bounded reads to avoid secret
leakage.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List

MAX_MANIFEST_READ = 64 * 1024  # bytes
MAX_CHANGED_FILES = 50
MAX_SCAN_FILES = 5000
MAX_SCAN_DEPTH = 6
_SKIP_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "node_modules",
    "target",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
})


class InvalidProjectRoot(Exception):
    pass


@dataclass
class ManifestInfo:
    path: str
    kind: str
    size: int
    parsed: Dict[str, Any] | None = None


@dataclass
class GitInfo:
    root: str | None
    branch: str | None
    dirty: bool
    changed_files: List[str] = field(default_factory=list)


@dataclass
class ProjectProfile:
    workspace_root: str
    manifests: List[ManifestInfo] = field(default_factory=list)
    package_managers: List[str] = field(default_factory=list)
    candidate_commands: Dict[str, List[str]] = field(default_factory=lambda: {
        "build": [],
        "test": [],
        "lint": [],
        "format": [],
        "type_check": [],
    })
    git: GitInfo | None = None
    warnings: List[str] = field(default_factory=list)
    host_native_available: bool = True


@dataclass
class StartTaskRequest:
    task: str
    mode: str = "implement"
    cwd: str | None = None


@dataclass
class StartTaskResponse:
    next_action: Dict[str, Any]
    profile: ProjectProfile
    warnings: List[str] = field(default_factory=list)
    selected_tier: str | None = None
    selected_model: str | None = None
    provider: Dict[str, Any] | None = None
    host_spawn_waves: List[Dict[str, Any]] | None = None
    handoff: Dict[str, Any] | None = None


# --- Internal helpers ------------------------------------------------------


def _safe_read(path: Path, max_bytes: int = MAX_MANIFEST_READ) -> str | None:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if size > max_bytes:
        return None
    try:
        return resolved.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None


def _json_safe(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


# --- Profiler --------------------------------------------------------------


def profile_project(workspace_root: Path | str) -> ProjectProfile:
    try:
        root = Path(workspace_root).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InvalidProjectRoot(f"Invalid workspace root: {workspace_root}") from exc
    if not root.exists() or not root.is_dir():
        raise InvalidProjectRoot(f"Invalid workspace root: {workspace_root}")

    profile = ProjectProfile(workspace_root=str(root))

    # Manifest detection patterns
    MANIFEST_PATTERNS = {
        "python": ["pyproject.toml", "setup.py", "requirements.txt", "Pipfile"],
        "node": ["package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"],
        "go": ["go.mod"],
        "rust": ["Cargo.toml"],
        "java": ["build.gradle", "build.gradle.kts", "pom.xml"],
        "ruby": ["Gemfile", "Gemfile.lock"],
        "dotnet": ["*.csproj", "Directory.Build.props"],
        "make": ["Makefile"],
    }

    # Collect candidate files with explicit depth and volume bounds.
    try:
        scanned_files = 0
        for current_dir, dirnames, filenames in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current_dir)
            depth = len(current_path.relative_to(root).parts)
            dirnames[:] = [
                name for name in dirnames
                if name not in _SKIP_DIRS
                and not name.startswith(".")
                and not (current_path / name).is_symlink()
            ]
            if depth >= MAX_SCAN_DEPTH:
                dirnames[:] = []
            for name in filenames:
                if scanned_files >= MAX_SCAN_FILES:
                    profile.warnings.append(
                        f"Manifest scan capped at {MAX_SCAN_FILES} files"
                    )
                    raise StopIteration
                scanned_files += 1
                p = current_path / name
                for kind, patterns in MANIFEST_PATTERNS.items():
                    if not any(
                        p.match(pattern) if pattern.startswith("*") else name == pattern
                        for pattern in patterns
                    ):
                        continue
                    try:
                        resolved = p.resolve(strict=False)
                    except OSError:
                        profile.warnings.append(f"Manifest skip error: {p}")
                        continue
                    if not resolved.is_relative_to(root):
                        profile.warnings.append(
                            f"Manifest skipped (outside workspace): {p}"
                        )
                        continue
                    try:
                        size = resolved.stat().st_size
                    except OSError:
                        continue
                    txt = _safe_read(resolved)
                    parsed = None
                    if txt is not None:
                        if name == "package.json":
                            parsed = _json_safe(txt)
                        elif name == "pyproject.toml":
                            if "[tool.poetry]" in txt:
                                parsed = {"poetry": True}
                        elif name == "Cargo.toml":
                            parsed = {"cargo": True}
                        elif len(txt) < 4096:
                            parsed = {"preview": txt[:4096]}
                    profile.manifests.append(
                        ManifestInfo(
                            path=str(resolved),
                            kind=kind,
                            size=size,
                            parsed=parsed,
                        )
                    )
    except StopIteration:
        pass
    except OSError as exc:
        profile.warnings.append(f"manifest scan error: {exc}")

    # Detect package managers and candidate commands heuristically
    pm = set()
    cmds = profile.candidate_commands

    # Presence tests
    if any(m.path.endswith("package.json") for m in profile.manifests):
        pm.add("npm")
        # extract npm scripts
        for m in profile.manifests:
            if m.path.endswith("package.json") and isinstance(m.parsed, dict):
                scripts = m.parsed.get("scripts") if isinstance(m.parsed, dict) else None
                if isinstance(scripts, dict):
                    for name in scripts.keys():
                        if "test" in name.lower():
                            cmds["test"].append(f"npm run {name}")
                        elif "build" in name.lower():
                            cmds["build"].append(f"npm run {name}")
                        else:
                            # generic script awareness
                            cmds["build"].append(f"npm run {name}")
        # generic fallbacks
        cmds["build"].extend([c for c in ["npm run build", "npm run build:prod"] if c not in cmds["build"]])
        cmds["test"].extend([c for c in ["npm test", "npm run test"] if c not in cmds["test"]])

    # yarn/pnpm detection by lockfiles
    if any(m.path.endswith("yarn.lock") for m in profile.manifests):
        pm.add("yarn")
        cmds["build"].append("yarn build")
        cmds["test"].append("yarn test")
    if any(m.path.endswith("pnpm-lock.yaml") for m in profile.manifests):
        pm.add("pnpm")
        cmds["build"].append("pnpm build")
        cmds["test"].append("pnpm test")

    # python
    if any(m.kind == "python" for m in profile.manifests):
        pm.add("pip")
        # poetry heuristic
        if any(m.parsed and isinstance(m.parsed, dict) and m.parsed.get("poetry") for m in profile.manifests):
            pm.add("poetry")
            cmds["build"].append("poetry build")
            cmds["test"].append("poetry run pytest")
        else:
            cmds["build"].append("python -m build")
            cmds["test"].append("python -m pytest")
        cmds["lint"].append("ruff .")
        cmds["format"].append("black .")
        cmds["type_check"].append("mypy .")

    # go
    if any(m.kind == "go" for m in profile.manifests):
        pm.add("go")
        cmds["build"].append("go build ./...")
        cmds["test"].append("go test ./...")

    # rust
    if any(m.kind == "rust" for m in profile.manifests):
        pm.add("cargo")
        cmds["build"].append("cargo build")
        cmds["test"].append("cargo test")
        cmds["format"].append("cargo fmt")

    # java/kotlin
    if any(m.kind == "java" for m in profile.manifests):
        pm.add("gradle/maven")
        if (root / "gradlew").exists():
            cmds["build"].append("./gradlew build")
            cmds["test"].append("./gradlew test")
        else:
            cmds["build"].append("gradle build")
            cmds["test"].append("mvn test")

    # ruby
    if any(m.kind == "ruby" for m in profile.manifests):
        pm.add("bundler")
        cmds["build"].append("bundle install")
        cmds["test"].append("bundle exec rake test")

    # dotnet
    if any(m.kind == "dotnet" for m in profile.manifests):
        pm.add("dotnet")
        cmds["build"].append("dotnet build")
        cmds["test"].append("dotnet test")

    # Makefile
    if (root / "Makefile").exists():
        pm.add("make")
        cmds["build"].append("make build")
        cmds["test"].append("make test")

    profile.package_managers = sorted(list(pm))

    # Clean duplicates while preserving order
    for k, v in list(cmds.items()):
        seen = set()
        out = []
        for item in v:
            if item not in seen:
                out.append(item)
                seen.add(item)
        cmds[k] = out

    # Git summary (best-effort, read-only)
    git_root = None
    git_branch = None
    dirty = False
    changed_files: List[str] = []
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if cp.returncode == 0:
            git_root = cp.stdout.strip()
            try:
                if git_root and not root.is_relative_to(
                    Path(git_root).resolve(strict=False)
                ):
                    profile.warnings.append(
                        "git root is outside the requested workspace; "
                        "some git-derived paths were omitted"
                    )
            except (OSError, RuntimeError):
                profile.warnings.append("git root could not be normalized")
            cb = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if cb.returncode == 0:
                git_branch = cb.stdout.strip()
            st = subprocess.run(
                ["git", "status", "--porcelain=v1", "-z"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if st.returncode == 0 and st.stdout:
                records = [record for record in st.stdout.split("\0") if record]
                dirty = bool(records)
                index = 0
                while index < len(records) and len(changed_files) < MAX_CHANGED_FILES:
                    record = records[index]
                    if len(record) >= 3 and record[2] == " ":
                        status = record[:2]
                        path = record[3:]
                    else:
                        separator = record.find(" ")
                        status = record[:separator] if separator >= 0 else ""
                        path = record[separator + 1:] if separator >= 0 else record
                    if "R" in status or "C" in status:
                        index += 1
                        if index < len(records):
                            changed_files.append(f"{records[index]} -> {path}")
                        else:
                            changed_files.append(path)
                    else:
                        changed_files.append(path)
                    index += 1
    except (OSError, subprocess.SubprocessError):
        # best-effort; do not fail profiling
        profile.warnings.append("git inspection failed or git not available")

    profile.git = GitInfo(root=git_root, branch=git_branch, dirty=dirty, changed_files=changed_files)

    return profile


def to_dict(obj: Any) -> Any:
    """Helpers to jsonify dataclasses for MCP responses."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    try:
        return asdict(obj) if hasattr(obj, "__dataclass_fields__") else obj
    except Exception:
        return str(obj)


def to_public_dict(profile: ProjectProfile) -> dict[str, Any]:
    """Serialize a profile without exposing host-absolute paths or manifest data."""
    root = Path(profile.workspace_root).resolve(strict=False)

    def relative_path(raw: str | None) -> str | None:
        if not raw:
            return None
        try:
            return Path(raw).resolve(strict=False).relative_to(root).as_posix() or "."
        except (OSError, ValueError):
            return None

    manifests = [
        {
            "path": relative_path(manifest.path),
            "kind": manifest.kind,
            "size": manifest.size,
        }
        for manifest in profile.manifests
    ]
    git = None
    if profile.git is not None:
        changed_files: list[str] = []

        def public_changed_path(raw_path: str) -> str | None:
            value = str(raw_path).replace("\\", "/")
            if " -> " in value:
                source, target = value.split(" -> ", 1)
                source_public = public_changed_path(source)
                target_public = public_changed_path(target)
                if source_public and target_public:
                    return f"{source_public} -> {target_public}"
                return target_public or source_public
            candidate = Path(value)
            if candidate.is_absolute():
                return relative_path(value)
            try:
                resolved = (root / candidate).resolve(strict=False)
                return resolved.relative_to(root).as_posix()
            except (OSError, ValueError):
                return None

        for raw_path in profile.git.changed_files:
            normalized = public_changed_path(raw_path)
            if normalized is not None:
                changed_files.append(normalized)
        git = {
            "root": relative_path(profile.git.root),
            "branch": profile.git.branch,
            "dirty": profile.git.dirty,
            "changed_files": changed_files,
        }
    return {
        "workspace_root": ".",
        "manifests": manifests,
        "package_managers": list(profile.package_managers),
        "candidate_commands": {
            key: list(commands)
            for key, commands in profile.candidate_commands.items()
        },
        "git": git,
        "warnings": list(profile.warnings),
        "host_native_available": profile.host_native_available,
    }


# Exported names
__all__ = [
    "ProjectProfile",
    "ManifestInfo",
    "GitInfo",
    "StartTaskRequest",
    "StartTaskResponse",
    "profile_project",
    "InvalidProjectRoot",
    "to_dict",
    "to_public_dict",
]
