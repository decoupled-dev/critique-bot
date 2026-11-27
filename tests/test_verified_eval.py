# SPDX-License-Identifier: Apache-2.0
"""scripts/eval/verified_eval.py: the Stage 7 numbers, on fake results (no model)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("verified_eval", ROOT / "scripts" / "eval" / "verified_eval.py")
ve = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(ve)


def _row(task: str, result: str, code: str = "COMPLETED", tokens: int = 1000, flow: dict | None = None) -> dict:
    row = {"task": task, "result": result, "exit_reason": code, "exit": 0 if code == "COMPLETED" else 1,
           "prompt_tokens": tokens, "completion_tokens": 0}
    if flow is not None:
        row["flow"] = flow
    return row


class MathTest(unittest.TestCase):
    def test_precision_counts_only_claimed_fixes(self) -> None:
        rows = [_row("a", "pass"), _row("b", "fail"), _row("c", "fail", "FAILED"), _row("d", "fail", "STOPPED"),
                {"task": "e", "result": "skipped"}]
        self.assertEqual(ve.precision(rows), 0.5)
        self.assertIsNone(ve.precision([_row("a", "fail", "FAILED")]))

    def test_pass_at_1_is_a_mean_over_tasks(self) -> None:
        rows = [_row("a", "pass"), _row("a", "fail"), _row("b", "pass"), _row("b", "pass")]
        self.assertAlmostEqual(ve.pass_at_1(rows), 0.75)

    def test_tokens_per_solved(self) -> None:
        rows = [_row("a", "pass", tokens=3000), _row("b", "fail", tokens=1000)]
        self.assertEqual(ve.tokens_per_solved(rows), 4000)
        self.assertIsNone(ve.tokens_per_solved([_row("a", "fail")]))

    def test_false_claims(self) -> None:
        rows = [_row("a", "fail", flow={"repro": "fails"}), _row("b", "fail", flow={"regressions": ["t"]}),
                _row("c", "fail", "FAILED", flow={"repro": "fails"}), _row("d", "pass", flow={"repro": "passes"})]
        self.assertEqual(ve.false_claims(rows), 2)

    def test_compare_and_targets(self) -> None:
        verified = [_row("a", "pass", tokens=1500), _row("b", "pass", tokens=1500), _row("c", "fail", "FAILED", tokens=1500)]
        plain = [_row("a", "pass"), _row("b", "fail"), _row("c", "fail")]
        numbers = ve.compare(verified, plain)
        self.assertAlmostEqual(numbers["verified"]["precision"], 1.0)
        self.assertAlmostEqual(numbers["plain"]["precision"], 1 / 3)
        self.assertAlmostEqual(numbers["ratios"]["precision"], 3.0)
        self.assertAlmostEqual(numbers["ratios"]["tokens_per_solved"], 2250 / 3000)
        self.assertEqual(set(numbers["targets"].values()), {True})
        self.assertIn("| precision | 1.00 | 0.33 |", ve.render(numbers, 0.75))
        worse = ve.compare([_row("a", "fail")], plain)
        self.assertFalse(worse["targets"]["pass@1 >= plain"])
        self.assertIsNone(worse["targets"]["tokens per solved <= 1.6x plain"])

    def test_report_writes_the_summary(self) -> None:
        folder = Path(tempfile.mkdtemp())
        (folder / "verified.jsonl").write_text(json.dumps(_row("a", "pass")) + "\n", encoding="utf-8")
        (folder / "plain.jsonl").write_text(json.dumps(_row("a", "fail")) + "\n", encoding="utf-8")
        (folder / "reviewer.json").write_text(json.dumps([{"caught": True}, {"caught": False}]), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ve.main(["report", str(folder)]), 0)
        text = (folder / "summary.md").read_text(encoding="utf-8")
        self.assertIn("reviewer catches >= 50% of seeded defects: yes (50%)", text)


class ReviewerFixturesTest(unittest.TestCase):
    def test_every_planted_line_is_in_its_diff(self) -> None:
        fixtures = ve.load_fixtures()
        self.assertGreaterEqual(len(fixtures), 8)
        for fixture in fixtures:
            lines = ve.added_lines(fixture["diff"])
            defect = fixture["defect"]
            self.assertIn(defect["path"], lines, fixture["name"])
            self.assertIn(defect["line"], lines[defect["path"]], fixture["name"])
            self.assertTrue(fixture["task"] and defect["why"], fixture["name"])

    def test_catch_scoring(self) -> None:
        defect = {"path": "orders/repo.py", "line": 36}
        self.assertTrue(ve.caught(defect, [{"path": "orders/repo.py", "line": 35, "severity": "security"}]))
        self.assertTrue(ve.caught(defect, [{"path": "repo.py", "line": 38}]))
        self.assertFalse(ve.caught(defect, [{"path": "orders/repo.py", "line": 20}]))
        self.assertFalse(ve.caught(defect, [{"path": "orders/other.py", "line": 36}]))
        self.assertEqual(ve.catch_rate([{"caught": True}, {"caught": False}, {"caught": True}, {"caught": True}]), 0.75)
        self.assertIsNone(ve.catch_rate([]))

    def test_a_scripted_reviewer_through_the_flow_parser(self) -> None:
        from critique_bot.engine.flows import verified

        fixture = next(item for item in ve.load_fixtures() if item["name"] == "sql-injection")
        answer = ('Risk: risky\n```json\n{"risk": "risky", "comments": [{"path": "orders/repo.py", "line": 36, '
                  '"side": "new", "severity": "security", "body": "string-built SQL"}]}\n```')
        review = verified.review_from_text(answer)
        self.assertTrue(ve.caught(fixture["defect"], review.findings))
        self.assertEqual(len(review.blocking), 1)


if __name__ == "__main__":
    unittest.main()
