"""The program's entry point: always crit, and a double-click in Windows Explorer."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from critique_bot import __main__ as entry


class NameTests(unittest.TestCase):
    def test_the_program_is_crit_and_as_crit_is_ignored(self) -> None:
        for argv in (["crit", "review"], ["critique-bot", "--as-crit", "review"], [r"C:\tools\crit.exe", "review"]):
            with patch.object(sys, "argv", argv), patch("critique_bot.agent_cli.main", return_value=0) as crit:
                self.assertEqual(entry._run(), 0)
            crit.assert_called_once_with(["review"])

    def test_no_mode_word_runs_the_agent(self) -> None:
        with patch.object(sys, "argv", ["crit", "fix the build"]), patch(
            "critique_bot.cli.main", return_value=0
        ) as cli_main:
            self.assertEqual(entry._run(), 0)
        cli_main.assert_called_once_with(["--mode", "agent", "--crit-defaults", "fix the build"])

    def test_not_windows_is_never_a_double_click(self) -> None:
        with patch.object(entry.os, "name", "posix"):
            self.assertFalse(entry.launched_from_explorer())


class DoubleClickTests(unittest.TestCase):
    def _main(self, argv: list[str], *, set_up: bool):
        location = SimpleNamespace(path=Path("/u/config.json"), exists=set_up)
        out = io.StringIO()
        with patch.object(entry, "launched_from_explorer", return_value=True), \
                patch.object(sys, "argv", argv), \
                patch("critique_bot.config_cmd.resolve_config_path", return_value=location), \
                patch("critique_bot.cli._first_run_wizard", return_value=Path("/u/config.json")) as wizard, \
                patch("builtins.input", return_value="") as enter, \
                redirect_stdout(out):
            code = entry.main()
        return code, out.getvalue(), wizard, enter

    def test_first_double_click_sets_up_then_explains_and_waits(self) -> None:
        code, out, wizard, enter = self._main(["crit.exe"], set_up=False)
        self.assertEqual(code, 0)
        wizard.assert_called_once()
        self.assertIn("crit runs in a terminal", out)
        enter.assert_called_once()

    def test_later_double_click_explains_and_waits(self) -> None:
        code, out, wizard, enter = self._main(["crit.exe"], set_up=True)
        self.assertEqual(code, 0)
        wizard.assert_not_called()
        self.assertIn("crit is set up", out)
        enter.assert_called_once()

    def test_a_failure_keeps_the_window_readable(self) -> None:
        out = io.StringIO()
        with patch.object(entry, "launched_from_explorer", return_value=True), \
                patch.object(sys, "argv", ["crit.exe"]), \
                patch("critique_bot.config_cmd.resolve_config_path", side_effect=RuntimeError("disk full")), \
                patch("builtins.input", return_value="") as enter, \
                redirect_stdout(out):
            code = entry.main()
        self.assertEqual(code, 1)
        self.assertIn("crit stopped: disk full", out.getvalue())
        enter.assert_called_once()

    def test_with_arguments_it_runs_the_command_then_waits(self) -> None:
        with patch.object(entry, "launched_from_explorer", return_value=True), \
                patch.object(sys, "argv", ["crit.exe", "config"]), \
                patch.object(entry, "_run", return_value=0) as run, \
                patch("builtins.input", return_value="") as enter:
            self.assertEqual(entry.main(), 0)
        run.assert_called_once()
        enter.assert_called_once()

    def test_from_a_terminal_nothing_waits(self) -> None:
        with patch.object(entry, "launched_from_explorer", return_value=False), \
                patch.object(entry, "_run", return_value=0), \
                patch("builtins.input") as enter:
            self.assertEqual(entry.main(), 0)
        enter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
