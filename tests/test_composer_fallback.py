"""When the configured prompt_input finds nothing, the composer is found by its shape."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from critique_bot import chat_client
from critique_bot.config import Selectors
from critique_bot.model_api import ChatError

CONFIGURED = "#prompt-textarea, [data-testid='prompt-textarea']"


def _selectors() -> Selectors:
    return Selectors(prompt_input=CONFIGURED, assistant_messages=".markdown")


class _Page:
    """A page where only the selectors in ``visible`` (or a union containing one) show up."""

    def __init__(self, visible: set[str]) -> None:
        self.visible = visible

    def _shows(self, selector: str) -> bool:
        return any(item in selector for item in self.visible)

    def locator(self, selector: str):
        loc = MagicMock()
        loc.page = self
        loc.first.selector = selector
        loc.first.is_visible.return_value = self._shows(selector) and selector in self.visible
        return loc


class FindComposerTests(unittest.TestCase):
    def setUp(self) -> None:
        chat_client._COMPOSER_FOUND.clear()

    def tearDown(self) -> None:
        chat_client._COMPOSER_FOUND.clear()

    def _run(self, page: _Page) -> None:
        def wait(target, timeout_ms, error):
            if not page._shows(target.selector):
                raise PlaywrightTimeoutError("timeout")

        with patch.object(chat_client, "_wait_for_sliced", side_effect=wait), patch(
            "critique_bot.browser.page_block_hint", return_value=""
        ):
            chat_client._find_composer(page, _selectors(), 30_000)

    def test_configured_selector_wins_when_it_matches(self) -> None:
        self._run(_Page({CONFIGURED}))
        self.assertEqual(chat_client._prompt_selector(_selectors()), CONFIGURED)

    def test_signed_in_chatgpt_editor_is_found_and_remembered(self) -> None:
        editor = "div.ProseMirror[contenteditable='true']"
        self._run(_Page({editor}))
        self.assertEqual(chat_client._prompt_selector(_selectors()), editor)

    def test_no_composer_names_the_configured_selector(self) -> None:
        with self.assertRaises(ChatError) as ctx:
            self._run(_Page(set()))
        self.assertIn(CONFIGURED, str(ctx.exception))
        self.assertEqual(chat_client._prompt_selector(_selectors()), CONFIGURED)


if __name__ == "__main__":
    unittest.main()
