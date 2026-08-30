from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot.config import (
    ABSOLUTE_MAX_PARALLEL_TABS,
    BACKEND_BROWSER,
    BACKEND_OLLAMA,
    BACKEND_OPENAI,
    BACKEND_OPENAI_COMPAT,
    ConfigError,
    compose_prompt,
    compose_prompt_from_args,
    dedicated_edge_user_data_dir,
    default_prompt_template_path,
    format_attachments,
    load_config,
    system_edge_user_data_dir,
)
from critique_bot.config import (
    _clean,
    _clamped_positive_int,
    _frozen_root,
    _non_negative_float,
    _normalize_base_url,
    _parse_backend,
    _positive_int,
    _resolve_api_key,
    _resolve_queue_dir,
    _resolve_user_data_dir,
)


_ENV = (
    "CRITIQUE_CHAT_URL",
    "CRITIQUE_MODEL",
    "CRITIQUE_STORAGE_STATE",
    "CRITIQUE_USER_DATA_DIR",
    "CRITIQUE_CDP_URL",
    "CRITIQUE_QUEUE_DIR",
    "CRITIQUE_MAX_PARALLEL_TABS",
    "CRITIQUE_BACKEND",
    "CRITIQUE_BASE_URL",
    "CRITIQUE_API_KEY",
    "OPENAI_API_KEY",
)


