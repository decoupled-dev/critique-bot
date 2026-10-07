from __future__ import annotations

import unittest
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from critique_bot.config import BotConfig, Selectors
from critique_bot.provider import (
    BrowserProvider,
    ChatSession,
    open_provider,
)


def _config(**overrides: object) -> BotConfig:
    values: dict[str, object] = {
        "url": "https://chat.example",
        "selectors": Selectors(prompt_input="t", assistant_messages=".a"),
        "model": "GPT-5.1",
        "timeout_ms": 5000,
    }
    values.update(overrides)
    return BotConfig(**values)  # type: ignore[arg-type]


class BrowserProviderTests(unittest.TestCase):
    def test_job_config_replace(self) -> None:
        from critique_bot.provider import _job_config

        cfg = _config(model="GPT-5.1")
        self.assertEqual(_job_config(cfg, None).model, "GPT-5.1")
        self.assertEqual(_job_config(cfg, "GPT-4o").model, "GPT-4o")

    def test_session_base_not_implemented(self) -> None:
        with self.assertRaises(NotImplementedError):
            ChatSession().send("x")

    def test_open_provider_is_browser(self) -> None:
        provider = open_provider(_config())
        self.assertIsInstance(provider, BrowserProvider)

    def test_browser_session_requires_start(self) -> None:
        from critique_bot.browser import BrowserError

        provider = BrowserProvider(_config(), headed=False)
        with self.assertRaises(BrowserError):
            provider.session()
        with self.assertRaises(BrowserError):
            provider.session(isolated=True)

    def test_new_page_closed_context_is_browser_error(self) -> None:
        from critique_bot.browser import BrowserError

        class Context:
            def new_page(self) -> None:
                raise RuntimeError(
                    "BrowserContext.new_page: Target page, context or browser has been closed"
                )

        class Home:
            context = Context()

            def is_closed(self) -> bool:
                return False

        provider = BrowserProvider(_config(), headed=False)
        provider._home = Home()
        with self.assertRaises(BrowserError) as ctx:
            provider.session()
        self.assertIn("closed", str(ctx.exception).lower())

    def test_closed_home_tab_is_browser_error(self) -> None:
        from critique_bot.browser import BrowserError

        class Home:
            context = object()

            def is_closed(self) -> bool:
                return True

        provider = BrowserProvider(_config(), headed=False)
        provider._home = Home()
        with self.assertRaises(BrowserError) as ctx:
            provider.session()
        self.assertIn("home tab", str(ctx.exception).lower())

    def test_missing_profile_signs_in_then_continues_headless(self) -> None:
        launches: list[dict[str, Any]] = []

        @contextmanager
        def fake_launch(**kwargs: Any):
            launches.append(kwargs)
            yield object()

        with patch("critique_bot.browser.needs_visible_login", return_value=True):
            with patch("critique_bot.browser.launch_edge", fake_launch):
                with patch("critique_bot.browser.wait_until_signed_in") as wait:
                    provider = BrowserProvider(_config(), headed=False)
                    with provider:
                        self.assertIsNotNone(provider._home)
        self.assertEqual([item["headed"] for item in launches], [True, False])
        self.assertNotIn("promote_missing_profile", launches[0])
        self.assertFalse(launches[1]["promote_missing_profile"])
        wait.assert_called_once()

    def test_saved_profile_stays_headless(self) -> None:
        launches: list[bool] = []

        @contextmanager
        def fake_launch(**kwargs: Any):
            launches.append(kwargs["headed"])
            yield object()

        with patch("critique_bot.browser.needs_visible_login", return_value=False):
            with patch("critique_bot.browser.launch_edge", fake_launch):
                with patch("critique_bot.browser.wait_until_signed_in") as wait:
                    with BrowserProvider(_config(), headed=False):
                        pass
        self.assertEqual(launches, [False])
        wait.assert_not_called()

    def test_chat_error_is_runtime_error(self) -> None:
        from critique_bot.chat_client import ChatError

        self.assertIsInstance(ChatError("boom"), RuntimeError)


if __name__ == "__main__":
    unittest.main()
