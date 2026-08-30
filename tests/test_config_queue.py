from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from critique_bot.config import load_config


def _write_config(folder: Path, extra: dict | None = None) -> Path:
    payload = {
        "url": "https://example.invalid/chat",
        "selectors": {
            "prompt_input": "textarea",
            "assistant_messages": ".assistant",
        },
    }
    if extra:
        payload.update(extra)
    path = folder / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class QueueDirConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self._saved = os.environ.pop("CRITIQUE_QUEUE_DIR", None)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("CRITIQUE_QUEUE_DIR", None)
        else:
            os.environ["CRITIQUE_QUEUE_DIR"] = self._saved
        self._tmp.cleanup()

    def test_default_queue_dir_next_to_config(self) -> None:
        path = _write_config(self.folder)
        config = load_config(path)
        self.assertEqual(config.queue_dir, str((self.folder / ".critique-queue").resolve()))
        self.assertEqual(config.min_interval_seconds, 30.0)
        self.assertEqual(config.interval_jitter_seconds, 5.0)

    def test_relative_queue_dir(self) -> None:
        path = _write_config(self.folder, {"queue_dir": "jobs"})
        config = load_config(path)
        self.assertEqual(config.queue_dir, str((self.folder / "jobs").resolve()))

    def test_env_overrides_queue_dir(self) -> None:
        path = _write_config(self.folder, {"queue_dir": "jobs"})
        other = self.folder / "from-env"
        os.environ["CRITIQUE_QUEUE_DIR"] = str(other)
        config = load_config(path)
        self.assertEqual(config.queue_dir, str(other.resolve()))

    def test_interval_zero_allowed(self) -> None:
        path = _write_config(
            self.folder,
            {"min_interval_seconds": 0, "interval_jitter_seconds": 0},
        )
        config = load_config(path)
        self.assertEqual(config.min_interval_seconds, 0.0)
        self.assertEqual(config.interval_jitter_seconds, 0.0)


if __name__ == "__main__":
    unittest.main()
