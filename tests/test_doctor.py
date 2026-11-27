"""crit doctor model against fake servers: server detection, the 12-turn probe scoring
(native, recovered by the fallback parser, failed), the cache, --write-profile, and the CLI."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from critique_bot import doctor, profiles
from critique_bot.config import BotConfig, LocalConfig
from critique_bot.local_model import _reported_ctx, detect_server

from test_native_tools import _call_chunks


def _answer(messages: list[dict[str, Any]]) -> tuple[str, list[tuple[str, dict]]]:
    """What a well-behaved model replies: (text, calls)."""
    last = messages[-1]
    if last["role"] == "tool":
        return "", [("read_file", {"path": "src/settings/loader.py"})]
    text = last["content"]
    if "17 + 25" in text:
        return "42", []
    if "src/a.py and src/b.py" in text:
        return "", [("read_file", {"path": "src/a.py"}), ("read_file", {"path": "src/b.py"})]
    if "quote.py" in text:
        return "", [("write_file", {"path": "quote.py", "content": doctor.CODE})]
    if "fox.txt" in text:
        return "", [("write_file", {"path": "fox.txt", "content": doctor.LONG})]
    if "notes.txt" in text:
        return "", [("write_file", {"path": "notes.txt", "content": doctor.UNICODE})]
    if "HTTP routes" in text:
        return "", [("read_file", {"path": "src/routes.py"})]
    if "regex mode" in text:
        return "", [("search", {"pattern": "TODO\\(\\w+\\)", "mode": "regex"})]
    if "pytest -q" in text:
        return "", [("run_command", {"command": "pytest -q", "timeout_s": 120, "background": False})]
    if "debug = True" in text:
        return "", [("edit_file", {"path": "src/app.py", "edits": [{"old": "debug = True", "new": "debug = False"},
                                                                    {"old": "port = 80", "new": "port = 8080"}]})]
    if "FIXME" in text:
        return "", [("search", {"pattern": "FIXME"})]
    return "", [("read_file", {"path": "src/app.py"})]


class _Fake:
    """A llama.cpp-like server; ``style`` picks how calls come back: native, text (Hermes in
    the content, for the fallback parser), or none (the server drops every call)."""

    def __init__(self, style: str = "native", props: dict | None = None,
                 get: dict[str, Any] | None = None) -> None:
        self.style = style
        self.requests: list[dict] = []
        outer = self
        routes = {"/v1/models": {"data": [{"id": "qwen3.6-27b"}]}}
        if props is not None:
            routes["/props"] = props
        routes.update(get or {})

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                if self.path not in routes:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps(routes[self.path]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append(request)
                text, calls = _answer(request["messages"])
                events: list[dict] = []
                if calls and outer.style == "native":
                    events += _call_chunks([(f"c{i}", n, a) for i, (n, a) in enumerate(calls)])
                elif calls and outer.style == "text":
                    text = "".join(f"<tool_call>{json.dumps({'name': n, 'arguments': a})}</tool_call>" for n, a in calls)
                if text:
                    events.append({"choices": [{"index": 0, "delta": {"content": text}}]})
                events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in events:
                    self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        host, port = self.httpd.server_address[:2]
        self.url = f"http://{host}:{port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _config(url: str, model: str = "qwen3.6-27b") -> BotConfig:
    return BotConfig(url=url, selectors=None, model=model, backend="local", local=LocalConfig(),
                     reply_start_ms=5_000, reply_stall_ms=5_000, reply_max_ms=20_000)


class _Home:
    def __enter__(self) -> Path:
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("CRIT_HOME")
        os.environ["CRIT_HOME"] = self.tmp.name
        _reported_ctx.clear()
        return Path(self.tmp.name)

    def __exit__(self, *exc: object) -> None:
        if self.old is None:
            os.environ.pop("CRIT_HOME", None)
        else:
            os.environ["CRIT_HOME"] = self.old
        self.tmp.cleanup()
        _reported_ctx.clear()


_PROPS = {"default_generation_settings": {"n_ctx": 32768}, "build_info": "b6000",
          "chat_template": "{% if tools %}...{% endif %}"}


def _run(fake: _Fake, **kwargs: Any) -> tuple[int, str]:
    lines: list[str] = []
    code = doctor.run_model(_config(fake.url), say=lines.append, **kwargs)
    return code, "\n".join(lines)


class ProbeTests(unittest.TestCase):
    def test_native_server_scores_12_of_12_and_caches_native(self) -> None:
        fake = _Fake("native", props=_PROPS)
        try:
            with _Home() as home:
                code, out = _run(fake)
                cache = json.loads((home / "cache" / "doctor.json").read_text())
        finally:
            fake.stop()
        self.assertEqual(code, 0, out)
        self.assertIn("12 native-ok, 0 recovered, 0 failed", out)
        self.assertIn("recommend tool_mode native", out)
        self.assertIn("type       llama.cpp b6000", out)
        self.assertIn("context    32768 tokens", out)
        self.assertIn("llama-server -m qwen3.6-27b --jinja", out)
        entry = cache[f"{fake.url}|qwen3.6-27b"]
        self.assertEqual(entry["recommend"], "native")
        # the probe used the profile's sampling and real tools
        self.assertEqual(fake.requests[0]["temperature"], 0.6)
        self.assertEqual(len(fake.requests), len(doctor.SCENARIOS))

    def test_calls_in_the_text_are_recovered_and_still_recommend_native_only_if_structured(self) -> None:
        fake = _Fake("text", props=_PROPS)
        try:
            with _Home():
                code, out = _run(fake)
        finally:
            fake.stop()
        self.assertEqual(code, 0)
        self.assertIn("1 native-ok, 11 recovered, 0 failed", out)  # only the no-call answer is native
        self.assertIn("recommend tool_mode prompted", out)

    def test_a_server_that_drops_calls_fails_and_auto_switches_to_prompted(self) -> None:
        fake = _Fake("none", props=_PROPS)
        try:
            with _Home():
                code, out = _run(fake, model="mystery-model")
                config = _config(fake.url, "mystery-model")
                from critique_bot import model_api

                self.assertFalse(model_api.native_tools_ok(config))
        finally:
            fake.stop()
        self.assertEqual(code, 1)
        self.assertIn("11 failed", out)

    def test_q4_kv_cache_and_a_template_without_tools_warn(self) -> None:
        props = {**_PROPS, "chat_template": "{{ messages }}", "default_generation_settings": {
            "n_ctx": 8192, "cache_type_k": "q4_0", "cache_type_v": "q4_0"}}
        fake = _Fake("native", props=props)
        try:
            with _Home():
                code, out = _run(fake, probe=False)
        finally:
            fake.stop()
        self.assertEqual(code, 0)
        self.assertIn("kv cache   q4_0", out)
        self.assertIn("WARNING    the KV cache is q4_0", out)
        self.assertIn("no tools section", out)

    def test_write_profile_saves_a_starter_user_profile(self) -> None:
        fake = _Fake("native", props=_PROPS)
        try:
            with _Home() as home:
                code, out = _run(fake, model="Org/New-Model-9B", write=True)
                saved = json.loads((home / "profiles" / "new-model-9b.json").read_text())
                again, out2 = _run(fake, model="Org/New-Model-9B", write=True)
                self.assertEqual(profiles.resolve("Org/New-Model-9B").id, "new-model-9b")
        finally:
            fake.stop()
        self.assertEqual(saved["tool_mode"], "native")
        self.assertIn("org/new-model-9b", saved["match"])
        self.assertNotIn("extends", saved)  # it matched generic
        self.assertIn("pass --force", out2)

    def test_write_profile_for_a_builtin_id_overrides_it(self) -> None:
        fake = _Fake("text", props=_PROPS)
        try:
            with _Home() as home:
                _run(fake, write=True)
                saved = json.loads((home / "profiles" / "qwen3.6-27b.json").read_text())
                found = profiles.resolve("qwen3.6-27b")
        finally:
            fake.stop()
        self.assertEqual(set(saved), {"id", "tool_mode", "notes"})
        self.assertEqual((found.origin, found.tool_mode), ("builtin+user", "prompted"))
        self.assertEqual(found.sampling_dict()["top_k"], 20)

    def test_unreachable_server(self) -> None:
        lines: list[str] = []
        with _Home():
            code = doctor.run_model(_config("http://127.0.0.1:9"), say=lines.append)
        self.assertEqual(code, 1)
        self.assertIn("refused", "\n".join(lines))


class DetectServerTests(unittest.TestCase):
    def _detect(self, get: dict[str, Any]) -> dict[str, Any]:
        fake = _Fake(get=get)
        try:
            with _Home():
                return detect_server(fake.url, None, "m")
        finally:
            fake.stop()

    def test_each_server_type(self) -> None:
        self.assertEqual(self._detect({"/version": {"version": "0.11.0"}})["type"], "vllm")
        sglang = self._detect({"/get_server_info": {"version": "0.5", "context_length": 65536,
                                                    "tool_call_parser": "qwen3_coder", "speculative_algorithm": "EAGLE"}})
        self.assertEqual((sglang["type"], sglang["context"], sglang["tool_call_parser"], sglang["speculation"]),
                         ("sglang", 65536, "qwen3_coder", "EAGLE"))
        self.assertEqual(self._detect({"/api/version": {"version": "0.12.0"}})["type"], "ollama")
        lms = self._detect({"/api/v0/models": {"data": [{"id": "m", "state": "loaded", "max_context_length": 40960}]}})
        self.assertEqual((lms["type"], lms["context"]), ("lmstudio", 40960))
        tgi = self._detect({"/info": {"model_id": "m", "max_total_tokens": 8192, "version": "3.0"}})
        self.assertEqual((tgi["type"], tgi["context"]), ("tgi", 8192))
        self.assertEqual(self._detect({})["type"], "generic")
        self.assertEqual(detect_server("https://openrouter.ai/api")["type"], "openrouter")


class ScoringTests(unittest.TestCase):
    def test_score_turn_cases(self) -> None:
        single = doctor.SCENARIOS[0]
        ok = {"native": [{"name": "read_file", "arguments": {"path": "src/app.py"}, "arguments_ok": True}]}
        self.assertEqual(doctor.score_turn(single, ok)[0], doctor.NATIVE_OK)
        bad_json = {"native": [{"name": "read_file", "arguments": "{oops", "arguments_ok": False}]}
        self.assertEqual(doctor.score_turn(single, bad_json)[0], doctor.FAILED)
        wrong = {"native": [{"name": "read_file", "arguments": {"path": "x.py"}, "arguments_ok": True}]}
        self.assertEqual(doctor.score_turn(single, wrong)[0], doctor.FAILED)
        rec = {"recovered": [{"name": "read_file", "arguments": {"path": "src/app.py"}, "source": "qwen-xml"}]}
        self.assertEqual(doctor.score_turn(single, rec), (doctor.RECOVERED, "from the qwen-xml format"))
        self.assertEqual(doctor.score_turn(single, {"error": "boom"})[0], doctor.FAILED)
        typed = next(s for s in doctor.SCENARIOS if s.name == "typed_arguments")
        as_string = {"native": [{"name": "run_command", "arguments": {"command": "pytest -q", "timeout_s": "120"},
                                 "arguments_ok": True}]}
        self.assertEqual(doctor.score_turn(typed, as_string)[0], doctor.FAILED)
        self.assertEqual(len(doctor.SCENARIOS), 12)
        self.assertGreater(len(doctor.LONG.encode()), 4096)

    def test_recommend(self) -> None:
        self.assertEqual(doctor.recommend({doctor.NATIVE_OK: 11, doctor.RECOVERED: 0, doctor.FAILED: 1}), "native")
        self.assertEqual(doctor.recommend({doctor.NATIVE_OK: 9, doctor.RECOVERED: 3, doctor.FAILED: 0}), "native")
        self.assertEqual(doctor.recommend({doctor.NATIVE_OK: 10, doctor.RECOVERED: 0, doctor.FAILED: 2}), "prompted")
        self.assertEqual(doctor.recommend({doctor.NATIVE_OK: 1, doctor.RECOVERED: 11, doctor.FAILED: 0}), "prompted")


class CliTests(unittest.TestCase):
    def test_crit_doctor_model_routes_with_a_config_file(self) -> None:
        from critique_bot import cli

        fake = _Fake("native", props=_PROPS)
        try:
            with _Home() as home:
                path = home / "config.json"
                path.write_text(json.dumps({"backend": "local", "url": fake.url, "model": "qwen3.6-27b"}))
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = cli.main(["doctor", "model", "--config", str(path), "--no-probe", "--json"])
        finally:
            fake.stop()
        self.assertEqual(code, 0)
        report = json.loads(buffer.getvalue())
        self.assertEqual((report["server"]["type"], report["profile"]), ("llama.cpp", "qwen3.6-27b"))
        self.assertNotIn("probe", report)


if __name__ == "__main__":
    unittest.main()
