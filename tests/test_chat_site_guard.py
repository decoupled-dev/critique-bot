"""The prompt is typed only into the configured chat site, never into a page the browser was sent to."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from critique_bot.webchat import chat_client
from critique_bot.config import BotConfig, Selectors
from critique_bot.model_api import ChatError

CHAT = "https://chat.corp.example/"


def _page(url: str) -> MagicMock:
    page = MagicMock()
    page.url = url
    return page


class SiteTests(unittest.TestCase):
    def test_the_chat_and_its_site_are_allowed(self) -> None:
        for url in ("https://chat.corp.example/c/42", "https://corp.example/x", "https://api.corp.example/"):
            with self.subTest(url):
                self.assertIsNone(chat_client._page_on_chat_site(_page(url), CHAT))

    def test_other_sites_and_look_alikes_are_refused(self) -> None:
        for url in (
            "https://login.microsoftonline.com/oauth2",
            "https://evilcorp.example/",
            "https://chat.corp.example.attacker.net/",
            "https://corp.example.attacker.net/",
            "about:blank",
        ):
            with self.subTest(url):
                self.assertIsNotNone(chat_client._page_on_chat_site(_page(url), CHAT))

    def test_a_page_without_a_url_is_refused(self) -> None:
        page = MagicMock()
        type(page).url = property(lambda self: (_ for _ in ()).throw(RuntimeError("closed")))
        self.assertEqual(chat_client._page_on_chat_site(page, CHAT), "(unknown)")


class SendTurnTests(unittest.TestCase):
    def test_nothing_is_typed_on_another_site(self) -> None:
        page = _page("https://sso.other-idp.example/login")
        config = BotConfig(url=CHAT, selectors=Selectors(prompt_input="textarea", assistant_messages=".a"))
        with patch.object(chat_client, "_ensure_idle", return_value=False), patch.object(
            chat_client, "_count_replies", return_value=0
        ), patch.object(chat_client, "_fill_prompt") as fill, patch.object(chat_client, "_send") as send:
            with self.assertRaises(ChatError) as ctx:
                chat_client.send_turn(page, config, "secret source code")
        fill.assert_not_called()
        send.assert_not_called()
        self.assertIn("sso.other-idp.example", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
