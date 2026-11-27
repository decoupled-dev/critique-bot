# SPDX-License-Identifier: Apache-2.0
"""delegate(agent=...): a file-defined agent's tools, steps, parallel cap and worktree isolation."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console

from critique_bot import agent_ui
from critique_bot.agent import run_agent_loop
from critique_bot.extensions import trust
from test_agent_helpers import _call, _Coordinator, _HelperTab


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


class AgentDelegateTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_ui.reset()
        agent_ui.set_console(Console(file=io.StringIO(), force_terminal=False, width=100, theme=agent_ui.rich_theme("dark")))
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CRIT_HOME": str(Path(self._tmp.name) / "crit-home")})
        env.start()
        self.addCleanup(env.stop)
        self.root = Path(self._tmp.name) / "repo"
        self.root.mkdir()
        (self.root / "a.xml").write_text("<a>\n    <x/>\n</a>\n", encoding="utf-8")
        _HelperTab.scripts = {}
        _HelperTab.threads = set()
        _HelperTab.instances = []
        _HelperTab.opened = 0

    def tearDown(self) -> None:
        agent_ui.reset()

    def agent_file(self, name: str, text: str) -> None:
        path = trust.user_dir() / "agents" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")

    def loop(self, coordinator: _Coordinator, task: str) -> None:
        run_agent_loop(
            coordinator, workspace=self.root, index_path=None, cache_dir=None, first_task=task, max_rounds=20,
            max_result_chars=60_000, read_message=lambda: None, emit=lambda _t: None, approve_mode="auto",
            helper_factory=_HelperTab, helper_count=2,
        )

    def helper_text(self) -> str:
        return "\n".join(sent for tab in _HelperTab.instances for sent in tab.sent)

    def test_a_tool_outside_the_agents_list_is_refused(self) -> None:
        _HelperTab.scripts = {"rev": [
            _call("edit_file", path="a.xml", old_string="<a>", new_string="<z>"),
            _call("read_files", path="a.xml"),
            "COMPLETED The root element is <a>.",
        ]}
        delegate = _call("delegate", tasks=[{"name": "rev", "agent": "code-reviewer", "files": ["a.xml"],
                                             "brief": "Review a.xml and report its root element."}])
        coordinator = _Coordinator([delegate, "COMPLETED"])
        self.loop(coordinator, "review a.xml")
        self.assertEqual((self.root / "a.xml").read_text(encoding="utf-8"), "<a>\n    <x/>\n</a>\n")
        sent = self.helper_text()
        self.assertIn("cannot use edit_file", sent)
        self.assertIn("AGENT: code-reviewer", sent)  # the agent's instructions went with the brief
        self.assertIn("ROLE: Code Reviewer", sent)
        self.assertIn("The root element is <a>.", coordinator.sent[1])

    def test_the_last_step_removes_the_tools(self) -> None:
        self.agent_file("short", """
            ---
            name: short
            description: Reads two things at most.
            tools: [read_files]
            max_steps: 2
            ---
            Be brief.
            """)
        _HelperTab.scripts = {"s": [_call("read_files", path="a.xml"), "COMPLETED the summary"]}
        delegate = _call("delegate", tasks=[{"name": "s", "agent": "short", "brief": "Read a.xml twice and summarize it."}])
        coordinator = _Coordinator([delegate, "COMPLETED"])
        self.loop(coordinator, "summarize a.xml")
        self.assertIn("last step: tools are off", self.helper_text())
        self.assertIn("the summary", coordinator.sent[1])

    def test_max_parallel(self) -> None:
        self.agent_file("solo", """
            ---
            name: solo
            description: One at a time.
            tools: [read_files]
            max_parallel: 1
            ---
            """)
        _HelperTab.scripts = {"one": ["COMPLETED one"], "two": ["COMPLETED two"]}
        delegate = _call("delegate", tasks=[
            {"name": "one", "agent": "solo", "brief": "Read a.xml and say what is in it."},
            {"name": "two", "agent": "solo", "brief": "Read a.xml and say what is in it too."},
        ])
        coordinator = _Coordinator([delegate, "COMPLETED"])
        self.loop(coordinator, "look")
        self.assertIn("at most 1 at a time", coordinator.sent[1])

    def test_unknown_agent(self) -> None:
        delegate = _call("delegate", agent="nobody", task="Read a.xml and say what is in it.")
        coordinator = _Coordinator([delegate, "COMPLETED"])
        self.loop(coordinator, "look")
        self.assertIn("no agent named 'nobody'", coordinator.sent[1])

    def test_a_writer_works_in_a_worktree_and_its_diff_comes_back(self) -> None:
        _git(self.root, "init", "-q")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "init")
        (self.root / "notes.txt").write_text("uncommitted\n", encoding="utf-8")  # carried into the worktree
        self.agent_file("fixer", """
            ---
            name: fixer
            description: Makes small fixes.
            tools: [read_files, edit_file, write_files]
            ---
            Fix it.
            """)
        _HelperTab.scripts = {"fix": [
            _call("read_files", path="notes.txt"),
            _call("edit_file", path="a.xml", old_string="    <x/>\n", new_string="    <x/>\n    <y/>\n"),
            "COMPLETED Added <y/>.",
        ]}
        delegate = _call("delegate", tasks=[{"name": "fix", "agent": "fixer", "brief": "Add <y/> after <x/> in a.xml."}])
        coordinator = _Coordinator([delegate, "COMPLETED", "COMPLETED", "COMPLETED"])
        self.loop(coordinator, "add y")
        self.assertEqual((self.root / "a.xml").read_text(encoding="utf-8"), "<a>\n    <x/>\n</a>\n")
        result = coordinator.sent[1]
        self.assertIn("Patch from fix", result)
        self.assertIn("+    <y/>", result)
        self.assertNotIn("notes.txt", result.split("Patch from fix", 1)[1])  # only the agent's change
        self.assertIn("uncommitted", self.helper_text())
        listed = subprocess.run(["git", "worktree", "list"], cwd=self.root, capture_output=True, text=True).stdout
        self.assertEqual(len(listed.strip().splitlines()), 1)  # the worktree is gone


if __name__ == "__main__":
    unittest.main()
