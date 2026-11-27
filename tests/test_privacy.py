# SPDX-License-Identifier: Apache-2.0
"""Telemetry stays off: crit's environment, its dependency list, and its own imports."""

from __future__ import annotations

import ast
import os
import re
import unittest
from pathlib import Path

from critique_bot import privacy
from webchat_support import HAVE_WEBCHAT, needs_webchat

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "critique_bot"

# Every package crit ships, each checked for reporting code at the pinned version.
# A new dependency fails this test until someone reviews it and adds it here.
REVIEWED_PACKAGES = {
    "playwright",
    "pyee",
    "greenlet",
    "rich",
    "pygments",
    "markdown-it-py",
    "mdurl",
    "prompt-toolkit",
    "wcwidth",
    "typing-extensions",
}

# Libraries whose job is to send usage, metrics, or errors somewhere.
REPORTING_MODULES = {
    "sentry_sdk", "raven", "posthog", "mixpanel", "analytics", "segment",
    "amplitude", "rudderstack", "bugsnag", "rollbar", "honeybadger", "newrelic",
    "ddtrace", "datadog", "opentelemetry", "statsd", "prometheus_client",
    "scarf", "telemetry", "applicationinsights", "opencensus",
}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class ProcessEnvTest(unittest.TestCase):
    def test_switches_are_forced_over_the_users_values(self) -> None:
        env = {"DO_NOT_TRACK": "0", "HF_HUB_DISABLE_TELEMETRY": "0", "KEEP": "me"}
        privacy.apply_to_process(env)
        self.assertEqual(env["DO_NOT_TRACK"], "1")
        self.assertEqual(env["HF_HUB_DISABLE_TELEMETRY"], "1")
        self.assertEqual(env["KEEP"], "me")
        for name, value in privacy.TELEMETRY_OFF_ENV.items():
            self.assertEqual(env[name], value, name)

    def test_values_are_plain_strings(self) -> None:
        for name, value in privacy.TELEMETRY_OFF_ENV.items():
            self.assertRegex(name, r"^[A-Za-z_][A-Za-z0-9_]*$")
            self.assertIsInstance(value, str)

    def test_default_target_is_os_environ(self) -> None:
        saved = os.environ.get("DO_NOT_TRACK")
        try:
            os.environ["DO_NOT_TRACK"] = "0"
            privacy.apply_to_process()
            self.assertEqual(os.environ["DO_NOT_TRACK"], "1")
        finally:
            if saved is None:
                os.environ.pop("DO_NOT_TRACK", None)
            else:
                os.environ["DO_NOT_TRACK"] = saved


class DependencyReviewTest(unittest.TestCase):
    def test_constraints_list_only_reviewed_runtime_packages(self) -> None:
        pinned = set()
        for line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            name = re.split(r"[=<>!~\[; ]", line, maxsplit=1)[0]
            pinned.add(_normalize(name))
        # Build-only and optional tools (PyInstaller, build, tree-sitter) may be pinned
        # too; they never run inside crit's process at run time, except tree-sitter,
        # which is a parser with no network code.
        build_only = {p for p in pinned if p.startswith(("pyinstaller", "build", "tree-sitter", "pip", "setuptools", "wheel", "altgraph", "packaging", "pefile", "macholib", "pywin32-ctypes", "pyproject-hooks"))}
        unreviewed = pinned - build_only - REVIEWED_PACKAGES
        self.assertEqual(unreviewed, set(), "review these for telemetry, then add them to REVIEWED_PACKAGES")

    def test_project_dependencies_are_reviewed(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        block = re.search(r"^dependencies = \[(.*?)\]", text, re.S | re.M)
        assert block is not None
        names = {_normalize(re.split(r"[=<>!~\[; ]", item.strip().strip('"'), maxsplit=1)[0])
                 for item in block.group(1).split(",") if item.strip()}
        self.assertLessEqual(names, REVIEWED_PACKAGES)


class OwnCodeTest(unittest.TestCase):
    def test_crit_imports_no_reporting_library(self) -> None:
        found = []
        for path in SRC.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    roots = [node.module.split(".")[0]]
                else:
                    continue
                for root in roots:
                    if root in REPORTING_MODULES:
                        found.append(f"{path.name}: {root}")
        self.assertEqual(found, [])


@needs_webchat
class BrowserQuietTest(unittest.TestCase):
    """The browser's own services (sync, Safe Browsing, sign-in, time, updates) stay quiet."""

    def test_service_hosts_are_blocked_unless_on_the_chat_site(self) -> None:
        from critique_bot import egress
        from critique_bot.webchat import browser

        saved = egress.policy()
        self.addCleanup(setattr, egress, "_policy", saved)
        egress.configure("open", "https://chat.company.example/")  # strict egress blocks everything; this is the open-mode list

        args = browser.browser_quiet_args("https://chat.company.example/")
        rules = next(a for a in args if a.startswith("--host-resolver-rules="))
        self.assertIn("MAP accounts.google.com ~NOTFOUND", rules)
        self.assertIn("MAP *.events.data.microsoft.com ~NOTFOUND", rules)
        # A chat on Google's own site keeps google.com hosts resolvable.
        gemini = browser.browser_quiet_args("https://gemini.google.com/app")
        gemini_rules = next((a for a in gemini if a.startswith("--host-resolver-rules=")), "")
        self.assertNotIn("accounts.google.com", gemini_rules)
        self.assertIn("edge.microsoft.com", gemini_rules)

    def test_disable_features_keeps_playwrights_list(self) -> None:
        from critique_bot.webchat import browser

        flag = next(a for a in browser.BROWSER_QUIET_ARGS if a.startswith("--disable-features="))
        features = set(flag.split("=", 1)[1].split(","))
        self.assertLessEqual(set(browser._PLAYWRIGHT_DISABLED_FEATURES), features)
        try:
            import playwright
        except ImportError:
            self.skipTest("playwright not installed")
        bundle = Path(playwright.__file__).parent / "driver" / "package" / "lib" / "coreBundle.js"
        if not bundle.is_file():
            self.skipTest("Playwright driver bundle not found")
        text = bundle.read_text(encoding="utf-8", errors="replace")
        start = text.find("disabledFeatures = [")
        if start < 0:
            self.skipTest("Playwright's feature list moved")
        block = text[start:text.find("]", start)]
        current = set(re.findall(r'"([A-Za-z0-9]+)"', block))
        self.assertLessEqual(current, features, "Playwright changed its --disable-features list; update browser.py")

    def test_private_prefs_merge_into_the_profile(self) -> None:
        import json
        import tempfile

        from critique_bot.webchat import browser

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / "Default").mkdir()
            (profile / "Default" / "Preferences").write_text(json.dumps({"keep": 1, "safebrowsing": {"enabled": True}}))
            browser.apply_private_prefs(profile)
            prefs = json.loads((profile / "Default" / "Preferences").read_text())
            local = json.loads((profile / "Local State").read_text())
        self.assertEqual(prefs["keep"], 1)
        self.assertFalse(prefs["safebrowsing"]["enabled"])
        self.assertFalse(prefs["signin"]["allowed"])
        self.assertEqual(local["dns_over_https"]["mode"], "off")


if __name__ == "__main__":
    unittest.main()
