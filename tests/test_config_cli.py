"""crit's settings without hand-editing JSON: the user config, ``crit config``, the first-run
questions, and the questions asked later (blocked sites, the GitLab address)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from critique_bot import config_prompts, egress, redaction

from critique_bot.webchat import browser
from critique_bot.agent_cli import main as crit_main
from critique_bot.cli import main as bot_main
from critique_bot.config import load_config, user_config_path
from critique_bot.config_cmd import main as config_main
from critique_bot.config_cmd import resolve_config_path

REPO = Path(__file__).resolve().parents[1]


def _capture(fn, *args, **kwargs) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


class _Isolated(unittest.TestCase):
    """A temp user-config folder, a temp project, and no CRITIQUE_* / CI variables."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.xdg = self.root / "xdg"
        self.project = self.root / "project"
        self.project.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("CRITIQUE_", "CRIT_", "CI"))}
        clean["XDG_CONFIG_HOME"] = str(self.xdg)
        clean["APPDATA"] = str(self.xdg)
        clean["CRIT_HOME"] = str(self.root / "crit-home")
        self._env = patch.dict(os.environ, clean, clear=True)
        self._env.start()
        self._policy = egress.policy()
        self.user = user_config_path()
        # Never resolve against the folder the tests run from (it may hold someone's config).
        self._cwd = os.getcwd()
        os.chdir(self.project)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        self._env.stop()
        egress._policy = self._policy  # load_config sets the process-wide policy
        config_prompts.disable()
        redaction.set_custom_terms(())
        redaction.set_pii_redaction(True)
        self._tmp.cleanup()

    def config(self, *argv: str) -> tuple[int, str, str]:
        return _capture(config_main, ["--repo-dir", str(self.project), *argv], is_tty=lambda: False)

    def data(self, path: Path | None = None) -> dict:
        return json.loads((path or self.user).read_text(encoding="utf-8"))


class UserConfigPathTests(unittest.TestCase):
    """Everything crit keeps for a user is in one folder: ~/.crit, %USERPROFILE%\\.crit, or CRIT_HOME.

    Pure paths stand in for Path so each OS's branch can be checked on the other."""

    def _paths(self, name: str, env: dict) -> tuple[str, str, str, str]:
        from pathlib import PurePosixPath

        from critique_bot import config as cfg

        clean = {k: v for k, v in os.environ.items() if k not in ("CRIT_HOME", "HOME", "USERPROFILE")}
        with patch.object(os, "name", name), patch.dict(os.environ, {**clean, **env}, clear=True), patch(
            "critique_bot.config.Path", PurePosixPath
        ):
            return (
                str(cfg.crit_home()), str(user_config_path()), str(cfg.default_browser_profile()),
                str(cfg.company_cache_path()),
            )

    def test_posix_home(self) -> None:
        self.assertEqual(
            self._paths("posix", {"HOME": "/home/me", "XDG_CONFIG_HOME": "/x/conf"}),
            ("/home/me/.crit", "/home/me/.crit/config.json", "/home/me/.crit/browser-profile",
             "/home/me/.crit/cache/config-file.cache.json"),
        )

    def test_windows_userprofile(self) -> None:
        home, user, profile, _ = self._paths("nt", {"USERPROFILE": "/Users/me", "HOME": "/elsewhere", "APPDATA": "/appdata"})
        self.assertEqual((home, user, profile), ("/Users/me/.crit", "/Users/me/.crit/config.json", "/Users/me/.crit/browser-profile"))

    def test_crit_home_overrides(self) -> None:
        for name in ("posix", "nt"):
            home, user, _, _ = self._paths(name, {"CRIT_HOME": "/srv/crit", "HOME": "/home/me", "USERPROFILE": "/Users/me"})
            self.assertEqual((home, user), ("/srv/crit", "/srv/crit/config.json"))


