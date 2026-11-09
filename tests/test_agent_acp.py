"""crit --acp: the Agent Client Protocol front end (Android Studio, JetBrains IDEs, Zed)."""

from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from critique_bot import agent, agent_acp, agent_ui
from critique_bot.agent import run_agent_loop
from critique_bot.agent_tools import Permission


def _call(tool: str, **arguments: object) -> str:
    return "<tool_call>\n" + json.dumps({"tool": tool, "arguments": arguments}) + "\n</tool_call>"


class _Scripted:
    def __init__(self, replies: list[str]) -> None:
        self.sent: list[str] = []
        self.last_detail = None
        self._replies = list(replies)

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        return self._replies.pop(0)


class _Client:
    """The IDE end of the pipes: sends JSON-RPC, collects what the agent writes."""

    def __init__(self) -> None:
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        self._to_agent = os.fdopen(in_w, "w", encoding="utf-8", buffering=1)
        self._from_agent = os.fdopen(out_r, "r", encoding="utf-8")
        self.messages: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.seen: list[dict[str, Any]] = []
        self._ids = iter(range(1, 10_000))
        threading.Thread(target=self._pump, daemon=True).start()
        agent_acp.start(
            stdin=os.fdopen(in_r, "r", encoding="utf-8"),
            stdout=os.fdopen(out_w, "w", encoding="utf-8", buffering=1),
        )

    def _pump(self) -> None:
        with self._from_agent:
            for line in self._from_agent:
                self.messages.put(json.loads(line))

    def write(self, **message: Any) -> None:
        self._to_agent.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self._to_agent.flush()

    def request(self, method: str, **params: Any) -> int:
        request_id = next(self._ids)
        self.write(id=request_id, method=method, params=params)
        return request_id

    def notify(self, method: str, **params: Any) -> None:
        self.write(method=method, params=params)

    def expect(self, match, what: str, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no {what}; saw {self.seen}")
            try:
                message = self.messages.get(timeout=left)
            except queue.Empty:
                continue
            self.seen.append(message)
            if match(message):
                return message

    def result(self, request_id: int) -> dict[str, Any]:
        return self.expect(lambda m: m.get("id") == request_id and "method" not in m, f"answer to {request_id}")

    def update(self, kind: str) -> dict[str, Any]:
        message = self.expect(
            lambda m: m.get("method") == "session/update" and m["params"]["update"]["sessionUpdate"] == kind,
            f"{kind} update",
        )
        return message["params"]["update"]

    def agent_request(self, method: str) -> dict[str, Any]:
        return self.expect(lambda m: m.get("method") == method and "id" in m, method)

    def close(self) -> None:
        try:
            self._to_agent.close()
        except OSError:
            pass


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        agent_acp.reset_for_tests()
        self.client = _Client()
        agent.use_ui(agent_acp)
        self.root = Path(tempfile.mkdtemp()).resolve()
        (self.root / "note.txt").write_text("one\n", encoding="utf-8")
        agent_acp.configure(workspace=self.root, approve_mode="ask")

    def tearDown(self) -> None:
        self.client.close()
        # Let the reader see the closed pipe, so waits this test left running return None now.
        deadline = time.monotonic() + 5
        while not agent_acp._state.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        out = agent_acp._out
        if out is not None:
            out.close()
        agent.use_ui(agent_ui)
        agent_acp.reset_for_tests()

    def run_in_thread(self, fn, *args, **kwargs) -> tuple[threading.Thread, list[Any]]:
        box: list[Any] = []

        def target() -> None:
            try:
                box.append(fn(*args, **kwargs))
            except BaseException as exc:  # surfaced by the test
                box.append(exc)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, box

    def open_session(self) -> tuple[str, threading.Thread, list[Any]]:
        """Handshake, then a read_message waiting for the first prompt."""
        init = self.client.request("initialize", protocolVersion=1, clientCapabilities={})
        self.assertEqual(self.client.result(init)["result"]["protocolVersion"], 1)
        new = self.client.request("session/new", cwd=str(self.root), mcpServers=[])
        self.assertEqual(agent_acp.wait_for_session(), self.root)
        thread, box = self.run_in_thread(agent_acp.read_message)
        session = self.client.result(new)["result"]
        self.assertEqual(session["modes"]["currentModeId"], "ask")
        return session["sessionId"], thread, box

    def prompt(self, session: str, text: str) -> int:
        return self.client.request("session/prompt", sessionId=session, prompt=[{"type": "text", "text": text}])


class HandshakeTests(_Base):
    def test_initialize_reports_capabilities_and_no_sign_in(self) -> None:
        result = self.client.result(self.client.request("initialize", protocolVersion=1))["result"]
        self.assertEqual(result["authMethods"], [])
        self.assertFalse(result["agentCapabilities"]["loadSession"])
        self.assertEqual(result["agentInfo"]["name"], "crit")

    def test_session_new_is_answered_once_the_agent_is_ready(self) -> None:
        session, thread, box = self.open_session()
        self.assertEqual(
            [m["id"] for m in agent_acp._session_result(session)["modes"]["availableModes"]], list(agent_ui.MODES)
        )
        commands = self.client.update("available_commands_update")["availableCommands"]
        self.assertIn("undo", [c["name"] for c in commands])
        self.client.close()
        thread.join(5)
        self.assertEqual(box, [None])

    def test_startup_failure_fails_the_waiting_session_new(self) -> None:
        new = self.client.request("session/new", cwd=str(self.root), mcpServers=[])
        agent_acp.wait_for_session()
        agent_acp.finish(2, "error: no config.json")
        reply = self.client.result(new)
        self.assertEqual(reply["error"]["message"], "error: no config.json")

    def test_a_relative_cwd_is_refused(self) -> None:
        reply = self.client.result(self.client.request("session/new", cwd="relative/path", mcpServers=[]))
        self.assertEqual(reply["error"]["code"], agent_acp.INVALID_PARAMS)

    def test_unknown_methods_get_method_not_found(self) -> None:
        reply = self.client.result(self.client.request("session/load", sessionId="x", cwd=str(self.root)))
        self.assertEqual(reply["error"]["code"], agent_acp.METHOD_NOT_FOUND)

    def test_closing_before_a_session_returns_none(self) -> None:
        self.client.close()
        self.assertIsNone(agent_acp.wait_for_session())


class ClientRecordTests(_Base):
    def test_what_the_client_offers_is_saved_without_secrets(self) -> None:
        (self.root / ".bot").mkdir()
        self.client.request(
            "initialize", protocolVersion=1, clientInfo={"name": "Android Studio"},
            clientCapabilities={"fs": {"readTextFile": True}, "terminal": True},
        )
        new = self.client.request(
            "session/new",
            cwd=str(self.root),
            mcpServers=[
                {"name": "studio", "command": "studio-mcp", "args": ["--token", "abc", "--port=1"],
                 "env": [{"name": "API_KEY", "value": "s3cret"}]},
                {"type": "http", "name": "web", "url": "http://127.0.0.1:9/mcp?key=s3cret",
                 "headers": [{"name": "Authorization", "value": "Bearer s3cret"}]},
            ],
        )
        agent_acp.wait_for_session()
        self.run_in_thread(agent_acp.read_message)
        self.client.result(new)
        saved = (self.root / ".bot" / "acp-client.json").read_text(encoding="utf-8")
        self.assertNotIn("s3cret", saved)
        self.assertNotIn('"abc"', saved)
        data = json.loads(saved)
        self.assertEqual(data["clientInfo"], {"name": "Android Studio"})
        self.assertTrue(data["clientCapabilities"]["terminal"])
        self.assertEqual([s["name"] for s in data["mcpServers"]], ["studio", "web"])
        self.assertEqual(data["mcpServers"][0]["env"], [{"name": "API_KEY", "value": "***"}])
        self.assertIn("--port=1", data["mcpServers"][0]["args"])


class TurnTests(_Base):
    def test_a_prompt_is_a_task_and_the_next_wait_ends_the_turn(self) -> None:
        session, thread, box = self.open_session()
        prompt = self.prompt(session, "fix the bug")
        thread.join(5)
        self.assertEqual(box, ["fix the bug"])
        agent_acp.assistant("Fixed.")
        self.assertEqual(self.client.update("agent_message_chunk")["content"]["text"], "Fixed.\n\n")
        agent_acp.final_status("COMPLETED", ok=True)
        thread, box = self.run_in_thread(agent_acp.read_message)
        self.assertEqual(self.client.result(prompt)["result"], {"stopReason": "end_turn"})
        self.client.close()
        thread.join(5)

    def test_a_second_prompt_while_one_runs_is_refused(self) -> None:
        session, thread, _ = self.open_session()
        self.prompt(session, "one")
        thread.join(5)
        reply = self.client.result(self.prompt(session, "two"))
        self.assertEqual(reply["error"]["code"], agent_acp.BUSY)

    def test_cancel_ends_the_turn_as_cancelled(self) -> None:
        session, thread, _ = self.open_session()
        prompt = self.prompt(session, "long task")
        thread.join(5)
        with mock.patch.object(agent_acp, "interrupt") as interrupt:
            self.client.notify("session/cancel", sessionId=session)
            deadline = time.monotonic() + 5
            while not interrupt.called and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertTrue(interrupt.called)
        agent_acp.final_status("INTERRUPTED", ok=False)
        self.run_in_thread(agent_acp.read_message)
        self.assertEqual(self.client.result(prompt)["result"], {"stopReason": "cancelled"})

    def test_slash_commands_end_their_turn_without_a_task(self) -> None:
        session, thread, box = self.open_session()
        prompt = self.prompt(session, "/mode auto")
        self.assertEqual(self.client.update("current_mode_update")["currentModeId"], "auto")
        self.assertEqual(self.client.result(prompt)["result"], {"stopReason": "end_turn"})
        self.assertTrue(thread.is_alive())
        self.prompt(session, "/new")
        thread.join(5)
        self.assertEqual(box, [agent_ui.NEW_CHAT])

    def test_set_mode_request(self) -> None:
        session, _, _ = self.open_session()
        reply = self.client.result(self.client.request("session/set_mode", sessionId=session, modeId="plan"))
        self.assertEqual(reply["result"], {})
        self.assertEqual(agent_acp.approve_mode(), "plan")
        bad = self.client.result(self.client.request("session/set_mode", sessionId=session, modeId="yolo-ish"))
        self.assertIn("error", bad)

    def test_another_session_new_starts_a_fresh_chat_in_the_same_folder(self) -> None:
        first, thread, box = self.open_session()
        reply = self.client.result(self.client.request("session/new", cwd=str(self.root), mcpServers=[]))
        self.assertNotEqual(reply["result"]["sessionId"], first)
        thread.join(5)
        self.assertEqual(box, [agent_ui.NEW_CHAT])
        other = Path(tempfile.mkdtemp())
        refused = self.client.result(self.client.request("session/new", cwd=str(other), mcpServers=[]))
        self.assertEqual(refused["error"]["code"], agent_acp.INVALID_PARAMS)

    def test_prompt_text_inlines_attached_files(self) -> None:
        text = agent_acp.prompt_text(
            [
                {"type": "text", "text": "explain"},
                {"type": "resource_link", "uri": (self.root / "note.txt").as_uri(), "name": "note.txt"},
                {"type": "resource", "resource": {"uri": "file:///x/y.kt", "text": "fun main() {}"}},
                {"type": "image", "data": "", "mimeType": "image/png"},
            ]
        )
        self.assertIn("explain", text)
        self.assertIn("@note.txt", text)
        self.assertIn("fun main() {}", text)
        self.assertIn("image attachment left out", text)


class QuestionTests(_Base):
    def test_approval_round_trip(self) -> None:
        self.open_session()
        perm = Permission("command", "Run make", "command:make", "make all", tool="run_command")
        thread, box = self.run_in_thread(agent_acp.approve, perm)
        pending = self.client.update("tool_call")
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["kind"], "execute")
        request = self.client.agent_request("session/request_permission")
        options = request["params"]["options"]
        self.assertEqual([o["optionId"] for o in options], ["yes", "always", "no", "auto"])
        self.assertEqual([o["kind"] for o in options][:3], ["allow_once", "allow_always", "reject_once"])
        self.client.write(id=request["id"], result={"outcome": {"outcome": "selected", "optionId": "no"}})
        thread.join(5)
        self.assertEqual(box, [("no", "")])
        # The refused call's done event closes the same card.
        agent_acp.tool_done("run_command", {"command": "make all"}, {"ok": False, "error": "denied by the user"})
        done = self.client.update("tool_call_update")
        self.assertEqual(done["toolCallId"], pending["toolCallId"])
        self.assertEqual(done["status"], "failed")

    def test_yes_and_auto_switches_the_mode(self) -> None:
        self.open_session()
        thread, box = self.run_in_thread(agent_acp.approve, Permission("edit", "Edit a.py", "edit", tool="edit_file"))
        request = self.client.agent_request("session/request_permission")
        self.client.write(id=request["id"], result={"outcome": {"outcome": "selected", "optionId": "auto"}})
        thread.join(5)
        self.assertEqual(box, [("yes", "")])
        self.assertEqual(agent_acp.approve_mode(), "auto")

    def test_a_cancelled_permission_is_a_no(self) -> None:
        self.open_session()
        thread, box = self.run_in_thread(agent_acp.approve, Permission("edit", "Edit a.py", "edit"))
        request = self.client.agent_request("session/request_permission")
        self.client.write(id=request["id"], result={"outcome": {"outcome": "cancelled"}})
        thread.join(5)
        self.assertEqual(box, [("no", "")])

    def test_numbered_question_becomes_a_choice(self) -> None:
        self.open_session()
        thread, box = self.run_in_thread(agent_acp.ask_question, "Which build?\n1. debug\n2. release")
        request = self.client.agent_request("session/request_permission")
        self.assertEqual(request["params"]["toolCall"]["title"], "Which build?")
        self.assertEqual([o["name"] for o in request["params"]["options"]][:2], ["debug", "release"])
        self.client.write(id=request["id"], result={"outcome": {"outcome": "selected", "optionId": "2"}})
        thread.join(5)
        self.assertEqual(box, ["release"])

    def test_free_text_question_ends_the_turn_and_takes_the_next_prompt(self) -> None:
        session, reader, _ = self.open_session()
        first = self.prompt(session, "rename it")
        reader.join(5)
        thread, box = self.run_in_thread(agent_acp.ask_question, "What should the new name be?")
        self.assertIn("new name", self.client.update("agent_message_chunk")["content"]["text"])
        self.assertEqual(self.client.result(first)["result"], {"stopReason": "end_turn"})
        second = self.prompt(session, "Gradle")
        thread.join(5)
        self.assertEqual(box, ["Gradle"])
        self.run_in_thread(agent_acp.read_message)
        self.assertEqual(self.client.result(second)["result"], {"stopReason": "end_turn"})

    def test_plan_review_start_in_edits_mode(self) -> None:
        self.open_session()
        thread, box = self.run_in_thread(agent_acp.review_plan)
        request = self.client.agent_request("session/request_permission")
        self.client.write(id=request["id"], result={"outcome": {"outcome": "selected", "optionId": "edits"}})
        thread.join(5)
        self.assertEqual(box, [("edits", "")])


