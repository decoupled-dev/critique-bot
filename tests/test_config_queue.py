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
        self._saved_parallel = os.environ.pop("CRITIQUE_MAX_PARALLEL_TABS", None)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("CRITIQUE_QUEUE_DIR", None)
        else:
            os.environ["CRITIQUE_QUEUE_DIR"] = self._saved
        if self._saved_parallel is None:
            os.environ.pop("CRITIQUE_MAX_PARALLEL_TABS", None)
        else:
            os.environ["CRITIQUE_MAX_PARALLEL_TABS"] = self._saved_parallel
        self._tmp.cleanup()

    def test_default_queue_dir_next_to_config(self) -> None:
        path = _write_config(self.folder)
        config = load_config(path)
        self.assertEqual(config.queue_dir, str((self.folder / ".critique-queue").resolve()))
        self.assertEqual(config.min_interval_seconds, 30.0)
        self.assertEqual(config.interval_jitter_seconds, 5.0)
        self.assertEqual(config.max_parallel_tabs, 1)

    def test_max_parallel_tabs(self) -> None:
        path = _write_config(self.folder, {"max_parallel_tabs": 3})
        config = load_config(path)
        self.assertEqual(config.max_parallel_tabs, 3)

    def test_max_parallel_tabs_clamped(self) -> None:
        path = _write_config(self.folder, {"max_parallel_tabs": 99})
        config = load_config(path)
        self.assertEqual(config.max_parallel_tabs, 8)

    def test_env_overrides_max_parallel_tabs(self) -> None:
        path = _write_config(self.folder, {"max_parallel_tabs": 1})
        os.environ["CRITIQUE_MAX_PARALLEL_TABS"] = "4"
        config = load_config(path)
        self.assertEqual(config.max_parallel_tabs, 4)

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


class BackendConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_backend_is_browser(self) -> None:
        path = _write_config(self.folder)
        config = load_config(path)
        self.assertEqual(config.backend, "browser")

    def test_http_backend_is_rejected(self) -> None:
        from critique_bot.config import ConfigError

        path = self.folder / "config.json"
        path.write_text(
            json.dumps({"backend": "ollama", "model": "llama3"}),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("not supported", str(ctx.exception))

    def test_unknown_backend_rejected(self) -> None:
        path = _write_config(self.folder, {"backend": "palm"})
        from critique_bot.config import ConfigError

        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("not supported", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
