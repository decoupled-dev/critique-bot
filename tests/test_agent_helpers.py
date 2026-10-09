"""One task split across helper chat tabs: parallel briefs, file ownership, reports back to the coordinator."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from rich.console import Console

from critique_bot import agent, agent_helpers, agent_tools, agent_ui
from critique_bot.agent import run_agent_loop


def _call(tool: str, **arguments: object) -> str:
    return "<tool_call>\n" + json.dumps({"tool": tool, "arguments": arguments}) + "\n</tool_call>"


class _HelperTab:
    """A fake helper chat tab. It answers by the brief's name, from a script per helper."""

    scripts: dict[str, list[str]] = {}
    threads: set[str] = set()
    opened = 0
    lock = threading.Lock()

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.last_detail = {"complete": True}
        self.replies: list[str] = []
        with _HelperTab.lock:
            _HelperTab.opened += 1

    def __enter__(self) -> "_HelperTab":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        with _HelperTab.lock:
            _HelperTab.threads.add(threading.current_thread().name)
        if not self.replies:
            for name, script in _HelperTab.scripts.items():
                if f"You are {name}," in prompt:
                    self.replies = list(script)
                    break
        time.sleep(0.2)  # a slow model: two tabs must overlap
        return self.replies.pop(0) if self.replies else "COMPLETED"


class _Coordinator:
    def __init__(self, replies: list[str]) -> None:
        self.sent: list[str] = []
        self.last_detail = None
        self.replies = list(replies)

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        return self.replies.pop(0)


class DelegateTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_ui.reset()
        agent_ui.set_console(Console(file=io.StringIO(), force_terminal=False, width=100, theme=agent_ui.rich_theme("dark")))
        self.root = Path(tempfile.mkdtemp())
        (self.root / "a.xml").write_text("<a>\n    <x/>\n</a>\n", encoding="utf-8")
        (self.root / "b.kt").write_text("fun b() {\n    println(1)\n}\n", encoding="utf-8")
        _HelperTab.scripts = {}
        _HelperTab.threads = set()
        _HelperTab.opened = 0

    def tearDown(self) -> None:
        agent_ui.reset()

    def _loop(self, coordinator: _Coordinator, task: str, **kwargs):
        outcome: list[str] = []
        run_agent_loop(
            coordinator,
            workspace=self.root,
            index_path=None,
            cache_dir=None,
            first_task=task,
            max_rounds=20,
            max_result_chars=20_000,
            read_message=lambda: None,
            emit=lambda _t: None,
            approve_mode="auto",
            outcome=outcome,
            helper_factory=_HelperTab,
            helper_count=2,
            **kwargs,
        )
        return outcome[-1] if outcome else ""

    def test_two_helpers_edit_their_own_files_at_the_same_time(self) -> None:
        _HelperTab.scripts = {
            "xml": [
                _call("read_files", path="a.xml"),
                _call("edit_file", path="a.xml", old_string="    <x/>\n", new_string="    <x/>\n    <y/>\n"),
                "COMPLETED Added <y/> after <x/> in a.xml line 3.",
            ],
            "kotlin": [
                _call("read_files", path="b.kt"),
                _call("edit_file", path="a.xml", old_string="<a>", new_string="<z>"),
                _call("edit_file", path="b.kt", old_string="    println(1)", new_string="    println(2)"),
                "COMPLETED Changed println(1) to println(2) in b.kt.",
            ],
        }
        delegate = _call(
            "delegate",
            tasks=[
                {"name": "xml", "brief": "In a.xml add <y/> after <x/> with the same indent.", "files": ["a.xml"]},
                {"name": "kotlin", "brief": "In b.kt change println(1) to println(2).", "files": ["b.kt"]},
            ],
        )
        coordinator = _Coordinator([delegate, _call("git_diff"), "COMPLETED"])
        started = time.monotonic()
        code = self._loop(coordinator, "add <y/> to a.xml and print 2 in b.kt")
        elapsed = time.monotonic() - started
        self.assertEqual(code, "COMPLETED")
        self.assertEqual((self.root / "a.xml").read_text(encoding="utf-8"), "<a>\n    <x/>\n    <y/>\n</a>\n")
        self.assertEqual((self.root / "b.kt").read_text(encoding="utf-8"), "fun b() {\n    println(2)\n}\n")
        result = coordinator.sent[1]
        self.assertIn("2 helper tabs finished", result)
        self.assertIn("Added <y/> after <x/>", result)
        self.assertIn("Changed println(1) to println(2)", result)
        self.assertIn("Files changed by helpers: a.xml, b.kt", result)
        self.assertEqual(len(_HelperTab.threads), 2)  # two tabs, two threads
        self.assertLess(elapsed, 0.2 * 7)  # 7 helper replies took about 4 rounds of time, not 7

    def test_helper_cannot_touch_another_helpers_file_or_build(self) -> None:
        _HelperTab.scripts = {
            "reader": [
                _call("edit_file", path="a.xml", old_string="<a>", new_string="<z>"),
                _call("run_command", command="./gradlew build"),
                "COMPLETED Could not change a.xml; it is not mine. Run ./gradlew build.",
            ],
        }
        delegate = _call("delegate", tasks=[{"name": "reader", "brief": "Investigate a.xml and report its root element."}])
        coordinator = _Coordinator([delegate, "COMPLETED"])
        self._loop(coordinator, "what is the root element of a.xml?")
        self.assertEqual((self.root / "a.xml").read_text(encoding="utf-8"), "<a>\n    <x/>\n</a>\n")
        result = coordinator.sent[1]
        self.assertIn("Run ./gradlew build", result)

    def test_tabs_are_reused_for_the_next_split(self) -> None:
        _HelperTab.scripts = {"one": ["COMPLETED first"], "two": ["COMPLETED second"]}
        first = _call("delegate", tasks=[{"name": "one", "brief": "Read a.xml and say what is in it."}])
        second = _call("delegate", tasks=[{"name": "two", "brief": "Read b.kt and say what is in it."}])
        coordinator = _Coordinator([first, second, "COMPLETED"])
        self._loop(coordinator, "look at both files and tell me")
        self.assertEqual(_HelperTab.opened, 1)
        self.assertIn("COMPLETED".lower(), coordinator.sent[2].lower())

    def test_without_helpers_the_tool_says_so(self) -> None:
        ctx = agent_tools.ToolContext(workspace=self.root)
        result = agent_tools.execute("delegate", {"tasks": ["Read a.xml and report the root element."]}, ctx)
        self.assertFalse(result["ok"])
        self.assertIn("do the work here", result["error"])
        seed = agent.seed_message(self.root, "", helpers=0)
        self.assertIn("helper tabs: none", seed)
        self.assertIn("helper tabs: 2", agent.seed_message(self.root, "", helpers=2))

    def test_file_ownership_is_validated(self) -> None:
        ctx = agent_tools.ToolContext(workspace=self.root)
        briefs, problem = agent_tools.delegate_briefs(
            {"tasks": [{"brief": "change a.xml in a careful way", "files": ["a.xml"]}, {"brief": "also change a.xml somehow", "files": "a.xml"}]},
            ctx,
        )
        self.assertIn("give each file to one helper", problem)
        _, problem = agent_tools.delegate_briefs({"tasks": [{"brief": "x"}]}, ctx)
        self.assertIn("self-contained brief", problem)
        perm = agent_tools.permission_for("delegate", {"tasks": [{"brief": "read a.xml and report back", "files": []}]}, ctx)
        self.assertEqual(perm.kind, "read")
        perm = agent_tools.permission_for("delegate", {"tasks": [{"brief": "change a.xml in a careful way", "files": ["a.xml"]}]}, ctx)
        self.assertEqual((perm.kind, perm.key), ("edit", "edit"))

    def test_report_formatting(self) -> None:
        reports = [
            agent_helpers.Report("xml", "COMPLETED", "done", ["a.xml"], 3, 12.0),
            agent_helpers.Report("kotlin", "FAILED", "", [], 1, 2.0, error="tab closed"),
        ]
        result = agent_helpers.format_reports(reports, 14.0)
        self.assertTrue(result["ok"])
        self.assertIn("--- kotlin: FAILED", result["output"])
        self.assertIn("error: tab closed", result["output"])
        self.assertEqual(result["ui"]["summary"], "2 helper tabs · 14s · changed 1 file")


if __name__ == "__main__":
    unittest.main()
