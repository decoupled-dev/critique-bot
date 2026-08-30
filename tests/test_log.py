from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from critique_bot import log


class LogHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        log.configure(enabled=False)

    def tearDown(self) -> None:
        log.configure(enabled=False)

    def test_preview_compacts_and_truncates(self) -> None:
        self.assertEqual(log.preview("  a   b  "), "a b")
        long = "x" * 200
        out = log.preview(long, 20)
        self.assertEqual(len(out), 20)
        self.assertTrue(out.endswith("..."))

    def test_kv_skips_empty(self) -> None:
        self.assertEqual(log.kv(a=1, b=None, c="", d="ok"), "a=1 d='ok'")
        self.assertEqual(log.kv(), "")

    def test_disabled_writes_nothing(self) -> None:
        log.configure(enabled=False)
        self.assertFalse(log.enabled())
        buf = io.StringIO()
        with redirect_stderr(buf):
            log.debug("d")
            log.info("i")
            log.warn("w")
            log.error("e")
            log.exception("x")
        self.assertEqual(buf.getvalue(), "")

    def test_enabled_writes_levels(self) -> None:
        log.configure(enabled=True)
        self.assertTrue(log.enabled())
        buf = io.StringIO()
        with redirect_stderr(buf):
            log.info("hello")
            log.warn("careful")
            log.error("boom")
            log.debug("detail")
        text = buf.getvalue()
        self.assertIn("[INFO ] hello", text)
        self.assertIn("[WARN ] careful", text)
        self.assertIn("[ERROR] boom", text)
        self.assertIn("[DEBUG] detail", text)

    def test_exception_prints_traceback_when_enabled(self) -> None:
        log.configure(enabled=True)
        buf = io.StringIO()
        with redirect_stderr(buf):
            try:
                raise ValueError("nope")
            except ValueError:
                log.exception("failed")
        text = buf.getvalue()
        self.assertIn("failed", text)
        self.assertIn("ValueError", text)

    def test_loading_skips_when_logs_enabled(self) -> None:
        log.configure(enabled=True)
        with log.loading("wait"):
            pass

    def test_loading_skips_when_not_tty(self) -> None:
        log.configure(enabled=False)
        with patch("sys.stderr") as err:
            err.isatty.return_value = False
            with log.loading("wait"):
                pass


if __name__ == "__main__":
    unittest.main()
