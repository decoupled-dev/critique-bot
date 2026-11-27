"""The edit match ladder (agent_edit.replace_span) and edit_file on top of it.

Each ladder step has fixtures: exact, line endings / trailing whitespace, indentation
(tabs vs spaces, 2 vs 4 spaces), markdown punctuation, fuzzy (a near miss that applies
and one that must refuse), and the refusal with the closest candidate. edit_file adds
the diff and the new line range, per-file failure counting with the whole-file hint,
stale-read protection and all-or-nothing batches.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from critique_bot import agent_edit, run_stats
from critique_bot.agent_edit import replace_span
from critique_bot.agent_tools import TaskState, ToolContext, execute

PY = "def f():\n    x = 1\n    return x\n\ndef g():\n    x = 1\n    return x + 1\n"


class ExactStepTests(unittest.TestCase):
    def test_unique_hit_applies_with_its_range(self) -> None:
        result = replace_span(PY, "    return x\n", "    return x * 2\n")
        self.assertEqual(result.step, "exact")
        self.assertIn("return x * 2\n\ndef g", result.text)
        self.assertEqual(result.lines, (3, 3))

    def test_several_hits_are_refused_with_their_line_numbers(self) -> None:
        result = replace_span(PY, "x = 1", "x = 2")
        self.assertIsNone(result.text)
        self.assertEqual(result.hits, (2, 6))
        self.assertIn("lines 2, 6", result.note)
        self.assertIn("replace_all", result.note)

    def test_replace_all_changes_every_hit(self) -> None:
        result = replace_span(PY, "x = 1", "x = 2", replace_all=True)
        self.assertEqual(result.count, 2)
        self.assertEqual(result.text.count("x = 2"), 2)
        self.assertEqual(result.lines, (2, 6))

    def test_copied_line_number_prefixes_are_removed(self) -> None:
        result = replace_span(PY, "2|    x = 1\n3|    return x\n", "    x = 5\n    return x\n")
        self.assertEqual(result.step, "exact")
        self.assertIn("removed N| prefixes", result.note)
        self.assertTrue(result.text.startswith("def f():\n    x = 5\n"))

    def test_same_text_is_refused(self) -> None:
        result = replace_span(PY, "x = 1", "x = 1")
        self.assertIsNone(result.text)
        self.assertIn("same text", result.note)

    def test_an_indentation_only_change_is_allowed(self) -> None:
        text = "if a:\n  b()\n"
        result = replace_span(text, "if a:\n  b()\n", "if a:\n    b()\n")
        self.assertEqual(result.text, "if a:\n    b()\n")


class WhitespaceStepTests(unittest.TestCase):
    def test_trailing_whitespace_and_crlf_in_old_string(self) -> None:
        result = replace_span(PY, "def f():   \r\n    x = 1  \r\n", "def f():\n    x = 9\n")
        self.assertEqual(result.step, "whitespace")
        self.assertTrue(result.text.startswith("def f():\n    x = 9\n    return x\n"))

    def test_trailing_whitespace_in_the_file(self) -> None:
        text = "a = 1   \nb = 2\t\nc = 3\n"
        result = replace_span(text, "a = 1\nb = 2\n", "a = 10\nb = 20\n")
        self.assertEqual(result.step, "whitespace")
        self.assertEqual(result.text, "a = 10\nb = 20\nc = 3\n")

    def test_last_line_without_a_final_newline(self) -> None:
        result = replace_span("a\nb", "b\n", "c\n")
        self.assertEqual(result.text, "a\nc")


class IndentStepTests(unittest.TestCase):
    def test_four_spaces_in_the_file_two_in_old_string(self) -> None:
        result = replace_span(PY, "def f():\n  x = 1\n  return x\n", "def f():\n  x = 3\n  if x:\n    return x\n")
        self.assertEqual(result.step, "indent")
        self.assertTrue(result.text.startswith("def f():\n    x = 3\n    if x:\n        return x\n\ndef g"))

    def test_tabs_in_the_file_spaces_in_old_string(self) -> None:
        java = "class A {\n\tvoid f() {\n\t\trun();\n\t}\n}\n"
        result = replace_span(java, "void f() {\n    run();\n}", "void f() {\n    run();\n    done();\n}")
        self.assertEqual(result.step, "indent")
        self.assertEqual(result.text, "class A {\n\tvoid f() {\n\t\trun();\n\t\tdone();\n\t}\n}\n")

    def test_spaces_in_the_file_tabs_in_old_string(self) -> None:
        text = "class A {\n    void f() {\n        run();\n    }\n}\n"
        result = replace_span(text, "\tvoid f() {\n\t\trun();", "\tvoid f() {\n\t\tgo();")
        self.assertEqual(result.step, "indent")
        self.assertIn("    void f() {\n        go();\n", result.text)

    def test_shifted_block_is_reindented_to_the_hit(self) -> None:
        java = "class A {\n    void f() {\n        run();\n    }\n}\n"
        result = replace_span(java, "void f() {\n    run();\n}", "void f() {\n    run();\n    done();\n}")
        self.assertIn("        done();\n", result.text)

    def test_ambiguous_after_ignoring_indent_lists_lines(self) -> None:
        text = "if a:\n    go()\nif b:\n  pass\nif a:\n        go()\n"
        result = replace_span(text, "if a:\n  go()", "if a:\n  stop()")
        self.assertIsNone(result.text)
        self.assertEqual(result.hits, (1, 5))


class PunctuationStepTests(unittest.TestCase):
    def test_markdown_backticks_dropped_by_the_chat(self) -> None:
        text = "`bot-agent` is an alias for `--mode agent`.\n"
        result = replace_span(text, "bot-agent is an alias for --mode agent.", "`bot-agent` runs the agent.")
        self.assertEqual(result.step, "punctuation")
        self.assertEqual(result.text, "`bot-agent` runs the agent.\n")


class FuzzyStepTests(unittest.TestCase):
    BODY = (
        "def total(items):\n"
        "    result = 0\n"
        "    for item in items:\n"
        "        result += item.price * item.count\n"
        "    return result\n"
    )

    def test_near_miss_applies_and_says_so(self) -> None:
        text = "import os\n\n" + self.BODY + "\nprint(1)\n"
        old = self.BODY.replace("item.count", "item.qty")  # one word misremembered
        new = self.BODY.replace("item.price * item.count", "item.cost()")
        result = replace_span(text, old, new)
        self.assertEqual(result.step, "fuzzy")
        self.assertIn("matched approximately at lines 3-7", result.note)
        self.assertIn("check the diff", result.note)
        self.assertIn("result += item.cost()", result.text)
        self.assertEqual(result.lines, (3, 7))

    def test_near_miss_with_a_close_runner_up_is_refused(self) -> None:
        other = self.BODY.replace("total", "subtotal").replace("item.count", "item.amount")
        text = self.BODY + "\n" + other
        old = self.BODY.replace("total", "totals").replace("item.count", "item.cnt")
        result = replace_span(text, old, "x\n")
        self.assertIsNone(result.text)
        self.assertEqual(len(result.hits), 2)
        self.assertIn("about equally close", result.note)

    def test_far_miss_is_refused(self) -> None:
        old = "def total(items):\n    return sum(i.price for i in items)\n"
        result = replace_span(self.BODY, old, "x\n")
        self.assertIsNone(result.text)
        self.assertEqual(result.step, "")


class RefusalTests(unittest.TestCase):
    def test_closest_candidate_and_the_line_that_differs(self) -> None:
        text = "def f(x):\n    if x:\n        return x + 1\n    return 0\n"
        result = replace_span(text, "def f(x):\n    if x:\n        return compute(x, 100)\n", "y")
        self.assertIsNone(result.text)
        self.assertIn("line 3 of old_string differs", result.note)
        self.assertIn("'        return compute(x, 100)'", result.note)
        self.assertIn("file line 3 has '        return x + 1'", result.note)
        self.assertIn("1|def f(x):", result.candidate)
        self.assertLessEqual(len(result.candidate.split("\n")), agent_edit.CANDIDATE_MAX_LINES)

    def test_nothing_close(self) -> None:
        result = replace_span("alpha\nbeta\n", "completely different text here", "y")
        self.assertIsNone(result.text)
        self.assertIn("not found", result.note)


def _ctx(root: Path, state: TaskState | None = None, settings: dict | None = None) -> ToolContext:
    return ToolContext(workspace=root, state=state, settings=settings)


class EditFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        run_stats.reset()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        run_stats.reset()

    def _read(self, ctx: ToolContext, path: str) -> None:
        result = execute("read_files", {"path": path, "force": True}, ctx)
        self.assertTrue(result["ok"], result)

    def test_result_has_the_diff_the_range_and_the_step(self) -> None:
        (self.root / "a.py").write_text(PY, encoding="utf-8")
        result = execute("edit_file", {"path": "a.py", "old_string": "    return x\n", "new_string": "    y = x\n    return y\n"},
                         _ctx(self.root))
        self.assertTrue(result["ok"], result)
        self.assertIn("now lines 3-4", result["output"])
        self.assertIn("+    y = x", result["output"])
        self.assertEqual(result["match"], "exact")
        self.assertEqual(run_stats.snapshot()["edits"]["edit_file"]["steps"], {"exact": 1})

    def test_ambiguous_edit_lists_hit_lines(self) -> None:
        (self.root / "a.py").write_text(PY, encoding="utf-8")
        result = execute("edit_file", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}, _ctx(self.root))
        self.assertFalse(result["ok"])
        self.assertIn("lines 2, 6", result["error"])
        self.assertEqual(result["hits"], [2, 6])

    def test_batch_of_five_with_one_failing_changes_nothing(self) -> None:
        text = "".join(f"line {i}\n" for i in range(10))
        (self.root / "n.txt").write_text(text, encoding="utf-8")
        edits = [{"old_string": f"line {i}\n", "new_string": f"LINE {i}\n"} for i in (1, 2, 3, 4)]
        edits.insert(2, {"old_string": "line 99\n", "new_string": "x\n"})
        result = execute("edit_file", {"path": "n.txt", "edits": edits}, _ctx(self.root))
        self.assertFalse(result["ok"])
        self.assertIn("edit 3 of 5", result["error"])
        self.assertIn("no edit was applied", result["error"])
        self.assertEqual((self.root / "n.txt").read_text(encoding="utf-8"), text)

    def test_whole_file_hint_after_repeated_failures_then_reset(self) -> None:
        (self.root / "a.py").write_text(PY, encoding="utf-8")
        ctx = _ctx(self.root, TaskState(task="t"))
        self._read(ctx, "a.py")
        bad = {"path": "a.py", "old_string": "def nothing_like_this():\n    pass\n", "new_string": "x\n"}
        first = execute("edit_file", bad, ctx)
        self.assertNotIn("write_files", first["error"])
        second = execute("edit_file", bad, ctx)
        self.assertIn("Use write_files with the full new content", second["error"])
        self.assertEqual(second["hint"], "whole_file")
        good = execute("edit_file", {"path": "a.py", "old_string": "    return x\n", "new_string": "    return -x\n"}, ctx)
        self.assertTrue(good["ok"], good)
        self.assertNotIn("a.py", ctx.state.edit_failures)

    def test_long_file_gets_the_smaller_edits_hint(self) -> None:
        (self.root / "big.py").write_text("".join(f"v{i} = {i}\n" for i in range(900)), encoding="utf-8")
        ctx = _ctx(self.root, TaskState(task="t"), {"edit_fail_limit": 1})
        self._read(ctx, "big.py")
        result = execute("edit_file", {"path": "big.py", "old_string": "nothing = 1\nlike = 2\n", "new_string": "x"}, ctx)
        self.assertIn("make smaller edits", result["error"])
        self.assertEqual(result["hint"], "smaller_edits")

    def test_settings_change_the_limits(self) -> None:
        (self.root / "a.py").write_text(PY, encoding="utf-8")
        ctx = _ctx(self.root, TaskState(task="t"), {"edit_fail_limit": 3})
        self._read(ctx, "a.py")
        bad = {"path": "a.py", "old_string": "def nothing_like_this():\n    pass\n", "new_string": "x\n"}
        execute("edit_file", bad, ctx)
        self.assertNotIn("write_files", execute("edit_file", bad, ctx)["error"])
        self.assertIn("write_files", execute("edit_file", bad, ctx)["error"])


class StaleReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "a.py").write_text(PY, encoding="utf-8")
        self.ctx = _ctx(self.root, TaskState(task="t"))
        run_stats.reset()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        run_stats.reset()

    def test_a_file_changed_after_the_read_is_refused_until_read_again(self) -> None:
        execute("read_files", {"path": "a.py"}, self.ctx)
        (self.root / "a.py").write_text(PY.replace("def g", "def h"), encoding="utf-8")
        edit = {"path": "a.py", "old_string": "    return x\n", "new_string": "    return 0\n"}
        refused = execute("edit_file", edit, self.ctx)
        self.assertFalse(refused["ok"])
        self.assertIn("changed on disk since you read it", refused["error"])
        self.assertIn("Re-read lines 1-", refused["error"])
        self.assertTrue(refused["stale"])
        self.assertEqual(run_stats.snapshot()["stale_edit_refusals"], 1)
        self.assertIn("def h", (self.root / "a.py").read_text(encoding="utf-8"))
        execute("read_files", {"path": "a.py", "force": True}, self.ctx)
        applied = execute("edit_file", edit, self.ctx)
        self.assertTrue(applied["ok"], applied)

    def test_same_size_same_mtime_change_is_still_caught(self) -> None:
        execute("read_files", {"path": "a.py"}, self.ctx)
        path = self.root / "a.py"
        stat = path.stat()
        path.write_text(PY.replace("x = 1", "x = 7", 1), encoding="utf-8")
        import os

        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        refused = execute("edit_file", {"path": "a.py", "old_string": "    return x\n", "new_string": "    return 0\n"}, self.ctx)
        self.assertFalse(refused["ok"])
        self.assertIn("changed on disk", refused["error"])

    def test_own_edits_do_not_make_the_file_stale(self) -> None:
        execute("read_files", {"path": "a.py"}, self.ctx)
        one = execute("edit_file", {"path": "a.py", "old_string": "    return x\n", "new_string": "    return 0\n"}, self.ctx)
        two = execute("edit_file", {"path": "a.py", "old_string": "    return x + 1\n", "new_string": "    return 1\n"}, self.ctx)
        self.assertTrue(one["ok"] and two["ok"], (one, two))

    def test_unread_file_exact_match_applies(self) -> None:
        result = execute("edit_file", {"path": "a.py", "old_string": "    return x + 1\n", "new_string": "    return 2\n"}, self.ctx)
        self.assertTrue(result["ok"], result)

    def test_unread_file_loose_match_is_refused(self) -> None:
        result = execute("edit_file", {"path": "a.py", "old_string": "def g():\n  x = 1\n", "new_string": "def g():\n  x = 2\n"}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertIn("was not read in this task", result["error"])
        self.assertEqual(run_stats.snapshot()["unread_edit_refusals"], 1)
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), PY)

    def test_new_files_need_no_read(self) -> None:
        result = execute("write_files", {"path": "new.py", "contents": "x = 1\n"}, self.ctx)
        self.assertTrue(result["ok"], result)
        edited = execute("edit_file", {"path": "new.py", "old_string": "x = 1", "new_string": "x = 2"}, self.ctx)
        self.assertTrue(edited["ok"], edited)

    def test_stale_refusals_do_not_count_toward_the_whole_file_hint(self) -> None:
        execute("read_files", {"path": "a.py"}, self.ctx)
        (self.root / "a.py").write_text(PY + "# more\n", encoding="utf-8")
        for _ in range(3):
            result = execute("edit_file", {"path": "a.py", "old_string": "    return x\n", "new_string": "y\n"}, self.ctx)
        self.assertNotIn("write_files", result["error"])


if __name__ == "__main__":
    unittest.main()
