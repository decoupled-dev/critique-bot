from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from critique_bot import log

from critique_bot.gitlab_post import (
    GitLabPostError,
    post_review,
)
from critique_bot.gitlab_post import (
    _diff_refs,
    _quote,
    _request,
    _resolve_token,
)


_ENV = (
    "CI_PROJECT_ID",
    "CI_MERGE_REQUEST_IID",
    "CI_API_V4_URL",
    "CRITIQUE_GITLAB_TOKEN",
    "GITLAB_TOKEN",
    "CI_JOB_TOKEN",
    "CI_MERGE_REQUEST_DIFF_BASE_SHA",
    "CI_COMMIT_SHA",
)


REVIEW = """\
Looks risky.

```json
{"comments":[{"path":"src/pay.py","line":12,"side":"new","body":"coupon is not defined"}]}
```
"""

PATCH = """\
diff --git a/src/pay.py b/src/pay.py
index 111..222 100644
--- a/src/pay.py
+++ b/src/pay.py
@@ -8,7 +8,9 @@ def total(qty, price):
     if qty < 0:
         return 0
-    return qty * price
+    # trusted caller
+    return qty * price * coupon
+
"""


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


class QuoteTests(unittest.TestCase):
    def test_numeric_unchanged(self) -> None:
        self.assertEqual(_quote("42"), "42")

    def test_path_encoded(self) -> None:
        self.assertEqual(_quote("group/app"), "group%2Fapp")


class ResolveTokenTests(EnvIsolated):
    def test_prefers_critique_token(self) -> None:
        os.environ["CRITIQUE_GITLAB_TOKEN"] = "pat"
        os.environ["GITLAB_TOKEN"] = "gl"
        os.environ["CI_JOB_TOKEN"] = "job"
        self.assertEqual(_resolve_token(), "pat")

    def test_falls_back_to_gitlab_token(self) -> None:
        os.environ["GITLAB_TOKEN"] = "gl"
        self.assertEqual(_resolve_token(), "gl")

    def test_job_token_used_with_warning(self) -> None:
        os.environ["CI_JOB_TOKEN"] = "job"
        self.assertEqual(_resolve_token(), "job")

    def test_empty(self) -> None:
        self.assertEqual(_resolve_token(), "")


class PostReviewValidationTests(EnvIsolated):
    def test_needs_api_url(self) -> None:
        review = self.folder / "review.md"
        review.write_text("ok", encoding="utf-8")
        with self.assertRaises(GitLabPostError) as ctx:
            post_review(
                review_file=review,
                project_id="1",
                mr_iid="2",
                token="t",
            )
        self.assertIn("api-url", str(ctx.exception).lower())

    def test_needs_project_and_mr(self) -> None:
        review = self.folder / "review.md"
        review.write_text("ok", encoding="utf-8")
        with self.assertRaises(GitLabPostError) as ctx:
            post_review(
                review_file=review,
                api_url="https://gitlab.example/api/v4",
                token="t",
            )
        self.assertIn("project-id", str(ctx.exception))

    def test_needs_token(self) -> None:
        review = self.folder / "review.md"
        review.write_text("ok", encoding="utf-8")
        with self.assertRaises(GitLabPostError) as ctx:
            post_review(
                review_file=review,
                project_id="1",
                mr_iid="2",
                api_url="https://gitlab.example/api/v4",
            )
        self.assertIn("token", str(ctx.exception).lower())

    def test_empty_review(self) -> None:
        review = self.folder / "review.md"
        review.write_text("  \n", encoding="utf-8")
        with self.assertRaises(GitLabPostError) as ctx:
            post_review(
                review_file=review,
                project_id="1",
                mr_iid="2",
                api_url="https://gitlab.example/api/v4",
                token="t",
            )
        self.assertIn("empty", str(ctx.exception))


