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
import argparse


class AgentCliTests(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "url": "https://chatgpt.com/",
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
            with patch("critique_bot.bot_home.rebuild_index") as rebuild:
                from critique_bot.code_index import IndexStats

                rebuild.return_value = IndexStats(files=0, symbols=0)
                code = main(["--mode", "agent", "--repo-dir", str(root), "init"])
                self.assertEqual(code, 0)
                self.assertTrue((root / ".bot" / "settings.json").is_file())
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
                        "url": "https://chatgpt.com/",
                        "selectors": {
                            "prompt_input": "textarea",
                            "assistant_messages": ".markdown",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch("critique_bot.bot_home.rebuild_index") as rebuild:
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
            self.assertTrue((Path(tmp) / ".bot" / "settings.json").is_file())

    def test_first_run_inits_then_asks_before_opening_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            with patch("critique_bot.bot_home.rebuild_index") as rebuild:
                from critique_bot.code_index import IndexStats

                rebuild.return_value = IndexStats(files=0, symbols=0)
                with patch("critique_bot.welcome.stdio_is_tty", return_value=True):
                    with patch("critique_bot.welcome.pick_theme", return_value=("dark", True)), patch(
                        "critique_bot.welcome.pick_role", return_value="aosp-expert"
                    ):
                        with patch("critique_bot.welcome.ask_login", return_value=True):
                            with patch("critique_bot.browser.needs_visible_login", return_value=True):
                                with patch("critique_bot.provider.login_in_edge") as login:
                                    with patch("critique_bot.agent.run_agent", return_value=0) as run:
                                        code = agent_main(["--repo-dir", str(root), "--config", str(config)])
            self.assertEqual(code, 0)
            settings = json.loads((root / ".bot" / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["theme"], "dark")
            self.assertTrue(settings["syntax_preview"])
            self.assertTrue(settings["welcomed"])
            self.assertEqual(settings["role"], "aosp-expert")
            login.assert_called_once()
            run.assert_called_once()
            self.assertEqual(run.call_args.args[2], "")
            self.assertIsNone(run.call_args.kwargs["role"])

    def test_declining_login_does_not_open_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            with patch("critique_bot.bot_home.rebuild_index") as rebuild:
                from critique_bot.code_index import IndexStats

                rebuild.return_value = IndexStats(files=0, symbols=0)
                with patch("critique_bot.welcome.stdio_is_tty", return_value=True):
                    with patch("critique_bot.welcome.pick_theme", return_value=("light", False)), patch(
                        "critique_bot.welcome.pick_role", return_value=None
                    ):
                        with patch("critique_bot.welcome.ask_login", return_value=False):
                            with patch("critique_bot.provider.login_in_edge") as login:
                                with patch("critique_bot.agent.run_agent", return_value=0) as run:
                                    code = agent_main(["--repo-dir", str(root)])
            self.assertEqual(code, 0)
            settings = json.loads((root / ".bot" / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["theme"], "light")
            self.assertNotIn("welcomed", settings)
            self.assertNotIn("role", settings)  # Esc on the role screen: asked again next time
            self.assertEqual(settings["config"], "config.json")
            login.assert_not_called()
            run.assert_not_called()
            self.assertTrue(config.is_file())

    def test_role_is_asked_once_and_then_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            (root / ".bot").mkdir()
            (root / ".bot" / "settings.json").write_text(
                json.dumps({"welcomed": True, "theme": "dark", "config": "config.json"}), encoding="utf-8"
            )
            with patch("critique_bot.bot_home.rebuild_index"), patch(
                "critique_bot.welcome.stdio_is_tty", return_value=True
            ), patch("critique_bot.welcome.pick_role", return_value="test-engineer") as pick, patch(
                "critique_bot.agent.run_agent", return_value=0
            ) as run:
                self.assertEqual(agent_main(["--repo-dir", str(root), "add tests"]), 0)
                pick.assert_called_once()
                settings = json.loads((root / ".bot" / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(settings["role"], "test-engineer")
                self.assertIsNone(run.call_args.kwargs["role"])
                # The second run in the same folder does not ask again.
                self.assertEqual(agent_main(["--repo-dir", str(root), "add more tests"]), 0)
                pick.assert_called_once()
            self.assertTrue(config.is_file())

    def test_role_flag_is_normalized_stored_once_and_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._config(root)
            with patch("critique_bot.bot_home.rebuild_index"), patch(
                "critique_bot.welcome.stdio_is_tty", return_value=False
            ), patch("critique_bot.welcome.pick_role") as pick, patch(
                "critique_bot.agent.run_agent", return_value=0
            ) as run:
                self.assertEqual(agent_main(["--repo-dir", str(root), "--role", "AOSPExpert", "fix the HAL"]), 0)
                settings = json.loads((root / ".bot" / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(settings["role"], "aosp-expert")
                self.assertEqual(run.call_args.kwargs["role"], "aosp-expert")
                # A later --role applies to that run only; the stored role stays.
                self.assertEqual(agent_main(["--repo-dir", str(root), "--role", "code-reviewer", "review it"]), 0)
                settings = json.loads((root / ".bot" / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(settings["role"], "aosp-expert")
                self.assertEqual(run.call_args.kwargs["role"], "code-reviewer")
                pick.assert_not_called()

    def test_unknown_role_flag_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._config(root)
            with patch("critique_bot.bot_home.rebuild_index"), patch(
                "critique_bot.welcome.stdio_is_tty", return_value=False
            ), patch("critique_bot.agent.run_agent", return_value=0) as run:
                self.assertEqual(agent_main(["--repo-dir", str(root), "--role", "wizard", "fix it"]), 1)
                run.assert_not_called()

    def test_piped_run_without_a_role_does_not_ask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._config(root)
            with patch("critique_bot.bot_home.rebuild_index"), patch(
                "critique_bot.welcome.stdio_is_tty", return_value=False
            ), patch("critique_bot.welcome.pick_role") as pick, patch(
                "critique_bot.agent.run_agent", return_value=0
            ):
                self.assertEqual(agent_main(["--repo-dir", str(root), "fix it"]), 0)
                pick.assert_not_called()
                settings = json.loads((root / ".bot" / "settings.json").read_text(encoding="utf-8"))
                self.assertNotIn("role", settings)

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
