from __future__ import annotations

import unittest

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

    def test_chat_error_is_runtime_error(self) -> None:
        from critique_bot.chat_client import ChatError

        self.assertIsInstance(ChatError("boom"), RuntimeError)


if __name__ == "__main__":
    unittest.main()
