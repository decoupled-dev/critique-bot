# SPDX-License-Identifier: Apache-2.0
"""First-run questions: a local model server or a Web UI, and the config they write."""

from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from critique_bot import local_model
from critique_bot.config import STARTER_SELECTORS, load_config
from critique_bot.local_model import Probe, probe_server, probe_site
from critique_bot.redaction import redact_outgoing, set_custom_terms
from critique_bot.setup_wizard import (
    CHANGE_LATER,
    CHOICE_PROMPT,
    NO_PICKER,
    SERVER_PROMPT,
    STOPPED,
    WEB_UI_DONE,
    css_problem,
    parse_server,
    run_wizard,
)


class _Script:
    """Answers questions in order and records what was asked and said."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.said: list[str] = []
        self.asked: list[str] = []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected question: {prompt}")
        answer = self.answers.pop(0)
        if answer == "^C":
            raise KeyboardInterrupt
        if answer == "^D":
            raise EOFError
        return answer

    def say(self, text: str) -> None:
        self.said.append(text)


def _probes(*results: Probe):
    """A fake probe answering ``results`` in turn, recording the urls."""
    seen: list[str] = []
    queue = list(results)

    def probe(url: str) -> Probe:
        seen.append(url)
        return queue.pop(0)

    probe.seen = seen  # type: ignore[attr-defined]
    return probe


OK = Probe("ok", "reachable, 2 models: qwen3-27b, gemma3-12b", ["qwen3-27b", "gemma3-12b"])
REFUSED = Probe("refused", "refused: nothing is listening on 192.168.1.50:8080")
NO_MODELS = Probe("no-models", "reachable, but it does not list models (is it an OpenAI-compatible server?)")
#: Enter at every page element (the starters) and at the names question.
_ELEMENTS_DEFAULT = ("",) * 7


class WizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "conf" / "config.json"
        from critique_bot import config as _config

        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name, "APPDATA": self.tmp.name, "CRITIQUE_CONFIG_FILE": ""})
        env.start()
        self.addCleanup(env.stop)
        system = patch.object(_config, "system_company_config_path", lambda: Path(self.tmp.name) / "no-system" / "crit.json")
        system.start()
        self.addCleanup(system.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        from critique_bot import egress

        egress._policy = egress.Policy()  # load_config set the process-wide policy
        set_custom_terms(())

    def _run(self, script: _Script, probe=None):
        return run_wizard(self.path, ask=script.ask, say=script.say, probe=probe or _probes())

    def data(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ the choice

    def test_the_choice_is_worded_exactly(self) -> None:
        script = _Script("3", "^C")
        self.assertIsNone(self._run(script))
        self.assertEqual(
            script.said[1:5],
            [
                "How does crit reach the model?",
                "  1) Local model (a model server on this PC or the network)",
                "  2) Web UI (a page in the browser)",
                "(Or use a config file: crit config --config-file PATH)",
            ],
        )
        self.assertEqual(script.asked, [CHOICE_PROMPT, CHOICE_PROMPT])
        self.assertEqual(CHOICE_PROMPT, "Choose 1 or 2: ")
        self.assertNotIn("Config file path (Enter if none): ", script.asked)

    # ------------------------------------------------------------------ local model

    def test_local_on_this_pc_defaults(self) -> None:
        probe = _probes(OK)
        script = _Script("1", "127.0.0.1:8080", "", "")
        result = self._run(script, probe)
        assert result is not None
        self.assertEqual(result.backend, "local")
        self.assertEqual(script.asked[1], SERVER_PROMPT)
        self.assertEqual(script.asked[2], "Model [qwen3-27b]: ")
        self.assertIn("Checking http://127.0.0.1:8080 ...", script.said)
        self.assertIn(OK.line, script.said)
        self.assertEqual(probe.seen, ["http://127.0.0.1:8080"])
        self.assertEqual(self.data(), {"backend": "local", "url": "http://127.0.0.1:8080", "model": "qwen3-27b"})
        config = load_config(self.path)
        self.assertTrue(config.is_local)
        self.assertEqual(config.allowed_hosts, ())
        self.assertFalse(any("send prompts only" in line for line in script.said))
        self.assertEqual(script.said[-3:], [script.said[-3], f"Saved to {self.path}.", CHANGE_LATER])

    def test_remote_server_goes_into_allowed_hosts_model_by_number(self) -> None:
        script = _Script("1", "192.168.1.50:8080", "2", "")
        self.assertIsNotNone(self._run(script, _probes(OK)))
        config = load_config(self.path)
        self.assertEqual(config.url, "http://192.168.1.50:8080")
        self.assertEqual(config.allowed_hosts, ("192.168.1.50",))
        self.assertEqual(config.model, "gemma3-12b")
        self.assertIn("crit will send prompts only to 192.168.1.50.", script.said)
        self.assertIn("  2) gemma3-12b", script.said)

    def test_model_by_name_and_a_full_https_address(self) -> None:
        probe = _probes(OK)
        script = _Script("1", "https://GPU-box.corp.example:8443/llm/", "my-model", "")
        self.assertIsNotNone(self._run(script, probe))
        self.assertEqual(probe.seen, ["https://gpu-box.corp.example:8443/llm"])
        self.assertEqual(self.data()["model"], "my-model")
        self.assertEqual(self.data()["allowed_hosts"], ["gpu-box.corp.example"])

    def test_a_number_out_of_range_is_asked_again(self) -> None:
        script = _Script("1", "localhost:8080", "9", "1", "")
        self._run(script, _probes(OK))
        self.assertEqual(self.data()["model"], "qwen3-27b")
        self.assertEqual(script.asked.count("Model [qwen3-27b]: "), 2)

    def test_no_port_is_refused_and_asked_again(self) -> None:
        script = _Script("1", "192.168.1.50", "192.168.1.50:8080", "", "")
        self._run(script, _probes(OK))
        self.assertEqual(script.asked[1:3], [SERVER_PROMPT, SERVER_PROMPT])
        self.assertTrue(any("with its port" in line for line in script.said))

    def test_refused_then_another_address(self) -> None:
        script = _Script("1", "192.168.1.50:8080", "", "127.0.0.1:8080", "", "")
        self._run(script, _probes(REFUSED, OK))
        self.assertIn(REFUSED.line, script.said)
        self.assertIn("Try another address? [Y/n] ", script.asked)
        self.assertEqual(self.data()["url"], "http://127.0.0.1:8080")
        self.assertNotIn("allowed_hosts", self.data())

    def test_refused_then_save_anyway_asks_for_the_name(self) -> None:
        script = _Script("1", "192.168.1.50:8080", "n", "qwen3", "")
        self.assertIsNotNone(self._run(script, _probes(REFUSED)))
        self.assertIn("Model name: ", script.asked)
        self.assertTrue(any("checks the server again on the next run" in line for line in script.said))
        self.assertEqual(self.data()["model"], "qwen3")
        self.assertEqual(self.data()["allowed_hosts"], ["192.168.1.50"])

    def test_no_model_list_asks_for_the_name(self) -> None:
        script = _Script("1", "localhost:1234", "", "")
        self._run(script, _probes(NO_MODELS))
        self.assertIn(NO_MODELS.line, script.said)
        self.assertNotIn("Try another address? [Y/n] ", script.asked)
        self.assertEqual(self.data()["model"], "")

    # ------------------------------------------------------------------ Web UI

    def test_web_ui_defaults(self) -> None:
        script = _Script("2", "https://ai.corp.example/", "", *_ELEMENTS_DEFAULT, "")
        result = self._run(script)
        assert result is not None
        self.assertEqual(result.backend, "browser")
        self.assertEqual(
            script.asked[1:10],
            [
                "url: ",
                "Model (Enter for the page's default): ",
                f"Message box (prompt_input) [{STARTER_SELECTORS['prompt_input']}]: ",
                f"Send button (send_button) [{STARTER_SELECTORS['send_button']}]: ",
                f"Stop button (stop_button) [{STARTER_SELECTORS['stop_button']}]: ",
                f"Replies (assistant_messages) [{STARTER_SELECTORS['assistant_messages']}]: ",
                "Model picker (model_dropdown) (Enter to skip): ",
                "Model picker identifier (model_dropdown_identifier) (Enter to skip): ",
                "Model option (model_option) (Enter to skip): ",
            ],
        )
        data = self.data()
        self.assertEqual(data["backend"], "browser")
        self.assertEqual(data["egress"], "strict")
        self.assertNotIn("model", data)
        self.assertEqual(data["selectors"]["prompt_input"], STARTER_SELECTORS["prompt_input"])
        self.assertEqual(data["selectors"]["model_dropdown"], "")
        self.assertIn(WEB_UI_DONE, script.said)
        self.assertNotIn(NO_PICKER, script.said)
        config = load_config(self.path)
        self.assertEqual(config.url, "https://ai.corp.example/")
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(any("chat" in line.lower() for line in script.said + script.asked))

    def test_web_ui_overrides_and_a_model_with_a_picker(self) -> None:
        script = _Script(
            "2", "https://ai.corp.example/", "gpt-x",
            "#composer", "button.send", "button.stop", ".reply", "button.models", "", "[role='option']", "",
        )
        self._run(script)
        data = self.data()
        self.assertEqual(data["model"], "gpt-x")
        self.assertEqual(
            data["selectors"],
            {
                "prompt_input": "#composer", "send_button": "button.send", "stop_button": "button.stop",
                "assistant_messages": ".reply", "model_dropdown": "button.models",
                "model_dropdown_identifier": "", "model_option": "[role='option']",
            },
        )
        self.assertNotIn(NO_PICKER, script.said)
        self.assertEqual(load_config(self.path).model, "gpt-x")

    def test_a_model_without_a_picker_says_so(self) -> None:
        script = _Script("2", "https://ai.corp.example/", "gpt-x", *_ELEMENTS_DEFAULT, "")
        self._run(script)
        self.assertEqual(self.data()["model"], "gpt-x")
        self.assertIn(NO_PICKER, script.said)
        self.assertEqual(
            NO_PICKER, "To switch models crit needs the model picker; without it the page's current model is used."
        )

    def test_url_needs_https_and_is_asked_again(self) -> None:
        script = _Script("2", "", "http://ai.corp.example/", "https://ai.corp.example/", "", *_ELEMENTS_DEFAULT, "")
        self._run(script)
        self.assertEqual(script.asked[1:4], ["url: "] * 3)
        self.assertTrue(any("https://" in line for line in script.said))

    def test_required_elements_are_asked_again_without_a_default(self) -> None:
        empty = {**STARTER_SELECTORS, "prompt_input": "", "assistant_messages": ""}
        with patch("critique_bot.setup_wizard.STARTER_SELECTORS", empty):
            script = _Script("2", "https://ai.corp.example/", "", "", "textarea", "", "", "", ".reply", "", "", "", "")
            self._run(script)
        self.assertEqual(script.asked.count("Message box (prompt_input): "), 2)
        self.assertEqual(script.asked.count("Replies (assistant_messages): "), 2)
        self.assertEqual(sum("This one is needed" in line for line in script.said), 2)
        self.assertEqual(self.data()["selectors"]["prompt_input"], "textarea")

    def test_broken_css_is_refused(self) -> None:
        script = _Script("2", "https://ai.corp.example/", "", "div[role='textbox'", "textarea", *("",) * 6, "")
        self._run(script)
        self.assertTrue(any("not a CSS selector" in line for line in script.said))
        self.assertEqual(self.data()["selectors"]["prompt_input"], "textarea")
        self.assertIsNotNone(css_problem("a)"))
        self.assertIsNotNone(css_problem("[x='y]"))
        self.assertIsNotNone(css_problem("> a"))
        self.assertIsNone(css_problem("button[aria-label*='Send' i], .x > y"))
        self.assertIsNone(css_problem(STARTER_SELECTORS["assistant_messages"]))

    # ------------------------------------------------------------------ after either path

    def test_names_to_hide_are_saved_and_registered(self) -> None:
        script = _Script("2", "https://ai.corp.example/", "", *_ELEMENTS_DEFAULT, "Globex, ab", "Acme Corp, Globex, acme corp")
        self.assertIsNotNone(self._run(script))
        self.assertTrue(any("too short" in line for line in script.said))
        self.assertEqual(self.data()["redact_terms"], ["Acme Corp", "Globex"])
        config = load_config(self.path)
        self.assertEqual(config.redact_terms, ("Acme Corp", "Globex"))
        self.assertEqual(redact_outgoing("AcmeCorpClient for globex"), "[REDACTED:name]Client for [REDACTED:name]")
        self.assertTrue(any(line.startswith("Personal data") for line in script.said))

    def test_ctrl_c_anywhere_writes_nothing(self) -> None:
        for answers in (("^C",), ("1", "^D"), ("2", "https://ai.corp.example/", "^C"), ("2", "https://ai.corp.example/", "", *_ELEMENTS_DEFAULT, "^C")):
            with self.subTest(answers=answers):
                script = _Script(*answers)
                self.assertIsNone(self._run(script, _probes(OK)))
                self.assertFalse(self.path.exists())
                self.assertEqual(script.said[-1], STOPPED)
                self.assertEqual(STOPPED, "Stopped; nothing was written.")

    def test_ctrl_c_keeps_an_earlier_file_as_it_was(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"theme": "light"}', encoding="utf-8")
        self.assertIsNone(self._run(_Script("1", "localhost:8080", "", "^C"), _probes(OK)))
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{"theme": "light"}')

    def test_preferences_in_the_file_are_kept(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"theme": "light"}', encoding="utf-8")
        self._run(_Script("1", "localhost:8080", "", ""), _probes(OK))
        self.assertEqual(self.data()["theme"], "light")

    def test_default_path_is_the_user_config(self) -> None:
        from critique_bot.config import user_config_path
        from critique_bot.setup_wizard import default_config_path

        self.assertEqual(default_config_path(), user_config_path())


class ServerAddressTests(unittest.TestCase):
    def test_addresses(self) -> None:
        self.assertEqual(parse_server("192.168.1.50:8080"), "http://192.168.1.50:8080")
        self.assertEqual(parse_server("GPU-Box.corp.example:11434"), "http://gpu-box.corp.example:11434")
        self.assertEqual(parse_server("[fe80::1]:8080"), "http://[fe80::1]:8080")
        self.assertEqual(parse_server("https://gpu.example/v1/"), "https://gpu.example/v1")
        self.assertEqual(parse_server("http://gpu.example:8080"), "http://gpu.example:8080")
        for bad in ("", "192.168.1.50", "gpu-box", "bad host!:80", "ftp://x:1", "x:notaport", "http://u:p@x:1", "x:80/path"):
            self.assertIsNone(parse_server(bad), bad)


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


def _connection(responses: dict[str, object]):
    """A fake http.client connection class: path -> _Response or an exception."""
    calls: list[tuple[str, str, float]] = []

    class Conn:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            self.host, self.port, self.timeout = host, port, timeout
            self.path = ""

        def request(self, method: str, path: str, headers=None) -> None:
            calls.append((method, path, self.timeout))
            self.path = path

        def getresponse(self):
            answer = responses.get(self.path, _Response(404, b""))
            if isinstance(answer, BaseException):
                raise answer
            return answer

        def close(self) -> None:
            pass

    Conn.calls = calls  # type: ignore[attr-defined]
    return Conn


class ProbeTests(unittest.TestCase):
    """Every probe outcome, with the socket and http.client mocked."""

    def _probe(self, url: str, *, connect=None, responses=None) -> tuple[Probe, list]:
        conn = _connection(responses or {})
        sock = MagicMock()
        create = MagicMock(side_effect=connect) if connect else MagicMock(return_value=sock)
        with patch.object(local_model.socket, "create_connection", create), patch.object(
            local_model.http.client, "HTTPConnection", conn
        ), patch.object(local_model.http.client, "HTTPSConnection", conn):
            result = probe_server(url)
        if not connect:
            self.assertEqual(create.call_args.kwargs["timeout"], 3.0)
        return result, conn.calls

    def test_models_listed(self) -> None:
        body = json.dumps({"data": [{"id": f"m{i}"} for i in range(7)]}).encode()
        result, calls = self._probe("http://10.0.0.5:8080", responses={"/v1/models": _Response(200, body)})
        self.assertEqual(result.line, "reachable, 7 models: m0, m1, m2, m3, m4, ...")
        self.assertTrue(result.reachable)
        self.assertEqual(result.models[:2], ["m0", "m1"])
        self.assertEqual(calls, [("GET", "/v1/models", 5.0)])

    def test_falls_back_to_models_then_health(self) -> None:
        body = json.dumps({"models": [{"name": "a"}, {"name": "b"}]}).encode()
        result, calls = self._probe("http://10.0.0.5:8080/api", responses={"/api/models": _Response(200, body)})
        self.assertEqual(result.line, "reachable, 2 models: a, b")
        self.assertEqual([c[1] for c in calls], ["/api/v1/models", "/api/models"])

    def test_reachable_without_a_model_list(self) -> None:
        result, calls = self._probe(
            "http://10.0.0.5:8080",
            responses={"/v1/models": http.client.RemoteDisconnected("x"), "/health": _Response(200, b"ok")},
        )
        self.assertEqual(result.line, "reachable, but it does not list models (is it an OpenAI-compatible server?)")
        self.assertTrue(result.reachable)
        self.assertEqual([c[1] for c in calls], ["/v1/models", "/models", "/health"])

    def test_refused(self) -> None:
        result, _ = self._probe("http://10.0.0.5:8080", connect=ConnectionRefusedError())
        self.assertEqual(result.line, "refused: nothing is listening on 10.0.0.5:8080")
        self.assertFalse(result.reachable)

    def test_timed_out(self) -> None:
        result, _ = self._probe("http://10.0.0.5:8080", connect=socket.timeout())
        self.assertEqual(result.line, "timed out: 10.0.0.5:8080 does not answer (firewall or wrong address?)")

    def test_unknown_host(self) -> None:
        result, _ = self._probe("http://nope.invalid:8080", connect=socket.gaierror())
        self.assertEqual(result.line, "unknown host: nope.invalid")

    def test_site_check(self) -> None:
        with patch.object(local_model.socket, "getaddrinfo", return_value=[]), patch.object(
            local_model.socket, "create_connection", return_value=MagicMock()
        ):
            self.assertEqual(probe_site("https://ai.corp.example/").line, "reachable: ai.corp.example resolves and answers on port 443")
        with patch.object(local_model.socket, "getaddrinfo", side_effect=socket.gaierror()):
            self.assertEqual(probe_site("https://ai.corp.example/").line, "unknown host: ai.corp.example")


if __name__ == "__main__":
    unittest.main()
