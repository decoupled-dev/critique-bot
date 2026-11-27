# SPDX-License-Identifier: Apache-2.0
"""The web chat is a removable module: crit works with critique_bot.webchat gone.

A child process blocks the package from importing (as if the folder were deleted), then
imports every core module and checks what a user sees. scripts/check_webchat_removable.py
goes further and runs the core test suite on a copy of the source without the folder.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from webchat_support import needs_webchat

SRC = Path(__file__).resolve().parents[1] / "src"

CHILD = textwrap.dedent('''
    import importlib, importlib.abc, json, pkgutil, sys

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "critique_bot.webchat" or name.startswith("critique_bot.webchat."):
                raise ModuleNotFoundError(f"No module named {name!r}")
            return None

    sys.meta_path.insert(0, Block())
    import critique_bot
    failed = {}
    for info in pkgutil.iter_modules(critique_bot.__path__):
        if info.name == "webchat":
            continue
        try:
            importlib.import_module(f"critique_bot.{info.name}")
        except Exception as exc:
            failed[info.name] = repr(exc)

    from critique_bot import model_api
    from critique_bot.config import BotConfig
    hooks = model_api.backend_hooks("browser")
    try:
        model_api.open_provider(BotConfig(url="https://chat.example", selectors=None, backend="browser"))
        opened = "opened"
    except model_api.BackendError as exc:
        opened = str(exc)
    print(json.dumps({"failed": failed, "hooks": hooks is None, "open": opened,
                      "loaded": sorted(m for m in sys.modules if m.startswith("critique_bot.webchat"))}))
''')


class WebChatRemovableTests(unittest.TestCase):
    def test_core_imports_and_explains_a_missing_web_chat(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        proc = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(result["failed"], {})
        self.assertTrue(result["hooks"], "backend_hooks should be None without the package")
        self.assertIn("pip install 'critique-bot[webchat]'", result["open"])
        self.assertEqual(result["loaded"], [])

    @needs_webchat
    def test_the_package_registers_the_browser_backend(self) -> None:
        from critique_bot import model_api

        hooks = model_api.backend_hooks("browser")
        self.assertIsNotNone(hooks)
        self.assertTrue(hasattr(hooks, "login") and hasattr(hooks, "setup_command"))


if __name__ == "__main__":
    unittest.main()
