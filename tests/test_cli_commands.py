"""Routing and output for the queue-status and setup subcommands."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from critique_bot.cli import SUBCOMMANDS, main


class _CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.config = self.folder / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "url": "https://example.invalid/chat",
                    "selectors": {
                        "prompt_input": "textarea",
                        "assistant_messages": ".assistant",
                    },
                    "queue_dir": str(self.folder / "queue"),
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()


class SubcommandRoutingTests(_CliCase):
    def test_every_advertised_subcommand_has_a_handler(self) -> None:
        for name in SUBCOMMANDS:
            with self.assertRaises(SystemExit) as ctx:
                self.run_cli([name, "--help"])
            self.assertEqual(ctx.exception.code, 0, name)

    def test_unknown_first_arg_falls_through_to_the_default_run(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli(["not-a-subcommand"])


class QueueStatusTests(_CliCase):
    def test_reports_a_missing_worker_and_exits_one(self) -> None:
        code, out, _ = self.run_cli(["queue-status", "--config", str(self.config)])
        self.assertEqual(code, 1)
        self.assertIn("worker     NOT RUNNING", out)
        self.assertIn("Start the worker", out)

    def test_lists_waiting_jobs(self) -> None:
        from critique_bot.queue import FileQueue

        queue = FileQueue(self.folder / "queue")
        queue.enqueue(mode="review", stem="review", prompt="patch", label="mr7")
        queue.beat()
        code, out, _ = self.run_cli(["queue-status", "--config", str(self.config)])
        self.assertEqual(code, 0)
        self.assertIn("worker     running", out)
        self.assertIn("waiting    1 job(s)", out)
        self.assertIn("mr7", out)

    def test_lists_recent_failures(self) -> None:
        from critique_bot.queue import FileQueue

        queue = FileQueue(self.folder / "queue")
        job_id = queue.enqueue(mode="review", stem="review", prompt="p", label="mr8")
        queue.claim()
        queue.fail(job_id, "model down", stem="review", label="mr8")
        queue.beat()
        code, out, _ = self.run_cli(["queue-status", "--config", str(self.config)])
        self.assertEqual(code, 0)
        self.assertIn("FAIL", out)
        self.assertIn("model down", out)

    def test_json_mode(self) -> None:
        code, out, _ = self.run_cli(
            ["queue-status", "--config", str(self.config), "--json"]
        )
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["worker_alive"])
        self.assertEqual(payload["waiting"], [])

    def test_bad_config_exits_one(self) -> None:
        self.config.write_text("{ nope", encoding="utf-8")
        code, _, err = self.run_cli(["queue-status", "--config", str(self.config)])
        self.assertEqual(code, 1)
        self.assertIn("invalid JSON", err)


class SetupCommandTests(_CliCase):
    def test_missing_config_is_reported(self) -> None:
        code, out, _ = self.run_cli(
            ["setup", "--config", str(self.folder / "nope.json"), "--no-open"]
        )
        self.assertEqual(code, 1)
        self.assertIn("config file not found", out)

    def test_serves_and_passes_through_options(self) -> None:
        with patch("critique_bot.setup_ui.run_setup", return_value=0) as run:
            code, _, _ = self.run_cli(
                ["setup", "--config", str(self.config), "--port", "1234", "--no-open"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["port"], 1234)
        self.assertFalse(run.call_args.kwargs["open_page"])


if __name__ == "__main__":
    unittest.main()
