"""The config file: a shared file with url, selectors, and the rest, layered under the user's own.

Every test runs with its own user folder, ProgramData, and system location (all temporary),
so nothing here reads or writes the person's ~/.config, /etc, or %APPDATA%.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PosixPath
from unittest.mock import patch

from critique_bot import config, config_cmd, egress, installer, redaction, roles, setup_wizard
from critique_bot.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


def _real_system_path() -> Path:
    """config.system_company_config_path itself (test_00_isolation points it at a temp file)."""
    return getattr(config, "_real_system_company_config_path", config.system_company_config_path)()

_TEAM = {
    "url": "https://ai.corp.example/",
    "selectors": {"prompt_input": "#box", "assistant_messages": ".reply"},
}


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.xdg = self.root / "xdg"
        self.project = self.root / "project"
        self.project.mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("CRITIQUE_", "CRIT_", "CI"))}
        clean.update(
            {
                "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.xdg),
                "APPDATA": str(self.xdg),
                "ProgramData": str(self.root / "ProgramData"),
            }
        )
        self._env = patch.dict(os.environ, clean, clear=True)
        self._env.start()
        self.system = self.root / "etc" / "critique-bot" / "crit.json"
        self._system = patch.object(config, "system_company_config_path", lambda: self.system)
        self._system.start()
        self._policy = egress.policy()
        self.user = config.user_config_path()
        self._cwd = os.getcwd()
        os.chdir(self.project)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        self._system.stop()
        self._env.stop()
        egress._policy = self._policy
        redaction.set_custom_terms(())
        redaction.set_pii_redaction(True)
        self._tmp.cleanup()

    def team_file(self, data: dict | None = None, name: str = "crit.json") -> Path:
        path = self.root / "share" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_TEAM if data is None else data), encoding="utf-8")
        return path

    def user_config(self, data: dict) -> Path:
        self.user.parent.mkdir(parents=True, exist_ok=True)
        self.user.write_text(json.dumps(data), encoding="utf-8")
        return self.user

    def load(self, user: dict, team: dict | None = None):
        path = self.team_file(team)
        return load_config(self.user_config({"config_file": str(path), **user}))


class DiscoveryTests(_Isolated):
    def test_order_settings_then_env_then_system(self) -> None:
        a, b = self.team_file(name="a.json"), self.team_file(name="b.json")
        self.system.parent.mkdir(parents=True)
        self.system.write_text(json.dumps(_TEAM), encoding="utf-8")
        self.assertEqual(config.company_config_location({}), (str(self.system), "system"))
        with patch.dict(os.environ, {"CRITIQUE_CONFIG_FILE": str(b)}):
            self.assertEqual(config.company_config_location({}), (str(b), "env"))
            self.user_config({"config_file": str(a)})
            self.assertEqual(config.company_config_location({}), (str(a), "settings"))
            # The loaded file's own key comes first.
            self.assertEqual(config.company_config_location({"config_file": str(b)}), (str(b), "settings"))

    def test_none_when_nothing_is_there(self) -> None:
        self.assertIsNone(config.company_config_location({}))
        self.assertIsNone(config.find_company_settings({}))

    @unittest.skipIf(os.name == "nt", "simulates Windows on POSIX")
    def test_windows_system_location_is_programdata(self) -> None:
        self._system.stop()
        try:
            # os.name is global: keep pathlib on this system's paths while crit thinks it is Windows.
            with patch.object(config.os, "name", "nt"), patch.object(config, "Path", PosixPath):
                path = _real_system_path()
            self.assertEqual(path, Path(os.environ["ProgramData"]) / "critique-bot" / "crit.json")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(_TEAM), encoding="utf-8")
            with patch.object(config.os, "name", "nt"), patch.object(config, "Path", PosixPath), patch.object(
                config, "system_company_config_path", _real_system_path
            ):
                self.assertEqual(config.company_config_location({}), (str(path), "system"))
        finally:
            self._system.start()

    @unittest.skipIf(os.name == "nt", "the Linux location; Windows uses %ProgramData% (tested separately)")
    def test_linux_system_location(self) -> None:
        self._system.stop()
        try:
            self.assertEqual(_real_system_path(), Path("/etc/critique-bot/crit.json"))
        finally:
            self._system.start()

    def test_pasted_paths(self) -> None:
        if os.name != "nt":  # on Windows a rooted path gets the current drive, as it should
            self.assertEqual(config.company_path_value('"/srv/team/crit.json"'), "/srv/team/crit.json")
            self.assertEqual(config.company_path_value("'/srv/team/crit.json'"), "/srv/team/crit.json")
        self.assertEqual(config.company_path_value(r'"\\server\share\crit.json"'), r"\\server\share\crit.json")
        self.assertEqual(config.company_path_value("//server/share/crit.json"), "//server/share/crit.json")
        with patch.object(config.os, "name", "nt"):
            self.assertEqual(config.company_path_value(r'"C:\Team\crit.json"'), r"C:\Team\crit.json")
            self.assertEqual(config.company_path_value(r"\\server\share\crit.json"), r"\\server\share\crit.json")
        self.assertTrue(os.path.isabs(config.company_path_value("crit.json")))


class LayeringTests(_Isolated):
    def test_no_config_file_keeps_the_old_behavior(self) -> None:
        cfg = load_config(self.user_config({**_TEAM, "model": "m"}))
        self.assertEqual(cfg.url, "https://ai.corp.example/")
        self.assertEqual(cfg.company_config, "")
        self.assertFalse(config.company_cache_path().exists())

    def test_locked_keys_ignore_the_users_values(self) -> None:
        team = {
            **_TEAM,
            "backend": "browser",
            "allowed_hosts": ["login.corp.example"],
            "redact_pii": True,
            "cdp_url": "",
            "gitlab": {"base_url": "https://gitlab.corp.example"},
        }
        cfg = self.load(
            {
                "url": "https://elsewhere.example/",
                "backend": "local",
                "allowed_hosts": ["evil.example"],
                "redact_pii": False,
                "cdp_url": "http://127.0.0.1:9222",
                "selectors": {"prompt_input": "#mine", "send_button": "#send"},
                "gitlab": {"base_url": "https://gitlab.elsewhere.example"},
                "gitlab_base_url": "https://gitlab.elsewhere.example",
            },
            team,
        )
        self.assertEqual(cfg.url, "https://ai.corp.example/")
        self.assertEqual(cfg.backend, "browser")
        self.assertEqual(cfg.allowed_hosts, ("login.corp.example",))
        self.assertTrue(cfg.redact_pii)
        self.assertIsNone(cfg.cdp_url)
        self.assertEqual(cfg.selectors.prompt_input, "#box")  # locked key by key
        self.assertEqual(cfg.selectors.send_button, "#send")  # the file did not set it
        self.assertEqual(cfg.gitlab.base_url, "https://gitlab.corp.example")

    def test_local_server_settings_are_locked(self) -> None:
        team = {"backend": "local", "url": "http://127.0.0.1:8080", "local": {"num_ctx": 16384}}
        cfg = self.load({"local": {"num_ctx": 4096, "temperature": 0.9}}, team)
        self.assertEqual(cfg.local.num_ctx, 16384)
        self.assertIsNone(cfg.local.temperature)

    def test_env_does_not_change_locked_keys(self) -> None:
        with patch.dict(os.environ, {"CRITIQUE_CHAT_URL": "https://other.example/", "CRITIQUE_EGRESS": "open"}):
            cfg = self.load({}, {**_TEAM, "egress": "strict"})
        self.assertEqual(cfg.url, "https://ai.corp.example/")
        self.assertEqual(cfg.egress, "strict")

    def test_cdp_override_ignored_when_the_file_sets_it(self) -> None:
        cfg = self.load({}, {**_TEAM, "egress": "open", "cdp_url": "http://127.0.0.1:9333"})
        self.assertEqual(cfg.cdp_url, "http://127.0.0.1:9333")
        cfg = load_config(self.user, cdp_url_override="http://127.0.0.1:9444")
        self.assertEqual(cfg.cdp_url, "http://127.0.0.1:9333")

    def test_redact_terms_are_both_lists(self) -> None:
        cfg = self.load({"redact_terms": ["Globex", "acme corp"]}, {**_TEAM, "redact_terms": ["Acme Corp"]})
        self.assertEqual(cfg.redact_terms, ("Acme Corp", "Globex"))

    def test_egress_may_be_tightened_never_loosened(self) -> None:
        self.assertEqual(self.load({"egress": "open"}, {**_TEAM, "egress": "strict"}).egress, "strict")
        self.assertEqual(self.load({"egress": "strict"}, {**_TEAM, "egress": "open"}).egress, "strict")
        self.assertEqual(self.load({}, {**_TEAM, "egress": "open"}).egress, "open")

    def test_page_network_lock(self) -> None:
        cfg = self.load({"page_network": "open"}, {**_TEAM, "page_network": "strict"})
        self.assertEqual(cfg.page_network, "strict")
        self.assertTrue(cfg.filter_chat_page)
        self.assertTrue(egress.page_restricted())
        cfg = self.load({"page_network": "strict"}, {**_TEAM, "page_network": "open"})
        self.assertEqual(cfg.page_network, "strict")
        cfg = self.load({}, _TEAM)
        self.assertEqual(cfg.page_network, "open")
        self.assertFalse(cfg.filter_chat_page)

    def test_other_keys_default_from_the_file_user_wins(self) -> None:
        team = {**_TEAM, "model": "team-model", "timeout_ms": 1000, "max_files": 5}
        cfg = self.load({"model": "mine"}, team)
        self.assertEqual(cfg.model, "mine")
        self.assertEqual(cfg.timeout_ms, 1000)
        self.assertEqual(cfg.max_files, 5)

    def test_unknown_keys_are_only_noted(self) -> None:
        cfg = self.load({}, {**_TEAM, "future_setting": 1})
        self.assertEqual(cfg.url, "https://ai.corp.example/")


class RequiredKeysTests(_Isolated):
    def test_missing_keys_are_named(self) -> None:
        path = self.team_file({"selectors": {"assistant_messages": ".r"}})
        self.user_config({"config_file": str(path)})
        with self.assertRaises(ConfigError) as caught:
            load_config(self.user)
        self.assertEqual(str(caught.exception), f"{path} is missing: url, selectors.prompt_input")

    def test_local_backend_needs_only_the_url(self) -> None:
        path = self.team_file({"backend": "local"})
        with self.assertRaises(ConfigError) as caught:
            load_config(self.user_config({"config_file": str(path)}))
        self.assertIn("is missing: url", str(caught.exception))

    def test_bad_values(self) -> None:
        for bad in ([1, 2], {**_TEAM, "egress": "loose"}, {**_TEAM, "page_network": "x"}, {**_TEAM, "selectors": "x"}):
            path = self.team_file(bad)  # type: ignore[arg-type]
            with self.assertRaises(ConfigError, msg=str(bad)) as caught:
                load_config(self.user_config({"config_file": str(path)}))
            self.assertIn(str(path), str(caught.exception))

    def test_invalid_json(self) -> None:
        path = self.root / "broken.json"
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            load_config(self.user_config({"config_file": str(path)}))
        self.assertIn("is not valid JSON", str(caught.exception))


class CacheTests(_Isolated):
    def test_saved_copy_is_used_with_a_notice(self) -> None:
        path = self.team_file()
        self.user_config({"config_file": str(path)})
        load_config(self.user)
        cache = config.company_cache_path()
        self.assertEqual(cache.parent, self.user.parent)
        record = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(record["source"], str(path))
        self.assertIn("saved_at", record)
        if os.name != "nt":
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        path.unlink()  # the share is offline
        err = io.StringIO()
        with redirect_stderr(err):
            cfg = load_config(self.user)
        self.assertEqual(cfg.url, "https://ai.corp.example/")
        self.assertIn(f"{path} cannot be read; using its copy from {time.strftime('%Y-%m-%d')}", err.getvalue())

    def test_no_file_and_no_copy(self) -> None:
        path = self.root / "share" / "gone.json"
        with self.assertRaises(ConfigError) as caught:
            load_config(self.user_config({"config_file": str(path)}))
        self.assertIn("there is no saved copy", str(caught.exception))

    def test_a_copy_of_another_file_is_not_used(self) -> None:
        load_config(self.user_config({"config_file": str(self.team_file())}))
        other = self.root / "share" / "other.json"
        with self.assertRaises(ConfigError):
            load_config(self.user_config({"config_file": str(other)}))


class _Script:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []
        self.said: list[str] = []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected question: {prompt}")
        return self.answers.pop(0)

    def say(self, text: str) -> None:
        self.said.append(text)


class FirstRunTests(_Isolated):
    def test_system_file_is_used_without_questions(self) -> None:
        self.system.parent.mkdir(parents=True)
        self.system.write_text(json.dumps(_TEAM), encoding="utf-8")
        script = _Script()
        result = setup_wizard.run_wizard(self.user, ask=script.ask, say=script.say)
        assert result is not None
        self.assertEqual(script.asked, [])
        self.assertEqual(script.said, [f"Using {self.system}"])
        self.assertFalse(result.needs_selectors)
        self.assertEqual(load_config(self.user).url, "https://ai.corp.example/")

    def test_env_file_is_used_without_questions(self) -> None:
        path = self.team_file()
        with patch.dict(os.environ, {"CRITIQUE_CONFIG_FILE": str(path)}):
            script = _Script()
            self.assertIsNotNone(setup_wizard.run_wizard(self.user, ask=script.ask, say=script.say))
        self.assertEqual(script.said, [f"Using {path}"])

    def test_asks_for_the_path_bad_then_good(self) -> None:
        good = self.team_file()
        bad = self.team_file({"url": "https://ai.corp.example/"}, name="bad.json")
        script = _Script(str(self.root / "nope.json"), str(bad), f'"{good}"')
        result = setup_wizard.run_wizard(self.user, ask=script.ask, say=script.say)
        assert result is not None
        self.assertEqual(script.asked, [setup_wizard.CONFIG_FILE_PROMPT] * 3)
        self.assertEqual(setup_wizard.CONFIG_FILE_PROMPT, "Config file path (Enter if none): ")
        self.assertIn(f"  {self.root / 'nope.json'} does not exist.", script.said)
        self.assertIn(f"  {bad} is missing: selectors.prompt_input, selectors.assistant_messages", script.said)
        self.assertEqual(script.said[-1], f"Using {good}")
        self.assertEqual(json.loads(self.user.read_text(encoding="utf-8")), {"config_file": str(good)})

    def test_enter_goes_on_to_the_questions(self) -> None:
        script = _Script("", "")  # no file; then Enter at "Where does the model run?"
        self.assertIsNone(setup_wizard.run_wizard(self.user, ask=script.ask, say=script.say))
        self.assertIn(setup_wizard.DIY_PREFACE, script.said)
        self.assertTrue(script.asked[1].startswith("Choose 1 or 2"))

    def test_command_line_first_run_continues_without_a_terminal(self) -> None:
        from critique_bot.cli import _find_config

        self.system.parent.mkdir(parents=True)
        self.system.write_text(json.dumps(_TEAM), encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err), patch("critique_bot.cli._may_ask", return_value=False):
            found = _find_config(None, self.project)
        self.assertEqual(found, self.user)
        self.assertIn(f"Using {self.system}", err.getvalue())

    def test_preferences_only_user_config_still_asks(self) -> None:
        self.user_config({"theme": "light"})
        location = config_cmd.resolve_config_path(None, self.project)
        self.assertEqual(location.source, "user")
        self.assertFalse(location.exists)
        good = self.team_file()
        script = _Script(str(good))
        setup_wizard.run_wizard(self.user, ask=script.ask, say=script.say)
        data = json.loads(self.user.read_text(encoding="utf-8"))
        self.assertEqual(data, {"config_file": str(good), "theme": "light"})  # the preference is kept


def _run_config(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = config_cmd.main(argv)
    return code, out.getvalue(), err.getvalue()


class ConfigCommandTests(_Isolated):
    def test_config_file_flag_sets_and_removes(self) -> None:
        good = self.team_file()
        code, out, err = _run_config(["--config-file", str(good)])
        self.assertEqual(code, 0, err)
        self.assertIn(f"Using {good}", out)
        self.assertEqual(json.loads(self.user.read_text(encoding="utf-8"))["config_file"], str(good))
        code, out, _ = _run_config([])
        self.assertIn(f"Config file: {good}", out)
        self.assertIn("url: https://ai.corp.example/ (config file, locked)", out)
        code, out, err = _run_config(["--config-file", "none"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("config_file", json.loads(self.user.read_text(encoding="utf-8")))

    def test_bad_file_is_not_saved(self) -> None:
        code, _, err = _run_config(["--config-file", str(self.root / "missing.json")])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)
        self.assertFalse(self.user.exists())

    def test_locked_keys_are_refused(self) -> None:
        good = self.team_file({**_TEAM, "allowed_hosts": ["x.corp.example"], "egress": "strict", "redact_pii": True, "redact_terms": ["Acme Corp"]})
        self.user_config({"config_file": str(good)})
        message = f"This is set in the config file {good}; change it there."
        for argv in (
            ["--url", "https://other.example/"],
            ["--allow-host", "evil.example"],
            ["--egress", "open"],
            ["--pii", "off"],
            ["--selector", "prompt_input=#other"],
            ["--unredact", "Acme Corp"],
        ):
            code, _, err = _run_config(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(message, err, argv)
        self.assertEqual(json.loads(self.user.read_text(encoding="utf-8")), {"config_file": str(good)})
        # Not locked: a selector the file leaves out, a model, more names to hide.
        code, _, err = _run_config(["--selector", "send_button=#send", "--model", "m", "--redact", "Globex"])
        self.assertEqual(code, 0, err)

    def test_page_network_flag_and_summary(self) -> None:
        code, _, err = _run_config(["--url", "https://ai.corp.example/", "--page-network", "strict"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(self.user.read_text(encoding="utf-8"))["page_network"], "strict")
        _run_config(["--page-network", "open"])
        _, out, _ = _run_config([])
        self.assertIn("Page network: open (the page loads its own sites); crit's own traffic: strict", out)

    def test_theme_flag_is_per_user(self) -> None:
        code, _, err = _run_config(["--theme", "light"])
        self.assertEqual(code, 0, err)
        self.assertEqual(config.user_preferences()["theme"], "light")
        code, _, err = _run_config(["--theme", "nope"])
        self.assertEqual(code, 2)


class InstallerTests(_Isolated):
    def test_scripts_pass_the_config_file_env(self) -> None:
        sh = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        ps1 = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn('"$exe" install --config-file "$CRIT_CONFIG_FILE"', sh)
        self.assertIn("install --config-file $env:CRIT_CONFIG_FILE", ps1)

    def test_install_flag_sets_the_config_file(self) -> None:
        good = self.team_file()
        with patch.object(installer, "install", return_value=0):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(installer.main(["--config-file", str(good)]), 0)
        self.assertEqual(json.loads(self.user.read_text(encoding="utf-8"))["config_file"], str(good))

    def test_example_is_a_valid_config_file(self) -> None:
        data = json.loads((ROOT / "config-file.example.json").read_text(encoding="utf-8"))
        config.validate_company_data(data, "config-file.example.json")
        # Shipped with the .deb (/usr/share/doc/critique-bot/examples); the zip holds only the program.
        self.assertIn("config-file.example.json", (ROOT / "scripts" / "build_deb.py").read_text(encoding="utf-8"))

    def test_deb_makes_the_system_folder_readable(self) -> None:
        self.assertIn("-m 0755 /etc/critique-bot", (ROOT / "scripts" / "build_deb.py").read_text(encoding="utf-8"))


class AutoInstallTests(_Isolated):
    def setUp(self) -> None:
        super().setUp()
        os.environ["LOCALAPPDATA"] = str(self.root / "local")
        os.environ["XDG_DATA_HOME"] = str(self.root / "data")
        self.bundle = self.root / "download" / "critique-bot"
        self.bundle.mkdir(parents=True)
        (self.bundle / installer._exe_name()).write_text("", encoding="utf-8")

    def _installed(self, version: str) -> None:
        app = installer.install_root() / f"app-{version}"
        app.mkdir(parents=True)
        (app / installer._exe_name()).write_text("", encoding="utf-8")

    def test_installs_once(self) -> None:
        self.assertTrue(installer.should_auto_install(["fix it"], self.bundle))
        said: list[str] = []
        with patch.object(installer, "frozen_bundle_dir", return_value=self.bundle), patch.object(
            installer, "_write_launchers"
        ), patch.object(installer, "_posix_add_to_path", return_value=[]), patch.object(
            installer, "_windows_add_to_path", return_value=False
        ):
            self.assertTrue(installer.auto_install(say=said.append))
        self.assertEqual(said, [
            f"Installed crit {installer.__version__} in {installer.install_root() / f'app-{installer.__version__}'} and added it to your PATH."
        ])
        self.assertFalse(installer.should_auto_install(["fix it"], self.bundle))

    def test_installed_copy_does_not_reinstall(self) -> None:
        self._installed(installer.__version__)
        app = installer.install_root() / f"app-{installer.__version__}"
        self.assertFalse(installer.should_auto_install([], app))
        self.assertFalse(installer.should_auto_install([], self.bundle))  # the same version is installed

    def test_older_version_is_upgraded(self) -> None:
        self._installed("0.0.1")
        self.assertTrue(installer.should_auto_install([], self.bundle))
        self._installed("999.0.0")
        self.assertFalse(installer.should_auto_install([], self.bundle))

    def test_opt_out_and_skipped_commands(self) -> None:
        with patch.dict(os.environ, {"CRIT_NO_AUTO_INSTALL": "1"}):
            self.assertFalse(installer.should_auto_install([], self.bundle))
        for argv in (["install"], ["--help"], ["-h"]):
            self.assertFalse(installer.should_auto_install(argv, self.bundle))
        with patch.object(installer, "frozen_bundle_dir", return_value=None):
            self.assertFalse(installer.should_auto_install([]))  # running from Python sources

    @unittest.skipIf(os.name == "nt", "the .deb is Linux only")
    def test_deb_install_counts_as_installed(self) -> None:
        self.assertTrue(installer.is_installed_copy(Path("/opt/critique-bot")))
        self.assertFalse(installer.should_auto_install([], Path("/opt/critique-bot")))

    def test_main_installs_before_the_command(self) -> None:
        from critique_bot import __main__ as entry

        with patch.object(installer, "should_auto_install", return_value=True), patch.object(
            installer, "auto_install", return_value=True
        ) as auto, patch.object(entry, "launched_from_explorer", return_value=False), patch.object(
            entry, "_run", return_value=0
        ), patch.object(entry.sys, "argv", ["critique-bot", "--help"]):
            self.assertEqual(entry.main(), 0)
        auto.assert_called_once()


class DoubleClickTests(_Isolated):
    def test_double_click_installs_asks_for_the_config_file_and_waits(self) -> None:
        from critique_bot import __main__ as entry

        good = self.team_file()
        answers = iter([str(good), ""])  # the config file question, then Enter to close
        asked: list[str] = []

        def fake_input(prompt: str = "") -> str:
            asked.append(prompt)
            return next(answers)

        out, err = io.StringIO(), io.StringIO()
        with patch.object(entry, "launched_from_explorer", return_value=True), patch.object(
            entry.sys, "argv", ["critique-bot.exe"]
        ), patch.object(installer, "should_auto_install", return_value=True), patch.object(
            installer, "auto_install", return_value=True
        ) as auto, patch("builtins.input", side_effect=fake_input), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(entry.main(), 0)
        auto.assert_called_once()
        self.assertEqual(asked[0], setup_wizard.CONFIG_FILE_PROMPT)
        self.assertNotIn("[Y/n]", "".join(asked))
        self.assertIn(f"Using {good}", out.getvalue())
        self.assertIn("crit runs in a terminal", out.getvalue())
        self.assertEqual(json.loads(self.user.read_text(encoding="utf-8")), {"config_file": str(good)})


class RoleDetectionTests(unittest.TestCase):
    def _role(self, files: dict[str, str]) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in files.items():
                path = Path(tmp) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            return roles.detect_role(tmp)

    def test_roles(self) -> None:
        self.assertEqual(self._role({}), "generic")
        self.assertEqual(self._role({"main.c": "int main(){}"}), "generic")
        self.assertEqual(self._role({"build/soong/README": ""}), "aosp-expert")
        self.assertEqual(self._role({".repo/manifest.xml": ""}), "aosp-expert")
        self.assertEqual(self._role({"app/src/main/AndroidManifest.xml": "<manifest/>"}), "android-app-engineer")
        self.assertEqual(self._role({"build.gradle.kts": 'plugins { id("com.android.application") }'}), "android-app-engineer")
        self.assertEqual(self._role({"feature/src/main/AndroidManifest.xml": ""}), "android-app-engineer")
        self.assertEqual(self._role({"requirements.txt": "anthropic>=0.30\nrequests\n"}), "ai-expert")
        self.assertEqual(self._role({"pyproject.toml": 'dependencies = ["torch", "numpy"]'}), "ai-expert")
        self.assertEqual(self._role({"requirements.txt": "requests\nopenapi-spec\n"}), "generic")


if __name__ == "__main__":
    unittest.main()
