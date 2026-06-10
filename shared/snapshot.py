from __future__ import annotations

import difflib
from dataclasses import dataclass
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class FileDiff:
    path: str
    change_type: str
    lines_added: int
    lines_removed: int
    diff: str


@dataclass
class SnapshotEntry:
    content: bytes
    size: int
    mtime_ns: int
    mode: int


class FileSnapshot:
    """Capture and restore a lightweight workspace file snapshot."""

    _IGNORED_DIR_NAMES = {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "node_modules",
        ".venv",
        "venv",
        "backup",
    }
    _IGNORED_FILE_NAMES = {
        "cache.db",
        "cache.db-shm",
        "cache.db-wal",
        "threnody-status.json",
    }

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(os.path.abspath(workspace_root))
        self._files: dict[Path, SnapshotEntry] = {}

    def _iter_files(self) -> list[Path]:
        files: list[Path] = []
        for root, dirs, filenames in os.walk(
            self.workspace_root,
            topdown=True,
            followlinks=False,
            onerror=lambda _err: None,
        ):
            dirs[:] = [name for name in dirs if name not in self._IGNORED_DIR_NAMES]
            root_path = Path(root)
            for filename in filenames:
                if (
                    filename in self._IGNORED_FILE_NAMES
                    or filename.startswith("cache.db.bak.")
                ):
                    continue
                path = root_path / filename
                if path.is_symlink():
                    continue
                files.append(path)
        return files

    def _resolve_path(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        normalized = Path(os.path.abspath(candidate))
        normalized.relative_to(self.workspace_root)
        return normalized

    def _relative_key(self, path: Path) -> Path:
        try:
            return path.relative_to(self.workspace_root)
        except ValueError:
            return path

    def _decode(self, content: bytes | None) -> str | None:
        if content is None:
            return None
        return content.decode("utf-8", errors="replace")

    def _read_entry(self, path: Path) -> SnapshotEntry | None:
        try:
            stat = path.stat()
            if path.is_symlink():
                return None
            return SnapshotEntry(
                content=path.read_bytes(),
                size=int(stat.st_size),
                mtime_ns=int(stat.st_mtime_ns),
                mode=int(stat.st_mode),
            )
        except (FileNotFoundError, OSError):
            return None

    def _scan_current_files(self) -> dict[Path, Path]:
        return {
            self._relative_key(path): path
            for path in self._iter_files()
        }

    def _write_bytes_nofollow(self, path: Path, content: bytes, mode: int) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, mode & 0o777)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
        finally:
            try:
                os.chmod(path, mode & 0o777)
            except OSError:
                pass

    def _has_symlink_parent(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.workspace_root)
        except ValueError:
            return True
        current = self.workspace_root
        for part in relative.parts[:-1]:
            current = current / part
            try:
                if current.is_symlink():
                    return True
            except OSError:
                return True
        return False

    def _build_diff(
        self,
        path: Path,
        before: SnapshotEntry | None,
        after: SnapshotEntry | None,
    ) -> FileDiff:
        before_text = self._decode(before.content if before is not None else None)
        after_text = self._decode(after.content if after is not None else None)
        display_path = str(path)

        if before is None and after is None:
            return FileDiff(display_path, "unchanged", 0, 0, "")
        if before is None and after_text is not None:
            new_lines = after_text.splitlines(keepends=True)
            diff_lines = list(
                difflib.unified_diff(
                    [],
                    new_lines,
                    fromfile="/dev/null",
                    tofile=display_path,
                    lineterm="",
                )
            )
            return FileDiff(
                path=display_path,
                change_type="created",
                lines_added=len(new_lines),
                lines_removed=0,
                diff="\n".join(diff_lines),
            )
        if before_text is not None and after is None:
            old_lines = before_text.splitlines(keepends=True)
            diff_lines = list(
                difflib.unified_diff(
                    old_lines,
                    [],
                    fromfile=f"a/{display_path}",
                    tofile="/dev/null",
                    lineterm="",
                )
            )
            removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
            return FileDiff(
                path=display_path,
                change_type="deleted",
                lines_added=0,
                lines_removed=removed,
                diff="\n".join(diff_lines),
            )

        assert before_text is not None
        assert after_text is not None
        before_lines = before_text.splitlines(keepends=True)
        after_lines = after_text.splitlines(keepends=True)
        if before_lines == after_lines:
            return FileDiff(display_path, "unchanged", 0, 0, "")

        diff_lines = list(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{display_path}",
                tofile=f"b/{display_path}",
                lineterm="",
            )
        )
        added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
        return FileDiff(
            path=display_path,
            change_type="modified",
            lines_added=added,
            lines_removed=removed,
            diff="\n".join(diff_lines),
            )

    def take(self) -> None:
        captured: dict[Path, SnapshotEntry] = {}
        for path in self._iter_files():
            entry = self._read_entry(path)
            if entry is not None:
                captured[self._relative_key(path)] = entry
        self._files = captured

    def diff_since(self, target_file: str | None = None) -> list[FileDiff]:
        current_files = self._scan_current_files()
        keys = sorted(set(self._files) | set(current_files), key=str)
        target_key: Path | None = None
        if target_file:
            try:
                target_key = self._relative_key(self._resolve_path(target_file))
            except ValueError:
                target_key = None
        if target_key is not None:
            keys.sort(key=lambda key: (key != target_key, str(key)))

        diffs: list[FileDiff] = []
        for key in keys:
            resolved = self._resolve_path(key)
            before = self._files.get(key)
            after_path = current_files.get(key)
            after = self._read_entry(after_path) if after_path is not None else None
            if (
                before is not None
                and after is not None
                and before.size == after.size
                and before.mtime_ns == after.mtime_ns
                and before.mode == after.mode
            ):
                continue
            diff = self._build_diff(
                resolved,
                before,
                after,
            )
            if diff.change_type != "unchanged":
                diffs.append(diff)
        return diffs

    def revert(self, diffs: list[FileDiff]) -> None:
        for diff in diffs:
            try:
                resolved = self._resolve_path(diff.path)
            except ValueError:
                log.warning("snapshot revert skipped path outside workspace: %s", diff.path)
                continue
            key = self._relative_key(resolved)
            original = self._files.get(key)
            try:
                if self._has_symlink_parent(resolved):
                    log.warning("snapshot revert skipped symlinked parent path: %s", resolved)
                    continue
                if original is None:
                    resolved.unlink(missing_ok=True)
                    continue
                if resolved.is_symlink():
                    resolved.unlink(missing_ok=True)
                resolved.parent.mkdir(parents=True, exist_ok=True)
                self._write_bytes_nofollow(resolved, original.content, original.mode)
            except (FileNotFoundError, OSError):
                log.warning("snapshot revert failed for %s", resolved, exc_info=True)


