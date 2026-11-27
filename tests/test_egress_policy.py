# SPDX-License-Identifier: Apache-2.0
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

from critique_bot import egress

from critique_bot.webchat import browser
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
        self.assertEqual(config.page_network, "open")
        self.assertFalse(config.filter_chat_page)  # the page works as for a person
        policy = egress.policy()
        self.assertTrue(policy.strict and policy.configured)
        self.assertFalse(policy.page_restricted)
        self.assertEqual(policy.hosts(), ("chat.corp.example",))

    def test_page_network_strict_filters_the_page(self) -> None:
        config = load_config(_write_config(self.folder, page_network="strict"))
        self.assertEqual(config.page_network, "strict")
        self.assertTrue(config.filter_chat_page)
        self.assertTrue(egress.policy().page_restricted)
        with self.assertRaises(ConfigError):
            load_config(_write_config(self.folder, page_network="sometimes"))

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
        policy = egress.configure("strict", "https://chat.corp.example/c", ("api.corp.example",), page_network="strict")
        self.assertTrue(policy.permits("chat.corp.example"))
        self.assertTrue(policy.permits("api.corp.example"))
        self.assertTrue(policy.permits("v1.api.corp.example"))
        self.assertTrue(policy.permits("127.0.0.1"))
        for host in ("corp.example", "other.corp.example", "evil.example", "chat.corp.example.evil.example", ""):
            self.assertFalse(policy.permits(host), host)

    def test_require_explains_what_to_do(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c", page_network="strict")
        with self.assertRaises(PermissionError) as caught:
            egress.require("gitlab.corp.example", "posting to GitLab")
        self.assertIn('"gitlab.corp.example" to allowed_hosts', str(caught.exception))


class BrowserTest(_PolicyReset):
    def test_resolver_allows_only_the_policy_hosts(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c", ("cdn.corp.example",), page_network="strict")
        with patch.object(browser, "system_proxy", return_value=("", "")):
            args = browser.browser_quiet_args("https://chat.corp.example/c")
        rules = next(a for a in args if a.startswith("--host-resolver-rules="))
        self.assertIn("MAP * ~NOTFOUND", rules)
        self.assertIn("EXCLUDE chat.corp.example", rules)
        self.assertIn("EXCLUDE *.cdn.corp.example", rules)
        self.assertNotIn("EXCLUDE corp.example,", rules + ",")
        self.assertIn("--no-proxy-server", args)

    def test_page_filter_uses_the_exact_chat_host(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c", page_network="strict")
        url = "https://chat.corp.example/c"
        self.assertTrue(browser.request_is_allowed("https://chat.corp.example/api", url))
        self.assertFalse(browser.request_is_allowed("https://static.corp.example/app.js", url))
        egress.configure("open", url)
        self.assertTrue(browser.request_is_allowed("https://static.corp.example/app.js", url))


class OpenChatPageTest(_PolicyReset):
    """Strict egress with "page_network": "open" (the default): the page works as for a person."""

    def test_no_catch_all_and_the_system_proxy(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c", ("cdn.corp.example",))
        self.assertTrue(egress.strict())
        self.assertFalse(egress.page_restricted())
        with patch.object(browser, "system_proxy", return_value=("", "")):
            args = browser.merge_resolver_rules(browser.browser_quiet_args("https://chat.corp.example/c"))
        self.assertNotIn("--no-proxy-server", args)
        self.assertFalse(any(a.startswith(("--proxy-server", "--proxy-pac-url")) for a in args))
        rules = next(a for a in args if a.startswith("--host-resolver-rules="))
        self.assertNotIn("MAP * ~NOTFOUND", rules)

    def test_browser_services_stay_off(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c")
        args = browser.merge_resolver_rules(browser.browser_quiet_args("https://chat.corp.example/c"))
        for quiet in browser.BROWSER_QUIET_ARGS:
            self.assertIn(quiet, args)
        rules = next(a for a in args if a.startswith("--host-resolver-rules="))
        for host in ("update.googleapis.com", "safebrowsing.googleapis.com", "*.events.data.microsoft.com", "edge.microsoft.com"):
            self.assertIn(f"MAP {host} ~NOTFOUND", rules)

    def test_profile_prefs_are_written(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c")
        browser.apply_private_prefs(self.folder)
        local_state = json.loads((self.folder / "Local State").read_text(encoding="utf-8"))
        prefs = json.loads((self.folder / "Default" / "Preferences").read_text(encoding="utf-8"))
        self.assertEqual(local_state["dns_over_https"]["mode"], "off")
        self.assertFalse(prefs["safebrowsing"]["enabled"])
        self.assertFalse(prefs["signin"]["allowed"])

    def test_page_is_not_filtered_but_crit_stays_strict(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c")
        url = "https://chat.corp.example/c"
        self.assertTrue(browser.request_is_allowed("https://static.corp.example/app.js", url))
        self.assertFalse(egress.policy().permits("evil.example"))
        from critique_bot.agent import effective_settings

        self.assertEqual(effective_settings({"web_fetch": True})["web_fetch"], False)

    def test_cdp_url_still_refused(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(_write_config(self.folder, cdp_url="http://127.0.0.1:9222"))


class ProxyTest(_PolicyReset):
    def test_company_proxy_is_used_and_still_resolvable(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c", page_network="strict")
        with patch.object(browser, "system_proxy", return_value=("http=proxy.corp.example:8080;https=proxy.corp.example:8443", "")):
            args = browser.browser_quiet_args("https://chat.corp.example/c")
        self.assertIn("--proxy-server=http=proxy.corp.example:8080;https=proxy.corp.example:8443", args)
        self.assertNotIn("--no-proxy-server", args)
        rules = next(a for a in args if a.startswith("--host-resolver-rules="))
        self.assertIn("EXCLUDE proxy.corp.example", rules)

    def test_pac_file(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c", page_network="strict")
        with patch.object(browser, "system_proxy", return_value=("", "http://wpad.corp.example/proxy.pac")):
            args = browser.browser_quiet_args("https://chat.corp.example/c")
        self.assertIn("--proxy-pac-url=http://wpad.corp.example/proxy.pac", args)
        self.assertIn("EXCLUDE wpad.corp.example", next(a for a in args if a.startswith("--host-resolver-rules=")))

    def test_env_proxy_off_windows(self) -> None:
        with patch.object(browser.os, "name", "posix"), patch.dict(os.environ, {"HTTPS_PROXY": "http://p.corp.example:3128"}):
            self.assertEqual(browser.system_proxy(), ("http://p.corp.example:3128", ""))


class ResolverBlockNoticeTest(_PolicyReset):
    def test_a_blocked_sign_in_page_is_explained_once(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c", page_network="strict")
        browser._resolver_reported.discard("login.idp.example")
        err = io.StringIO()
        with redirect_stderr(err):
            browser._report_resolver_block("login.idp.example", navigation=True)
            browser._report_resolver_block("login.idp.example", navigation=True)
        self.assertEqual(err.getvalue(), "")  # never on screen: the log file has it, once
        logged = __import__("critique_bot.log", fromlist=["x"]).log_file_path().read_text(encoding="utf-8")
        self.assertEqual(logged.count("Blocked: the page sent the browser to login.idp.example"), 1)
        self.assertIn("crit config --allow-host login.idp.example", logged)
        self.assertIn("login.idp.example", browser.drain_blocked_hosts())


class GitLabTest(_PolicyReset):
    def test_gitlab_host_must_be_allowed(self) -> None:
        from critique_bot import gitlab

        egress.configure("strict", "https://chat.corp.example/c", page_network="strict")
        with self.assertRaises(gitlab.GitLabError) as caught:
            gitlab._require_safe_url("https://gitlab.corp.example/api/v4/projects/1")
        self.assertIn("allowed_hosts", str(caught.exception))
        egress.configure("strict", "https://chat.corp.example/c", ("gitlab.corp.example",), page_network="strict")
        gitlab._require_safe_url("https://gitlab.corp.example/api/v4/projects/1")
        # gitlab.base_url's host is reachable by the GitLab client, not by the browser.
        egress.configure("strict", "https://chat.corp.example/c", gitlab_url="https://gitlab.corp.example", page_network="strict")
        gitlab._require_safe_url("https://gitlab.corp.example/api/v4/projects/1")
        self.assertNotIn("gitlab.corp.example", egress.policy().hosts())


class AgentSettingsTest(_PolicyReset):
    def test_strict_forces_offline_and_no_fetch(self) -> None:
        from critique_bot.agent import effective_settings

        egress.configure("strict", "https://chat.corp.example/c", page_network="strict")
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


class FriendlyNavigationErrorTest(_PolicyReset):
    def test_messages(self) -> None:
        egress.configure("strict", "https://chat.corp.example/c", page_network="strict")
        url = "https://chat.corp.example/c"
        cases = {
            "net::ERR_NAME_NOT_RESOLVED at x\nCall log:\n  - navigating": "does not resolve",
            "net::ERR_PROXY_CONNECTION_FAILED": "company proxy",
            "net::ERR_CERT_AUTHORITY_INVALID": "certificate",
            "net::ERR_CONNECTION_REFUSED": "refused the connection",
            "timeout": "did not load in time",
            "something odd\nCall log: ...": "something odd",
        }
        for error, expected in cases.items():
            message = browser.friendly_navigation_error(url, error)
            self.assertIn(expected, message, error)
            self.assertNotIn("Call log", message)
            self.assertNotIn("\n", message)