class ResolutionTests(_Isolated):
    def test_order(self) -> None:
        # nothing anywhere: the user config, not yet there
        loc = resolve_config_path(None, self.project)
        self.assertEqual((loc.path, loc.source, loc.exists), (self.user, "user", False))
        # a config.json in the project root (not committed) wins over the user config
        root_cfg = self.project / "config.json"
        root_cfg.write_text("{}", encoding="utf-8")
        self.assertEqual(resolve_config_path(None, self.project).source, "project-root")
        # the config stored in .crit/settings.json wins over that
        stored = self.root / "stored.json"
        stored.write_text("{}", encoding="utf-8")
        (self.project / ".crit").mkdir()
        (self.project / ".crit" / "settings.json").write_text(json.dumps({"config": str(stored)}), encoding="utf-8")
        loc = resolve_config_path(None, self.project)
        self.assertEqual((loc.path, loc.source), (stored, "project"))
        # CRITIQUE_CONFIG wins over the project; --config over everything
        with patch.dict(os.environ, {"CRITIQUE_CONFIG": str(self.root / "env.json")}):
            self.assertEqual(resolve_config_path(None, self.project).source, "env")
            self.assertEqual(resolve_config_path(self.root / "flag.json", self.project).source, "flag")

    def test_stored_config_that_is_missing_falls_through(self) -> None:
        (self.project / ".crit").mkdir()
        (self.project / ".crit" / "settings.json").write_text(json.dumps({"config": "gone.json"}), encoding="utf-8")
        self.assertEqual(resolve_config_path(None, self.project).source, "user")


