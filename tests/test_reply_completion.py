"""The reply must not be cut short when the model merely pauses."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from critique_bot import chat_client
from critique_bot.chat_client import ChatError, _settle_ms, _stream_state, _wait_for_reply
from critique_bot.config import Selectors
from critique_bot.llm import COMPLETION_IDLE, COMPLETION_STOPPED

SELECTORS = Selectors(
    prompt_input="#p",
    assistant_messages=".a",
    stop_button="button.stop",
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Item:
    def __init__(self, page: _Page) -> None:
        self._page = page

    def is_visible(self) -> bool:
        return True

    def inner_text(self) -> str:
        return self._page.frame()[0]


class _Locator:
    def __init__(self, item: _Item) -> None:
        self._item = item

    def count(self) -> int:
        return 1

    def nth(self, index: int) -> _Item:
        del index
        return self._item


class _Page:
    """Replays a scripted stream: one frame of (text, generating) per poll."""

    def __init__(self, script: list[tuple[str, bool]], clock: _Clock) -> None:
        self.script = script
        self.clock = clock
        self.tick = 0
        self.item = _Item(self)

    def frame(self) -> tuple[str, bool]:
        # The last frame repeats, so a settled page stays settled.
        return self.script[min(self.tick, len(self.script) - 1)]

    def locator(self, selector: str) -> _Locator:
        del selector
        return _Locator(self.item)

    def evaluate(self, script: str, arg: object = None) -> object:
        del script, arg
        active = self.frame()[1]
        return {"active": active, "signal": "stop-button" if active else ""}

    def wait_for_timeout(self, ms: int) -> None:
        self.tick += 1
        self.clock.advance(ms / 1000.0)


def _run(
    script: list[tuple[str, bool]],
    *,
    idle_ms: int = 400,
    timeout_ms: int = 60_000,
) -> tuple[str, dict]:
    clock = _Clock()
    page = _Page(script, clock)
    detail: dict[str, object] = {}
    with patch.object(chat_client.time, "monotonic", clock.monotonic):
        reply = _wait_for_reply(
            page,  # type: ignore[arg-type]
            ".a",
            previous_count=0,
            timeout_ms=timeout_ms,
            idle_ms=idle_ms,
            selectors=SELECTORS,
            detail=detail,
        )
    return reply, detail


class StreamStateTests(unittest.TestCase):
    def test_no_selectors_means_unknown(self) -> None:
        page = _Page([("x", True)], _Clock())
        self.assertEqual(_stream_state(page, None), (False, ""))  # type: ignore[arg-type]

    def test_page_without_evaluate_is_safe(self) -> None:
        class Bare:
            pass

        self.assertEqual(_stream_state(Bare(), SELECTORS), (False, ""))  # type: ignore[arg-type]

    def test_evaluate_failure_is_swallowed(self) -> None:
        class Boom:
            def evaluate(self, script: str, arg: object = None) -> object:
                raise RuntimeError("page detached")

        self.assertEqual(_stream_state(Boom(), SELECTORS), (False, ""))  # type: ignore[arg-type]

    def test_settle_window_is_bounded(self) -> None:
        self.assertEqual(_settle_ms(0), 300)
        self.assertEqual(_settle_ms(4_000), 1_000)
        self.assertEqual(_settle_ms(600_000), 2_000)


class WaitForReplyTests(unittest.TestCase):
    def test_pause_while_generating_does_not_truncate(self) -> None:
        # Quiet for 3s (far past idle_ms) while still generating, then finishes.
        script = [("partial", True)] * 12 + [("partial and the rest", False)] * 10
        reply, detail = _run(script, idle_ms=400)
        self.assertEqual(reply, "partial and the rest")
        self.assertEqual(detail["completion"], COMPLETION_STOPPED)
        self.assertTrue(detail["complete"])

    def test_completion_detected_when_stop_button_disappears(self) -> None:
        script = [("hello", True), ("hello there", True)] + [("hello there", False)] * 12
        reply, detail = _run(script, idle_ms=10_000)
        self.assertEqual(reply, "hello there")
        self.assertEqual(detail["completion"], COMPLETION_STOPPED)
        self.assertEqual(detail["signal"], "stop-button")

    def test_falls_back_to_idle_without_any_signal(self) -> None:
        reply, detail = _run([("only answer", False)] * 10, idle_ms=0)
        self.assertEqual(reply, "only answer")
        self.assertEqual(detail["completion"], COMPLETION_IDLE)
        self.assertFalse(detail["complete"])

    def test_stuck_signal_gives_up_instead_of_hanging(self) -> None:
        # A page whose stop button never clears must not block until timeout.
        original = chat_client._SIGNAL_STALL_MS
        chat_client._SIGNAL_STALL_MS = 500
        try:
            reply, detail = _run([("stuck text", True)] * 40, idle_ms=250)
        finally:
            chat_client._SIGNAL_STALL_MS = original
        self.assertEqual(reply, "stuck text")
        self.assertEqual(detail["completion"], COMPLETION_IDLE)

    def test_timeout_still_raises(self) -> None:
        with self.assertRaises(ChatError):
            _run([("never ends", True)] * 5, idle_ms=100, timeout_ms=1)


if __name__ == "__main__":
    unittest.main()
