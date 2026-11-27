# SPDX-License-Identifier: Apache-2.0
"""crit serve: the contract in docs/api/openapi.yaml, the token, the bind check, two clients on
one session, and a server killed mid-task that a new one resumes."""

from __future__ import annotations

import http.client
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import openapi_support
from test_engine import _edit_call, _engine_home, _FakeProvider

from critique_bot import api_client, server
from critique_bot.config import BotConfig, Selectors
from critique_bot.engine import Engine

ROOT = Path(__file__).resolve().parents[1]


def _raw(url: str, method: str, path: str, *, token: str | None, body: object = None) -> tuple[int, object]:
    host, port = url.split("//", 1)[1].rsplit(":", 1)
    conn = http.client.HTTPConnection(host, int(port), timeout=20)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    kind = response.getheader("Content-Type") or ""
    if "json" in kind:
        return response.status, json.loads(raw)
    return response.status, raw.decode("utf-8")


def _sse(text: str) -> list[dict]:
    events, data = [], []
    for line in text.splitlines():
        if line.startswith("data:"):
            data.append(line[5:].strip())
        elif not line and data:
            events.append(json.loads("\n".join(data)))
            data = []
    return events


class _ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "project"
        self.root.mkdir()
        (self.root / "note.txt").write_text("one\n", encoding="utf-8")
        env = mock.patch.dict(os.environ, {"CRIT_HOME": str(base / "home")})
        env.start()
        self.addCleanup(env.stop)
        self.home = _engine_home(self.root)
        self.provider = _FakeProvider([])
        for item in (
            mock.patch("critique_bot.model_api.open_provider", lambda *_a, **_k: self.provider),
            mock.patch("critique_bot.engine.setup._open_shell_session", return_value=None),
            mock.patch("critique_bot.engine.setup._remove_old_codegraph"),
            mock.patch("critique_bot.engine.setup._prewarm_shell_environment"),
        ):
            item.start()
            self.addCleanup(item.stop)
        config = BotConfig(url="https://chat.example/", selectors=Selectors(prompt_input="textarea",
                                                                              assistant_messages=".m"), model="Model")
        self.app = server.CritServer(Engine(config, self.home), port=0)
        self.url = self.app.start()
        self.addCleanup(self.app.close)
        self.client = api_client.Client(self.url, self.app.token)

    def call(self, method: str, path: str, body: object = None, *, token: str | None = "default") -> tuple[int, object]:
        return _raw(self.url, method, path, token=self.app.token if token == "default" else token, body=body)

    def wait_for(self, sid: str, kind: str, since: int = 0) -> dict:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status, text = self.call("GET", f"/v1/sessions/{sid}/events?since={since}&follow=0")
            for event in _sse(text) if status == 200 else []:
                if event["type"] == kind:
                    return event
            time.sleep(0.05)
        self.fail(f"no {kind} event in session {sid}")


