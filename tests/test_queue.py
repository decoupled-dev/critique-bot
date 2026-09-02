from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from critique_bot.queue import FileQueue, QueueError, RateLimiter, job_label


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

    def test_enqueue_labels_job_id_from_mr_meta(self) -> None:
        job_id = self.queue.enqueue(
            mode="review",
            stem="review",
            prompt="patch",
            meta={
                "CI_PROJECT_PATH": "group/app",
                "CI_MERGE_REQUEST_IID": "42",
                "CI_JOB_ID": "99",
            },
        )
        self.assertIn("-group-app-mr42-", job_id)
        claimed = self.queue.claim()
        assert claimed is not None
        self.assertEqual(claimed.label, "group-app-mr42")
        self.assertEqual(claimed.meta["CI_MERGE_REQUEST_IID"], "42")
        self.queue.write_job_record(claimed)
        record = json.loads(
            (self.queue.result_dir(job_id) / "job.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["label"], "group-app-mr42")
        self.assertEqual(record["meta"]["CI_JOB_ID"], "99")

    def test_enqueue_round_trips_staged_files(self) -> None:
        job_id = self.queue.enqueue(
            mode="review",
            stem="review",
            prompt="REVIEW NOW patch",
            files={"Foo.java": "class Foo {}", "Bar.kt": "class Bar"},
        )
        claimed = self.queue.claim()
        assert claimed is not None
        self.assertEqual(claimed.id, job_id)
        self.assertEqual(claimed.files["Foo.java"], "class Foo {}")
        self.assertEqual(claimed.files["Bar.kt"], "class Bar")
        raw = json.loads((self.root / "processing" / f"{job_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["files"]["Foo.java"], "class Foo {}")

    def test_enqueue_omits_empty_files(self) -> None:
        job_id = self.queue.enqueue(mode="review", stem="review", prompt="p")
        raw = json.loads((self.root / "inbox" / f"{job_id}.json").read_text(encoding="utf-8"))
        self.assertNotIn("files", raw)
        claimed = self.queue.claim()
        assert claimed is not None
        self.assertEqual(claimed.files, {})

    def test_enqueue_explicit_label_overrides_meta(self) -> None:
        job_id = self.queue.enqueue(
            mode="review",
            stem="review",
            prompt="patch",
            meta={"CI_MERGE_REQUEST_IID": "7"},
            label="hotfix",
        )
        self.assertIn("-hotfix-", job_id)

    def test_finish_persists_meta(self) -> None:
        from critique_bot.queue import JobStatus

        job_id = self.queue.enqueue(
            mode="review",
            stem="review",
            prompt="patch",
            meta={"CI_MERGE_REQUEST_IID": "3"},
        )
        claimed = self.queue.claim()
        assert claimed is not None
        self.queue.finish(
            job_id,
            JobStatus(
                id=job_id,
                ok=True,
                error=None,
                stem="review",
                started_at=None,
                finished_at=None,
                elapsed_seconds=1.0,
                label=claimed.label,
                meta=claimed.meta,
            ),
        )
        status = self.queue.read_status(job_id)
        assert status is not None
        self.assertEqual(status.label, "mr3")
        self.assertEqual(status.meta["CI_MERGE_REQUEST_IID"], "3")


class JobLabelTests(unittest.TestCase):
    def test_gitlab_mr(self) -> None:
        self.assertEqual(
            job_label(
                {
                    "CI_PROJECT_PATH": "decoupled-group/critique-bot",
                    "CI_MERGE_REQUEST_IID": "12",
                }
            ),
            "decoupled-group-critique-bot-mr12",
        )

    def test_explicit_and_local(self) -> None:
        self.assertEqual(job_label({}, explicit=" My Review "), "My-Review")
        self.assertEqual(job_label({}), "local")


class RateLimiterSleepTests(unittest.TestCase):
    def test_second_call_waits(self) -> None:
        limiter = RateLimiter(0.15, 0)
        limiter.wait()
        started = time.monotonic()
        slept = limiter.wait()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(slept, 0.05)
        self.assertGreaterEqual(elapsed, 0.05)

    def test_negative_values_clamped(self) -> None:
        limiter = RateLimiter(-1, -5)
        self.assertEqual(limiter.min_interval, 0.0)
        self.assertEqual(limiter.jitter, 0.0)


class FileQueueMoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.queue = FileQueue(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_claim_empty_prompt_fails_job(self) -> None:
        dest = self.queue.inbox / "bad.json"
        dest.write_text(json.dumps({"id": "bad", "prompt": "  "}), encoding="utf-8")
        self.assertIsNone(self.queue.claim())
        status = self.queue.read_status("bad")
        assert status is not None
        self.assertFalse(status.ok)
        self.assertIn("empty", status.error or "")

    def test_claim_invalid_json(self) -> None:
        (self.queue.inbox / "nope.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.queue.claim())
        status = self.queue.read_status("nope")
        assert status is not None
        self.assertFalse(status.ok)

    def test_claim_json_array(self) -> None:
        (self.queue.inbox / "arr.json").write_text("[1]", encoding="utf-8")
        self.assertIsNone(self.queue.claim())

    def test_general_mode_default_stem(self) -> None:
        job_id = self.queue.enqueue(mode="general", stem="", prompt="hello")
        claimed = self.queue.claim()
        assert claimed is not None
        self.assertEqual(claimed.id, job_id)
        self.assertEqual(claimed.stem, "reply")
        self.assertEqual(claimed.mode, "general")

    def test_read_status_missing(self) -> None:
        self.assertIsNone(self.queue.read_status("missing"))

    def test_fail_writes_status(self) -> None:
        job_id = self.queue.enqueue(mode="review", stem="review", prompt="p")
        self.queue.fail(job_id, "nope", stem="review", label="x")
        status = self.queue.read_status(job_id)
        assert status is not None
        self.assertFalse(status.ok)
        self.assertEqual(status.error, "nope")
        self.assertEqual(status.label, "x")

    def test_worker_hint_no_file(self) -> None:
        self.assertEqual(self.queue.worker_hint(), "no worker heartbeat file")

    def test_worker_hint_unreadable(self) -> None:
        self.queue.heartbeat_path.write_text("not json", encoding="utf-8")
        hint = self.queue.worker_hint()
        self.assertIn("pid=?", hint)

    def test_worker_hint_after_beat(self) -> None:
        self.queue.beat(pid=12345)
        hint = self.queue.worker_hint()
        self.assertIn("12345", hint)

    def test_wait_timeout(self) -> None:
        job_id = self.queue.enqueue(mode="review", stem="review", prompt="p")
        self.queue.beat()
        with self.assertRaises(QueueError) as ctx:
            self.queue.wait(job_id, timeout_sec=0.2, poll_sec=0.05)
        self.assertIn("timed out", str(ctx.exception))

    def test_worker_alive_stale(self) -> None:
        self.queue.beat()
        self.assertFalse(self.queue.worker_alive(stale_sec=-1))

    def test_close_lock_twice(self) -> None:
        lock = self.queue.acquire_worker_lock()
        lock.close()
        lock.close()

    def test_model_none_when_empty(self) -> None:
        job_id = self.queue.enqueue(mode="review", stem="review", prompt="p", model="")
        claimed = self.queue.claim()
        assert claimed is not None
        self.assertEqual(claimed.id, job_id)
        self.assertIsNone(claimed.model)

    def test_requeue_none_when_empty(self) -> None:
        self.assertEqual(self.queue.requeue_stale_processing(), 0)

    def test_requeue_job_moves_processing_to_inbox(self) -> None:
        job_id = self.queue.enqueue(mode="review", stem="review", prompt="p")
        self.assertIsNotNone(self.queue.claim())
        self.assertTrue((self.queue.processing / f"{job_id}.json").is_file())
        self.assertTrue(self.queue.requeue_job(job_id))
        self.assertTrue((self.queue.inbox / f"{job_id}.json").is_file())
        self.assertFalse((self.queue.processing / f"{job_id}.json").exists())
        self.assertTrue(self.queue.requeue_job(job_id))

    def test_requeue_job_missing(self) -> None:
        self.assertFalse(self.queue.requeue_job("no-such-job"))


class SlugAndLabelTests(unittest.TestCase):
    def test_ci_job_id(self) -> None:
        self.assertEqual(job_label({"CI_JOB_ID": "55"}), "ci55")

    def test_gitlab_mr_without_project(self) -> None:
        self.assertEqual(job_label({"CI_MERGE_REQUEST_IID": "8"}), "mr8")

    def test_safe_slug_special_chars(self) -> None:
        from critique_bot.queue import _safe_slug

        self.assertEqual(_safe_slug("Hello World!!", 48), "Hello-World")
        self.assertEqual(_safe_slug("@@@", 8), "job")
        self.assertEqual(_safe_slug("", 8), "")
        self.assertEqual(_safe_slug("a" * 80, 5), "a" * 5)


if __name__ == "__main__":
    unittest.main()