def apply_unified_diff(target_path: Path, diff_text: str) -> tuple[str, int, int]:
    """Apply a unified diff to target_path. Returns (new_content, lines_added, lines_removed).

    Raises ValueError on hunk mismatch. Pure Python — no subprocess or third-party deps.
    """
    try:
        original_content = target_path.read_text()
    except Exception:
        original_content = ""
    original_had_trailing_newline = original_content.endswith("\n")
    orig_lines = original_content.splitlines()
    diff_lines = diff_text.splitlines()
    new_lines: list[str] = []
    orig_ptr = 0
    added = 0
    removed = 0
    i = 0
    n = len(diff_lines)

    def _parse_range(token: str) -> tuple[int, int]:
        token = token.lstrip("+-")
        if "," in token:
            a, b = token.split(",", 1)
            return int(a), int(b)
        return int(token), 1

    while i < n:
        line = diff_lines[i]
        if line.startswith("---") or line.startswith("+++"):
            i += 1
            continue
        if not line.startswith("@@"):
            i += 1
            continue
        # parse hunk header: @@ -start[,count] +start[,count] @@
        parts = line.split("@@")
        if len(parts) < 3:
            raise ValueError(f"Invalid hunk header: {line}")
        tokens = parts[1].strip().split()
        if len(tokens) < 2:
            raise ValueError(f"Invalid hunk header counts: {line}")
        orig_start, _orig_count = _parse_range(tokens[0])
        i += 1
        hunk_orig_index = orig_start - 1
        if hunk_orig_index < 0:
            raise ValueError(f"Invalid hunk original start: {orig_start}")
        if orig_ptr > hunk_orig_index:
            raise ValueError("Hunk overlaps previous hunk")
        # copy lines before this hunk
        while orig_ptr < hunk_orig_index and orig_ptr < len(orig_lines):
            new_lines.append(orig_lines[orig_ptr])
            orig_ptr += 1
        # process hunk body
        while i < n and not diff_lines[i].startswith("@@"):
            body_line = diff_lines[i]
            i += 1
            if not body_line:
                continue
            prefix = body_line[0]
            content = body_line[1:]
            if content.endswith("\r"):
                content = content[:-1]
            if prefix == " ":
                if orig_ptr >= len(orig_lines) or orig_lines[orig_ptr] != content:
                    raise ValueError(
                        f"Hunk mismatch context at original line {orig_ptr + 1}: "
                        f"expected {repr(content)}, got {repr(orig_lines[orig_ptr] if orig_ptr < len(orig_lines) else '<EOF>')}"
                    )
                new_lines.append(content)
                orig_ptr += 1
            elif prefix == "-":
                if orig_ptr >= len(orig_lines) or orig_lines[orig_ptr] != content:
                    raise ValueError(
                        f"Hunk mismatch removal at original line {orig_ptr + 1}: "
                        f"expected {repr(content)}"
                    )
                orig_ptr += 1
                removed += 1
            elif prefix == "+":
                new_lines.append(content)
                added += 1
            # else: metadata lines like \ No newline — skip

    # append remaining original lines after last hunk
    while orig_ptr < len(orig_lines):
        new_lines.append(orig_lines[orig_ptr])
        orig_ptr += 1

    result = "\n".join(new_lines)
    if original_had_trailing_newline and result and not result.endswith("\n"):
        result += "\n"
    return result, added, removed
