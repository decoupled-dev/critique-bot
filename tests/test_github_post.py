"""GitHub PR posting: prose in the summary, JSON block turned into comments."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot.github_post import GitHubPostError, _pr_number_from_env, post_review

PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 import os
+import sys
 
 def main():
"""

REVIEW = """## Review

`sys` is imported but never used.

```json
{"comments":[{"path":"app.py","line":2,"side":"new","body":"Unused import."}]}
```
"""


class _Recorder:
    """Stands in for the GitHub REST API."""

    def __init__(self, fail_on: str = "") -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail_on = fail_on

    def __call__(self, method: str, url: str, token: str, payload=None):
        del token
        self.calls.append((method, url, payload))
        if self.fail_on and self.fail_on in url and method == "POST":
            raise GitHubPostError("HTTP 422")
        if method == "GET":
            return {"head": {"sha": "headsha"}}
        return {"id": 1}

    def posts_to(self, fragment: str) -> list[dict]:
        return [
            payload
            for method, url, payload in self.calls
            if method == "POST" and fragment in url and payload is not None
        ]


class PostReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.review = self.root / "review.md"
        self.review.write_text(REVIEW, encoding="utf-8")
        self.patch_file = self.root / "diff.patch"
        self.patch_file.write_text(PATCH, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _post(self, recorder: _Recorder, **kwargs):
        with patch("critique_bot.github_post._request", recorder):
            return post_review(
                review_file=self.review,
                patch_file=self.patch_file,
                repo="acme/widgets",
                pr_number="7",
                token="t0ken",
                **kwargs,
            )

    def test_summary_never_contains_the_json_block(self) -> None:
        recorder = _Recorder()
        self.assertEqual(self._post(recorder), 0)
        summaries = recorder.posts_to("/issues/7/comments")
        self.assertEqual(len(summaries), 1)
        body = summaries[0]["body"]
        self.assertIn("`sys` is imported but never used.", body)
        self.assertNotIn("```json", body)
        self.assertNotIn('"comments"', body)

    def test_inline_comment_is_anchored_to_the_head_sha(self) -> None:
        recorder = _Recorder()
        self._post(recorder)
        inline = recorder.posts_to("/pulls/7/comments")
        self.assertEqual(len(inline), 1)
        self.assertEqual(inline[0]["path"], "app.py")
        self.assertEqual(inline[0]["line"], 2)
        self.assertEqual(inline[0]["side"], "RIGHT")
        self.assertEqual(inline[0]["commit_id"], "headsha")
        self.assertEqual(inline[0]["body"], "Unused import.")

    def test_summary_mentions_the_inline_count(self) -> None:
        recorder = _Recorder()
        self._post(recorder)
        self.assertIn(
            "1 inline comment(s)", recorder.posts_to("/issues/7/comments")[0]["body"]
        )

    def test_rejected_inline_comment_does_not_stop_the_summary(self) -> None:
        recorder = _Recorder(fail_on="/pulls/7/comments")
        self.assertEqual(self._post(recorder), 0)
        self.assertEqual(len(recorder.posts_to("/issues/7/comments")), 1)

    def test_failed_summary_returns_non_zero(self) -> None:
        recorder = _Recorder(fail_on="/issues/7/comments")
        self.assertEqual(self._post(recorder), 1)

    def test_comment_outside_the_diff_is_skipped(self) -> None:
        self.review.write_text(
            REVIEW.replace('"line":2', '"line":900').replace(
                '"path":"app.py"', '"path":"other.py"'
            ),
            encoding="utf-8",
        )
        recorder = _Recorder()
        self.assertEqual(self._post(recorder), 0)
        self.assertEqual(recorder.posts_to("/pulls/7/comments"), [])
        self.assertEqual(len(recorder.posts_to("/issues/7/comments")), 1)

    def test_missing_repo_is_rejected(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(GitHubPostError) as ctx:
                post_review(review_file=self.review, pr_number="7", token="t")
        self.assertIn("owner/name", str(ctx.exception))

    def test_missing_token_is_rejected(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(GitHubPostError) as ctx:
                post_review(review_file=self.review, repo="a/b", pr_number="7")
        self.assertIn("token", str(ctx.exception))

    def test_empty_review_is_rejected(self) -> None:
        self.review.write_text("   \n", encoding="utf-8")
        with self.assertRaises(GitHubPostError):
            post_review(
                review_file=self.review, repo="a/b", pr_number="7", token="t"
            )

    def test_head_sha_falls_back_to_env(self) -> None:
        class FailingGet(_Recorder):
            def __call__(self, method, url, token, payload=None):
                if method == "GET":
                    raise GitHubPostError("HTTP 404")
                return super().__call__(method, url, token, payload)

        recorder = FailingGet()
        with patch.dict("os.environ", {"GITHUB_SHA": "envsha"}, clear=False):
            self._post(recorder)
        self.assertEqual(recorder.posts_to("/pulls/7/comments")[0]["commit_id"], "envsha")


class PrNumberTests(unittest.TestCase):
    def test_explicit_env_wins(self) -> None:
        with patch.dict("os.environ", {"GITHUB_PR_NUMBER": "42"}, clear=True):
            self.assertEqual(_pr_number_from_env(), "42")

    def test_parsed_from_ref(self) -> None:
        with patch.dict("os.environ", {"GITHUB_REF": "refs/pull/12/merge"}, clear=True):
            self.assertEqual(_pr_number_from_env(), "12")

    def test_read_from_the_event_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            event.write_text(json.dumps({"pull_request": {"number": 99}}), encoding="utf-8")
            with patch.dict(
                "os.environ", {"GITHUB_EVENT_PATH": str(event)}, clear=True
            ):
                self.assertEqual(_pr_number_from_env(), "99")

    def test_nothing_available(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_pr_number_from_env(), "")


if __name__ == "__main__":
    unittest.main()
