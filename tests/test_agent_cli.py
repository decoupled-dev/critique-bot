from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot.agent_cli import main as agent_main
from critique_bot.cli import main
from critique_bot.config import ConfigError
from critique_bot.cli import _resolve_mode
from webchat_support import needs_webchat
import argparse


def _own_user_folder(test: unittest.TestCase) -> Path:
    """A fresh user config folder for one test (preferences are per user)."""
    import os

    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    env = patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name, "APPDATA": tmp.name, "CRIT_HOME": tmp.name + "/.crit", "CRITIQUE_CONFIG_FILE": ""})
    env.start()
    test.addCleanup(env.stop)
    from critique_bot.config import user_config_path

    return user_config_path()


class AgentCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user_config = _own_user_folder(self)

    def _prefs(self) -> dict:
        return json.loads(self.user_config.read_text(encoding="utf-8")) if self.user_config.is_file() else {}

    def _config(self, root: Path) -> Path:
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "url": "https://chat.company.example/",
                    "selectors": {
                        "prompt_input": "textarea",
                        "assistant_messages": ".markdown",
                    },
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_init_from_mode_and_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("critique_bot.project_home.rebuild_index") as rebuild:
                from critique_bot.code_index import IndexStats

                rebuild.return_value = IndexStats(files=0, symbols=0)
                code = main(["--mode", "agent", "--repo-dir", str(root), "init"])
                self.assertEqual(code, 0)
                self.assertTrue((root / ".crit" / "settings.json").is_file())
                code = agent_main(["--repo-dir", str(root), "init"])
                self.assertEqual(code, 0)
                self.assertGreaterEqual(rebuild.call_count, 2)

    def test_task_dispatches_without_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "critique_bot.welcome.stdio_is_tty", return_value=False
        ):
            root = Path(tmp)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "url": "https://chat.company.example/",
                        "selectors": {
                            "prompt_input": "textarea",
                            "assistant_messages": ".markdown",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch("critique_bot.project_home.rebuild_index") as rebuild:
                from critique_bot.code_index import IndexStats

                rebuild.return_value = IndexStats(files=0, symbols=0)
                with patch("critique_bot.agent.run_agent", return_value=0) as run:
                    self.assertEqual(
                        main(["--config", str(config), "--mode", "agent", "--repo-dir", str(root), "init"]),
                        0,
                    )
                run.assert_not_called()
                with patch("critique_bot.agent.run_agent", return_value=0) as run:
                    self.assertEqual(
                        main(["--mode", "agent", "--repo-dir", str(root)]),
                        0,
                    )
                self.assertEqual(run.call_args.args[2], "")
            with patch("critique_bot.agent.run_agent", return_value=0) as run:
                code = main(
                    [
                        "--mode",
                        "agent",
                        "--repo-dir",
                        str(root),
                        "update the test cases",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.args[2], "update the test cases")
            with patch("critique_bot.agent.run_agent", return_value=0) as run:
                code = agent_main(["--repo-dir", str(root), "update the test cases"])
            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.args[2], "update the test cases")

    def test_missing_bot_home_still_indexes_then_needs_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "critique_bot.welcome.stdio_is_tty", return_value=False
        ):
            code = main(["--mode", "agent", "--repo-dir", tmp, "fix it"])
            self.assertEqual(code, 1)
            self.assertTrue((Path(tmp) / ".crit" / "settings.json").is_file())

    @needs_webchat
    def test_first_run_inits_then_asks_before_opening_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            with patch("critique_bot.project_home.rebuild_index") as rebuild:
                from critique_bot.code_index import IndexStats

                rebuild.return_value = IndexStats(files=0, symbols=0)
                with patch("critique_bot.welcome.stdio_is_tty", return_value=True):
                    with patch("critique_bot.welcome.pick_theme", return_value=("dark", True)), patch(
                        "critique_bot.welcome.pick_role", return_value="aosp-expert"
                    ):
                        with patch("critique_bot.welcome.ask_login", return_value=True):
                            with patch("critique_bot.webchat.browser.needs_visible_login", return_value=True):
                                with patch("critique_bot.webchat.provider.login_in_edge") as login:
                                    with patch("critique_bot.agent.run_agent", return_value=0) as run:
                                        code = agent_main(["--repo-dir", str(root), "--config", str(config)])
            self.assertEqual(code, 0)
            settings = json.loads((root / ".crit" / "settings.json").read_text(encoding="utf-8"))
            prefs = self._prefs()  # the style and the welcome are per user
            self.assertEqual(prefs["theme"], "dark")
            self.assertTrue(prefs["syntax_preview"])
            self.assertTrue(prefs["welcomed"])
            self.assertNotIn("theme", settings)
            self.assertEqual(settings["role"], "generic")  # picked from the (empty) project, not asked
            login.assert_called_once()
            run.assert_called_once()
            self.assertEqual(run.call_args.args[2], "")
            self.assertIsNone(run.call_args.kwargs["role"])

    @needs_webchat
    def test_declining_login_does_not_open_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            with patch("critique_bot.project_home.rebuild_index") as rebuild:
                from critique_bot.code_index import IndexStats

                rebuild.return_value = IndexStats(files=0, symbols=0)
                with patch("critique_bot.welcome.stdio_is_tty", return_value=True):
                    with patch("critique_bot.welcome.pick_theme", return_value=("light", False)), patch(
                        "critique_bot.welcome.pick_role", return_value=None
                    ):
                        with patch("critique_bot.welcome.ask_login", return_value=False):
                            with patch("critique_bot.webchat.provider.login_in_edge") as login:
                                with patch("critique_bot.agent.run_agent", return_value=0) as run:
                                    code = agent_main(["--repo-dir", str(root)])
            self.assertEqual(code, 0)
            settings = json.loads((root / ".crit" / "settings.json").read_text(encoding="utf-8"))
            prefs = self._prefs()
            self.assertEqual(prefs["theme"], "light")
            self.assertNotIn("welcomed", prefs)
            self.assertEqual(settings["role"], "generic")
            self.assertEqual(settings["config"], "config.json")
            login.assert_not_called()
            run.assert_not_called()
            self.assertTrue(config.is_file())

    def test_role_is_picked_from_the_files_once(self) -> None:
        import io
        from contextlib import redirect_stderr

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            (root / "app" / "src" / "main").mkdir(parents=True)
            (root / "app" / "src" / "main" / "AndroidManifest.xml").write_text("<manifest/>", encoding="utf-8")
            err = io.StringIO()
            with patch("critique_bot.project_home.rebuild_index"), patch(
                "critique_bot.welcome.stdio_is_tty", return_value=False
            ), patch("critique_bot.welcome.pick_role", side_effect=AssertionError("asked")), patch(
                "critique_bot.agent.run_agent", return_value=0
            ) as run, redirect_stderr(err):
                self.assertEqual(agent_main(["--repo-dir", str(root), "add tests"]), 0)
                settings = json.loads((root / ".crit" / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(settings["role"], "android-app-engineer")
                self.assertIsNone(run.call_args.kwargs["role"])
                # Bookkeeping, not a question: the log file has it, the screen does not.
                self.assertNotIn("Role for this project", err.getvalue())
                self.assertIn("Role for this project: Android App Engineer (change with: crit --role <name>)", __import__("critique_bot.log", fromlist=["x"]).log_file_path().read_text(encoding="utf-8"))
                # The second run in the same folder says nothing about the role.
                err.truncate(0)
                err.seek(0)
                self.assertEqual(agent_main(["--repo-dir", str(root), "add more tests"]), 0)
                self.assertNotIn("Role for this project", err.getvalue())
            self.assertTrue(config.is_file())

    def test_second_folder_asks_nothing(self) -> None:
        """Set up once per user: a new folder asks no question and opens no screen."""
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            team = Path(first) / "crit.json"
            team.write_text(json.dumps({"url": "https://ai.corp.example/", "selectors": {"prompt_input": "textarea", "assistant_messages": ".m"}}), encoding="utf-8")
            self.user_config.parent.mkdir(parents=True, exist_ok=True)
            self.user_config.write_text(json.dumps({"config_file": str(team), "theme": "dark", "welcomed": True}), encoding="utf-8")
            with patch("critique_bot.project_home.rebuild_index"), patch(
                "critique_bot.welcome.stdio_is_tty", return_value=True
            ), patch("critique_bot.cli._may_ask", return_value=True), patch(
                "builtins.input", side_effect=AssertionError("asked a question")
            ), patch("critique_bot.welcome.pick_theme", side_effect=AssertionError("theme screen")), patch(
                "critique_bot.welcome.pick_role", side_effect=AssertionError("role screen")
            ), patch("critique_bot.welcome.ask_login", side_effect=AssertionError("login question")), patch(
                "critique_bot.agent.run_agent", return_value=0
            ) as run:
                self.assertEqual(agent_main(["--repo-dir", second, "fix it"]), 0)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0].url, "https://ai.corp.example/")

    @needs_webchat
    def test_theme_is_asked_once_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            for root in (Path(one), Path(two)):
                self._config(root)
            with patch("critique_bot.project_home.rebuild_index"), patch(
                "critique_bot.welcome.stdio_is_tty", return_value=True
            ), patch("critique_bot.welcome.pick_theme", return_value=("light", True)) as pick, patch(
                "critique_bot.welcome.ask_login", return_value=True
            ), patch("critique_bot.webchat.browser.needs_visible_login", return_value=False), patch(
                "critique_bot.agent.run_agent", return_value=0
            ):
                self.assertEqual(agent_main(["--repo-dir", one, "fix it"]), 0)
                self.assertEqual(agent_main(["--repo-dir", two, "fix it"]), 0)
            pick.assert_called_once()
            self.assertEqual(self._prefs()["theme"], "light")
            self.assertTrue(self._prefs()["welcomed"])
            from critique_bot.config import effective_theme

            self.assertEqual(effective_theme({}), "light")
            self.assertEqual(effective_theme({"theme": "dark"}), "dark")  # a project's own choice stays

    def test_role_flag_is_normalized_stored_once_and_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._config(root)
            with patch("critique_bot.project_home.rebuild_index"), patch(
                "critique_bot.welcome.stdio_is_tty", return_value=False
            ), patch("critique_bot.welcome.pick_role") as pick, patch(
                "critique_bot.agent.run_agent", return_value=0
            ) as run:
                self.assertEqual(agent_main(["--repo-dir", str(root), "--role", "AOSPExpert", "fix the HAL"]), 0)
                settings = json.loads((root / ".crit" / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(settings["role"], "aosp-expert")
                self.assertEqual(run.call_args.kwargs["role"], "aosp-expert")
                # A later --role applies to that run only; the stored role stays.
                self.assertEqual(agent_main(["--repo-dir", str(root), "--role", "code-reviewer", "review it"]), 0)
                settings = json.loads((root / ".crit" / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(settings["role"], "aosp-expert")
                self.assertEqual(run.call_args.kwargs["role"], "code-reviewer")
                pick.assert_not_called()

    def test_unknown_role_flag_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._config(root)
            with patch("critique_bot.project_home.rebuild_index"), patch(
                "critique_bot.welcome.stdio_is_tty", return_value=False
            ), patch("critique_bot.agent.run_agent", return_value=0) as run:
                self.assertEqual(agent_main(["--repo-dir", str(root), "--role", "wizard", "fix it"]), 1)
                run.assert_not_called()

    def test_piped_run_without_a_role_does_not_ask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._config(root)
            with patch("critique_bot.project_home.rebuild_index"), patch(
                "critique_bot.welcome.stdio_is_tty", return_value=False
            ), patch("critique_bot.welcome.pick_role") as pick, patch(
                "critique_bot.agent.run_agent", return_value=0
            ):
                self.assertEqual(agent_main(["--repo-dir", str(root), "fix it"]), 0)
                pick.assert_not_called()
                settings = json.loads((root / ".crit" / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(settings["role"], "generic")

    def test_submit_rejects_agent(self) -> None:
        code = main(["submit", "--config", "config.json", "--mode", "agent"])
        self.assertEqual(code, 1)

    def test_prompt_template_rejected(self) -> None:
        args = argparse.Namespace(
            mode="agent",
            prompt=None,
            prompt_file=None,
            prompt_template="t.txt",
            paths=[],
        )
        with self.assertRaises(ConfigError):
            _resolve_mode(args)


class OneCommandTests(unittest.TestCase):
    """``crit MODE ...`` and ``critique-bot MODE ...`` route the same way as ``--mode``."""

    def test_crit_mode_words_route_to_run(self) -> None:
        cases = {
            "review": "review",
            "general": "general",
            "ask": "general",
            "chat": "chat",
            "agent": "agent",
        }
        for word, mode in cases.items():
            with patch("critique_bot.cli._main_run", return_value=0) as run:
                self.assertEqual(agent_main([word, "--prompt", "x"]), 0)
                self.assertEqual(run.call_args.args[0][:2], ["--mode", mode])
                self.assertEqual(run.call_args.args[0][2:], ["--crit-defaults", "--prompt", "x"])
            with patch("critique_bot.cli._main_run", return_value=0) as run:
                self.assertEqual(main([word]), 0)
                self.assertEqual(run.call_args.args[0], ["--mode", mode])

    def test_crit_subcommands_route(self) -> None:
        with patch("critique_bot.cli._main_queue_status", return_value=0) as status:
            self.assertEqual(agent_main(["queue-status", "--config", "c.json"]), 0)
            status.assert_called_once_with(["--config", "c.json"])

    def test_crit_task_is_still_agent(self) -> None:
        with patch("critique_bot.cli._main_run", return_value=0) as run:
            self.assertEqual(agent_main(["review the auth code", "-y"]), 0)
            self.assertEqual(
                run.call_args.args[0], ["--mode", "agent", "--crit-defaults", "review the auth code", "--yes"]
            )

    def test_mode_word_and_flag_conflict(self) -> None:
        self.assertEqual(agent_main(["chat", "--mode", "review"]), 1)
        self.assertEqual(main(["chat", "--mode=review"]), 1)

    def test_review_uses_stored_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".crit").mkdir()
            config = root / "my-config.json"
            config.write_text("{}", encoding="utf-8")
            (root / ".crit" / "settings.json").write_text(
                json.dumps({"config": str(config)}), encoding="utf-8"
            )
            with patch("critique_bot.cli.load_config", side_effect=ConfigError("stop")) as load:
                self.assertEqual(agent_main(["chat", "--repo-dir", str(root)]), 1)
            self.assertEqual(load.call_args.args[0], str(config))

    def test_review_without_any_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(agent_main(["chat", "--repo-dir", tmp]), 1)


class FrontEndTests(unittest.TestCase):
    def tearDown(self) -> None:
        from critique_bot import agent

        agent.use_ui(None)

    def test_terminal_ui_does_not_replace_an_installed_front_end(self) -> None:
        from critique_bot import agent, agent_acp, agent_ui
        from critique_bot.cli import _use_terminal_ui

        agent.use_ui(agent_acp)
        _use_terminal_ui()
        self.assertIs(agent.agent_ui, agent_acp)

    def test_terminal_ui_is_installed_when_none_is(self) -> None:
        from critique_bot import agent, agent_ui
        from critique_bot.cli import _use_terminal_ui

        agent.use_ui(None)
        _use_terminal_ui()
        self.assertIs(agent.agent_ui, agent_ui)


def tearDownModule() -> None:
    """load_config sets the process-wide egress policy; leave none behind for the next module."""
    from critique_bot import egress

    egress._policy = egress.Policy()
