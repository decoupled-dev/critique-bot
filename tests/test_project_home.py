# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from critique_bot.project_home import (
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
            self.assertTrue((root / ".crit" / "settings.json").is_file())
            self.assertTrue((root / ".crit" / "cache").is_dir())
            self.assertTrue((root / ".crit" / "sessions").is_dir())
            self.assertIn("cache/", (root / ".crit" / ".gitignore").read_text(encoding="utf-8"))
            settings = json.loads(home.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(settings["config"], "config.json")
            self.assertNotIn("max_rounds", settings)
            ignore = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".crit/cache/", ignore)
            self.assertIn(".crit/sessions/", ignore)
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
            settings_path = root / ".crit" / "settings.json"
            settings_path.write_text(
                json.dumps({"max_rounds": 7}) + "\n",
                encoding="utf-8",
            )
            init_bot_home(root)
            kept = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(kept["max_rounds"], 7)
            ignore = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertEqual(ignore.count(".crit/cache/"), 1)

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
            ignore = root / ".crit" / ".gitignore"
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
            for rel in (".crit/sessions", ".crit/cache", ".crit/cache/undo"):
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
            self.assertIn(b".crit/cache/\r\n", data)
            self.assertNotIn(b"\n.bot", data.replace(b"\r\n", b""))

    def test_update_settings_is_atomic_and_merges(self) -> None:
        from critique_bot.project_home import update_settings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = init_bot_home(root)
            update_settings(home, theme="light", permissions="auto")
            stored = json.loads(home.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["theme"], "light")
            self.assertEqual(stored["permissions"], "auto")
            leftovers = [p.name for p in home.bot_dir.iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])


class GitTracksTests(unittest.TestCase):
    def setUp(self) -> None:
        import shutil
        import subprocess

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git = shutil.which("git")
        self.subprocess = subprocess

    def _repo(self) -> Path:
        if self.git is None:
            self.skipTest("git is not installed")
        repo = self.root / "repo"
        (repo / "sub").mkdir(parents=True)
        self.subprocess.run([self.git, "init", "-q", str(repo)], check=True)
        (repo / "sub" / "config.json").write_text("{}", encoding="utf-8")
        (repo / "sub" / "mine.json").write_text("{}", encoding="utf-8")
        self.subprocess.run([self.git, "-C", str(repo), "add", "sub/config.json"], check=True)
        return repo

    def test_subfolder_of_a_repository_is_checked(self) -> None:
        from critique_bot.project_home import git_tracks

        repo = self._repo()
        workspace = repo / "sub"
        self.assertTrue(git_tracks(workspace, workspace / "config.json"))
        self.assertFalse(git_tracks(workspace, workspace / "mine.json"))

    def test_outside_any_repository_is_untracked(self) -> None:
        from unittest.mock import patch

        from critique_bot.project_home import git_tracks

        if self.git is None:
            self.skipTest("git is not installed")
        (self.root / "c.json").write_text("{}", encoding="utf-8")
        with patch.dict("os.environ", {"GIT_CEILING_DIRECTORIES": str(self.root.parent)}):
            self.assertFalse(git_tracks(self.root, self.root / "c.json"))

    def test_fails_closed_without_git_or_on_timeout(self) -> None:
        from unittest.mock import patch

        from critique_bot import project_home

        target = self.root / "c.json"
        with patch.object(project_home.shutil, "which", return_value=None):
            self.assertTrue(project_home.git_tracks(self.root, target))
        with patch.object(project_home.shutil, "which", return_value="/usr/bin/git"), patch.object(
            project_home.subprocess, "run", side_effect=self.subprocess.TimeoutExpired("git", 10)
        ):
            self.assertTrue(project_home.git_tracks(self.root, target))
        failed = self.subprocess.CompletedProcess([], 128, "", "fatal: detected dubious ownership")
        with patch.object(project_home.shutil, "which", return_value="/usr/bin/git"), patch.object(
            project_home.subprocess, "run", return_value=failed
        ):
            self.assertTrue(project_home.git_tracks(self.root, target))


class PrivateBotDirTests(unittest.TestCase):
    @unittest.skipIf(__import__("os").name == "nt", "POSIX permissions")
    def test_existing_folders_are_narrowed_when_found(self) -> None:
        import os

        from critique_bot.project_home import ensure_private_dir

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bot = root / ".crit"
            for folder in (bot, bot / "sessions", bot / "cache"):
                folder.mkdir(parents=True, exist_ok=True)
                os.chmod(folder, 0o755)
            (bot / "settings.json").write_text("{}", encoding="utf-8")
            find_bot_home(root)
            for folder in (bot, bot / "sessions", bot / "cache"):
                self.assertEqual(folder.stat().st_mode & 0o777, 0o700, folder)
            undo = ensure_private_dir(bot / "cache" / "undo" / "x")
            self.assertEqual(undo.stat().st_mode & 0o777, 0o700)

    def test_windows_grants_before_dropping_inheritance(self) -> None:
        import subprocess
        from unittest.mock import patch

        from critique_bot import output

        calls: list[list[str]] = []

        def fake_run(argv, **_kw):
            calls.append(list(argv))
            if "/user" in argv:
                return subprocess.CompletedProcess(argv, 0, '"corp\\me","S-1-5-21-1"\n', "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            output._WINDOWS_DONE.clear()
            with patch.object(output.shutil, "which", side_effect=lambda n: n), patch.object(
                output.subprocess, "run", side_effect=fake_run
            ):
                output._restrict_windows(Path(tmp))
        icacls = [c for c in calls if c[0] == "icacls"]
        self.assertEqual(icacls[0][2:], ["/grant:r", "*S-1-5-21-1:(OI)(CI)F"])
        self.assertEqual(icacls[1][2:], ["/inheritance:r"])

    def test_windows_failed_grant_keeps_inheritance(self) -> None:
        import subprocess
        from unittest.mock import patch

        from critique_bot import output

        calls: list[list[str]] = []

        def fake_run(argv, **_kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 5, "", "denied")

        with tempfile.TemporaryDirectory() as tmp:
            output._WINDOWS_DONE.clear()
            with patch.object(output.shutil, "which", side_effect=lambda n: n), patch.object(
                output.subprocess, "run", side_effect=fake_run
            ):
                output._restrict_windows(Path(tmp))
        self.assertFalse(any("/inheritance:r" in c for c in calls))