class ContractTest(_ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.spec = openapi_support.load()

    def test_the_spec_and_the_routes_are_the_same(self) -> None:
        documented = {(path, method.upper()) for path, item in self.spec["paths"].items()
                      for method in item if method in {"get", "post", "put", "delete"}}
        served = {(template, verb) for template, verb, _ in server._ROUTE_TABLE}
        self.assertEqual(documented, served)

    def test_the_spec_reads_the_same_with_pyyaml(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        self.assertEqual(openapi_support.load(), yaml.safe_load(openapi_support.SPEC.read_text(encoding="utf-8")))

    def _check(self, path: str, method: str, status: int, body: object) -> None:
        operation = self.spec["paths"][path][method]
        self.assertIn(str(status), operation["responses"], f"{method.upper()} {path} answered {status}: {body}")
        response = openapi_support.resolve(self.spec, operation["responses"][str(status)])
        content = response.get("content") or {}
        if "application/json" in content:
            problems = openapi_support.check(self.spec, content["application/json"]["schema"], body)
            self.assertEqual(problems, [], f"{method.upper()} {path} -> {status}")
        elif "text/event-stream" in content:
            schema = content["text/event-stream"]["schema"]
            for event in _sse(body):
                self.assertEqual(openapi_support.check(self.spec, schema, event), [], event)

    def test_every_operation_answers_as_documented(self) -> None:
        self.provider.script[:] = ["4."]
        for path, method, body in (("/v1/health", "get", None), ("/v1/tools", "get", None), ("/v1/models", "get", None),
                                   ("/v1/agents", "get", None), ("/v1/sessions", "get", None)):
            status, payload = self.call(method.upper(), path, body)
            self._check(path, method, status, payload)
        status, created = self.call("POST", "/v1/sessions", {"mode": "auto", "title": "contract"})
        self._check("/v1/sessions", "post", status, created)
        sid = created["id"]
        status, payload = self.call("POST", f"/v1/sessions/{sid}/messages", {"text": "what is 2 + 2?"})
        self._check("/v1/sessions/{id}/messages", "post", status, payload)
        finished = self.wait_for(sid, "task_finished")
        self.assertEqual(finished["code"], "COMPLETED")
        for path, method, body, real in (
            ("/v1/sessions/{id}", "get", None, f"/v1/sessions/{sid}"),
            ("/v1/sessions/{id}/mode", "put", {"mode": "plan"}, f"/v1/sessions/{sid}/mode"),
            ("/v1/sessions/{id}/cancel", "post", {}, f"/v1/sessions/{sid}/cancel"),
            ("/v1/sessions/{id}/undo", "post", {}, f"/v1/sessions/{sid}/undo"),
            ("/v1/sessions/{id}/requests/{rid}", "post", {"reply": {"decision": "yes"}},
             f"/v1/sessions/{sid}/requests/abc123"),
            ("/v1/sessions/{id}/events", "get", None, f"/v1/sessions/{sid}/events?follow=0"),
            ("/v1/sessions/{id}/fork", "post", {"at_seq": 2}, f"/v1/sessions/{sid}/fork"),
            ("/v1/sessions/{id}/messages", "post", {}, f"/v1/sessions/{sid}/messages"),
            ("/v1/sessions/{id}", "get", None, "/v1/sessions/nosuchsession"),
            ("/v1/sessions/{id}/mode", "put", {"mode": "sideways"}, f"/v1/sessions/{sid}/mode"),
        ):
            status, payload = self.call(method.upper(), real, body)
            self._check(path, method, status, payload)
        agents = self.call("GET", "/v1/agents")[1]["agents"]
        self.assertTrue(agents)
        self.provider.script[:] = ["Done."]
        status, payload = self.call("POST", f"/v1/agents/{agents[0]['name']}/runs", {"task": "what is 2 + 2?"})
        self._check("/v1/agents/{name}/runs", "post", status, payload)
        self.wait_for(payload["id"], "session_closed")
        status, payload = self.call("POST", "/v1/agents/no-such-agent/runs", {"task": "x"})
        self._check("/v1/agents/{name}/runs", "post", status, payload)

    def test_no_token_is_401_everywhere(self) -> None:
        for template, verb, _ in server._ROUTE_TABLE:
            path = template.replace("{id}", "x1").replace("{rid}", "ab").replace("{name}", "a")
            for token in (None, "wrong"):
                status, payload = self.call(verb, path, {} if verb in {"POST", "PUT"} else None, token=token)
                self.assertEqual(status, 401, f"{verb} {path}")
                self.assertEqual(openapi_support.check(self.spec, {"$ref": "#/components/schemas/Error"}, payload), [])


class SecurityTest(_ServerCase):
    def test_the_address_and_the_token_are_private_files(self) -> None:
        info = json.loads(server.server_file().read_text(encoding="utf-8"))
        self.assertEqual(info["url"], self.url)
        self.assertNotIn("token", info)
        self.assertEqual(server.token_file().read_text(encoding="utf-8").strip(), self.app.token)
        if os.name == "posix":
            for path in (server.server_file(), server.token_file()):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
        self.assertEqual(api_client.Client.from_server_file().health()["project"], str(self.root))
        self.app.close()
        self.assertFalse(server.token_file().exists())

    def test_a_non_loopback_bind_needs_insecure(self) -> None:
        for host in ("127.0.0.1", "::1", "localhost"):
            server.check_bind(host, insecure=False)
        with self.assertRaises(server.ServerError):
            server.check_bind("0.0.0.0", insecure=False)
        with self.assertRaises(server.ServerError):
            server.CritServer(self.app.engine, host="0.0.0.0", publish=False)
        from critique_bot.cli import main

        with mock.patch("critique_bot.cli._engine_inputs", side_effect=AssertionError("started")), \
                mock.patch("critique_bot.cli.log.notice") as notice:
            self.assertEqual(main(["serve", "--listen", "0.0.0.0"]), 2)
        self.assertIn("--insecure", notice.call_args.args[0])
        open_one = server.CritServer(self.app.engine, host="0.0.0.0", insecure=True, publish=False)
        open_one.httpd.server_close()

    def test_two_clients_see_the_same_events_and_one_answer_counts(self) -> None:
        self.provider.script[:] = [_edit_call("note.txt", "one", "two"), "COMPLETED"]
        sid = self.call("POST", "/v1/sessions", {"mode": "ask"})[1]["id"]
        seen: list[list[int]] = [[], []]

        def watch(index: int) -> None:
            for event in api_client.Client(self.url, self.app.token).events(sid):
                seen[index].append(event["seq"])
                if event["type"] == "task_finished":
                    return

        watchers = [threading.Thread(target=watch, args=(i,), daemon=True) for i in range(2)]
        for thread in watchers:
            thread.start()
        self.call("POST", f"/v1/sessions/{sid}/messages", {"text": "change note.txt to two"})
        request = self.wait_for(sid, "request")
        statuses: list[int] = []

        def answer() -> None:
            statuses.append(self.call("POST", f"/v1/sessions/{sid}/requests/{request['id']}",
                                      {"reply": {"decision": "yes"}})[0])

        answers = [threading.Thread(target=answer) for _ in range(2)]
        for thread in answers:
            thread.start()
        for thread in answers + watchers:
            thread.join(20)
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertTrue(seen[0])
        self.assertEqual(seen[0], seen[1])
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "two\n")



class HeadlessRunTest(_ServerCase):
    def test_crit_run_json_streams_events_as_json_lines(self) -> None:
        import io
        from contextlib import redirect_stdout

        from critique_bot.cli import main

        self.provider.script[:] = ["2 + 2 is 4."]
        out = io.StringIO()
        with mock.patch("critique_bot.cli._engine_inputs", return_value=(self.app.engine.config, self.home)), \
                redirect_stdout(out):
            code = main(["run", "--json", "--agent", "code-reviewer", "--repo-dir", str(self.root), "what is 2 + 2?"])
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds[0], "session_started")
        self.assertIn("assistant_text", kinds)
        self.assertEqual([e["code"] for e in events if e["type"] == "task_finished"], ["COMPLETED"])
        self.assertEqual(kinds[-2], "session_closed")
        # The final object (Stage 7): the outcome and the verified flow's evidence (none for a question).
        self.assertEqual(events[-1], {"type": "final", "code": "COMPLETED", "changed": [], "evidence": None})

    def test_crit_run_without_its_flags_is_still_a_task(self) -> None:
        from critique_bot import cli

        with mock.patch("critique_bot.cli._main_run", return_value=0) as run, \
                mock.patch("critique_bot.cli._main_headless", side_effect=AssertionError("headless")):
            self.assertEqual(cli.main(["run", "the", "tests"]), 0)
        self.assertEqual(run.call_args.args[0], ["run", "the", "tests"])


