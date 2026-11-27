"""Shell defaults: the timeout from settings, explicit longer timeouts, convert-to-background
on timeout (the job stays listed), and the environment snapshot."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from critique_bot import agent_shell, tool_api
from critique_bot.agent_shell import Shell, ShellSession
from critique_bot.agent_tools import (
    DEFAULT_COMMAND_TIMEOUT,
    MAX_COMMAND_TIMEOUT,
    TaskState,
    ToolContext,
    execute,
    normalize_args,
)
from critique_bot.env_snapshot import environment_snapshot, git_summary, test_command


class TimeoutArgumentTests(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertEqual(DEFAULT_COMMAND_TIMEOUT, 60)
        self.assertEqual(MAX_COMMAND_TIMEOUT, 1800)

    def test_milliseconds_and_seconds(self) -> None:
        self.assertEqual(normalize_args("run_command", {"command": "x", "timeout": 120000})["timeout"], 120.0)
        self.assertEqual(normalize_args("run_command", {"command": "x", "timeout": 1200})["timeout"], 1200)
        self.assertEqual(normalize_args("run_command", {"command": "x", "timeout": 5000})["timeout"], MAX_COMMAND_TIMEOUT)
        self.assertEqual(normalize_args("run_command", {"command": "x", "timeout_s": "90"})["timeout"], 90)

    def test_schema_allows_thirty_minutes(self) -> None:
        spec = tool_api.registry().spec("run_command")
        self.assertEqual(spec.parameters["properties"]["timeout"]["maximum"], MAX_COMMAND_TIMEOUT)


@unittest.skipUnless(shutil.which("bash") and sys.platform != "win32", "bash")
class ConvertToBackgroundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        bash = Shell("bash", shutil.which("bash") or "bash", "bash")
        self.session = ShellSession(self.root, bash, available={"bash": bash})

    def tearDown(self) -> None:
        self.session.close()
        self.tmp.cleanup()

    def _ctx(self, settings: dict | None) -> ToolContext:
        return ToolContext(workspace=self.root, state=TaskState(task="t"), session=self.session, settings=settings,
                           cache_dir=self.root / ".cache")

    def test_setting_sets_the_default_and_a_timeout_keeps_the_process(self) -> None:
        ctx = self._ctx({"shell_timeout_s": 1})
        result = execute("run_command", {"command": "echo started; sleep 30"}, ctx)
        self.assertTrue(result["ok"], result)
        self.assertIn("still running after 1s", result["output"])
        self.assertIn("NOT stopped", result["output"])
        jobs = self.session.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["running"])
        listing = execute("command_output", {}, ctx)
        self.assertIn(jobs[0]["id"], listing["output"])
        self.assertIn(jobs[0]["id"], environment_snapshot(self.root, session=self.session))
        killed = execute("kill_command", {"job_id": jobs[0]["id"]}, ctx)
        self.assertTrue(killed["ok"], killed)

    def test_explicit_timeout_wins_over_the_setting(self) -> None:
        ctx = self._ctx({"shell_timeout_s": 1})
        result = execute("run_command", {"command": "sleep 2; echo done", "timeout": 20}, ctx)
        self.assertTrue(result["ok"], result)
        self.assertIn("done", result["output"])
        self.assertEqual(self.session.jobs(), [])


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @unittest.skipUnless(shutil.which("git"), "git")
    def test_snapshot_lists_the_facts(self) -> None:
        for argv in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "a@b.c"],
                     ["git", "config", "user.name", "a"]):
            subprocess.run(argv, cwd=self.root, check=True, capture_output=True)
        (self.root / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "Main.kt").write_text("fun main() {}\n", encoding="utf-8")
        (self.root / "AGENT.md").write_text("Notes\n\n- Test command: pytest -q\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py", "Main.kt"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True, capture_output=True)
        (self.root / "app.py").write_text("x = 2\n", encoding="utf-8")
        text = environment_snapshot(self.root, shell=agent_shell.detect_shell())
        self.assertIn("os: ", text)
        self.assertIn(f"cwd: {self.root}", text)
        self.assertIn("git: branch main; 1 modified, 1 untracked", text)
        self.assertIn("Python (1 files)", text)
        self.assertIn("Kotlin (1 files)", text)
        self.assertIn("test command: pytest -q (from AGENT.md)", text)
        self.assertIn("git", text.split("tools on PATH: ")[1])
        self.assertIn("background jobs: none", text)

    def test_settings_check_command_wins_and_no_git(self) -> None:
        self.assertEqual(test_command(self.root, {"check_command": "make test"}),
                         "make test (check_command in .crit/settings.json)")
        self.assertEqual(test_command(self.root), "")
        self.assertIn("not a git repository", git_summary(self.root)) if shutil.which("git") else None

    def test_exported_by_the_tool_interface(self) -> None:
        self.assertIs(tool_api.environment_snapshot, environment_snapshot)


if __name__ == "__main__":
    unittest.main()
