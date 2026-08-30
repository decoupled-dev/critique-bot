from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build.py"


def _load_build():
    spec = importlib.util.spec_from_file_location("critique_bot_build_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = _load_build()

    def test_version_matches_package(self) -> None:
        from critique_bot import __version__

        self.assertEqual(self.build._version(), __version__)

    def test_platform_tag_linux_x64(self) -> None:
        with patch.object(self.build.sys, "platform", "linux"):
            with patch.object(self.build.platform, "machine", return_value="x86_64"):
                self.assertEqual(self.build._platform_tag(), "linux-x64")

    def test_platform_tag_windows_arm(self) -> None:
        with patch.object(self.build.sys, "platform", "win32"):
            with patch.object(self.build.platform, "machine", return_value="ARM64"):
                self.assertEqual(self.build._platform_tag(), "windows-arm64")

    def test_platform_tag_macos_amd64(self) -> None:
        with patch.object(self.build.sys, "platform", "darwin"):
            with patch.object(self.build.platform, "machine", return_value="amd64"):
                self.assertEqual(self.build._platform_tag(), "macos-x64")

    def test_binary_name(self) -> None:
        with patch.object(self.build.sys, "platform", "win32"):
            self.assertEqual(self.build._binary_name(), "critique-bot.exe")
        with patch.object(self.build.sys, "platform", "linux"):
            self.assertEqual(self.build._binary_name(), "critique-bot")

    def test_write_readme_linux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "README.txt"
            with patch.object(self.build.sys, "platform", "linux"):
                self.build._write_readme(dest, "critique-bot")
            text = dest.read_text(encoding="utf-8")
            self.assertIn("./critique-bot worker", text)
            self.assertIn("config.json", text)

    def test_write_readme_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "README.txt"
            with patch.object(self.build.sys, "platform", "win32"):
                self.build._write_readme(dest, "critique-bot.exe")
            text = dest.read_text(encoding="utf-8")
            self.assertIn(".\\critique-bot.exe", text)

    def test_main_missing_spec(self) -> None:
        with patch.object(self.build, "SPEC", Path("/no/such.spec")):
            with self.assertRaises(SystemExit) as ctx:
                self.build.main(["--skip-smoke"])
        self.assertIn("missing spec", str(ctx.exception))

    def test_main_missing_pyinstaller(self) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "PyInstaller":
                raise ImportError("nope")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", fake_import):
            with self.assertRaises(SystemExit) as ctx:
                self.build.main(["--skip-smoke"])
        self.assertIn("PyInstaller", str(ctx.exception))

    def test_smoke_test_failure(self) -> None:
        class Result:
            returncode = 1
            stdout = ""
            stderr = "boom"

        with patch.object(self.build.subprocess, "run", return_value=Result()):
            with self.assertRaises(SystemExit) as ctx:
                self.build._smoke_test(Path("critique-bot"))
        self.assertIn("--help failed", str(ctx.exception))

    def test_package_version_dunder(self) -> None:
        from critique_bot import __version__

        self.assertEqual(__version__, "0.1.0")


if __name__ == "__main__":
    unittest.main()
