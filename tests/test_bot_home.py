from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from critique_bot.bot_home import find_bot_home, init_bot_home


class BotHomeTests(unittest.TestCase):
    def test_init_creates_tree_and_stores_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text("{}", encoding="utf-8")
            (root / ".gitignore").write_text("venv/\n", encoding="utf-8")
            home = init_bot_home(root)
            self.assertTrue((root / ".bot" / "settings.json").is_file())
            self.assertTrue((root / ".bot" / "cache").is_dir())
            self.assertTrue((root / ".bot" / "sessions").is_dir())
            self.assertIn("cache/", (root / ".bot" / ".gitignore").read_text(encoding="utf-8"))
            settings = json.loads(home.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(settings["config"], "config.json")
            self.assertNotIn("max_rounds", settings)
            ignore = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".bot/cache/", ignore)
            self.assertIn(".bot/sessions/", ignore)
            self.assertTrue(home.index_path.is_file())
            self.assertTrue(home.notes_path.is_file())
            self.assertEqual(home.project_notes(), "")
            with home.notes_path.open("a", encoding="utf-8") as notes:
                notes.write("\nRun tests with: gradlew.bat test\n")
            self.assertEqual(home.project_notes(), "Run tests with: gradlew.bat test")
            init_bot_home(root)
            self.assertIn("gradlew.bat", home.project_notes())

    def test_second_init_keeps_settings_and_gitignore_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text("venv/\n", encoding="utf-8")
            init_bot_home(root)
            settings_path = root / ".bot" / "settings.json"
            settings_path.write_text(
                json.dumps({"max_rounds": 7}) + "\n",
                encoding="utf-8",
            )
            init_bot_home(root)
            kept = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(kept["max_rounds"], 7)
            ignore = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertEqual(ignore.count(".bot/cache/"), 1)

    def test_find_walks_upward(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_bot_home(root)
            nested = root / "src" / "app"
            nested.mkdir(parents=True)
            found = find_bot_home(nested)
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found.root, root.resolve())
            self.assertIsNone(find_bot_home(Path(tempfile.mkdtemp())))
