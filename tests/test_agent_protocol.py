"""crit --protocol: the JSON-lines front end a GUI drives."""

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

from critique_bot import agent, agent_protocol, agent_ui
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


class _Gui:
    """The GUI end of the pipes: writes messages, collects events."""

    def __init__(self) -> None:
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        self._to_agent = os.fdopen(in_w, "w", encoding="utf-8", buffering=1)
        self._from_agent = os.fdopen(out_r, "r", encoding="utf-8")
        self.events: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.seen: list[dict[str, Any]] = []
        threading.Thread(target=self._pump, daemon=True).start()
        agent_protocol.start(
            stdin=os.fdopen(in_r, "r", encoding="utf-8"),
            stdout=os.fdopen(out_w, "w", encoding="utf-8", buffering=1),
        )

    def _pump(self) -> None:
        with self._from_agent:
            for line in self._from_agent:
                self.events.put(json.loads(line))

    def send(self, **message: Any) -> None:
        self._to_agent.write(json.dumps(message) + "\n")
        self._to_agent.flush()

    def expect(self, kind: str, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no {kind!r} event; saw {[e['type'] for e in self.seen]}")
            try:
                event = self.events.get(timeout=left)
            except queue.Empty:
                continue
            self.seen.append(event)
            if event["type"] == kind:
                return event

    def close(self) -> None:
        try:
            self._to_agent.close()
        except OSError:
            pass


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        agent_protocol.reset_for_tests()
        self.gui = _Gui()
        agent.use_ui(agent_protocol)
        self.root = Path(tempfile.mkdtemp())
        (self.root / "note.txt").write_text("one\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.gui.close()
        out = agent_protocol._out
        if out is not None:
            out.close()  # ends the pump thread and frees its descriptor now, not at GC
        agent.use_ui(agent_ui)
        agent_protocol.reset_for_tests()

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


class EventTests(_Base):
    def test_hello_carries_the_protocol_version_and_choices(self) -> None:
        agent_protocol.configure(workspace=self.root, model="gpt", approve_mode="edits", role="code-reviewer")
        agent_protocol.welcome_header(version="9.9")
        hello = self.gui.expect("hello")
        self.assertEqual(hello["protocol"], agent_protocol.PROTOCOL_VERSION)
        self.assertEqual(hello["mode"], "edits")
        self.assertEqual(hello["role"], "code-reviewer")
        self.assertEqual([m["id"] for m in hello["modes"]], list(agent_ui.MODES))
        self.assertIn("generic", [r["id"] for r in hello["roles"]])

    def test_approval_round_trip(self) -> None:
        perm = Permission("command", "Run make", "command:make", "make all")
        thread, box = self.run_in_thread(agent_protocol.approve, perm)
        request = self.gui.expect("approval")
        self.assertEqual(request["summary"], "Run make")
        self.assertEqual([o["value"] for o in request["options"]], ["yes", "always", "no", "auto"])
        self.gui.send(type="answer", id=request["id"], value="no", reason="use ninja")
        thread.join(5)
        self.assertEqual(box, [("no", "use ninja")])

    def test_yes_and_auto_switches_the_mode(self) -> None:
        perm = Permission("edit", "Edit a.py", "edit")
        thread, box = self.run_in_thread(agent_protocol.approve, perm)
        request = self.gui.expect("approval")
        self.gui.send(type="answer", id=request["id"], value="auto")
        thread.join(5)
        self.assertEqual(box, [("yes", "")])
        self.assertEqual(agent_protocol.approve_mode(), "auto")
        self.assertEqual(self.gui.expect("mode")["mode"], "auto")

    def test_a_risky_command_offers_only_yes_and_no(self) -> None:
        agent_protocol.configure(approve_mode="auto")
        perm = Permission("command", "Push", "command:git")
        thread, box = self.run_in_thread(agent_protocol.approve, perm, risky="pushes to a remote")
        request = self.gui.expect("approval")
        self.assertEqual([o["value"] for o in request["options"]], ["yes", "no"])
        self.gui.send(type="answer", id=request["id"], value="always")  # not offered: treated as no
        thread.join(5)
        self.assertEqual(box[0][0], "no")

    def test_closing_the_gui_cancels_a_pending_request(self) -> None:
        thread, box = self.run_in_thread(agent_protocol.ask_question, "Which?", ["a", "b"])
        request = self.gui.expect("question")
        self.assertEqual(request["options"], ["a", "b"])
        self.gui.close()
        thread.join(5)
        self.assertEqual(box, [None])
        self.assertEqual(self.gui.expect("cancel")["id"], request["id"])

    def test_plan_review_without_feedback_stops(self) -> None:
        thread, box = self.run_in_thread(agent_protocol.review_plan)
        request = self.gui.expect("plan_review")
        self.gui.send(type="answer", id=request["id"], value="no")
        thread.join(5)
        self.assertEqual(box, [("stop", "")])

    def test_bad_messages_report_errors_and_keep_reading(self) -> None:
        self.gui.send(type="nonsense")
        self.assertIn("unknown message type", self.gui.expect("error")["message"])
        self.gui._to_agent.write("not json\n")
        self.gui._to_agent.flush()
        self.assertIn("not JSON", self.gui.expect("error")["message"])
        self.gui.send(type="set_mode", mode="plan")
        self.assertEqual(self.gui.expect("mode")["mode"], "plan")

    def test_interrupt_is_ignored_while_idle(self) -> None:
        self.assertFalse(agent_protocol.interrupt())

    def test_typed_slash_commands_are_handled_while_idle(self) -> None:
        thread, box = self.run_in_thread(agent_protocol.read_message)
        self.gui.expect("ready")
        self.gui.send(type="prompt", text="/mode auto")
        self.assertEqual(self.gui.expect("mode")["mode"], "auto")
        self.gui.send(type="prompt", text="/new")
        thread.join(5)
        self.assertEqual(box, [agent_ui.NEW_CHAT])


class LoopTests(_Base):
    def test_a_task_with_an_approved_edit(self) -> None:
        session = _Scripted(
            [_call("edit_file", path="note.txt", old_string="one", new_string="two"), "Done. COMPLETED"]
        )
        outcome: list[str] = []
        thread, box = self.run_in_thread(
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
        self.gui.expect("ready")
        self.gui.send(type="prompt", text="change note.txt")
        self.assertEqual(self.gui.expect("note")["kind"], "task")
        request = self.gui.expect("approval")
        self.assertEqual(request["kind"], "edit")
        self.gui.send(type="answer", id=request["id"], value="yes")
        done = self.gui.expect("tool_done")
        self.assertTrue(done["ok"], done)
        self.assertEqual(self.gui.expect("status")["code"], "COMPLETED")
        self.gui.expect("ready")
        self.gui.send(type="shutdown")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "two\n")


if __name__ == "__main__":
    unittest.main()
