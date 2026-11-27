# SPDX-License-Identifier: Apache-2.0
"""Shared fakes for the permissions / prompt-injection security tests (test_sec_permissions_*).

The "attacker" in these tests is the scripted model output (a prompt-injected model) or a
file planted in a temporary workspace. Nothing here touches the real home folder: the
isolation module points HOME and CRIT_HOME at a temporary folder before crit is imported,
and every workspace is a fresh temporary directory.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import test_00_isolation  # noqa: F401  (HOME, CRIT_HOME and XDG folders -> a temp folder, before crit loads)

from rich.console import Console

from critique_bot import agent_ui, taint
from critique_bot.agent import run_agent_loop


def call(tool: str, **arguments: object) -> str:
    """One tool call as the model would write it."""
    return "<tool_call>\n" + json.dumps({"tool": tool, "arguments": arguments}) + "\n</tool_call>"


class Scripted:
    """A model whose replies are fixed in advance: the attacker-controlled side of the session."""

    def __init__(self, replies: list[str]) -> None:
        self.sent: list[str] = []
        self.last_detail = None
        self._replies = list(replies)

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        return self._replies.pop(0) if self._replies else "COMPLETED"


GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


def git_repo(root: Path) -> None:
    """``root`` as a git repository with one commit of everything in it."""
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", "start")


class Approvals:
    """Records every approval prompt and answers from a list (then ``default``)."""

    def __init__(self, answers: list[str] | None = None, default: str = "no") -> None:
        self.asked: list[Any] = []
        self.answers = list(answers or [])
        self.default = default

    def __call__(self, perm: Any, **_kw: Any) -> tuple[str, str]:
        self.asked.append(perm)
        answer = self.answers.pop(0) if self.answers else self.default
        return answer, ""

    def patch(self) -> Any:
        return mock.patch("critique_bot.engine.ui._approve_prompt", self)


class SecBase(unittest.TestCase):
    """A temporary workspace (``self.root``) and a sibling folder outside it (``self.outside``)."""

    def setUp(self) -> None:
        agent_ui.reset()
        agent_ui.set_console(Console(file=io.StringIO(), force_terminal=False, width=100,
                                     theme=agent_ui.rich_theme("dark")))
        taint.reset()
        self._tmp = Path(tempfile.mkdtemp(prefix="crit-sec-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.root = self._tmp / "ws"
        self.root.mkdir()
        self.outside = self._tmp / "outside"
        self.outside.mkdir()
        home = self._tmp / "home"
        (home / ".crit").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"HOME": str(home), "CRIT_HOME": str(home / ".crit")})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self) -> None:
        taint.reset()
        agent_ui.reset()

    def loop(self, session: Scripted, task: str = "do the task", **kwargs: Any) -> str:
        kwargs.setdefault("max_rounds", 12)
        kwargs.setdefault("max_result_chars", 20_000)
        kwargs.setdefault("read_message", lambda: None)
        kwargs.setdefault("emit", lambda _text: None)
        kwargs.setdefault("ask_user", lambda _question: None)
        outcome: list[str] = []
        run_agent_loop(session, workspace=self.root, index_path=None, cache_dir=None, first_task=task,
                       outcome=outcome, **kwargs)
        return outcome[-1] if outcome else ""


def use_terminal_ui() -> Callable[[], None]:
    """Drive the engine with the terminal UI (as tests/test_agent_modes.py does); returns the undo."""
    from critique_bot import agent as _agent

    _agent.use_ui(agent_ui)
    return lambda: _agent.use_ui(None)
