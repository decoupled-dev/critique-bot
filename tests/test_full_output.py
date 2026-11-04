"""Long command output: the middle is saved to a file the model can read."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from critique_bot import agent_tools


class FullOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.cache = self.ws / ".bot" / "cache"
        self.cache.mkdir(parents=True)
        self.ctx = agent_tools.ToolContext(workspace=self.ws, cache_dir=self.cache, max_chars=4000)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, lines: int) -> dict:
        command = f'"{sys.executable}" -c "for i in range({lines}): print(\'line\', i)"'
        return agent_tools.execute("run_command", {"command": command}, self.ctx)

    def test_long_output_is_saved_and_readable(self) -> None:
        result = self._run(3000)
        self.assertIn("FULL OUTPUT", result["output"])
        name = result["output"].split("saved to ")[1].split(":")[0]
        self.assertTrue(name.startswith(".bot/cache/output/"))
        self.assertIn("line 1777", (self.ws / name).read_text(encoding="utf-8"))
        middle = agent_tools.execute("read_files", {"path": name, "offset": 1500, "limit": 3}, self.ctx)
        self.assertIn("line 1498", middle["output"])

    def test_short_output_saves_nothing(self) -> None:
        result = self._run(5)
        self.assertNotIn("FULL OUTPUT", result["output"])
        self.assertFalse((self.cache / "output").exists())

    def test_old_outputs_are_removed(self) -> None:
        folder = self.cache / "output"
        folder.mkdir()
        for index in range(agent_tools.KEEP_OUTPUTS + 5):
            (folder / f"old-{index:03d}.log").write_text("x", encoding="utf-8")
        self._run(3000)
        self.assertEqual(len(list(folder.glob("*.log"))), agent_tools.KEEP_OUTPUTS)


if __name__ == "__main__":
    unittest.main()