class ConfigCommandTests(_Isolated):
    def test_url_writes_a_private_loadable_user_config(self) -> None:
        code, out, err = self.config("--url", "https://chat.corp.example/", "--redact", "Acme Corp, Globex", "--redact", "Initech")
        self.assertEqual(code, 0, err)
        self.assertIn(f"Saved {self.user}", out)
        self.assertIn("now hiding Acme Corp, Globex, Initech", out)
        config = load_config(self.user)
        self.assertEqual(config.url, "https://chat.corp.example/")
        self.assertEqual(config.redact_terms, ("Acme Corp", "Globex", "Initech"))
        if os.name != "nt":
            self.assertEqual(self.user.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.user.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(sorted(p.name for p in self.user.parent.iterdir() if p.name != "logs"), ["config.json"])  # no temp file left

    def test_show_prints_plain_settings_without_tokens(self) -> None:
        self.config("--url", "https://chat.corp.example/", "--redact", "Acme Corp", "--gitlab-url", "https://gitlab.corp.example/")
        code, out, _ = self.config()
        self.assertEqual(code, 0)
        self.assertIn(f"Settings file: {self.user} (your user settings)", out)
        self.assertIn("url: https://chat.corp.example/", out)
        self.assertIn("Network: strict", out)
        self.assertIn("Names hidden: 1 (Acme Corp)", out)
        self.assertIn("Personal data hidden: on", out)
        self.assertIn("GitLab: https://gitlab.corp.example", out)
        self.assertIn("GitLab token: not set", out)
        secret = "glpat-" + "x" * 20
        self.assertEqual(self.config("--gitlab-token", secret)[0], 0)
        code, out, _ = self.config()
        self.assertIn("GitLab token: set", out)
        self.assertNotIn(secret, out)  # a token is never printed, only whether it is set

    def test_show_before_setup(self) -> None:
        code, out, _ = self.config()
        self.assertEqual(code, 0)
        self.assertIn("Nothing is set up yet", out)

    def test_path(self) -> None:
        self.assertEqual(self.config("--path")[1].strip(), str(self.user))
        other = self.root / "other.json"
        self.assertEqual(self.config("--config", str(other), "--path")[1].strip(), str(other))

    def test_custom_url_gets_starter_selectors_and_a_hint(self) -> None:
        code, out, _ = self.config("--url", "https://chat.corp.example/")
        self.assertEqual(code, 0)
        self.assertIn("crit setup", out)
        self.assertEqual(load_config(self.user).url, "https://chat.corp.example/")

    def test_url_must_be_https(self) -> None:
        code, _, err = self.config("--url", "http://chat.corp.example/")
        self.assertEqual(code, 2)
        self.assertIn("https://", err)
        self.assertFalse(self.user.exists())

    def test_hosts_add_and_remove(self) -> None:
        self.config("--url", "https://chat.corp.example/")
        code, out, _ = self.config("--allow-host", "https://CDN.corp.example/x", "--allow-host", "api.corp.example")
        self.assertEqual(code, 0)
        self.assertEqual(self.data()["allowed_hosts"], ["cdn.corp.example", "api.corp.example"])
        self.config("--remove-host", "cdn.corp.example")
        self.assertEqual(self.data()["allowed_hosts"], ["api.corp.example"])
        code, _, err = self.config("--allow-host", "not a host")
        self.assertEqual(code, 2)
        self.assertIn("not a host name", err)

    def test_redact_validation_and_unredact(self) -> None:
        self.config("--url", "https://chat.corp.example/", "--redact", "Acme Corp,Globex")
        code, _, err = self.config("--redact", "ab")
        self.assertEqual(code, 2)
        self.assertIn("too short", err)
        self.assertEqual(self.data()["redact_terms"], ["Acme Corp", "Globex"])
        self.config("--unredact", "acme corp")
        self.assertEqual(self.data()["redact_terms"], ["Globex"])

    def test_pii_egress_model_gitlab_selector(self) -> None:
        self.config("--url", "https://chat.corp.example/")
        code, out, _ = self.config("--pii", "off", "--egress", "open", "--model", "GPT-5", "--selector", "send_button=button.go")
        self.assertEqual(code, 0)
        self.assertIn("NO LONGER hidden", out)
        data = self.data()
        self.assertEqual((data["redact_pii"], data["egress"], data["model"]), (False, "open", "GPT-5"))
        self.assertEqual(data["selectors"]["send_button"], "button.go")
        self.assertEqual(self.config("--selector", "nope=x")[0], 2)
        self.assertEqual(self.config("--selector", "prompt_input=")[0], 2)
        self.assertEqual(self.config("--gitlab-url", "http://gitlab.corp.example")[0], 2)
        self.config("--gitlab-url", "https://gitlab.corp.example/group/?x=1")
        self.assertEqual(self.data()["gitlab"]["base_url"], "https://gitlab.corp.example/group")
        self.config("--gitlab-url", "")
        self.assertNotIn("gitlab", self.data())

    def test_local_backend(self) -> None:
        code, out, err = self.config("--backend", "local", "--model", "qwen2.5-coder-14b")
        self.assertEqual(code, 0, err)
        config = load_config(self.user)
        self.assertTrue(config.is_local)
        self.assertEqual(config.url, "http://127.0.0.1:8080")
        code, out, err = self.config("--local-url", "gpu.corp.example:8080")  # the host is allowed with it
        self.assertEqual(code, 0, err)
        self.assertIn("gpu.corp.example", out)
        config = load_config(self.user)
        self.assertEqual(config.url, "http://gpu.corp.example:8080")
        self.assertIn("gpu.corp.example", config.allowed_hosts)
        self.assertEqual(self.config("--local-url", "gpu.corp.example")[0], 2)  # no port

    def test_selector_key_with_nothing_clears_an_optional_one(self) -> None:
        self.config("--url", "https://chat.corp.example/", "--selector", "send_button=button.send")
        self.assertEqual(self.data()["selectors"]["send_button"], "button.send")
        code, _, err = self.config("--selector", "send_button=")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.data()["selectors"]["send_button"], "")

    def test_help_lists_a_flag_for_every_first_run_question(self) -> None:
        from critique_bot.config_cmd import build_parser

        text = build_parser().format_help()
        for flag in ("--backend", "--local-url", "--model", "--url", "--selector", "--redact", "--unredact", "--check"):
            self.assertIn(flag, text)

    def test_check_pings_the_local_server(self) -> None:
        from critique_bot import config_cmd
        from critique_bot.local_model import Probe

        self.config("--backend", "local")
        location = resolve_config_path(None, self.project)
        seen: list[str] = []
        ok = lambda url: seen.append(url) or Probe("ok", "reachable, 1 model: qwen", ["qwen"])  # noqa: E731
        code, out, _ = _capture(config_cmd.check, location, probe_server=ok)
        self.assertEqual(code, 0)
        self.assertEqual(seen, ["http://127.0.0.1:8080"])
        self.assertIn("Checking http://127.0.0.1:8080 ...\nreachable, 1 model: qwen", out)
        refused = lambda url: Probe("refused", "refused: nothing is listening on 127.0.0.1:8080")  # noqa: E731
        code, out, _ = _capture(config_cmd.check, location, probe_server=refused)
        self.assertEqual(code, 1)
        self.assertIn("refused: nothing is listening", out)

    def test_check_web_ui_reports_the_site(self) -> None:
        from critique_bot import config_cmd
        from critique_bot.local_model import Probe

        self.config("--url", "https://chat.corp.example/")
        location = resolve_config_path(None, self.project)
        site = lambda url: Probe("unknown-host", "unknown host: chat.corp.example")  # noqa: E731
        code, out, _ = _capture(config_cmd.check, location, probe_site=site)
        self.assertEqual(code, 1)
        self.assertIn("unknown host: chat.corp.example", out)
        with patch("critique_bot.local_model.probe_site", return_value=Probe("ok", "reachable: x")):
            self.assertEqual(self.config("--check")[0], 0)

    def test_a_change_that_breaks_a_working_config_is_refused(self) -> None:
        self.config("--url", "https://chat.corp.example/")
        before = self.user.read_text(encoding="utf-8")
        data = self.data()
        data["cdp_url"] = "http://127.0.0.1:9222"
        data["egress"] = "open"
        self.user.write_text(json.dumps(data), encoding="utf-8")
        code, _, err = self.config("--egress", "strict")
        self.assertEqual(code, 2)
        self.assertIn("cdp_url", err)
        self.assertEqual(self.data()["egress"], "open")
        self.assertNotEqual(before, "")

    def test_partial_setup_is_saved_with_a_note(self) -> None:
        code, out, _ = self.config("--redact", "Acme Corp")
        self.assertEqual(code, 0)
        self.assertIn("Not ready yet", out)
        self.assertEqual(self.data()["redact_terms"], ["Acme Corp"])

    def test_reset(self) -> None:
        self.config("--url", "https://chat.corp.example/")
        code, _, err = self.config("--reset")
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        self.assertTrue(self.user.exists())
        code, _, _ = _capture(
            config_main, ["--repo-dir", str(self.project), "--reset"], is_tty=lambda: True, confirm=lambda q: False
        )
        self.assertEqual(code, 0)
        self.assertTrue(self.user.exists())
        code, _, _ = _capture(
            config_main, ["--repo-dir", str(self.project), "--reset"], is_tty=lambda: True, confirm=lambda q: True
        )
        self.assertFalse(self.user.exists())
        self.config("--url", "https://chat.corp.example/")
        self.assertEqual(self.config("--reset", "--yes")[0], 0)
        self.assertFalse(self.user.exists())

    def test_other_file_with_config_flag(self) -> None:
        other = self.root / "deep" / "team.json"
        code, out, _ = self.config("--config", str(other), "--url", "https://chat.corp.example/")
        self.assertEqual(code, 0)
        self.assertTrue(other.is_file())
        self.assertFalse(self.user.exists())

    def test_both_entry_points_reach_it(self) -> None:
        with patch("critique_bot.config_cmd.main", return_value=0) as run:
            self.assertEqual(crit_main(["config", "--url", "https://x.example/"]), 0)
            run.assert_called_once_with(["--url", "https://x.example/"])
        with patch("critique_bot.config_cmd.main", return_value=0) as run:
            self.assertEqual(bot_main(["config", "--path"]), 0)
            run.assert_called_once_with(["--path"])


