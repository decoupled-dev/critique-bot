from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from critique_bot.output import isoformat, save_failure, write_output, write_review


class WriteOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_writes_markdown_and_json_and_prints(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            write_output(
                self.folder,
                "hello review",
                {"mode": "review", "ok": True},
                stem="review",
            )
        self.assertEqual((self.folder / "review.md").read_text(encoding="utf-8"), "hello review")
        payload = json.loads((self.folder / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["mode"], "review")
        self.assertIn("hello review", buf.getvalue())

    def test_print_body_false(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            write_output(self.folder, "secret", {"x": 1}, stem="reply", print_body=False)
        self.assertEqual(buf.getvalue(), "")
        self.assertTrue((self.folder / "reply.md").is_file())

    def test_write_review_uses_review_stem(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            write_review(self.folder, "body", {"k": "v"})
        self.assertTrue((self.folder / "review.md").is_file())
        self.assertTrue((self.folder / "review.json").is_file())


class IsoformatTests(unittest.TestCase):
    def test_seconds_precision_and_timezone(self) -> None:
        moment = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        text = isoformat(moment)
        self.assertIn("2024-01-02", text)
        self.assertNotIn(".", text.split("+")[0].split("-")[-1] if False else text)
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


class SaveFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_saves_screenshot_and_html(self) -> None:
        page = MagicMock()
        page.screenshot = MagicMock()
        page.content = MagicMock(return_value="<html>ok</html>")
        with patch("critique_bot.browser.describe_page", return_value="url='x'"):
            save_failure(page, self.folder)
        page.screenshot.assert_called_once()
        html = (self.folder / "page.html").read_text(encoding="utf-8")
        self.assertEqual(html, "<html>ok</html>")

    def test_truncates_huge_html(self) -> None:
        page = MagicMock()
        page.screenshot = MagicMock()
        page.content = MagicMock(return_value="x" * 2_000_100)
        save_failure(page, self.folder)
        html = (self.folder / "page.html").read_text(encoding="utf-8")
        self.assertLess(len(html), 2_000_100)
        self.assertIn("truncated", html)

    def test_screenshot_and_html_errors_are_swallowed(self) -> None:
        page = MagicMock()
        page.screenshot.side_effect = RuntimeError("no shot")
        page.content.side_effect = RuntimeError("no html")
        save_failure(page, self.folder)
        self.assertFalse((self.folder / "screenshot.png").exists())
        self.assertFalse((self.folder / "page.html").exists())

    def test_describe_page_failure_still_saves(self) -> None:
        page = MagicMock()
        page.screenshot = MagicMock()
        page.content = MagicMock(return_value="<p/>")
        with patch(
            "critique_bot.browser.describe_page",
            side_effect=RuntimeError("boom"),
        ):
            save_failure(page, self.folder)
        self.assertTrue((self.folder / "page.html").is_file())


if __name__ == "__main__":
    unittest.main()
