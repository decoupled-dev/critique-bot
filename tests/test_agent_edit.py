"""agent_edit: byte-exact saves, permissions, symlinks, and undo checkpoints."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from critique_bot import agent_edit
from critique_bot.agent_edit import Checkpoints, atomic_write_bytes, load_text, save_text, undo_last


class EncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def test_non_utf8_file_round_trips_every_byte(self) -> None:
        path = self.root / "legacy.txt"
        raw = b"caf\xe9\nkeep \x81\x8d\xff bytes\nlast\n"
        path.write_bytes(raw)
        loaded = load_text(path)
        self.assertNotIn("�", loaded.text)
        save_text(path, loaded.text, loaded)
        self.assertEqual(path.read_bytes(), raw)
        save_text(path, loaded.text.replace("last", "LAST"), loaded)
        self.assertEqual(path.read_bytes(), raw.replace(b"last", b"LAST"))

    def test_unencodable_new_text_is_refused_not_mangled(self) -> None:
        path = self.root / "legacy.txt"
        path.write_bytes(b"caf\xe9\n")
        loaded = load_text(path)
        with self.assertRaises(ValueError) as caught:
            save_text(path, loaded.text + "☃\n", loaded)
        self.assertIn("cannot hold", str(caught.exception))
        self.assertEqual(path.read_bytes(), b"caf\xe9\n")

    def test_bom_is_kept(self) -> None:
        path = self.root / "bom.txt"
        path.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
        loaded = load_text(path)
        self.assertTrue(loaded.bom)
        save_text(path, loaded.text.replace("two", "2"), loaded)
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbfone\r\n2\r\n")

    def test_mixed_endings_untouched_lines_keep_theirs(self) -> None:
        path = self.root / "mixed.txt"
        path.write_bytes(b"a\r\nb\nc\rd\r\ne\r\n")
        loaded = load_text(path)
        self.assertEqual(loaded.eol, "\r\n")
        text = loaded.text.replace("b\n", "B\nnew\n")
        save_text(path, text, loaded)
        self.assertEqual(path.read_bytes(), b"a\r\nB\r\nnew\r\nc\rd\r\ne\r\n")

    def test_uniform_crlf_file_stays_crlf(self) -> None:
        path = self.root / "crlf.txt"
        path.write_bytes(b"one\r\ntwo\r\n")
        loaded = load_text(path)
        save_text(path, loaded.text + "three\n", loaded)
        self.assertEqual(path.read_bytes(), b"one\r\ntwo\r\nthree\r\n")


class WriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_new_file_gets_umask_permissions_not_0600(self) -> None:
        path = self.root / "new.txt"
        atomic_write_bytes(path, b"x")
        mask = os.umask(0)
        os.umask(mask)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o666 & ~mask)

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_existing_file_keeps_its_mode(self) -> None:
        path = self.root / "run.sh"
        path.write_bytes(b"old")
        path.chmod(0o750)
        atomic_write_bytes(path, b"new")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o750)

    @unittest.skipIf(sys.platform == "win32", "symlinks need privileges on Windows")
    def test_symlink_is_written_through(self) -> None:
        target = self.root / "real.txt"
        target.write_bytes(b"old")
        link = self.root / "link.txt"
        link.symlink_to(target)
        atomic_write_bytes(link, b"new")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"new")


class CheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.cache = self.root / ".bot" / "cache"

    def test_task_folders_sort_in_creation_order(self) -> None:
        checkpoints = Checkpoints(self.cache, self.root)
        names = []
        for _ in range(30):
            checkpoints.start_task()
            assert checkpoints.task_dir is not None
            checkpoints.task_dir.mkdir(parents=True)
            names.append(checkpoints.task_dir.name)
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(set(names)), len(names))

    def test_undo_restores_latest_task_and_removes_created_folders(self) -> None:
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        first = Checkpoints(self.cache, self.root)
        first.start_task()
        first.save(self.root / "a.txt")
        (self.root / "a.txt").write_text("two\n", encoding="utf-8")
        second = Checkpoints(self.cache, self.root)
        second.start_task()
        second.save(self.root / "a.txt")
        new_file = self.root / "deep" / "er" / "n.txt"
        second.save(new_file)
        new_file.parent.mkdir(parents=True)
        new_file.write_text("n", encoding="utf-8")
        (self.root / "a.txt").write_text("three\n", encoding="utf-8")
        restored = undo_last(self.cache, self.root)
        self.assertEqual(sorted(restored), ["a.txt", "deep/er/n.txt"])
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "two\n")
        self.assertFalse((self.root / "deep").exists())
        undo_last(self.cache, self.root)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "one\n")

    def test_outside_path_is_checkpointed_by_absolute_path(self) -> None:
        outside = Path(tempfile.mkdtemp()) / "conf.txt"
        outside.write_text("before\n", encoding="utf-8")
        checkpoints = Checkpoints(self.cache, self.root)
        checkpoints.start_task()
        checkpoints.save(outside)
        outside.write_text("after\n", encoding="utf-8")
        self.assertIn("+after", checkpoints.disk_diff() or "")
        restored = undo_last(self.cache, self.root)
        self.assertEqual(restored, [str(outside.resolve())])
        self.assertEqual(outside.read_text(encoding="utf-8"), "before\n")

    def test_task_stamp_never_sorts_before_an_existing_task(self) -> None:
        undo = self.cache / "undo"
        (undo / "99999999T999999.999999999-9999").mkdir(parents=True)
        name = agent_edit._task_stamp(undo)
        self.assertGreater(name, "99999999T999999.999999999-9999")


if __name__ == "__main__":
    unittest.main()
