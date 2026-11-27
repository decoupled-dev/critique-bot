# SPDX-License-Identifier: Apache-2.0
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



# --------------------------------------------------------------------------- the Engine API

import threading  # noqa: E402

from critique_bot.engine import api as engine_api  # noqa: E402


def _edit_call(path: str, old: str, new: str) -> str:
    return ('<tool_call>\n{"tool": "edit_file", "arguments": '
            + __import__("json").dumps({"path": path, "old_string": old, "new_string": new}) + "}\n</tool_call>")


class _ScriptSession:
    """A model session that answers from a shared script; ``gate`` holds a reply back until set."""

    def __init__(self, script: list[str], gate: threading.Event | None = None) -> None:
        self.script = script
        self.gate = gate
        self.sent: list[str] = []
        self.last_detail = None

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        if self.gate is not None:
            self.gate.wait(10)
        return self.script.pop(0) if self.script else "COMPLETED"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeProvider:
    def __init__(self, script: list[str], gate: threading.Event | None = None) -> None:
        self.script = script
        self.gate = gate
        self.sessions: list[_ScriptSession] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def session(self, **_kwargs):
        made = _ScriptSession(self.script, self.gate)
        self.sessions.append(made)
        return made


def _engine_home(root: Path):
    from critique_bot.code_index import IndexStats
    from critique_bot.project_home import init_bot_home

    with mock.patch("critique_bot.project_home.rebuild_index", return_value=IndexStats(files=0, symbols=0)):
        home = init_bot_home(root)
    home.settings.update({"tool_format": "tags", "helper_sessions": 0, "environment_snapshot": False})
    return home


