"""
Worktree isolation for execute_subtask.

Each task gets a dedicated git worktree under
~/.local/lib/threnody/worktrees/<task_id>.
After execution, the caller calls release() with "merge" or "discard".
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_WORKTREE_ROOT = Path.home() / ".local" / "lib" / "Threnody" / "worktrees"
_lock = threading.Lock()


class WorktreeError(RuntimeError):
    """Raised when worktree operations fail."""


def _run(cmd: list[str], *, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def _is_git_repo(path: str | Path) -> bool:
    result = _run(["git", "rev-parse", "--git-dir"], cwd=path)
    return result.returncode == 0


class WorktreeManager:
    """Manage per-task git worktrees for isolated subtask execution.

    Usage:
        wm = WorktreeManager(repo_root="/path/to/repo")
        worktree_path = wm.acquire("task-123")
        # ... execute subtask writing to worktree_path ...
        conflicts = wm.release("task-123", action="merge")
    """

    def __init__(
        self,
        repo_root: str | Path,
        worktree_base: str | Path | None = None,
        ttl_hours: float = 24.0,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.worktree_base = Path(worktree_base or _WORKTREE_ROOT)
        self.ttl_hours = ttl_hours
        self._active: dict[str, Path] = {}

    def acquire(self, task_id: str) -> Path:
        """Create a git worktree for task_id. Returns the worktree path.

        Raises WorktreeError if the repo root is not a git repository.
        """
        if not _is_git_repo(self.repo_root):
            raise WorktreeError(
                f"Not a git repository: {self.repo_root}. "
                "Worktree isolation requires git."
            )
        wt_path = self.worktree_base / task_id
        with _lock:
            if task_id in self._active:
                return self._active[task_id]
            wt_path.parent.mkdir(parents=True, exist_ok=True)
            if wt_path.exists():
                shutil.rmtree(str(wt_path), ignore_errors=True)
            result = _run(
                ["git", "worktree", "add", "--detach", str(wt_path)],
                cwd=self.repo_root,
            )
            if result.returncode != 0:
                raise WorktreeError(
                    f"git worktree add failed for task {task_id!r}: {result.stderr.strip()}"
                )
            self._active[task_id] = wt_path
            log.debug("acquired worktree %s for task %s", wt_path, task_id)
        return wt_path

    def release(self, task_id: str, action: str = "discard") -> list[str]:
        """Release worktree for task_id.

        action:
          "merge"   — attempt fast-forward merge into main branch
          "discard" — remove worktree without merging (gate failed)

        Returns list of conflict file paths (empty on clean merge or discard).
        """
        with _lock:
            wt_path = self._active.pop(task_id, None)
        if wt_path is None:
            log.debug("release: no active worktree for task %s", task_id)
            return []

        conflicts: list[str] = []
        if action == "merge":
            conflicts = self._merge_back(wt_path)
        self._remove_worktree(wt_path)
        return conflicts

    def _merge_back(self, wt_path: Path) -> list[str]:
        """Try to merge worktree changes back into repo_root HEAD.

        Returns list of conflicted paths on failure.
        """
        # Get the worktree commit hash
        result = _run(["git", "rev-parse", "HEAD"], cwd=wt_path)
        if result.returncode != 0:
            log.warning("merge_back: could not get HEAD of worktree %s", wt_path)
            return []
        wt_commit = result.stdout.strip()
        if not wt_commit:
            return []

        # Get current HEAD in repo_root
        result = _run(["git", "rev-parse", "HEAD"], cwd=self.repo_root)
        repo_commit = result.stdout.strip() if result.returncode == 0 else ""

        if wt_commit == repo_commit:
            return []  # No changes

        # Try fast-forward merge
        result = _run(
            ["git", "merge", "--ff-only", "--no-edit", wt_commit],
            cwd=self.repo_root,
        )
        if result.returncode == 0:
            log.debug("merge_back: fast-forward succeeded for %s", wt_path)
            return []

        # Fall back to 3-way merge
        result = _run(
            ["git", "merge", "--no-commit", wt_commit],
            cwd=self.repo_root,
        )
        if result.returncode == 0:
            _run(["git", "merge", "--abort"], cwd=self.repo_root)
            return []

        # Collect conflicted files
        conflict_result = _run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=self.repo_root,
        )
        _run(["git", "merge", "--abort"], cwd=self.repo_root)
        conflicts = [f for f in conflict_result.stdout.splitlines() if f]
        log.warning("merge_back: %d conflict(s) in %s", len(conflicts), wt_path)
        return conflicts

    def _remove_worktree(self, wt_path: Path) -> None:
        result = _run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=self.repo_root,
        )
        if result.returncode != 0:
            shutil.rmtree(str(wt_path), ignore_errors=True)
        _run(["git", "worktree", "prune"], cwd=self.repo_root)
        log.debug("removed worktree %s", wt_path)

    def prune_stale(self) -> int:
        """Remove worktrees older than ttl_hours. Returns count pruned."""
        cutoff = time.time() - self.ttl_hours * 3600
        pruned = 0
        if not self.worktree_base.exists():
            return 0
        for entry in self.worktree_base.iterdir():
            if not entry.is_dir():
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    shutil.rmtree(str(entry), ignore_errors=True)
                    _run(["git", "worktree", "prune"], cwd=self.repo_root)
                    pruned += 1
                    log.debug("pruned stale worktree %s", entry)
                except Exception:
                    log.debug("prune_stale: failed on %s", entry, exc_info=True)
        return pruned
