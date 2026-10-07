from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from critique_bot.bot_home import (
    check_command_from_notes,
    find_bot_home,
    init_bot_home,
    resolve_check_command,
)


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
            self.assertEqual(check_command_from_notes(home.project_notes()), "")

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

    def test_test_with_line_is_the_check_and_settings_win(self) -> None:
        notes = "Build with: ./gradlew assemble\nTest with: ./gradlew test\n"
        self.assertEqual(check_command_from_notes(notes), "./gradlew test")
        self.assertEqual(resolve_check_command({}, notes), "./gradlew test")
        self.assertEqual(
            resolve_check_command({"check_command": "pytest -q"}, notes),
            "pytest -q",
        )
        self.assertIsNone(resolve_check_command({}, "Build with: make"))


class BotHomeHardeningTests(unittest.TestCase):
    def test_local_gitignore_is_written_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_bot_home(root)
            ignore = root / ".bot" / ".gitignore"
            ignore.write_text("cache/\nsessions/\nmine/\n", encoding="utf-8")
            init_bot_home(root)
            self.assertIn("mine/", ignore.read_text(encoding="utf-8"))

    @unittest.skipIf(__import__("os").name == "nt", "POSIX permissions")
    def test_private_dirs_are_owner_only(self) -> None:
        import os
        import stat

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_bot_home(root)
            for rel in (".bot/sessions", ".bot/cache", ".bot/cache/undo"):
                mode = stat.S_IMODE(os.stat(root / rel).st_mode)
                self.assertEqual(mode, 0o700, rel)

    def test_append_gitignore_keeps_crlf_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = b"# caf\xc3\xa9\r\nnode_modules/\r\n"
            (root / ".gitignore").write_bytes(original)
            init_bot_home(root)
            data = (root / ".gitignore").read_bytes()
            self.assertTrue(data.startswith(original))
            self.assertIn(b".bot/cache/\r\n", data)
            self.assertNotIn(b"\n.bot", data.replace(b"\r\n", b""))

    def test_update_settings_is_atomic_and_merges(self) -> None:
        from critique_bot.bot_home import update_settings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = init_bot_home(root)
            update_settings(home, theme="light", permissions="auto")
            stored = json.loads(home.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["theme"], "light")
            self.assertEqual(stored["permissions"], "auto")
            leftovers = [p.name for p in home.bot_dir.iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])