class ToolCallTests(_Base):
    def test_an_edit_shows_as_a_diff(self) -> None:
        self.open_session()
        args = {"path": "note.txt", "old_string": "one", "new_string": "two"}
        agent_acp.tool_start("edit_file", args)
        start = self.client.update("tool_call")
        self.assertEqual(start["kind"], "edit")
        self.assertEqual(start["locations"], [{"path": str(self.root / "note.txt")}])
        self.assertEqual(start["content"][0], {"type": "diff", "path": str(self.root / "note.txt"), "oldText": "one", "newText": "two"})
        agent_acp.tool_done("edit_file", args, {"ok": True, "output": "edited"})
        done = self.client.update("tool_call_update")
        self.assertEqual(done["status"], "completed")
        self.assertNotIn("content", done)  # the diff from the start stays

    def test_command_output_streams_into_its_call(self) -> None:
        self.open_session()
        agent_acp.tool_start("run_command", {"command": "ls"})
        call = self.client.update("tool_call")["toolCallId"]
        agent_acp.tool_output("a\n")
        agent_acp.tool_output("b\n")
        self.client.update("tool_call_update")
        live = self.client.update("tool_call_update")
        self.assertEqual(live["toolCallId"], call)
        self.assertIn("a\nb\n", live["content"][0]["content"]["text"])


