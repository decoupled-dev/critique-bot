"""A throwaway git worktree for an agent that writes, so it never touches the user's files.

The worktree starts at HEAD plus the workspace's uncommitted changes (tracked and new
files), committed there as a base. When the agent is done, :meth:`Worktree.diff` is what
it changed on top of that base, as a patch the coordinating agent applies (or not).
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - fixed git argv lists
import uuid
from dataclasses import dataclass
from pathlib import Path

from critique_bot.extensions import runner

__all__ = ["Worktree", "WorktreeError", "create"]

_GIT_ID = ["-c", "user.name=crit", "-c", "user.email=crit@localhost", "-c", "commit.gpgsign=false"]


class WorktreeError(RuntimeError):
    pass


def _git(cwd: Path, *args: str, stdin: bytes | None = None, timeout: float = 120) -> str:
    result = runner.run_argv(["git", *args], cwd=cwd, stdin=stdin, timeout=timeout)
    if result.error or result.exit_code != 0:
        raise WorktreeError(f"git {' '.join(args[:3])} failed: {result.error or result.stderr.strip()[:300]}")
    return result.stdout


@dataclass
class Worktree:
    repo: Path
    path: Path
    base: str

    def diff(self) -> str:
        """Everything the agent changed in the worktree, new files included, as a git patch."""
        _git(self.path, "add", "-A")
        return _git(self.path, "diff", "--cached", "--binary", self.base)

    def remove(self) -> None:
        try:
            _git(self.repo, "worktree", "remove", "--force", str(self.path))
        except WorktreeError:
            shutil.rmtree(self.path, ignore_errors=True)
            try:
                _git(self.repo, "worktree", "prune")
            except WorktreeError:
                pass


def create(workspace: Path, parent: Path) -> Worktree:
    """A detached worktree of ``workspace`` under ``parent`` with the uncommitted changes copied in."""
    workspace = Path(workspace)
    top = Path(_git(workspace, "rev-parse", "--show-toplevel").strip())
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / f"agent-{uuid.uuid4().hex[:10]}"
    _git(top, "worktree", "add", "--detach", str(path), "HEAD")
    try:
        # Tracked changes, then untracked files (not ignored ones), carried over as one base commit.
        patch = _git(top, "diff", "--binary", "HEAD")
        if patch.strip():
            _git(path, "apply", "--whitespace=nowarn", stdin=patch.encode("utf-8"))
        untracked = _git(top, "ls-files", "--others", "--exclude-standard", "-z")
        for name in [item for item in untracked.split("\0") if item]:
            source = top / name
            if source.is_file() and not source.is_symlink():
                target = path / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        _git(path, "add", "-A")
        _git(path, *_GIT_ID, "commit", "--allow-empty", "-q", "--no-verify", "-m", "crit: base for an agent")
        base = _git(path, "rev-parse", "HEAD").strip()
    except BaseException:
        Worktree(top, path, "").remove()
        raise
    return Worktree(top, path, base)


def is_git_repo(path: Path) -> bool:
    try:
        return subprocess.run(  # nosec B603 B607 - fixed argv
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=str(path), capture_output=True, text=True, timeout=10
        ).stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False
