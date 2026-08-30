from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot.config import load_config
from critique_bot.queue import FileQueue, JobStatus, RateLimiter
from critique_bot.worker import _parallel_loop


class ParallelWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        config_path = self.folder / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "url": "https://example.invalid/chat",
                    "selectors": {
                        "prompt_input": "textarea",
                        "assistant_messages": ".assistant",
                    },
                    "queue_dir": str(self.folder / "queue"),
                    "max_parallel_tabs": 3,
                    "min_interval_seconds": 0,
                    "interval_jitter_seconds": 0,
                }
            ),
            encoding="utf-8",
        )
        self.config = load_config(config_path)
        self.queue = FileQueue(Path(self.config.queue_dir))
        for index in range(3):
            self.queue.enqueue(
                mode="review",
                stem="review",
                prompt=f"patch {index}",
                label=f"mr{index}",
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_three_jobs_overlap(self) -> None:
        barrier = threading.Barrier(3)
        finished: list[str] = []
        overlap = {"ok": False}

        def fake_run(_provider, _config, queue, job, isolated=False) -> None:
            del isolated
            try:
                barrier.wait(timeout=2)
                overlap["ok"] = True
            except threading.BrokenBarrierError as exc:
                raise AssertionError("jobs did not overlap") from exc
            queue.finish(
                job.id,
                JobStatus(
                    id=job.id,
                    ok=True,
                    error=None,
                    stem=job.stem,
                    started_at=None,
                    finished_at=None,
                    elapsed_seconds=0.1,
                    label=job.label,
                    meta=job.meta,
                ),
            )
            finished.append(job.id)

        class _StubProvider:
            can_parallelize = True

        with patch("critique_bot.worker._run_job", fake_run):
            _parallel_loop(
                _StubProvider(),
                self.config,
                self.queue,
                RateLimiter(0, 0),
                lambda: len(finished) >= 3,
                parallel=3,
            )
        self.assertTrue(overlap["ok"])
        self.assertEqual(len(finished), 3)
        self.assertIsNone(self.queue.claim())


if __name__ == "__main__":
    unittest.main()
