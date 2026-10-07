"""A reply still being written must be finished or stopped before the next send."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from unittest.mock import patch

from critique_bot import chat_client
from critique_bot.chat_client import ChatError, send_turn, stop_generation
from critique_bot.config import BotConfig, Selectors

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


class _Item:
    def __init__(self, page: _Page, selector: str, index: int) -> None:
        self.page = page
        self.selector = selector
        self.index = index

    def is_visible(self) -> bool:
        return True

    def inner_text(self) -> str:
        return self.page.messages[self.index]

    def click(self, timeout: int = 0, force: bool = False) -> None:
        del timeout, force
        if self.selector == "button.stop":
            self.page.stop_clicks += 1
            self.page.on_stop()
        if self.selector == "button.more":
            self.page.more_visible = False
            self.page.on_more()


class _Locator:
    def __init__(self, page: _Page, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> _Locator:
        return self

    def count(self) -> int:
        if self.selector == ".a":
            return len(self.page.messages)
        if self.selector == "button.stop":
            return 1 if self.page.generating else 0
        if self.selector == "#p":
            return 1
        if self.selector == "button.more":
            return 1 if self.page.more_visible else 0
        return 0

    def nth(self, index: int) -> _Item:
        return _Item(self.page, self.selector, index)

    def wait_for(self, state: str = "visible", timeout: int = 0) -> None:
        del state, timeout

    def fill(self, text: str, timeout: int = 0) -> None:
        del timeout
        self.page.filled.append((text, self.page.generating))

    def press(self, key: str, timeout: int = 0) -> None:
        del timeout
        if key == "Enter":
            self.page.sent.append(self.page.filled[-1][0])
            self.page.on_send()


class _Page:
    """A chat page driven by a tick clock: each poll advances 250 ms and runs due events."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.tick = 0
        self.messages: list[str] = []
        self.generating = False
        self.stop_clicks = 0
        self.filled: list[tuple[str, bool]] = []
        self.sent: list[str] = []
        self.events: list[tuple[int, Callable[[], None]]] = []
        self.on_send: Callable[[], None] = lambda: None
        self.on_stop: Callable[[], None] = self.stop_soon
        self.more_visible = False
        self.on_more: Callable[[], None] = lambda: None

    def at(self, ticks_from_now: int, action: Callable[[], None]) -> None:
        self.events.append((self.tick + ticks_from_now, action))

    def stop_soon(self) -> None:
        self.at(2, lambda: setattr(self, "generating", False))

    def reply_after_send(self, text: str, delay: int = 2) -> None:
        def start() -> None:
            self.messages.append(text[:3])
            self.generating = True

        def grow() -> None:
            self.messages[-1] = text

        self.at(delay, start)
        self.at(delay + 2, grow)
        self.at(delay + 4, lambda: setattr(self, "generating", False))

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    def evaluate(self, script: str, arg: object = None) -> object:
        del script
        if isinstance(arg, dict) and "stopSelector" in arg:
            return {"active": self.generating, "signal": "stop-button" if self.generating else ""}
        return None

    def wait_for_timeout(self, ms: int) -> None:
        self.tick += 1
        self.clock.now += ms / 1000.0
        due = [event for event in self.events if event[0] <= self.tick]
        self.events = [event for event in self.events if event[0] > self.tick]
        for _when, action in due:
            action()


def _config(timeout_ms: int = 60_000, selectors: Selectors = SELECTORS) -> BotConfig:
    return BotConfig(url="https://chat.example/", selectors=selectors, timeout_ms=timeout_ms, idle_ms=400)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.page = _Page(self.clock)
        patcher = patch.object(chat_client.time, "monotonic", self.clock.monotonic)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Playwright is not needed: the fake locators are always visible.
        visible = patch.object(chat_client, "_wait_visible", lambda *_a, **_k: None)
        visible.start()
        self.addCleanup(visible.stop)


class StopGenerationTests(_Case):
    def test_idle_page_is_left_alone(self) -> None:
        self.page.messages = ["done"]
        self.assertFalse(stop_generation(self.page, SELECTORS))  # type: ignore[arg-type]
        self.assertEqual(self.page.stop_clicks, 0)

    def test_clicks_stop_and_waits_until_the_signal_is_gone(self) -> None:
        self.page.messages = ["half a rep"]
        self.page.generating = True
        self.page.on_stop = lambda: self.page.at(4, lambda: setattr(self.page, "generating", False))
        self.assertTrue(stop_generation(self.page, SELECTORS))  # type: ignore[arg-type]
        self.assertEqual(self.page.stop_clicks, 1)
        self.assertFalse(self.page.generating)

    def test_clicks_again_when_the_first_click_does_not_take(self) -> None:
        self.page.generating = True
        clicks: list[int] = []

        def on_stop() -> None:
            clicks.append(self.page.tick)
            if len(clicks) == 2:
                self.page.stop_soon()

        self.page.on_stop = on_stop
        self.assertTrue(stop_generation(self.page, SELECTORS, wait_ms=4_000))  # type: ignore[arg-type]
        self.assertEqual(self.page.stop_clicks, 2)
        self.assertFalse(self.page.generating)

    def test_gives_up_after_the_bound(self) -> None:
        self.page.generating = True
        self.page.on_stop = lambda: None
        start = self.clock.now
        self.assertTrue(stop_generation(self.page, SELECTORS, wait_ms=2_000))  # type: ignore[arg-type]
        self.assertLess(self.clock.now - start, 6.0)


