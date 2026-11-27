# SPDX-License-Identifier: Apache-2.0
"""The local model server's API key (asked only when needed, never shown or logged) and the
context window read from the server."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from critique_bot import config, config_cmd, local_model, log, model_api, redaction
from critique_bot.config import load_config
from critique_bot.local_model import LocalModelClient, LocalModelError, probe_server
from critique_bot.setup_wizard import run_wizard

KEY = "sk-local-7f3a9c2e1b"


class _Server:
    """A model server that wants ``Authorization: Bearer KEY`` (or no key when ``key`` is None)."""

    def __init__(self, key: str | None = KEY, *, props: dict | None = None, models: list[dict] | None = None) -> None:
        self.seen: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def _send(self, status: int, data: object) -> None:
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                owner.seen.append(self.headers.get("Authorization") or "")
                if key is not None and self.headers.get("Authorization") != f"Bearer {key}":
                    self._send(401, {"error": "unauthorized"})
                    return
                if self.path == "/props":
                    if props is None:
                        self._send(404, {"error": "no"})
                    else:
                        self._send(200, props)
                    return
                if self.path in ("/v1/models", "/models"):
                    self._send(200, {"data": models if models is not None else [{"id": "qwen3"}]})
                    return
                self._send(404, {})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _Script:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []
        self.said: list[str] = []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.answers.pop(0)

    def say(self, text: str) -> None:
        self.said.append(text)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"CRIT_HOME": str(self.root / ".crit"), "CRIT_API_KEY": ""})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CRIT_API_KEY", None)
        self.addCleanup(redaction._secret_values.clear)
        local_model._reported_ctx.clear()
        self.addCleanup(local_model._reported_ctx.clear)

    def server(self, *args, **kwargs) -> _Server:
        server = _Server(*args, **kwargs)
        self.addCleanup(server.close)
        return server

    def log_text(self) -> str:
        path = log.log_file_path()
        return path.read_text(encoding="utf-8") if path.exists() else ""


class WizardKeyTests(_Base):
    def _wizard(self, server: _Server, *, answers: tuple[str, ...], secrets: tuple[str, ...]):
        script = _Script(*answers)
        keys = list(secrets)
        asked_secret: list[str] = []

        def ask_secret(prompt: str) -> str:
            asked_secret.append(prompt)
            return keys.pop(0)

        target = config.user_config_path()
        with patch("builtins.input", side_effect=AssertionError("echoing input used for the key")):
            result = run_wizard(target, ask=script.ask, say=script.say, ask_secret=ask_secret)
        return result, script, asked_secret, target

    def test_401_asks_for_the_key_then_goes_on(self) -> None:
        server = self.server()
        host = server.url.split("//", 1)[1]
        result, script, secret, target = self._wizard(server, answers=("1", host, "", ""), secrets=(KEY,))
        self.assertIsNotNone(result)
        self.assertIn(f"Checking {server.url} ...", script.said)
        self.assertIn("the server needs an API key", script.said)
        self.assertEqual(secret, ["API key: "])
        self.assertNotIn(KEY, "\n".join(script.said + script.asked))
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data["local"]["api_key"], KEY)
        if os.name != "nt":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(load_config(target).local.api_key, KEY)

    def test_a_wrong_key_is_asked_again_then_offers_another(self) -> None:
        server = self.server()
        host = server.url.split("//", 1)[1]
        # wrong, wrong -> "Try another key?" yes -> right
        result, script, secret, target = self._wizard(
            server, answers=("1", host, "", "", ""), secrets=("wrong-1", "wrong-2", KEY)
        )
        self.assertIsNotNone(result)
        self.assertEqual(secret, ["API key: "] * 3)
        self.assertIn("Try another key? [Y/n] ", script.asked)
        self.assertEqual(script.said.count("the server refused the API key (401)"), 2)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["local"]["api_key"], KEY)

    def test_giving_up_saves_without_a_key(self) -> None:
        server = self.server()
        host = server.url.split("//", 1)[1]
        result, script, _secret, target = self._wizard(
            server, answers=("1", host, "n", "n", "qwen3", ""), secrets=("wrong-1", "wrong-2")
        )
        self.assertNotIn("local", json.loads(target.read_text(encoding="utf-8")))

    def test_a_server_without_a_key_asks_nothing(self) -> None:
        server = self.server(None)
        host = server.url.split("//", 1)[1]
        result, _script, secret, target = self._wizard(server, answers=("1", host, "", ""), secrets=())
        self.assertIsNotNone(result)
        self.assertEqual(secret, [])
        self.assertNotIn("local", json.loads(target.read_text(encoding="utf-8")))


class SendingTests(_Base):
    def _config(self, url: str, data: dict | None = None) -> Path:
        path = self.root / "config.json"
        path.write_text(json.dumps({"backend": "local", "url": url, "model": "qwen3", **(data or {})}), encoding="utf-8")
        return path

    def test_the_key_goes_to_the_server_and_never_to_the_log(self) -> None:
        server = self.server()
        cfg = load_config(self._config(server.url, {"local": {"api_key": KEY}}))
        self.assertNotIn(KEY, repr(cfg))
        self.assertEqual(LocalModelClient(cfg).list_models(), ["qwen3"])
        self.assertEqual(server.seen[-1], f"Bearer {KEY}")
        log.info(f"something mentions {KEY} by mistake")
        self.assertNotIn(KEY, self.log_text())
        self.assertIn("[REDACTED:api_key]", self.log_text())
        self.assertNotIn(KEY, redaction.redact_outgoing(f"the key is {KEY}"))

    def test_without_the_key_the_error_says_how_to_set_it(self) -> None:
        server = self.server()
        cfg = load_config(self._config(server.url))
        with self.assertRaises(LocalModelError) as err:
            LocalModelClient(cfg).list_models()
        self.assertIn("crit config --api-key", str(err.exception))

    def test_env_key_wins(self) -> None:
        server = self.server()
        with patch.dict(os.environ, {"CRIT_API_KEY": KEY}):
            # This config is not the user's own: the env key goes only to a server it is bound to (SF-4).
            cfg = load_config(self._config(server.url, {"local": {"api_key": "old-one-1234"}}))
            self.assertEqual(cfg.local.api_key, "old-one-1234")
            with patch.dict(os.environ, {"CRIT_API_KEY_URL": server.url}):
                cfg = load_config(self._config(server.url, {"local": {"api_key": "old-one-1234"}}))
        self.assertEqual(cfg.local.api_key, KEY)
        self.assertEqual(LocalModelClient(cfg).list_models(), ["qwen3"])

    def test_a_shared_config_files_key_is_ignored(self) -> None:
        team = self.root / "share" / "crit.json"
        team.parent.mkdir()
        team.write_text(json.dumps({"backend": "local", "url": "http://127.0.0.1:9", "local": {"api_key": "team-key-1234"}}), encoding="utf-8")
        own = self.root / "own.json"
        own.write_text(json.dumps({"config_file": str(team)}), encoding="utf-8")
        cfg = load_config(own)
        self.assertEqual(cfg.local.api_key, "")
        self.assertIn("local.api_key in the config file", self.log_text())
        self.assertNotIn("team-key-1234", self.log_text())
        # The shared file used directly as the config: ignored too.
        with patch.dict(os.environ, {"CRIT_CONFIG_FILE": str(team)}):
            self.assertEqual(load_config(team).local.api_key, "")

    def test_probe_sends_the_key_only_when_given(self) -> None:
        server = self.server()
        self.assertEqual(probe_server(server.url).outcome, local_model.PROBE_NEEDS_KEY)
        self.assertEqual(server.seen[-1], "")
        self.assertEqual(probe_server(server.url, KEY).outcome, local_model.PROBE_OK)


class ConfigCommandTests(_Base):
    def test_set_ask_show_and_remove(self) -> None:
        user = config.user_config_path()
        out = io.StringIO()
        with patch("getpass.getpass", return_value=KEY) as asked, redirect_stdout(out):
            self.assertEqual(config_cmd.main(["--config", str(self.root / "c.json"), "--api-key"]), 0)
        asked.assert_called_once()
        self.assertNotIn(KEY, out.getvalue())
        self.assertEqual(json.loads(user.read_text())["local"]["api_key"], KEY)
        with patch("sys.stdin", io.StringIO("from-stdin-5678\n")), redirect_stdout(io.StringIO()):
            self.assertEqual(config_cmd.main(["--config", str(self.root / "c.json"), "--api-key", "-"]), 0)
        self.assertEqual(json.loads(user.read_text())["local"]["api_key"], "from-stdin-5678")
        data = json.loads(user.read_text())
        data.update({"backend": "local", "url": "http://127.0.0.1:8080"})
        user.write_text(json.dumps(data))
        shown = io.StringIO()
        with redirect_stdout(shown):
            config_cmd.main(["--config", str(user)])
        self.assertIn("API key: set", shown.getvalue())
        self.assertNotIn("from-stdin-5678", shown.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(config_cmd.main(["--config", str(self.root / "c.json"), "--api-key", "none"]), 0)
        self.assertNotIn("api_key", json.loads(user.read_text()).get("local", {}))
        shown = io.StringIO()
        with redirect_stdout(shown):
            config_cmd.main(["--config", str(user)])
        self.assertIn("API key: not set", shown.getvalue())


class ContextWindowTests(_Base):
    def _cfg(self, url: str):
        path = self.root / "config.json"
        path.write_text(json.dumps({"backend": "local", "url": url, "model": "qwen3"}), encoding="utf-8")
        return load_config(path)

    def test_llama_cpp_props(self) -> None:
        server = self.server(None, props={"default_generation_settings": {"n_ctx": 65536}})
        self.assertEqual(model_api.context_tokens(self._cfg(server.url)), 65536)

    def test_vllm_max_model_len(self) -> None:
        server = self.server(None, models=[{"id": "other", "max_model_len": 4096}, {"id": "qwen3", "max_model_len": 131072}])
        self.assertEqual(model_api.context_tokens(self._cfg(server.url)), 131072)

    def test_nothing_reported_keeps_the_default(self) -> None:
        server = self.server(None)
        self.assertEqual(model_api.context_tokens(self._cfg(server.url)), local_model.DEFAULT_NUM_CTX)

    def test_a_set_num_ctx_is_not_asked(self) -> None:
        path = self.root / "config.json"
        path.write_text(json.dumps({"backend": "local", "url": "http://127.0.0.1:9", "local": {"num_ctx": 8192}}), encoding="utf-8")
        with patch.object(local_model, "_get_json", side_effect=AssertionError("asked the server")):
            self.assertEqual(model_api.context_tokens(load_config(path)), 8192)

    def test_with_a_key(self) -> None:
        server = self.server(KEY, props={"default_generation_settings": {"n_ctx": 16384}})
        path = self.root / "config.json"
        path.write_text(json.dumps({"backend": "local", "url": server.url, "local": {"api_key": KEY}}), encoding="utf-8")
        self.assertEqual(model_api.context_tokens(load_config(path)), 16384)


if __name__ == "__main__":
    unittest.main()
