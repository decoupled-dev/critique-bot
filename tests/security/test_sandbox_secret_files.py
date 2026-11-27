# SPDX-License-Identifier: Apache-2.0
"""Adversarial review: secret credential files the sandbox fails to mask.

docs/security.md claims the sandbox makes secret locations "read as empty or unreadable".
The masked list (:data:`critique_bot.sandbox.SECRET_HOME_PATHS`) is incomplete: several
very common credential files are left readable, so a sandboxed command (driven by model
output, a malicious repo build step, or an MCP server) can exfiltrate them once the session
is in ``open`` egress or the token itself is pasted into the conversation.

Each test asserts the SECURE behavior (the file is masked) and fails today while the finding
is open. All planted secrets are FAKE and live only under a throwaway home outside the repo.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from critique_bot import sandbox


def _outside_tmp() -> Path:
    """A folder outside /tmp (the sandbox's private /tmp tmpfs would otherwise hide it)."""
    for base in ("/var/tmp", str(Path(__file__).resolve().parents[2] / ".crit-test-tmp")):
        try:
            os.makedirs(base, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="crit-sec-", dir=base))
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix="crit-sec-"))


BWRAP = sys.platform.startswith("linux") and sandbox.availability().ok

# Common credential files and the fake secret planted in each.
SECRET_FILES = {
    ".npmrc": "//registry.npmjs.org/:_authToken=FAKE-NPM-TOKEN",
    ".cargo/credentials.toml": '[registry]\ntoken = "FAKE-CARGO-TOKEN"',
    ".gem/credentials": ":rubygems_api_key: FAKE-GEM-KEY",
    ".config/composer/auth.json": '{"http-basic": {"repo": {"password": "FAKE-COMPOSER"}}}',
    ".m2/settings.xml": "<settings><servers><server><password>FAKE-MAVEN</password>"
                        "</server></servers></settings>",
}


def _plant(home: Path) -> None:
    for rel, body in SECRET_FILES.items():
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


class SecretMaskingPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = _outside_tmp()
        self.home = self.base / "home"
        self.ws = self.base / "ws"
        self.home.mkdir(parents=True)
        self.ws.mkdir()
        _plant(self.home)
        self.addCleanup(__import__("shutil").rmtree, self.base, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"CRIT_HOME": str(self.home / ".crit")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _policy(self) -> sandbox.Policy:
        return sandbox.build_policy(
            sandbox.Settings(mode=sandbox.WORKSPACE_WRITE), self.ws, home=self.home, make_caches=False,
        )

    def _is_masked(self, policy: sandbox.Policy, rel: str) -> bool:
        real = Path(os.path.realpath(self.home / rel))
        return any(real == m or self._within(real, m) for m in policy.masked)

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    # SECURITY FINDING SB-1: ~/.npmrc, ~/.cargo, ~/.gem, ~/.m2/settings.xml etc. are not masked
    @unittest.expectedFailure
    def test_common_credential_files_are_masked(self) -> None:
        policy = self._policy()
        unmasked = [rel for rel in SECRET_FILES if not self._is_masked(policy, rel)]
        self.assertEqual(unmasked, [], f"sandbox leaves these credential files readable: {unmasked}")


@unittest.skipUnless(BWRAP, "bubblewrap is not usable here")
class SecretReadUnderBwrapTests(unittest.TestCase):
    """Prove end-to-end that a sandboxed command reads the fake npm token off disk."""

    def setUp(self) -> None:
        self.base = _outside_tmp()
        self.home = self.base / "home"
        self.ws = self.base / "ws"
        self.home.mkdir(parents=True)
        self.ws.mkdir()
        _plant(self.home)
        self.addCleanup(__import__("shutil").rmtree, self.base, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"CRIT_HOME": str(self.home / ".crit")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv: list[str]) -> str:
        policy = sandbox.build_policy(
            sandbox.Settings(mode=sandbox.WORKSPACE_WRITE), self.ws, home=self.home, make_caches=False,
        )
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home)}
        proc = sandbox.run(argv, cwd=self.ws, env=env, policy=policy, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        out, _ = proc.communicate(timeout=60)
        return out.decode("utf-8", "replace")

    # SECURITY FINDING SB-1: a sandboxed command can read ~/.npmrc (npm auth token)
    @unittest.expectedFailure
    def test_npmrc_token_is_not_readable(self) -> None:
        out = self._run(["/bin/sh", "-c", "cat ~/.npmrc 2>&1"])
        self.assertNotIn("FAKE-NPM-TOKEN", out, "sandboxed command read the npm auth token")


if __name__ == "__main__":
    unittest.main()
