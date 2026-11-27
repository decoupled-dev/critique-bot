"""Native tool calling with a local server: the request, streamed tool_calls, the fallback
parser, the history the server sees, and a whole task through the engine."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from rich.console import Console

from critique_bot import agent_ui, run_stats
from critique_bot.agent import TOOL_FORMAT_NATIVE, _tool_setup, load_prompt_sections, run_agent_loop
from critique_bot.config import BotConfig, LocalConfig
from critique_bot.local_model import LocalModelClient, LocalModelSession, responses_text, split_results

TOOLS = [{"type": "function", "function": {"name": "read_files", "description": "read",
                                           "parameters": {"type": "object", "properties": {}}}}]


def _call_chunks(calls: list[tuple[str, str, dict]], split: int = 7) -> list[dict]:
    """Streamed deltas for calls: the name first, then the arguments in small pieces."""
    events = []
    for index, (call_id, name, args) in enumerate(calls):
        events.append({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": index, "id": call_id, "type": "function", "function": {"name": name, "arguments": ""}}]}}]})
        text = json.dumps(args)
        for start in range(0, len(text), split):
            events.append({"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": index, "function": {"arguments": text[start:start + split]}}]}}]})
    return events


class _Server:
    """Replies are lists of SSE events (dicts); each request is recorded."""

    def __init__(self) -> None:
        self.replies: list[list[dict]] = []
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                body = json.dumps({"data": [{"id": "qwen-test"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append(request)
                events = outer.replies.pop(0) if outer.replies else [
                    {"choices": [{"index": 0, "delta": {"content": "COMPLETED"}}]}]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                last = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
                for event in [*events, last]:
                    self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _config(url: str) -> BotConfig:
    return BotConfig(url=url, selectors=None, model="qwen-test", backend="local", local=LocalConfig(),
                     reply_start_ms=5_000, reply_stall_ms=5_000, reply_max_ms=20_000)


class NativeSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _Server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        self.server.replies, self.server.requests = [], []
        run_stats.reset()
        self.session = LocalModelSession(LocalModelClient(_config(self.server.url)))
        self.session.use_tools(TOOLS)

    def test_streamed_calls_become_responses_items_and_results_become_tool_messages(self) -> None:
        self.server.replies = [
            [{"choices": [{"index": 0, "delta": {"content": "Reading both."}}]},
             *_call_chunks([("c1", "read_files", {"paths": ["a.py"]}), ("c2", "read_files", {"paths": ["b.py"]})])],
            [{"choices": [{"index": 0, "delta": {"content": "COMPLETED"}}]}],
        ]
        reply = self.session.send("Task: look")
        request = self.server.requests[0]
        self.assertEqual(request["tools"], TOOLS)
        self.assertEqual(request["tool_choice"], "auto")
        output = json.loads(reply)["output"]
        self.assertEqual(output[0]["type"], "message")
        self.assertEqual([(i["call_id"], i["arguments"]) for i in output[1:]],
                         [("c1", {"paths": ["a.py"]}), ("c2", {"paths": ["b.py"]})])
        results = json.dumps({"input": [
            {"type": "function_call_output", "call_id": "c1", "output": {"tool": "read_files", "ok": True, "output": "A"}},
            {"type": "function_call_output", "call_id": "c2", "output": {"tool": "read_files", "ok": False, "error": "no such file"}},
        ]}) + "\n\nSTATE\ntask: look"
        self.assertEqual(self.session.send(results), "COMPLETED")
        sent = self.server.requests[1]["messages"]
        self.assertEqual([m["role"] for m in sent], ["user", "assistant", "tool", "tool", "user"])
        self.assertEqual(sent[1]["tool_calls"][1]["function"]["arguments"], json.dumps({"paths": ["b.py"]}))
        self.assertEqual((sent[2]["tool_call_id"], sent[2]["content"]), ("c1", "A"))
        self.assertEqual(sent[3]["content"], "ERROR: no such file")
        self.assertEqual(sent[4]["content"], "STATE\ntask: look")
        self.assertEqual(run_stats.snapshot()["model_requests"], 2)

    def test_calls_in_the_text_or_reasoning_are_recovered(self) -> None:
        xml = "<tool_call>\n<function=read_files>\n<parameter=paths>\n[\"x.py\"]\n</parameter>\n</function>\n</tool_call>"
        self.server.replies = [
            [{"choices": [{"index": 0, "delta": {"content": "Let me look.\n" + xml}}]}],
            [{"choices": [{"index": 0, "delta": {"reasoning_content": '<tool_call>{"name": "read_files", '
                                                                      '"arguments": {"paths": ["y.py"]}}</tool_call>'}}]}],
        ]
        first = json.loads(self.session.send("go"))["output"]
        self.assertEqual(first[0]["content"][0]["text"], "Let me look.")
        self.assertEqual((first[1]["name"], first[1]["arguments"], first[1]["call_id"]), ("read_files", {"paths": ["x.py"]}, "call_1"))
        self.assertIn("qwen-xml", self.session.last_detail["parse_notes"][0])
        results = json.dumps({"input": [{"type": "function_call_output", "call_id": "call_1",
                                         "output": {"tool": "read_files", "ok": True, "output": "X"}}]})
        second = json.loads(self.session.send(results))["output"]
        self.assertEqual(second[0]["arguments"], {"paths": ["y.py"]})
        self.assertEqual(run_stats.snapshot()["recovered_calls"], 2)

    def test_a_failed_request_leaves_no_half_turn_and_unanswered_calls_get_a_result(self) -> None:
        self.server.replies = [_call_chunks([("c9", "read_files", {})])]
        self.session.send("go")
        self.session.client.url = "http://127.0.0.1:1"  # the server is gone
        self.session.client._port = 1
        before = list(self.session.messages)
        with self.assertRaises(Exception):
            self.session.send("next step")
        self.assertEqual(self.session.messages, before)
        self.session.client = LocalModelClient(_config(self.server.url))
        self.session.send("next step")  # no result for c9 was sent: it is answered as not run
        sent = self.server.requests[-1]["messages"]
        self.assertEqual([m["role"] for m in sent[-3:]], ["assistant", "tool", "user"])
        self.assertEqual(sent[-2], {"role": "tool", "tool_call_id": "c9", "content": "(not run)"})

    def test_helpers(self) -> None:
        self.assertEqual(split_results("plain text"), ([], "plain text"))
        items, rest = split_results('{"input": [{"type": "function_call_output", "call_id": "a", "output": {}}]}\n\nmore')
        self.assertEqual((len(items), rest), (1, "more"))
        text = responses_text("", [{"id": "z", "function": {"name": "n", "arguments": "not json"}}])
        self.assertEqual(json.loads(text)["output"][0]["arguments"], "not json")


class NativeEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _Server()
        agent_ui.reset()
        agent_ui.set_console(Console(file=io.StringIO(), force_terminal=False, width=100,
                                     theme=agent_ui.rich_theme("dark")))
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "note.txt").write_text("one line\n", encoding="utf-8")
        agent_ui.configure(workspace=self.root)

    def tearDown(self) -> None:
        agent_ui.reset()
        self.server.stop()
        self._tmp.cleanup()

    def test_local_backends_default_to_native_and_the_web_chat_cannot(self) -> None:
        self.assertEqual(_tool_setup({}, helpers=0, web=False, native_ok=True).tool_format, TOOL_FORMAT_NATIVE)
        self.assertEqual(_tool_setup({"tool_format": "openai"}, helpers=0, web=False, native_ok=True).tool_format, "openai")
        self.assertEqual(_tool_setup({"tool_format": "native"}, helpers=0, web=False, native_ok=False).tool_format, "openai")
        setup = _tool_setup({}, helpers=0, web=False, native_ok=True)
        self.assertEqual(setup.tools_block, "")  # the tools go in the request, not the first message
        names = [t["function"]["name"] for t in setup.native_tools]
        self.assertEqual(names, sorted(names))
        self.assertNotIn("delegate", names)
        prompt = load_prompt_sections(TOOL_FORMAT_NATIVE)
        self.assertNotIn('{"output"', prompt["SYSTEM"] + prompt["NUDGE"] + prompt["RECOVER"])
        self.assertIn("tool-calling interface", prompt["SYSTEM"])

    def test_a_task_runs_a_native_call_and_finishes(self) -> None:
        self.server.replies = [
            [{"choices": [{"index": 0, "delta": {"content": "READY"}}]}],
            _call_chunks([("r1", "read_files", {"paths": ["note.txt"]})]),
            [{"choices": [{"index": 0, "delta": {"content": "note.txt holds one line. COMPLETED"}}]}],
        ]
        setup = _tool_setup({}, helpers=0, web=False, native_ok=True)
        session = LocalModelSession(LocalModelClient(_config(self.server.url)))
        outcome: list[str] = []
        run_agent_loop(
            session, workspace=self.root, index_path=None, cache_dir=None,
            first_task="What is in note.txt?", max_rounds=5, max_result_chars=8000,
            seed="INSTRUCTIONS. Reply with exactly READY.", seed_first=True,
            read_message=lambda: None, emit=lambda _text: None, ask_user=lambda _q: None, outcome=outcome,
            settings={"reply_retries": 0, "tool_format": TOOL_FORMAT_NATIVE, "_native_tools": setup.native_tools},
        )
        self.assertEqual(outcome[-1], "COMPLETED")
        last = self.server.requests[-1]
        self.assertEqual(last["tools"], setup.native_tools)
        roles = [m["role"] for m in last["messages"]]
        self.assertIn("tool", roles)
        tool_message = next(m for m in last["messages"] if m["role"] == "tool")
        self.assertEqual(tool_message["tool_call_id"], "r1")
        self.assertIn("one line", tool_message["content"])


if __name__ == "__main__":
    unittest.main()
