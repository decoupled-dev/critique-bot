"""The local setup UI: config merging, endpoints, and the browser bridge."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from critique_bot.setup_ui import (
    PICK_FIELDS,
    BrowserBridge,
    SetupState,
    make_handler,
)

CONFIG = {
    "backend": "browser",
    "url": "https://chat.example/",
    "selectors": {
        "prompt_input": "#p",
        "assistant_messages": ".a",
        "send_button": "button.send",
    },
    "model": "GPT-5.1",
}


class _Server:
    """Runs the setup handler on a free port for the duration of a test."""

    def __init__(self, state: SetupState) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, response.read()

    def post(self, path: str, payload: dict):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class SetupStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self.path.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
        self.state = SetupState(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_save_merges_selectors_instead_of_replacing_them(self) -> None:
        result = self.state.save({"selectors": {"stop_button": "button.stop"}})
        self.assertTrue(result["saved"])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["selectors"]["stop_button"], "button.stop")
        # Untouched selectors survive the edit.
        self.assertEqual(saved["selectors"]["prompt_input"], "#p")
        self.assertEqual(saved["url"], "https://chat.example/")

    def test_save_keeps_unrelated_top_level_keys(self) -> None:
        self.state.save({"url": "https://other.example/"})
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["url"], "https://other.example/")
        self.assertEqual(saved["model"], "GPT-5.1")
        self.assertEqual(saved["backend"], "browser")

    def test_save_reports_a_config_that_no_longer_loads(self) -> None:
        result = self.state.save({"selectors": {"prompt_input": ""}})
        self.assertTrue(result["saved"])
        self.assertFalse(result["valid"])
        self.assertIn("prompt_input", result["error"])

    def test_save_reports_a_valid_config(self) -> None:
        self.assertTrue(self.state.save({"model": "GPT-5.2"})["valid"])


class EndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self.path.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
        self.state = SetupState(self.path)
        self.server = _Server(self.state)

    def tearDown(self) -> None:
        self.server.close()
        self._tmp.cleanup()

    def test_index_serves_the_page(self) -> None:
        status, body = self.server.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"critique-bot setup", body)

    def test_state_reports_checks_and_queue(self) -> None:
        status, body = self.server.get("/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["config"]["url"], "https://chat.example/")
        self.assertTrue(payload["checks"]["checks"])
        self.assertIn("queue", payload)
        self.assertFalse(payload["browser"])

    def test_state_reports_a_broken_config_without_crashing(self) -> None:
        self.path.write_text(json.dumps({"backend": "browser"}), encoding="utf-8")
        payload = json.loads(self.server.get("/api/state")[1])
        self.assertIn("prompt_input", payload["error"])
        self.assertNotIn("checks", payload)

    def test_saving_config_through_the_api(self) -> None:
        result = self.server.post(
            "/api/config", {"config": {"selectors": {"stop_button": "button.stop"}}}
        )
        self.assertTrue(result["valid"])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["selectors"]["stop_button"], "button.stop")

    def test_actions_needing_a_browser_report_it(self) -> None:
        for path, payload in (
            ("/api/pick", {"field": "prompt_input"}),
            ("/api/validate", {}),
            ("/api/test", {}),
        ):
            self.assertEqual(
                self.server.post(path, payload)["error"], "open the browser first"
            )

    def test_unknown_pick_field_is_rejected(self) -> None:
        result = self.server.post("/api/pick", {"field": "nope"})
        self.assertIn("unknown field", result["error"])

    def test_pick_status_starts_empty(self) -> None:
        payload = json.loads(self.server.get("/api/pick")[1])
        self.assertIsNone(payload["pick"])
        self.assertFalse(payload["browser"])

    def test_doctor_endpoint_returns_a_report(self) -> None:
        payload = self.server.post("/api/doctor", {})
        self.assertIn("checks", payload)
        self.assertIn("ok", payload)

    def test_unknown_path_is_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.server.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_open_browser_is_refused_for_http_backends(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "backend": "ollama",
                    "model": "llama3",
                    "base_url": "http://127.0.0.1:11434/v1",
                }
            ),
            encoding="utf-8",
        )
        result = self.server.post("/api/browser/open", {})
        self.assertIn("browser backend", result["error"])


class BrowserBridgeTests(unittest.TestCase):
    def test_calling_a_closed_bridge_raises(self) -> None:
        bridge = BrowserBridge()
        self.assertFalse(bridge.running)
        with self.assertRaises(RuntimeError) as ctx:
            bridge.call(lambda page: page)
        self.assertIn("not open", str(ctx.exception))

    def test_pick_result_is_stored_and_cleared(self) -> None:
        bridge = BrowserBridge()
        bridge._on_pick({"field": "prompt_input", "options": []})
        self.assertEqual(bridge.current_pick()["field"], "prompt_input")
        bridge.clear_pick()
        self.assertIsNone(bridge.current_pick())

    def test_non_dict_pick_is_ignored(self) -> None:
        bridge = BrowserBridge()
        bridge._on_pick("garbage")
        self.assertIsNone(bridge.current_pick())

    def test_stopping_an_unstarted_bridge_is_safe(self) -> None:
        BrowserBridge().stop()

    def test_start_surfaces_a_launch_failure(self) -> None:
        from critique_bot.browser import BrowserError
        from critique_bot.config import BotConfig, Selectors

        config = BotConfig(
            url="https://chat.example/",
            selectors=Selectors(prompt_input="#p", assistant_messages=".a"),
        )
        with patch(
            "critique_bot.browser.launch_edge",
            side_effect=BrowserError("no Edge here"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                BrowserBridge().start(config)
        self.assertIn("no Edge here", str(ctx.exception))


class PickFieldTests(unittest.TestCase):
    def test_every_field_has_a_label_and_mode(self) -> None:
        for name, spec in PICK_FIELDS.items():
            self.assertTrue(spec["label"], name)
            self.assertIn(spec["mode"], {"unique", "group"}, name)

    def test_assistant_messages_is_a_group_selector(self) -> None:
        # A reply selector must match every reply, not just the one clicked.
        self.assertEqual(PICK_FIELDS["assistant_messages"]["mode"], "group")


if __name__ == "__main__":
    unittest.main()
