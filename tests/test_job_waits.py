"""Background jobs that are waited on and reported, so the model never has to poll."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from critique_bot import agent_shell
from critique_bot.agent_shell import Shell, ShellSession
from critique_bot.agent_tools import TaskState, ToolContext, execute, finished_job_notes

POSIX = os.name == "posix"


@unittest.skipUnless(POSIX and agent_shell.shutil.which("bash"), "needs bash")
class JobWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        bash = Shell("bash", agent_shell.shutil.which("bash") or "bash", "bash")
        self.session = ShellSession(self.root, bash, available={"bash": bash})
        self.ctx = ToolContext(workspace=self.root, state=TaskState(task="t"), session=self.session,
                               cache_dir=self.root / ".cache")

    def tearDown(self) -> None:
        self.session.close()
        self.tmp.cleanup()

    def test_wakes_on_a_matching_line_without_consuming_output(self) -> None:
        job = self.session.start_background("echo compiling; sleep 0.3; echo 'BUILD FAILED in 3s'; sleep 5")
        started = time.monotonic()
        got = self.session.wait_jobs([job], until=re.compile(r"BUILD (SUCCESSFUL|FAILED)"), timeout=10)
        self.assertEqual(got, (job, "match", "BUILD FAILED in 3s"))
        self.assertLess(time.monotonic() - started, 3)
        text, running, _ = self.session.read_background(job)
        self.assertIn("compiling", text)  # the wait did not eat the output
        self.assertTrue(running)

    def test_exit_ends_the_wait_even_without_a_match(self) -> None:
        job = self.session.start_background("sleep 0.2; exit 3")
        got = self.session.wait_jobs([job], until=re.compile("never printed"), timeout=10)
        self.assertEqual(got[:2], (job, "exit"))
        self.assertEqual(self.session.job_info(job)["exit_code"], 3)

    def test_match_on_the_last_line_without_a_newline(self) -> None:
        job = self.session.start_background("printf 'done: error: boom'")
        got = self.session.wait_jobs([job], until=re.compile("error:"), timeout=10)
        self.assertEqual(got[1], "match")

    def test_timeout_and_cancel(self) -> None:
        job = self.session.start_background("sleep 30")
        self.assertEqual(self.session.wait_jobs([job], timeout=0.3)[1], "timeout")
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        started = time.monotonic()
        self.assertEqual(self.session.wait_jobs([job], timeout=30, cancel=cancel)[1], "cancel")
        self.assertLess(time.monotonic() - started, 5)

    def test_first_of_several_jobs(self) -> None:
        slow = self.session.start_background("sleep 30")
        quick = self.session.start_background("sleep 0.2; echo ok")
        self.assertEqual(self.session.wait_jobs([slow, quick], timeout=10)[:2], (quick, "exit"))

    def test_finished_jobs_are_reported_once(self) -> None:
        job = self.session.start_background("echo hello; exit 2")
        self.session.wait_jobs([job], timeout=10)
        notes = finished_job_notes(self.ctx)
        self.assertEqual(len(notes), 1)
        self.assertIn(f"background job {job} finished: exit 2", notes[0])
        self.assertEqual(finished_job_notes(self.ctx), [])

    def test_a_job_the_model_waited_on_is_not_reported_again(self) -> None:
        job = self.session.start_background("sleep 0.2; echo 'src/A.kt:3: error: unresolved reference'; exit 1")
        result = execute("command_output", {"job_id": job, "until": "exit"}, self.ctx)
        self.assertIn(f"{job}: exit 1", result["output"])
        self.assertIn("unresolved reference", result["output"])
        self.assertEqual(finished_job_notes(self.ctx), [])

    def test_until_any_job(self) -> None:
        self.session.start_background("sleep 30")
        quick = self.session.start_background("sleep 0.2; echo 'Tests passed: 12'")
        result = execute("command_output", {"job_id": "any", "until": "Tests passed"}, self.ctx)
        self.assertIn(quick, result["output"])
        self.assertIn("Tests passed: 12", result["output"])

    def test_long_output_keeps_a_complete_log(self) -> None:
        result = self.session.run("for i in $(seq 1 9000); do echo line $i; done", timeout=60)
        self.assertIn("lines omitted", result.stdout)
        self.assertIsNotNone(result.full_log)
        text = Path(result.full_log).read_text()
        self.assertIn("line 4500\n", text)  # the middle the capture dropped
        self.assertEqual(text.count("\n"), 9000)

    def test_tool_result_points_at_the_complete_log(self) -> None:
        result = execute("run_command", {"command": "for i in $(seq 1 9000); do echo row $i; done"}, self.ctx)
        self.assertIn("FULL OUTPUT saved to", result["output"])
        saved = re.search(r"FULL OUTPUT saved to (\S+):", result["output"]).group(1)
        path = Path(saved) if Path(saved).is_absolute() else self.root / saved
        self.assertIn("row 4500\n", path.read_text())

    def test_short_output_leaves_no_log_behind(self) -> None:
        result = self.session.run("echo hi", timeout=30)
        self.assertIsNone(result.full_log)


@unittest.skipUnless(POSIX and agent_shell.shutil.which("bash"), "needs bash")
class EngineNoteTests(unittest.TestCase):
    def test_the_next_message_says_a_job_finished(self) -> None:
        import json
        from unittest import mock

        import critique_bot.agent as agent

        def call(tool: str, **arguments: object) -> str:
            return "<tool_call>\n" + json.dumps({"tool": tool, "arguments": arguments}) + "\n</tool_call>"

        class Scripted:
            def __init__(self, replies: list[str]) -> None:
                self.sent: list[str] = []
                self.last_detail = None
                self._replies = list(replies)

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                return self._replies.pop(0)

        root = Path(tempfile.mkdtemp()).resolve()
        shell_session = agent._open_shell_session(root, agent.agent_shell.detect_shell(), None)
        if shell_session is None:
            self.skipTest("no persistent shell session")
        self.addCleanup(shell_session.close)
        chat = Scripted([
            call("run_command", command="sleep 1.5; echo built-it; exit 4", background=True),
            call("run_command", command="sleep 2"),
            "COMPLETED",
        ])
        with mock.patch.object(agent, "_approve_prompt", lambda *_a, **_k: ("yes", "")):
            agent.run_agent_loop(
                chat, workspace=root, index_path=None, cache_dir=None, first_task="build it",
                max_rounds=None, max_result_chars=8000, read_message=lambda: None, emit=lambda _t: None,
                approve_mode="auto", ask_user=lambda _q: None, session_shell=shell_session,
            )
        after_second = chat.sent[2]
        self.assertIn("background job b1 finished: exit 4", after_second)
        self.assertNotIn("finished: exit 4", chat.sent[1])

    def test_completed_with_a_running_job_is_questioned_once(self) -> None:
        import json
        from unittest import mock

        import critique_bot.agent as agent

        class Scripted:
            def __init__(self, replies: list[str]) -> None:
                self.sent: list[str] = []
                self.last_detail = None
                self._replies = list(replies)

            def send(self, prompt: str) -> str:
                self.sent.append(prompt)
                return self._replies.pop(0)

        root = Path(tempfile.mkdtemp()).resolve()
        shell_session = agent._open_shell_session(root, agent.agent_shell.detect_shell(), None)
        if shell_session is None:
            self.skipTest("no persistent shell session")
        self.addCleanup(shell_session.close)
        start = "<tool_call>\n" + json.dumps({"tool": "run_command", "arguments": {"command": "sleep 30", "background": True}}) + "\n</tool_call>"
        chat = Scripted([start, "COMPLETED", "COMPLETED"])
        outcome: list[str] = []
        with mock.patch.object(agent, "_approve_prompt", lambda *_a, **_k: ("yes", "")):
            agent.run_agent_loop(
                chat, workspace=root, index_path=None, cache_dir=None, first_task="start the server",
                max_rounds=None, max_result_chars=8000, read_message=lambda: None, emit=lambda _t: None,
                approve_mode="auto", ask_user=lambda _q: None, session_shell=shell_session, outcome=outcome,
            )
        self.assertIn("still running:\n- b1: sleep 30", chat.sent[2])
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertEqual(len(chat.sent), 3)


if __name__ == "__main__":
    unittest.main()
