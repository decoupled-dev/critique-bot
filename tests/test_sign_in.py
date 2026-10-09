"""The sign-in window is a plain browser; sandbox flags only where they are needed."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from critique_bot import browser


class SandboxFlagTests(unittest.TestCase):
    def test_no_sandbox_flags_by_default(self) -> None:
        self.assertNotIn("--no-sandbox", browser.EDGE_LAUNCH_ARGS)
        self.assertNotIn("--disable-setuid-sandbox", browser.EDGE_LAUNCH_ARGS)
        with patch.object(browser.sys, "platform", "win32"):
            self.assertFalse(browser.sandbox_disabled())
            self.assertNotIn("--no-sandbox", browser._launch_args())

    def test_linux_root_or_forced(self) -> None:
        with patch.object(browser.sys, "platform", "linux"), patch.object(browser.os, "geteuid", return_value=0, create=True):
            self.assertTrue(browser.sandbox_disabled())
            self.assertEqual(browser._launch_args()[0], "--no-sandbox")
        with patch.dict("os.environ", {"CRIT_NO_SANDBOX": "1"}):
            self.assertTrue(browser.sandbox_disabled())
        with patch.dict("os.environ", {"CRIT_NO_SANDBOX": "0"}), patch.object(browser.os, "geteuid", return_value=0, create=True):
            self.assertFalse(browser.sandbox_disabled())


def _cookie_db(profile: Path, rows: list[tuple[str, str]]) -> None:
    path = profile / "Default" / "Network" / "Cookies"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("create table cookies (host_key text, name text)")
        db.executemany("insert into cookies values (?, ?)", rows)


class SignInTests(unittest.TestCase):
    def test_session_cookie_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            self.assertFalse(browser.session_cookie_present(profile, "https://chatgpt.com/"))
            _cookie_db(profile, [(".chatgpt.com", "__Secure-next-auth.session-token")])
            self.assertTrue(browser.session_cookie_present(profile, "https://chatgpt.com/"))
            self.assertIsNone(browser.session_cookie_present(profile, "https://chat.example.com/"))

    def test_plain_window_has_no_automation_flags_and_closes_when_signed_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            seen: list[list[str]] = []
            proc = MagicMock(pid=4242)
            proc.poll.return_value = None
            checks = iter([False, False, True, True])

            def popen(cmd, **kwargs):
                seen.append(cmd)
                return proc

            with patch.object(browser, "resolve_browser", return_value=("/usr/bin/msedge", "msedge")), \
                 patch.object(browser.subprocess, "Popen", side_effect=popen), \
                 patch.object(browser, "close_existing_edge_sessions"), \
                 patch.object(browser, "session_cookie_present", side_effect=lambda *_: next(checks)), \
                 patch.object(browser, "_close_gracefully") as close, \
                 patch.object(browser.time, "sleep"):
                browser.sign_in_with_plain_browser("https://chatgpt.com/", profile, timeout_s=60)
            cmd = seen[0]
            self.assertEqual(cmd[-1], "https://chatgpt.com/")
            joined = " ".join(cmd)
            for flag in ("remote-debugging", "enable-automation", "AutomationControlled", "no-sandbox", "setuid"):
                self.assertNotIn(flag, joined)
            close.assert_called_once_with(proc)

    def test_closed_without_session_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            proc = MagicMock(pid=1)
            proc.poll.return_value = 0
            with patch.object(browser, "resolve_browser", return_value=("/usr/bin/msedge", "msedge")), \
                 patch.object(browser.subprocess, "Popen", return_value=proc), \
                 patch.object(browser, "close_existing_edge_sessions"), \
                 patch.object(browser, "session_cookie_present", return_value=False):
                with self.assertRaises(browser.BrowserError):
                    browser.sign_in_with_plain_browser("https://chatgpt.com/", profile, timeout_s=60)


if __name__ == "__main__":
    unittest.main()


class GuardTests(unittest.TestCase):
    def test_every_window_is_filtered(self) -> None:
        page = MagicMock()
        page._critique_chat_guard = None
        browser.guard_page_network(page, "https://chatgpt.com/")
        page.route.assert_called_once()

    def test_headless_reaches_only_the_chat_hosts(self) -> None:
        self.assertTrue(browser.request_is_allowed("https://chatgpt.com/backend-api/x", "https://chatgpt.com/"))
        for url in (
            "https://accounts.google.com/gsi/client",
            "https://www.gstatic.com/x.js",
            "https://login.microsoftonline.com/common/oauth2",
            "https://www.google-analytics.com/g/collect",
        ):
            with self.subTest(url=url):
                self.assertFalse(browser.request_is_allowed(url, "https://chatgpt.com/"))

    def test_websocket_to_another_host_is_blocked(self) -> None:
        route = MagicMock()
        route.request.url = "wss://tracker.example/socket"
        route.request.resource_type = "websocket"
        browser._filter_chat_route(route, "https://chatgpt.com/")
        route.abort.assert_called_once()
        route.continue_.assert_not_called()

    def test_debug_port_is_not_open_to_web_pages(self) -> None:
        self.assertNotIn("--remote-allow-origins", " ".join(browser.EDGE_LAUNCH_ARGS + browser.EDGE_PRIVATE_ARGS))
        self.assertIn("--disable-background-networking", browser.EDGE_PRIVATE_ARGS)
