# SPDX-License-Identifier: Apache-2.0
"""scripts/eval/obfuscate.py: identifiers renamed, behavior, strings and line numbers kept."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASKS = REPO / "tests" / "eval_tasks"


def load():
    spec = importlib.util.spec_from_file_location("obfuscate", REPO / "scripts" / "eval" / "obfuscate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ObfuscateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ob = load()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_python_tree_keeps_behavior_and_strings(self) -> None:
        src = TASKS / "loc-py-thermostat-unit" / "files"
        info = self.ob.obfuscate_tree(src, self.out / "py", strip_comments=True)
        mapping = info["mapping"]
        self.assertIn("load_unit", mapping)
        self.assertIn("SettingsStore", mapping)
        text = (self.out / "py" / "thermo" / "settings_store.py").read_text()
        self.assertNotIn("load_unit", text)
        self.assertIn('"temperature_unit"', text)  # strings stay exact
        self.assertNotIn("#", text)  # comments stripped
        original = (src / "thermo" / "settings_store.py").read_text()
        self.assertEqual(text.count("\n"), original.count("\n"))  # line numbers kept
        proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                              cwd=self.out / "py", capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_library_names_are_kept(self) -> None:
        src = TASKS / "loc-js-cache-ttl" / "files"
        info = self.ob.obfuscate_tree(src, self.out / "js")
        for name in ("get", "set", "entries", "require", "now"):
            self.assertNotIn(name, info["mapping"])
        self.assertIn("isFresh", info["mapping"])

    @unittest.skipUnless(shutil.which("javac"), "javac not installed")
    def test_java_files_follow_their_class_and_compile(self) -> None:
        src = TASKS / "loc-java-coupon-expiry" / "files"
        info = self.ob.obfuscate_tree(src, self.out / "java")
        self.assertIn("src/shop/CouponValidator.java", info["files"])
        self.assertEqual(self.ob.check_tree(self.out / "java"), [])

    @unittest.skipIf(sys.version_info < (3, 12), "the obfuscator needs Python 3.12+ (names inside f-strings)")
    def test_generated_tasks_match_their_source(self) -> None:
        """The committed -obf tasks are what the script produces now (regenerate when it changes)."""
        for task in sorted(TASKS.glob("loc-*-obf")):
            source = TASKS / task.name[: -len("-obf")]
            with self.subTest(task=task.name):
                fresh = self.out / task.name
                self.ob.obfuscate_task(source, fresh, strip_comments=False)
                self.assertEqual(json.loads((fresh / "task.json").read_text()), json.loads((task / "task.json").read_text()))
                for path in sorted((fresh / "files").rglob("*")):
                    if path.is_file():
                        rel = path.relative_to(fresh)
                        self.assertEqual(path.read_text(), (task / rel).read_text(), str(rel))


if __name__ == "__main__":
    unittest.main()
