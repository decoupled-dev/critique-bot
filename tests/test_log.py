from __future__ import annotations

import io
import os
import sys
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

    def test_nested_loading_keeps_one_spinner(self) -> None:
        class Tty:
            encoding = "utf-8"

            def __init__(self) -> None:
                self.chunks: list[str] = []

            def isatty(self) -> bool:
                return True

            def write(self, text: str) -> int:
                self.chunks.append(text)
                return len(text)

            def flush(self) -> None:
                return None

        err = Tty()
        log.configure(enabled=False)
        with patch("sys.stderr", err):
            with log.loading("Thinking..."):
                outer = log._active_spinner
                self.assertIsNotNone(outer)
                with log.loading("Waiting for assistant..."):
                    self.assertIs(log._active_spinner, outer)
                    assert outer is not None
                    self.assertEqual(outer.message, "Waiting for assistant...")
                self.assertEqual(outer.message, "Thinking...")
            self.assertIsNone(log._active_spinner)
        shown = "".join(err.chunks)
        self.assertIn("Waiting for assistant...", shown)
        self.assertIn("Thinking...", shown)

    def test_print_safe_survives_charmap_codec(self) -> None:
        class CharmapStream:
            encoding = "cp1252"

            def __init__(self) -> None:
                self.chunks: list[str] = []

            def write(self, text: str) -> int:
                text.encode("cp1252")
                self.chunks.append(text)
                return len(text)

            def flush(self) -> None:
                return None

        stream = CharmapStream()
        hyphen = "risk: non\u2011blocking path"
        with self.assertRaises(UnicodeEncodeError):
            stream.write(hyphen)
        log.print_safe(hyphen, file=stream, flush=True)
        self.assertTrue(stream.chunks)
        self.assertNotIn("\u2011", "".join(stream.chunks))

    def test_configure_stdio_utf8_replace(self) -> None:
        buf = io.BytesIO()
        wrapper = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
        original_out = sys.stdout
        original_err = sys.stderr
        sys.stdout = wrapper
        try:
            log.configure_stdio()
            wrapper.write("non\u2011breaking")
            wrapper.flush()
            data = buf.getvalue()
        finally:
            sys.stdout = original_out
            sys.stderr = original_err
            wrapper.close()
        self.assertIn(b"non", data)


class PaintTests(unittest.TestCase):
    def test_plain_when_not_a_tty(self) -> None:
        buf = io.StringIO()
        self.assertEqual(log.paint("hello", log.MODEL_COLOR, file=buf), "hello")

    def test_color_when_tty(self) -> None:
        buf = io.StringIO()
        buf.isatty = lambda: True  # type: ignore[method-assign]
        env = os.environ.copy()
        env.pop("NO_COLOR", None)
        with patch.dict(os.environ, env, clear=True):
            painted = log.paint("hello", log.MODEL_COLOR, file=buf)
        self.assertEqual(painted, f"\033[{log.MODEL_COLOR}mhello\033[0m")

    def test_no_color_env_disables(self) -> None:
        buf = io.StringIO()
        buf.isatty = lambda: True  # type: ignore[method-assign]
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            self.assertEqual(log.paint("hello", "35", file=buf), "hello")


if __name__ == "__main__":
    unittest.main()
