"""Worker time limits, attempt ceilings, retention, and completion metadata."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot.browser import BrowserError
from critique_bot.config import BotConfig, Selectors
from critique_bot.chat_client import COMPLETION_IDLE, COMPLETION_STOPPED
from critique_bot.provider import ChatProvider, ChatSession
from critique_bot.queue import FileQueue, Job
from critique_bot.worker import _execute_job, _JobWatchdog


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


class _Session(ChatSession):
    def __init__(self, reply: str | BaseException, detail: dict | None = None) -> None:
        self._reply = reply
        self.last_detail = detail

    def send(self, prompt: str) -> str:
        del prompt
        if isinstance(self._reply, BaseException):
            raise self._reply
        return self._reply


class _Provider(ChatProvider):
    def __init__(self, reply: str | BaseException, detail: dict | None = None) -> None:
        self._reply = reply
        self._detail = detail

    def session(self, *, isolated: bool = False, model: str | None = None) -> _Session:
        del isolated, model
        return _Session(self._reply, self._detail)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.queue = FileQueue(self.root)
        self.config = _config(str(self.root))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def claim(self, prompt: str = "patch") -> Job:
        self.queue.enqueue(mode="review", stem="review", prompt=prompt, label="t")
        job = self.queue.claim()
        assert job is not None
        return job


class JobTimeoutTests(_Case):
    def test_default_limit_is_derived_from_the_call_timeout(self) -> None:
        self.assertEqual(_config(str(self.root), timeout_ms=180_000).job_timeout_sec, 420.0)

    def test_explicit_limit_wins(self) -> None:
        config = _config(str(self.root), job_timeout_seconds=42.0)
        self.assertEqual(config.job_timeout_sec, 42.0)

    def test_watchdog_fails_an_overrunning_job(self) -> None:
        job = self.claim()
        watchdog = _JobWatchdog(self.queue, job, 0.05)
        with watchdog:
            import time

            time.sleep(0.3)
        self.assertTrue(watchdog.fired)
        status = self.queue.read_status(job.id)
        assert status is not None
        self.assertFalse(status.ok)
        self.assertIn("time limit", status.error or "")

    def test_watchdog_is_cancelled_when_the_job_finishes(self) -> None:
        job = self.claim()
        with _JobWatchdog(self.queue, job, 5.0) as watchdog:
            pass
        self.assertFalse(watchdog.fired)
        self.assertIsNone(self.queue.read_status(job.id))

    def test_watchdog_does_not_overwrite_a_finished_job(self) -> None:
        job = self.claim()
        self.queue.fail(job.id, "already failed", stem=job.stem)
        watchdog = _JobWatchdog(self.queue, job, 0.05)
        watchdog._fire()
        status = self.queue.read_status(job.id)
        assert status is not None
        self.assertEqual(status.error, "already failed")


class CompletionMetadataTests(_Case):
    def test_a_clean_finish_is_recorded(self) -> None:
        job = self.claim()
        provider = _Provider("LGTM", {"completion": COMPLETION_STOPPED, "complete": True})
        _execute_job(provider, self.config, self.queue, job, isolated=False)
        payload = json.loads(
            (self.queue.result_dir(job.id) / "review.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["completion"]["completion"], COMPLETION_STOPPED)
        self.assertTrue(payload["completion"]["complete"])

    def test_an_idle_finish_is_flagged_as_possibly_truncated(self) -> None:
        job = self.claim()
        provider = _Provider("half a rev", {"completion": COMPLETION_IDLE, "complete": False})
        with patch("critique_bot.worker.log.warn") as warn:
            _execute_job(provider, self.config, self.queue, job, isolated=False)
        payload = json.loads(
            (self.queue.result_dir(job.id) / "review.json").read_text(encoding="utf-8")
        )
        self.assertFalse(payload["completion"]["complete"])
        self.assertTrue(
            any("truncated" in str(call.args[0]) for call in warn.call_args_list)
        )

    def test_no_detail_means_no_completion_key(self) -> None:
        job = self.claim()
        _execute_job(_Provider("LGTM"), self.config, self.queue, job, isolated=False)
        payload = json.loads(
            (self.queue.result_dir(job.id) / "review.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("completion", payload)


class AttemptCeilingTests(_Case):
    def test_browser_errors_stop_requeuing_at_the_ceiling(self) -> None:
        config = _config(str(self.root), max_attempts=2)
        provider = _Provider(BrowserError("edge gone"))

        job = self.claim()
        with self.assertRaises(BrowserError):
            _execute_job(provider, config, self.queue, job, isolated=False)
        self.assertTrue((self.queue.inbox / f"{job.id}.json").is_file())

        again = self.queue.claim()
        assert again is not None
        self.assertEqual(again.attempts, 1)
        with self.assertRaises(BrowserError):
            _execute_job(provider, config, self.queue, again, isolated=False)

        # Out of attempts: failed for good instead of cycling forever.
        self.assertFalse((self.queue.inbox / f"{job.id}.json").exists())
        status = self.queue.read_status(job.id)
        assert status is not None
        self.assertFalse(status.ok)
        self.assertIn("gave up", status.error or "")

    def test_results_are_pruned_after_a_job(self) -> None:
        config = _config(str(self.root), result_retention=1)
        for _ in range(3):
            job = self.claim()
            _execute_job(_Provider("ok"), config, self.queue, job, isolated=False)
        self.assertEqual(len(list(self.queue.results.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
