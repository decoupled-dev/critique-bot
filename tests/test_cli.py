from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from critique_bot.cli import (
    MODE_CHAT,
    MODE_GENERAL,
    MODE_REVIEW,
    _build_prompt,
    _build_prompt_payload,
    _chat_file_turn,
    _ci_meta,
    _collect_attachments,
    _copy_job_results,
    _format_chat_transcript,
    _read_chat_message,
    _read_text_file,
    _resolve_mode,
    build_parser,
    main,
)
from critique_bot.config import ConfigError
from critique_bot.patch import InputLimits


class ResolveModeTests(unittest.TestCase):
    def _ns(self, **kwargs) -> argparse.Namespace:
        values = {
            "mode": None,
            "prompt": None,
            "prompt_file": None,
            "prompt_template": None,
        }
        values.update(kwargs)
        return argparse.Namespace(**values)

    def test_default_review(self) -> None:
        self.assertEqual(_resolve_mode(self._ns()), MODE_REVIEW)

    def test_prompt_selects_general(self) -> None:
        self.assertEqual(_resolve_mode(self._ns(prompt="hi")), MODE_GENERAL)

    def test_review_rejects_prompt(self) -> None:
        with self.assertRaises(ConfigError):
            _resolve_mode(self._ns(mode=MODE_REVIEW, prompt="hi"))

    def test_general_requires_prompt(self) -> None:
        with self.assertRaises(ConfigError):
            _resolve_mode(self._ns(mode=MODE_GENERAL))

    def test_prompt_template_only_review(self) -> None:
        with self.assertRaises(ConfigError):
            _resolve_mode(self._ns(mode=MODE_GENERAL, prompt="x", prompt_template="t.txt"))
        with self.assertRaises(ConfigError):
            _resolve_mode(self._ns(mode=MODE_CHAT, prompt_template="t.txt"))

    def test_chat_ok(self) -> None:
        self.assertEqual(_resolve_mode(self._ns(mode=MODE_CHAT)), MODE_CHAT)


class ChatHelperTests(unittest.TestCase):
    def test_format_transcript(self) -> None:
        text = _format_chat_transcript(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ]
        )
        self.assertIn("# Chat", text)
        self.assertIn("## You", text)
        self.assertIn("hi", text)
        self.assertIn("## Assistant", text)
        self.assertTrue(text.endswith("\n"))

    def test_read_message_simple(self) -> None:
        with patch("builtins.input", side_effect=["hello"]):
            self.assertEqual(_read_chat_message(), "hello")

    def test_read_message_skips_blank_then_quit(self) -> None:
        with patch("builtins.input", side_effect=["", "  ", "quit"]):
            self.assertIsNone(_read_chat_message())

    def test_read_message_continuation(self) -> None:
        with patch("builtins.input", side_effect=["hello\\", "world"]):
            self.assertEqual(_read_chat_message(), "hello\nworld")

    def test_read_message_eof_empty(self) -> None:
        with patch("builtins.input", side_effect=EOFError):
            with redirect_stdout(io.StringIO()):
                self.assertIsNone(_read_chat_message())

    def test_read_message_eof_with_chunks(self) -> None:
        with patch("builtins.input", side_effect=["one\\", EOFError]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(_read_chat_message(), "one")

    def test_read_message_keyboard_interrupt(self) -> None:
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with redirect_stdout(io.StringIO()):
                self.assertIsNone(_read_chat_message())

    def test_chat_file_turn_needs_path(self) -> None:
        with self.assertRaises(ConfigError):
            _chat_file_turn("/file", InputLimits())


class CiMetaAndCopyTests(unittest.TestCase):
    def test_ci_meta_filters_empty(self) -> None:
        saved = {k: os.environ.pop(k, None) for k in ("CI_JOB_ID", "CI_PIPELINE_ID")}
        try:
            os.environ["CI_JOB_ID"] = "99"
            os.environ.pop("CI_PIPELINE_ID", None)
            meta = _ci_meta()
            self.assertEqual(meta["CI_JOB_ID"], "99")
            self.assertNotIn("CI_PIPELINE_ID", meta)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_copy_job_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            dest = Path(tmp) / "dest"
            src.mkdir()
            (src / "review.md").write_text("ok", encoding="utf-8")
            (src / "status.json").write_text("{}", encoding="utf-8")
            (src / "ignored.txt").write_text("no", encoding="utf-8")
            _copy_job_results(src, dest, "review")
            self.assertTrue((dest / "review.md").is_file())
            self.assertTrue((dest / "status.json").is_file())
            self.assertFalse((dest / "ignored.txt").exists())


class ParserTests(unittest.TestCase):
    def test_requires_config(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_parses_common_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--config",
                "c.json",
                "--mode",
                "general",
                "--prompt",
                "hi",
                "--file",
                "a.py",
                "b.py",
                "--headed",
                "--logs",
                "--label",
                "mr1",
            ]
        )
        self.assertEqual(args.config, "c.json")
        self.assertEqual(args.mode, "general")
        self.assertEqual(args.prompt, "hi")
        self.assertEqual(args.files, ["a.py"])
        self.assertEqual(args.paths, ["b.py"])
        self.assertTrue(args.headed)
        self.assertTrue(args.logs)
        self.assertEqual(args.label, "mr1")


class BuildPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.limits = InputLimits(max_prompt_chars=20_000, max_file_chars=10_000)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_review_wraps_patch(self) -> None:
        patch = self.folder / "d.patch"
        patch.write_text("diff --git a/a b/a\n+hi\n", encoding="utf-8")
        template = self.folder / "t.txt"
        template.write_text("REVIEW:\n{patch}\n", encoding="utf-8")
        args = argparse.Namespace(
            patch_file=str(patch),
            files=None,
            paths=[],
            prompt=None,
            prompt_file=None,
            prompt_template=str(template),
        )
        prompt = _build_prompt(args, MODE_REVIEW, self.limits)
        self.assertIn("REVIEW:", prompt)
        self.assertIn("+hi", prompt)

    def test_review_injects_gitlab_mr_context(self) -> None:
        from critique_bot.config import BotConfig, GitLabConfig, Selectors
        from critique_bot.gitlab import MrContext

        patch_path = self.folder / "d.patch"
        patch_path.write_text("diff --git a/a b/a\n+hi\n", encoding="utf-8")
        template = self.folder / "t.txt"
        template.write_text("CTX {mr_context}\n{patch}\n", encoding="utf-8")
        args = argparse.Namespace(
            patch_file=str(patch_path),
            files=None,
            paths=[],
            prompt=None,
            prompt_file=None,
            prompt_template=str(template),
        )
        config = BotConfig(
            url="https://example.invalid/chat",
            selectors=Selectors(prompt_input="textarea", assistant_messages=".a"),
            gitlab=GitLabConfig(base_url="https://gitlab.example.com"),
        )
        ctx = MrContext(
            title="AAOS-1 HVAC",
            tickets=("AAOS-1",),
            commits=("abc Fix leak",),
        )
        with patch("critique_bot.cli._load_gitlab_mr_context", return_value=ctx):
            prompt = _build_prompt(args, MODE_REVIEW, self.limits, config)
        self.assertIn("AAOS-1 HVAC", prompt)
        self.assertIn("AAOS-1", prompt)
        self.assertIn("+hi", prompt)

    def test_review_attaches_changed_files_outside_diff_fence(self) -> None:
        src = self.folder / "Foo.java"
        src.write_text("class Foo {\n    void bar() {}\n}\n", encoding="utf-8")
        patch = self.folder / "d.patch"
        patch.write_text(
            "diff --git a/Foo.java b/Foo.java\n"
            "--- a/Foo.java\n"
            "+++ b/Foo.java\n"
            "@@ -1,2 +1,3 @@\n"
            " class Foo {\n"
            "+    void bar() {}\n"
            " }\n",
            encoding="utf-8",
        )
        template = self.folder / "t.txt"
        template.write_text("FILES\n{files}\nPATCH\n```diff\n{patch}\n```\n", encoding="utf-8")
        args = argparse.Namespace(
            patch_file=str(patch),
            files=None,
            paths=[],
            prompt=None,
            prompt_file=None,
            prompt_template=str(template),
            repo_dir=str(self.folder),
            write_patch=None,
        )
        prompt = _build_prompt(args, MODE_REVIEW, self.limits)
        self.assertIn("class Foo {", prompt)
        self.assertIn("--- file: Foo.java ---", prompt)
        self.assertIn("```diff", prompt)
        files_part, patch_part = prompt.split("```diff", 1)
        self.assertIn("class Foo {", files_part)
        self.assertIn("+    void bar() {}", patch_part)
        self.assertNotIn("--- file: Foo.java ---", patch_part)

    def test_review_overflow_stages_files_out_of_the_prompt(self) -> None:
        src = self.folder / "Foo.java"
        src.write_text("class Foo {\n" + ("    int n;\n" * 80) + "}\n", encoding="utf-8")
        patch = self.folder / "d.patch"
        patch.write_text(
            "diff --git a/Foo.java b/Foo.java\n"
            "--- a/Foo.java\n"
            "+++ b/Foo.java\n"
            "@@ -1 +1,2 @@\n"
            " class Foo {\n"
            "+    int n;\n"
            " }\n",
            encoding="utf-8",
        )
        template = self.folder / "t.txt"
        template.write_text("FILES\n{files}\nPATCH\n```diff\n{patch}\n```\n", encoding="utf-8")
        args = argparse.Namespace(
            patch_file=str(patch),
            files=None,
            paths=[],
            prompt=None,
            prompt_file=None,
            prompt_template=str(template),
            repo_dir=str(self.folder),
            write_patch=None,
        )
        limits = InputLimits(max_prompt_chars=500, max_file_chars=8_000)
        payload = _build_prompt_payload(args, MODE_REVIEW, limits)
        self.assertIn("Foo.java", payload.files)
        self.assertGreater(payload.files["Foo.java"].count("int n;"), 10)
        self.assertNotIn("--- file: Foo.java ---", payload.prompt)
        self.assertIn("already sent", payload.prompt)
        self.assertIn("```diff", payload.prompt)

    def test_general_appends_files(self) -> None:
        src = self.folder / "a.py"
        src.write_text("x = 1\n", encoding="utf-8")
        args = argparse.Namespace(
            patch_file=None,
            files=[str(src)],
            paths=[],
            prompt="Explain this",
            prompt_file=None,
            prompt_template=None,
        )
        prompt = _build_prompt(args, MODE_GENERAL, self.limits)
        self.assertIn("Explain this", prompt)
        self.assertIn("x = 1", prompt)

    def test_chat_empty_without_input(self) -> None:
        args = argparse.Namespace(
            patch_file=None,
            files=None,
            paths=[],
            prompt=None,
            prompt_file=None,
            prompt_template=None,
        )
        self.assertEqual(_build_prompt(args, MODE_CHAT, self.limits), "")

    def test_prompt_file(self) -> None:
        pf = self.folder / "p.txt"
        pf.write_text("Do the thing", encoding="utf-8")
        args = argparse.Namespace(
            patch_file=None,
            files=None,
            paths=[],
            prompt=None,
            prompt_file=str(pf),
            prompt_template=None,
        )
        prompt = _build_prompt(args, MODE_GENERAL, self.limits)
        self.assertEqual(prompt, "Do the thing")

    def test_chat_file_turn(self) -> None:
        path = self.folder / "notes.txt"
        path.write_text("alpha", encoding="utf-8")
        out = _chat_file_turn(f"/file {path} what is this", self.limits)
        self.assertIn("what is this", out)
        self.assertIn("alpha", out)

    def test_read_text_file_missing(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _read_text_file(self.folder / "nope.txt", self.limits)
        self.assertIn("not found", str(ctx.exception))

    def test_read_text_file_empty(self) -> None:
        path = self.folder / "empty.txt"
        path.write_text("   \n", encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            _read_text_file(path, self.limits)
        self.assertIn("empty", str(ctx.exception))

    def test_read_text_file_rejects_binary_when_disallowed(self) -> None:
        path = self.folder / "x.png"
        path.write_bytes(b"xxxx")
        with self.assertRaises(ConfigError) as ctx:
            _read_text_file(path, self.limits, allow_binary=False)
        self.assertIn("binary", str(ctx.exception))

    def test_collect_attachments_open_cap(self) -> None:
        files = []
        for i in range(3):
            p = self.folder / f"f{i}.txt"
            p.write_text(f"c{i}", encoding="utf-8")
            files.append(str(p))
        loaded = _collect_attachments(None, files, InputLimits(max_files=1), allow_stdin=False)
        self.assertEqual(len(loaded), 3)


class MainDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.config = self.folder / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "url": "https://example.invalid/chat",
                    "selectors": {
                        "prompt_input": "textarea",
                        "assistant_messages": ".assistant",
                    },
                    "queue_dir": str(self.folder / "queue"),
                }
            ),
            encoding="utf-8",
        )
        self.patch = self.folder / "diff.patch"
        self.patch.write_text("diff --git a/a b/a\n+hello\n", encoding="utf-8")
        self.template = self.folder / "review.txt"
        self.template.write_text("P:{patch}", encoding="utf-8")
        self.out = self.folder / "out"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_one_shot_review_writes_files(self) -> None:
        class Session:
            page = None

            def send(self, prompt: str) -> str:
                self.prompt = prompt
                return "LGTM"

            def __enter__(self) -> Session:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        class Provider:
            def session(self, **kwargs: object) -> Session:
                return Session()

            def __enter__(self) -> Provider:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        buf = io.StringIO()
        with patch("critique_bot.provider.open_provider", return_value=Provider()):
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "--config",
                        str(self.config),
                        "--patch-file",
                        str(self.patch),
                        "--prompt-template",
                        str(self.template),
                        "--output-dir",
                        str(self.out),
                    ]
                )
        self.assertEqual(code, 0)
        self.assertIn("LGTM", buf.getvalue())
        self.assertTrue((self.out / "review.md").is_file())
        payload = json.loads((self.out / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["mode"], "review")
        self.assertEqual(payload["response"], "LGTM")

    def test_empty_reply_is_error(self) -> None:
        class Session:
            page = None

            def send(self, prompt: str) -> str:
                return "   "

            def __enter__(self) -> Session:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        class Provider:
            def session(self, **kwargs: object) -> Session:
                return Session()

            def __enter__(self) -> Provider:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        err = io.StringIO()
        with patch("critique_bot.provider.open_provider", return_value=Provider()):
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = main(
                    [
                        "--config",
                        str(self.config),
                        "--mode",
                        "general",
                        "--prompt",
                        "hi",
                        "--output-dir",
                        str(self.out),
                    ]
                )
        self.assertEqual(code, 1)
        self.assertIn("empty", err.getvalue().lower())

    def test_chat_error_returns_one(self) -> None:
        from critique_bot.chat_client import ChatError

        class Provider:
            def __enter__(self) -> Provider:
                raise ChatError("down")

            def __exit__(self, *exc: object) -> None:
                return None

        err = io.StringIO()
        with patch("critique_bot.provider.open_provider", return_value=Provider()):
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = main(
                    [
                        "--config",
                        str(self.config),
                        "--mode",
                        "general",
                        "--prompt",
                        "hi",
                        "--output-dir",
                        str(self.out),
                    ]
                )
        self.assertEqual(code, 1)
        self.assertIn("down", err.getvalue())

    def test_config_error_on_missing_file(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--config", str(self.folder / "missing.json"), "--mode", "general", "--prompt", "x"])
        self.assertEqual(code, 1)
        self.assertIn("error:", err.getvalue())

    def test_gitlab_post_missing_review(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(
                [
                    "gitlab-post",
                    "--review-file",
                    str(self.folder / "no.md"),
                    "--api-url",
                    "https://gitlab.example/api/v4",
                    "--project-id",
                    "1",
                    "--mr-iid",
                    "2",
                ]
            )
        self.assertEqual(code, 1)

    def test_worker_bad_config(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["worker", "--config", str(self.folder / "nope.json")])
        self.assertEqual(code, 1)
        self.assertIn("error:", err.getvalue())

    def test_general_mode_without_prompt_fails(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--config", str(self.config), "--mode", "general"])
        self.assertEqual(code, 1)
        self.assertIn("--prompt", err.getvalue())


if __name__ == "__main__":
    unittest.main()