class PostReviewFlowTests(EnvIsolated):
    def test_posts_inline_and_summary(self) -> None:
        review = self.folder / "review.md"
        review.write_text(REVIEW, encoding="utf-8")
        patch_path = self.folder / "diff.patch"
        patch_path.write_text(PATCH, encoding="utf-8")
        calls: list[tuple[str, str, object]] = []

        def fake_request(method, url, token, payload=None):
            calls.append((method, url, payload))
            if method == "GET":
                return {
                    "diff_refs": {
                        "base_sha": "aaa",
                        "start_sha": "bbb",
                        "head_sha": "ccc",
                    }
                }
            return {}

        buf = io.StringIO()
        with patch("critique_bot.gitlab_post._request", side_effect=fake_request):
            with redirect_stdout(buf):
                code = post_review(
                    review_file=review,
                    patch_file=patch_path,
                    project_id="9",
                    mr_iid="4",
                    api_url="https://gitlab.example/api/v4",
                    token="pat",
                )
        self.assertEqual(code, 0)
        methods = [c[0] for c in calls]
        self.assertEqual(methods[0], "GET")
        self.assertIn("POST", methods)
        self.assertIn("1 inline thread", buf.getvalue())
        discussion_posts = [
            c for c in calls if c[0] == "POST" and "discussions" in c[1]
        ]
        inline = next(c for c in discussion_posts if c[2] and c[2].get("position"))
        summary = next(
            c for c in discussion_posts if c[2] and not c[2].get("position")
        )
        self.assertEqual(inline[2]["body"], "coupon is not defined")
        self.assertEqual(
            inline[2]["position"],
            {
                "base_sha": "aaa",
                "start_sha": "bbb",
                "head_sha": "ccc",
                "old_path": "src/pay.py",
                "new_path": "src/pay.py",
                "position_type": "text",
                "new_line": 12,
            },
        )
        self.assertIn("Looks risky.", summary[2]["body"])
        self.assertNotIn("```json", summary[2]["body"])
        self.assertNotIn('"comments"', summary[2]["body"])
        self.assertIn("### AAOS system-app review", summary[2]["body"])
        self.assertIn("**Risk:", summary[2]["body"])
        self.assertIn("Changes", summary[2]["body"])
        self.assertFalse(any(c[1].endswith("/notes") for c in calls if c[0] == "POST"))

    def test_uses_config_base_url(self) -> None:
        from critique_bot.config import GitLabConfig

        review = self.folder / "review.md"
        review.write_text("summary only", encoding="utf-8")
        calls: list[tuple[str, str, object]] = []

        def fake_request(method, url, token, payload=None):
            calls.append((method, url, payload))
            if method == "GET":
                return {
                    "diff_refs": {
                        "base_sha": "aaa",
                        "start_sha": "bbb",
                        "head_sha": "ccc",
                    }
                }
            return {}

        with patch("critique_bot.gitlab_post._request", side_effect=fake_request):
            with redirect_stdout(io.StringIO()):
                code = post_review(
                    review_file=review,
                    gitlab=GitLabConfig(base_url="https://gitlab.example.com"),
                    project_id="9",
                    mr_iid="4",
                    token="pat",
                )
        self.assertEqual(code, 0)
        self.assertTrue(
            any(
                "https://gitlab.example.com/api/v4/projects/9/merge_requests/4"
                in c[1]
                for c in calls
            )
        )

    def test_skips_unmapped_comment(self) -> None:
        review = self.folder / "review.md"
        review.write_text(
            'hello\n```json\n{"comments":[{"path":"nope.c","line":1,"side":"new","body":"x"}]}\n```\n',
            encoding="utf-8",
        )
        patch_path = self.folder / "diff.patch"
        patch_path.write_text(PATCH, encoding="utf-8")

        def fake_request(method, url, token, payload=None):
            if method == "GET":
                return {"diff_refs": {"base_sha": "a", "start_sha": "a", "head_sha": "b"}}
            return {}

        buf = io.StringIO()
        with patch("critique_bot.gitlab_post._request", side_effect=fake_request):
            with redirect_stdout(buf):
                post_review(
                    review_file=review,
                    patch_file=patch_path,
                    project_id="1",
                    mr_iid="2",
                    api_url="https://gitlab.example/api/v4",
                    token="t",
                )
        self.assertIn("1 overview thread", buf.getvalue())
        self.assertIn("0 skipped", buf.getvalue())

    def test_inline_rejection_is_skipped(self) -> None:
        review = self.folder / "review.md"
        review.write_text(REVIEW, encoding="utf-8")
        patch_path = self.folder / "diff.patch"
        patch_path.write_text(PATCH, encoding="utf-8")

        def fake_request(method, url, token, payload=None):
            if method == "GET":
                return {"diff_refs": {"base_sha": "a", "start_sha": "a", "head_sha": "b"}}
            if payload and payload.get("position"):
                raise GitLabPostError("HTTP 400")
            return {}

        buf = io.StringIO()
        with patch("critique_bot.gitlab_post._request", side_effect=fake_request):
            with redirect_stdout(buf):
                post_review(
                    review_file=review,
                    patch_file=patch_path,
                    project_id="1",
                    mr_iid="2",
                    api_url="https://gitlab.example/api/v4",
                    token="t",
                )
        self.assertIn("1 overview thread", buf.getvalue())
        self.assertIn("0 inline thread", buf.getvalue())

    def test_maps_from_gitlab_diffs_when_patch_missing(self) -> None:
        review = self.folder / "review.md"
        review.write_text(REVIEW, encoding="utf-8")
        calls: list[tuple[str, str, object]] = []

        def fake_request(method, url, token, payload=None):
            calls.append((method, url, payload))
            if method == "GET":
                if "/diffs" in url:
                    return [
                        {
                            "old_path": "src/pay.py",
                            "new_path": "src/pay.py",
                            "diff": (
                                "@@ -8,7 +8,9 @@ def total(qty, price):\n"
                                "     if qty < 0:\n"
                                "         return 0\n"
                                "-    return qty * price\n"
                                "+    # trusted caller\n"
                                "+    return qty * price * coupon\n"
                                "+\n"
                            ),
                        }
                    ]
                return {
                    "diff_refs": {
                        "base_sha": "aaa",
                        "start_sha": "bbb",
                        "head_sha": "ccc",
                    }
                }
            return {}

        buf = io.StringIO()
        with patch("critique_bot.gitlab_post._request", side_effect=fake_request):
            with redirect_stdout(buf):
                code = post_review(
                    review_file=review,
                    project_id="9",
                    mr_iid="4",
                    api_url="https://gitlab.example/api/v4",
                    token="pat",
                )
        self.assertEqual(code, 0)
        self.assertIn("1 inline thread", buf.getvalue())
        inline = next(
            c
            for c in calls
            if c[0] == "POST" and isinstance(c[2], dict) and c[2].get("position")
        )
        self.assertEqual(inline[2]["position"]["new_line"], 12)

    def test_reads_ids_from_env(self) -> None:
        review = self.folder / "review.md"
        review.write_text("summary only", encoding="utf-8")
        os.environ["CI_PROJECT_ID"] = "11"
        os.environ["CI_MERGE_REQUEST_IID"] = "22"
        os.environ["CI_API_V4_URL"] = "https://gitlab.example/api/v4/"

        def fake_request(method, url, token, payload=None):
            if method == "GET":
                self.assertIn("/projects/11/merge_requests/22", url)
                return {"diff_refs": {"base_sha": "a", "start_sha": "a", "head_sha": "b"}}
            return {}

        with patch("critique_bot.gitlab_post._request", side_effect=fake_request):
            with patch("critique_bot.gitlab_post._resolve_token", return_value="tok"):
                with redirect_stdout(io.StringIO()):
                    code = post_review(review_file=review)
        self.assertEqual(code, 0)

    def test_unfenced_json_posts_inline_without_json_in_summary(self) -> None:
        review = self.folder / "review.md"
        review.write_text(
            "**Risk: Risky**\n\n1. coupon is not defined.\n\n"
            "{\n"
            '  "risk": "risky",\n'
            '  "comments": [\n'
            '    {"path": "src/pay.py", "line": 12, "body": "coupon is not defined"}\n'
            "  ]\n"
            "}\n",
            encoding="utf-8",
        )
        patch_path = self.folder / "diff.patch"
        patch_path.write_text(PATCH, encoding="utf-8")
        calls: list[tuple[str, str, object]] = []

        def fake_request(method, url, token, payload=None):
            calls.append((method, url, payload))
            if method == "GET":
                return {
                    "diff_refs": {
                        "base_sha": "aaa",
                        "start_sha": "bbb",
                        "head_sha": "ccc",
                    }
                }
            return {}

        buf = io.StringIO()
        with patch("critique_bot.gitlab_post._request", side_effect=fake_request):
            with redirect_stdout(buf):
                code = post_review(
                    review_file=review,
                    patch_file=patch_path,
                    project_id="9",
                    mr_iid="4",
                    api_url="https://gitlab.example/api/v4",
                    token="pat",
                )
        self.assertEqual(code, 0)
        self.assertIn("1 inline thread", buf.getvalue())
        discussion_posts = [
            c for c in calls if c[0] == "POST" and "discussions" in c[1]
        ]
        inline = next(c for c in discussion_posts if c[2] and c[2].get("position"))
        summary = next(
            c for c in discussion_posts if c[2] and not c[2].get("position")
        )
        self.assertEqual(inline[2]["position"]["new_line"], 12)
        self.assertIn("coupon is not defined", inline[2]["body"])
        self.assertNotIn("```json", summary[2]["body"])
        self.assertNotIn('"comments"', summary[2]["body"])
        self.assertIn("**Risk:", summary[2]["body"])

    def test_summary_actions_become_inline_when_json_missing(self) -> None:
        review = self.folder / "review.md"
        review.write_text(
            "**Risk: Risky**\n\n"
            "**1 action**\n\n"
            "1. coupon is not defined in `src/pay.py:12`.\n",
            encoding="utf-8",
        )
        patch_path = self.folder / "diff.patch"
        patch_path.write_text(PATCH, encoding="utf-8")
        calls: list[tuple[str, str, object]] = []

        def fake_request(method, url, token, payload=None):
            calls.append((method, url, payload))
            if method == "GET":
                return {
                    "diff_refs": {
                        "base_sha": "aaa",
                        "start_sha": "bbb",
                        "head_sha": "ccc",
                    }
                }
            return {}

        buf = io.StringIO()
        with patch("critique_bot.gitlab_post._request", side_effect=fake_request):
            with redirect_stdout(buf):
                code = post_review(
                    review_file=review,
                    patch_file=patch_path,
                    project_id="9",
                    mr_iid="4",
                    api_url="https://gitlab.example/api/v4",
                    token="pat",
                )
        self.assertEqual(code, 0)
        self.assertIn("1 inline thread", buf.getvalue())
        inline = next(
            c
            for c in calls
            if c[0] == "POST" and isinstance(c[2], dict) and c[2].get("position")
        )
        self.assertEqual(inline[2]["position"]["new_line"], 12)
        summary = next(
            c
            for c in calls
            if c[0] == "POST" and isinstance(c[2], dict) and not c[2].get("position")
        )
        self.assertNotIn('"comments"', summary[2]["body"])

    def test_paragraph_summary_posts_inline(self) -> None:
        review = self.folder / "review.md"
        review.write_text(
            "**Risk: Risky** Restore Binder identity in `src/pay.py:12` "
            "after clearCallingIdentity.\n",
            encoding="utf-8",
        )
        patch_path = self.folder / "diff.patch"
        patch_path.write_text(PATCH, encoding="utf-8")
        calls: list[tuple[str, str, object]] = []

        def fake_request(method, url, token, payload=None):
            calls.append((method, url, payload))
            if method == "GET":
                return {
                    "diff_refs": {
                        "base_sha": "aaa",
                        "start_sha": "bbb",
                        "head_sha": "ccc",
                    }
                }
            return {}

        buf = io.StringIO()
        with patch("critique_bot.gitlab_post._request", side_effect=fake_request):
            with redirect_stdout(buf):
                code = post_review(
                    review_file=review,
                    patch_file=patch_path,
                    project_id="9",
                    mr_iid="4",
                    api_url="https://gitlab.example/api/v4",
                    token="pat",
                )
        self.assertEqual(code, 0)
        self.assertIn("1 inline thread", buf.getvalue())
        summary = next(
            c
            for c in calls
            if c[0] == "POST" and isinstance(c[2], dict) and not c[2].get("position")
        )
        self.assertRegex(summary[2]["body"], r"1\. Restore Binder identity.*  \n")


