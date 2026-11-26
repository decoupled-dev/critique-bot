"""Windowed reads of very large files: sparse line index, long-line clipping, read_files."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from critique_bot import agent_edit, big_file
from critique_bot.agent_tools import TaskState, ToolContext, execute


class LineIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _file(self, lines: int, *, trailing: bool = True) -> Path:
        path = self.root / "big.log"
        text = "\n".join(f"line {i}" for i in range(1, lines + 1))
        path.write_text(text + ("\n" if trailing else ""))
        return path

    def test_windows_anywhere_in_the_file(self) -> None:
        path = self._file(50_000)
        self.assertEqual(big_file.line_index(path).total, 50_000)
        self.assertEqual(big_file.read_lines(path, 1, 2), ["line 1", "line 2"])
        self.assertEqual(big_file.read_lines(path, big_file.STEP, 3),
                         [f"line {big_file.STEP}", f"line {big_file.STEP + 1}", f"line {big_file.STEP + 2}"])
        self.assertEqual(big_file.read_lines(path, 49_999, 10), ["line 49999", "line 50000"])
        self.assertEqual(big_file.read_lines(path, 50_001, 10), [])

    def test_no_trailing_newline_and_index_refresh(self) -> None:
        path = self._file(10, trailing=False)
        self.assertEqual(big_file.line_index(path).total, 10)
        time.sleep(0.01)
        path.write_text("a\nb\n")
        self.assertEqual(big_file.line_index(path).total, 2)

    def test_clip_line(self) -> None:
        long = "x" * 5000
        clipped = big_file.clip_line(long, limit=100)
        self.assertTrue(clipped.startswith("x" * 100))
        self.assertIn("+4900 chars; read on with col=100", clipped)
        self.assertTrue(big_file.clip_line(long, limit=100, column=4950).endswith("x" * 50))
        self.assertIn("past its end", big_file.clip_line("abc", column=10))


class ReadFilesWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ctx = ToolContext(workspace=self.root, state=TaskState(task="t"))
        path = self.root / "logcat.txt"
        with open(path, "w") as out:
            for i in range(1, 120_001):
                if i == 70_000:
                    out.write("E AndroidRuntime: FATAL EXCEPTION: main " + "y" * 30_000 + "\n")
                elif i == 80_000:
                    out.write("token=ghp_" + "A" * 36 + "\n")
                else:
                    out.write(f"I CarService: event {i}\n")
        self.path = path

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_large_file_is_not_loaded_whole(self) -> None:
        with mock.patch.object(big_file, "LARGE_FILE_BYTES", 1024), mock.patch.object(
            agent_edit, "load_text", side_effect=AssertionError("loaded the whole file")
        ):
            result = execute("read_files", {"path": "logcat.txt", "offset": 69_999, "limit": 3}, self.ctx)
        out = result["output"]
        self.assertIn("lines 69999-70001 of 120000", out)
        self.assertIn("70000|E AndroidRuntime: FATAL EXCEPTION", out)
        self.assertIn("read on with col=", out)
        self.assertLess(len(out), 6000)

    def test_column_reads_on_in_a_long_line(self) -> None:
        with mock.patch.object(big_file, "LARGE_FILE_BYTES", 1024):
            result = execute("read_files", {"path": "logcat.txt", "offset": 70_000, "limit": 1, "col": 29_000}, self.ctx)
        self.assertIn("70000|[from col=29000] yyy", result["output"])

    def test_secrets_in_a_window_are_hidden(self) -> None:
        with mock.patch.object(big_file, "LARGE_FILE_BYTES", 1024):
            result = execute("read_files", {"path": "logcat.txt", "offset": 79_999, "limit": 3}, self.ctx)
        self.assertNotIn("ghp_" + "A" * 36, result["output"])
        self.assertIn("80000|", result["output"])


if __name__ == "__main__":
    unittest.main()
