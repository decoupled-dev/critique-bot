from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from critique_bot.agent import execute_tool
from critique_bot.code_index import (
    candidate_files,
    find_symbols,
    outline,
    rebuild_index,
    refresh_index,
    repo_map,
    search_symbols,
    split_ident,
    symbol_at,
)


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

    def test_refresh_only_reparses_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("def one():\n    return 1\n", encoding="utf-8")
            (root / "b.py").write_text("def two():\n    return 2\n", encoding="utf-8")
            index = root / "index.sqlite"
            rebuild_index(root, index)
            self.assertEqual(refresh_index(root, index).changed, 0)
            (root / "b.py").write_text("def three():\n    return 3\n", encoding="utf-8")
            (root / "a.py").unlink()
            stats = refresh_index(root, index)
            self.assertEqual(stats.changed, 1)
            self.assertEqual(stats.removed, 1)
            names = {hit[3] for hit in search_symbols(index, ".", limit=50)}
            self.assertEqual(names, {"three"})

    def test_subtokens_spans_outline_and_symbol_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = ["class OrderService:", "    def place_order(self, item):", "        return item", ""]
            body += [f"# filler {i}" for i in range(450)]
            body += ["def helper():", "    return 0", ""]
            (root / "orders.py").write_text("\n".join(body), encoding="utf-8")
            index = root / "index.sqlite"
            rebuild_index(root, index)
            hits = find_symbols(index, "order service", limit=5)
            self.assertEqual(hits[0].name, "OrderService")
            method = symbol_at(index, "orders.py", "OrderService.place_order")
            self.assertIsNotNone(method)
            assert method is not None
            self.assertEqual((method.line, method.end_line, method.parent), (2, 3, "OrderService"))
            names = [s.name for s in outline(index, "orders.py")]
            self.assertEqual(names[:2], ["OrderService", "place_order"])
            read = execute_tool("read_files", {"path": "orders.py"}, workspace=root, index_path=index)
            self.assertIn("outline", read["output"])
            self.assertIn("helper", read["output"])
            one = execute_tool(
                "read_files", {"path": "orders.py", "symbol": "place_order"}, workspace=root, index_path=index
            )
            self.assertIn("2|    def place_order(self, item):", one["output"])
            self.assertNotIn("filler", one["output"])

    def test_full_text_prefilter_and_repo_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pay.py").write_text("def charge_card():\n    return 'stripe-token'\n", encoding="utf-8")
            (root / "cart.py").write_text(
                "from pay import charge_card\n\ndef checkout():\n    return charge_card()\n", encoding="utf-8"
            )
            (root / "misc.py").write_text("def unrelated():\n    return None\n", encoding="utf-8")
            index = root / "index.sqlite"
            rebuild_index(root, index)
            self.assertEqual(candidate_files(index, "stripe-token"), ["pay.py"])
            self.assertIsNone(candidate_files(index, "ab"))
            text = repo_map(index, keywords=("charge",), budget_chars=2000)
            self.assertIn("pay.py", text)
            self.assertIn("charge_card", text)
            self.assertLess(text.index("pay.py"), text.index("misc.py") if "misc.py" in text else len(text))
            self.assertEqual(split_ident("parseHTTPResponse_v2"), ["parse", "http", "response", "v2"])
