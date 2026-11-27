# SPDX-License-Identifier: Apache-2.0
"""Adversarial review: writable build caches are a code-persistence vector.

docs/security.md says crit keeps ``.git/config`` and ``.git/hooks`` read-only "so a command
cannot ... plant a hook or ``core.fsmonitor`` that runs later outside the sandbox." But the
default ``workspace-write`` mode makes the build caches (``~/.gradle``, ``~/.m2``, ...)
fully writable, and those directories hold files that tools execute automatically:
``~/.gradle/init.gradle`` and ``~/.gradle/init.d/*.gradle`` are run as Groovy by *every*
later Gradle invocation, including one the user runs by hand or one crit runs in ``full``
mode. That is the exact persistence the hook protection is meant to prevent.

Each test asserts the SECURE behavior (the auto-run file cannot be planted) and fails today.
Everything happens under a throwaway home outside the repo; no Gradle is actually invoked.
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
    for base in ("/var/tmp", str(Path(__file__).resolve().parents[2] / ".crit-test-tmp")):
        try:
            os.makedirs(base, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="crit-sec-", dir=base))
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix="crit-sec-"))


BWRAP = sys.platform.startswith("linux") and sandbox.availability().ok

# Files inside writable caches that tools auto-execute on a later, possibly unsandboxed run.
AUTO_RUN = (
    "~/.gradle/init.gradle",
    "~/.gradle/init.d/zz-crit.gradle",
)


class CachePersistencePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = _outside_tmp()
        self.home = self.base / "home"
        self.ws = self.base / "ws"
        self.home.mkdir(parents=True)
        self.ws.mkdir()
        (self.home / ".gradle" / "init.d").mkdir(parents=True)
        self.addCleanup(__import__("shutil").rmtree, self.base, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"CRIT_HOME": str(self.home / ".crit")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _policy(self) -> sandbox.Policy:
        return sandbox.build_policy(
            sandbox.Settings(mode=sandbox.WORKSPACE_WRITE), self.ws, home=self.home, make_caches=True,
        )

    # SECURITY FINDING SB-2 (fixed): ~/.gradle/init.gradle (auto-run Groovy) is writable in the sandbox
    def test_gradle_init_scripts_cannot_be_planted(self) -> None:
        policy = self._policy()
        writable = [p for p in AUTO_RUN
                    if policy.can_write(Path(os.path.expanduser(p.replace("~", str(self.home)))))]
        self.assertEqual(writable, [], f"sandbox lets a command plant auto-run files: {writable}")


@unittest.skipUnless(BWRAP, "bubblewrap is not usable here")
class CachePersistenceUnderBwrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = _outside_tmp()
        self.home = self.base / "home"
        self.ws = self.base / "ws"
        self.home.mkdir(parents=True)
        self.ws.mkdir()
        (self.home / ".gradle").mkdir()
        self.addCleanup(__import__("shutil").rmtree, self.base, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"CRIT_HOME": str(self.home / ".crit")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv: list[str]) -> int:
        policy = sandbox.build_policy(
            sandbox.Settings(mode=sandbox.WORKSPACE_WRITE), self.ws, home=self.home, make_caches=False,
        )
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home)}
        proc = sandbox.run(argv, cwd=self.ws, env=env, policy=policy, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        proc.communicate(timeout=60)
        return proc.returncode

    # SECURITY FINDING SB-2 (fixed): a sandboxed command plants ~/.gradle/init.gradle that persists on host
    def test_planted_gradle_init_does_not_persist(self) -> None:
        payload = "// crit-security-probe: would run outside the sandbox"
        self._run(["/bin/sh", "-c", f"printf '%s' '{payload}' > ~/.gradle/init.gradle"])
        planted = self.home / ".gradle" / "init.gradle"
        self.assertFalse(
            planted.exists() and payload in planted.read_text(encoding="utf-8"),
            "a sandboxed command planted an auto-run Gradle init script that survives on the host",
        )


if __name__ == "__main__":
    unittest.main()
