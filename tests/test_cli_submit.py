from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from critique_bot.cli import main


class SubmitCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.config = self.folder / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "url": "https://example.invalid/chat",
                    "selectors": {
                        "prompt_input": "textarea",
                        "assistant_messages": ".assistant",
                    },
                    "queue_dir": str(self.folder / "queue"),
                }
            ),
            encoding="utf-8",
        )
        self.patch = self.folder / "diff.patch"
        self.patch.write_text("diff --git a/a b/a\n+hello\n", encoding="utf-8")
        self.out = self.folder / "out"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_submit_fails_if_worker_is_down(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "submit",
                    "--config",
                    str(self.config),
                    "--patch-file",
                    str(self.patch),
                    "--output-dir",
                    str(self.out),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("worker is not running", stderr.getvalue())

    def test_submit_rejects_chat_mode(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "submit",
                    "--config",
                    str(self.config),
                    "--mode",
                    "chat",
                    "--prompt",
                    "hi",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("chat", stderr.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