class EnvIsolated(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self._saved = {key: os.environ.pop(key, None) for key in _ENV}

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _write(self, payload: dict) -> Path:
        path = self.folder / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _browser(self, extra: dict | None = None) -> dict:
        payload = {
            "url": "https://example.invalid/chat",
            "selectors": {
                "prompt_input": "textarea",
                "assistant_messages": ".assistant",
            },
        }
        if extra:
            payload.update(extra)
        return payload


class ComposePromptTests(unittest.TestCase):
    def test_replaces_patch(self) -> None:
        self.assertEqual(compose_prompt("before {patch} after", "DIFF"), "before DIFF after")

    def test_missing_placeholder(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            compose_prompt("no placeholder", "x")
        self.assertIn("{patch}", str(ctx.exception))


class FormatAttachmentsTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(format_attachments([]), "")

    def test_unnamed_joins_raw(self) -> None:
        self.assertEqual(
            format_attachments([("a.txt", "one\n"), ("b.txt", "two")], named=False),
            "one\n\n\ntwo",
        )

    def test_named_sections(self) -> None:
        out = format_attachments([("a.txt", "hello\n"), ("b.txt", "world")])
        self.assertIn("--- file: a.txt ---", out)
        self.assertIn("hello", out)
        self.assertIn("--- file: b.txt ---", out)
        self.assertTrue(out.endswith("\n"))


class ComposePromptFromArgsTests(unittest.TestCase):
    def test_empty_prompt_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            compose_prompt_from_args("   ")
        self.assertIn("empty", str(ctx.exception))

    def test_no_attachments(self) -> None:
        self.assertEqual(compose_prompt_from_args("hello"), "hello")

    def test_appends_named_files(self) -> None:
        out = compose_prompt_from_args("Look:", [("a.py", "x = 1")])
        self.assertTrue(out.startswith("Look:"))
        self.assertIn("--- file: a.py ---", out)
        self.assertIn("x = 1", out)

    def test_files_placeholder(self) -> None:
        out = compose_prompt_from_args("Files:\n{files}", [("a.py", "code")])
        self.assertIn("--- file: a.py ---", out)
        self.assertNotIn("{files}", out)

    def test_patch_placeholder_single_raw(self) -> None:
        out = compose_prompt_from_args("Patch:\n{patch}", [("diff.patch", "diff --git")])
        self.assertIn("diff --git", out)
        self.assertNotIn("{patch}", out)

    def test_patch_placeholder_multiple_named(self) -> None:
        out = compose_prompt_from_args(
            "{patch}",
            [("a.diff", "one"), ("b.diff", "two")],
        )
        self.assertIn("--- file: a.diff ---", out)
        self.assertIn("--- file: b.diff ---", out)

    def test_placeholder_without_files(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            compose_prompt_from_args("see {files}")
        self.assertIn("no files", str(ctx.exception))
        with self.assertRaises(ConfigError):
            compose_prompt_from_args("see {patch}")

    def test_both_placeholders(self) -> None:
        out = compose_prompt_from_args(
            "files={files}\npatch={patch}",
            [("only.txt", "BODY")],
        )
        self.assertIn("BODY", out)
        self.assertNotIn("{files}", out)
        self.assertNotIn("{patch}", out)


class ParseHelpersTests(unittest.TestCase):
    def test_clean(self) -> None:
        self.assertEqual(_clean(None), "")
        self.assertEqual(_clean("  x  "), "x")
        self.assertEqual(_clean(3), "3")

    def test_positive_int(self) -> None:
        self.assertEqual(_positive_int("n", None, 7), 7)
        self.assertEqual(_positive_int("n", "", 7), 7)
        self.assertEqual(_positive_int("n", "9", 7), 9)
        with self.assertRaises(ConfigError):
            _positive_int("n", "nope", 1)
        with self.assertRaises(ConfigError):
            _positive_int("n", 0, 1)
        with self.assertRaises(ConfigError):
            _positive_int("n", -2, 1)

    def test_clamped_positive_int(self) -> None:
        self.assertEqual(_clamped_positive_int("n", 3, 1, 8), 3)
        self.assertEqual(_clamped_positive_int("n", 99, 1, 8), 8)

    def test_non_negative_float(self) -> None:
        self.assertEqual(_non_negative_float("n", None, 1.5), 1.5)
        self.assertEqual(_non_negative_float("n", "", 1.5), 1.5)
        self.assertEqual(_non_negative_float("n", "0", 1.5), 0.0)
        with self.assertRaises(ConfigError):
            _non_negative_float("n", "x", 1.0)
        with self.assertRaises(ConfigError):
            _non_negative_float("n", -0.1, 1.0)

    def test_parse_backend_aliases(self) -> None:
        self.assertEqual(_parse_backend(""), BACKEND_BROWSER)
        self.assertEqual(_parse_backend("WEB"), BACKEND_BROWSER)
        self.assertEqual(_parse_backend("playwright"), BACKEND_BROWSER)
        self.assertEqual(_parse_backend("local"), BACKEND_OLLAMA)
        self.assertEqual(_parse_backend("openai_compatible"), BACKEND_OPENAI_COMPAT)
        self.assertEqual(_parse_backend("compatible"), BACKEND_OPENAI_COMPAT)
        with self.assertRaises(ConfigError) as ctx:
            _parse_backend("palm")
        self.assertIn("unknown backend", str(ctx.exception))

    def test_normalize_base_url(self) -> None:
        self.assertEqual(
            _normalize_base_url(BACKEND_OLLAMA, ""),
            "http://127.0.0.1:11434/v1",
        )
        self.assertEqual(
            _normalize_base_url(BACKEND_OPENAI, ""),
            "https://api.openai.com/v1",
        )
        self.assertEqual(_normalize_base_url(BACKEND_OPENAI_COMPAT, ""), "")
        self.assertEqual(
            _normalize_base_url(BACKEND_OLLAMA, "http://h:11434/v1/"),
            "http://h:11434/v1",
        )
        self.assertEqual(
            _normalize_base_url(BACKEND_OLLAMA, "http://h:11434/api"),
            "http://h:11434/v1",
        )
        self.assertEqual(
            _normalize_base_url(BACKEND_OLLAMA, "http://h:11434"),
            "http://h:11434/v1",
        )
        self.assertEqual(
            _normalize_base_url(BACKEND_OPENAI, "https://proxy.example/v1/"),
            "https://proxy.example/v1",
        )

    def test_resolve_api_key_order(self) -> None:
        os.environ.pop("CRITIQUE_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            self.assertEqual(_resolve_api_key({"api_key": " from-json "}, BACKEND_OLLAMA), "from-json")
            os.environ["CUSTOM_KEY"] = "from-named"
            self.assertEqual(
                _resolve_api_key({"api_key_env": "CUSTOM_KEY", "api_key": "json"}, BACKEND_OLLAMA),
                "from-named",
            )
            os.environ["CRITIQUE_API_KEY"] = "from-critique"
            self.assertEqual(
                _resolve_api_key({"api_key_env": "CUSTOM_KEY"}, BACKEND_OLLAMA),
                "from-critique",
            )
        finally:
            os.environ.pop("CUSTOM_KEY", None)
            os.environ.pop("CRITIQUE_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)

    def test_resolve_queue_dir(self) -> None:
        config_path = Path("/tmp/fake-config.json")
        default = _resolve_queue_dir("", config_path)
        self.assertTrue(default.endswith(".critique-queue"))
        relative = _resolve_queue_dir("jobs", Path("/tmp/cfg/config.json"))
        self.assertTrue(relative.endswith("jobs") or "jobs" in relative)

    def test_resolve_user_data_dir(self) -> None:
        system = _resolve_user_data_dir("system")
        self.assertEqual(system, str(system_edge_user_data_dir()))
        defaulted = _resolve_user_data_dir("DEFAULT")
        self.assertEqual(defaulted, str(system_edge_user_data_dir()))
        custom = _resolve_user_data_dir(".")
        self.assertTrue(Path(custom).is_absolute())

    def test_frozen_root_none_in_tests(self) -> None:
        self.assertIsNone(_frozen_root())

    def test_dedicated_edge_profile(self) -> None:
        dedicated = dedicated_edge_user_data_dir()
        system = system_edge_user_data_dir()
        self.assertEqual(dedicated.parent, system.parent)
        self.assertTrue(str(dedicated).endswith("-critique-bot"))


class LoadConfigErrorTests(EnvIsolated):
    def test_missing_file(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.folder / "nope.json")
        self.assertIn("not found", str(ctx.exception))

    def test_invalid_json(self) -> None:
        path = self.folder / "config.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_root_must_be_object(self) -> None:
        path = self.folder / "config.json"
        path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("JSON object", str(ctx.exception))

    def test_selectors_must_be_object(self) -> None:
        path = self._write({"url": "https://x", "selectors": []})
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("selectors", str(ctx.exception))

    def test_placeholder_url(self) -> None:
        path = self._write(self._browser({"url": "https://YOUR_CHAT_UI/chat"}))
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("placeholder", str(ctx.exception))

    def test_placeholder_model(self) -> None:
        path = self._write(self._browser({"model": "YOUR_MODEL_NAME"}))
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("placeholder", str(ctx.exception))

    def test_missing_prompt_input(self) -> None:
        path = self._write(
            {
                "url": "https://example.invalid/chat",
                "selectors": {"assistant_messages": ".a"},
            }
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("prompt_input", str(ctx.exception))

    def test_missing_assistant_messages(self) -> None:
        path = self._write(
            {
                "url": "https://example.invalid/chat",
                "selectors": {"prompt_input": "textarea"},
            }
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("assistant_messages", str(ctx.exception))

    def test_missing_url(self) -> None:
        path = self._write(
            {"selectors": {"prompt_input": "t", "assistant_messages": ".a"}}
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("url is required", str(ctx.exception))

    def test_invalid_timeout(self) -> None:
        path = self._write(self._browser({"timeout_ms": "fast"}))
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_storage_state_missing(self) -> None:
        path = self._write(self._browser({"storage_state": "no-such.json"}))
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("storage_state", str(ctx.exception))


class LoadConfigSuccessTests(EnvIsolated):
    def test_selectors_and_top_level_identifier(self) -> None:
        path = self._write(
            self._browser(
                {
                    "model_dropdown_identifier": "Model picker",
                    "selectors": {
                        "prompt_input": " textarea ",
                        "assistant_messages": ".msg",
                        "model_dropdown": "#dd",
                        "model_option": ".opt",
                        "send_button": "button.send",
                    },
                    "timeout_ms": 12_000,
                    "idle_ms": 2_000,
                    "model": "GPT-4",
                    "cdp_url": "http://127.0.0.1:9222",
                }
            )
        )
        config = load_config(path)
        self.assertEqual(config.selectors.prompt_input, "textarea")
        self.assertEqual(config.selectors.model_dropdown, "#dd")
        self.assertEqual(config.selectors.model_dropdown_identifier, "Model picker")
        self.assertEqual(config.selectors.model_option, ".opt")
        self.assertEqual(config.selectors.send_button, "button.send")
        self.assertEqual(config.timeout_ms, 12_000)
        self.assertEqual(config.idle_ms, 2_000)
        self.assertEqual(config.model, "GPT-4")
        self.assertEqual(config.cdp_url, "http://127.0.0.1:9222")
        self.assertTrue(config.uses_browser)
        self.assertEqual(config.input_limits.max_files, 80)

    def test_model_override_beats_env_and_file(self) -> None:
        path = self._write(self._browser({"model": "file-model"}))
        os.environ["CRITIQUE_MODEL"] = "env-model"
        config = load_config(path, model_override="cli-model")
        self.assertEqual(config.model, "cli-model")

    def test_env_overrides_url_and_cdp(self) -> None:
        path = self._write(self._browser({"cdp_url": "http://old:1"}))
        os.environ["CRITIQUE_CHAT_URL"] = "https://from-env/chat"
        os.environ["CRITIQUE_CDP_URL"] = "http://127.0.0.1:9"
        config = load_config(path)
        self.assertEqual(config.url, "https://from-env/chat")
        self.assertEqual(config.cdp_url, "http://127.0.0.1:9")

    def test_cdp_override_argument(self) -> None:
        path = self._write(self._browser())
        config = load_config(path, cdp_url_override="http://127.0.0.1:9333")
        self.assertEqual(config.cdp_url, "http://127.0.0.1:9333")

    def test_storage_state_file(self) -> None:
        state = self.folder / "state.json"
        state.write_text("{}", encoding="utf-8")
        path = self._write(self._browser({"storage_state": str(state)}))
        config = load_config(path)
        self.assertEqual(config.storage_state, str(state))

    def test_max_prompt_chars_clamped(self) -> None:
        path = self._write(self._browser({"max_prompt_chars": 9_999_999}))
        config = load_config(path)
        self.assertEqual(config.max_prompt_chars, 400_000)

    def test_openai_compatible(self) -> None:
        path = self._write(
            {
                "backend": "openai-compatible",
                "model": "local",
                "base_url": "http://127.0.0.1:8000/v1",
            }
        )
        config = load_config(path)
        self.assertEqual(config.backend, BACKEND_OPENAI_COMPAT)
        self.assertEqual(config.base_url, "http://127.0.0.1:8000/v1")
        self.assertFalse(config.uses_browser)

    def test_api_key_in_json(self) -> None:
        path = self._write(
            {"backend": "openai", "model": "gpt-4o", "api_key": "sk-json"}
        )
        config = load_config(path)
        self.assertEqual(config.api_key, "sk-json")

    def test_critique_api_key_env(self) -> None:
        path = self._write({"backend": "openai", "model": "gpt-4o"})
        os.environ["CRITIQUE_API_KEY"] = "sk-env"
        config = load_config(path)
        self.assertEqual(config.api_key, "sk-env")

    def test_user_data_dir_env(self) -> None:
        path = self._write(self._browser())
        custom = self.folder / "profile"
        custom.mkdir()
        os.environ["CRITIQUE_USER_DATA_DIR"] = str(custom)
        config = load_config(path)
        self.assertEqual(config.user_data_dir, str(custom.resolve()))

    def test_absolute_max_parallel_constant(self) -> None:
        self.assertEqual(ABSOLUTE_MAX_PARALLEL_TABS, 8)


class DefaultTemplateTests(unittest.TestCase):
    def test_finds_bundled_or_cwd_template(self) -> None:
        path = default_prompt_template_path()
        self.assertTrue(path.is_file())
        self.assertIn("{patch}", path.read_text(encoding="utf-8"))

    def test_missing_template_error(self) -> None:
        with patch("critique_bot.config.Path.is_file", return_value=False):
            with self.assertRaises(ConfigError) as ctx:
                default_prompt_template_path()
        self.assertIn("template not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
