from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from critique_bot.agent import execute_tool
from critique_bot.code_index import rebuild_index, search_symbols


class CodeIndexTests(unittest.TestCase):
    def test_python_and_java_symbols_skip_vendor_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "calc.py").write_text(
                "def alpha():\n    return 1\n",
                encoding="utf-8",
            )
            (root / "src" / "Greeter.java").write_text(
                "public class Greeter {\n    public void hello() {}\n}\n",
                encoding="utf-8",
            )
            vendor = root / "node_modules" / "pkg"
            vendor.mkdir(parents=True)
            (vendor / "skip.py").write_text("def hidden():\n    return 0\n", encoding="utf-8")
            prebuilts = root / "prebuilts" / "sdk"
            prebuilts.mkdir(parents=True)
            (prebuilts / "Stub.java").write_text(
                "public class Stub { public void nope() {} }\n",
                encoding="utf-8",
            )
            git = root / ".git"
            git.mkdir()
            (git / "secret.py").write_text("def secret():\n    return 0\n", encoding="utf-8")
            index = root / "index.sqlite"
            stats = rebuild_index(root, index)
            names = {hit[3] for hit in search_symbols(index, ".", limit=50)}
            self.assertIn("alpha", names)
            self.assertIn("Greeter", names)
            self.assertIn("hello", names)
            self.assertNotIn("hidden", names)
            self.assertNotIn("secret", names)
            self.assertNotIn("Stub", names)
            self.assertNotIn("nope", names)
            self.assertGreaterEqual(stats.files, 2)
            self.assertGreaterEqual(stats.symbols, 3)

    def test_edit_refreshes_symbol_and_search_orders_symbol_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "calc.py"
            source.write_text(
                "def alpha():\n    return 1\n# alpha mentioned later\n",
                encoding="utf-8",
            )
            index = root / "index.sqlite"
            rebuild_index(root, index)
            result = execute_tool(
                "edit_file",
                {
                    "path": "calc.py",
                    "old_string": "def alpha():",
                    "new_string": "def beta():",
                },
                workspace=root,
                index_path=index,
            )
            self.assertTrue(result["ok"], result)
            names = [hit[3] for hit in search_symbols(index, "beta|alpha", limit=10)]
            self.assertIn("beta", names)
            self.assertNotIn("alpha", names)
            found = execute_tool(
                "search_code",
                {"pattern": "alpha"},
                workspace=root,
                index_path=index,
            )
            self.assertTrue(found["ok"], found)
            lines = found["output"].splitlines()
            self.assertTrue(lines)
            self.assertNotIn("function alpha", lines[0])
            edited = execute_tool(
                "search_code",
                {"pattern": "beta"},
                workspace=root,
                index_path=index,
            )
            self.assertTrue(edited["output"].splitlines()[0].startswith("calc.py:1: function beta"))
