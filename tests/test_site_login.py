"""The chat site's own sign-in (HTTP or Windows Negotiate/NTLM) on machines without SSO.

On Windows the signed-in account answers such a sign-in; on Linux the browser crit drives
had nothing to give, so navigation failed with net::ERR_INVALID_AUTH_CREDENTIALS and crit
said to check the address. crit now gives the person's own ``site_login`` to that one host,
and allowlists the site's hosts so a Kerberos ticket works.
"""

from __future__ import annotations

from unittest import mock

from critique_bot import browser, integrations
from critique_bot.config import load_config

from test_config_cli import _Isolated

PASSWORD = "site-pass-" + "q" * 16


class SiteLoginConfigTests(_Isolated):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.config("--url", "https://chat.corp.example/")[0], 0)

    def tearDown(self) -> None:
        integrations.configure()
        super().tearDown()

    def test_flags_save_to_the_user_config_and_never_print_the_password(self) -> None:
        self.assertEqual(self.config("--site-user", "jdoe")[0], 0)
        code, out, _ = self.config("--site-password", PASSWORD)
        self.assertEqual(code, 0)
        self.assertNotIn(PASSWORD, out)
        self.assertEqual(self.data()["site_login"], {"username": "jdoe", "password": PASSWORD})
        _, out, _ = self.config()
        self.assertIn("Site sign-in: jdoe, password set, for the url's host", out)
        self.assertNotIn(PASSWORD, out)

        loaded = load_config(self.user)
        self.assertTrue(loaded.site_login.ready)
        self.assertEqual(loaded.site_login.host, "chat.corp.example")  # the url's host by default
        self.assertNotIn(PASSWORD, repr(loaded))
        self.assertTrue(integrations.current().site_login_ready)

        self.assertEqual(self.config("--site-login-host", "sso.corp.example")[0], 0)
        self.assertEqual(load_config(self.user).site_login.host, "sso.corp.example")
        self.assertEqual(self.config("--site-password", "none")[0], 0)
        self.assertFalse(load_config(self.user).site_login.ready)

    def test_environment_overrides(self) -> None:
        with mock.patch.dict("os.environ", {"CRIT_SITE_USER": "envuser", "CRIT_SITE_PASSWORD": PASSWORD}):
            login = load_config(self.user).site_login
        self.assertEqual((login.username, login.password, login.host), ("envuser", PASSWORD, "chat.corp.example"))


class BrowserSiteAuthTests(_Isolated):
    def tearDown(self) -> None:
        integrations.configure()
        super().tearDown()

    def test_credentials_only_for_the_site_origin(self) -> None:
        self.assertIsNone(browser.site_http_credentials("https://chat.corp.example/"))
        integrations.configure(site_user="jdoe", site_password=PASSWORD, site_host="chat.corp.example")
        self.assertEqual(
            browser.site_http_credentials("https://chat.corp.example:8443/c/new"),
            {"username": "jdoe", "password": PASSWORD, "origin": "https://chat.corp.example:8443"},
        )
        integrations.configure(site_user="jdoe", site_password=PASSWORD, site_host="sso.corp.example")
        self.assertEqual(browser.site_http_credentials("https://chat.corp.example/")["origin"], "https://sso.corp.example")

    def test_kerberos_allowlist_names_the_site_hosts(self) -> None:
        self.assertEqual(browser.site_auth_args("https://chat.corp.example/x"),
                         ["--auth-server-allowlist=chat.corp.example"])
        integrations.configure(site_user="jdoe", site_password=PASSWORD, site_host="sso.corp.example")
        self.assertEqual(browser.site_auth_args("https://chat.corp.example/x"),
                         ["--auth-server-allowlist=chat.corp.example,sso.corp.example"])
        self.assertEqual(browser.site_auth_args(None), ["--auth-server-allowlist=sso.corp.example"])

    def test_the_error_names_the_sign_in_not_the_address(self) -> None:
        error = "Page.goto: net::ERR_INVALID_AUTH_CREDENTIALS at https://chat.corp.example/"
        text = browser.friendly_navigation_error("https://chat.corp.example/", error)
        self.assertIn("crit config --site-user NAME --site-password", text)
        self.assertIn("kinit", text)
        self.assertNotIn("Check the address", text)
        integrations.configure(site_user="jdoe", site_password=PASSWORD, site_host="chat.corp.example")
        self.assertIn("did not accept the sign-in", browser.friendly_navigation_error("https://chat.corp.example/", error))
