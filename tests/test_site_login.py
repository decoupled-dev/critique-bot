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
        with mock.patch.object(browser, "system_proxy", return_value=("", "")):
            text = browser.friendly_navigation_error("https://chat.corp.example/", error)
        self.assertIn("crit config --site-user NAME --site-password", text)
        self.assertIn("kinit", text)
        self.assertNotIn("Check the address", text)
        integrations.configure(site_user="jdoe", site_password=PASSWORD, site_host="chat.corp.example")
        self.assertIn("(site_login) was not accepted", browser.friendly_navigation_error("https://chat.corp.example/", error))


PROXY_PASSWORD = "proxy-pass-" + "z" * 16


class ProxyLoginTests(_Isolated):
    """The company proxy asks every browser for a password; Chrome only fills in a saved one.
    crit's own browser profile has none, so it gives proxy_login."""

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.config("--url", "https://chat.corp.example/")[0], 0)

    def tearDown(self) -> None:
        integrations.configure()
        super().tearDown()

    def test_flags_and_display_never_show_the_password(self) -> None:
        self.assertEqual(self.config("--proxy-user", "CORP\\jdoe")[0], 0)
        code, out, _ = self.config("--proxy-password", PROXY_PASSWORD)
        self.assertEqual((code, PROXY_PASSWORD in out), (0, False))
        _, out, _ = self.config()
        self.assertIn("Proxy sign-in: CORP\\jdoe, password set", out)
        self.assertNotIn(PROXY_PASSWORD, out)
        load_config(self.user)
        self.assertEqual(browser.proxy_login(), ("CORP\\jdoe", PROXY_PASSWORD))

    def test_login_inside_https_proxy_is_used_and_kept_off_the_command_line(self) -> None:
        env = {"HTTPS_PROXY": f"http://jdoe:{PROXY_PASSWORD}@proxy.corp.example:8080", "NO_PROXY": "", "no_proxy": ""}
        with mock.patch.dict("os.environ", env), mock.patch("shutil.which", return_value=None):
            self.assertEqual(browser.proxy_login(), ("jdoe", PROXY_PASSWORD))
            args, _hosts = browser._strict_proxy_args()
        self.assertEqual(args, ["--proxy-server=http://proxy.corp.example:8080"])

    def test_a_known_proxy_gets_the_login_for_every_request(self) -> None:
        integrations.configure(proxy_user="jdoe", proxy_password=PROXY_PASSWORD)
        kwargs: dict = {}
        with mock.patch.object(browser, "proxy_bypass", return_value="localhost,*.corp.example"):
            args = browser.apply_proxy_login(
                ["--a", "--proxy-server=proxy.corp.example:8080", "--proxy-bypass-list=x"], kwargs, "https://chat.corp.example/")
        self.assertEqual(args, ["--a"])
        self.assertEqual(kwargs["proxy"], {"server": "proxy.corp.example:8080", "username": "jdoe", "password": PROXY_PASSWORD,
                                           "bypass": "localhost,*.corp.example"})

    def test_gnome_manual_proxy_and_its_ignore_hosts(self) -> None:
        answers = {
            ("org.gnome.system.proxy", "mode"): "'manual'",
            ("org.gnome.system.proxy.https", "host"): "''",
            ("org.gnome.system.proxy.https", "port"): "0",
            ("org.gnome.system.proxy.http", "host"): "'proxy.corp.example'",
            ("org.gnome.system.proxy.http", "port"): "8080",
            ("org.gnome.system.proxy", "ignore-hosts"): "['localhost', '127.0.0.0/8', '*.corp.example']",
        }

        def run(cmd, **_kwargs):
            return mock.Mock(returncode=0, stdout=answers.get((cmd[2], cmd[3]), "''") + "\n")

        clean = {k: v for k, v in __import__("os").environ.items()
                 if k.upper() not in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")}
        with mock.patch.dict("os.environ", clean, clear=True), \
                mock.patch.object(browser.sys, "platform", "linux"), \
                mock.patch("shutil.which", return_value="/usr/bin/gsettings"), \
                mock.patch.object(browser.subprocess, "run", side_effect=run):
            self.assertEqual(browser.system_proxy(), ("proxy.corp.example:8080", ""))
            self.assertEqual(browser.proxy_bypass(), "localhost,127.0.0.0/8,*.corp.example")

    def test_a_pac_proxy_gets_the_login_through_the_chat_page(self) -> None:
        integrations.configure(proxy_user="jdoe", proxy_password=PROXY_PASSWORD)
        kwargs: dict = {}
        args = browser.apply_proxy_login(["--proxy-pac-url=http://wpad/p.pac"], kwargs, "https://chat.corp.example/x")
        self.assertEqual(args, ["--proxy-pac-url=http://wpad/p.pac"])
        self.assertNotIn("proxy", kwargs)
        self.assertEqual(kwargs["http_credentials"]["origin"], "https://chat.corp.example")

    def test_no_proxy_no_login(self) -> None:
        integrations.configure(proxy_user="jdoe", proxy_password=PROXY_PASSWORD)
        kwargs: dict = {}
        self.assertEqual(browser.apply_proxy_login(["--no-proxy-server"], kwargs, "https://chat.corp.example/"), ["--no-proxy-server"])
        self.assertEqual(kwargs, {})
        integrations.configure()
        with mock.patch.object(browser, "system_proxy", return_value=("", "")):
            self.assertEqual(browser.apply_proxy_login(["--proxy-server=p:1"], kwargs, "https://chat.corp.example/"), ["--proxy-server=p:1"])
        self.assertEqual(kwargs, {})

    def test_the_error_names_the_proxy_first(self) -> None:
        error = "Page.goto: net::ERR_INVALID_AUTH_CREDENTIALS at https://chat.corp.example/"
        with mock.patch.object(browser, "system_proxy", return_value=("", "")):
            text = browser.friendly_navigation_error("https://chat.corp.example/", error)
        self.assertIn("crit config --proxy-user NAME --proxy-password", text)
        integrations.configure(proxy_user="jdoe", proxy_password=PROXY_PASSWORD)
        self.assertIn("(proxy_login) was not accepted", browser.friendly_navigation_error("https://chat.corp.example/", error))
