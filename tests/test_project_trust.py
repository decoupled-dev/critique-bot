# SPDX-License-Identifier: Apache-2.0
"""Project settings that loosen crit count only once the user approved them (SF-3, PM-9).

Temporary folders only (outside any git work tree), with HOME and CRIT_HOME redirected.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_00_isolation  # noqa: F401  (HOME/CRIT_HOME -> a temp folder before crit loads)

from critique_bot import project_home
from critique_bot.extensions import trust


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        (self.home / ".crit").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "CRIT_HOME": str(self.home / ".crit")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(project_home.ENV_EVAL_SETTINGS, None)
        self.root = self.base / "proj"
        (self.root / ".crit").mkdir(parents=True)
        self.settings = self.root / ".crit" / "settings.json"
        if project_home.git_tracks(self.root, self.root / "x"):
            self.skipTest("the temporary folder is inside a git work tree here")

    def write(self, values: dict) -> None:
        self.settings.write_text(json.dumps(values), encoding="utf-8")


class ApprovalTests(_Isolated):
    def test_approved_values_count_until_they_change(self) -> None:
        self.write({"permissions": "auto", "allow": ["git status*"], "theme": "light"})
        home = project_home.find_bot_home(self.root)
        self.assertEqual(home.settings, {"theme": "light"})
        self.assertEqual(sorted(home.unapproved), ["allow", "permissions"])
        home = project_home.approve_settings(home)
        self.assertEqual(home.settings["permissions"], "auto")
        self.assertEqual(home.unapproved, {})
        # Another key that does not loosen anything changes: no new question.
        self.write({"permissions": "auto", "allow": ["git status*"], "theme": "dark"})
        self.assertEqual(project_home.find_bot_home(self.root).settings["permissions"], "auto")
        # A loosening value changes: off again until approved.
        self.write({"permissions": "auto", "allow": ["*"], "theme": "dark"})
        home = project_home.find_bot_home(self.root)
        self.assertNotIn("allow", home.settings)
        self.assertNotIn("permissions", home.settings)

    def test_values_crit_writes_stay_approved(self) -> None:
        self.write({"theme": "light"})
        home = project_home.find_bot_home(self.root)
        project_home.update_settings(home, permissions="auto")
        project_home.add_allow_rule(project_home.find_bot_home(self.root), "git log*")
        home = project_home.find_bot_home(self.root)
        self.assertEqual(home.settings["permissions"], "auto")
        self.assertEqual(home.settings["allow"], ["git log*"])

    def test_a_crit_write_does_not_approve_values_someone_else_put_there(self) -> None:
        self.write({"allow": ["*"]})
        home = project_home.find_bot_home(self.root)
        project_home.update_settings(home, permissions="ask")
        home = project_home.find_bot_home(self.root)
        self.assertNotIn("allow", home.settings)
        self.assertNotIn("permissions", home.settings)

    def test_an_approved_config_is_bound_to_its_content(self) -> None:
        model = self.root / ".crit" / "model.json"
        model.write_text(json.dumps({"backend": "local", "url": "http://127.0.0.1:8080"}), encoding="utf-8")
        self.write({"config": ".crit/model.json"})
        home = project_home.approve_settings(project_home.find_bot_home(self.root))
        self.assertEqual(home.config_file(), model)
        model.write_text(json.dumps({"backend": "local", "url": "https://attacker.example/v1"}), encoding="utf-8")
        self.assertIsNone(project_home.find_bot_home(self.root).config_file())

    def test_a_project_config_json_needs_approval(self) -> None:
        from critique_bot.config_cmd import resolve_config_path

        self.settings.unlink(missing_ok=True)
        config = self.root / "config.json"
        config.write_text(json.dumps({"backend": "local", "url": "https://attacker.example/v1"}), encoding="utf-8")
        self.assertEqual(resolve_config_path(None, self.root).source, "user")
        project_home.approve_root_config(config)
        self.assertEqual(resolve_config_path(None, self.root).source, "project-root")
        config.write_text(json.dumps({"backend": "local", "url": "https://other.example/v1"}), encoding="utf-8")
        self.assertEqual(resolve_config_path(None, self.root).source, "user")

    def test_settings_in_crits_own_folder_need_no_approval(self) -> None:
        own = self.home / ".crit" / "projects" / "home"
        own.mkdir(parents=True)
        (own / "settings.json").write_text(json.dumps({"permissions": "auto"}), encoding="utf-8")
        home = project_home.find_bot_home(self.home)
        self.assertEqual(home.settings_path, own / "settings.json")
        self.assertEqual(home.settings["permissions"], "auto")

    def test_crit_trust_approves_a_settings_file(self) -> None:
        from critique_bot.extensions_cmd import main_trust

        self.write({"permissions": "auto"})
        with mock.patch("builtins.print"):
            self.assertEqual(main_trust([str(self.settings)]), 0)
        self.assertEqual(project_home.find_bot_home(self.root).settings["permissions"], "auto")
        self.assertIn(str(self.settings), trust.settings_approvals())
        with mock.patch("builtins.print"):
            main_trust(["--revoke", str(self.settings)])
        self.assertNotIn("permissions", project_home.find_bot_home(self.root).settings)


class WalkTests(_Isolated):
    def test_the_walk_stops_at_the_home_folder(self) -> None:
        above = self.base / ".crit"
        above.mkdir()
        (above / "settings.json").write_text(json.dumps({"theme": "x"}), encoding="utf-8")
        project = self.home / "work" / "repo"
        project.mkdir(parents=True)
        self.assertIsNone(project_home.find_bot_home(project))

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_a_world_writable_ancestor_is_skipped(self) -> None:
        shared = self.base / "shared"
        (shared / ".crit").mkdir(parents=True)
        (shared / ".crit" / "settings.json").write_text("{}", encoding="utf-8")
        os.chmod(shared / ".crit", 0o777)
        self.addCleanup(os.chmod, shared / ".crit", 0o700)
        (shared / "repo").mkdir()
        self.assertIsNone(project_home.find_bot_home(shared / "repo"))
        os.chmod(shared / ".crit", 0o700)
        self.assertEqual(project_home.find_bot_home(shared / "repo").root, shared)


class StartQuestionTests(_Isolated):
    def run_question(self, *, tty: bool, answer: bool) -> tuple:
        from critique_bot import cli

        args = argparse.Namespace(config=None, acp=False)
        home = project_home.find_bot_home(self.root)
        with mock.patch.object(cli, "_may_ask", return_value=tty), \
                mock.patch("critique_bot.config_cmd._confirm", return_value=answer) as confirm, \
                mock.patch.object(cli.log, "notice") as notice, mock.patch("builtins.print"):
            home = cli._approve_project_settings(home, args)
        return home, confirm, notice

    def test_without_a_terminal_one_line_says_how_to_approve(self) -> None:
        self.write({"permissions": "auto"})
        home, confirm, notice = self.run_question(tty=False, answer=True)
        confirm.assert_not_called()
        self.assertNotIn("permissions", home.settings)
        self.assertIn("crit trust", notice.call_args.args[0])

    def test_yes_approves_once(self) -> None:
        self.write({"permissions": "auto"})
        home, confirm, _ = self.run_question(tty=True, answer=True)
        self.assertEqual(home.settings["permissions"], "auto")
        _home, confirm, _ = self.run_question(tty=True, answer=True)
        confirm.assert_not_called()  # one approval per content

    def test_no_leaves_them_off(self) -> None:
        self.write({"permissions": "auto"})
        home, _confirm, _ = self.run_question(tty=True, answer=False)
        self.assertNotIn("permissions", home.settings)


if __name__ == "__main__":
    unittest.main()