class FirstRunTests(_Isolated):
    def _agent(self, answers: list[str], *, tty: bool = True) -> tuple[int, object, str]:
        with patch("critique_bot.project_home.rebuild_index"), patch(
            "critique_bot.welcome.stdio_is_tty", return_value=tty
        ), patch("critique_bot.welcome.pick_theme", return_value=("dark", True)), patch(
            "critique_bot.welcome.pick_role", return_value="generic"
        ), patch("critique_bot.welcome.ask_login", return_value=True), patch(
            "critique_bot.webchat.browser.needs_visible_login", return_value=False
        ), patch("builtins.input", side_effect=answers), patch(
            "critique_bot.webchat.setup_ui.run_setup", return_value=0
        ) as setup, patch("critique_bot.agent.run_agent", return_value=0) as run:
            code, out, err = _capture(crit_main, ["--repo-dir", str(self.project), "fix the build"])
        return code, (run, setup), out + err

    def test_web_ui_saves_then_goes_on_with_the_command(self) -> None:
        answers = ["2", "https://chat.corp.example/", "", "", "", "", "", "", "", "", "Acme Corp"]
        code, (run, setup), text = self._agent(answers)
        self.assertEqual(code, 0, text)
        self.assertIn("To check the page elements against the real page: crit setup", text)
        self.assertNotIn("chat", text.replace("chat.corp.example", "").lower())
        setup.assert_not_called()  # crit setup is never started by itself
        run.assert_called_once()

    def test_local_model(self) -> None:
        from critique_bot.local_model import Probe

        with patch("critique_bot.local_model.probe_server", return_value=Probe("ok", "reachable, 1 model: qwen", ["qwen"])):
            code, (run, _), text = self._agent(["1", "127.0.0.1:8080", "", ""])
        self.assertEqual(code, 0, text)
        self.assertTrue(load_config(self.user).is_local)
        run.assert_called_once()

    def test_without_a_terminal_it_says_which_command_to_run(self) -> None:
        with patch("critique_bot.welcome.stdio_is_tty", return_value=False):
            code, out, err = _capture(crit_main, ["ask", "--prompt", "hi", "--repo-dir", str(self.project)])
        self.assertEqual(code, 1)
        self.assertIn("crit config --url https://ai.example.com/ --selector prompt_input=", err)
        self.assertIn("crit config --backend local --local-url http://IP:PORT", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(self.user.exists())

    def test_in_ci_it_does_not_ask(self) -> None:
        with patch.dict(os.environ, {"CI": "true"}), patch("critique_bot.welcome.stdio_is_tty", return_value=True), patch(
            "builtins.input", side_effect=AssertionError("asked")
        ):
            code, _, err = _capture(bot_main, ["chat", "--repo-dir", str(self.project)])
        self.assertEqual(code, 1)
        self.assertIn("crit config", err)

    def test_setup_without_config_flag_uses_the_user_config(self) -> None:
        self.assertEqual(self.config("--url", "https://chat.corp.example/")[0], 0)
        with patch("critique_bot.webchat.setup_ui.run_setup", return_value=0) as run:
            code, _, _ = _capture(bot_main, ["setup", "--no-open"])
        self.assertEqual(code, 0)
        self.assertEqual(Path(run.call_args.args[0]), self.user)

    def test_worker_without_any_config_explains(self) -> None:
        code, _, err = _capture(bot_main, ["queue-status"])
        self.assertEqual(code, 1)
        self.assertIn("crit config", err)


class BlockedHostTests(_Isolated):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.config("--url", "https://chat.corp.example/")[0], 0)
        browser.drain_blocked_hosts()
        # The Allow question is asked only when the page itself is restricted (page_network strict).
        restricted = patch.object(config_prompts.egress, "page_restricted", return_value=True)
        restricted.start()
        self.addCleanup(restricted.stop)

    def test_the_page_filter_records_a_blocked_host_once(self) -> None:
        class Route:
            def __init__(self, url: str) -> None:
                self.request = type("R", (), {"url": url, "resource_type": "script", "method": "GET"})()
                self.aborted = False

            def abort(self, *_):
                self.aborted = True

            def continue_(self):
                raise AssertionError("allowed")

        load_config(self.user)
        err = io.StringIO()
        with redirect_stderr(err):
            for _ in range(2):
                route = Route("https://cdn-once.blocked.example/a.js")
                browser._filter_chat_route(route, "https://chat.corp.example/")
                self.assertTrue(route.aborted)
        self.assertNotIn("Blocked:", err.getvalue())  # the log file only, once
        logged = __import__("critique_bot.log", fromlist=["x"]).log_file_path().read_text(encoding="utf-8")
        self.assertEqual(logged.count("Blocked: the page tried to contact cdn-once.blocked.example"), 1)
        self.assertIn("crit config --allow-host cdn-once.blocked.example", logged)
        self.assertEqual(browser.drain_blocked_hosts(), ["cdn-once.blocked.example"])
        self.assertEqual(browser.drain_blocked_hosts(), [])

    def test_yes_adds_the_host_and_is_asked_once(self) -> None:
        asked: list[str] = []
        config_prompts.enable(self.user, confirm=lambda q: asked.append(q) or True, drain=browser.drain_blocked_hosts)
        browser._note_blocked_host("cdn.corp-static.example")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(config_prompts.offer_blocked_hosts(), ["cdn.corp-static.example"])
            browser._note_blocked_host("cdn.corp-static.example")
            self.assertEqual(config_prompts.offer_blocked_hosts(), [])
        self.assertEqual(len(asked), 1)
        self.assertIn("The page tried to reach cdn.corp-static.example. Allow it from now on?", asked[0])
        self.assertIn("next time crit starts", err.getvalue())
        self.assertIn("cdn.corp-static.example", load_config(self.user).allowed_hosts)

    def test_no_keeps_it_blocked(self) -> None:
        config_prompts.enable(self.user, confirm=lambda q: False, drain=browser.drain_blocked_hosts)
        browser._note_blocked_host("tracker.example")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(config_prompts.offer_blocked_hosts(), [])
        self.assertNotIn("allowed_hosts", self.data())

    def test_page_network_open_never_asks(self) -> None:
        with patch.object(config_prompts.egress, "page_restricted", return_value=False):
            config_prompts.enable(self.user, confirm=lambda q: self.fail("asked"), drain=browser.drain_blocked_hosts)
            browser._note_blocked_host("tracker.example")
            self.assertEqual(config_prompts.offer_blocked_hosts(), [])

    def test_not_enabled_never_asks(self) -> None:
        browser._note_blocked_host("tracker.example")
        self.assertEqual(config_prompts.offer_blocked_hosts(), [])

    def test_default_question_is_no(self) -> None:
        config_prompts.enable(self.user, drain=browser.drain_blocked_hosts)
        browser._note_blocked_host("tracker.example")
        with patch("builtins.input", return_value="") as ask, redirect_stderr(io.StringIO()):
            self.assertEqual(config_prompts.offer_blocked_hosts(), [])
        self.assertIn("[y/N]", ask.call_args.args[0])


