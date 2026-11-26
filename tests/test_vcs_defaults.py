"""git in run_command: no editor can hang a command, and conflicts come back with the next step."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from critique_bot import agent_shell
from critique_bot.agent_shell import Shell, ShellSession
from critique_bot.agent_tools import TaskState, ToolContext, execute

POSIX = os.name == "posix"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=ENV)


@unittest.skipUnless(POSIX and agent_shell.shutil.which("bash") and agent_shell.shutil.which("git"), "needs bash and git")
class RebaseConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        _git(self.root, "init", "-q", "-b", "main")
        (self.root / "greeting.txt").write_text("hello\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "base")
        _git(self.root, "branch", "feature")
        (self.root / "greeting.txt").write_text("hello\nfrom main\n")
        _git(self.root, "commit", "-qam", "main")
        _git(self.root, "checkout", "-q", "feature")
        (self.root / "greeting.txt").write_text("hello\nfrom feature\n")
        _git(self.root, "commit", "-qam", "feature")
        bash = Shell("bash", agent_shell.shutil.which("bash") or "bash", "bash")
        identity = {key: ENV[key] for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
        self.session = ShellSession(self.root, bash, available={"bash": bash}, env_extra=identity)
        self.ctx = ToolContext(workspace=self.root, state=TaskState(task="t"), session=self.session)

    def tearDown(self) -> None:
        self.session.close()
        self.tmp.cleanup()

    def test_conflict_lists_files_and_continue_needs_no_editor(self) -> None:
        stopped = execute("run_command", {"command": "git rebase main"}, self.ctx)
        self.assertFalse(stopped["ok"])
        self.assertIn("CONFLICTS (1 files): greeting.txt", stopped["output"])
        self.assertIn("git rebase --continue", stopped["output"])
        (self.root / "greeting.txt").write_text("hello\nfrom main\nfrom feature\n")
        done = execute("run_command", {"command": "git add greeting.txt && git rebase --continue", "timeout": 30}, self.ctx)
        self.assertTrue(done["ok"], done)
        log = subprocess.run(["git", "-C", str(self.root), "log", "--format=%s"], capture_output=True, text=True).stdout
        self.assertEqual(log.split(), ["feature", "main", "base"])


if __name__ == "__main__":
    unittest.main()
