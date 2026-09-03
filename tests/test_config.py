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
    _positive_int,
    _reject_http_backend,
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
    "CRITIQUE_GITLAB_URL",
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

    def test_replaces_mr_context_placeholder(self) -> None:
        out = compose_prompt("CTX {mr_context}\n{patch}", "DIFF", "Title: Hello")
        self.assertIn("Title: Hello", out)
        self.assertIn("DIFF", out)
        self.assertNotIn("{mr_context}", out)

    def test_injects_mr_context_without_placeholder(self) -> None:
        out = compose_prompt("REVIEW:\n{patch}", "DIFF", "Tickets: AAOS-1")
        self.assertTrue(out.startswith("SYSTEM"))
        self.assertIn("Tickets: AAOS-1", out)
        self.assertIn("DIFF", out)

    def test_empty_mr_context_placeholder(self) -> None:
        out = compose_prompt("{mr_context}\n{patch}", "DIFF", "")
        self.assertIn("No GitLab merge-request metadata", out)

    def test_replaces_files_placeholder(self) -> None:
        out = compose_prompt("FILES\n{files}\nPATCH\n{patch}", "DIFF", "", "class Foo {}")
        self.assertIn("class Foo {}", out)
        self.assertIn("DIFF", out)
        self.assertNotIn("{files}", out)

    def test_empty_files_placeholder(self) -> None:
        out = compose_prompt("{files}\n{patch}", "DIFF", "", "")
        self.assertIn("No full-file context was attached.", out)

    def test_injects_files_without_placeholder(self) -> None:
        out = compose_prompt("REVIEW:\n{patch}", "DIFF", "", "class Foo {}")
        self.assertIn("FILE CONTEXT", out)
        self.assertIn("class Foo {}", out)
        self.assertIn("DIFF", out)

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

    def test_reject_http_backends(self) -> None:
        _reject_http_backend("")
        _reject_http_backend(None)
        _reject_http_backend("browser")
        _reject_http_backend("WEB")
        _reject_http_backend("playwright")
        for value in ("ollama", "openai", "openai-compatible", "palm"):
            with self.assertRaises(ConfigError) as ctx:
                _reject_http_backend(value)
            self.assertIn("not supported", str(ctx.exception))

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
        self.assertNotEqual(dedicated.resolve(), system.resolve())
        # Chromium 136+ treats a path that string-prefix-matches the default
        # User Data dir as the daily profile (HTTP 403). A sibling named
        # "User Data-critique-bot" fails that check on Windows.
        self.assertFalse(
            str(dedicated.resolve()).startswith(str(system.resolve()))
        )
        self.assertIn("critique-bot", str(dedicated))
        self.assertIn("msedge-user-data", str(dedicated))


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

    def test_http_backend_rejected(self) -> None:
        path = self._write({"backend": "ollama", "model": "llama3"})
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("not supported", str(ctx.exception))


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
        self.assertEqual(config.backend, BACKEND_BROWSER)
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

    def test_unknown_patch_only_key_is_ignored(self) -> None:
        path = self._write(self._browser({"patch_only_file_count": 25}))
        config = load_config(path)
        self.assertFalse(hasattr(config, "patch_only_file_count"))
        self.assertFalse(hasattr(config.input_limits, "patch_only_file_count"))

    def test_user_data_dir_env(self) -> None:
        path = self._write(self._browser())
        custom = self.folder / "profile"
        custom.mkdir()
        os.environ["CRITIQUE_USER_DATA_DIR"] = str(custom)
        config = load_config(path)
        self.assertEqual(config.user_data_dir, str(custom.resolve()))

    def test_gitlab_nested_and_flat_keys(self) -> None:
        path = self._write(
            self._browser(
                {
                    "gitlab": {
                        "base_url": "https://gitlab.example.com/",
                        "project_id": "ignored",
                        "mr_iid": "ignored",
                    }
                }
            )
        )
        config = load_config(path)
        self.assertEqual(config.gitlab.base_url, "https://gitlab.example.com/")
        self.assertFalse(hasattr(config.gitlab, "project_id"))

    def test_gitlab_base_url_env_and_flat_alias(self) -> None:
        path = self._write(self._browser({"gitlab_base_url": "https://from-file.example"}))
        os.environ["CRITIQUE_GITLAB_URL"] = "https://from-env.example"
        config = load_config(path)
        self.assertEqual(config.gitlab.base_url, "https://from-env.example")

    def test_absolute_max_parallel_constant(self) -> None:
        self.assertEqual(ABSOLUTE_MAX_PARALLEL_TABS, 8)


class DefaultTemplateTests(unittest.TestCase):
    def test_finds_bundled_or_cwd_template(self) -> None:
        path = default_prompt_template_path()
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("{patch}", text)
        self.assertIn("{files}", text)
        self.assertIn("{mr_context}", text)
        self.assertIn("AAOS", text)
        self.assertIn("SYSTEM", text)
        self.assertIn("ROLE", text)
        self.assertIn("FEW-SHOT", text)
        self.assertIn("privapp-permissions", text)
        self.assertIn("No actionable findings.", text)
        self.assertIn("**Risk: Safe**", text)
        self.assertIn('"risk"', text)

    def test_missing_template_error(self) -> None:
        with patch("critique_bot.config.Path.is_file", return_value=False):
            with self.assertRaises(ConfigError) as ctx:
                default_prompt_template_path()
        self.assertIn("template not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
