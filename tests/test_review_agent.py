from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from critique_bot import review_agent
from critique_bot.agent import run_agent_loop
from critique_bot.agent_cli import main as crit_main
from critique_bot.cli import main as bot_main


def _call(tool: str, **arguments: object) -> str:
    return "<tool_call>\n" + json.dumps({"tool": tool, "arguments": arguments}) + "\n</tool_call>"


class _Scripted:
    def __init__(self, replies: list[str]) -> None:
        self.sent: list[str] = []
        self.last_detail = None
        self._replies = list(replies)

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        return self._replies.pop(0)


def _loop(session, root: Path, task: str, **kwargs):
    kwargs.setdefault("max_rounds", None)
    kwargs.setdefault("max_result_chars", 8000)
    kwargs.setdefault("read_message", lambda: None)
    kwargs.setdefault("emit", lambda _text: None)
    kwargs.setdefault("ask_user", lambda _question: None)
    # Never prompt: a review must not reach the approval prompt at all.
    with patch("critique_bot.agent._approve_prompt", side_effect=AssertionError("asked for approval")):
        return run_agent_loop(
            session, workspace=root, index_path=None, cache_dir=None, first_task=task, **kwargs
        )

REVIEW = (
    "**Risk: Moderate risk**\n\n"
    "Adds a greeting.\nNo test covers it.\nAdd one.\n\n"
    "```json\n"
    + json.dumps(
        {
            "risk": "moderate",
            "comments": [
                {
                    "path": "app.py",
                    "line": 1,
                    "side": "new",
                    "severity": "test",
                    "body": "**Missing test**\n\nNo test.\n\n**Action:** add one.",
                    "impact": "A regression ships.",
                }
            ],
        }
    )
    + "\n```"
)


class ReviewLoopTests(unittest.TestCase):
    def test_reads_run_writes_and_commands_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
            session = _Scripted(
                [
                    _call("read_files", paths=["app.py"])
                    + "\n"
                    + _call("write_files", path="app.py", contents="boom\n")
                    + "\n"
                    + _call("run_command", command="touch made-by-review"),
                    REVIEW + "\n\nCOMPLETED",
                ]
            )
            turns = _loop(session, root, "review it", review=True, approve_mode="auto")
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "print('hi')\n")
            self.assertFalse((root / "made-by-review").exists())
            self.assertEqual(len(session.sent), 2)
            results = session.sent[1]
            self.assertIn("print('hi')", results)
            self.assertIn("read-only review", results)
            text = review_agent.review_text(turns)
            self.assertTrue(text.startswith("**Risk: Moderate risk**"))
            self.assertNotIn("COMPLETED", text)

    def test_review_that_hands_work_back_still_ends(self) -> None:
        # "you should add a test" reads like a hand-back to the generic loop; a review is the answer.
        with tempfile.TemporaryDirectory() as tmp:
            session = _Scripted([REVIEW.replace("Add one.", "You should add a test yourself.")])
            turns = _loop(session, Path(tmp), "review it", review=True)
            self.assertEqual(len(session.sent), 1)
            self.assertTrue(review_agent.review_text(turns))

    def test_missing_review_is_asked_for_then_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Scripted(["Looks fine to me."] * (review_agent.MAX_NUDGES + 1))
            outcome: list[str] = []
            turns = _loop(session, Path(tmp), "review it", review=True, outcome=outcome)
            self.assertEqual(outcome, ["FAILED"])
            self.assertIn(review_agent.NUDGE, session.sent[1])
            self.assertEqual(review_agent.review_text(turns), "")

    def test_last_round_asks_for_the_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("a\n", encoding="utf-8")
            session = _Scripted([_call("read_files", paths=["a.txt"]), REVIEW])
            _loop(session, root, "review it", review=True, max_rounds=2)
            self.assertNotIn(review_agent.LAST_ROUND, session.sent[0])
            self.assertIn(review_agent.LAST_ROUND, session.sent[1])


class BuildTaskTests(unittest.TestCase):
    def test_task_has_patch_paths_and_no_file_bodies(self) -> None:
        task = review_agent.build_task(
            "FILES\n{files}\nPATCH\n{patch}\n", "+x = 1", changed_paths=["src/a.py"], max_rounds=7
        )
        self.assertIn("- src/a.py", task)
        self.assertIn("+x = 1", task)
        self.assertIn("at most 7 tool rounds", task)
        self.assertIn(review_agent.FILES_NOTE, task)


class EngineChoiceTests(unittest.TestCase):
    def _choice(self, main, argv: list[str]) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text("{}", encoding="utf-8")
            with patch("critique_bot.cli._main_review_agent", return_value=0) as agent_review, patch(
                "critique_bot.cli.load_config", side_effect=__import__("critique_bot.config").config.ConfigError("stop")
            ):
                main([*argv, "--config", str(config), "--repo-dir", tmp])
            return agent_review.called

    def test_crit_review_is_the_agent_review(self) -> None:
        self.assertTrue(self._choice(crit_main, ["review"]))
        self.assertFalse(self._choice(crit_main, ["review", "--quick"]))
        self.assertTrue(self._choice(crit_main, ["--mode", "review"]))

    def test_critique_bot_review_stays_one_shot(self) -> None:
        self.assertFalse(self._choice(bot_main, ["review"]))
        self.assertFalse(self._choice(bot_main, []))
        self.assertTrue(self._choice(bot_main, ["review", "--deep"]))

    def test_engine_flags_are_review_only_and_not_for_submit(self) -> None:
        self.assertEqual(crit_main(["chat", "--quick", "--config", "c.json"]), 1)
        self.assertEqual(bot_main(["submit", "--config", "c.json", "--deep"]), 1)


if __name__ == "__main__":
    unittest.main()
