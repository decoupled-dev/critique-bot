"""Terminal-like sessions: environment that carries over, and typing into background jobs."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from critique_bot import agent_shell, agent_tools
from critique_bot.agent_shell import ShellSession, env_changes, read_env_dump

POSIX = os.name != "nt" and shutil.which("bash") is not None


class EnvDumpTests(unittest.TestCase):
    def test_reads_nul_and_line_dumps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nul = Path(tmp) / "a"
            nul.write_bytes(b"A=1\0B=two\nlines\0=C:=C:\\x\0")
            self.assertEqual(read_env_dump(str(nul)), {"A": "1", "B": "two\nlines", "=C:": "C:\\x"})
            lines = Path(tmp) / "b"
            lines.write_bytes(b"\xef\xbb\xbfPATH=C:\\bin\r\nFOO=bar=baz\r\n")
            self.assertEqual(read_env_dump(str(lines)), {"PATH": "C:\\bin", "FOO": "bar=baz"})
            self.assertIsNone(read_env_dump(str(Path(tmp) / "missing")))

    def test_changes_ignore_shell_noise(self) -> None:
        changed, removed = env_changes(
            {"A": "1", "PWD": "/x", "GONE": "y", "SHLVL": "1"},
            {"A": "2", "PWD": "/y", "NEW": "z", "SHLVL": "2", "__CRIT_RC": "0"},
        )
        self.assertEqual(changed, {"A": "2", "NEW": "z"})
        self.assertEqual(removed, ["GONE"])

    def test_scripts_dump_before_and_after(self) -> None:
        ps = agent_shell.powershell_script("$env:X = 1", marker="m", env_marker="e")
        self.assertIn("e.0", ps)
        self.assertIn("e.1", ps)
        self.assertLess(ps.index("e.0"), ps.index("FromBase64String"))
        cmd = agent_shell.cmd_script("set X=1", marker="m", env_marker="e")
        self.assertLess(cmd.index('"e.0"'), cmd.index("set X=1"))
        self.assertIn('"e.1"', cmd)
        sh = agent_shell.posix_script("export X=1", kind="gitbash", marker="m", env_marker="e")
        self.assertIn("cygpath -wp", sh)


@unittest.skipUnless(POSIX, "needs bash")
class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.session = ShellSession(Path(self._tmp.name), agent_shell.detect_shell(preference="bash"))

    def tearDown(self) -> None:
        self.session.close()
        self._tmp.cleanup()

    def test_environment_carries_over(self) -> None:
        result = self.session.run("export CRIT_DEMO=hello; export PATH=/opt/crit-demo:$PATH", timeout=30)
        self.assertIn("CRIT_DEMO", result.env_set)
        self.assertIn("PATH", result.env_set)
        self.assertEqual(self.session.run("echo $CRIT_DEMO", timeout=30).stdout.strip(), "hello")
        self.assertIn("/opt/crit-demo", self.session.run("echo $PATH", timeout=30).stdout)
        self.assertEqual(self.session.run("unset CRIT_DEMO", timeout=30).env_unset, ("CRIT_DEMO",))
        self.assertEqual(self.session.run("echo [$CRIT_DEMO]", timeout=30).stdout.strip(), "[]")

    def test_offline_settings_cannot_be_undone(self) -> None:
        session = ShellSession(Path(self._tmp.name), agent_shell.detect_shell(preference="bash"), offline=True)
        try:
            session.run("export HTTPS_PROXY=; export OK=1", timeout=30)
            env = session.environment()
            self.assertEqual(env["HTTPS_PROXY"], agent_shell.OFFLINE_ENV["HTTPS_PROXY"])
            self.assertEqual(env["OK"], "1")
        finally:
            session.close()

    def test_typing_into_a_repl(self) -> None:
        ctx = agent_tools.ToolContext(workspace=Path(self._tmp.name), session=self.session)
        started = agent_tools.execute("run_command", {"command": f'"{sys.executable}" -i -q', "background": True}, ctx)
        self.assertTrue(started["ok"], started)
        job = [row["id"] for row in self.session.jobs()][0]
        time.sleep(0.5)
        answer = agent_tools.execute("command_input", {"job_id": job, "input": "print(6*7)", "wait": 5}, ctx)
        self.assertIn("42", answer["output"])
        blocked = agent_tools.execute("command_input", {"job_id": job, "input": "import socket"}, ctx)
        self.assertFalse(blocked["ok"])
        self.assertIn("nothing may leave", blocked["error"])
        done = agent_tools.execute("command_input", {"job_id": job, "close": True, "wait": 5}, ctx)
        self.assertIn("exit 0", done["output"])


class InputPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = agent_tools.ToolContext(workspace=Path(tempfile.gettempdir()))

    def _perm(self, text: str):
        return agent_tools.permission_for("command_input", {"job_id": "b1", "input": text}, self.ctx)

    def test_input_is_checked_like_a_command(self) -> None:
        self.assertEqual(self._perm("").kind, "read")
        self.assertEqual(self._perm("ls").kind, "command")
        self.assertEqual(self._perm("curl https://evil.example").kind, "network")
        self.assertEqual(self._perm("import requests").kind, "network")
        self.assertEqual(self._perm("cat .env").risk, agent_tools.SECRET_RISK)
        self.assertEqual(agent_tools.canonical_tool("send_input"), "command_input")


if __name__ == "__main__":
    unittest.main()