class GitLabPromptTests(_Isolated):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.config("--url", "https://chat.corp.example/")[0], 0)

    def test_asked_once_https_only_and_saved(self) -> None:
        answers = iter(["http://gitlab.corp.example", "https://gitlab.corp.example/"])
        config_prompts.enable(self.user, ask_text=lambda _: next(answers))
        with redirect_stderr(io.StringIO()) as err:
            url = config_prompts.ensure_gitlab_url("")
        self.assertEqual(url, "https://gitlab.corp.example")
        self.assertIn("https://", err.getvalue())
        self.assertEqual(self.data()["gitlab"]["base_url"], "https://gitlab.corp.example")
        self.assertEqual(config_prompts.ensure_gitlab_url(url), url)

    def test_not_asked_without_a_terminal(self) -> None:
        self.assertEqual(config_prompts.ensure_gitlab_url(""), "")

    def test_gitlab_post_asks_for_the_address(self) -> None:
        review = self.root / "review.md"
        review.write_text("ok", encoding="utf-8")
        with patch("critique_bot.welcome.stdio_is_tty", return_value=True), patch(
            "builtins.input", return_value="https://gitlab.corp.example"
        ), patch("critique_bot.gitlab_post.post_review", return_value=0) as post:
            code, _, _ = _capture(
                bot_main,
                ["gitlab-post", "--no-logs", "--review-file", str(review), "--config", str(self.user),
                 "--mr-url", "https://gitlab.corp.example/g/p/-/merge_requests/3"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(post.call_args.kwargs["gitlab"].base_url, "https://gitlab.corp.example")
        self.assertEqual(self.data()["gitlab"]["base_url"], "https://gitlab.corp.example")


if __name__ == "__main__":
    unittest.main()
