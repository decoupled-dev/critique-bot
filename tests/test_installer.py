"""crit install / crit uninstall: the program in the user's folders, launchers, PATH, and removing it all."""

from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from critique_bot import __version__, installer


@unittest.skipIf(os.name == "nt", "the POSIX layout")
class PosixInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.bundle = root / "download" / f"crit-{__version__}-linux-x64"
        (self.bundle / "_internal").mkdir(parents=True)
        exe = self.bundle / "crit"
        exe.write_text("#!/bin/sh\necho hi\n")
        exe.chmod(0o644)
        (self.home / ".bashrc").write_text("# mine\n")
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "XDG_DATA_HOME": "", "XDG_CONFIG_HOME": "", "CRIT_HOME": "", "PATH": "/usr/bin:/bin"})
        self.env.start()
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()

    def tearDown(self) -> None:
        self.home_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_install_layout_path_and_rerun(self) -> None:
        out: list[str] = []
        self.assertEqual(installer.install(self.bundle, say=out.append), 0)
        crit_home = self.home / ".crit"
        app = crit_home / "app" / __version__
        self.assertTrue((app / "crit").stat().st_mode & stat.S_IXUSR)
        self.assertEqual(crit_home.stat().st_mode & 0o777, 0o700)
        crit = crit_home / "bin" / "crit"
        self.assertNotIn("--as-crit", crit.read_text())
        self.assertIn(str(app / "crit"), crit.read_text())
        self.assertEqual(sorted(p.name for p in (crit_home / "bin").iterdir()), ["crit"])  # no critique-bot launcher
        self.assertTrue(crit.stat().st_mode & stat.S_IXUSR)
        profile = (self.home / ".profile").read_text()
        self.assertIn(f'export PATH="{crit_home / "bin"}:$PATH"', profile)
        self.assertIn("# mine", (self.home / ".bashrc").read_text())
        self.assertIn(".crit/bin", (self.home / ".bashrc").read_text())
        self.assertFalse((self.home / ".local").exists())
        self.assertTrue(installer.is_installed_copy(app))
        self.assertFalse(installer.is_installed_copy(self.bundle))
        # Installing again (an update) does not add PATH twice.
        self.assertEqual(installer.install(self.bundle, say=out.append), 0)
        self.assertEqual((self.home / ".profile").read_text().count(".crit/bin"), 1)

    def test_old_versions_are_removed(self) -> None:
        old = self.home / ".crit" / "app" / "0.0.1"
        old.mkdir(parents=True)
        installer.install(self.bundle, say=lambda _: None)
        self.assertFalse(old.exists())
        self.assertEqual(installer.installed_versions(), [__version__])

    def test_the_old_install_is_cleaned_up(self) -> None:
        old_root = self.home / ".local" / "share" / "critique-bot"
        (old_root / "app-1.0.3").mkdir(parents=True)
        old_bin = self.home / ".local" / "bin"
        old_bin.mkdir(parents=True)
        (old_bin / "crit").write_text(f'#!/bin/sh\nexec "{old_root}/app-1.0.3/critique-bot" --as-crit "$@"\n')
        (old_bin / "critique-bot").write_text("#!/bin/sh\nexec /somewhere/else/critique-bot\n")  # not ours
        (self.home / ".profile").write_text(f'export PATH="{old_bin}:$PATH"  # added by critique-bot install\n')
        self.assertEqual(installer.install(self.bundle, say=lambda _: None), 0)
        self.assertFalse(old_root.exists())
        self.assertFalse((old_bin / "crit").exists())
        self.assertTrue((old_bin / "critique-bot").exists())
        self.assertIn(str(old_bin), (self.home / ".profile").read_text())  # left alone

    def test_the_old_install_stays_while_it_is_running(self) -> None:
        old_root = self.home / ".local" / "share" / "critique-bot"
        running = old_root / "app-1.0.3"
        running.mkdir(parents=True)
        installer.remove_legacy_install(running=running)
        self.assertTrue(running.exists())

    def test_from_source_it_explains(self) -> None:
        out: list[str] = []
        with patch.object(installer, "frozen_bundle_dir", return_value=None):
            self.assertEqual(installer.install(say=out.append), 1)
        self.assertIn("downloaded program", out[0])

    def _user_files(self) -> dict[str, Path]:
        crit = self.home / ".crit"
        files = {
            "config": crit / "config.json",
            "profile": crit / "browser-profile" / "Default" / "Cookies",
            "edge": crit / "msedge-user-data" / "Local State",
            "cache": crit / "cache" / "config-file.cache.json",
            "log": crit / "logs" / "crit.log",
            "old_config": self.home / ".config" / "critique-bot" / "config.json",
            "old_app": self.home / ".local" / "share" / "critique-bot" / "app-1.0.3" / "critique-bot",
            "project": self.home / "src" / "app" / ".crit" / "settings.json",
            "old_project": self.home / "src" / "other" / ".bot" / "settings.json",
        }
        for path in files.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        old_bin = self.home / ".local" / "bin"
        old_bin.mkdir(parents=True, exist_ok=True)
        (old_bin / "crit").write_text('#!/bin/sh\nexec "/x/critique-bot" --as-crit "$@"\n')
        (old_bin / "critique-bot").write_text("#!/bin/sh\nexec /somewhere/else/critique-bot\n")  # not ours
        (old_bin / "tool").write_text("#!/bin/sh\n")
        (self.home / ".profile").write_text(
            "# mine\nexport EDITOR=vi\n"
            f'export PATH="{self.home / ".crit" / "bin"}:$PATH"  # added by crit install\n'
        )
        (self.home / ".bashrc").write_text(
            f'# mine\nexport PATH="{old_bin}:$PATH"  # added by critique-bot install\nalias ll="ls -l"\n'
        )
        return files

    def _uninstall(self, running: Path | None) -> list[str]:
        out: list[str] = []
        with patch.object(installer, "frozen_bundle_dir", return_value=running), patch(
            "builtins.input", side_effect=AssertionError("asked a question")
        ), patch.object(installer.subprocess, "Popen", side_effect=AssertionError("started a program")), patch.object(
            installer.subprocess, "run", side_effect=AssertionError("ran a program")
        ):
            self.assertEqual(installer.uninstall(say=out.append), 0)
        return out

    def test_uninstall_removes_everything_but_project_folders(self) -> None:
        installer.install(self.bundle, say=lambda _: None)
        files = self._user_files()
        app = self.home / ".crit" / "app" / __version__
        out = self._uninstall(app)
        self.assertEqual(out, ["crit is removed (settings, sign-in, and program). Project .crit folders were kept."])
        self.assertFalse((self.home / ".crit").exists())
        for name in ("old_config", "old_app"):
            self.assertFalse(files[name].exists(), name)
        self.assertFalse((self.home / ".config" / "critique-bot").exists())
        self.assertFalse((self.home / ".local" / "share" / "critique-bot").exists())
        self.assertTrue(files["project"].exists())
        self.assertTrue(files["old_project"].exists())
        old_bin = self.home / ".local" / "bin"
        self.assertFalse((old_bin / "crit").exists())
        self.assertTrue((old_bin / "critique-bot").exists())
        self.assertTrue((old_bin / "tool").exists())
        # Only the lines crit's installers marked are gone.
        self.assertEqual((self.home / ".profile").read_text(), "# mine\nexport EDITOR=vi\n")
        self.assertEqual((self.home / ".bashrc").read_text(), '# mine\nalias ll="ls -l"\n')

    def test_uninstall_keeps_the_home_folders_project_data(self) -> None:
        self._user_files()
        kept = self.home / ".crit" / "projects" / "home" / "settings.json"
        kept.parent.mkdir(parents=True)
        kept.write_text("{}")
        self._uninstall(None)
        self.assertTrue(kept.exists())
        self.assertEqual([p.name for p in (self.home / ".crit").iterdir()], ["projects"])

    def test_uninstall_from_the_deb_says_how_to_remove_the_package(self) -> None:
        self._user_files()
        out = self._uninstall(Path("/opt/crit"))
        self.assertEqual(out[-1], "Run: sudo apt purge crit")
        self.assertEqual(len(out), 2)
        self.assertFalse((self.home / ".crit").exists())
        out = self._uninstall(Path("/opt/critique-bot"))  # the 1.0.4 package
        self.assertEqual(out[-1], "Run: sudo apt purge crit")

    def test_uninstall_from_source_or_with_nothing_there(self) -> None:
        out = self._uninstall(None)
        self.assertEqual(out, ["crit is removed (settings and sign-in). Project .crit folders were kept."])

    def test_crit_uninstall_asks_nothing_and_never_installs(self) -> None:
        import sys

        from critique_bot import __main__ as entry
        from critique_bot import agent_cli

        self._user_files()
        out = io.StringIO()
        with patch.object(sys, "argv", ["crit", "uninstall"]), patch.object(
            installer, "should_auto_install", side_effect=AssertionError("auto install")
        ), patch.object(installer, "auto_install", side_effect=AssertionError("auto install")), patch(
            "builtins.input", side_effect=AssertionError("asked")
        ), patch.object(entry, "launched_from_explorer", return_value=False), patch(
            "critique_bot.cli.main", side_effect=AssertionError("setup or config")
        ), patch.object(installer, "frozen_bundle_dir", return_value=self.bundle), redirect_stdout(out):
            self.assertEqual(entry.main(), 0)
            self.assertEqual(agent_cli.main(["uninstall"]), 0)
        self.assertIn("crit is removed", out.getvalue())
        self.assertFalse((self.home / ".crit").exists())

    def test_uninstall_help(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as done:
            installer.main_uninstall(["--help"])
        self.assertEqual(done.exception.code, 0)
        self.assertIn("crit uninstall", out.getvalue())
        self.assertIn(".crit folders are kept", " ".join(out.getvalue().split()))


class WindowsLayoutTests(unittest.TestCase):
    """The Windows layout and PATH, with os.name and the registry faked."""

    def test_paths_and_old_bin_dropped_from_path(self) -> None:
        import sys
        import types

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {"USERPROFILE": str(root / "Users" / "me"), "LOCALAPPDATA": str(root / "Local"), "CRIT_HOME": ""}
            old_bin = root / "Local" / "Programs" / "critique-bot" / "bin"
            store = {"Path": (f"C:\\Windows;{old_bin};C:\\tools", 2)}

            class Key:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            fake = types.SimpleNamespace(
                HKEY_CURRENT_USER=1, KEY_READ=1, KEY_WRITE=2, REG_SZ=1, REG_EXPAND_SZ=2,
                OpenKey=lambda *a: Key(), QueryValueEx=lambda key, name: store[name],
                SetValueEx=lambda key, name, _r, kind, value: store.__setitem__(name, (value, kind)),
            )
            # Path itself must stay POSIX here, so only crit's modules see "nt".
            fake_os = types.ModuleType("os")
            fake_os.__dict__.update(os.__dict__)
            fake_os.name = "nt"
            from critique_bot import config as cfg

            with patch.object(installer, "os", fake_os), patch.object(cfg, "os", fake_os), patch.dict(
                os.environ, env
            ), patch.dict(sys.modules, {"winreg": fake}):
                self.assertEqual(installer.install_root(), root / "Users" / "me" / ".crit" / "app")
                self.assertEqual(installer.bin_dir(), root / "Users" / "me" / ".crit" / "bin")
                self.assertEqual(installer.legacy_bin_dir(), old_bin)
                old_bin.mkdir(parents=True)
                (old_bin / "crit.cmd").write_text(f'@"{old_bin.parent}\\app-1.0.3\\critique-bot.exe" --as-crit %*\r\n')
                installer.remove_legacy_install()
            self.assertEqual(store["Path"][0], "C:\\Windows;C:\\tools")
            self.assertFalse(old_bin.parent.exists())


class WindowsUninstallTests(unittest.TestCase):
    """crit uninstall on Windows, with os.name, the registry, and subprocess faked."""

    def test_registry_path_and_the_locked_exe(self) -> None:
        import sys
        import types

        from critique_bot import config as cfg

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            me = root / "Users" / "me"
            env = {
                "USERPROFILE": str(me), "LOCALAPPDATA": str(me / "AppData" / "Local"),
                "APPDATA": str(me / "AppData" / "Roaming"), "CRIT_HOME": "", "TEMP": str(root),
            }
            crit_home = me / ".crit"
            app = crit_home / "app" / __version__
            (app / "_internal").mkdir(parents=True)
            (app / "crit.exe").write_text("exe")
            (crit_home / "bin").mkdir()
            (crit_home / "bin" / "crit.cmd").write_text(f'@"{app}\\crit.exe" %*\r\n')
            (crit_home / "browser-profile").mkdir()
            (crit_home / "config.json").write_text("{}")
            old_programs = me / "AppData" / "Local" / "Programs" / "critique-bot"
            (old_programs / "bin").mkdir(parents=True)
            (me / "AppData" / "Local" / "critique-bot" / "msedge-user-data").mkdir(parents=True)
            (me / "AppData" / "Roaming" / "critique-bot").mkdir(parents=True)
            (me / "AppData" / "Roaming" / "critique-bot" / "config.json").write_text("{}")
            project = me / "src" / "app" / ".crit"
            project.mkdir(parents=True)

            store = {"Path": (f"C:\\Windows;{crit_home / 'bin'};{old_programs / 'bin'};C:\\tools", 2)}
            deleted: list[str] = []

            class Key:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            fake = types.SimpleNamespace(
                HKEY_CURRENT_USER=1, KEY_READ=1, KEY_WRITE=2, REG_SZ=1, REG_EXPAND_SZ=2,
                OpenKey=lambda *a: Key(), QueryValueEx=lambda key, name: store[name],
                SetValueEx=lambda key, name, _r, kind, value: store.__setitem__(name, (value, kind)),
                DeleteKey=lambda hive, key: deleted.append(key),
            )
            fake_os = types.ModuleType("os")
            fake_os.__dict__.update(os.__dict__)
            fake_os.name = "nt"
            real_rmtree = installer.shutil.rmtree

            def locked_rmtree(path, ignore_errors=False, onerror=None, **kw):
                # Windows keeps the running crit.exe (and its folder) locked.
                if Path(path) in (crit_home / "app", app):
                    return
                real_rmtree(path, ignore_errors=ignore_errors)

            started: list[str] = []
            out: list[str] = []
            with patch.object(installer, "os", fake_os), patch.object(cfg, "os", fake_os), patch.dict(
                os.environ, env
            ), patch.dict(sys.modules, {"winreg": fake}), patch.object(
                installer, "frozen_bundle_dir", return_value=app
            ), patch.object(installer, "_broadcast_environment_change") as broadcast, patch.object(
                installer.shutil, "rmtree", side_effect=locked_rmtree
            ), patch.object(
                installer.subprocess, "Popen", side_effect=lambda cmd, **kw: started.append(cmd)
            ), patch("builtins.input", side_effect=AssertionError("asked")):
                self.assertEqual(installer.uninstall(say=out.append), 0)
            self.assertEqual(store["Path"][0], "C:\\Windows;C:\\tools")
            broadcast.assert_called()
            self.assertEqual(deleted, [installer.UNINSTALL_KEY])
            self.assertEqual(len(started), 1)
            self.assertIn("rmdir /s /q", started[0])
            self.assertIn(str(crit_home), started[0])
            self.assertIn("ping -n 3 127.0.0.1", started[0])  # about 2 s before the first try
            self.assertEqual(out, ["crit is removed (settings, sign-in, and program). Project .crit folders were kept."])
            self.assertFalse((crit_home / "browser-profile").exists())
            self.assertFalse((crit_home / "config.json").exists())
            self.assertFalse((crit_home / "bin").exists())
            self.assertFalse(old_programs.exists())
            self.assertFalse((me / "AppData" / "Local" / "critique-bot").exists())
            self.assertFalse((me / "AppData" / "Roaming" / "critique-bot").exists())
            self.assertTrue(project.exists())

    def test_install_adds_the_installed_apps_entry(self) -> None:
        import types

        values: dict[str, object] = {}

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        fake = types.SimpleNamespace(
            HKEY_CURRENT_USER=1, REG_SZ=1, REG_DWORD=4,
            CreateKey=lambda hive, key: values.setdefault("_key", key) and Key(),
            SetValueEx=lambda key, name, _r, kind, value: values.__setitem__(name, value),
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict("sys.modules", {"winreg": fake}), patch.dict(
            os.environ, {"CRIT_HOME": str(Path(tmp) / ".crit")}
        ):
            app = Path(tmp) / ".crit" / "app" / __version__
            app.mkdir(parents=True)
            (app / installer._exe_name()).write_bytes(b"x" * 4096)
            installer._windows_register_app(app)
        self.assertEqual(values["_key"], installer.UNINSTALL_KEY)
        self.assertEqual(values["DisplayName"], "crit")
        self.assertEqual(values["Publisher"], "crit")
        self.assertEqual(values["DisplayVersion"], __version__)
        self.assertEqual(values["UninstallString"], f'"{app / installer._exe_name()}" uninstall')
        self.assertEqual(values["InstallLocation"], str(Path(tmp) / ".crit"))
        self.assertEqual((values["NoModify"], values["NoRepair"]), (1, 1))
        self.assertGreaterEqual(values["EstimatedSize"], 4)


if __name__ == "__main__":
    unittest.main()
