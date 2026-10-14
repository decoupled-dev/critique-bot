"""The local-model backend: config, the adapter, the two wire formats, and the agent loop on it."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from rich.console import Console

from critique_bot import agent_ui
from critique_bot.agent import _loop_settings, _model_label, run_agent_loop
from critique_bot.chat_client import COMPLETION_IDLE, COMPLETION_STOPPED, ChatError
from critique_bot.config import BotConfig, ConfigError, LocalConfig, load_config
from critique_bot.local_model import (
    DEFAULT_NUM_CTX,
    LocalModelError,
    LocalModelProvider,
    LocalModelSession,
    server_label,
    strip_thinking,
)
from critique_bot.provider import BrowserProvider, open_provider


class _FakeServer:
    """An Ollama-and-OpenAI-shaped server on 127.0.0.1 with a scripted reply per request.

    ``replies`` is a list of strings; ``"ERROR:<text>"`` streams an error event instead,
    ``"HTTP:<code>:<text>"`` answers with that status, and ``"STALL"`` sends one chunk
    and then nothing.
    """

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.models = ["qwen2.5-coder:7b", "gemma3:4b"]
        self.chunk = 4
        self.stall_seconds = 3.0
        self.reasoning = ""
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def do_GET(self) -> None:
                if self.path == "/api/tags":
                    self._json({"models": [{"name": name, "model": name} for name in server.models]})
                elif self.path == "/v1/models":
                    self._json({"object": "list", "data": [{"id": name} for name in server.models]})
                else:
                    self._json({"error": "not found"}, status=404)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                server.requests.append({"path": self.path, "body": body})
                reply = server.replies.pop(0) if server.replies else "COMPLETED"
                if reply.startswith("HTTP:"):
                    _, code, text = reply.split(":", 2)
                    self._json({"error": text}, status=int(code))
                    return
                if self.path == "/api/chat":
                    self._stream_ollama(body, reply)
                elif self.path == "/v1/chat/completions":
                    self._stream_openai(body, reply)
                else:
                    self._json({"error": "not found"}, status=404)

            def _json(self, payload: dict, status: int = 200) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _start_stream(self, content_type: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

            def _chunked(self, data: bytes) -> None:
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

            def _end(self) -> None:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            def _pieces(self, reply: str) -> list[str]:
                return [reply[i : i + server.chunk] for i in range(0, len(reply), server.chunk)] or [""]

            def _stream_ollama(self, body: dict, reply: str) -> None:
                self._start_stream("application/x-ndjson")
                model = body.get("model")
                if reply.startswith("ERROR:"):
                    self._chunked(json.dumps({"error": reply[6:]}).encode() + b"\n")
                    self._end()
                    return
                if reply == "STALL":
                    self._chunked(json.dumps({"model": model, "message": {"role": "assistant", "content": "part"}, "done": False}).encode() + b"\n")
                    time.sleep(server.stall_seconds)
                    self._end()
                    return
                if server.reasoning:
                    self._chunked(json.dumps({"model": model, "message": {"role": "assistant", "content": "", "thinking": server.reasoning}, "done": False}).encode() + b"\n")
                for piece in self._pieces(reply):
                    self._chunked(json.dumps({"model": model, "message": {"role": "assistant", "content": piece}, "done": False}).encode() + b"\n")
                final = {
                    "model": model,
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": sum(len(m.get("content", "")) for m in body.get("messages", [])) // 4,
                    "eval_count": len(reply) // 4,
                }
                self._chunked(json.dumps(final).encode() + b"\n")
                self._end()

            def _stream_openai(self, body: dict, reply: str) -> None:
                self._start_stream("text/event-stream")
                if reply.startswith("ERROR:"):
                    self._chunked(b"data: " + json.dumps({"error": {"message": reply[6:]}}).encode() + b"\n\n")
                    self._end()
                    return
                for piece in self._pieces(reply):
                    event = {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                    self._chunked(b"data: " + json.dumps(event).encode() + b"\n\n")
                last = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
                self._chunked(b"data: " + json.dumps(last).encode() + b"\n\n")
                self._chunked(b"data: [DONE]\n\n")
                self._end()

        class Server(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                pass  # a client that hangs up mid-reply (the stall test) is expected

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "_FakeServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _config(url: str, *, api: str = "ollama", model: str = "qwen2.5-coder:7b", **overrides: Any) -> BotConfig:
    values: dict[str, Any] = dict(
        url=url,
        selectors=None,  # type: ignore[arg-type]
        model=model,
        backend="local",
        local=LocalConfig(api=api, num_ctx=overrides.pop("num_ctx", 0)),
        reply_start_ms=1_000,
        reply_stall_ms=1_000,
        reply_max_ms=20_000,
    )
    values.update(overrides)
    return BotConfig(**values)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load(self, payload: dict) -> BotConfig:
        path = self.folder / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_config(path)

    def test_local_backend_needs_no_selectors_and_defaults_to_ollama(self) -> None:
        config = self._load({"backend": "local", "model": "gemma3:12b"})
        self.assertTrue(config.is_local)
        self.assertEqual(config.url, "http://127.0.0.1:11434")
        self.assertEqual(config.local.api, "ollama")
        self.assertEqual(config.local.num_ctx, 0)
        self.assertEqual(config.model, "gemma3:12b")

    def test_openai_api_is_guessed_from_another_port_and_can_be_set(self) -> None:
        config = self._load({"backend": "local", "url": "http://localhost:8080/"})
        self.assertEqual(config.url, "http://localhost:8080")
        self.assertEqual(config.local.api, "openai")
        config = self._load({"backend": "lmstudio", "url": "http://127.0.0.1:1234/v1", "local": {"api": "openai-compatible"}})
        self.assertEqual(config.local.api, "openai")
        self.assertEqual(config.url, "http://127.0.0.1:1234/v1")
        config = self._load({"backend": "local", "url": "127.0.0.1:9000", "local": {"api": "ollama", "num_ctx": 8192, "temperature": 0.1, "keep_alive_seconds": 300}})
        self.assertEqual(config.url, "http://127.0.0.1:9000")
        self.assertEqual(config.local, LocalConfig(api="ollama", num_ctx=8192, temperature=0.1, keep_alive_seconds=300))

    def test_a_model_on_another_machine_needs_allowed_hosts(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self._load({"backend": "local", "url": "http://gpu-box.lan:11434"})
        self.assertIn("allowed_hosts", str(ctx.exception))
        config = self._load({"backend": "local", "url": "http://gpu-box.lan:11434", "allowed_hosts": ["gpu-box.lan"]})
        self.assertEqual(config.url, "http://gpu-box.lan:11434")
        with self.assertRaises(ConfigError):
            self._load({"backend": "local", "url": "ftp://127.0.0.1/"})
        with self.assertRaises(ConfigError):
            self._load({"backend": "local", "url": "http://user:pw@127.0.0.1:11434"})
        with self.assertRaises(ConfigError) as ctx:
            self._load({"backend": "local", "local": {"api": "grpc"}})
        self.assertIn("local.api", str(ctx.exception))
        with self.assertRaises(ConfigError):
            self._load({"backend": "local", "local": "ollama"})

    def test_browser_config_is_unchanged(self) -> None:
        config = self._load({"url": "https://chat.example/", "selectors": {"prompt_input": "t", "assistant_messages": ".a"}})
        self.assertEqual(config.backend, "browser")
        self.assertFalse(config.is_local)
        self.assertEqual(config.local, LocalConfig())
        self.assertIsInstance(open_provider(config), BrowserProvider)

    def test_loop_settings_shrink_the_chat_for_a_local_model(self) -> None:
        local = _config("http://127.0.0.1:11434")
        merged = _loop_settings(local, {"theme": "dark"})
        self.assertEqual(merged["compact_after_chars"], DEFAULT_NUM_CTX * 2)
        self.assertEqual(merged["max_result_chars"], DEFAULT_NUM_CTX // 2)
        self.assertEqual(merged["theme"], "dark")
        kept = _loop_settings(_config("http://127.0.0.1:11434", num_ctx=8192), {"compact_after_chars": 50_000})
        self.assertEqual(kept["compact_after_chars"], 50_000)
        self.assertEqual(kept["max_result_chars"], 4_096)
        browser = BotConfig(url="https://chat.example", selectors=None, model="GPT")  # type: ignore[arg-type]
        settings = {"a": 1}
        self.assertIs(_loop_settings(browser, settings), settings)
        self.assertEqual(_model_label(browser), "GPT")
        self.assertEqual(_model_label(local), "qwen2.5-coder:7b · local at 127.0.0.1:11434")
        self.assertEqual(server_label("http://localhost/v1"), "localhost")


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _FakeServer().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        self.server.replies = []
        self.server.requests = []
        self.server.reasoning = ""
        self.server.models = ["qwen2.5-coder:7b", "gemma3:4b"]

    def test_open_provider_picks_the_local_adapter(self) -> None:
        provider = open_provider(_config(self.server.url), headed=True)
        self.assertIsInstance(provider, LocalModelProvider)
        self.assertTrue(provider.can_parallelize)
        with provider:
            self.assertEqual(provider.models, ["qwen2.5-coder:7b", "gemma3:4b"])
            session = provider.session()
            self.assertIsInstance(session, LocalModelSession)
            other = provider.session(isolated=True, model="gemma3:4b")
            self.assertEqual(other.client.model, "gemma3:4b")
            self.assertIs(provider.session().client, provider.client)

    def test_no_model_in_config_takes_the_first_served_one(self) -> None:
        with open_provider(_config(self.server.url, model="")) as provider:
            self.assertEqual(provider.model, "qwen2.5-coder:7b")
        self.server.models = []
        with self.assertRaises(LocalModelError) as ctx:
            with open_provider(_config(self.server.url, model="")):
                pass
        self.assertIn("ollama pull", str(ctx.exception))

    def test_a_server_that_is_down_is_a_chat_error_with_the_fix(self) -> None:
        with self.assertRaises(ChatError) as ctx:
            with open_provider(_config("http://127.0.0.1:1")):
                pass
        self.assertIn("ollama serve", str(ctx.exception))
        session = LocalModelSession(open_provider(_config("http://127.0.0.1:1", api="openai")).client)
        with self.assertRaises(LocalModelError) as ctx:
            session.send("hi")
        self.assertIn("no model server at http://127.0.0.1:1", str(ctx.exception))
        self.assertEqual(session.messages, [])

    def test_ollama_conversation_keeps_its_history_until_new_chat(self) -> None:
        self.server.replies = ["Hello there, friend.", "Second reply", "Fresh"]
        with open_provider(_config(self.server.url, num_ctx=4096)) as provider:
            session = provider.session()
            self.assertEqual(session.send("one"), "Hello there, friend.")
            self.assertEqual(session.send("two"), "Second reply")
            sent = self.server.requests[-1]["body"]
            self.assertEqual(sent["model"], "qwen2.5-coder:7b")
            self.assertTrue(sent["stream"])
            self.assertEqual(sent["options"], {"num_ctx": 4096})
            self.assertEqual(
                [(m["role"], m["content"]) for m in sent["messages"]],
                [("user", "one"), ("assistant", "Hello there, friend."), ("user", "two")],
            )
            self.assertEqual(session.last_detail["completion"], COMPLETION_STOPPED)
            self.assertEqual(session.last_detail["done_reason"], "stop")
            self.assertIn("prompt_eval_count", session.last_detail)
            self.assertFalse(session.stop_generation())
            self.assertTrue(session.new_chat())
            self.assertEqual(session.send("three"), "Fresh")
            self.assertEqual([m["content"] for m in self.server.requests[-1]["body"]["messages"]], ["three"])
            self.assertEqual(self.server.requests[-1]["path"], "/api/chat")

    def test_ollama_default_context_window_is_asked_for(self) -> None:
        self.server.replies = ["ok"]
        with open_provider(_config(self.server.url)) as provider:
            provider.session().send("x")
        self.assertEqual(self.server.requests[-1]["body"]["options"], {"num_ctx": DEFAULT_NUM_CTX})

    def test_openai_compatible_conversation(self) -> None:
        self.server.replies = ["Streamed over SSE"]
        with open_provider(_config(self.server.url, api="openai", model="local-model")) as provider:
            session = provider.session()
            self.assertEqual(session.send("hello"), "Streamed over SSE")
            request = self.server.requests[-1]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertNotIn("options", request["body"])
            self.assertEqual(session.last_detail["completion"], COMPLETION_STOPPED)
            self.assertEqual(session.last_detail["prompt_eval_count"], 12)
            self.assertEqual(session.last_detail["done_reason"], "stop")

    def test_thinking_is_kept_out_of_the_reply(self) -> None:
        self.server.replies = ["<think>\nlet me see\n</think>\nCOMPLETED"]
        self.server.reasoning = "hidden chain of thought"
        with open_provider(_config(self.server.url)) as provider:
            self.assertEqual(provider.session().send("go"), "COMPLETED")
        self.assertEqual(strip_thinking("plain"), "plain")
        self.assertEqual(strip_thinking("<think>only thinking, never closed"), "")
        self.assertEqual(strip_thinking("a <THINK>x</THINK> b"), "a b")

    def test_server_errors_become_local_model_errors_and_the_prompt_is_not_kept(self) -> None:
        self.server.replies = ["ERROR:something broke", "HTTP:404:model 'nope' not found, try pulling it first", "HTTP:400:This model's maximum context length is 4096 tokens"]
        with open_provider(_config(self.server.url)) as provider:
            session = provider.session()
            with self.assertRaises(LocalModelError) as ctx:
                session.send("a")
            self.assertIn("something broke", str(ctx.exception))
            self.assertEqual(session.messages, [])
            with self.assertRaises(LocalModelError) as ctx:
                session.send("b")
            self.assertIn("ollama pull qwen2.5-coder:7b", str(ctx.exception))
            with self.assertRaises(LocalModelError) as ctx:
                session.send("c")
            self.assertIn("compact_after_chars", str(ctx.exception))
            self.server.replies = ["fine"]
            self.assertEqual(session.send("d"), "fine")
            self.assertEqual([m["content"] for m in self.server.requests[-1]["body"]["messages"]], ["d"])

    def test_a_reply_that_stops_arriving_is_noticed_after_reply_stall_ms(self) -> None:
        self.server.replies = ["STALL"]
        with open_provider(_config(self.server.url)) as provider:
            session = provider.session()
            started = time.monotonic()
            with self.assertRaises(LocalModelError) as ctx:
                session.send("slow")
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertIn("stopped arriving", str(ctx.exception))
            self.assertEqual(session.last_detail["completion"], COMPLETION_IDLE)

    def test_wrong_api_for_the_server_is_explained(self) -> None:
        self.server.replies = ["HTTP:404:not found"]
        with open_provider(_config(self.server.url, api="openai")) as provider:
            with self.assertRaises(LocalModelError) as ctx:
                provider.session().send("x")
        self.assertIn("local.api", str(ctx.exception))


class AgentLoopOnLocalModelTests(unittest.TestCase):
    """The agent loop drives the adapter exactly as it drives a browser tab."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _FakeServer().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        agent_ui.reset()
        agent_ui.set_console(Console(file=io.StringIO(), force_terminal=False, width=100, theme=agent_ui.rich_theme("dark")))
        self.root = Path(tempfile.mkdtemp())
        (self.root / "note.txt").write_text("one\n", encoding="utf-8")
        agent_ui.configure(workspace=self.root)
        self.server.replies = []
        self.server.requests = []

    def tearDown(self) -> None:
        agent_ui.reset()

    def test_a_task_runs_a_tool_and_finishes_through_the_adapter(self) -> None:
        call = "<tool_call>\n" + json.dumps({"tool": "read_file", "arguments": {"path": "note.txt"}}) + "\n</tool_call>"
        self.server.replies = ["READY", call, "The file holds one line. COMPLETED"]
        outcome: list[str] = []
        with open_provider(_config(self.server.url)) as provider:
            with provider.session() as session:
                run_agent_loop(
                    session,
                    workspace=self.root,
                    index_path=None,
                    cache_dir=None,
                    first_task="What is in note.txt?",
                    max_rounds=5,
                    max_result_chars=8000,
                    seed="INSTRUCTIONS. Reply with exactly READY.",
                    seed_first=True,
                    read_message=lambda: None,
                    emit=lambda _text: None,
                    ask_user=lambda _q: None,
                    outcome=outcome,
                    settings={"reply_retries": 0},
                )
        self.assertEqual(outcome[-1], "COMPLETED")
        bodies = [req["body"]["messages"] for req in self.server.requests]
        self.assertEqual(len(bodies), 3)
        self.assertEqual(bodies[0][0]["content"], "INSTRUCTIONS. Reply with exactly READY.")
        self.assertEqual(len(bodies[2]), 5)  # seed, READY, task, call, tool result
        self.assertIn("one", bodies[2][-1]["content"])

    def test_a_dead_server_ends_the_task_with_a_retry_note_not_a_crash(self) -> None:
        self.server.replies = ["ERROR:boom", "ERROR:boom"]
        outcome: list[str] = []
        with open_provider(_config(self.server.url)) as provider:
            session = provider.session()
            with unittest.mock.patch("critique_bot.agent._sleep", lambda _s: None):
                run_agent_loop(
                    session,
                    workspace=self.root,
                    index_path=None,
                    cache_dir=None,
                    first_task="Say hi",
                    max_rounds=3,
                    max_result_chars=8000,
                    read_message=lambda: None,
                    emit=lambda _text: None,
                    ask_user=lambda _q: None,
                    outcome=outcome,
                    settings={"reply_retries": 1},
                )
        self.assertEqual(outcome[-1], "FAILED")


if __name__ == "__main__":
    unittest.main()
