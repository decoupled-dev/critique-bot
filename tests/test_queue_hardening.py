# SPDX-License-Identifier: Apache-2.0
"""Attempt limits, dead-lettering, retention, and queue introspection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot.queue import FileQueue


class AttemptLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = FileQueue(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claimed(self) -> str:
        job_id = self.queue.enqueue(mode="review", stem="review", prompt="patch")
        claimed = self.queue.claim()
        assert claimed is not None
        return job_id

    def test_requeue_counts_attempts(self) -> None:
        job_id = self._claimed()
        self.assertTrue(self.queue.requeue_job(job_id, max_attempts=3))
        payload = json.loads((self.queue.inbox / f"{job_id}.json").read_text())
        self.assertEqual(payload["attempts"], 1)
        job = self.queue.claim()
        assert job is not None
        self.assertEqual(job.attempts, 1)

    def test_gives_up_after_max_attempts(self) -> None:
        job_id = self._claimed()
        self.assertTrue(self.queue.requeue_job(job_id, max_attempts=2))
        self.assertIsNotNone(self.queue.claim())
        # Second failure hits the ceiling: the job is failed, not requeued.
        self.assertFalse(self.queue.requeue_job(job_id, max_attempts=2))
        self.assertFalse((self.queue.inbox / f"{job_id}.json").exists())
        self.assertFalse((self.queue.processing / f"{job_id}.json").exists())
        status = self.queue.read_status(job_id)
        assert status is not None
        self.assertFalse(status.ok)
        self.assertIn("gave up after", status.error or "")

    def test_unreadable_job_is_failed_not_requeued(self) -> None:
        job_id = self._claimed()
        (self.queue.processing / f"{job_id}.json").write_text("{ broken", encoding="utf-8")
        self.assertFalse(self.queue.requeue_job(job_id))
        status = self.queue.read_status(job_id)
        assert status is not None
        self.assertFalse(status.ok)
        self.assertIn("unreadable", status.error or "")

    def test_missing_job_returns_false(self) -> None:
        self.assertFalse(self.queue.requeue_job("does-not-exist"))

    def test_already_requeued_job_is_idempotent(self) -> None:
        job_id = self._claimed()
        self.queue.requeue_job(job_id)
        self.assertTrue(self.queue.requeue_job(job_id))

    def test_stale_processing_recovery_counts_attempts(self) -> None:
        job_id = self._claimed()
        self.assertEqual(self.queue.requeue_stale_processing(), 1)
        payload = json.loads((self.queue.inbox / f"{job_id}.json").read_text())
        self.assertEqual(payload["attempts"], 1)

    def test_stale_processing_dead_letters_at_the_limit(self) -> None:
        job_id = self._claimed()
        self.assertEqual(self.queue.requeue_stale_processing(max_attempts=1), 0)
        status = self.queue.read_status(job_id)
        assert status is not None
        self.assertFalse(status.ok)


class OrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = FileQueue(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fifo_holds_for_jobs_enqueued_in_the_same_millisecond(self) -> None:
        # Job ids sort lexicographically; without a monotonic stamp the random
        # suffix decides the order of same-millisecond jobs.
        with patch("critique_bot.queue.time.time", return_value=1_700_000_000.0):
            ids = [
                self.queue.enqueue(mode="review", stem="review", prompt=str(index))
                for index in range(25)
            ]
        self.assertEqual(len(set(ids)), 25)
        claimed = []
        while True:
            job = self.queue.claim()
            if job is None:
                break
            claimed.append(job.id)
        self.assertEqual(claimed, ids)


class RetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = FileQueue(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_prunes_oldest_result_folders(self) -> None:
        for index in range(6):
            self.queue.result_dir(f"{index:013d}-job-abcdef01")
        self.assertEqual(self.queue.prune_results(keep=2), 4)
        remaining = sorted(item.name for item in self.queue.results.iterdir())
        self.assertEqual(len(remaining), 2)
        # Job ids are timestamp-prefixed, so the newest survive.
        self.assertTrue(remaining[-1].startswith("0000000000005"))

    def test_keeps_everything_under_the_limit(self) -> None:
        self.queue.result_dir("0000000000001-job-aaaaaaaa")
        self.assertEqual(self.queue.prune_results(keep=10), 0)

    def test_keep_is_never_zero(self) -> None:
        self.queue.result_dir("0000000000001-job-aaaaaaaa")
        self.queue.prune_results(keep=0)
        self.assertEqual(len(list(self.queue.results.iterdir())), 1)


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = FileQueue(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_snapshot_reports_waiting_and_processing(self) -> None:
        self.queue.enqueue(mode="review", stem="review", prompt="a", label="mr1")
        second = self.queue.enqueue(mode="review", stem="review", prompt="b", label="mr2")
        self.queue.claim()
        snapshot = self.queue.snapshot()
        self.assertEqual(len(snapshot["waiting"]), 1)
        self.assertEqual(len(snapshot["processing"]), 1)
        self.assertEqual(snapshot["waiting"][0]["id"], second)
        self.assertEqual(snapshot["waiting"][0]["label"], "mr2")
        self.assertEqual(snapshot["waiting"][0]["prompt_chars"], 1)
        self.assertFalse(snapshot["worker_alive"])

    def test_snapshot_survives_a_corrupt_job_file(self) -> None:
        (self.queue.inbox / "broken.json").write_text("nope", encoding="utf-8")
        snapshot = self.queue.snapshot()
        self.assertEqual(snapshot["waiting"][0]["error"], "unreadable job file")

    def test_recent_results_include_failures(self) -> None:
        job_id = self.queue.enqueue(mode="review", stem="review", prompt="a")
        self.queue.claim()
        self.queue.fail(job_id, "model down", stem="review", label="mr1")
        recent = self.queue.recent_results(limit=5)
        self.assertEqual(len(recent), 1)
        self.assertFalse(recent[0]["ok"])
        self.assertEqual(recent[0]["error"], "model down")

    def test_recent_results_limit_zero(self) -> None:
        self.assertEqual(self.queue.recent_results(limit=0), [])

    def test_worker_alive_after_beat(self) -> None:
        self.queue.beat()
        self.assertTrue(self.queue.worker_alive())
        self.assertIn("pid=", self.queue.worker_hint())


if __name__ == "__main__":
    unittest.main()


class PathTraversalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.queue = FileQueue(self.base / "q")

    def _drop(self, name: str, payload: dict) -> None:
        (self.queue.inbox / name).write_text(json.dumps(payload), encoding="utf-8")

    def _outside(self) -> list[Path]:
        return [p for p in self.base.rglob("*") if self.queue.root not in (p, *p.parents)]

    def test_bad_id_falls_back_to_file_stem(self) -> None:
        self._drop("good-job.json", {"id": "../../escape", "prompt": "p"})
        job = self.queue.claim()
        assert job is not None
        self.assertEqual(job.id, "good-job")
        self.queue.result_dir(job.id)
        self.assertEqual(self._outside(), [])

    def test_bad_stem_is_rejected_and_failed_inside_results(self) -> None:
        for stem in ("../../evil", "a/b", "..", ".hidden", "x" * 200, 5):
            with self.subTest(stem=stem):
                self._drop("job-1.json", {"id": "job-1", "prompt": "p", "stem": stem})
                self.assertIsNone(self.queue.claim())
                status = self.queue.read_status("job-1")
                assert status is not None
                self.assertFalse(status.ok)
                self.assertEqual(status.stem, "review")
        self.assertEqual(self._outside(), [])

    def test_unsafe_file_name_is_failed_safely(self) -> None:
        self._drop("...json", {"prompt": "p"})
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self._outside(), [])
        failed = [p.name for p in self.queue.results.iterdir()]
        self.assertTrue(any(name.startswith("invalid-") for name in failed), failed)

    def test_result_dir_and_read_status_refuse_traversal(self) -> None:
        from critique_bot.queue import QueueError

        for bad in ("..", "../x", "a/../../b", "/etc", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(QueueError):
                    self.queue.result_dir(bad)
                self.assertIsNone(self.queue.read_status(bad))
                self.assertFalse(self.queue.requeue_job(bad))

    def test_enqueue_rejects_bad_stem(self) -> None:
        from critique_bot.queue import QueueError

        with self.assertRaises(QueueError):
            self.queue.enqueue(mode="review", stem="../x", prompt="p")

    def test_write_output_stays_in_its_folder(self) -> None:
        from critique_bot.output import write_output

        with self.assertRaises(ValueError):
            write_output(self.base / "out", "b", {}, stem="../escape", print_body=False)
        self.assertFalse((self.base / "escape.md").exists())


@unittest.skipIf(__import__("os").name == "nt", "POSIX permissions")
class SharedQueueTests(unittest.TestCase):
    def test_shared_mode_sets_group_modes(self) -> None:
        import stat

        with tempfile.TemporaryDirectory() as tmp:
            queue = FileQueue(Path(tmp) / "q", shared=True)
            mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
            self.assertEqual(mode(queue.root), 0o2750)
            self.assertEqual(mode(queue.inbox), 0o2770)
            self.assertEqual(mode(queue.processing), 0o700)
            self.assertEqual(mode(queue.results), 0o2750)
            job_id = queue.enqueue(mode="review", stem="review", prompt="p")
            self.assertEqual(mode(queue.inbox / f"{job_id}.json"), 0o640)
            queue.fail(job_id, "x", stem="review")
            self.assertEqual(mode(queue.result_dir(job_id)), 0o2750)
            self.assertEqual(mode(queue.result_dir(job_id) / "status.json"), 0o640)
            # A second process (the CI side) detects shared mode from the setgid bit.
            self.assertTrue(FileQueue(queue.root).shared)

    def test_private_mode_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"CRITIQUE_QUEUE_SHARED": ""}):
                queue = FileQueue(Path(tmp) / "q")
            self.assertFalse(queue.shared)
            self.assertEqual(queue.inbox.stat().st_mode & 0o7777, 0o700)
