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
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "XDG_DATA_HOME": "", "PATH": "/usr/bin:/bin"})
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
        app = self.home / ".local" / "share" / "critique-bot" / f"app-{__version__}"
        self.assertTrue((app / "critique-bot").stat().st_mode & stat.S_IXUSR)
        crit = self.home / ".local" / "bin" / "crit"
        self.assertIn("--as-crit", crit.read_text())
        self.assertIn(str(app / "critique-bot"), crit.read_text())
        self.assertTrue(crit.stat().st_mode & stat.S_IXUSR)
        profile = (self.home / ".profile").read_text()
        self.assertIn(".local/bin", profile)
        self.assertIn("# mine", (self.home / ".bashrc").read_text())
        self.assertIn(".local/bin", (self.home / ".bashrc").read_text())
        self.assertTrue(installer.is_installed_copy(app))
        self.assertFalse(installer.is_installed_copy(self.bundle))
        # Installing again (an update) does not add PATH twice.
        self.assertEqual(installer.install(self.bundle, say=out.append), 0)
        self.assertEqual((self.home / ".profile").read_text().count(".local/bin"), 1)

    def test_old_versions_are_removed(self) -> None:
        old = self.home / ".local" / "share" / "critique-bot" / "app-0.0.1"
        old.mkdir(parents=True)
        installer.install(self.bundle, say=lambda _: None)
        self.assertFalse(old.exists())

    def test_from_source_it_explains(self) -> None:
        out: list[str] = []
        with patch.object(installer, "frozen_bundle_dir", return_value=None):
            self.assertEqual(installer.install(say=out.append), 1)
        self.assertIn("downloaded program", out[0])

    def test_uninstall_keeps_settings(self) -> None:
        installer.install(self.bundle, say=lambda _: None)
        self.assertEqual(installer.uninstall(say=lambda _: None), 0)
        self.assertFalse((self.home / ".local" / "bin" / "crit").exists())
        self.assertFalse((self.home / ".local" / "share" / "critique-bot").exists())


if __name__ == "__main__":
    unittest.main()
