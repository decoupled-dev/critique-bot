"""No reply: resend, stall detection, retry in a new chat. Long sessions: rotate with a handoff note."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from critique_bot import agent, agent_ui, chat_client
from critique_bot.agent import run_agent_loop
from critique_bot.chat_client import ChatError, ReplyNotStarted, ReplyStalled
from critique_bot.config import BotConfig, Selectors

SELECTORS = Selectors(prompt_input="#p", assistant_messages=".a", stop_button="button.stop", send_button="button.send")


def _call(tool: str, **arguments: object) -> str:
    return "<tool_call>\n" + json.dumps({"tool": tool, "arguments": arguments}) + "\n</tool_call>"


class _Session:
    """Replies may be exceptions (a page that fails). new_chat is counted."""

    def __init__(self, replies: list) -> None:
        self.sent: list[str] = []
        self.replies = list(replies)
        self.new_chats: list[int] = []
        self.last_detail = {"complete": True}

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def new_chat(self) -> bool:
        self.new_chats.append(len(self.sent))
        return True

    def stop_generation(self) -> bool:
        return False


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        agent_ui.reset()
        agent_ui.set_console(Console(file=io.StringIO(), force_terminal=False, width=100, theme=agent_ui.rich_theme("dark")))
        self.root = Path(tempfile.mkdtemp())
        (self.root / "note.txt").write_text("one\n", encoding="utf-8")
        self.sleeps = patch.object(agent, "_sleep", lambda _s: None)
        self.sleeps.start()

    def tearDown(self) -> None:
        self.sleeps.stop()
        agent_ui.reset()

    def _loop(self, session, task: str, *, tasks: list | None = None, **kwargs):
        queue = list(tasks or [])
        outcome: list[str] = []
        run_agent_loop(
            session, workspace=self.root, index_path=None, cache_dir=None, first_task=task,
            max_rounds=20, max_result_chars=8000, read_message=lambda: queue.pop(0) if queue else None,
            emit=lambda _t: None, approve_mode="auto", outcome=outcome, seed="INSTRUCTIONS", **kwargs,
        )
        return outcome


class RetryTests(_Base):
    def test_second_failure_moves_to_a_new_chat_with_the_task_summary(self) -> None:
        session = _Session(
            [
                _call("read_files", path="note.txt"),
                ChatError("the chat page did not start a reply within 60s of the send"),
                ChatError("the chat page did not start a reply within 60s of the send"),
                _call("edit_file", path="note.txt", old_string="one", new_string="two"),
                "COMPLETED",
            ]
        )
        outcome = self._loop(session, "change note.txt to two")
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertEqual(session.new_chats, [3])  # after the second failed try, not the first
        moved = session.sent[3]
        self.assertTrue(moved.startswith("INSTRUCTIONS"))
        self.assertIn("CONTINUING IN A NEW CHAT", moved)
        self.assertIn("Task: change note.txt to two", moved)
        self.assertIn("1|one", moved)  # the results it was waiting for are resent too
        self.assertEqual(session.sent[2], session.sent[1])  # the first retry is the same message, same chat
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "two\n")

    def test_empty_reply_is_retried_not_nudged(self) -> None:
        session = _Session(["", "The answer is 4."])
        outcome = self._loop(session, "what is 2 + 2?")
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertEqual(session.sent[1], session.sent[0])


class RotationTests(_Base):
    def test_turn_limit_rotates_with_a_handoff_note(self) -> None:
        replies = [_call("read_files", path="note.txt")] * 1 + [_call("list_files", path=".")] * 4
        replies += ["Handoff: note.txt holds one; change it to two next.", _call("edit_file", path="note.txt", old_string="one", new_string="two"), "COMPLETED"]
        session = _Session(replies)
        self._loop(session, "change note.txt to two", settings={"compact_after_turns": 5})
        self.assertIn("handoff note", session.sent[5].lower())
        self.assertEqual(session.new_chats, [6])
        moved = session.sent[6]
        self.assertIn("Handoff note from the previous chat", moved)
        self.assertIn("note.txt holds one", moved)
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "two\n")

    def test_new_chat_between_tasks_lists_earlier_tasks(self) -> None:
        looks = [_call("list_files", path="."), _call("find_files", glob="*.txt"), _call("git_status")]
        session = _Session(looks + [_call("edit_file", path="note.txt", old_string="one", new_string="two"), "COMPLETED", "It says two."])
        self._loop(
            session, "change note.txt to two", tasks=["what does note.txt say?"],
            settings={"compact_after_turns": 5, "handoff_notes": False},
        )
        self.assertEqual(session.new_chats, [5])
        self.assertTrue(session.sent[5].startswith("INSTRUCTIONS"))
        self.assertIn("EARLIER IN THIS SESSION", session.sent[5])
        self.assertIn("change note.txt to two -> COMPLETED (changed note.txt)", session.sent[5])

    def test_reasons(self) -> None:
        chat = agent._Chat(_Session([]), compact_turns=3, compact_minutes=10)
        self.assertEqual(chat.rotation_reason(), "")
        chat.sends = 3
        self.assertIn("3 messages", chat.rotation_reason())
        chat.sends = 1
        chat.born -= 11 * 60
        self.assertIn("open 11 minutes", chat.rotation_reason())


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


class _Page:
    """count, generating, text per poll; the composer may still hold the prompt."""

    def __init__(self, frames, clock, composer: str = "") -> None:
        self.frames = frames
        self.clock = clock
        self.tick = 0
        self.composer = composer
        self.sends = 0

    def frame(self):
        return self.frames[min(self.tick, len(self.frames) - 1)]

    def evaluate(self, script, arg=None):
        count, generating, text = self.frame()
        return {"ok": True, "count": count, "generating": generating, "signal": "stop-button" if generating else "",
                "length": len(text), "hash": hash(text) & 0xFFFF, "text": text}

    def locator(self, selector):
        page = self

        class Loc:
            first = None

            def evaluate(self, script):
                return page.composer

            def click(self, timeout=None):
                page.sends += 1
                page.composer = ""

            def wait_for(self, state=None, timeout=None):
                return None

        loc = Loc()
        loc.first = loc
        return loc

    def wait_for_timeout(self, ms):
        self.tick += 1
        self.clock.now += ms / 1000


class BrowserSideTests(unittest.TestCase):
    def _config(self) -> BotConfig:
        return BotConfig(url="https://chat.example/", selectors=SELECTORS, reply_start_ms=2_000, reply_stall_ms=3_000, thinking_max_ms=5_000)

    def test_a_send_that_did_not_go_out_is_sent_again(self) -> None:
        clock = _Clock()
        page = _Page([(0, False, "")] * 9 + [(1, True, "")], clock, composer="Task: fix the build")
        with patch.object(chat_client.time, "monotonic", clock.monotonic):
            chat_client._ensure_started(page, SELECTORS, "Task: fix the build", 0, self._config())
        self.assertEqual(page.sends, 1)

    def test_no_reply_and_empty_box_raises(self) -> None:
        clock = _Clock()
        page = _Page([(0, False, "")], clock, composer="")
        with patch.object(chat_client.time, "monotonic", clock.monotonic):
            with self.assertRaises(ReplyNotStarted):
                chat_client._ensure_started(page, SELECTORS, "Task: fix the build", 0, self._config())
        self.assertEqual(page.sends, 0)

    def _wait(self, frames):
        clock = _Clock()
        page = _Page(frames, clock)
        with patch.object(chat_client.time, "monotonic", clock.monotonic):
            return chat_client._wait_for_reply(
                page, ".a", previous_count=0, timeout_ms=600_000, idle_ms=1_000, selectors=SELECTORS,
                detail={}, stall_ms=3_000, thinking_ms=5_000,
            ), clock

    def test_started_then_silent_is_a_stall(self) -> None:
        with self.assertRaises(ReplyStalled):
            self._wait([(1, False, "")])

    def test_thinking_is_allowed_then_capped(self) -> None:
        text, _clock = self._wait([(1, True, "")] * 12 + [(1, True, "Done thinking.")] + [(1, False, "Done thinking.")] * 10)
        self.assertEqual(text, "Done thinking.")
        with self.assertRaises(ReplyStalled):
            self._wait([(1, True, "")])


if __name__ == "__main__":
    unittest.main()