class LoopTests(_Base):
    def test_a_task_with_an_approved_edit(self) -> None:
        session = _Scripted(
            [_call("edit_file", path="note.txt", old_string="one", new_string="two"), "Done. COMPLETED"]
        )
        outcome: list[str] = []
        new = self.client.request("session/new", cwd=str(self.root), mcpServers=[])
        agent_acp.wait_for_session()
        thread, _ = self.run_in_thread(
            run_agent_loop,
            session,
            workspace=self.root,
            index_path=None,
            cache_dir=None,
            first_task="",
            max_rounds=10,
            max_result_chars=8000,
            outcome=outcome,
            approve_mode="ask",
        )
        sid = self.client.result(new)["result"]["sessionId"]
        prompt = self.prompt(sid, "change note.txt")
        request = self.client.agent_request("session/request_permission")
        self.client.write(id=request["id"], result={"outcome": {"outcome": "selected", "optionId": "yes"}})
        self.assertEqual(self.client.result(prompt)["result"], {"stopReason": "end_turn"})
        updates = [m["params"]["update"] for m in self.client.seen if m.get("method") == "session/update"]
        statuses = [u.get("status") for u in updates if u["sessionUpdate"].startswith("tool_call")]
        self.assertIn("completed", statuses)
        self.client.close()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "two\n")


if __name__ == "__main__":
    unittest.main()
