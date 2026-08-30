from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from critique_bot.queue import FileQueue, QueueError, RateLimiter


class RateLimiterTests(unittest.TestCase):
    def test_first_call_does_not_sleep(self) -> None:
        limiter = RateLimiter(30, 5)
        self.assertEqual(limiter.wait(), 0.0)

    def test_zero_interval_never_sleeps(self) -> None:
        limiter = RateLimiter(0, 10)
        self.assertEqual(limiter.wait(), 0.0)
        self.assertEqual(limiter.wait(), 0.0)


class FileQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.queue = FileQueue(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_enqueue_claim_fifo(self) -> None:
        first = self.queue.enqueue(mode="review", stem="review", prompt="first patch")
        second = self.queue.enqueue(mode="review", stem="review", prompt="second patch")
        claimed = self.queue.claim()
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.id, first)
        self.assertEqual(claimed.prompt, "first patch")
        claimed = self.queue.claim()
        assert claimed is not None
        self.assertEqual(claimed.id, second)
        self.assertIsNone(self.queue.claim())

    def test_finish_and_wait(self) -> None:
        job_id = self.queue.enqueue(mode="review", stem="review", prompt="patch")
        claimed = self.queue.claim()
        assert claimed is not None
        self.queue.beat()
        out = self.queue.result_dir(job_id)
        (out / "review.md").write_text("looks good\n", encoding="utf-8")
        from critique_bot.queue import JobStatus

        self.queue.finish(
            job_id,
            JobStatus(
                id=job_id,
                ok=True,
                error=None,
                stem="review",
                started_at=None,
                finished_at=None,
                elapsed_seconds=1.5,
            ),
        )
        status = self.queue.wait(job_id, timeout_sec=2)
        self.assertTrue(status.ok)
        self.assertEqual(status.stem, "review")
        self.assertFalse((self.queue.processing / f"{job_id}.json").exists())

    def test_wait_fails_when_worker_dead(self) -> None:
        job_id = self.queue.enqueue(mode="review", stem="review", prompt="patch")
        with self.assertRaises(QueueError) as ctx:
            self.queue.wait(job_id, timeout_sec=1, poll_sec=0.05)
        self.assertIn("worker is not running", str(ctx.exception))

    def test_requeue_stale_processing(self) -> None:
        job_id = self.queue.enqueue(mode="review", stem="review", prompt="patch")
        self.assertIsNotNone(self.queue.claim())
        self.assertTrue((self.queue.processing / f"{job_id}.json").is_file())
        moved = self.queue.requeue_stale_processing()
        self.assertEqual(moved, 1)
        self.assertTrue((self.queue.inbox / f"{job_id}.json").is_file())

    def test_worker_lock_is_exclusive(self) -> None:
        first = self.queue.acquire_worker_lock()
        with self.assertRaises(QueueError):
            self.queue.acquire_worker_lock()
        first.close()
        second = self.queue.acquire_worker_lock()
        second.close()

    def test_heartbeat_alive(self) -> None:
        self.assertFalse(self.queue.worker_alive())
        self.queue.beat()
        self.assertTrue(self.queue.worker_alive())
        payload = json.loads(self.queue.heartbeat_path.read_text(encoding="utf-8"))
        self.assertIn("pid", payload)


class RateLimiterSleepTests(unittest.TestCase):
    def test_second_call_waits(self) -> None:
        limiter = RateLimiter(0.15, 0)
        limiter.wait()
        started = time.monotonic()
        slept = limiter.wait()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(slept, 0.05)
        self.assertGreaterEqual(elapsed, 0.05)


if __name__ == "__main__":
    unittest.main()
