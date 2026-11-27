"""apply_patch without git: crit's own parser for unified diffs and V4A patches.

Hunks are found by context through the edit match ladder, so wrong line numbers and
shifted context still apply; CRLF files keep CRLF; an ambiguous hunk is refused; a
patch over several files applies all or nothing.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from critique_bot import agent_edit, agent_patch, run_stats
from critique_bot.agent_patch import PatchError, apply_hunks, parse_patch
from critique_bot.agent_tools import TaskState, ToolContext, execute, patched_paths

SOURCE = "".join(f"line {i}\n" for i in range(1, 21))


class ParserTests(unittest.TestCase):
    def test_git_diff_with_prefixes(self) -> None:
        files = parse_patch("diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n")
        self.assertEqual([(f.path, f.kind) for f in files], [("x.py", "update")])
        self.assertEqual(files[0].hunks[0].old_lines, ["a", "b"])
        self.assertEqual(files[0].hunks[0].new_lines, ["a", "c"])

    def test_plain_paths_new_and_deleted_files(self) -> None:
        files = parse_patch("--- /dev/null\n+++ new.txt\n@@ -0,0 +1 @@\n+hi\n--- old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n")
        self.assertEqual([(f.path, f.kind) for f in files], [("new.txt", "add"), ("old.txt", "delete")])

    def test_rename(self) -> None:
        files = parse_patch("diff --git a/r1.txt b/r2.txt\nsimilarity index 100%\nrename from r1.txt\nrename to r2.txt\n")
        self.assertEqual((files[0].path, files[0].move_to), ("r1.txt", "r2.txt"))

    def test_header_without_numbers_and_a_minus_minus_line(self) -> None:
        files = parse_patch("--- a/q.sql\n+++ b/q.sql\n@@ @@\n--- old comment\n+-- new comment\n select 1;\n")
        hunk = files[0].hunks[0]
        self.assertEqual(hunk.old_lines, ["-- old comment", "select 1;"])
        self.assertEqual(hunk.new_lines, ["-- new comment", "select 1;"])

    def test_no_newline_markers(self) -> None:
        files = parse_patch("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n")
        hunk = files[0].hunks[0]
        self.assertTrue(hunk.old_no_eol and hunk.new_no_eol)

    def test_v4a(self) -> None:
        patch = (
            "*** Begin Patch\n"
            "*** Update File: src/app.py\n"
            "@@ def main():\n"
            "     x = 1\n"
            "-    y = 2\n"
            "+    y = 3\n"
            "*** Add File: src/new.py\n"
            "+print('hi')\n"
            "*** Delete File: src/old.py\n"
            "*** Update File: src/a.py\n"
            "*** Move to: src/b.py\n"
            "@@\n"
            "-a\n"
            "+b\n"
            "*** End Patch\n"
        )
        files = parse_patch(patch)
        self.assertEqual([(f.path, f.kind, f.move_to) for f in files], [
            ("src/app.py", "update", ""), ("src/new.py", "add", ""), ("src/old.py", "delete", ""),
            ("src/a.py", "update", "src/b.py"),
        ])
        self.assertEqual(files[0].hunks[0].anchor, "def main():")
        self.assertEqual(agent_patch.new_file_text(files[1]), "print('hi')\n")

    def test_fenced_patch(self) -> None:
        files = parse_patch("```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```\n")
        self.assertEqual(files[0].path, "x")

    def test_garbage_is_refused(self) -> None:
        with self.assertRaises(PatchError):
            parse_patch("please change line 3 to say hello")

    def test_patched_paths_include_v4a_headers(self) -> None:
        self.assertEqual(
            patched_paths("*** Begin Patch\n*** Update File: ../evil.py\n*** Move to: x.py\n@@\n-a\n+b\n*** End Patch\n"),
            ["../evil.py", "x.py"],
        )


def _file(text: str) -> agent_patch.FilePatch:
    return parse_patch(text)[0]


class ApplyTests(unittest.TestCase):
    def test_wrong_line_numbers_are_ignored(self) -> None:
        patch = _file("--- a/f\n+++ b/f\n@@ -90,3 +90,3 @@\n line 9\n-line 10\n+LINE TEN\n line 11\n")
        result = apply_hunks(SOURCE, patch)
        self.assertIn("line 9\nLINE TEN\nline 11\n", result.text)
        self.assertEqual(result.steps, ["exact"])

    def test_shifted_context_still_applies(self) -> None:
        shifted = "header\nmore header\n" + SOURCE
        patch = _file("--- a/f\n+++ b/f\n@@ -4,3 +4,3 @@\n line 4\n-line 5\n+line five\n line 6\n")
        self.assertIn("line 4\nline five\nline 6\n", apply_hunks(shifted, patch).text)

    def test_indentation_drift_goes_through_the_ladder(self) -> None:
        text = "def f():\n    a = 1\n    return a\n"
        patch = _file("--- a/f.py\n+++ b/f.py\n@@ -1,3 +1,3 @@\n def f():\n-  a = 1\n+  a = 2\n   return a\n")
        result = apply_hunks(text, patch)
        self.assertEqual(result.text, "def f():\n    a = 2\n    return a\n")
        self.assertEqual(result.steps, ["indent"])

    def test_several_hunks_in_order(self) -> None:
        patch = _file(
            "--- a/f\n+++ b/f\n@@ -2,1 +2,1 @@\n-line 2\n+two\n@@ -15,1 +15,2 @@\n line 15\n+fifteen and a half\n"
        )
        result = apply_hunks(SOURCE, patch)
        self.assertIn("line 1\ntwo\nline 3\n", result.text)
        self.assertIn("line 15\nfifteen and a half\nline 16\n", result.text)

    def test_ambiguous_hunk_is_refused(self) -> None:
        text = "if a:\n    go()\nif b:\n    go()\n"
        with self.assertRaises(PatchError) as caught:
            apply_hunks(text, _file("--- a/f\n+++ b/f\n@@ -1 +1 @@\n-    go()\n+    stop()\n"))
        self.assertIn("matches 2 places", str(caught.exception))
        self.assertIn("lines 2, 4", str(caught.exception))

    def test_a_hunk_that_does_not_match_shows_the_closest_text(self) -> None:
        with self.assertRaises(PatchError) as caught:
            apply_hunks(SOURCE, _file("--- a/f\n+++ b/f\n@@ -1 +1 @@\n-line 999 is not here\n+x\n"))
        self.assertIn("does not apply", str(caught.exception))

    def test_pure_insertion_uses_the_start_line(self) -> None:
        result = apply_hunks("a\nb\n", _file("--- a/f\n+++ b/f\n@@ -1,0 +2 @@\n+inserted\n"))
        self.assertEqual(result.text, "a\ninserted\nb\n")
        top = apply_hunks("a\nb\n", _file("--- a/f\n+++ b/f\n@@ -0,0 +1 @@\n+first\n"))
        self.assertEqual(top.text, "first\na\nb\n")

    def test_end_of_file_newline_markers(self) -> None:
        patch = _file("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n")
        self.assertEqual(apply_hunks("a\n", patch).text, "b")
        back = _file("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-b\n\\ No newline at end of file\n+b\n")
        self.assertEqual(apply_hunks("b", back).text, "b\n")

    def test_v4a_anchor_picks_the_right_block(self) -> None:
        text = "def a():\n    x = 1\n\ndef b():\n    x = 1\n"
        patch = _file("*** Begin Patch\n*** Update File: f.py\n@@ def b():\n-    x = 1\n+    x = 2\n*** End Patch\n")
        self.assertEqual(apply_hunks(text, patch).text, "def a():\n    x = 1\n\ndef b():\n    x = 2\n")


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        run_stats.reset()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        run_stats.reset()

    def _run(self, patch: str, ctx: ToolContext | None = None) -> dict:
        return execute("apply_patch", {"patch": patch}, ctx or ToolContext(workspace=self.root))

    def test_crlf_file_and_crlf_patch(self) -> None:
        (self.root / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
        result = self._run("--- a/w.txt\r\n+++ b/w.txt\r\n@@ -2 +2 @@\r\n-two\r\n+TWO\r\n")
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.root / "w.txt").read_bytes(), b"one\r\nTWO\r\nthree\r\n")
        self.assertIn("updated w.txt (now line 2)", result["output"])

    def test_all_files_or_none(self) -> None:
        (self.root / "a.txt").write_text("alpha\n", encoding="utf-8")
        (self.root / "b.txt").write_text("beta\n", encoding="utf-8")
        result = self._run("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-alpha\n+ALPHA\n"
                           "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-gamma\n+GAMMA\n")
        self.assertFalse(result["ok"])
        self.assertIn("b.txt", result["error"])
        self.assertIn("no file was changed", result["error"])
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "alpha\n")

    def test_failed_write_part_way_restores_the_files_written(self) -> None:
        (self.root / "a.txt").write_text("alpha\n", encoding="utf-8")
        (self.root / "b.txt").write_text("beta\n", encoding="utf-8")
        real = agent_edit.save_text
        calls = []

        def flaky(path, text, like=None):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(path, text, like)

        with mock.patch.object(agent_edit, "save_text", side_effect=flaky):
            result = self._run("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-alpha\n+ALPHA\n"
                               "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-beta\n+BETA\n")
        self.assertFalse(result["ok"])
        self.assertIn("every file is as it was", result["error"])
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "alpha\n")
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), "beta\n")

    def test_v4a_add_update_delete_and_move(self) -> None:
        (self.root / "keep.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "gone.py").write_text("y = 1\n", encoding="utf-8")
        (self.root / "old.py").write_text("z = 1\n", encoding="utf-8")
        result = self._run(
            "*** Begin Patch\n*** Add File: made.py\n+w = 1\n*** Update File: keep.py\n@@\n-x = 1\n+x = 2\n"
            "*** Delete File: gone.py\n*** Update File: old.py\n*** Move to: moved.py\n@@\n-z = 1\n+z = 2\n*** End Patch\n"
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.root / "made.py").read_text(encoding="utf-8"), "w = 1\n")
        self.assertEqual((self.root / "keep.py").read_text(encoding="utf-8"), "x = 2\n")
        self.assertFalse((self.root / "gone.py").exists())
        self.assertFalse((self.root / "old.py").exists())
        self.assertEqual((self.root / "moved.py").read_text(encoding="utf-8"), "z = 2\n")

    def test_adding_an_existing_file_is_refused(self) -> None:
        (self.root / "made.py").write_text("w = 0\n", encoding="utf-8")
        result = self._run("--- /dev/null\n+++ b/made.py\n@@ -0,0 +1 @@\n+w = 1\n")
        self.assertFalse(result["ok"])
        self.assertIn("already exists", result["error"])

    def test_steps_are_recorded(self) -> None:
        (self.root / "f.py").write_text("def f():\n    a = 1\n", encoding="utf-8")
        self._run("--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n def f():\n-  a = 1\n+  a = 2\n")
        self.assertEqual(run_stats.snapshot()["edits"]["apply_patch"]["steps"], {"indent": 1})

    def test_stale_file_is_refused(self) -> None:
        (self.root / "f.txt").write_text("a\n", encoding="utf-8")
        ctx = ToolContext(workspace=self.root, state=TaskState(task="t"))
        execute("read_files", {"path": "f.txt"}, ctx)
        (self.root / "f.txt").write_text("a\nb\n", encoding="utf-8")
        result = self._run("--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+c\n", ctx)
        self.assertFalse(result["ok"])
        self.assertIn("changed on disk", result["error"])

    def test_breaking_json_is_not_applied(self) -> None:
        (self.root / "c.json").write_text('{"a": 1}\n', encoding="utf-8")
        result = self._run('--- a/c.json\n+++ b/c.json\n@@ -1 +1 @@\n-{"a": 1}\n+{"a": 1,\n')
        self.assertFalse(result["ok"])
        self.assertIn("syntax", result["error"])
        self.assertEqual((self.root / "c.json").read_text(encoding="utf-8"), '{"a": 1}\n')


if __name__ == "__main__":
    unittest.main()