class EngineApiTest(unittest.TestCase):
    def setUp(self) -> None:
        from critique_bot.config import Selectors

        self.tmp = tempfile.TemporaryDirectory()
        # Cleanups run last-in first-out: the folder goes only after the engine has closed
        # (its sessions' threads may still be writing memory and logs into it).
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "note.txt").write_text("one\n", encoding="utf-8")
        self.home = _engine_home(self.root)
        self.config = BotConfig(url="https://chat.example/", selectors=Selectors(prompt_input="textarea",
                                                                                   assistant_messages=".m"),
                                model="Model")
        self.provider = _FakeProvider([])
        patches = [
            mock.patch("critique_bot.model_api.open_provider", lambda *_a, **_k: self.provider),
            mock.patch("critique_bot.engine.setup._open_shell_session", return_value=None),
            mock.patch("critique_bot.engine.setup._remove_old_codegraph"),
            mock.patch("critique_bot.engine.setup._prewarm_shell_environment"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.engine = engine_api.Engine(self.config, self.home)
        self.addCleanup(self.engine.close)

    def _until(self, handle, kind: str, since: int = 0) -> dict:
        for event in handle.events(since, idle_timeout=10):
            if event["type"] == kind:
                return event
        self.fail(f"no {kind} event")

    def test_an_approval_is_a_request_event_answered_once(self) -> None:
        self.provider.script[:] = [_edit_call("note.txt", "one", "two"), "COMPLETED"]
        handle = self.engine.create_session(mode="ask")
        self.assertTrue(handle.id)
        handle.send("change note.txt to two")
        request = self._until(handle, "request")
        self.assertEqual(request["kind"], "permission")
        self.assertEqual(handle.state, engine_api.WAITING)
        handle.answer(request["id"], {"decision": "yes"})
        with self.assertRaises(engine_api.RequestClosed):
            handle.answer(request["id"], {"decision": "no"})
        finished = self._until(handle, "task_finished", request["seq"])
        self.assertEqual(finished["code"], "COMPLETED")
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "two\n")
        kinds = [event["type"] for event in handle.events(0, follow=False)]
        self.assertIn("request_answered", kinds)
        self.assertIn("tool_result", kinds)

    def test_two_watchers_see_the_same_events(self) -> None:
        self.provider.script[:] = ["4."]
        handle = self.engine.create_session(mode="auto")
        seen: list[list[int]] = [[], []]

        def watch(index: int) -> None:
            for event in handle.events(0, idle_timeout=10):
                seen[index].append(event["seq"])
                if event["type"] == "task_finished":
                    return

        threads = [threading.Thread(target=watch, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        handle.send("what is 2 + 2?")
        for thread in threads:
            thread.join(15)
        self.assertTrue(seen[0])
        self.assertEqual(seen[0], seen[1])

    def test_unattended_requests_are_denied_and_cancel_interrupts(self) -> None:
        self.provider.script[:] = [_edit_call("note.txt", "one", "two"), "COMPLETED", "COMPLETED", "COMPLETED"]
        handle = self.engine.create_session(mode="ask", unattended=True)
        handle.send("change note.txt to two")
        answered = self._until(handle, "request_answered")
        self.assertEqual(answered["by"], "unattended")
        self._until(handle, "task_finished")
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "one\n")

        self.provider.script[:] = [_edit_call("note.txt", "one", "three")]
        second = self.engine.create_session(mode="ask")
        second.send("change note.txt to three")
        request = self._until(second, "request")
        self.assertTrue(second.cancel())
        finished = self._until(second, "task_finished", request["seq"])
        self.assertEqual(finished["code"], "INTERRUPTED")
        with self.assertRaises(engine_api.RequestClosed):
            second.answer(request["id"], "yes")

    def test_close_then_open_resumes_the_same_log(self) -> None:
        self.provider.script[:] = ["4."]
        handle = self.engine.create_session(mode="auto")
        handle.send("what is 2 + 2?", close_after=True)
        self._until(handle, "session_closed")
        last = handle.last_seq()
        self.assertIsNone(self.engine.session(handle.id))
        self.assertEqual(self.engine.list_sessions()[0]["id"], handle.id)
        again = self.engine.open_session(handle.id)
        self.assertEqual(again.last_seq(), last)
        self.provider.script[:] = ["6."]
        again.send("and 3 + 3?", close_after=True)
        resumed = self._until(again, "session_resumed", last)
        self.assertGreater(resumed["seq"], last)
        self._until(again, "session_closed", resumed["seq"])

    def test_set_mode_and_bad_replies(self) -> None:
        handle = self.engine.create_session(mode="ask")
        self.assertEqual(handle.set_mode("yolo"), "auto")
        with self.assertRaises(engine_api.BadReply):
            handle.set_mode("sideways")
        with self.assertRaises(engine_api.BadReply):
            engine_api._check_reply("permission", {"decision": "maybe"})
        self.assertEqual(engine_api._check_reply("question", "blue"), {"text": "blue"})
        with self.assertRaises(engine_api.EngineError):
            self.engine.create_session(cwd="/somewhere/else")

    def test_a_front_end_drives_a_session_in_process(self) -> None:
        from critique_bot import ui_api

        class Front(ui_api.NullUI):
            def __init__(self) -> None:
                super().__init__()
                self.asked: list[str] = []
                self.notes: list[str] = []
                self.tasks = ["and now three"]

            def note(self, kind: str, message: str) -> None:
                self.notes.append(message)

            def approve(self, permission, *, risky: str = ""):
                self.asked.append(permission.kind)
                return "yes", ""

            def read_message(self):
                return self.tasks.pop(0) if self.tasks else None

        self.provider.script[:] = [_edit_call("note.txt", "one", "two"), "COMPLETED",
                                   _edit_call("note.txt", "two", "three"), "COMPLETED"]
        handle = self.engine.create_session(mode="ask")
        front = Front()
        handle.send("change note.txt to two")
        code = engine_api.drive_ui(handle.events(0, idle_timeout=10), front, answer=handle.answer, send=handle.send)
        self.assertEqual(code, "COMPLETED")
        self.assertEqual(front.asked, ["edit", "edit"])
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "three\n")
        self.assertTrue(any("Working on" in note for note in front.notes))


if __name__ == "__main__":
    unittest.main()
