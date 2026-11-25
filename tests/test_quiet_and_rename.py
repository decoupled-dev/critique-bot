"""Quiet sessions (the log file instead of the screen), the .bot -> .crit rename, and the
CRITIQUE_* -> CRIT_* environment names."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from critique_bot import config, env_names, log, project_home, redaction


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"CRIT_HOME": str(self.root / ".crit")})
        env.start()
        self.addCleanup(env.stop)

    def log_text(self) -> str:
        path = log.log_file_path()
        return path.read_text(encoding="utf-8") if path.exists() else ""


class LogFileTests(_Home):
    def test_lines_go_to_the_file_not_the_screen_and_are_redacted(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            log.info("Role for this project: Generic")
            log.warn("token ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8 leaked to alice@corp-mail.com")
        self.assertEqual(err.getvalue(), "")
        text = self.log_text()
        self.assertIn("[INFO ] Role for this project: Generic", text)
        self.assertNotIn("ghp_a1B2", text)
        self.assertNotIn("alice@corp-mail.com", text)
        self.assertIn("[REDACTED:", text)
        self.assertEqual(log.log_file_path(), self.root / ".crit" / "logs" / "crit.log")
        if os.name != "nt":
            self.assertEqual(log.log_file_path().stat().st_mode & 0o777, 0o600)
            self.assertEqual(log.log_file_path().parent.stat().st_mode & 0o777, 0o700)

    def test_custom_names_are_redacted_in_the_file(self) -> None:
        redaction.set_custom_terms(["Globex Secret"])
        self.addCleanup(redaction.set_custom_terms, ())
        log.info("working on Globex Secret today")
        self.assertNotIn("Globex Secret", self.log_text())

    def test_with_logs_it_also_prints(self) -> None:
        log.configure(enabled=True)
        self.addCleanup(log.configure, enabled=False)
        err = io.StringIO()
        with redirect_stderr(err):
            log.info("shown with --logs")
        self.assertIn("shown with --logs", err.getvalue())
        self.assertIn("shown with --logs", self.log_text())

    def test_rotation_keeps_three_files(self) -> None:
        with patch.object(log, "LOG_MAX_BYTES", 2000):
            for index in range(200):
                log.info(f"line {index:04d} " + "x" * 60)
        folder = log.log_file_path().parent
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["crit.log", "crit.log.1", "crit.log.2"])
        for path in folder.iterdir():
            self.assertLessEqual(path.stat().st_size, 2000)
        self.assertIn("line 0199", self.log_text())

    def test_an_unwritable_log_never_breaks_crit(self) -> None:
        blocker = self.root / ".crit"
        blocker.write_text("a file where the folder should be")
        log.info("nowhere to go")  # no exception

    def test_the_stop_line_points_at_the_log_file(self) -> None:
        from critique_bot import cli

        err = io.StringIO()
        with patch.object(cli, "_main", side_effect=RuntimeError("disk full")), redirect_stderr(err):
            self.assertEqual(cli.main(["review"]), 1)
        self.assertIn("crit stopped: disk full. Details: ", err.getvalue())
        self.assertIn("crit.log", err.getvalue())
        self.assertNotIn("--logs", err.getvalue())
        self.assertIn("disk full", self.log_text())


class ProjectFolderTests(_Home):
    def test_an_old_bot_folder_is_renamed_silently(self) -> None:
        project = self.root / "proj"
        (project / ".bot").mkdir(parents=True)
        (project / ".bot" / "settings.json").write_text(json.dumps({"role": "android"}), encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            home = project_home.find_bot_home(project)
        assert home is not None
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(home.bot_dir.resolve(), (project / ".crit").resolve())  # RUNNER~1 vs the long name
        self.assertFalse((project / ".bot").exists())
        self.assertEqual(home.settings["role"], "android")
        self.assertIn("renamed", self.log_text())

    def test_both_folders_use_crit_and_leave_bot(self) -> None:
        project = self.root / "proj"
        for name, role in ((".bot", "old"), (".crit", "new")):
            (project / name).mkdir(parents=True)
            (project / name / "settings.json").write_text(json.dumps({"role": role}), encoding="utf-8")
        home = project_home.find_bot_home(project)
        assert home is not None
        self.assertEqual(home.settings["role"], "new")
        self.assertTrue((project / ".bot" / "settings.json").exists())

    def test_the_home_folder_keeps_its_data_inside_crits_folder(self) -> None:
        home_dir = self.root / "home"
        home_dir.mkdir()
        with patch.dict(os.environ, {"HOME": str(home_dir), "USERPROFILE": str(home_dir), "CRIT_HOME": str(home_dir / ".crit")}), patch(
            "critique_bot.project_home.rebuild_index"
        ) as rebuild:
            rebuild.return_value = type("S", (), {"files": 0, "symbols": 0})()
            created = project_home.init_bot_home(home_dir)
            self.assertEqual(created.bot_dir, home_dir / ".crit" / "projects" / "home")
            self.assertFalse((home_dir / ".crit" / "settings.json").exists())
            found = project_home.find_bot_home(home_dir / "notes")
            assert found is not None
            self.assertEqual(found.bot_dir, home_dir / ".crit" / "projects" / "home")

    def test_gitignore_gets_crit_lines(self) -> None:
        project = self.root / "proj"
        project.mkdir()
        (project / ".gitignore").write_text("node_modules/\n")
        with patch("critique_bot.project_home.rebuild_index") as rebuild:
            rebuild.return_value = type("S", (), {"files": 0, "symbols": 0})()
            project_home.init_bot_home(project)
        text = (project / ".gitignore").read_text()
        self.assertIn(".crit/settings.json", text)
        self.assertNotIn(".bot/", text)

    def test_the_model_cannot_rewrite_settings_in_either_folder(self) -> None:
        from critique_bot import agent_tools

        project = self.root / "proj"
        (project / ".crit").mkdir(parents=True)
        (project / ".bot" / "skills").mkdir(parents=True)
        ctx = type("Ctx", (), {"workspace": project})()
        protected = agent_tools._protected_paths(ctx)  # type: ignore[arg-type]
        for path in (".crit/settings.json", ".crit/skills", ".bot/settings.json", ".bot/skills"):
            self.assertIn(project / path, protected)


class EnvironmentNameTests(unittest.TestCase):
    def setUp(self) -> None:
        env_names._reported.clear()

    def test_new_name_wins_and_old_name_still_works(self) -> None:
        with patch.dict(os.environ, {"CRITIQUE_CHAT_URL": "https://old.example/"}):
            os.environ.pop("CRIT_URL", None)
            self.assertEqual(env_names.get("CRIT_URL"), "https://old.example/")
            with patch.dict(os.environ, {"CRIT_URL": "https://new.example/"}):
                self.assertEqual(env_names.get("CRIT_URL"), "https://new.example/")
        self.assertIsNone(env_names.get("CRIT_URL"))

    def test_the_old_name_is_reported_in_the_log_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"CRIT_HOME": tmp, "CRITIQUE_CONFIG": "/x/config.json"}
        ):
            os.environ.pop("CRIT_CONFIG", None)
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(env_names.get(config.ENV_CONFIG), "/x/config.json")
            self.assertEqual(err.getvalue(), "")
            self.assertIn("CRITIQUE_CONFIG", log.log_file_path().read_text(encoding="utf-8"))

    def test_config_reads_the_new_names(self) -> None:
        self.assertEqual(config.ENV_CHAT_URL, "CRIT_URL")
        self.assertEqual(config.ENV_CONFIG, "CRIT_CONFIG")
        self.assertEqual(config.ENV_COMPANY_CONFIG, "CRIT_CONFIG_FILE")


if __name__ == "__main__":
    unittest.main()
