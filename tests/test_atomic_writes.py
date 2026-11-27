# SPDX-License-Identifier: Apache-2.0
"""Edits and whole-file writes keep everything about a file except the edited text.

CRLF, a BOM, no final newline, the executable bit and a Latin-1 file round-trip; a crash
between the write and the rename leaves the original intact and no temp file behind.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from critique_bot import agent_edit
from critique_bot.agent_tools import TaskState, ToolContext, execute


class RoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ctx = ToolContext(workspace=self.root, state=TaskState(task="t"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _edit(self, name: str, old: str, new: str) -> dict:
        execute("read_files", {"path": name}, self.ctx)
        result = execute("edit_file", {"path": name, "old_string": old, "new_string": new}, self.ctx)
        self.assertTrue(result["ok"], result)
        return result

    def test_crlf(self) -> None:
        (self.root / "a.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
        self._edit("a.txt", "two\n", "TWO\nmore\n")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"one\r\nTWO\r\nmore\r\nthree\r\n")

    def test_bom(self) -> None:
        (self.root / "a.cs").write_bytes(b"\xef\xbb\xbfclass A { int x = 1; }\n")
        self._edit("a.cs", "x = 1", "x = 2")
        self.assertEqual((self.root / "a.cs").read_bytes(), b"\xef\xbb\xbfclass A { int x = 2; }\n")

    def test_no_final_newline(self) -> None:
        (self.root / "a.txt").write_bytes(b"alpha\nbeta")
        self._edit("a.txt", "alpha", "ALPHA")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"ALPHA\nbeta")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_executable_bit(self) -> None:
        script = self.root / "run.sh"
        script.write_bytes(b"#!/bin/sh\necho one\n")
        script.chmod(0o755)
        self._edit("run.sh", "echo one", "echo two")
        self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o755)
        self.assertEqual(script.read_bytes(), b"#!/bin/sh\necho two\n")

    def test_latin1(self) -> None:
        (self.root / "legacy.txt").write_bytes("caf\xe9 = 1\nna\xefve = 2\n".encode("latin-1"))
        self._edit("legacy.txt", "= 2", "= 3")
        self.assertEqual((self.root / "legacy.txt").read_bytes(), "caf\xe9 = 1\nna\xefve = 3\n".encode("latin-1"))

    def test_whole_file_write_keeps_crlf_bom_and_final_newline_state(self) -> None:
        (self.root / "a.txt").write_bytes(b"\xef\xbb\xbfone\r\ntwo")
        execute("read_files", {"path": "a.txt"}, self.ctx)
        result = execute("write_files", {"path": "a.txt", "contents": "one\nTWO\n"}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"\xef\xbb\xbfone\r\nTWO")

    def test_whole_file_write_adds_the_final_newline_the_file_had(self) -> None:
        (self.root / "a.txt").write_bytes(b"one\ntwo\n")
        execute("read_files", {"path": "a.txt"}, self.ctx)
        execute("write_files", {"path": "a.txt", "contents": "one\nTWO"}, self.ctx)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"one\nTWO\n")


class CrashTests(unittest.TestCase):
    def test_a_crash_before_the_rename_leaves_the_original_and_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "a.txt"
            target.write_bytes(b"original\n")
            with mock.patch.object(agent_edit, "_replace_with_retry", side_effect=OSError("power cut")):
                with self.assertRaises(OSError):
                    agent_edit.atomic_write_bytes(target, b"new\n")
            self.assertEqual(target.read_bytes(), b"original\n")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["a.txt"])

    def test_edit_tool_reports_the_failed_write_and_keeps_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_bytes(b"one\n")
            with mock.patch.object(agent_edit, "_replace_with_retry", side_effect=OSError("disk full")):
                result = execute("edit_file", {"path": "a.txt", "old_string": "one", "new_string": "two"},
                                 ToolContext(workspace=root))
            self.assertFalse(result["ok"])
            self.assertIn("disk full", result["error"])
            self.assertEqual((root / "a.txt").read_bytes(), b"one\n")

    def test_temp_file_is_fsynced_in_the_same_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.txt"
            seen: list[int] = []
            real = os.fsync
            with mock.patch.object(agent_edit.os, "fsync", side_effect=lambda fd: (seen.append(fd), real(fd))):
                agent_edit.atomic_write_bytes(target, b"x\n")
            self.assertTrue(seen)
            self.assertEqual(target.read_bytes(), b"x\n")


if __name__ == "__main__":
    unittest.main()
