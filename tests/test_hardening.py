"""Hardening from the offline security scans: token transport, private folders, profile location."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot import gitlab
from critique_bot.browser import BrowserError, _open_cdp_version
from critique_bot.config import load_config
from critique_bot.output import private_dir
from critique_bot.queue import FileQueue


class GitLabTokenTests(unittest.TestCase):
    def test_token_goes_only_over_https(self) -> None:
        for url in ("http://gitlab.corp.example/api/v4/projects", "file:///etc/passwd", "ftp://gitlab/x", "gitlab/api"):
            with self.subTest(url):
                with patch("urllib.request.urlopen") as opened:
                    with self.assertRaises(gitlab.GitLabError):
                        gitlab.request("GET", url, "glpat-secret")
                    opened.assert_not_called()

    def test_https_and_a_local_test_instance_are_allowed(self) -> None:
        for url in ("https://gitlab.corp.example/api/v4/user", "http://127.0.0.1:8929/api/v4/user"):
            with self.subTest(url):
                gitlab._require_safe_url(url)


@unittest.skipIf(os.name == "nt", "POSIX permissions")
class PrivateFolderTests(unittest.TestCase):
    def test_new_and_existing_folders_become_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fresh = private_dir(Path(tmp) / "a" / "profile")
            self.assertEqual(fresh.stat().st_mode & 0o777, 0o700)
            existing = Path(tmp) / "open"
            existing.mkdir(mode=0o775)
            os.chmod(existing, 0o775)
            private_dir(existing)
            self.assertEqual(existing.stat().st_mode & 0o777, 0o700)

    def test_queue_folders_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = FileQueue(Path(tmp) / "q")
            for path in (queue.root, queue.inbox, queue.processing, queue.results, queue.result_dir("job1")):
                with self.subTest(str(path)):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o700)


class ProfileLocationTests(unittest.TestCase):
    def test_relative_profile_is_next_to_the_config_not_the_current_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "crit-home"
            home.mkdir()
            config = home / "config.json"
            config.write_text(json.dumps({
                "url": "https://chat.corp.example/",
                "selectors": {"prompt_input": "textarea", "assistant_messages": ".a"},
            }), encoding="utf-8")
            project = Path(tmp) / "some-project"
            project.mkdir()
            old = os.getcwd()
            os.chdir(project)
            try:
                loaded = load_config(config)
            finally:
                os.chdir(old)
            self.assertEqual(Path(loaded.user_data_dir), (home / ".edge-profile").resolve())


class CdpTests(unittest.TestCase):
    def test_only_http_cdp_addresses_are_opened(self) -> None:
        with patch("urllib.request.urlopen") as opened:
            with self.assertRaises(BrowserError):
                _open_cdp_version("file:///etc/passwd")
            opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