class DiffRefsTests(EnvIsolated):
    def test_from_versions_endpoint(self) -> None:
        with patch(
            "critique_bot.gitlab_post._request",
            return_value=[
                {
                    "head_commit_sha": "h",
                    "base_commit_sha": "b",
                    "start_commit_sha": "s",
                }
            ],
        ):
            refs = _diff_refs("https://g/api/v4", "1", "2", "t")
        self.assertEqual(refs["head_sha"], "h")
        self.assertEqual(refs["base_sha"], "b")
        self.assertEqual(refs["start_sha"], "s")

    def test_from_api(self) -> None:
        with patch(
            "critique_bot.gitlab_post._request",
            return_value={
                "diff_refs": {"base_sha": "b", "head_sha": "h", "start_sha": "s"}
            },
        ):
            refs = _diff_refs("https://g/api/v4", "1", "2", "t")
        self.assertEqual(refs["base_sha"], "b")
        self.assertEqual(refs["start_sha"], "s")
        self.assertEqual(refs["head_sha"], "h")

    def test_start_sha_defaults_to_base(self) -> None:
        with patch(
            "critique_bot.gitlab_post._request",
            return_value={"diff_refs": {"base_sha": "b", "head_sha": "h"}},
        ):
            refs = _diff_refs("https://g/api/v4", "1", "2", "t")
        self.assertEqual(refs["start_sha"], "b")

    def test_falls_back_to_ci_env(self) -> None:
        os.environ["CI_MERGE_REQUEST_DIFF_BASE_SHA"] = "base"
        os.environ["CI_COMMIT_SHA"] = "head"
        with patch(
            "critique_bot.gitlab_post._request",
            side_effect=GitLabPostError("down"),
        ):
            refs = _diff_refs("https://g/api/v4", "1", "2", "t")
        self.assertEqual(refs["head_sha"], "head")
        self.assertEqual(refs["base_sha"], "base")

    def test_raises_when_no_refs(self) -> None:
        with patch("critique_bot.gitlab_post._request", return_value={}):
            with self.assertRaises(GitLabPostError) as ctx:
                _diff_refs("https://g/api/v4", "1", "2", "t")
        self.assertIn("diff_refs", str(ctx.exception))


