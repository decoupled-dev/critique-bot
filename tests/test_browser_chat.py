from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from critique_bot.browser import (
    BrowserError,
    _first_existing,
    _helpful_edge_error,
    _is_system_profile,
    _stderr_tail,
    _urls_match,
    as_browser_error,
    describe_page,
    is_blank_url,
    is_browser_closed_error,
    page_block_hint,
    warn_if_login_page,
)
from critique_bot.chat_client import (
    ChatError,
    _click_timeout,
    _has_pinned_opener,
    _last_visible,
    _looks_like_locator,
    _text_looks_modelish,
    _visible_count,
    _wait_for_reply,
)
from critique_bot.config import Selectors
from critique_bot.llm import LLMError


class BlankUrlTests(unittest.TestCase):
    def test_known_blank_urls(self) -> None:
        for url in (
            "",
            "about:blank",
            "about://blank",
            "chrome://newtab",
            "chrome://newtab/",
            "edge://newtab",
            "edge://new-tab-page",
            "  ABOUT:BLANK  ",
        ):
            self.assertTrue(is_blank_url(url), url)

    def test_real_url_not_blank(self) -> None:
        self.assertFalse(is_blank_url("https://chat.example/"))


class UrlsMatchTests(unittest.TestCase):
    def test_ignores_trailing_slash_and_case(self) -> None:
        self.assertTrue(
            _urls_match("https://Example.com/chat/", "https://example.com/chat")
        )

    def test_query_must_match(self) -> None:
        self.assertFalse(
            _urls_match("https://ex.com/a?x=1", "https://ex.com/a?x=2")
        )

    def test_same_query(self) -> None:
        self.assertTrue(_urls_match("https://ex.com/a?x=1", "https://ex.com/a?x=1"))


class HelpfulEdgeErrorTests(unittest.TestCase):
    def test_profile_in_use(self) -> None:
        err = _helpful_edge_error(RuntimeError("profile is in use / SingletonLock"))
        self.assertIsInstance(err, BrowserError)
        self.assertIn("already in use", str(err))
        self.assertIn("cdp_url", str(err))

    def test_generic_launch_failure(self) -> None:
        err = _helpful_edge_error(RuntimeError("spawn failed"))
        self.assertIn("Playwright", str(err))
        self.assertIn("microsoft-edge", str(err).lower())


class ClosedBrowserErrorTests(unittest.TestCase):
    def test_playwright_new_page_message(self) -> None:
        exc = RuntimeError(
            "BrowserContext.new_page: Target page, context or browser has been closed"
        )
        self.assertTrue(is_browser_closed_error(exc))
        wrapped = as_browser_error(exc)
        self.assertIsInstance(wrapped, BrowserError)
        assert wrapped is not None
        self.assertIn("restart the browser", str(wrapped).lower())

    def test_nested_cause(self) -> None:
        inner = RuntimeError("Target closed")
        outer = RuntimeError("send failed")
        outer.__cause__ = inner
        self.assertTrue(is_browser_closed_error(outer))

    def test_unrelated_error(self) -> None:
        self.assertFalse(is_browser_closed_error(RuntimeError("boom")))
        self.assertIsNone(as_browser_error(RuntimeError("boom")))

    def test_existing_browser_error_passthrough(self) -> None:
        err = BrowserError("already")
        self.assertIs(as_browser_error(err), err)


class PageHintTests(unittest.TestCase):
    def test_cloudflare(self) -> None:
        page = MagicMock()
        page.url = "https://x/__cf_chl_tk=1"
        page.title.return_value = "Just a moment"
        hint = page_block_hint(page)
        self.assertIn("Cloudflare", hint)

    def test_login(self) -> None:
        page = MagicMock()
        page.url = "https://sso.example/signin"
        page.title.return_value = "Login"
        hint = page_block_hint(page)
        self.assertIn("login", hint.lower())

    def test_clean_page(self) -> None:
        page = MagicMock()
        page.url = "https://chat.example/"
        page.title.return_value = "Chat"
        self.assertEqual(page_block_hint(page), "")

    def test_warn_if_login_page(self) -> None:
        page = MagicMock()
        page.url = "https://login.example/"
        page.title.return_value = "x"
        warn_if_login_page(page)

    def test_describe_page(self) -> None:
        page = MagicMock()
        page.title.return_value = "Chat"
        page.url = "https://x"
        self.assertIn("https://x", describe_page(page))
        self.assertIn("Chat", describe_page(page))

    def test_describe_page_errors(self) -> None:
        class Broken:
            def title(self) -> str:
                raise RuntimeError("t")

            @property
            def url(self) -> str:
                raise RuntimeError("u")

        text = describe_page(Broken())  # type: ignore[arg-type]
        self.assertIn("title error", text)
        self.assertIn("url error", text)


class ProfileAndTailTests(unittest.TestCase):
    def test_empty_user_data_is_not_system(self) -> None:
        self.assertFalse(_is_system_profile(None))
        self.assertFalse(_is_system_profile(""))

    def test_stderr_tail_missing(self) -> None:
        self.assertEqual(_stderr_tail(None), "")
        self.assertEqual(_stderr_tail(Path("/no/such/file")), "")

    def test_stderr_tail_truncates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "err.txt"
            path.write_text("word " * 400, encoding="utf-8")
            out = _stderr_tail(path, limit=40)
            self.assertEqual(len(out), 40)

    def test_first_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hit = Path(tmp) / "edge"
            hit.write_text("x", encoding="utf-8")
            self.assertEqual(_first_existing(["/nope", str(hit), "/also-no"]), str(hit))
            self.assertIsNone(_first_existing(["/nope", ""]))


