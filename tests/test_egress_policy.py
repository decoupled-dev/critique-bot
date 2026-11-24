"""Strict egress (the default): crit reaches the chat host and allowed_hosts, nothing else."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from critique_bot import browser, egress
from critique_bot.config import ConfigError, load_config

_SELECTORS = {"prompt_input": "textarea", "assistant_messages": ".msg"}


def _write_config(folder: Path, **extra) -> Path:
    data = {"url": "https://chat.corp.example/c", "selectors": _SELECTORS, "model": "m", **extra}
    path = folder / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class _PolicyReset(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = egress.policy()
        self._env = patch.dict(os.environ, {k: "" for k in ("CRITIQUE_EGRESS", "CRITIQUE_CDP_URL", "CRITIQUE_CHAT_URL")})
        self._env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self) -> None:
        egress._policy = self._saved
        browser.set_allowed_hosts(())
        self._env.stop()
        self.tmp.cleanup()


class ConfigTest(_PolicyReset):
    def test_strict_is_the_default(self) -> None:
        config = load_config(_write_config(self.folder))
        self.assertEqual(config.egress, "strict")
        self.assertTrue(config.filter_chat_page)
        policy = egress.policy()
        self.assertTrue(policy.strict and policy.configured)
        self.assertEqual(policy.hosts(), ("chat.corp.example",))

    def test_open_is_an_explicit_choice(self) -> None:
        config = load_config(_write_config(self.folder, egress="open"))
        self.assertEqual(config.egress, "open")
        self.assertFalse(config.filter_chat_page)
        self.assertTrue(egress.policy().permits("cdn.anything.example"))

    def test_bad_value_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(_write_config(self.folder, egress="sometimes"))

    def test_attached_browser_is_refused_when_strict(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_config(_write_config(self.folder, cdp_url="http://127.0.0.1:9222"))
        self.assertIn("cdp_url", str(caught.exception))


class PolicyTest(_PolicyReset):
    def test_only_the_chat_host_and_allowed_hosts(self) -> None:
        policy = egress.configure("strict", "https://chat.corp.example/c", ("api.corp.example",))
        self.assertTrue(policy.permits("chat.corp.example"))
        self.assertTrue(policy.permits("api.corp.example"))
        self.assertTrue(policy.permits("v1.api.corp.example"))
        self.assertTrue(policy.permits("127.0.0.1"))
        for host in ("corp.example", "other.corp.example", "evil.example", "chat.corp.example.evil.example", ""):
            self.assertFalse(policy.permits(host), host)

    def test_require_explains_what_to_do(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c")
        with self.assertRaises(PermissionError) as caught:
            egress.require("gitlab.corp.example", "posting to GitLab")
        self.assertIn('"gitlab.corp.example" to allowed_hosts', str(caught.exception))


class BrowserTest(_PolicyReset):
    def test_resolver_allows_only_the_policy_hosts(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c", ("cdn.corp.example",))
        with patch.object(browser, "system_proxy", return_value=("", "")):
            args = browser.browser_quiet_args("https://chat.corp.example/c")
        rules = next(a for a in args if a.startswith("--host-resolver-rules="))
        self.assertIn("MAP * ~NOTFOUND", rules)
        self.assertIn("EXCLUDE chat.corp.example", rules)
        self.assertIn("EXCLUDE *.cdn.corp.example", rules)
        self.assertNotIn("EXCLUDE corp.example,", rules + ",")
        self.assertIn("--no-proxy-server", args)

    def test_page_filter_uses_the_exact_chat_host(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c")
        url = "https://chat.corp.example/c"
        self.assertTrue(browser.request_is_allowed("https://chat.corp.example/api", url))
        self.assertFalse(browser.request_is_allowed("https://static.corp.example/app.js", url))
        egress.configure("open", url)
        self.assertTrue(browser.request_is_allowed("https://static.corp.example/app.js", url))


class ProxyTest(_PolicyReset):
    def test_company_proxy_is_used_and_still_resolvable(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c")
        with patch.object(browser, "system_proxy", return_value=("http=proxy.corp.example:8080;https=proxy.corp.example:8443", "")):
            args = browser.browser_quiet_args("https://chat.corp.example/c")
        self.assertIn("--proxy-server=http=proxy.corp.example:8080;https=proxy.corp.example:8443", args)
        self.assertNotIn("--no-proxy-server", args)
        rules = next(a for a in args if a.startswith("--host-resolver-rules="))
        self.assertIn("EXCLUDE proxy.corp.example", rules)

    def test_pac_file(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c")
        with patch.object(browser, "system_proxy", return_value=("", "http://wpad.corp.example/proxy.pac")):
            args = browser.browser_quiet_args("https://chat.corp.example/c")
        self.assertIn("--proxy-pac-url=http://wpad.corp.example/proxy.pac", args)
        self.assertIn("EXCLUDE wpad.corp.example", next(a for a in args if a.startswith("--host-resolver-rules=")))

    def test_env_proxy_off_windows(self) -> None:
        with patch.object(browser.os, "name", "posix"), patch.dict(os.environ, {"HTTPS_PROXY": "http://p.corp.example:3128"}):
            self.assertEqual(browser.system_proxy(), ("http://p.corp.example:3128", ""))


class ResolverBlockNoticeTest(_PolicyReset):
    def test_a_blocked_sign_in_page_is_explained_once(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c")
        browser._resolver_reported.discard("login.idp.example")
        err = io.StringIO()
        with redirect_stderr(err):
            browser._report_resolver_block("login.idp.example", navigation=True)
            browser._report_resolver_block("login.idp.example", navigation=True)
        self.assertEqual(err.getvalue().count("Blocked:"), 1)
        self.assertIn("crit config --allow-host login.idp.example", err.getvalue())
        self.assertIn("login.idp.example", browser.drain_blocked_hosts())


class GitLabTest(_PolicyReset):
    def test_gitlab_host_must_be_allowed(self) -> None:
        from critique_bot import gitlab

        egress.configure("strict", "https://chat.corp.example/c")
        with self.assertRaises(gitlab.GitLabError) as caught:
            gitlab._require_safe_url("https://gitlab.corp.example/api/v4/projects/1")
        self.assertIn("allowed_hosts", str(caught.exception))
        egress.configure("strict", "https://chat.corp.example/c", ("gitlab.corp.example",))
        gitlab._require_safe_url("https://gitlab.corp.example/api/v4/projects/1")
        # gitlab.base_url's host is reachable by the GitLab client, not by the browser.
        egress.configure("strict", "https://chat.corp.example/c", gitlab_url="https://gitlab.corp.example")
        gitlab._require_safe_url("https://gitlab.corp.example/api/v4/projects/1")
        self.assertNotIn("gitlab.corp.example", egress.policy().hosts())


class AgentSettingsTest(_PolicyReset):
    def test_strict_forces_offline_and_no_fetch(self) -> None:
        from critique_bot.agent import effective_settings

        egress.configure("strict", "https://chat.corp.example/c")
        settings = effective_settings({"offline": False, "web_fetch": True, "network_commands": "ask", "theme": "x"})
        self.assertEqual(settings, {"offline": True, "web_fetch": False, "network_commands": "block", "theme": "x"})
        egress.configure("open", "https://chat.corp.example/c")
        self.assertEqual(effective_settings({"web_fetch": True}), {"web_fetch": True})


class FriendlyErrorsTest(unittest.TestCase):
    def test_unexpected_failure_is_one_plain_sentence(self) -> None:
        from critique_bot import cli

        err = io.StringIO()
        with patch.object(cli, "_main", side_effect=RuntimeError("disk on fire")), redirect_stderr(err):
            code = cli.main(["anything"])
        self.assertEqual(code, 1)
        self.assertIn("crit stopped: disk on fire", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_interrupt_says_stopped(self) -> None:
        from critique_bot import cli

        err = io.StringIO()
        with patch.object(cli, "_main", side_effect=KeyboardInterrupt), redirect_stderr(err):
            self.assertEqual(cli.main([]), 130)
        self.assertIn("Stopped.", err.getvalue())


if __name__ == "__main__":
    unittest.main()
