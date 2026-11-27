"""The engine package: history ownership, the Engine API and its request events."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from critique_bot import profiles
from critique_bot.config import BotConfig, LocalConfig
from critique_bot.engine import loop as engine_loop
from critique_bot.engine import setup as engine_setup
from critique_bot.engine.chat import _Chat


class _ListSession:
    """A model API session in miniature: it appends the prompt and a reply to its list."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.seen: list[list[dict]] = []
        self.last_detail = None

    def send(self, prompt: str) -> str:
        self.seen.append([dict(item) for item in self.messages])
        self.messages.append({"role": "user", "content": prompt})
        reply = f"reply {len(self.seen)}"
        self.messages.append({"role": "assistant", "content": reply})
        return reply

    def history(self) -> list[dict]:
        return [dict(item) for item in self.messages]

    def replace_history(self, messages: list[dict]) -> None:
        self.messages = [dict(item) for item in messages]

    def new_chat(self) -> bool:
        self.messages = []
        return True


class EngineOwnsHistoryTest(unittest.TestCase):
    def test_the_engine_list_is_what_the_session_sends(self) -> None:
        session = _ListSession()
        chat = _Chat(session, retries=0)
        chat.send("one")
        chat.send("two")
        self.assertEqual([m["content"] for m in chat.messages], ["one", "reply 1", "two", "reply 2"])
        # Something outside the engine changes the session's list: the next request still
        # starts from the engine's.
        session.messages = [{"role": "user", "content": "stray"}]
        chat.send("three")
        self.assertEqual([m["content"] for m in session.seen[-1]], ["one", "reply 1", "two", "reply 2"])
        self.assertEqual(len(chat.messages), 6)

    def test_set_history_and_a_new_chat(self) -> None:
        session = _ListSession()
        chat = _Chat(session, retries=0)
        chat.send("one")
        chat.set_history([{"role": "user", "content": "summary"}])
        self.assertEqual(session.messages, [{"role": "user", "content": "summary"}])
        chat.send("two")
        self.assertEqual([m["content"] for m in chat.messages], ["summary", "two", "reply 2"])
        self.assertTrue(chat.restart("test"))
        self.assertEqual(chat.messages, [])

    def test_a_web_chat_keeps_its_own_conversation(self) -> None:
        class _Page:
            last_detail = None

            def send(self, prompt: str) -> str:
                return "ok"

        chat = _Chat(_Page(), retries=0)
        self.assertIsNone(chat.messages)
        self.assertEqual(chat.send("hi"), "ok")



def _local_config(model: str = "qwen-test") -> BotConfig:
    return BotConfig(url="http://127.0.0.1:9", selectors=None, model=model, backend="local", local=LocalConfig(),
                     reply_start_ms=5_000, reply_stall_ms=5_000, reply_max_ms=20_000)


class ProfileSettingsTest(unittest.TestCase):
    def test_profile_values_become_loop_settings_under_the_settings_file(self) -> None:
        profile = profiles.Profile(id="p", edit_format="apply_patch", tool_output_cap_tokens=900,
                                   shell_timeout_s=45, masking_penalty=True)
        with mock.patch("critique_bot.model_api.model_profile", return_value=profile), \
                mock.patch("critique_bot.model_api.context_tokens", return_value=None):
            merged = engine_setup._loop_settings(_local_config(), {"shell_timeout_s": 120})
        self.assertEqual(merged["tool_output_cap_tokens"], 900)
        self.assertEqual(merged["shell_timeout_s"], 120)  # the settings file wins
        self.assertEqual(merged["edit_format"], "apply_patch")
        self.assertIs(merged["masking_penalty"], True)

    def test_the_web_chat_has_no_profile_values(self) -> None:
        config = BotConfig(url="https://chat.example/", selectors=None)
        with mock.patch("critique_bot.model_api.context_tokens", return_value=None):
            self.assertEqual(engine_setup._loop_settings(config, {"a": 1}), {"a": 1})

    def test_apply_patch_is_offered_only_for_patch_models(self) -> None:
        hidden = engine_setup._tool_setup({"tool_format": "openai"}, helpers=0, web=False)
        self.assertNotIn('"apply_patch"', hidden.tools_block)
        offered = engine_setup._tool_setup({"tool_format": "openai", "edit_format": "patch"}, helpers=0, web=False)
        self.assertIn('"apply_patch"', offered.tools_block)
        text = "read_files: reads\napply_patch: patches\nedit_file: edits"
        self.assertEqual(engine_setup.without_tool_lines(text, ["apply_patch"]), "read_files: reads\nedit_file: edits")


