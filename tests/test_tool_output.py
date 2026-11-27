"""Output shaping: head and tail within the budget, the full text saved, never empty; read
windows, multi-pattern search with counts first, and response_format."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from critique_bot import tool_api, tool_output
from critique_bot.agent_tools import TaskState, ToolContext, execute


class ShapeOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cache = self.root / ".crit" / "cache"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_head_and_tail_kept_full_text_saved_path_in_result(self) -> None:
        text = "\n".join(f"row {i}" for i in range(5000))
        result = tool_api.shape_output({"tool": "x", "ok": True, "output": text}, 2000,
                                       cache_dir=self.cache, workspace=self.root)
        shaped = result["output"]
        self.assertLessEqual(len(shaped), 2000)
        self.assertTrue(shaped.startswith("row 0\n"))
        self.assertTrue(shaped.endswith("row 4999"))
        self.assertIn("lines (", shaped)
        saved = result["full_output"]
        self.assertTrue(saved.startswith(".crit/cache/out/") and saved.endswith(".txt"), saved)
        self.assertIn(saved, shaped)
        self.assertEqual((self.root / saved).read_text(encoding="utf-8"), text)

    def test_under_budget_is_untouched(self) -> None:
        result = tool_output.shape_output({"tool": "x", "ok": True, "output": "short"}, 2000, cache_dir=self.cache)
        self.assertEqual(result["output"], "short")
        self.assertNotIn("full_output", result)
        self.assertFalse((self.cache / "out").exists())

    def test_only_the_newest_files_are_kept(self) -> None:
        folder = self.cache / "out"
        folder.mkdir(parents=True)
        for index in range(tool_output.KEEP_FILES + 3):
            (folder / f"old-{index:04d}.txt").write_text("x", encoding="utf-8")
        tool_output.save_full("new", self.cache, self.root)
        self.assertEqual(len(list(folder.glob("*.txt"))), tool_output.KEEP_FILES)

    def test_empty_results_say_what_happened(self) -> None:
        self.assertEqual(tool_output.shape_output({"tool": "todo", "ok": True, "output": ""}, 2000)["output"],
                         "(no output; todo succeeded)")
        failed = tool_output.shape_output({"tool": "x", "ok": False, "error": ""}, 2000)
        self.assertIn("failed", failed["error"])

    def test_budget_from_settings_and_context(self) -> None:
        self.assertEqual(tool_output.budget_chars(16_000, None), 16_000)
        self.assertEqual(tool_output.budget_chars(None, None), tool_output.DEFAULT_CAP_TOKENS * 4)
        self.assertEqual(tool_output.budget_chars(16_000, {"tool_output_cap_tokens": 2000}), 8000)
        self.assertEqual(tool_output.budget_chars(16_000, {"tool_output_cap_tokens": 1}), tool_output.MIN_BUDGET_CHARS)

    def test_every_builtin_result_respects_the_budget(self) -> None:
        big = self.root / "big.txt"
        big.write_text("\n".join("x" * 300 for _ in range(3000)), encoding="utf-8")
        for cap in (500, 2000):
            ctx = ToolContext(workspace=self.root, cache_dir=self.cache, max_chars=100_000,
                              settings={"tool_output_cap_tokens": cap})
            for name, args in (
                ("read_files", {"path": "big.txt", "limit": 400}),
                ("search_code", {"pattern": "x+", "head_limit": 200, "path": "big.txt"}),
                ("run_command", {"command": f'"{sys.executable}" -c "print(\'y\' * 200000)"'}),
            ):
                with self.subTest(cap=cap, tool=name):
                    result = execute(name, args, ctx)
                    self.assertLessEqual(len(result.get("output") or ""), cap * 4 + 50, result.get("error"))


class ReadWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_long_file_gets_a_100_line_window(self) -> None:
        (self.root / "long.txt").write_text("".join(f"line {i}\n" for i in range(1, 2432)), encoding="utf-8")
        out = execute("read_files", {"path": "long.txt"}, ToolContext(workspace=self.root, max_chars=100_000))["output"]
        self.assertIn("(lines 1-100 of 2,431)", out)
        self.assertIn("100|line 100", out)
        self.assertNotIn("101|line 101", out)
        self.assertIn("next offset=101", out)

    def test_file_up_to_300_lines_is_read_whole(self) -> None:
        (self.root / "mid.txt").write_text("".join(f"line {i}\n" for i in range(1, 301)), encoding="utf-8")
        out = execute("read_files", {"path": "mid.txt"}, ToolContext(workspace=self.root, max_chars=100_000))["output"]
        self.assertIn("(lines 1-300 of 300)", out)
        self.assertIn("300|line 300", out)

    def test_detailed_adds_eol_and_version(self) -> None:
        (self.root / "a.txt").write_bytes(b"a\r\nb\r\n")
        ctx = ToolContext(workspace=self.root)
        concise = execute("read_files", {"path": "a.txt"}, ctx)["output"]
        self.assertIn("(lines 1-2 of 2, crlf)", concise)
        self.assertNotIn("version", concise)
        detailed = execute("read_files", {"path": "a.txt", "response_format": "detailed"}, ctx)["output"]
        self.assertIn("eol crlf, version", detailed)
        bad = execute("read_files", {"path": "a.txt", "response_format": "verbose"}, ctx)
        self.assertFalse(bad["ok"])


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "a.py").write_text("alpha = 1\nalpha = 2\nalpha = 3\nbeta = 1\n", encoding="utf-8")
        (self.root / "b.py").write_text("alpha = 9\n", encoding="utf-8")
        self.ctx = ToolContext(workspace=self.root, state=TaskState(task="t"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_files_and_counts_come_first(self) -> None:
        out = execute("search_code", {"pattern": "alpha"}, self.ctx)["output"]
        lines = out.split("\n")
        self.assertEqual(lines[0], "4 matches in 2 files:")
        self.assertEqual(lines[1], "  a.py (3)")
        self.assertEqual(lines[2], "  b.py (1)")
        self.assertIn("a.py:1: alpha = 1", out)

    def test_no_matches_says_how_many_files_were_searched(self) -> None:
        result = execute("search_code", {"pattern": "gamma"}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertRegex(result["output"], r"^no matches for 'gamma' in \d+ files")

    def test_several_patterns_in_one_call(self) -> None:
        result = execute("search_code", {"patterns": ["alpha", "beta", "gamma"]}, self.ctx)
        out = result["output"]
        self.assertTrue(out.startswith("3 patterns searched:\n"))
        self.assertIn("'beta': 1 match in 1 file\n", out)
        self.assertIn("'alpha': 4 matches in 2 files", out)
        self.assertIn("'gamma': no matches", out)
        self.assertIn("== 'beta' ==\n1 match in 1 file\na.py:4: beta = 1", out)

    def test_patterns_as_json_text(self) -> None:
        out = execute("search_code", {"patterns": '["alpha", "beta"]'}, self.ctx)["output"]
        self.assertIn("2 patterns searched", out)

    def test_detailed_shows_more_hits_per_file(self) -> None:
        (self.root / "c.py").write_text("".join(f"delta{i} = {i}\n" for i in range(12)), encoding="utf-8")
        concise = execute("search_code", {"pattern": "delta"}, self.ctx)["output"]
        detailed = execute("search_code", {"pattern": "delta", "response_format": "detailed"}, self.ctx)["output"]
        self.assertNotIn("c.py:10:", concise)
        self.assertIn("c.py:10:", detailed)


class EmptyOutputTests(unittest.TestCase):
    def test_a_silent_command_says_its_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = execute("run_command", {"command": f'"{sys.executable}" -c "pass"'}, ToolContext(workspace=Path(tmp)))
        self.assertIn("(no output; exit 0)", result["output"])


if __name__ == "__main__":
    unittest.main()
