# SPDX-License-Identifier: Apache-2.0
"""scripts/eval/memory_eval.py: the metric math of the warm-up-then-test protocol on fake
session transcripts (turns to the first correct file, tokens before the first edit, hunch acc@5
and wrong hunches, the on/off deltas) and the leakage guard."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("memory_eval", ROOT / "scripts" / "eval" / "memory_eval.py")
memory_eval = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(memory_eval)


def turn(n: int, prompt: int, completion: int = 10) -> dict:
    return {"type": "assistant_turn", "turn": n, "usage": {"prompt": prompt, "completion": completion}}


def call(n: int, tool: str, **args) -> dict:
    return {"type": "tool_started", "turn": n, "tool": tool, "arguments": args}


def transcript(*, hunch: list[str] | None) -> list[dict]:
    events = [{"type": "user_message", "task": True, "text": "fix the unit"}]
    if hunch is not None:
        events.append({"type": "memory_recall", "files": [{"path": p, "confidence": 0.5} for p in hunch]})
    events += [
        turn(1, 1000), call(1, "search_code", pattern="unit"),
        turn(2, 1500), call(2, "read_files", paths=["src/other.py"]),
        turn(3, 2000), call(3, "read_files", paths=[{"path": "./src/helper.py"}]),
        turn(4, 2500), call(4, "edit_file", path="src/helper.py", old_string="a", new_string="b"),
        turn(5, 3000),
        {"type": "task_finished", "code": "COMPLETED", "changed": ["src/helper.py", "tests/test_helper.py"]},
    ]
    return events


class MetricTests(unittest.TestCase):
    def test_task_metrics(self) -> None:
        found = memory_eval.task_metrics(transcript(hunch=["src/helper.py", "src/panel.py"]))
        self.assertEqual(found["turns_to_first_correct_file"], 3)
        self.assertEqual(found["tokens_before_first_edit"], 1010 + 1510 + 2010 + 2510)
        self.assertEqual(found["turns"], 5)
        self.assertTrue(found["acc_at_5"])
        self.assertFalse(found["wrong_hunch"])
        wrong = memory_eval.task_metrics(transcript(hunch=["src/panel.py", "src/helper.py"]))
        self.assertTrue(wrong["acc_at_5"])
        self.assertTrue(wrong["wrong_hunch"])
        miss = memory_eval.task_metrics(transcript(hunch=["a.py", "b.py", "c.py", "d.py", "e.py", "src/helper.py"]))
        self.assertFalse(miss["acc_at_5"])  # the right file is sixth
        none = memory_eval.task_metrics(transcript(hunch=None))
        self.assertIsNone(none["acc_at_5"])
        self.assertIsNone(none["wrong_hunch"])

    def test_changed_from_git_wins_and_patch_paths_count(self) -> None:
        events = [turn(1, 100), call(1, "apply_patch", patch="*** Begin Patch\n*** Update File: lib/x.py\n@@\n-a\n+b\n"),
                  {"type": "task_finished", "changed": []}]
        found = memory_eval.task_metrics(events, changed=["lib/x.py"])
        self.assertEqual(found["turns_to_first_correct_file"], 1)
        self.assertEqual(found["tokens_before_first_edit"], 110)  # the turn that edits counts

    def test_aggregate_deltas_and_wrong_hunch_pass_rate(self) -> None:
        def rec(task, condition, run, turns, tokens, passed, wrong=None, acc=None, phase="test", leak=""):
            return {"task": task, "condition": condition, "run": run, "phase": phase, "passed": passed, "leak": leak,
                    "turns_to_first_correct_file": turns, "tokens_before_first_edit": tokens, "tokens": tokens * 2,
                    "seconds": 10.0, "wrong_hunch": wrong, "acc_at_5": acc}

        records = [
            rec("a", "on", 1, 2, 3000, True, wrong=False, acc=True),
            rec("b", "on", 1, 3, 4000, False, wrong=True, acc=True),
            rec("a", "off", 1, 5, 5000, True),
            rec("b", "off", 1, 5, 5000, False),
            rec("w", "on", 1, 1, 1, True, phase="warmup"),
            rec("leaky", "on", 1, 1, 1, True, leak="w"),
        ]
        summary = memory_eval.aggregate(records)
        self.assertEqual(summary["on"]["trials"], 2)
        self.assertAlmostEqual(summary["delta"]["turns_to_first_correct_file"], (2.5 - 5) / 5)
        self.assertAlmostEqual(summary["delta"]["tokens_before_first_edit"], (3500 - 5000) / 5000)
        self.assertEqual(summary["delta"]["pass_at_1"], 0.0)
        self.assertEqual(summary["on"]["acc_at_5"], 1.0)
        self.assertEqual(summary["on"]["wrong_hunch_rate"], 0.5)
        self.assertEqual(summary["delta"]["pass_at_1_wrong_hunch_on"], 0.0)
        self.assertEqual(summary["delta"]["pass_at_1_wrong_hunch_off"], 0.0)
        self.assertEqual(summary["leaks"], ["leaky"])
        text = memory_eval.render(summary)
        self.assertIn("| turns_to_first_correct_file | 2.50 | 5.00 | -50% |", text)
        self.assertIn("leaky", text)


class LeakTests(unittest.TestCase):
    def test_overlapping_references_are_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, body in (("warm", "def convert(value):\n    return value * 9 / 5 + 32\n"),
                               ("same", "def convert(value):\n    return value * 9 / 5 + 32\n"),
                               ("new", "def render_panel(state):\n    return draw_everything(state)\n")):
                (root / name / "solution").mkdir(parents=True)
                (root / name / "solution" / "x.py").write_text(body, encoding="utf-8")
            lines = {name: memory_eval.reference_lines(root / name, {"reference": "solution"}) for name in ("warm", "same", "new")}
            found = memory_eval.leaks({"warm": lines["warm"]}, {"same": lines["same"], "new": lines["new"]})
            self.assertEqual(found, {"same": "warm"})


if __name__ == "__main__":
    unittest.main()