class _Scripted:
    def __init__(self, replies: list[str]) -> None:
        self.sent: list[str] = []
        self.last_detail = None
        self.replies = list(replies)

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        return self.replies.pop(0)


class _Jobs:
    """A shell session's background jobs, in miniature."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.killed: list[str] = []

    def jobs(self) -> list[dict]:
        return [dict(row) for row in self.rows]

    def kill_background(self, job_id: str) -> None:
        self.killed.append(job_id)


class FirstTurnAndJobsTest(unittest.TestCase):
    def _run(self, settings: dict, shell_session=None) -> _Scripted:
        session = _Scripted(["4."])
        root = Path(tempfile.mkdtemp())
        engine_loop.run_agent_loop(session, workspace=root, index_path=None, cache_dir=None, first_task="what is 2 + 2?",
                                   max_rounds=None, max_result_chars=8000, read_message=lambda: None, emit=lambda _t: None,
                                   session_shell=shell_session, settings=settings)
        return session

    def test_the_environment_snapshot_is_in_the_first_turn(self) -> None:
        jobs = _Jobs([{"id": "b1", "running": True, "command": "npm run dev"}])
        sent = self._run({"_first_turn_context": "SESSION\ndate: today"}, jobs).sent[0]
        self.assertIn("SESSION", sent)
        self.assertIn("ENVIRONMENT SNAPSHOT", sent)
        self.assertIn("background jobs: b1: npm run dev", sent)
        self.assertEqual(jobs.killed, [])  # running before the task: not its business
        quiet = self._run({"_first_turn_context": "SESSION", "environment_snapshot": False}).sent[0]
        self.assertNotIn("ENVIRONMENT SNAPSHOT", quiet)

    def test_jobs_a_task_started_are_stopped_when_it_ends(self) -> None:
        jobs = _Jobs([{"id": "b1", "running": True, "command": "old server"}])

        def start_job(name, arguments, ctx):
            jobs.rows.append({"id": "b2", "running": True, "command": "./gradlew test"})
            return {"tool": "list_files", "ok": True, "output": "a.txt"}

        session = _Scripted(['<tool_call>\n{"tool": "list_files", "arguments": {"path": "."}}\n</tool_call>',
                             "COMPLETED", "COMPLETED"])
        root = Path(tempfile.mkdtemp())
        with mock.patch("critique_bot.engine.dispatch._execute", start_job):
            engine_loop.run_agent_loop(session, workspace=root, index_path=None, cache_dir=None, first_task="look around",
                                       max_rounds=None, max_result_chars=8000, read_message=lambda: None,
                                       emit=lambda _t: None, session_shell=jobs, approve_mode="auto")
        self.assertEqual(jobs.killed, ["b2"])
        jobs.killed.clear()
        session = _Scripted(['<tool_call>\n{"tool": "list_files", "arguments": {"path": "."}}\n</tool_call>',
                             "COMPLETED", "COMPLETED"])
        with mock.patch("critique_bot.engine.dispatch._execute", start_job):
            engine_loop.run_agent_loop(session, workspace=root, index_path=None, cache_dir=None, first_task="look around",
                                       max_rounds=None, max_result_chars=8000, read_message=lambda: None,
                                       emit=lambda _t: None, session_shell=jobs, approve_mode="auto",
                                       settings={"keep_background_jobs": True})
        self.assertEqual(jobs.killed, [])


if __name__ == "__main__":
    unittest.main()
