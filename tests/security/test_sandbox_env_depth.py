# SPDX-License-Identifier: Apache-2.0
"""Adversarial review: .env files below the shallow scan are left readable.

docs/security.md lists ``.env``, ``.env.*``, ``*.env`` and ``.envrc`` among the secret files
masked inside the sandbox. The scan that finds them (:func:`critique_bot.sandbox._env_files`)
only enqueues directories while ``depth < 2`` and stops after 400 directories, so a ``.env``
nested three or more levels down -- ordinary in a monorepo, and the common case in a huge
tree such as AOSP where the 400-directory budget is spent long before a project's config --
is never masked. A sandboxed command can then read it, and its contents reach the chat.

The test plants a FAKE secret in a deep ``.env`` under a throwaway workspace and asserts the
SECURE behavior (it is masked); it fails today.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from critique_bot import sandbox


def _outside_tmp() -> Path:
    for base in ("/var/tmp", str(Path(__file__).resolve().parents[2] / ".crit-test-tmp")):
        try:
            os.makedirs(base, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="crit-sec-", dir=base))
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix="crit-sec-"))


class EnvDepthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = _outside_tmp()
        self.home = self.base / "home"
        self.ws = self.base / "ws"
        self.home.mkdir(parents=True)
        self.ws.mkdir()
        self.deep = self.ws / "services" / "api" / "config"
        self.deep.mkdir(parents=True)
        (self.deep / ".env").write_text("DB_PASSWORD=FAKE-DEEP-ENV-SECRET", encoding="utf-8")
        # A shallow .env too, to show the scan does mask those (sanity).
        (self.ws / ".env").write_text("ROOT=FAKE-ROOT", encoding="utf-8")
        self.addCleanup(__import__("shutil").rmtree, self.base, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"CRIT_HOME": str(self.home / ".crit")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _is_masked(self, path: Path) -> bool:
        policy = sandbox.build_policy(
            sandbox.Settings(mode=sandbox.WORKSPACE_WRITE), self.ws, home=self.home, make_caches=False,
        )
        real = Path(os.path.realpath(path))
        return any(real == m for m in policy.masked)

    def test_shallow_env_is_masked(self) -> None:
        # Sanity: the top-level .env is masked, so the test above isolates the depth bug.
        self.assertTrue(self._is_masked(self.ws / ".env"))

    # SECURITY FINDING SB-3 (fixed): .env files deeper than two levels are not masked
    def test_deep_env_is_masked(self) -> None:
        self.assertTrue(
            self._is_masked(self.deep / ".env"),
            "a .env three levels deep is left readable inside the sandbox",
        )


if __name__ == "__main__":
    unittest.main()