class RequestTests(EnvIsolated):
    def test_http_error_wrapped(self) -> None:
        error = HTTPError(
            "https://g/api/v4/x",
            403,
            "Forbidden",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"nope"),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(GitLabPostError) as ctx:
                _request("GET", "https://g/api/v4/x", "tok")
        self.assertIn("HTTP 403", str(ctx.exception))

    def test_empty_body_returns_none(self) -> None:
        class Resp:
            def read(self) -> bytes:
                return b""

            def __enter__(self) -> Resp:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        with patch("urllib.request.urlopen", return_value=Resp()):
            self.assertIsNone(_request("POST", "https://g/x", "tok", {"a": 1}))

    def test_invalid_json_returns_none(self) -> None:
        class Resp:
            def read(self) -> bytes:
                return b"not-json"

            def __enter__(self) -> Resp:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        with patch("urllib.request.urlopen", return_value=Resp()):
            self.assertIsNone(_request("GET", "https://g/x", "tok"))

    def test_job_token_header(self) -> None:
        os.environ["CI_JOB_TOKEN"] = "jobtok"

        class Resp:
            def read(self) -> bytes:
                return json.dumps({"ok": True}).encode()

            def __enter__(self) -> Resp:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        with patch("urllib.request.urlopen", return_value=Resp()) as opener:
            data = _request("GET", "https://g/x", "jobtok")
        self.assertEqual(data, {"ok": True})
        headers = opener.call_args[0][0].headers
        self.assertEqual(headers.get("Job-token") or headers.get("JOB-TOKEN"), "jobtok")

    def test_http_error_includes_gitlab_message_and_logs_endpoint(self) -> None:
        payload = json.dumps(
            {"message": {"position": ["must be a valid line code"]}}
        ).encode()
        error = HTTPError(
            "https://g/api/v4/projects/1/merge_requests/2/discussions",
            400,
            "Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(payload),
        )
        log.configure(enabled=True)
        err = io.StringIO()
        try:
            with patch("urllib.request.urlopen", side_effect=error):
                with redirect_stderr(err):
                    with self.assertRaises(GitLabPostError) as ctx:
                        _request(
                            "POST",
                            "https://g/api/v4/projects/1/merge_requests/2/discussions",
                            "tok",
                            {
                                "body": "coupon is not defined",
                                "position": {
                                    "new_path": "src/pay.py",
                                    "new_line": 12,
                                },
                            },
                        )
        finally:
            log.configure(enabled=False)
        text = str(ctx.exception)
        logs = err.getvalue()
        self.assertIn("HTTP 400", text)
        self.assertIn("must be a valid line code", text)
        self.assertIn("POST", logs)
        self.assertIn("/discussions", logs)
        self.assertIn("400", logs)
        self.assertIn("src/pay.py", logs)

    def test_url_error_is_wrapped(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=URLError("connection refused"),
        ):
            with self.assertRaises(GitLabPostError) as ctx:
                _request("GET", "https://g/api/v4/x", "tok")
        self.assertIn("failed", str(ctx.exception).lower())
        self.assertIn("connection refused", str(ctx.exception))

    def test_success_logs_status_and_note_url(self) -> None:
        class Resp:
            status = 201

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "id": "abc",
                        "notes": [
                            {
                                "id": 44,
                                "web_url": "https://g/x/-/merge_requests/2#note_44",
                            }
                        ],
                    }
                ).encode()

            def __enter__(self) -> Resp:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        log.configure(enabled=True)
        err = io.StringIO()
        try:
            with patch("urllib.request.urlopen", return_value=Resp()):
                with redirect_stderr(err):
                    data = _request(
                        "POST",
                        "https://g/api/v4/projects/1/merge_requests/2/discussions",
                        "tok",
                        {"body": "summary"},
                    )
        finally:
            log.configure(enabled=False)
        self.assertEqual(data["id"], "abc")
        logs = err.getvalue()
        self.assertIn("201", logs)
        self.assertIn("/discussions", logs)
        self.assertIn("note=44", logs)
        self.assertIn("https://g/x/-/merge_requests/2#note_44", logs)


class PostReviewLogTests(EnvIsolated):
    def test_logs_discussions_endpoint(self) -> None:
        review = self.folder / "review.md"
        review.write_text("summary only", encoding="utf-8")
        log.configure(enabled=True)
        err = io.StringIO()

        def fake_request(method, url, token, payload=None):
            if method == "GET":
                return {
                    "diff_refs": {
                        "base_sha": "aaa",
                        "start_sha": "bbb",
                        "head_sha": "ccc",
                    }
                }
            return {"id": "sum", "notes": [{"id": 9}]}

        try:
            with patch("critique_bot.gitlab_post._request", side_effect=fake_request):
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    code = post_review(
                        review_file=review,
                        project_id="9",
                        mr_iid="4",
                        api_url="https://gitlab.example/api/v4",
                        token="pat",
                    )
        finally:
            log.configure(enabled=False)
        self.assertEqual(code, 0)
        logs = err.getvalue()
        self.assertIn("/projects/9/merge_requests/4/discussions", logs)
        self.assertIn("mr='4'", logs)


if __name__ == "__main__":
    unittest.main()
