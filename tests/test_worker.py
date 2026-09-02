from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot.browser import BrowserError
from critique_bot.config import BotConfig, Selectors
from critique_bot.provider import ChatProvider, ChatSession
from critique_bot.queue import FileQueue, Job, RateLimiter
from critique_bot.worker import (
    _execute_job,
    _run_job,
    _save_job_failure,
    _sequential_loop,
    _session_loop,
)


def _config(queue_dir: str, **overrides: object) -> BotConfig:
    values: dict[str, object] = {
        "url": "https://chat.example/",
        "selectors": Selectors(prompt_input="textarea", assistant_messages=".a"),
        "model": "GPT-5.1",
        "queue_dir": queue_dir,
        "min_interval_seconds": 0,
        "interval_jitter_seconds": 0,
        "max_parallel_tabs": 1,
        "timeout_ms": 5_000,
    }
    values.update(overrides)
    return BotConfig(**values)  # type: ignore[arg-type]


class FakeSession(ChatSession):
    def __init__(self, reply: str | BaseException, *, page: object | None = None) -> None:
        self._reply = reply
        self.page = page
        self.prompts: list[str] = []

    def send(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self._reply, BaseException):
            raise self._reply
        return self._reply

    def close(self) -> None:
        return None


class FakeProvider(ChatProvider):
    can_parallelize = True

    def __init__(self, reply: str | BaseException = "looks good") -> None:
        self.reply = reply
        self.sessions: list[FakeSession] = []
        self.isolated_flags: list[bool] = []

    def session(self, *, isolated: bool = False, model: str | None = None) -> FakeSession:
        del model
        session = FakeSession(self.reply)
        self.sessions.append(session)
        self.isolated_flags.append(isolated)
        return session


class ExecuteJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.queue = FileQueue(self.root)
        self.config = _config(str(self.root))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _job(self, prompt: str = "patch") -> Job:
        job_id = self.queue.enqueue(mode="review", stem="review", prompt=prompt, label="t")
        claimed = self.queue.claim()
        assert claimed is not None
        self.assertEqual(claimed.id, job_id)
        return claimed

    def test_success_writes_review_and_status(self) -> None:
        job = self._job()
        provider = FakeProvider("LGTM")
        _execute_job(provider, self.config, self.queue, job, isolated=False)
        status = self.queue.read_status(job.id)
        assert status is not None
        self.assertTrue(status.ok)
        body = (self.queue.result_dir(job.id) / "review.md").read_text(encoding="utf-8")
        self.assertEqual(body, "LGTM")
        record = json.loads(
            (self.queue.result_dir(job.id) / "job.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["id"], job.id)
        self.assertEqual(provider.isolated_flags, [False])

    def test_empty_reply_fails(self) -> None:
        job = self._job()
        _execute_job(FakeProvider("  \n"), self.config, self.queue, job, isolated=False)
        status = self.queue.read_status(job.id)
        assert status is not None
        self.assertFalse(status.ok)
        self.assertIn("empty", status.error or "")

    def test_chat_error_fails_without_raising(self) -> None:
        from critique_bot.chat_client import ChatError

        job = self._job()
        _execute_job(
            FakeProvider(ChatError("model down")),
            self.config,
            self.queue,
            job,
            isolated=False,
        )
        status = self.queue.read_status(job.id)
        assert status is not None
        self.assertFalse(status.ok)
        self.assertIn("model down", status.error or "")

    def test_unexpected_error_fails_without_raising(self) -> None:
        job = self._job()
        _execute_job(
            FakeProvider(RuntimeError("boom")),
            self.config,
            self.queue,
            job,
            isolated=False,
        )
        status = self.queue.read_status(job.id)
        assert status is not None
        self.assertFalse(status.ok)
        self.assertIn("unexpected", status.error or "")

    def test_browser_error_requeues_and_reraises(self) -> None:
        job = self._job()
        with self.assertRaises(BrowserError):
            _execute_job(
                FakeProvider(BrowserError("edge gone")),
                self.config,
                self.queue,
                job,
                isolated=True,
            )
        self.assertIsNone(self.queue.read_status(job.id))
        self.assertTrue((self.queue.inbox / f"{job.id}.json").is_file())
        self.assertFalse((self.queue.processing / f"{job.id}.json").exists())

    def test_closed_context_is_requeued_as_browser_error(self) -> None:
        job = self._job()
        with self.assertRaises(BrowserError) as ctx:
            _execute_job(
                FakeProvider(
                    RuntimeError(
                        "BrowserContext.new_page: Target page, context or browser has been closed"
                    )
                ),
                self.config,
                self.queue,
                job,
                isolated=False,
            )
        self.assertIn("restart the browser", str(ctx.exception).lower())
        self.assertIsNone(self.queue.read_status(job.id))
        self.assertTrue((self.queue.inbox / f"{job.id}.json").is_file())

    def test_chat_closed_page_is_requeued(self) -> None:
        from critique_bot.chat_client import ChatError

        job = self._job()
        with self.assertRaises(BrowserError):
            _execute_job(
                FakeProvider(
                    ChatError("Target page, context or browser has been closed")
                ),
                self.config,
                self.queue,
                job,
                isolated=False,
            )
        self.assertIsNone(self.queue.read_status(job.id))
        self.assertTrue((self.queue.inbox / f"{job.id}.json").is_file())

    def test_run_job_requeues_when_execute_did_not(self) -> None:
        job = self._job()
        with patch(
            "critique_bot.worker._execute_job",
            side_effect=BrowserError("later"),
        ):
            with self.assertRaises(BrowserError):
                _run_job(
                    FakeProvider("unused"),
                    self.config,
                    self.queue,
                    job,
                    isolated=False,
                )
        self.assertIsNone(self.queue.read_status(job.id))
        self.assertTrue((self.queue.inbox / f"{job.id}.json").is_file())

    def test_run_job_does_not_overwrite_status(self) -> None:
        job = self._job()
        self.queue.fail(job.id, "already", stem=job.stem, label=job.label)
        with patch(
            "critique_bot.worker._execute_job",
            side_effect=BrowserError("later"),
        ):
            with self.assertRaises(BrowserError):
                _run_job(
                    FakeProvider("unused"),
                    self.config,
                    self.queue,
                    job,
                    isolated=False,
                )
        status = self.queue.read_status(job.id)
        assert status is not None
        self.assertEqual(status.error, "already")

    def test_save_job_failure_no_page(self) -> None:
        _save_job_failure(FakeSession("x"), self.root)
        _save_job_failure(None, self.root)


class SessionLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.queue = FileQueue(self.root)
        self.config = _config(str(self.root), max_parallel_tabs=3)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_falls_back_to_sequential_without_parallel(self) -> None:
        provider = FakeProvider()
        provider.can_parallelize = False
        called = {"seq": False, "par": False}

        def seq(*args: object, **kwargs: object) -> None:
            called["seq"] = True

        def par(*args: object, **kwargs: object) -> None:
            called["par"] = True

        with patch("critique_bot.worker._sequential_loop", seq):
            with patch("critique_bot.worker._parallel_loop", par):
                _session_loop(
                    provider,
                    self.config,
                    self.queue,
                    RateLimiter(0, 0),
                    lambda: True,
                )
        self.assertTrue(called["seq"])
        self.assertFalse(called["par"])

    def test_sequential_processes_one_job(self) -> None:
        self.queue.enqueue(mode="review", stem="review", prompt="p")
        provider = FakeProvider("ok")
        stop_after = {"n": 0}

        def stop() -> bool:
            return stop_after["n"] >= 1

        real_run = _run_job

        def wrapped(*args: object, **kwargs: object) -> None:
            stop_after["n"] += 1
            return real_run(*args, **kwargs)  # type: ignore[arg-type]

        with patch("critique_bot.worker._run_job", wrapped):
            _sequential_loop(
                provider,
                _config(str(self.root)),
                self.queue,
                RateLimiter(0, 0),
                stop,
            )
        status_files = list((self.root / "results").glob("*/status.json"))
        self.assertEqual(len(status_files), 1)


class HeartbeatLoopSmokeTests(unittest.TestCase):
    def test_heartbeat_loop_stops(self) -> None:
        from critique_bot.worker import _heartbeat_loop

        with tempfile.TemporaryDirectory() as tmp:
            queue = FileQueue(Path(tmp))
            stop = threading.Event()

            def flag() -> bool:
                return stop.is_set()

            thread = threading.Thread(target=_heartbeat_loop, args=(queue, flag), daemon=True)
            thread.start()
            deadline = threading.Event()
            deadline.wait(0.3)
            self.assertTrue(queue.heartbeat_path.is_file())
            stop.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
