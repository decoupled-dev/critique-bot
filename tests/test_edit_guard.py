"""The guard after every write or edit: new syntax errors flagged or rejected, only new lint issues."""

from __future__ import annotations

import shlex
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from critique_bot import edit_guard
from critique_bot.agent_tools import ToolContext, execute

KT = "class A {\n    fun f() {\n        run()\n    }\n}\n"

# A linter: one issue per line holding BAD, in the usual path:line:col: message shape.
LINTER = textwrap.dedent(
    """
    import sys, time
    path = sys.argv[1]
    text = open(path, encoding="utf-8").read()
    if "SLOW" in text:
        time.sleep(5)
    for number, line in enumerate(text.split("\\n"), 1):
        if "BAD" in line:
            print(f"{path}:{number}:1: X100 bad word {line.strip().split()[-1]}")
    """
)


class ParseTests(unittest.TestCase):
    def test_brace_language_new_error_only(self) -> None:
        _parse, problem = edit_guard.new_syntax_error("A.kt", KT, KT.replace("    }\n}", "}"))
        self.assertIn("braces unbalanced", problem)
        broken = KT[:-2]
        _parse, again = edit_guard.new_syntax_error("A.kt", broken, broken + "\n// more\n")
        self.assertEqual(again, "")  # it was already broken: nothing new

    def test_new_file(self) -> None:
        _parse, problem = edit_guard.new_syntax_error("n.json", None, "{")
        self.assertTrue(problem)

    @unittest.skipUnless(shutil.which("bash"), "bash")
    def test_shell_scripts_use_bash_n(self) -> None:
        parse = edit_guard.parse_check("run.sh", "if true; then\n  echo hi\n")
        self.assertTrue(parse.broken)
        self.assertEqual(parse.via, "bash -n")
        self.assertFalse(edit_guard.parse_check("run.sh", "echo hi\n").broken)

    def test_toml(self) -> None:
        if sys.version_info < (3, 11):
            self.skipTest("tomllib")
        self.assertTrue(edit_guard.parse_check("p.toml", "[a\nb = 1\n").broken)
        self.assertEqual(edit_guard.parse_check("p.toml", "[a]\nb = 1\n").status, "ok")

    def test_tree_sitter_counts_error_nodes_when_installed(self) -> None:
        def node(kind, line=0, children=(), has_error=False, missing=False):
            return SimpleNamespace(type=kind, start_point=(line, 0), children=list(children), has_error=has_error,
                                   is_missing=missing)

        class FakeParser:
            def parse(self, data: bytes):
                errors = data.count(b"@@")
                kids = [node("ERROR", line=i) for i in range(errors)]
                return SimpleNamespace(root_node=node("module", children=kids, has_error=bool(errors)))

        with mock.patch.dict(edit_guard._parsers, {".rs": FakeParser()}):
            _parse, problem = edit_guard.new_syntax_error("m.rs", "fn a() {} @@", "fn a() {} @@ @@")
            self.assertIn("syntax error", problem)
            _parse, same = edit_guard.new_syntax_error("m.rs", "fn a() {} @@", "fn b() {} @@")
            self.assertEqual(same, "")
            self.assertEqual(edit_guard.parse_check("m.rs", "fn a() {}").via, "tree-sitter")


class GuardModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "A.kt").write_text(KT, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _edit(self, settings: dict | None) -> dict:
        return execute("edit_file", {"path": "A.kt", "old_string": "        run()\n    }\n", "new_string": "        run()\n"},
                       ToolContext(workspace=self.root, settings=settings))

    def test_flag_keeps_the_edit_and_says_what_broke(self) -> None:
        result = self._edit(None)
        self.assertTrue(result["ok"], result)
        self.assertIn("syntax: the change introduced an error in A.kt", result["output"])
        self.assertNotEqual((self.root / "A.kt").read_text(encoding="utf-8"), KT)

    def test_reject_refuses_it(self) -> None:
        result = self._edit({"edit_guard": "reject"})
        self.assertFalse(result["ok"])
        self.assertIn("edit_guard is reject", result["error"])
        self.assertEqual((self.root / "A.kt").read_text(encoding="utf-8"), KT)

    def test_off_says_nothing(self) -> None:
        result = self._edit({"edit_guard": "off"})
        self.assertTrue(result["ok"])
        self.assertNotIn("syntax: the change", result["output"])

    def test_python_that_parsed_is_always_refused(self) -> None:
        (self.root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        for settings in (None, {"edit_guard": "off"}):
            result = execute("edit_file", {"path": "a.py", "old_string": "return 1", "new_string": "return (1"},
                             ToolContext(workspace=self.root, settings=settings))
            self.assertFalse(result["ok"])
            self.assertIn("syntax", result["error"])

    def test_reject_applies_to_new_files_too(self) -> None:
        result = execute("write_files", {"path": "B.kt", "contents": "class B {\n"},
                         ToolContext(workspace=self.root, settings={"edit_guard": "reject"}))
        self.assertFalse(result["ok"])
        self.assertFalse((self.root / "B.kt").exists())


class LintTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        script = self.root / "lint.py"
        script.write_text(LINTER, encoding="utf-8")
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} {{file}}"
        self.settings = {"lint": {".txt": command}, "lint_timeout_s": 3}
        (self.root / "n.txt").write_text("ok\nold BAD one\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _edit(self, old: str, new: str, settings: dict | None = None) -> dict:
        ctx = ToolContext(workspace=self.root, settings=settings if settings is not None else self.settings)
        return execute("edit_file", {"path": "n.txt", "old_string": old, "new_string": new}, ctx)

    def test_only_new_issues_are_reported(self) -> None:
        result = self._edit("ok\n", "ok\nnew BAD two\n")
        self.assertTrue(result["ok"], result)
        self.assertIn("lint: 1 new issue", result["output"])
        self.assertIn("n.txt:2 X100 bad word two", result["output"])
        self.assertNotIn("bad word one", result["output"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["lint.py", "n.txt"])  # temp copy removed

    def test_an_issue_that_already_existed_is_not_reported(self) -> None:
        result = self._edit("ok\n", "fine\n")
        self.assertTrue(result["ok"], result)
        self.assertNotIn("lint:", result["output"])

    def test_moved_existing_issue_is_not_new(self) -> None:
        result = self._edit("ok\n", "ok\n\n\n")
        self.assertNotIn("lint:", result["output"])

    def test_slow_linter_is_skipped_with_a_note(self) -> None:
        result = self._edit("ok\n", "SLOW\n", {**self.settings, "lint_timeout_s": 1})
        self.assertTrue(result["ok"], result)
        self.assertIn("lint: skipped", result["output"])

    def test_lint_false_turns_it_off(self) -> None:
        result = self._edit("ok\n", "ok\nnew BAD two\n", {"lint": False})
        self.assertNotIn("lint:", result["output"])

    def test_missing_linter_is_silent(self) -> None:
        result = self._edit("ok\n", "ok\nnew BAD two\n", {"lint": {".txt": "no-such-linter-xyz {file}"}})
        self.assertTrue(result["ok"])
        self.assertNotIn("lint:", result["output"])

    def test_new_file_reports_every_issue(self) -> None:
        result = execute("write_files", {"path": "m.txt", "contents": "a BAD x\n"},
                         ToolContext(workspace=self.root, settings=self.settings))
        self.assertIn("m.txt:1 X100 bad word x", result["output"])

    def test_lint_is_a_security_key(self) -> None:
        from critique_bot.project_home import SECURITY_KEYS

        self.assertIn("lint", SECURITY_KEYS)


if __name__ == "__main__":
    unittest.main()