class SendWhileGeneratingTests(_Case):
    def test_waits_for_the_previous_reply_to_finish_then_sends(self) -> None:
        self.page.messages = ["old rep"]
        self.page.generating = True
        self.page.at(3, lambda: (self.page.messages.__setitem__(0, "old reply"), setattr(self.page, "generating", False)))
        self.page.on_send = lambda: self.page.reply_after_send("new answer")
        reply = send_turn(self.page, _config(), "next")  # type: ignore[arg-type]
        self.assertEqual(reply, "new answer")
        self.assertEqual(self.page.stop_clicks, 0)
        self.assertEqual(self.page.filled, [("next", False)])

    def test_stops_a_reply_that_does_not_finish_then_sends(self) -> None:
        self.page.messages = ["endless"]
        self.page.generating = True
        self.page.on_send = lambda: self.page.reply_after_send("new answer")
        with patch.object(chat_client, "_PREVIOUS_REPLY_WAIT_MS", 1_000):
            reply = send_turn(self.page, _config(), "next")  # type: ignore[arg-type]
        self.assertEqual(reply, "new answer")
        self.assertEqual(self.page.stop_clicks, 1)
        self.assertEqual(self.page.filled, [("next", False)])


class RetryAttributionTests(_Case):
    def test_retry_after_timeout_does_not_take_the_old_reply(self) -> None:
        page = self.page

        def thinking() -> None:
            page.generating = True  # stop button up, no reply bubble yet

        def stopped_bubble_renders_late() -> None:
            page.at(1, lambda: setattr(page, "generating", False))
            page.at(3, lambda: page.messages.append("old stopped reply"))

        page.on_send = thinking
        page.on_stop = stopped_bubble_renders_late
        with self.assertRaises(ChatError):
            send_turn(page, _config(timeout_ms=2_000), "question")  # type: ignore[arg-type]

        # What the agent does before a retry: stop, then send again.
        self.assertTrue(stop_generation(page, SELECTORS))  # type: ignore[arg-type]
        # The new reply takes a while to start, so a late old bubble would sit
        # alone and idle long enough to be taken as the answer.
        page.on_send = lambda: page.reply_after_send("the real answer", delay=12)
        reply = send_turn(page, _config(), "question")  # type: ignore[arg-type]
        self.assertEqual(reply, "the real answer")
        self.assertEqual(page.messages, ["old stopped reply", "the real answer"])

    def test_send_turn_alone_stops_and_skips_the_old_reply(self) -> None:
        page = self.page
        page.on_send = lambda: setattr(page, "generating", True)
        page.on_stop = lambda: (
            page.at(1, lambda: setattr(page, "generating", False)),
            page.at(3, lambda: page.messages.append("old stopped reply")),
        )
        with self.assertRaises(ChatError):
            send_turn(page, _config(timeout_ms=2_000), "question")  # type: ignore[arg-type]
        page.on_send = lambda: page.reply_after_send("the real answer")
        with patch.object(chat_client, "_PREVIOUS_REPLY_WAIT_MS", 500):
            reply = send_turn(page, _config(), "question")  # type: ignore[arg-type]
        self.assertEqual(reply, "the real answer")
        self.assertEqual(page.stop_clicks, 1)


class ContinueButtonTests(_Case):
    def _cut_reply_page(self) -> None:
        page = self.page

        def cut() -> None:
            page.more_visible = True

        def resume() -> None:
            page.generating = True
            page.at(2, lambda: page.messages.__setitem__(-1, "first half, second half"))
            page.at(4, lambda: setattr(page, "generating", False))

        def on_send() -> None:
            page.reply_after_send("first half")
            page.at(6, cut)

        page.on_send = on_send
        page.on_more = resume

    def test_configured_continue_is_clicked_once_and_merged(self) -> None:
        self._cut_reply_page()
        selectors = Selectors(prompt_input="#p", assistant_messages=".a", stop_button="button.stop", continue_button="button.more")
        reply = send_turn(self.page, _config(selectors=selectors), "go")  # type: ignore[arg-type]
        self.assertEqual(reply, "first half, second half")
        self.assertFalse(self.page.more_visible)

    def test_continue_is_left_alone_unless_configured(self) -> None:
        self._cut_reply_page()
        reply = send_turn(self.page, _config(), "go")  # type: ignore[arg-type]
        self.assertEqual(reply, "first half")
        self.assertTrue(self.page.more_visible)


class InterruptibleWaitTests(unittest.TestCase):
    def test_visible_wait_is_sliced(self) -> None:
        clock = _Clock()
        timeouts: list[int] = []

        class Boom(Exception):
            pass

        class Target:
            def wait_for(self, state: str = "visible", timeout: int = 0) -> None:
                timeouts.append(timeout)
                clock.now += timeout / 1000
                raise Boom()

        with patch.object(chat_client.time, "monotonic", clock.monotonic):
            with self.assertRaises(Boom):
                chat_client._wait_for_sliced(Target(), 2_200, Boom)  # type: ignore[arg-type]
        self.assertTrue(all(value <= chat_client._WAIT_SLICE_MS for value in timeouts))
        self.assertEqual(sum(timeouts), 2_200)


class ProviderSessionTests(unittest.TestCase):
    def test_page_session_stops_its_page(self) -> None:
        from critique_bot.provider import ChatSession, PageBrowserSession

        page = _Page(_Clock())
        page.generating = True
        session = PageBrowserSession(page, _config(), close_page=False)
        with patch.object(chat_client.time, "monotonic", page.clock.monotonic):
            self.assertTrue(session.stop_generation())
        self.assertEqual(page.stop_clicks, 1)
        self.assertFalse(ChatSession().stop_generation())


if __name__ == "__main__":
    unittest.main()
