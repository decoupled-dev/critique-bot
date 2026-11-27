# SPDX-License-Identifier: Apache-2.0
"""End to end: crit eval runs the real crit process against a scripted model server."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from critique_bot import eval_harness as ev

EDIT = {"tool": "edit_file", "arguments": {"path": "temps.py", "old_string": "celsius * 9 / 5 + 23",
                                            "new_string": "celsius * 9 / 5 + 32"}}


class _Handler(BaseHTTPRequestHandler):
    """READY for the seed, then one edit_file call, then a finished reply."""

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        body = json.dumps({"data": [{"id": "fake"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        messages = request.get("messages") or []
        sent_edit = any(m["role"] == "assistant" and "edit_file" in m["content"] for m in messages)
        if len(messages) == 1 and "READY" in messages[-1]["content"]:
            reply = "READY"
        elif not sent_edit:
            reply = "<tool_call>\n" + json.dumps(EDIT) + "\n</tool_call>"
        else:
            reply = "Fixed to_fahrenheit: the offset is 32. COMPLETED"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        usage = {"prompt_tokens": 500, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 400}}
        for event in ({"choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}):
            self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@unittest.skipUnless(shutil.which("python3") and shutil.which("git"), "the py-fix-test task needs python3 and git")
class EvalSmokeTest(unittest.TestCase):
    def test_crit_fixes_the_python_task_and_reports_its_counters(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / "config.json"
                host, port = server.server_address[:2]
                config.write_text(json.dumps({"backend": "local", "url": f"http://{host}:{port}", "model": "fake"}))
                out = root / "out"
                with contextlib.redirect_stdout(io.StringIO()):
                    code = ev.main(["run", "--only", "py-fix-test", "--config", str(config),
                                    "--crit", f"{sys.executable} -m critique_bot --as-crit", "--out", str(out)])
                record = ev.read_results(out)[0]
                self.assertEqual(code, 0, record)
                self.assertEqual(record["result"], "pass", record)
                self.assertEqual(record["exit_reason"], "COMPLETED")
                self.assertEqual(record["tool_calls"].get("edit_file"), 1)
                self.assertEqual(record["edits"]["edit_file"]["ok"], 1)
                self.assertGreaterEqual(record["cached_tokens"], 400)
                self.assertTrue((out / "summary.md").is_file())
                self.assertTrue((out / "meta.json").is_file())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
