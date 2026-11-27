# SPDX-License-Identifier: Apache-2.0
"""Stage 8b: ``crit memory`` (status, show, review, export/import, prune) and ``--no-memory``."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

from critique_bot import memory_cmd
from critique_bot.memory import recall
from critique_bot.memory.store import MemoryStore, db_path
from test_memory import _Tmp, make_project


class CommandTests(_Tmp):
    def run_cmd(self, *argv: str, answers: list[str] | None = None) -> tuple[int, str]:
        out = io.StringIO()
        pending = list(answers or [])
        code = memory_cmd.main(["--repo-dir", str(self.root), *argv], out=out, ask=lambda _q: pending.pop(0) if pending else "q")
        return code, out.getvalue()

    def test_status_show_forget_review_export_import_prune(self) -> None:
        (self.root / ".crit" / "settings.json").write_text("{}", encoding="utf-8")
        code, text = self.run_cmd("status")
        self.assertIn("No project memory yet", text)
        store = self.store()
        episode = store.add_episode({"task": "fix fan", "session": "s1", "files_changed": ["helper.py"]})
        tainted = store.add_episode({"task": "tainted fix", "session": "s2", "trusted": False})
        note = store.add_convention("Fan tests run with `python -m pytest test_panel.py`.", ["exit 0"], provenance="session:s1")
        other = store.add_convention("Never edit `generated/` files in this repo.", ["x"], provenance="session:s1")
        code, text = self.run_cmd("status")
        self.assertIn("episodes", text)
        code, text = self.run_cmd("show", episode.ref)
        self.assertIn("fix fan", text)
        code, text = self.run_cmd("review", answers=["a", "r"])
        self.assertIn(f"Approved {note.ref}", text)
        self.assertIn(f"Rejected {other.ref}", text)
        export = self.root / "mem.json"
        code, text = self.run_cmd("export", "-o", str(export))
        data = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual([e["task"] for e in data["episodes"]], ["fix fan"])  # the tainted one stays out
        self.assertEqual(len(data["conventions"]), 1)
        code, text = self.run_cmd("forget", tainted.ref)
        self.assertIn("Forgot", text)
        other_root = self.root / "other"
        make_project(other_root)
        (other_root / ".crit" / "settings.json").write_text("{}", encoding="utf-8")
        code = memory_cmd.main(["--repo-dir", str(other_root), "import", str(export)], out=io.StringIO())
        self.assertEqual(code, 0)
        imported = MemoryStore(db_path(other_root))
        self.stores.append(imported)
        self.assertFalse(imported.episodes()[0].trusted)
        self.assertTrue(imported.episodes()[0].provenance.startswith("import:mem.json"))
        self.assertEqual(imported.conventions()[0].status, "proposed")  # approval is per repo
        code, text = self.run_cmd("prune", "--dry-run")
        self.assertIn("Would remove", text)
        code, text = self.run_cmd("prune")
        self.assertIn("Removed 1 item", text)  # the rejected note

    def test_no_convention_enters_memory_without_approval(self) -> None:
        store = self.store()
        store.add_convention("Builds use `m CarService` after lunch.", ["x"], provenance="session:s")
        self.assertEqual(store.conventions("approved"), [])
        found = recall.gather(store, self.root, None, recall.parse("Builds use m CarService"))
        self.assertEqual(found.conventions, [])
        code, _ = self.run_cmd("review", "--approve", "n1")
        self.assertEqual(code, 0)
        found = recall.gather(store, self.root, None, recall.parse("Builds use m CarService"))
        self.assertEqual(len(found.conventions), 1)

    def test_crit_memory_is_a_subcommand_and_no_memory_a_flag(self) -> None:
        from critique_bot import cli

        self.assertIn("memory", cli.SUBCOMMANDS)
        home = SimpleNamespace(settings={})
        cli._apply_tool_flags(SimpleNamespace(tool_format=None, tool_set=None, no_memory=True), home)
        self.assertEqual(home.settings["memory"], "off")


if __name__ == "__main__":
    import unittest

    unittest.main()
