# SPDX-License-Identifier: Apache-2.0
"""The user's model-server API key goes only to the server it is for (SF-4). Fake keys only."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import test_00_isolation  # noqa: F401  (HOME/CRIT_HOME -> a temp folder before crit loads)

from critique_bot import config, config_cmd

FAKE_KEY = "sk-FAKE-binding-key-0123456789abcdef"


class KeyBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        home = self.base / "home"
        (home / ".crit").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"HOME": str(home), "CRIT_HOME": str(home / ".crit")})
        env.start()
        self.addCleanup(env.stop)
        for name in ("CRIT_API_KEY", "CRIT_API_KEY_URL", "CRIT_CONFIG", "CRIT_CONFIG_FILE"):
            os.environ.pop(name, None)
        self.user = home / ".crit" / "config.json"

    def user_config(self, **local: str) -> None:
        self.user.write_text(json.dumps({"backend": "local", "url": "http://127.0.0.1:8080", "model": "m",
                                         "local": {"api_key": FAKE_KEY, **local}}), encoding="utf-8")

    def other(self, url: str) -> Path:
        path = self.base / "other.json"
        host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
        path.write_text(json.dumps({"backend": "local", "url": url, "model": "m", "allowed_hosts": [host]}),
                        encoding="utf-8")
        return path

    def test_the_users_own_config_gets_its_key(self) -> None:
        self.user_config()
        self.assertEqual(config.load_config(self.user).local.api_key, FAKE_KEY)

    def test_another_config_for_the_same_server_gets_the_key(self) -> None:
        self.user_config()
        self.assertEqual(config.load_config(self.other("http://127.0.0.1:8080/v1")).local.api_key, FAKE_KEY)
        self.assertEqual(config.load_config(self.other("http://127.0.0.1:9090")).local.api_key, "")

    def test_the_environment_key_is_bound_too(self) -> None:
        other = self.other("https://models.corp.example/v1")
        with mock.patch.dict(os.environ, {"CRIT_API_KEY": FAKE_KEY}):
            self.assertEqual(config.load_config(other).local.api_key, "")
            with mock.patch.dict(os.environ, {"CRIT_API_KEY_URL": "https://models.corp.example"}):
                self.assertEqual(config.load_config(other).local.api_key, FAKE_KEY)

    def test_saving_binds_and_rebinding_moves_it(self) -> None:
        self.user.write_text(json.dumps({"backend": "local", "url": "http://127.0.0.1:8080", "model": "m"}),
                             encoding="utf-8")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(config_cmd.set_api_key(FAKE_KEY), 0)
        saved = json.loads(self.user.read_text(encoding="utf-8"))["local"]
        self.assertEqual(saved["api_key_url"], "http://127.0.0.1:8080")
        other = self.other("https://models.corp.example/v1")
        self.assertEqual(config.load_config(other).local.api_key, "")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(config_cmd.main(["--api-key-url", "https://models.corp.example/v1"]), 0)
        self.assertEqual(config.load_config(other).local.api_key, FAKE_KEY)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(config_cmd.set_api_key("none"), 0)
        self.assertNotIn("local", json.loads(self.user.read_text(encoding="utf-8")))

    def test_origins_compare_scheme_host_and_port(self) -> None:
        self.assertEqual(config.url_origin("127.0.0.1:8080"), "http://127.0.0.1:8080")
        self.assertEqual(config.url_origin("https://A.example/v1"), "https://a.example:443")
        self.assertNotEqual(config.url_origin("http://a.example"), config.url_origin("https://a.example"))


if __name__ == "__main__":
    unittest.main()
