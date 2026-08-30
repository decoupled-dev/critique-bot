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


_LLM_ENV = (
    "CRITIQUE_BACKEND",
    "CRITIQUE_BASE_URL",
    "CRITIQUE_API_KEY",
    "CRITIQUE_MODEL",
    "OPENAI_API_KEY",
)


class BackendConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self._saved = {key: os.environ.pop(key, None) for key in _LLM_ENV}

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def test_default_backend_is_browser(self) -> None:
        path = _write_config(self.folder)
        config = load_config(path)
        self.assertEqual(config.backend, "browser")
        self.assertTrue(config.uses_browser)

    def test_ollama_does_not_need_url_or_selectors(self) -> None:
        path = self.folder / "config.json"
        path.write_text(
            json.dumps({"backend": "ollama", "model": "llama3"}),
            encoding="utf-8",
        )
        config = load_config(path)
        self.assertEqual(config.backend, "ollama")
        self.assertFalse(config.uses_browser)
        self.assertEqual(config.model, "llama3")
        self.assertEqual(config.base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(config.url, "")

    def test_ollama_appends_v1_to_host(self) -> None:
        path = _write_config(
            self.folder,
            {
                "backend": "ollama",
                "model": "mistral",
                "base_url": "http://127.0.0.1:11434",
            },
        )
        config = load_config(path)
        self.assertEqual(config.base_url, "http://127.0.0.1:11434/v1")

    def test_ollama_requires_model(self) -> None:
        path = self.folder / "config.json"
        path.write_text(json.dumps({"backend": "ollama"}), encoding="utf-8")
        from critique_bot.config import ConfigError

        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("model", str(ctx.exception))

    def test_openai_requires_api_key(self) -> None:
        path = _write_config(
            self.folder, {"backend": "openai", "model": "gpt-4o"}
        )
        from critique_bot.config import ConfigError

        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("API key", str(ctx.exception))

    def test_openai_reads_env_key(self) -> None:
        path = _write_config(
            self.folder, {"backend": "openai", "model": "gpt-4o"}
        )
        os.environ["OPENAI_API_KEY"] = "sk-test"
        config = load_config(path)
        self.assertEqual(config.backend, "openai")
        self.assertEqual(config.api_key, "sk-test")
        self.assertEqual(config.base_url, "https://api.openai.com/v1")

    def test_openai_compatible_requires_base_url(self) -> None:
        path = self.folder / "config.json"
        path.write_text(
            json.dumps({"backend": "openai-compatible", "model": "local-model"}),
            encoding="utf-8",
        )
        from critique_bot.config import ConfigError

        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("base_url", str(ctx.exception))

    def test_env_overrides_backend(self) -> None:
        path = _write_config(self.folder, {"model": "llama3"})
        os.environ["CRITIQUE_BACKEND"] = "ollama"
        config = load_config(path)
        self.assertEqual(config.backend, "ollama")
        self.assertFalse(config.uses_browser)

    def test_unknown_backend_rejected(self) -> None:
        path = _write_config(self.folder, {"backend": "palm"})
        from critique_bot.config import ConfigError

        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("unknown backend", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
