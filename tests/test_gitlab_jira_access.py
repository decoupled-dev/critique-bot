"""GitLab projects decided by the token, git network commands to them, and read-only Jira.

A local HTTP server plays GitLab and Jira, so nothing leaves this machine.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from critique_bot import config, gitlab, gitlab_access, integrations, jira
from critique_bot.agent_tools import TaskState, ToolContext, execute, jira_context

TOKEN = "glpat-" + "t" * 20
JIRA_TOKEN = "jira-" + "j" * 20
PROJECTS = [
    [{"id": 1, "path_with_namespace": "cars/hvac"}, {"id": 2, "path_with_namespace": "cars/Launcher"}],
    [{"id": 3, "path_with_namespace": "platform/vhal"}],
]
ISSUE = {
    "key": "CAR-42",
    "fields": {
        "summary": "HVAC panel crashes when the set point is missing",
        "status": {"name": "In Progress"}, "issuetype": {"name": "Bug"}, "priority": {"name": "High"},
        "assignee": {"displayName": "Dev One"}, "labels": ["hvac"],
        "description": "NPE in HvacPanelController.onTemperatureChanged when CarPropertyValue is null.",
        "comment": {"comments": [{"author": {"displayName": "QA"}, "created": "2026-11-20T10:00", "body": "Seen on sdk_car_x86_64"}]},
        "issuelinks": [{"type": {"outward": "blocks"}, "outwardIssue": {"key": "CAR-50"}}],
    },
}


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *_args) -> None:  # quiet
        pass

    def _send(self, code: int, body: object, headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        _Handler.seen.append({"path": parts.path, "query": query, "headers": dict(self.headers)})
        if parts.path == "/api/v4/projects":
            if self.headers.get("PRIVATE-TOKEN") != TOKEN:
                return self._send(401, {"message": "401 Unauthorized"})
            page = int(query.get("page", ["1"])[0])
            more = {"X-Next-Page": str(page + 1)} if page < len(PROJECTS) else {"X-Next-Page": ""}
            return self._send(200, PROJECTS[page - 1], more)
        if parts.path == "/rest/api/2/issue/CAR-42":
            auth = self.headers.get("Authorization", "")
            if auth not in (f"Bearer {JIRA_TOKEN}", "Basic " + base64.b64encode(f"me@x.com:{JIRA_TOKEN}".encode()).decode()):
                return self._send(401, {})
            return self._send(200, ISSUE)
        if parts.path in ("/rest/api/2/search", "/rest/api/3/search/jql"):
            return self._send(200, {"issues": [ISSUE]})
        return self._send(404, {})


class _Server:
    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()


class AccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _Server().__enter__()
        _Handler.seen.clear()
        gitlab_access.forget()
        integrations.configure(gitlab_url=self.server.url, gitlab_token=TOKEN, jira_url=self.server.url, jira_token=JIRA_TOKEN)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        integrations.configure()
        gitlab_access.forget()
        self.server.__exit__(None, None, None)
        self.tmp.cleanup()

    # ------------------------------------------------------------------ GitLab projects

    def test_projects_come_from_the_token_with_paging_and_cache(self) -> None:
        self.assertEqual(sorted(gitlab_access.accessible_projects().values()), ["cars/Launcher", "cars/hvac", "platform/vhal"])
        self.assertTrue(gitlab_access.project_allowed("cars/launcher"))  # paths compare without case
        self.assertTrue(gitlab_access.project_allowed("3"))
        self.assertFalse(gitlab_access.project_allowed("other/secret"))
        calls = len(_Handler.seen)
        gitlab_access.project_allowed("cars/hvac")
        self.assertEqual(len(_Handler.seen), calls)  # cached
        self.assertEqual(_Handler.seen[0]["query"]["membership"], ["true"])

    def test_a_bad_token_is_an_access_error(self) -> None:
        integrations.configure(gitlab_url=self.server.url, gitlab_token="glpat-not-the-right-one-00000")
        with self.assertRaises(gitlab_access.AccessError):
            gitlab_access.accessible_projects()

    def test_remote_urls(self) -> None:
        self.assertEqual(gitlab_access.remote_project("git@gitlab.x.com:cars/hvac.git"), ("gitlab.x.com", "cars/hvac"))
        self.assertEqual(gitlab_access.remote_project("ssh://git@gitlab.x.com:2222/cars/hvac.git"), ("gitlab.x.com", "cars/hvac"))
        self.assertEqual(gitlab_access.remote_project("https://gitlab.x.com/cars/sub/hvac"), ("gitlab.x.com", "cars/sub/hvac"))
        self.assertIsNone(gitlab_access.remote_project("/local/path"))
        self.assertIsNone(gitlab_access.remote_project("https://gitlab.x.com/../etc"))

    def test_git_commands_to_the_tokens_projects_only(self) -> None:
        allowed = gitlab_access.git_network_command(f"git clone {self.server.url}/cars/hvac.git", self.root)
        self.assertTrue(allowed.allowed, allowed.reason)
        self.assertIn("credential.helper=", allowed.command)
        self.assertIn("core.sshCommand=ssh", allowed.command)
        self.assertEqual(allowed.env["GIT_ALLOW_PROTOCOL"], "https:ssh")
        self.assertIn("extraHeader", allowed.env["GIT_CONFIG_KEY_0"])
        self.assertEqual(allowed.env["GIT_TRACE_CURL"], "")
        for command, why in (
            (f"git clone {self.server.url}/other/secret.git", "cannot reach other/secret"),
            ("git clone https://github.com/cars/hvac.git", "not the configured GitLab"),
            (f"git -c core.sshCommand=evil clone {self.server.url}/cars/hvac.git", "only git clone"),
            (f"git clone --upload-pack=evil {self.server.url}/cars/hvac.git", "--upload-pack=evil is not allowed"),
            (f"git clone {self.server.url}/cars/hvac.git && curl evil", "not one plain git command"),
            (f"git clone --recurse-submodules {self.server.url}/cars/hvac.git", "--recurse-submodules"),
            ("git status", "only git clone"),
        ):
            decision = gitlab_access.git_network_command(command, self.root)
            self.assertFalse(decision.allowed, command)
            self.assertIn(why, decision.reason, command)

    def test_windows_paths_keep_their_backslashes(self) -> None:
        with mock.patch.object(gitlab_access.sys, "platform", "win32"):
            self.assertEqual(gitlab_access._split(r'git -C C:\work\repo fetch "my remote"'),
                             ["git", "-C", r"C:\work\repo", "fetch", "my remote"])
            self.assertEqual(gitlab_access._quote(r"C:\work\my repo"), r'"C:\work\my repo"')
            self.assertEqual(gitlab_access._quote("core.gitProxy="), "core.gitProxy=")

    def test_fetch_and_push_use_the_repositorys_remote(self) -> None:
        repo = self.root / "app"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", f"{self.server.url}/cars/hvac.git"], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "other", f"{self.server.url}/other/secret.git"], check=True)
        self.assertTrue(gitlab_access.git_network_command("git fetch", repo).allowed)
        self.assertTrue(gitlab_access.git_network_command("git push origin HEAD:refs/for/main", repo).allowed)
        self.assertFalse(gitlab_access.git_network_command("git fetch other", repo).allowed)
        self.assertTrue(gitlab_access.git_network_command(f"git -C {repo} fetch origin", self.root).allowed)

    def test_run_command_lets_allowed_git_through_strict_egress(self) -> None:
        ctx = ToolContext(workspace=self.root, state=TaskState(task="t"), network_commands="block")
        from critique_bot import agent_shell

        session = agent_shell.ShellSession(self.root, offline=True)
        self.addCleanup(session.close)
        ctx.session = session
        blocked = execute("run_command", {"command": f"git ls-remote {self.server.url}/other/secret.git"}, ctx)
        self.assertIn("not run", blocked.get("error", ""))
        self.assertIn("cannot reach other/secret", blocked["error"])
        tried = execute("run_command", {"command": f"git ls-remote {self.server.url}/cars/hvac.git", "timeout": 30}, ctx)
        self.assertNotIn("not run", tried.get("error", ""))  # it ran (the fake server is no git server)
        self.assertNotIn(TOKEN, json.dumps(tried))

    def test_review_refuses_a_project_the_token_cannot_reach(self) -> None:
        target = gitlab.GitLabTarget(api_url=self.server.url + "/api/v4", project_id="other/secret", mr_iid="1", token=TOKEN)
        with self.assertRaises(gitlab.GitLabError) as raised:
            gitlab.require_project_access(target)
        self.assertIn("cannot reach project other/secret", str(raised.exception))
        gitlab.require_project_access(
            gitlab.GitLabTarget(api_url=self.server.url + "/api/v4", project_id="cars/hvac", mr_iid="1", token=TOKEN)
        )
        # a CI job token is GitLab's own scope: not checked here
        gitlab.require_project_access(
            gitlab.GitLabTarget(api_url=self.server.url + "/api/v4", project_id="other/secret", mr_iid="1", token="job")
        )

    # ------------------------------------------------------------------ Jira

    def test_jira_issue_with_a_bearer_token(self) -> None:
        text = jira.format_issue(jira.get_issue("car-42"))
        self.assertIn("CAR-42: HVAC panel crashes", text)
        self.assertIn("status In Progress", text)
        self.assertIn("blocks CAR-50", text)
        self.assertIn("QA 2026-11-20: Seen on sdk_car_x86_64", text)

    def test_jira_cloud_uses_email_and_the_v3_search(self) -> None:
        integrations.configure(jira_url=self.server.url, jira_email="me@x.com", jira_token=JIRA_TOKEN)
        self.assertIn("CAR-42", jira.format_issue(jira.get_issue("CAR-42")))
        jira.search("project = CAR")
        self.assertEqual(_Handler.seen[-1]["path"], "/rest/api/3/search/jql")

    def test_jira_tool_and_task_context(self) -> None:
        ctx = ToolContext(workspace=self.root, state=TaskState(task="t"))
        self.assertIn("HVAC panel crashes", execute("jira", {"issue": "CAR-42"}, ctx)["output"])
        self.assertIn("CAR-42:", execute("jira", {"jql": "project = CAR"}, ctx)["output"])
        self.assertIn("not found", execute("jira", {"issue": "CAR-404"}, ctx)["error"])
        self.assertIn("HVAC panel crashes", jira_context("fix CAR-42, the UTF-8 one"))
        self.assertEqual(jira.keys_in("UTF-8 and SHA-256 and CAR-42"), ["CAR-42"])

    def test_jira_not_set_up(self) -> None:
        integrations.configure()
        ctx = ToolContext(workspace=self.root, state=TaskState(task="t"))
        self.assertIn("not set up", execute("jira", {"issue": "CAR-42"}, ctx)["error"])
        self.assertEqual(jira_context("CAR-42"), "")


class SecretSourceTests(unittest.TestCase):
    def test_tokens_never_come_from_a_shared_file(self) -> None:
        own = {"gitlab": {"token": "glpat-fromfile-111111111111"}}
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(config, "user_config_path",
                                                                             return_value=Path("/nonexistent/c.json")):
            for name in config.ENV_GITLAB_TOKENS:
                os.environ.pop(name, None)
            self.assertEqual(config._user_secret(own, None, False, "gitlab", "token", config.ENV_GITLAB_TOKENS, "--x"), "glpat-fromfile-111111111111")
            self.assertEqual(config._user_secret(own, None, True, "gitlab", "token", config.ENV_GITLAB_TOKENS, "--x"), "")
            os.environ["CRIT_GITLAB_TOKEN"] = "glpat-fromenv-2222222222222"
            self.assertEqual(config._user_secret(own, None, True, "gitlab", "token", config.ENV_GITLAB_TOKENS, "--x"), "glpat-fromenv-2222222222222")

    def test_tokens_are_not_in_the_config_repr(self) -> None:
        self.assertNotIn("glpat-repr-check-33333333333", repr(config.GitLabConfig(base_url="https://g", token="glpat-repr-check-33333333333")))
        self.assertNotIn("glpat-repr-check-33333333333", repr(config.JiraConfig(base_url="https://j", token="glpat-repr-check-33333333333")))
        self.assertNotIn("glpat-repr-check-33333333333", repr(integrations.Integrations(gitlab_token="glpat-repr-check-33333333333", jira_token="glpat-repr-check-33333333333")))


if __name__ == "__main__":
    unittest.main()