class _FakeModel:
    """An OpenAI-compatible server answering from a script (text replies, streamed)."""

    def __init__(self, replies: list[str]) -> None:
        from test_native_tools import _Server

        self.server = _Server()
        self.server.replies = [[{"choices": [{"index": 0, "delta": {"content": text}}]}] for text in replies]

    @property
    def url(self) -> str:
        return self.server.url

    def stop(self) -> None:
        self.server.stop()


@unittest.skipUnless(os.name == "posix", "kills the server with SIGKILL")
class KilledServerTest(unittest.TestCase):
    def test_a_killed_server_is_resumed_by_the_next_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            root.mkdir()
            (root / "note.txt").write_text("one\n", encoding="utf-8")
            (root / ".crit").mkdir()
            (root / ".crit" / "settings.json").write_text(json.dumps(
                {"tool_format": "tags", "helper_sessions": 0, "environment_snapshot": False, "role": "generic",
                 "flow": "plain"}),  # the replies are a fixed script: no task-class question, no verified flow
                encoding="utf-8")
            edit = _edit_call("note.txt", "one", "two")
            model = _FakeModel([edit, edit, "COMPLETED"])
            self.addCleanup(model.stop)
            config = base / "config.json"
            config.write_text(json.dumps({"backend": "local", "url": model.url, "model": "fake-model",
                                          "local": {"num_ctx": 32768}}), encoding="utf-8")
            env = {**os.environ, "CRIT_HOME": str(base / "home"), "PYTHONPATH": str(ROOT / "src"),
                   "CRIT_CONFIG": str(config)}

            def start() -> tuple[subprocess.Popen, api_client.Client]:
                process = subprocess.Popen(  # noqa: S603 - our own interpreter and module
                    [sys.executable, "-m", "critique_bot", "serve", "--repo-dir", str(root)],
                    env=env, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                info_path = base / "home" / server.SERVER_FILE
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        info = json.loads(info_path.read_text(encoding="utf-8"))
                        if info.get("pid") == process.pid:
                            token = (base / "home" / server.TOKEN_FILE).read_text(encoding="utf-8").strip()
                            return process, api_client.Client(info["url"], token)
                    except (OSError, ValueError):
                        pass
                    if process.poll() is not None:
                        self.fail("crit serve stopped: " + process.stderr.read().decode("utf-8", "replace")[-2000:])
                    time.sleep(0.1)
                process.kill()
                self.fail("crit serve did not start")

            first, client = start()
            try:
                created = client.create(message="change note.txt to two", mode="ask", title="kill test")
                sid = created["id"]
                for event in client.events(sid):
                    if event["type"] == "request":
                        break
            finally:
                first.send_signal(signal.SIGKILL)
                first.wait(10)
                first.stderr.close()
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "one\n")

            second, client = start()
            try:
                rows = {row["id"]: row for row in client.sessions()}
                self.assertEqual(rows[sid]["status"], "killed")

                from critique_bot import ui_api

                class Front(ui_api.NullUI):
                    def approve(self, permission, *, risky: str = ""):
                        return "yes", ""

                    def read_message(self):
                        return None

                code = api_client.attach(client, sid, Front(), task="carry on: change note.txt to two")
                self.assertEqual(code, 0)
                kinds = [event["type"] for event in client.events(sid, follow=False)]
                self.assertIn("session_resumed", kinds)
                self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")
            finally:
                second.terminate()
                second.wait(10)
                second.stderr.close()


if __name__ == "__main__":
    unittest.main()
