"""A send that gets no reply message and no generating signal is reported as stalled, not waited on for 20 minutes."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from critique_bot import chat_client
from critique_bot.model_api import ReplyStalled


class NoReplyTests(unittest.TestCase):
    def _page(self) -> MagicMock:
        page = MagicMock()
        page.wait_for_timeout.side_effect = lambda ms: None
        return page

    def test_no_message_and_no_signal_stalls(self) -> None:
        clock = iter(range(0, 10_000, 5))
        with patch.object(chat_client, "_reply_state", return_value={"count": 3, "generating": False}), patch(
            "critique_bot.chat_client.time.monotonic", side_effect=lambda: next(clock)
        ):
            with self.assertRaises(ReplyStalled):
                chat_client._wait_for_reply(
                    self._page(), ".a", previous_count=3, timeout_ms=1_200_000, idle_ms=6000, stall_ms=90_000
                )

    def test_a_page_that_keeps_generating_is_not_stalled_yet(self) -> None:
        states = [{"count": 3, "generating": True}] * 30 + [{"count": 4, "generating": False, "text": "done"}]
        clock = iter(range(0, 10_000, 5))
        calls = {"n": 0}

        def state(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] > len(states):
                raise RuntimeError("stop here")  # past the wait for a message: that part is tested elsewhere
            return states[calls["n"] - 1]

        with patch.object(chat_client, "_reply_state", side_effect=state), patch(
            "critique_bot.chat_client.time.monotonic", side_effect=lambda: next(clock)
        ):
            with self.assertRaises(RuntimeError):  # reached the reply, no ReplyStalled on the way
                chat_client._wait_for_reply(
                    self._page(), ".a", previous_count=3, timeout_ms=1_200_000, idle_ms=6000, stall_ms=90_000
                )


class VirtualizedChatTests(unittest.TestCase):
    """ChatGPT drops old messages from the page: the count stays flat, the last reply changes."""

    def test_a_different_last_reply_is_new_even_when_the_count_is_flat(self) -> None:
        page = MagicMock()
        page._critique_last_reply_key = "data-dil-message-id=old"
        self.assertTrue(chat_client._is_new_reply(page, {"count": 3, "key": "data-dil-message-id=new"}, 3))
        self.assertFalse(chat_client._is_new_reply(page, {"count": 3, "key": "data-dil-message-id=old"}, 3))
        self.assertTrue(chat_client._is_new_reply(page, {"count": 4, "key": ""}, 3))

    def test_no_key_on_either_side_falls_back_to_the_count(self) -> None:
        page = MagicMock()
        page._critique_last_reply_key = ""
        self.assertFalse(chat_client._is_new_reply(page, {"count": 3, "key": ""}, 3))

    def test_wait_takes_the_new_reply_when_an_old_one_left_the_page(self) -> None:
        page = MagicMock()
        page.wait_for_timeout.side_effect = lambda ms: None
        page._critique_last_reply_key = "k=old"
        states = iter([{"count": 3, "key": "k=old", "generating": True}] * 3 + [{"count": 3, "key": "k=new", "generating": True}])
        with patch.object(chat_client, "_reply_state", side_effect=lambda *a, **k: next(states)):
            with self.assertRaises(StopIteration):  # got past "waiting for a new message" into reading it
                chat_client._wait_for_reply(page, ".a", previous_count=3, timeout_ms=60_000, idle_ms=6000)


if __name__ == "__main__":
    unittest.main()
