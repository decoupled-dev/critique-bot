"""A scripted Ollama-style model server for end-to-end tests of ``crit --protocol``.

    python fake_model.py PORT

Serves ``/api/tags`` and ``/api/chat``. The reply depends on how many
assistant turns the conversation already has (the last one repeats): first an
edit to note.txt, then a finish. Each conversation starts over, so one server
serves several tests.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EDIT = (
    "I'll update the note.\n<tool_call>\n"
    + json.dumps({"tool": "edit_file", "arguments": {"path": "note.txt", "old_string": "one", "new_string": "two"}})
    + "\n</tool_call>"
)
REPLIES = [EDIT, "Changed note.txt from one to two. COMPLETED"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def _json(self, data: object) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.endswith("/api/tags"):
            self._json({"models": [{"name": "fake"}]})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            request = {}
        if not self.path.endswith("/api/chat"):
            self.send_error(404)
            return
        turns = sum(1 for m in request.get("messages") or [] if isinstance(m, dict) and m.get("role") == "assistant")
        reply = REPLIES[min(turns, len(REPLIES) - 1)]
        lines = [
            {"message": {"role": "assistant", "content": reply}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
        ]
        body = "".join(json.dumps(line) + "\n" for line in lines).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11999
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
