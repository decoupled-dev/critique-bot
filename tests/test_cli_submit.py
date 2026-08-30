from __future__ import annotations

import io
import json
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr
from pathlib import Path

from critique_bot.cli import main


class SubmitCliTests(unittest.TestCase):
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
        self.patch = self.folder / "diff.patch"
        self.patch.write_text("diff --git a/a b/a\n+hello\n", encoding="utf-8")
        self.out = self.folder / "out"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_submit_fails_if_worker_is_down(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "submit",
                    "--config",
                    str(self.config),
                    "--patch-file",
                    str(self.patch),
                    "--output-dir",
                    str(self.out),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("worker is not running", stderr.getvalue())

    def test_submit_rejects_chat_mode(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "submit",
                    "--config",
                    str(self.config),
                    "--mode",
                    "chat",
                    "--prompt",
                    "hi",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("chat", stderr.getvalue().lower())

    def test_submit_success_copies_results(self) -> None:
        from critique_bot.queue import FileQueue, JobStatus

        queue = FileQueue(self.folder / "queue")
        queue.beat()

        original_wait = FileQueue.wait

        def finish_then_wait(self_q, job_id, *, timeout_sec, poll_sec=0.5):
            dest = self_q.result_dir(job_id)
            (dest / "review.md").write_text("ok from worker\n", encoding="utf-8")
            (dest / "review.json").write_text("{}\n", encoding="utf-8")
            self_q.finish(
                job_id,
                JobStatus(
                    id=job_id,
                    ok=True,
                    error=None,
                    stem="review",
                    started_at=None,
                    finished_at=None,
                    elapsed_seconds=0.2,
                    label="local",
                    meta={},
                ),
            )
            return original_wait(self_q, job_id, timeout_sec=timeout_sec, poll_sec=poll_sec)

        stderr = io.StringIO()
        with unittest.mock.patch.object(FileQueue, "wait", finish_then_wait):
            with redirect_stderr(stderr):
                from contextlib import redirect_stdout

                with redirect_stdout(io.StringIO()):
                    code = main(
                        [
                            "submit",
                            "--config",
                            str(self.config),
                            "--patch-file",
                            str(self.patch),
                            "--output-dir",
                            str(self.out),
                            "--wait-timeout",
                            "5",
                        ]
                    )
        self.assertEqual(code, 0)
        self.assertTrue((self.out / "review.md").is_file())
        self.assertIn("ok from worker", (self.out / "review.md").read_text(encoding="utf-8"))

    def test_submit_failed_job(self) -> None:
        from critique_bot.queue import FileQueue, JobStatus

        queue = FileQueue(self.folder / "queue")
        queue.beat()

        def fail_wait(self_q, job_id, *, timeout_sec, poll_sec=0.5):
            del timeout_sec, poll_sec
            self_q.fail(job_id, "model crashed", stem="review")
            status = self_q.read_status(job_id)
            assert status is not None
            return status

        stderr = io.StringIO()
        with unittest.mock.patch.object(FileQueue, "wait", fail_wait):
            with redirect_stderr(stderr):
                code = main(
                    [
                        "submit",
                        "--config",
                        str(self.config),
                        "--patch-file",
                        str(self.patch),
                        "--output-dir",
                        str(self.out),
                    ]
                )
        self.assertEqual(code, 1)
        self.assertIn("model crashed", stderr.getvalue())

    def test_submit_headed_is_ignored(self) -> None:
        from critique_bot.queue import FileQueue

        FileQueue(self.folder / "queue").beat()

        def wait_ok(self_q, job_id, *, timeout_sec, poll_sec=0.5):
            from critique_bot.queue import JobStatus

            dest = self_q.result_dir(job_id)
            (dest / "review.md").write_text("x\n", encoding="utf-8")
            self_q.finish(
                job_id,
                JobStatus(
                    id=job_id,
                    ok=True,
                    error=None,
                    stem="review",
                    started_at=None,
                    finished_at=None,
                    elapsed_seconds=0.1,
                ),
            )
            return self_q.read_status(job_id)

        with unittest.mock.patch.object(FileQueue, "wait", wait_ok):
            with redirect_stderr(io.StringIO()):
                with io.StringIO() as buf:
                    from contextlib import redirect_stdout

                    with redirect_stdout(buf):
                        code = main(
                            [
                                "submit",
                                "--config",
                                str(self.config),
                                "--patch-file",
                                str(self.patch),
                                "--output-dir",
                                str(self.out),
                                "--headed",
                            ]
                        )
        self.assertEqual(code, 0)

    def test_submit_bad_config(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "submit",
                    "--config",
                    str(self.folder / "missing.json"),
                    "--patch-file",
                    str(self.patch),
                ]
            )
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