class ChatClientHelperTests(unittest.TestCase):
    def test_looks_like_locator(self) -> None:
        self.assertTrue(_looks_like_locator(".model-picker"))
        self.assertTrue(_looks_like_locator("#model"))
        self.assertTrue(_looks_like_locator("[data-testid=model]"))
        self.assertTrue(_looks_like_locator("xpath=//button"))
        self.assertTrue(_looks_like_locator("button.model"))
        self.assertFalse(_looks_like_locator(""))
        self.assertFalse(_looks_like_locator("Model picker"))

    def test_has_pinned_opener(self) -> None:
        self.assertFalse(
            _has_pinned_opener(Selectors(prompt_input="t", assistant_messages=".a"))
        )
        self.assertTrue(
            _has_pinned_opener(
                Selectors(prompt_input="t", assistant_messages=".a", model_dropdown="#d")
            )
        )
        self.assertTrue(
            _has_pinned_opener(
                Selectors(
                    prompt_input="t",
                    assistant_messages=".a",
                    model_dropdown_identifier="Model",
                )
            )
        )

    def test_text_looks_modelish(self) -> None:
        self.assertTrue(_text_looks_modelish("GPT-4o", "GPT-4o"))
        self.assertTrue(_text_looks_modelish("Choose Claude", "other"))
        self.assertFalse(_text_looks_modelish("", "x"))
        self.assertFalse(_text_looks_modelish("Search timezone", "x"))

    def test_click_timeout_clamped(self) -> None:
        self.assertEqual(_click_timeout(0), 1)
        self.assertEqual(_click_timeout(500), 500)
        self.assertEqual(_click_timeout(90_000), 8_000)

    def test_chat_error_is_llm_error(self) -> None:
        self.assertTrue(issubclass(ChatError, LLMError))


class _Item:
    def __init__(self, visible: bool, text: str = "") -> None:
        self._visible = visible
        self._text = text

    def is_visible(self) -> bool:
        return self._visible

    def inner_text(self) -> str:
        return self._text


class _Locator:
    def __init__(self, items: list[_Item]) -> None:
        self._items = items

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int) -> _Item:
        return self._items[index]


class VisibleLocatorTests(unittest.TestCase):
    def test_visible_count(self) -> None:
        loc = _Locator([_Item(True), _Item(False), _Item(True)])
        self.assertEqual(_visible_count(loc), 2)  # type: ignore[arg-type]

    def test_visible_count_on_error(self) -> None:
        loc = MagicMock()
        loc.count.side_effect = RuntimeError("gone")
        self.assertEqual(_visible_count(loc), 0)

    def test_last_visible(self) -> None:
        items = [_Item(True, "a"), _Item(False, "b"), _Item(True, "c")]
        loc = _Locator(items)
        last = _last_visible(loc)  # type: ignore[arg-type]
        self.assertIs(last, items[2])

    def test_last_visible_none(self) -> None:
        loc = _Locator([_Item(False)])
        self.assertIsNone(_last_visible(loc))  # type: ignore[arg-type]


class WaitForReplyTests(unittest.TestCase):
    def test_returns_idle_text(self) -> None:
        item = _Item(True, "hello world")
        loc = _Locator([item])

        class Page:
            def locator(self, selector: str) -> _Locator:
                del selector
                return loc

            def wait_for_timeout(self, ms: int) -> None:
                del ms

        reply = _wait_for_reply(
            Page(),  # type: ignore[arg-type]
            ".assistant",
            previous_count=0,
            timeout_ms=5_000,
            idle_ms=0,
        )
        self.assertEqual(reply, "hello world")

    def test_timeout_when_no_message(self) -> None:
        loc = _Locator([])

        class Page:
            def locator(self, selector: str) -> _Locator:
                del selector
                return loc

            def wait_for_timeout(self, ms: int) -> None:
                del ms

        with self.assertRaises(ChatError) as ctx:
            _wait_for_reply(
                Page(),  # type: ignore[arg-type]
                ".assistant",
                previous_count=0,
                timeout_ms=0,
                idle_ms=10,
            )
        self.assertIn("no assistant message appeared", str(ctx.exception))


class ResolveBrowserTests(unittest.TestCase):
    def test_no_browser_raises(self) -> None:
        from critique_bot.browser import resolve_browser

        with patch("critique_bot.browser._find_edge_executable", return_value=None):
            with patch("critique_bot.browser._find_chrome_executable", return_value=None):
                with self.assertRaises(BrowserError) as ctx:
                    resolve_browser()
        self.assertIn("No Chromium", str(ctx.exception))

    def test_falls_back_to_chrome(self) -> None:
        from critique_bot.browser import resolve_browser

        with patch("critique_bot.browser._find_edge_executable", return_value=None):
            with patch(
                "critique_bot.browser._find_chrome_executable",
                return_value="/usr/bin/google-chrome",
            ):
                executable, channel = resolve_browser()
        self.assertEqual(executable, "/usr/bin/google-chrome")
        self.assertEqual(channel, "chrome")


if __name__ == "__main__":
    unittest.main()
