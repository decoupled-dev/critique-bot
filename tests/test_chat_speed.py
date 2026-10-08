"""Speed: one probe per reply poll, headless request trimming, and no throttling of a hidden window."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from critique_bot import browser, chat_client
from critique_bot.chat_client import COMPLETION_IDLE, COMPLETION_STOPPED, _wait_for_reply
from critique_bot.config import Selectors

SELECTORS = Selectors(prompt_input="#p", assistant_messages=".a", stop_button="button.stop")


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


class _ProbePage:
    """A page whose evaluate answers the one-probe reply state from a script of frames.

    Each frame is (count, text, generating). Locator calls are counted: the fast
    path must not use them except for a final read it does not need here.
    """

    def __init__(self, frames: list[tuple[int, str, bool]], clock: _Clock, *, broken_selector: bool = False) -> None:
        self.frames = frames
        self.clock = clock
        self.tick = 0
        self.evaluations = 0
        self.locator_calls = 0
        self.text_reads = 0
        self.broken_selector = broken_selector

    def frame(self) -> tuple[int, str, bool]:
        return self.frames[min(self.tick, len(self.frames) - 1)]

    def evaluate(self, script: str, arg: object = None) -> object:
        self.evaluations += 1
        if self.broken_selector:
            return {"ok": False}
        count, text, generating = self.frame()
        payload = arg if isinstance(arg, dict) else {}
        if payload.get("withText"):
            self.text_reads += 1
        return {
            "ok": True,
            "count": count,
            "generating": generating,
            "signal": "stop-button" if generating else "",
            "length": len(text),
            "hash": hash(text) & 0xFFFFFFFF,
            "text": text if payload.get("withText") else None,
        }

    def locator(self, selector: str):
        self.locator_calls += 1
        page = self

        class Item:
            def is_visible(self) -> bool:
                return True

            def inner_text(self) -> str:
                return page.frame()[1]

        class Loc:
            def count(self) -> int:
                return page.frame()[0]

            def nth(self, index: int) -> Item:
                return Item()

        return Loc()

    def wait_for_timeout(self, ms: int) -> None:
        self.tick += 1
        self.clock.now += ms / 1000.0


def _run(page: _ProbePage, clock: _Clock, *, idle_ms: int = 4000, previous: int = 1) -> tuple[str, dict]:
    detail: dict[str, object] = {}
    with patch.object(chat_client.time, "monotonic", clock.monotonic):
        reply = _wait_for_reply(
            page,  # type: ignore[arg-type]
            ".a",
            previous_count=previous,
            timeout_ms=120_000,
            idle_ms=idle_ms,
            selectors=SELECTORS,
            detail=detail,
        )
    return reply, detail


class FastReplyTests(unittest.TestCase):
    def test_one_probe_per_poll_and_text_read_once(self) -> None:
        clock = _Clock()
        frames = (
            [(1, "old", False)] * 3
            + [(2, "Hel", True), (2, "Hello wo", True), (2, "Hello world", True)]
            + [(2, "Hello world", True)] * 4
            + [(2, "Hello world", False)] * 6
        )
        page = _ProbePage(frames, clock)
        reply, detail = _run(page, clock)
        self.assertEqual(reply, "Hello world")
        self.assertEqual(detail["completion"], COMPLETION_STOPPED)
        self.assertTrue(detail["complete"])
        self.assertEqual(page.locator_calls, 0)
        self.assertEqual(page.text_reads, 1)
        self.assertLessEqual(page.evaluations, page.tick + 4)  # one per poll, plus the path check and the final read
        self.assertIn("first_text_seconds", detail)

    def test_stop_must_be_gone_two_polls_and_settle_is_shorter(self) -> None:
        clock = _Clock()
        frames = [(2, "Answer", True)] * 3 + [(2, "Answer", False), (2, "Answer", True)] + [(2, "Answer", True)] * 2
        frames += [(2, "Answer", False)] * 10
        page = _ProbePage(frames, clock)
        start = clock.now
        reply, detail = _run(page, clock, previous=1)
        self.assertEqual(reply, "Answer")
        self.assertEqual(detail["completion"], COMPLETION_STOPPED)
        # The one-poll flicker at tick 3 did not end it; the clear run from tick 7 did, within about a second.
        self.assertGreaterEqual(page.tick, 8)
        self.assertLess(clock.now - start, 3.5)

    def test_no_signal_page_still_uses_idle(self) -> None:
        clock = _Clock()
        page = _ProbePage([(2, "quiet reply", False)] * 40, clock)
        reply, detail = _run(page, clock, idle_ms=1000)
        self.assertEqual(reply, "quiet reply")
        self.assertEqual(detail["completion"], COMPLETION_IDLE)

    def test_a_playwright_only_selector_uses_the_locator_path(self) -> None:
        clock = _Clock()
        page = _ProbePage([(1, "x", False)] * 2 + [(2, "fine", False)] * 40, clock, broken_selector=True)
        reply, detail = _run(page, clock, idle_ms=1000)
        self.assertEqual(reply, "fine")
        self.assertGreater(page.locator_calls, 0)
        self.assertEqual(getattr(page, "_critique_slow_reply", None), ".a")
        evaluations = page.evaluations
        chat_client._count_replies(page, ".a")  # type: ignore[arg-type]
        self.assertEqual(page.evaluations, evaluations)

    def test_count_replies_is_one_call(self) -> None:
        clock = _Clock()
        page = _ProbePage([(57, "x", False)], clock)
        self.assertEqual(chat_client._count_replies(page, ".a"), 57)  # type: ignore[arg-type]
        self.assertEqual((page.evaluations, page.locator_calls), (1, 0))


class _Route:
    def __init__(self, url: str, resource_type: str) -> None:
        self.request = MagicMock(url=url, method="GET", resource_type=resource_type)
        self.continued = False
        self.aborted = None

    def continue_(self) -> None:
        self.continued = True

    def abort(self, reason: str | None = None) -> None:
        self.aborted = reason


class LeanHeadlessTests(unittest.TestCase):
    CHAT = "https://chatgpt.com/"

    def _handler(self, lean: bool):
        handlers: list = []
        page = MagicMock()
        page._critique_chat_guard = None
        page.route.side_effect = lambda _pattern, handler: handlers.append(handler)
        with patch.object(browser, "_lean_launch", lean):
            browser.guard_page_network(page, self.CHAT)
        return handlers[0]

    def test_headless_skips_decoration_only(self) -> None:
        handle = self._handler(lean=True)
        for kind in ("image", "font", "media"):
            route = _Route("https://cdn.oaistatic.com/x." + kind, kind)
            handle(route)
            self.assertEqual(route.aborted, "blockedbyclient", kind)
        for kind in ("script", "stylesheet", "xhr", "fetch", "websocket", "document", "eventsource"):
            route = _Route("https://chatgpt.com/backend-api/x", kind)
            handle(route)
            self.assertTrue(route.continued, kind)
        challenge = _Route("https://challenges.cloudflare.com/turnstile/v0/img.png", "image")
        handle(challenge)
        self.assertTrue(challenge.continued)

    def test_visible_window_keeps_images(self) -> None:
        handle = self._handler(lean=False)
        route = _Route("https://cdn.oaistatic.com/avatar.png", "image")
        handle(route)
        self.assertTrue(route.continued)

    def test_env_switch_turns_it_off(self) -> None:
        with patch.dict("os.environ", {"CRIT_LEAN_HEADLESS": "0"}):
            handle = self._handler(lean=True)
        route = _Route("https://cdn.oaistatic.com/avatar.png", "image")
        handle(route)
        self.assertTrue(route.continued)


class DesktopEdgeArgsTests(unittest.TestCase):
    def test_no_throttle_flags_in_both_modes(self) -> None:
        for headed in (True, False):
            seen: list[list[str]] = []

            def fake_popen(cmd, **kwargs):
                seen.append(list(cmd))
                return MagicMock(pid=1)

            with patch.object(browser, "resolve_browser", return_value=("/usr/bin/msedge", "msedge")), \
                 patch.object(browser.subprocess, "Popen", side_effect=fake_popen), \
                 patch.object(browser, "_wait_for_cdp", return_value=None), \
                 patch.object(browser, "_free_port", return_value=9333):
                try:
                    browser._start_desktop_edge(headed=headed, start_url=None, user_data_dir=browser.Path("/tmp/crit-test-profile"))
                except Exception:
                    pass
            self.assertTrue(seen, "desktop Edge was not started")
            for flag in browser.EDGE_NO_THROTTLE_ARGS:
                self.assertIn(flag, seen[0], (headed, flag))


if __name__ == "__main__":
    unittest.main()
