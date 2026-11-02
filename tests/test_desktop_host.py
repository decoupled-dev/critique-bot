"""critique-bot desktop-host: project settings for the desktop app, and own tabs in a shared browser."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from critique_bot import browser

ROOT = Path(__file__).resolve().parents[1]


def _host(argv: list[str], stdin: str = "") -> tuple[int, list[dict], str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.run(
        [sys.executable, "-m", "critique_bot", "desktop-host", *argv],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return proc.returncode, lines, proc.stderr


class DesktopHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "proj"
        self.project.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_local_model_reports_config_and_no_browser(self) -> None:
        (self.project / "config.json").write_text(
            json.dumps({"backend": "local", "url": "http://127.0.0.1:1", "model": "m", "local": {"api": "ollama"}}),
            encoding="utf-8",
        )
        code, lines, _err = _host(["--repo-dir", str(self.project)])
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1, "only JSON lines on stdout")
        msg = lines[0]
        self.assertEqual(msg["type"], "host")
        self.assertTrue(msg["local"])
        self.assertIsNone(msg["cdp_url"])
        self.assertEqual(Path(msg["config"]), (self.project / "config.json").resolve())
        self.assertTrue((self.project / ".bot" / "settings.json").is_file(), "the project is set up like crit does")

    def test_configured_cdp_url_is_passed_through(self) -> None:
        config = self.project / "c.json"
        config.write_text(
            json.dumps(
                {
                    "url": "https://example.invalid/chat",
                    "cdp_url": "http://127.0.0.1:9333",
                    "selectors": {"prompt_input": "textarea", "assistant_messages": ".a"},
                }
            ),
            encoding="utf-8",
        )
        code, lines, _err = _host(["--repo-dir", str(self.project), "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(lines[0]["cdp_url"], "http://127.0.0.1:9333")
        self.assertFalse(lines[0]["local"])

    def test_missing_config_is_an_error_line(self) -> None:
        code, lines, _err = _host(["--repo-dir", str(self.project)])
        self.assertEqual(code, 1)
        self.assertEqual(lines[0]["type"], "error")
        self.assertIn("no config.json", lines[0]["message"])

    def test_bad_config_is_an_error_line(self) -> None:
        (self.project / "config.json").write_text(json.dumps({"url": "https://x.invalid"}), encoding="utf-8")
        code, lines, _err = _host(["--repo-dir", str(self.project)])
        self.assertEqual(code, 1)
        self.assertIn("selectors.prompt_input", lines[0]["message"])


class OwnTabTests(unittest.TestCase):
    def _context(self) -> MagicMock:
        existing = MagicMock()
        existing.is_closed.return_value = False
        context = MagicMock()
        context.pages = [existing]
        context.new_page.return_value = MagicMock(name="new")
        return context

    def test_reuses_a_tab_by_default(self) -> None:
        context = self._context()
        with patch.dict(os.environ, {browser.ENV_CDP_OWN_TAB: ""}):
            page = browser._page_for_cdp(context, None)
        self.assertIs(page, context.pages[0])
        context.new_page.assert_not_called()

    def test_own_tab_opens_a_new_one(self) -> None:
        context = self._context()
        with patch.dict(os.environ, {browser.ENV_CDP_OWN_TAB: "1"}):
            page = browser._page_for_cdp(context, "https://example.invalid/chat")
        self.assertIs(page, context.new_page.return_value)

    def test_own_tab_is_closed_when_attaching_fails(self) -> None:
        context = self._context()
        fake_browser = MagicMock()
        fake_browser.contexts = [context]
        playwright = MagicMock()
        playwright.chromium.connect_over_cdp.return_value = fake_browser
        with (
            patch.dict(os.environ, {browser.ENV_CDP_OWN_TAB: "1"}),
            patch.object(browser, "navigate", side_effect=browser.BrowserError("blocked")),
            patch.object(browser, "attach_page_debug"),
            patch.object(browser, "_log_context"),
        ):
            with self.assertRaises(browser.BrowserError):
                browser._attach_over_cdp(playwright, "http://127.0.0.1:1", start_url="https://example.invalid", timeout_ms=10)
        context.new_page.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
