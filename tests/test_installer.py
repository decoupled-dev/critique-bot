"""critique-bot install: the program in the user's folders, launchers, and PATH."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot import __version__, installer


@unittest.skipIf(os.name == "nt", "the POSIX layout")
class PosixInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.bundle = root / "download" / f"critique-bot-{__version__}-linux-x64"
        (self.bundle / "_internal").mkdir(parents=True)
        exe = self.bundle / "critique-bot"
        exe.write_text("#!/bin/sh\necho hi\n")
        exe.chmod(0o644)
        (self.home / ".bashrc").write_text("# mine\n")
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "XDG_DATA_HOME": "", "CRIT_HOME": "", "PATH": "/usr/bin:/bin"})
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
        self.assertTrue((app / "critique-bot").stat().st_mode & stat.S_IXUSR)
        self.assertEqual(crit_home.stat().st_mode & 0o777, 0o700)
        crit = crit_home / "bin" / "crit"
        self.assertIn("--as-crit", crit.read_text())
        self.assertIn(str(app / "critique-bot"), crit.read_text())
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

    def test_uninstall_keeps_settings(self) -> None:
        installer.install(self.bundle, say=lambda _: None)
        self.assertEqual(installer.uninstall(say=lambda _: None), 0)
        self.assertFalse((self.home / ".crit" / "bin" / "crit").exists())
        self.assertFalse((self.home / ".crit" / "app").exists())


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


if __name__ == "__main__":
    unittest.main()
