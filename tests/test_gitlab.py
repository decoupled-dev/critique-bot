from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from critique_bot.config import GitLabConfig
from critique_bot.gitlab import (
    GitLabTarget,
    MrContext,
    api_v4_url,
    extract_tickets,
    fetch_mr_context,
    format_mr_context,
    mr_api_url,
    parse_mr_url,
    resolve_target,
    summarize_changed_files,
)


_ENV = (
    "CI_API_V4_URL",
    "CI_SERVER_URL",
    "CI_PROJECT_ID",
    "CI_PROJECT_PATH",
    "CI_MERGE_REQUEST_IID",
    "CI_MERGE_REQUEST_URL",
    "CRITIQUE_GITLAB_URL",
    "CRITIQUE_GITLAB_TOKEN",
    "GITLAB_TOKEN",
    "CI_JOB_TOKEN",
)


class EnvIsolated(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.pop(key, None) for key in _ENV}

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ApiUrlTests(unittest.TestCase):
    def test_appends_api_v4(self) -> None:
        self.assertEqual(
            api_v4_url("https://gitlab.example.com"),
            "https://gitlab.example.com/api/v4",
        )
        self.assertEqual(
            api_v4_url("https://gitlab.example.com/api/v4/"),
            "https://gitlab.example.com/api/v4",
        )

    def test_mr_api_url_encodes_path(self) -> None:
        url = mr_api_url("https://g.example/api/v4", "group/app", "9")
        self.assertEqual(
            url,
            "https://g.example/api/v4/projects/group%2Fapp/merge_requests/9",
        )
        self.assertEqual(
            mr_api_url("https://g.example/api/v4", "11", "9"),
            "https://g.example/api/v4/projects/11/merge_requests/9",
        )


class ParseMrUrlTests(unittest.TestCase):
    def test_nested_group(self) -> None:
        parsed = parse_mr_url(
            "https://gitlab.example.com/acme/car/settings/-/merge_requests/42#note_1"
        )
        self.assertEqual(
            parsed,
            ("https://gitlab.example.com", "acme/car/settings", "42"),
        )

    def test_invalid(self) -> None:
        self.assertIsNone(parse_mr_url("https://example.com/not-an-mr"))


class ResolveTargetTests(EnvIsolated):
    def test_cli_beats_config(self) -> None:
        target = resolve_target(
            GitLabConfig(base_url="https://from-config.example", project_id="1", mr_iid="2"),
            api_url="https://from-cli.example/api/v4",
            project_id="9",
            mr_iid="4",
        )
        self.assertEqual(target.api_url, "https://from-cli.example/api/v4")
        self.assertEqual(target.project_id, "9")
        self.assertEqual(target.mr_iid, "4")

    def test_config_base_url_builds_api(self) -> None:
        target = resolve_target(
            GitLabConfig(base_url="https://gitlab.example.com", project_id="8", mr_iid="3")
        )
        self.assertEqual(target.api_url, "https://gitlab.example.com/api/v4")
        self.assertEqual(target.project_id, "8")
        self.assertEqual(target.mr_iid, "3")

    def test_mr_url_fills_gaps(self) -> None:
        target = resolve_target(
            mr_url="https://gitlab.example.com/group/app/-/merge_requests/12"
        )
        self.assertEqual(target.api_url, "https://gitlab.example.com/api/v4")
        self.assertEqual(target.project_id, "group/app")
        self.assertEqual(target.mr_iid, "12")

    def test_ci_env(self) -> None:
        os.environ["CI_API_V4_URL"] = "https://ci.example/api/v4"
        os.environ["CI_PROJECT_ID"] = "77"
        os.environ["CI_MERGE_REQUEST_IID"] = "5"
        target = resolve_target()
        self.assertEqual(target.api_url, "https://ci.example/api/v4")
        self.assertEqual(target.project_id, "77")
        self.assertEqual(target.mr_iid, "5")


class TicketTests(unittest.TestCase):
    def test_jira_and_gitlab_hashes(self) -> None:
        tickets = extract_tickets(
            "AAOS-1234: fix HVAC",
            "Closes #88 and see #90",
            "feature/AAOS-1234",
        )
        self.assertIn("AAOS-1234", tickets)
        self.assertIn("#88", tickets)
        self.assertIn("#90", tickets)


class ContextFormatTests(unittest.TestCase):
    def test_format_includes_system_fields(self) -> None:
        text = format_mr_context(
            MrContext(
                title="Fix Binder leak",
                description="Do not leak identity.",
                web_url="https://gitlab.example.com/g/a/-/merge_requests/4",
                iid="4",
                source_branch="fix/hvac",
                target_branch="master",
                labels=("aaos",),
                tickets=("AAOS-9", "#4"),
                commits=("abc123 Fix leak\nRestore in finally",),
                changed_files=("Hvac.java  +88-90",),
            )
        )
        self.assertIn("Title: !4 Fix Binder leak", text)
        self.assertIn("Tickets: AAOS-9, #4", text)
        self.assertIn("Do not leak identity.", text)
        self.assertIn("- abc123 Fix leak", text)
        self.assertIn("Hvac.java  +88-90", text)

    def test_summarize_changed_files(self) -> None:
        patch = (
            "diff --git a/src/pay.py b/src/pay.py\n"
            "--- a/src/pay.py\n"
            "+++ b/src/pay.py\n"
            "@@ -8,7 +8,9 @@ def total(qty, price):\n"
            "     if qty < 0:\n"
            "         return 0\n"
            "-    return qty * price\n"
            "+    # trusted caller\n"
            "+    return qty * price * coupon\n"
            "+\n"
        )
        files = summarize_changed_files(patch)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith("src/pay.py"))
        self.assertIn("+", files[0])
        self.assertIn("-", files[0])


class FetchContextTests(unittest.TestCase):
    def test_loads_title_commits_issues_and_diffs(self) -> None:
        calls: list[str] = []

        def fake_request(method, url, token, payload=None):
            calls.append(url)
            if url.endswith("/merge_requests/4"):
                return {
                    "title": "AAOS-7 HVAC",
                    "description": "Closes #3",
                    "iid": 4,
                    "source_branch": "fix/hvac",
                    "target_branch": "master",
                    "web_url": "https://g.example/p/-/merge_requests/4",
                    "labels": ["aaos"],
                }
            if "/commits" in url:
                return [
                    {
                        "short_id": "abc1234",
                        "title": "Fix leak",
                        "message": "Fix leak\n\nRestore Binder identity.",
                    }
                ]
            if url.endswith("/closes_issues"):
                return [{"iid": 3, "title": "identity leak"}]
            if "/diffs" in url:
                return [
                    {
                        "old_path": "Hvac.java",
                        "new_path": "Hvac.java",
                        "diff": "@@ -1,1 +1,2 @@\n x\n+y\n",
                    }
                ]
            return {}

        ctx = fetch_mr_context(
            GitLabTarget(
                api_url="https://g.example/api/v4",
                project_id="1",
                mr_iid="4",
                token="tok",
            ),
            do_request=fake_request,
        )
        self.assertEqual(ctx.title, "AAOS-7 HVAC")
        self.assertIn("AAOS-7", ctx.tickets)
        self.assertIn("#3", ctx.tickets)
        self.assertTrue(any("Fix leak" in item for item in ctx.commits))
        self.assertTrue(ctx.patch)
        self.assertTrue(ctx.changed_files)
        self.assertTrue(any(url.endswith("/merge_requests/4") for url in calls))


if __name__ == "__main__":
    unittest.main()
