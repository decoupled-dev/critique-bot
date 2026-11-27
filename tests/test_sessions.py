"""Stage 3: the session event log, its projection, resume after a hard kill, fork, redaction,
the byte-stable prompt prefix, and the session commands."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from critique_bot import agent, session_log, ui_api
from critique_bot.agent import TOOL_FORMAT_NATIVE, _tool_setup, run_agent_loop
from critique_bot.local_model import LocalModelClient, LocalModelSession
from critique_bot.session_log import SessionLog, history_from_events, read_events
from test_native_tools import _call_chunks, _config, _Server

SECRET = "sk-live-abcdefghijklmnopqrstuvwxyz0123456789"


def _text(content: str) -> list[dict]:
    return [{"choices": [{"index": 0, "delta": {"content": content}}]}]


def _read(call_id: str, path: str) -> list[dict]:
    return _call_chunks([(call_id, "read_files", {"paths": [path]})])


class _UI(ui_api.NullUI):
    """A headless UI whose mode and role the test changes between tasks."""

    def __init__(self) -> None:
        super().__init__()
        self.mode = "auto"
        self.role: str | None = None
        self.session_request: dict | None = None

    def approve_mode(self) -> str:
        return self.mode

    def take_role_change(self) -> str | None:
        role, self.role = self.role, None
        return role

    def take_session_change(self) -> dict | None:
        request, self.session_request = self.session_request, None
        return request


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _Server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        self.server.replies, self.server.requests = [], []
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "note.txt").write_text(f"hello\nAPI_KEY={SECRET}\n", encoding="utf-8")
        (self.root / "other.txt").write_text("other file\n", encoding="utf-8")
        self.sessions = self.root / ".crit" / "sessions"
        self.setup = _tool_setup({}, helpers=0, web=False, native_ok=True)

    def tearDown(self) -> None:
        agent.use_ui(None)
        for slog in getattr(self, "_logs", []):
            slog.close()
        self.tmp.cleanup()

    def track(self, slog):
        self.__dict__.setdefault("_logs", []).append(slog)
        return slog

    def session(self) -> LocalModelSession:
        return LocalModelSession(LocalModelClient(_config(self.server.url)))

    def run_loop(self, session, slog, task: str, *, tasks=(), resume=False, window=32_000, **extra):
        pending = list(tasks)
        outcome: list[str] = []
        settings = {"reply_retries": 0, "tool_format": TOOL_FORMAT_NATIVE, "_native_tools": self.setup.native_tools,
                    "_context_window": window, **extra.pop("settings", {})}
        run_agent_loop(
            session, workspace=self.root, index_path=None, cache_dir=None, first_task=task, max_rounds=8,
            max_result_chars=8000, seed="INSTRUCTIONS: use the tools.\n\nReply with exactly READY.",
            read_message=lambda: pending.pop(0) if pending else None, emit=lambda _t: None,
            ask_user=lambda _q: None, outcome=outcome, settings=settings, session_log=slog,
            approve_mode="auto", resume=resume, **extra,
        )
        return outcome


class EventLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_events_carry_seq_ts_turn_and_long_strings_go_to_blobs(self) -> None:
        slog = SessionLog.create(self.dir, title="a task")
        slog.write("user_message", text="hi", task=True)
        long = "x" * (session_log.BLOB_MIN_CHARS + 10)
        slog.write("assistant_turn", text=long)
        slog.write("tool_result", tool="read_files", ok=True, result={"output": long})
        slog.close("COMPLETED")
        events = read_events(slog.folder)
        self.assertEqual([e["seq"] for e in events], [1, 2, 3])
        self.assertEqual([e["turn"] for e in events], [0, 1, 1])
        self.assertTrue(all(e["ts"].endswith("Z") for e in events))
        ref = events[1]["text"]
        self.assertIn("$blob", ref)
        self.assertEqual(session_log.load_blob(slog.folder, ref), long)
        self.assertEqual(events[2]["result"]["output"]["$blob"], ref["$blob"])  # stored once
        self.assertEqual(len(list((slog.folder / "blobs").iterdir())), 1)
        self.assertEqual(session_log.read_meta(slog.folder)["status"], "COMPLETED")

    def test_secrets_are_redacted_in_events_and_blobs(self) -> None:
        slog = SessionLog.create(self.dir)
        slog.write("tool_result", tool="run_command", ok=True, summary=f"token={SECRET}",
                   result={"output": f"API_KEY={SECRET}\n" + "y" * 5000})
        slog.close()
        raw = (slog.folder / "events.jsonl").read_text(encoding="utf-8")
        blobs = "".join(path.read_text(encoding="utf-8") for path in (slog.folder / "blobs").iterdir())
        self.assertNotIn(SECRET, raw)
        self.assertNotIn(SECRET, blobs)
        self.assertIn("REDACTED", raw + blobs)

    def test_a_half_written_last_line_is_skipped_and_appending_goes_on(self) -> None:
        slog = SessionLog.create(self.dir)
        slog.write("user_message", text="one", task=True)
        slog.close()
        with open(slog.folder / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write('{"seq": 2, "type": "assistant_tu')  # killed mid-write
        self.assertEqual(len(read_events(slog.folder)), 1)
        again = SessionLog.open(slog.folder)
        again.write("user_message", text="two", task=True)
        again.close()
        events = read_events(slog.folder)
        self.assertEqual([e["text"] for e in events], ["one", "two"])
        self.assertEqual(events[-1]["seq"], 2)

    def test_unknown_event_types_are_refused(self) -> None:
        slog = SessionLog.create(self.dir)
        with self.assertRaises(ValueError):
            slog.write("made_up")
        slog.close()


class ProjectionTests(unittest.TestCase):
    def test_wire_masking_summary_and_new_chat_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            slog = SessionLog.create(Path(tmp))
            user = {"role": "user", "content": "PREFIX\n\ntask"}
            call = {"id": "c1", "type": "function", "function": {"name": "read_files", "arguments": '{"paths": ["a.py"]}'}}
            turn1 = [user, {"role": "assistant", "content": "", "tool_calls": [call]}]
            original = "line\n" * 900
            turn2 = [{"role": "tool", "tool_call_id": "c1", "content": original}, {"role": "assistant", "content": "done"}]
            slog.write("assistant_turn", text="x", wire=turn1, tool_calls=[call])
            slog.write("tool_result", tool="read_files", call_id="c1", ok=True)
            slog.write("assistant_turn", text="done", wire=turn2)
            events = read_events(slog.folder)
            self.assertEqual(history_from_events(events, slog.folder).messages, turn1 + turn2)
            blob = slog.put_blob(original)
            slog.write("compaction", strategy="mask", masked=[{"index": 2, "text": "[elided: a.py]", "blob": blob}])
            masked = history_from_events(read_events(slog.folder), slog.folder).messages
            self.assertEqual(masked[2]["content"], "[elided: a.py]")
            self.assertEqual(session_log.load_blob(slog.folder, blob), original)  # recoverable
            summary = [{"role": "user", "content": "PREFIX\n\ntask\n\nCONTEXT SUMMARY (...)"}, turn2[1]]
            slog.write("compaction", strategy="summarize", history=summary)
            self.assertEqual(history_from_events(read_events(slog.folder), slog.folder).messages, summary)
            slog.write("compaction", strategy="new_chat", reason="asked")
            slog.close()
            projection = history_from_events(read_events(slog.folder), slog.folder)
            self.assertEqual(projection.messages, [])
            self.assertTrue(projection.has_wire)

    def test_a_request_without_a_reply_adds_nothing(self) -> None:
        events = [
            {"seq": 1, "type": "assistant_turn", "wire": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]},
            {"seq": 2, "type": "model_request", "chars": 3},
        ]
        self.assertEqual(len(history_from_events(events).messages), 2)


class EngineLogTests(_Base):
    def test_the_projection_equals_the_session_and_the_log_has_no_secret(self) -> None:
        self.server.replies = [_read("r1", "note.txt"), _text("note.txt has a key. COMPLETED")]
        session = self.session()
        slog = self.track(SessionLog.create(self.sessions, title="t"))
        self.assertEqual(self.run_loop(session, slog, "What is in note.txt?"), ["COMPLETED"])
        events = read_events(slog.folder)
        kinds = [e["type"] for e in events]
        for kind in ("system_prompt", "tools_declared", "user_message", "model_request", "assistant_turn",
                     "tool_started", "tool_result", "task_finished"):
            self.assertIn(kind, kinds)
        self.assertEqual(history_from_events(events, slog.folder).messages, session.history())
        everything = (slog.folder / "events.jsonl").read_text(encoding="utf-8")
        if (slog.folder / "blobs").exists():
            everything += "".join(p.read_text(encoding="utf-8") for p in (slog.folder / "blobs").iterdir())
        self.assertNotIn(SECRET, everything)

    def test_resume_after_a_hard_kill_continues_from_the_last_complete_turn(self) -> None:
        self.server.replies = [
            _read("r1", "other.txt"), _text("other.txt is short. COMPLETED"),
            _read("r2", "note.txt"),
        ]
        slog = self.track(SessionLog.create(self.sessions, title="t"))
        killed = mock.patch("critique_bot.engine.dispatch._execute", side_effect=SystemExit("killed"))
        real = agent._execute
        calls = {"n": 0}

        def execute(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise SystemExit("killed")  # a hard kill while the second task's tool runs
            return real(*args, **kwargs)

        killed = mock.patch("critique_bot.engine.dispatch._execute", side_effect=execute)
        with killed, self.assertRaises(SystemExit):
            self.run_loop(self.session(), slog, "Read other.txt", tasks=["Now read note.txt"])
        with open(slog.folder / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write('{"seq": 99, "type": "tool_res')
        before = history_from_events(read_events(slog.folder), slog.folder).messages
        self.assertEqual(before[-1]["tool_calls"][0]["id"], "r2")

        self.server.requests = []
        self.server.replies = [_text("Picked up again. COMPLETED")]
        resumed = self.track(SessionLog.open(slog.folder))
        session = self.session()
        self.assertEqual(self.run_loop(session, resumed, "go on", resume=True), ["COMPLETED"])
        sent = self.server.requests[0]["messages"]
        self.assertEqual(sent[: len(before)], before)
        self.assertEqual(sent[len(before)]["role"], "tool")  # the unfinished call gets an answer
        self.assertIn("resumed", sent[-1]["content"])
        self.assertIn("go on", sent[-1]["content"])
        self.assertNotIn("INSTRUCTIONS", sent[-1]["content"])  # the prefix is not sent twice
        kinds = [e["type"] for e in read_events(slog.folder)]
        self.assertIn("session_resumed", kinds)
        self.assertEqual(history_from_events(read_events(slog.folder), slog.folder).messages, session.history())

    def test_fork_at_seq_gives_the_same_history_up_to_that_event(self) -> None:
        self.server.replies = [_read("r1", "other.txt"), _text("ok. COMPLETED"), _read("r2", "note.txt"), _text("done. COMPLETED")]
        slog = self.track(SessionLog.create(self.sessions, title="t"))
        self.run_loop(self.session(), slog, "Read other.txt", tasks=["Read note.txt"])
        slog.close("COMPLETED")
        events = read_events(slog.folder)
        for at in (1, 5, events[len(events) // 2]["seq"], events[-1]["seq"]):
            fork = session_log.fork_session(self.sessions, slog.id, at=at)
            self.assertEqual(session_log.read_meta(fork.folder)["parent"], slog.id)
            self.assertEqual(
                history_from_events(read_events(fork.folder), fork.folder).messages,
                history_from_events(read_events(slog.folder, upto=at), slog.folder).messages,
            )

    def test_a_session_without_a_message_list_resumes_with_a_summary(self) -> None:
        class Web:
            def __init__(self, replies):
                self.sent, self.replies, self.last_detail = [], list(replies), None

            def send(self, prompt):
                self.sent.append(prompt)
                return self.replies.pop(0)

        slog = self.track(SessionLog.create(self.sessions))
        slog.write("user_message", text="Fix the parser bug", task=True)
        slog.write("model_request", payload="Fix the parser bug", chars=18)
        slog.write("assistant_turn", text="Looking at parser.py first.")
        web = Web(["Carrying on. COMPLETED"] * 6)
        outcome: list[str] = []
        run_agent_loop(web, workspace=self.root, index_path=None, cache_dir=None, first_task="continue",
                       max_rounds=3, max_result_chars=8000, seed="SEED TEXT", read_message=lambda: None,
                       emit=lambda _t: None, outcome=outcome, session_log=self.track(SessionLog.open(slog.folder)),
                       resume=True, settings={"tool_format": "openai"})
        self.assertIn("RESUMED SESSION", web.sent[0])
        self.assertIn("Fix the parser bug", web.sent[0])
        self.assertIn("SEED TEXT", web.sent[0])


class PrefixStabilityTests(_Base):
    def test_the_prefix_and_tools_stay_byte_identical_across_20_turns_with_mode_and_role_changes(self) -> None:
        ui = _UI()
        agent.use_ui(ui)
        tasks = [f"Task {n}: read other.txt" for n in range(2, 11)]
        replies = []
        for n in range(10):
            replies += [_read(f"r{n}", "other.txt"), _text(f"Done {n}. COMPLETED")]
        self.server.replies = replies
        changes = iter(["edits", "ask", "auto", "edits", "ask", "auto", "edits", "ask", "auto"])
        reader_tasks = list(tasks)

        def read_message():
            if not reader_tasks:
                return None
            ui.mode = next(changes)
            if len(reader_tasks) == 5:
                ui.role = "test-engineer"
            return reader_tasks.pop(0)

        slog = self.track(SessionLog.create(self.sessions))
        outcome: list[str] = []
        session = self.session()
        run_agent_loop(
            session, workspace=self.root, index_path=None, cache_dir=None, first_task="Task 1: read other.txt",
            max_rounds=8, max_result_chars=8000, seed="INSTRUCTIONS: use the tools.\n\nReply with exactly READY.",
            read_message=read_message, emit=lambda _t: None, outcome=outcome, session_log=slog, approve_mode="auto",
            settings={"reply_retries": 0, "tool_format": TOOL_FORMAT_NATIVE, "_native_tools": self.setup.native_tools,
                      "_context_window": 200_000},
        )
        requests = self.server.requests
        self.assertEqual(len(requests), 20)
        firsts = {json.dumps(r["messages"][0], sort_keys=True) for r in requests}
        self.assertEqual(len(firsts), 1)  # the prefix message never changed
        self.assertEqual(len({json.dumps(r["tools"], sort_keys=True) for r in requests}), 1)
        for earlier, later in zip(requests, requests[1:]):  # append-only: each request extends the last
            self.assertEqual(later["messages"][: len(earlier["messages"])], earlier["messages"])
        everything = json.dumps(requests[-1]["messages"])
        self.assertIn("MODE CHANGED: edits", everything)
        self.assertIn("ROLE CHANGED", everything)
        events = read_events(slog.folder)
        self.assertEqual(sum(e["type"] == "mode_changed" for e in events), 9)
        self.assertEqual(sum(e["type"] == "role_changed" for e in events), 1)
        hashes = {e.get("prefix_hash") for e in events if e["type"] == "model_request" and e.get("prefix_hash")}
        self.assertEqual(len(hashes), 1)
        self.assertTrue(all(e.get("prefix_ok") for e in events if e["type"] == "model_request" and "prefix_ok" in e))


class SessionFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_list_find_prune_and_export(self) -> None:
        old = SessionLog.create(self.dir, session_id="20200101T000000Z-aaaa", title="old one")
        old.write("user_message", text="old", task=True)
        old.close("COMPLETED")
        meta = session_log.read_meta(old.folder)
        meta["updated"] = "2020-01-01T00:00:00.000Z"
        (old.folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        new = SessionLog.create(self.dir, title="new one")
        new.write("user_message", text=f"use {SECRET} in {self.dir}", task=True)
        new.close("COMPLETED")
        rows = session_log.list_sessions(self.dir)
        self.assertEqual([r.title for r in rows], ["new one", "old one"])
        self.assertEqual(session_log.find_session(self.dir, None), new.folder)
        self.assertEqual(session_log.find_session(self.dir, "2020"), old.folder)
        with self.assertRaises(session_log.SessionNotFound):
            session_log.find_session(self.dir, "nope")
        exported = session_log.export_session(new.folder, redacted=True)
        self.assertTrue(exported["redacted"])
        self.assertNotIn(SECRET, json.dumps(exported))
        removed = session_log.prune_sessions(self.dir, keep_days=30, keep_count=200,
                                             now=datetime.now(timezone.utc) + timedelta(days=1))
        self.assertEqual(removed, [old.id])
        self.assertFalse(old.folder.exists())

    def test_a_killed_session_lists_as_killed(self) -> None:
        slog = SessionLog.create(self.dir)
        slog.write("user_message", text="x", task=True)
        slog.close()
        meta = session_log.read_meta(slog.folder)
        meta["status"] = "running"
        meta["pid"] = 999_999_999
        (slog.folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        self.assertEqual(session_log.list_sessions(self.dir)[0].status, "killed")

    def test_show_renders_a_transcript(self) -> None:
        slog = SessionLog.create(self.dir)
        slog.write("user_message", text="Fix it", task=True)
        slog.write("assistant_turn", text="Reading.", usage={"prompt": 10, "completion": 2})
        slog.write("tool_started", tool="read_files", summary="Reading a.py")
        slog.write("tool_result", tool="read_files", ok=True, summary="1|x")
        slog.write("task_finished", code="COMPLETED")
        slog.close()
        text = session_log.render_transcript(read_events(slog.folder), slog.folder)
        for part in ("## Task", "Fix it", "Reading.", "read_files", "COMPLETED"):
            self.assertIn(part, text)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        from critique_bot.project_home import init_bot_home

        self.home = init_bot_home(self.root)
        self.slog = SessionLog.create(self.home.sessions_dir, title="the first task")
        self.slog.write("user_message", text="the first task", task=True)
        self.slog.write("assistant_turn", text="ok")
        self.slog.close("COMPLETED")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sessions_list_show_export_prune(self) -> None:
        from critique_bot import sessions_cmd

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(sessions_cmd.main_sessions(["--repo-dir", str(self.root)]), 0)
        self.assertIn(self.slog.id, out.getvalue())
        self.assertIn("the first task", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(sessions_cmd.main_sessions(["--repo-dir", str(self.root), "show", self.slog.id[-4:]]), 0)
        self.assertIn("the first task", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(sessions_cmd.main_sessions(["--repo-dir", str(self.root), "export", self.slog.id, "--redacted"]), 0)
        self.assertTrue(json.loads(out.getvalue())["redacted"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(sessions_cmd.main_sessions(["--repo-dir", str(self.root), "prune", "--dry-run"]), 0)
        self.assertIn("would remove 0", out.getvalue())

    def test_resume_and_fork_build_the_agent_command_line(self) -> None:
        from critique_bot import sessions_cmd

        self.assertEqual(sessions_cmd.split_resume(["--last", "-p", "go"]), ("last", ["-p", "go"]))
        self.assertEqual(sessions_cmd.split_fork(["abc", "--at", "7", "--yes"]), ("abc", 7, ["--yes"]))
        argv = sessions_cmd.resume_argv(["--repo-dir", str(self.root)])
        self.assertEqual(argv[-2:], ["--resume", self.slog.id])
        found = sessions_cmd.fork_argv([self.slog.id, "--at", "1", "--repo-dir", str(self.root)])
        rest, line = found
        self.assertIn("into", line)
        self.assertEqual(rest[-2], "--resume")
        fork_id = rest[-1]
        self.assertEqual(len(read_events(self.home.sessions_dir / fork_id)), 1)
        self.assertIsInstance(sessions_cmd.resume_argv(["nope", "--repo-dir", str(self.root)]), str)

    def test_tui_resume_and_fork_ask_the_loop_to_switch(self) -> None:
        from critique_bot import agent_ui

        agent_ui.reset()
        agent_ui.set_session(self.home.sessions_dir, "")
        with mock.patch.object(agent_ui, "note"):
            agent_ui.handle_command("/resume", self.slog.id)
            self.assertEqual(agent_ui.take_session_change(), {"folder": str(self.slog.folder)})
            self.assertIsNone(agent_ui.take_session_change())
            agent_ui.handle_command("/fork", f"{self.slog.id} --at 1")
            request = agent_ui.take_session_change()
        self.assertNotEqual(request["folder"], str(self.slog.folder))
        self.assertEqual(session_log.read_meta(Path(request["folder"]))["parent"], self.slog.id)
        agent_ui.reset()

    def test_status_shows_the_cache_hit_ratio(self) -> None:
        from critique_bot import agent_ui, run_stats

        run_stats.reset()
        self.assertEqual(agent_ui.cache_hit_line(), "")
        run_stats.add_usage(1000, 10, 800)
        self.assertIn("80%", agent_ui.cache_hit_line())
        run_stats.reset()


if __name__ == "__main__":
    unittest.main()
