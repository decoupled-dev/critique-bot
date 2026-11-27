#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run the core test suite on a copy of crit without critique_bot/webchat/.

Test modules that import the web chat are left out (they test it); every other test must
pass. CI runs this so the web chat stays removable.

    python3 scripts/check_webchat_removable.py
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def uses_web_chat(path: Path) -> bool:
    """True when the module imports the web chat at its top level (a web-chat test module).

    Web-chat tests inside other modules import it inside the test and carry
    ``@needs_webchat`` (tests/webchat_support.py), so they skip instead.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module.startswith("critique_bot.webchat") or (
                node.module == "critique_bot" and any(a.name == "webchat" for a in node.names))):
            return True
        if isinstance(node, ast.Import) and any(a.name.startswith("critique_bot.webchat") for a in node.names):
            return True
    return False


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="crit-no-webchat-") as tmp:
        copy = Path(tmp)
        # The whole checkout (tests read prompts, scripts, packaging), minus the web chat.
        shutil.copytree(ROOT, copy, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv", "build", "dist", "out", ".crit"))
        shutil.rmtree(copy / "src" / "critique_bot" / "webchat")
        left_out = []
        for path in sorted((copy / "tests").glob("test_*.py")):
            if uses_web_chat(path):
                left_out.append(path.name)
                path.unlink()
        print(f"left out {len(left_out)} web-chat test modules: {', '.join(left_out)}", flush=True)
        env = {**os.environ, "PYTHONPATH": str(copy / "src")}
        proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=copy, env=env)
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
