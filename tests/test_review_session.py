from __future__ import annotations

import unittest

from critique_bot.patch import InputLimits, SanitizeStats
from critique_bot.review_session import (
    FILES_ALREADY_SENT,
    MAX_STAGED_FILES,
    PATCH_ONLY_FILES,
    REVIEW_NOW,
    PromptPayload,
    format_file_turn,
    format_prime_turn,
    one_shot_fits,
    reply_is_ack,
    run_review_session,
    sanitize_context_files,
    split_review_payload,
)


class FakeSession:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def send(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.replies:
            raise AssertionError(f"unexpected send: {prompt[:80]!r}")
        return self.replies.pop(0)


class FitAndSplitTests(unittest.TestCase):
    def test_one_shot_fits_accounts_for_sanitize_note(self) -> None:
        limits = InputLimits(max_prompt_chars=100)
        stats = SanitizeStats()
        self.assertTrue(one_shot_fits("x" * 100, limits, stats))
        stats.files_truncated = 1
        self.assertFalse(one_shot_fits("x" * 100, limits, stats))

    def test_small_files_stay_one_shot(self) -> None:
        limits = InputLimits(max_prompt_chars=2_000, max_file_chars=1_000)
        stats = SanitizeStats()
        payload = split_review_payload(
            "FILES\n{files}\nPATCH\n{patch}\n",
            "diff --git a/a b/a\n+hi\n",
            "",
            [("Foo.java", "class Foo {}\n")],
            limits,
            stats,
        )
        self.assertEqual(payload.files, {})
        self.assertIn("class Foo {}", payload.prompt)
        self.assertIn("--- file: Foo.java ---", payload.prompt)
        self.assertNotIn(FILES_ALREADY_SENT, payload.prompt)
        self.assertNotIn(REVIEW_NOW, payload.prompt)

    def test_overflow_stages_files_and_omits_bodies_from_prompt(self) -> None:
        body = "class Foo {\n" + ("    int x;\n" * 40) + "}\n"
        limits = InputLimits(max_prompt_chars=400, max_file_chars=2_000)
        payload = split_review_payload(
            "FILES\n{files}\nPATCH\n{patch}\n",
            "+hi\n",
            "",
            [("Foo.java", body)],
            limits,
            SanitizeStats(),
        )
        self.assertIn("Foo.java", payload.files)
        self.assertEqual(payload.files["Foo.java"], body)
        self.assertIn(FILES_ALREADY_SENT, payload.prompt)
        self.assertNotIn("int x;", payload.prompt)
        self.assertIn("+hi", payload.prompt)

    def test_overflow_caps_staged_file_turns(self) -> None:
        limits = InputLimits(
            max_prompt_chars=200, max_file_chars=2_000, patch_only_file_count=80
        )
        files = [(f"f{i}.java", "class X {}\n" * 5) for i in range(12)]
        payload = split_review_payload(
            "FILES\n{files}\nPATCH\n{patch}\n",
            "+hi\n",
            "",
            files,
            limits,
            SanitizeStats(),
        )
        self.assertEqual(len(payload.files), MAX_STAGED_FILES)
        self.assertEqual(list(payload.files), [f"f{i}.java" for i in range(MAX_STAGED_FILES)])
        self.assertNotIn("f8.java", payload.files)

    def test_many_changed_files_are_patch_only(self) -> None:
        limits = InputLimits(max_prompt_chars=400, max_file_chars=2_000)
        files = [(f"f{i}.java", "class X {}\n") for i in range(3)]
        payload = split_review_payload(
            "FILES\n{files}\nPATCH\n{patch}\n",
            "+hi\n",
            "",
            files,
            limits,
            SanitizeStats(),
            changed_path_count=10,
        )
        self.assertEqual(payload.files, {})
        self.assertIn(PATCH_ONLY_FILES, payload.prompt)
        self.assertIn("+hi", payload.prompt)
        self.assertNotIn("class X {}", payload.prompt)
        self.assertNotIn(FILES_ALREADY_SENT, payload.prompt)

    def test_patch_only_threshold_is_configurable(self) -> None:
        limits = InputLimits(
            max_prompt_chars=200,
            max_file_chars=2_000,
            patch_only_file_count=20,
        )
        files = [(f"f{i}.java", "class X {}\n" * 5) for i in range(12)]
        payload = split_review_payload(
            "FILES\n{files}\nPATCH\n{patch}\n",
            "+hi\n",
            "",
            files,
            limits,
            SanitizeStats(),
            changed_path_count=12,
        )
        self.assertEqual(len(payload.files), MAX_STAGED_FILES)


class SanitizeContextFilesTests(unittest.TestCase):
    def test_caps_each_file_not_the_combined_budget(self) -> None:
        limits = InputLimits(max_prompt_chars=80, max_file_chars=20, max_files=4)
        out, stats = sanitize_context_files(
            [("a.java", "a" * 50), ("b.java", "b" * 50)],
            limits,
        )
        self.assertEqual(len(out), 2)
        self.assertTrue(stats.files_truncated)
        self.assertGreater(len(out[0][1]) + len(out[1][1]), limits.max_prompt_chars)

    def test_max_files_skips_the_rest(self) -> None:
        limits = InputLimits(max_files=1, max_file_chars=100)
        out, stats = sanitize_context_files(
            [("a.java", "a"), ("b.java", "b")],
            limits,
        )
        self.assertEqual([name for name, _ in out], ["a.java"])
        self.assertEqual(stats.skipped_attachments, 1)


class FormatTurnTests(unittest.TestCase):
    def test_prime_lists_paths(self) -> None:
        text = format_prime_turn({"src/Foo.java": "abc", "Bar.kt": "xx"})
        self.assertIn("2 changed file", text)
        self.assertIn("src/Foo.java (3 chars)", text)
        self.assertIn("ACK <path>", text)
        self.assertIn(REVIEW_NOW, text)

    def test_file_turn_is_one_path(self) -> None:
        text = format_file_turn(1, 2, "Foo.java", "class Foo {}")
        self.assertIn("FILE 1 of 2", text)
        self.assertIn("ACK Foo.java", text)
        self.assertIn("--- file: Foo.java ---", text)
        self.assertIn("class Foo {}", text)


class AckTests(unittest.TestCase):
    def test_ack_first_line(self) -> None:
        self.assertTrue(reply_is_ack("ACK Foo.java", "Foo.java"))
        self.assertTrue(reply_is_ack("ack Foo.java\nextra", "Foo.java"))
        self.assertFalse(reply_is_ack("looks risky", "Foo.java"))
        self.assertFalse(reply_is_ack("", "Foo.java"))
        self.assertFalse(reply_is_ack("ACK other.java", "Foo.java"))


class RunReviewSessionTests(unittest.TestCase):
    def test_empty_files_single_send(self) -> None:
        session = FakeSession(["review body"])
        limits = InputLimits(max_prompt_chars=10_000)
        out = run_review_session(session, "ONE SHOT", {}, limits)
        self.assertEqual(out, "review body")
        self.assertEqual(session.prompts, ["ONE SHOT"])

    def test_staged_discards_intermediate_replies(self) -> None:
        session = FakeSession(
            ["ok", "ACK a.java", "I already found a bug", "FINAL REVIEW"]
        )
        limits = InputLimits(max_prompt_chars=10_000)
        files = {"a.java": "class A {}", "b.java": "class B {}"}
        sleeps: list[float] = []
        out = run_review_session(
            session,
            "REVIEW TEMPLATE\nPATCH",
            files,
            limits,
            turn_pause_seconds=0.5,
            sleep=sleeps.append,
        )
        self.assertEqual(out, "FINAL REVIEW")
        self.assertEqual(len(session.prompts), 4)
        self.assertIn("ACK <path>", session.prompts[0])
        self.assertIn("FILE 1 of 2", session.prompts[1])
        self.assertIn("class A {}", session.prompts[1])
        self.assertNotIn("class B {}", session.prompts[1])
        self.assertIn("FILE 2 of 2", session.prompts[2])
        self.assertIn("class B {}", session.prompts[2])
        self.assertTrue(session.prompts[3].rstrip().endswith(REVIEW_NOW))
        self.assertIn("REVIEW TEMPLATE", session.prompts[3])
        self.assertEqual(sleeps, [0.5, 0.5, 0.5])

    def test_no_pause_when_interval_is_zero(self) -> None:
        session = FakeSession(["ACK a.java", "ACK a.java", "done"])
        called = []
        run_review_session(
            session,
            "final",
            {"a.java": "x"},
            InputLimits(),
            turn_pause_seconds=0,
            sleep=lambda _: called.append(True),
        )
        self.assertEqual(called, [])

    def test_file_turn_chat_error_still_sends_review(self) -> None:
        from critique_bot.chat_client import ChatError

        class BoomSession:
            def __init__(self) -> None:
                self.prompts: list[str] = []
                self.n = 0

            def send(self, prompt: str) -> str:
                self.prompts.append(prompt)
                self.n += 1
                if self.n == 3:
                    raise ChatError(
                        "no assistant message appeared "
                        "(selector=\"[data-message-author-role='assistant']\", "
                        "previous_count=3)"
                    )
                if REVIEW_NOW in prompt:
                    return "FINAL REVIEW"
                return "ACK ok"

        session = BoomSession()
        out = run_review_session(
            session,
            "REVIEW TEMPLATE\nPATCH",
            {"a.java": "class A {}", "b.java": "class B {}", "c.java": "class C {}"},
            InputLimits(max_prompt_chars=10_000),
        )
        self.assertEqual(out, "FINAL REVIEW")
        self.assertEqual(len(session.prompts), 4)
        self.assertIn("FILE 1 of 3", session.prompts[1])
        self.assertIn("FILE 2 of 3", session.prompts[2])
        self.assertNotIn("FILE 3 of 3", "".join(session.prompts))
        self.assertTrue(session.prompts[-1].rstrip().endswith(REVIEW_NOW))


class PromptPayloadTests(unittest.TestCase):
    def test_default_files_empty(self) -> None:
        payload = PromptPayload(prompt="x")
        self.assertEqual(payload.files, {})
        self.assertEqual(payload.prompt, "x")
