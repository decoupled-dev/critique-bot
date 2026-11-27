# SPDX-License-Identifier: Apache-2.0
"""Stage 7: task classes, the verified flow, its stop rules, plan triggers, the impact list, evidence.

The flow runs on scripted model sessions (no model server) over a small real Python
project, so the repro and the selected tests are real commands.
"""

from __future__ import annotations

import io
import json
import shlex
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from critique_bot import session_log
from critique_bot.engine import loop as engine_loop
from critique_bot.engine import modes, task_class
from critique_bot.engine.flows import checks, verified
from critique_bot.engine.flows import start as flow_start
from critique_bot.engine.impact import ImpactTracker, changed_signatures, definitions
from critique_bot.engine.parse import ToolCall
from critique_bot.tool_api import TaskState

PY = shlex.quote(sys.executable)


def _call(tool: str, **arguments: object) -> str:
    return "<tool_call>\n" + json.dumps({"tool": tool, "arguments": arguments}) + "\n</tool_call>"


class _Script:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []
        self.last_detail = None

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        return self.replies.pop(0) if self.replies else "COMPLETED"


class _Recorder(session_log.NullLog):
    """A session log that keeps the events (and checks their types like the real one)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, event_type: str, /, **fields):
        if event_type not in session_log.EVENT_TYPES:
            raise ValueError(f"unknown session event type: {event_type}")
        row = {"type": event_type, **fields}
        self.rows.append(row)
        return row

    def of(self, kind: str) -> list[dict]:
        return [row for row in self.rows if row["type"] == kind]


def _project() -> Path:
    root = Path(tempfile.mkdtemp(prefix="crit-flow-"))
    (root / "shop.py").write_text(textwrap.dedent("""\
        def price(amount):
            return amount * 2
        """), encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_shop.py").write_text(textwrap.dedent("""\
        import unittest

        from shop import price


        class PriceTest(unittest.TestCase):
            def test_double(self):
                self.assertEqual(price(2), 4)
        """), encoding="utf-8")
    return root


REPRO_FILE = textwrap.dedent("""\
    import unittest

    from shop import price


    class NegativeTest(unittest.TestCase):
        def test_negative_raises(self):
            with self.assertRaises(ValueError):
                price(-1)
    """)
REPRO_CMD = f"{PY} -m unittest discover -s .crit/repro -p test_negative.py"
REPRO_LINES = f"REPRO: {REPRO_CMD}\nEXPECTED: AssertionError: ValueError not raised"
GOOD_FIX = ("    return amount * 2\n", "    if amount < 0:\n        raise ValueError('negative')\n    return amount * 2\n")
SAFE_REVIEW = 'Risk: safe\nLooks right.\n```json\n{"risk": "safe", "comments": []}\n```'
BLOCKING_REVIEW = ('Risk: risky\n```json\n{"risk": "risky", "comments": [{"path": "shop.py", "line": 2, '
                   '"severity": "must-fix", "body": "zero is also invalid"}]}\n```')


def _run(root: Path, task: str, replies: list[str], settings: dict | None = None, **kwargs):
    session = _Script(replies)
    slog = _Recorder()
    options = {"flow": "verified", "plan_triggers": False, **(settings or {})}
    cache = root / ".crit" / "cache"
    with mock.patch("critique_bot.engine.ui._approve_prompt", lambda *_a, **_k: ("yes", "")):
        engine_loop.run_agent_loop(
            session, workspace=root, index_path=None, cache_dir=cache, first_task=task, max_rounds=40,
            max_result_chars=8000, read_message=lambda: None, emit=lambda _t: None, approve_mode="auto",
            ask_user=lambda _q: None, settings=options, session_log=slog, single_task=True, **kwargs)
    return session, slog


def _evidence(slog: _Recorder) -> dict:
    finished = slog.of("task_finished")
    return finished[-1].get("evidence") or {}


# --------------------------------------------------------------------------- classes


class TaskClassTest(unittest.TestCase):
    def test_rules(self) -> None:
        cases = {
            "fix the crash when the price is negative": "bugfix",
            "parse_total returns 3 instead of 4 for empty carts": "bugfix",
            "add a --verbose flag to the cli": "feature",
            "rename total to grand_total": "refactor",
            "what does price() return?": "question",
            "the build fails with a missing symbol, sort it out": "ops",
            "rebase this branch on main and commit": "ops",
        }
        for task, kind in cases.items():
            self.assertEqual(task_class.classify(task).kind, kind, task)

    def test_the_model_decides_only_when_rules_are_unclear(self) -> None:
        asked: list[str] = []

        def model(task: str) -> str:
            asked.append(task)
            return "Refactor."

        self.assertEqual(task_class.classify("fix the null crash", model).source, "rules")
        self.assertEqual(asked, [])
        found = task_class.classify("update the readme for the release", model)
        self.assertEqual((found.kind, found.source), ("refactor", "model"))
        self.assertEqual(task_class.classify("tidy things", lambda _t: "no idea").source, "unclear")
        self.assertEqual(task_class.classify("tidy things", lambda _t: 1 / 0).source, "unclear")

    def test_flow_for(self) -> None:
        bug = task_class.classify("fix the crash")
        self.assertEqual((task_class.flow_for(bug, "auto").flow, task_class.flow_for(bug, "auto").repro), ("verified", True))
        feature = task_class.classify("add a flag")
        self.assertEqual((task_class.flow_for(feature, "auto").flow, task_class.flow_for(feature, "auto").repro),
                         ("verified", False))
        self.assertEqual(task_class.flow_for(task_class.classify("rebase on main"), "auto").flow, "plain")
        self.assertEqual(task_class.flow_for(task_class.classify("what is x?"), "verified").flow, "plain")
        self.assertEqual(task_class.flow_for(bug, "plain").flow, "plain")
        unclear = task_class.classify("tidy things")
        self.assertEqual(task_class.flow_for(unclear, "auto").flow, "plain")
        self.assertEqual(task_class.flow_for(unclear, "verified").flow, "verified")

    def test_model_classifier_uses_a_fresh_session(self) -> None:
        made: list[_Script] = []

        def factory():
            made.append(_Script(["bugfix"]))
            return made[-1]

        self.assertEqual(task_class.model_classifier(factory)("x"), "bugfix")
        self.assertIn("one word", made[0].sent[0])

    def test_flow_setting_precedence(self) -> None:
        root = Path(tempfile.mkdtemp())
        agents = root / ".crit" / "agents"
        agents.mkdir(parents=True)
        (agents / "careful.md").write_text("---\nname: careful\ndescription: d\nflow: verified\n---\nbody\n",
                                           encoding="utf-8")
        self.assertEqual(flow_start.flow_setting({}, None, root)[:2], ("plain", "default"))
        self.assertEqual(flow_start.flow_setting({"_flow_default": "auto"}, None, root)[:2], ("auto", "default"))
        self.assertEqual(flow_start.flow_setting({"flow": "plain"}, "careful", root)[:2], ("verified", "agent"))
        self.assertEqual(flow_start.flow_setting({"_flow_flag": "plain"}, "careful", root)[:2], ("plain", "flag"))

    def test_agent_file_flow_is_checked(self) -> None:
        from critique_bot.extensions import agent_files

        with self.assertRaises(agent_files.AgentError):
            agent_files.parse_agent("---\nname: x\ndescription: d\nflow: sometimes\n---\n")
        self.assertEqual(agent_files.parse_agent("---\nname: x\ndescription: d\nflow: Verified\n---\n").flow, "verified")
        verifier = agent_files.find_agent("verifier", Path(tempfile.mkdtemp()))
        self.assertIsNotNone(verifier)
        self.assertTrue(verifier.read_only)
        self.assertEqual(verifier.output, "review")


# --------------------------------------------------------------------------- checks


class ChecksTest(unittest.TestCase):
    def test_repro_reply(self) -> None:
        command, expected = checks.parse_repro_reply("Done.\nREPRO: `pytest -q t.py`\nEXPECTED: ValueError: bad price")
        self.assertEqual(command, "pytest -q t.py")
        self.assertEqual((expected.kind, expected.message), ("ValueError", "bad price"))
        self.assertEqual(checks.parse_repro_reply("nothing here"), ("", None))

    def test_wrong_reason_is_not_a_repro(self) -> None:
        expected = checks.parse_expected("ValueError: price must be positive")
        wrong = checks.judge_repro(expected, 1, "Traceback\nModuleNotFoundError: No module named 'shop'")
        self.assertFalse(wrong.accepted)
        self.assertIn("wrong reason", wrong.reason)
        self.assertFalse(checks.judge_repro(expected, 0, "OK").accepted)
        self.assertIn("passed", checks.judge_repro(expected, 0, "OK").reason)
        self.assertFalse(checks.judge_repro(expected, 1, "ValueError: quantity too large").accepted)
        self.assertTrue(checks.judge_repro(expected, 1, "ValueError: price must be positive").accepted)
        self.assertFalse(checks.judge_repro(None, 1, "ValueError").accepted)
        # no type stated: a setup error is still the wrong reason
        loose = checks.parse_expected("the total is 3, not 4")
        self.assertFalse(checks.judge_repro(loose, 1, "SyntaxError: invalid syntax").accepted)
        self.assertTrue(checks.judge_repro(loose, 1, "AssertionError: total 3 != 4").accepted)

    def test_failing_counts(self) -> None:
        self.assertEqual(checks.failing_count("=== 2 failed, 3 passed in 0.12s ===", False), 2)
        self.assertEqual(checks.failing_count("1 failed, 1 error in 0.2s", False), 2)
        self.assertEqual(checks.failing_count("FAILED (failures=2, errors=1)", False), 3)
        self.assertEqual(checks.failing_count("Tests run: 9, Failures: 1, Errors: 1", False), 2)
        self.assertEqual(checks.failing_count("--- FAIL: TestA\n--- FAIL: TestB\nFAIL", False), 2)
        self.assertEqual(checks.failing_count("test result: FAILED. 3 passed; 2 failed", False), 2)
        self.assertEqual(checks.failing_count("whatever", False), 1)
        self.assertEqual(checks.failing_count("Ran 3 tests\nOK", True), 0)
        self.assertTrue(checks.looks_like_tests("python -m pytest -q"))
        self.assertTrue(checks.looks_like_tests("./gradlew :app:testDebugUnitTest"))
        self.assertFalse(checks.looks_like_tests("ls -la"))

    def test_selection(self) -> None:
        root = _project()
        (root / "pkg").mkdir()
        (root / "pkg" / "Cart.java").write_text("class Cart {}", encoding="utf-8")
        (root / "pkg" / "CartTest.java").write_text("class CartTest {}", encoding="utf-8")
        (root / "go").mkdir()
        (root / "go" / "thumb.go").write_text("package go", encoding="utf-8")
        (root / "go" / "thumb_test.go").write_text("package go", encoding="utf-8")
        chosen = checks.select_tests(root, ["shop.py", "pkg/Cart.java", "go/thumb.go", ".crit/repro/x.py"],
                                     check_command="make test")
        labels = [target.source for target in chosen.targets]
        self.assertEqual(labels[0], "check")
        commands = " | ".join(target.command for target in chosen.targets)
        self.assertIn("unittest discover -s tests -p test_shop.py", commands)
        self.assertIn("go test ./go", commands)
        self.assertEqual(chosen.unrun, ["pkg/CartTest.java"])
        (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        self.assertIn("pytest -q tests/test_shop.py", checks.select_tests(root, ["shop.py"], check_command=None).targets[0].command)
        graph = checks.select_tests(root, ["shop.py"], check_command=None,
                                    nav=lambda rel: "1 file(s) import shop.py:\nit/test_flow_it.py:3 (shop)")
        self.assertIn("it/test_flow_it.py", graph.targets[0].files)
        self.assertTrue(checks.is_test_path("src/test/java/a/FooTest.java"))
        self.assertTrue(checks.is_test_path(".crit/repro/x.sh"))
        self.assertFalse(checks.is_test_path("src/main/Foo.java"))

    def test_regressions(self) -> None:
        target = checks.TestTarget("t", "cmd", "files")
        before = {"t": checks.TestResult(target, True, 0), "u": checks.TestResult(target, False, 1)}
        after = {"t": checks.TestResult(target, False, 1), "u": checks.TestResult(target, False, 1)}
        self.assertEqual(checks.regressions(before, after), ["t"])

    def test_review_parsing(self) -> None:
        review = verified.review_from_text(BLOCKING_REVIEW)
        self.assertEqual(review.risk, "risky")
        self.assertEqual(len(review.blocking), 1)
        notes = verified.review_from_text('```json\n{"risk": "moderate", "comments": [{"severity": "test", "body": "x"},'
                                          ' {"severity": "nit", "body": "y"}]}\n```')
        self.assertEqual((len(notes.findings), len(notes.blocking)), (1, 0))
        self.assertTrue(verified.review_from_text("no json").skipped)

    def test_prompt_copies_match(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        self.assertEqual((repo / "prompts" / "flows" / "verified.txt").read_text(encoding="utf-8"),
                         (repo / "src" / "critique_bot" / "prompts" / "flows" / "verified.txt").read_text(encoding="utf-8"))
        sections = verified.flow_sections()
        for key in ("REPRODUCE", "REPRO_NEEDED", "REPRO_REJECTED", "REPRO_ACCEPTED", "REPRO_UNTRUSTED", "FIX", "REPAIR",
                    "NEW_ATTEMPT", "REVIEW_BLOCKING", "REVIEW_TASK", "REVIEW_NUDGE", "PLAN_TRIGGERED", "REPRODUCE_BUDGET"):
            self.assertIn(key, sections)


# --------------------------------------------------------------------------- the flow


class VerifiedFlowTest(unittest.TestCase):
    def _reviewer(self, *answers: str):
        seen: list[verified.ReviewRequest] = []
        queue = list(answers)

        def review(request: verified.ReviewRequest) -> str:
            seen.append(request)
            return queue.pop(0) if queue else SAFE_REVIEW

        return review, seen

    def test_bugfix_runs_every_state(self) -> None:
        root = _project()
        review, seen = self._reviewer(SAFE_REVIEW)
        session, slog = _run(root, "fix: price() accepts negative amounts, it should raise ValueError", [
            _call("write_files", path=".crit/repro/test_negative.py", content=REPRO_FILE),
            REPRO_LINES,
            _call("edit_file", path="shop.py", old_string=GOOD_FIX[0], new_string=GOOD_FIX[1]),
            "COMPLETED",
        ], {"_reviewer": review})
        self.assertIn("REPRODUCE", session.sent[0])
        states = [row["state"] for row in slog.of("flow_state")]
        self.assertEqual(states[0], "REPRODUCE")
        for state in ("FIX", "TEST", "REVIEW", "DONE"):
            self.assertIn(state, states)
        repro = slog.of("repro_result")
        self.assertTrue(repro[0]["accepted"])
        self.assertTrue(repro[-1]["passed"])
        tests = slog.of("test_run")[-1]
        self.assertTrue(tests["before"]["tests/test_shop.py"]["ok"])
        self.assertTrue(tests["after"]["tests/test_shop.py"]["ok"])
        evidence = _evidence(slog)
        self.assertEqual(evidence["repro"], "passes")
        self.assertEqual((evidence["failing_before"], evidence["failing_after"]), (0, 0))
        self.assertEqual(evidence["review"]["risk"], "safe")
        self.assertTrue(any(line.startswith("repro: ✓") for line in evidence["checklist"]))
        self.assertEqual(slog.of("task_finished")[-1]["code"], "COMPLETED")
        # the reviewer saw the task, the diff and the evidence
        self.assertIn("raise ValueError", seen[0].diff)
        self.assertNotIn(".crit/repro", seen[0].diff)
        self.assertIn("Repro:", seen[0].evidence)

    def test_source_edits_wait_for_the_fix_step(self) -> None:
        root = _project()
        review, _seen = self._reviewer()
        session, _slog = _run(root, "fix: price() accepts negative amounts, it should raise ValueError", [
            _call("edit_file", path="shop.py", old_string=GOOD_FIX[0], new_string=GOOD_FIX[1]),
            _call("write_files", path=".crit/repro/test_negative.py", content=REPRO_FILE),
            REPRO_LINES,
            _call("edit_file", path="shop.py", old_string=GOOD_FIX[0], new_string=GOOD_FIX[1]),
            "COMPLETED",
        ], {"_reviewer": review})
        self.assertIn("the REPRODUCE step writes only the repro", session.sent[1])
        self.assertIn("raise ValueError", (root / "shop.py").read_text(encoding="utf-8"))

    def test_a_repro_that_fails_for_the_wrong_reason_is_untrusted(self) -> None:
        root = _project()
        review, _seen = self._reviewer()
        broken = f"REPRO: {PY} -c \"import shopp\"\nEXPECTED: ValueError: negative amount"
        session, slog = _run(root, "fix: price() accepts negative amounts, it should raise ValueError", [
            broken, broken,
            _call("edit_file", path="shop.py", old_string=GOOD_FIX[0], new_string=GOOD_FIX[1]),
            "COMPLETED",
        ], {"_reviewer": review})
        self.assertIn("wrong reason", session.sent[1])
        self.assertIn("No trusted repro", session.sent[2])
        evidence = _evidence(slog)
        self.assertEqual(evidence["repro"], "untrusted")
        self.assertTrue(any("no trusted repro" in item for item in evidence["caveats"]))
        self.assertTrue(any(line.startswith("repro: untrusted") for line in evidence["checklist"]))
        self.assertEqual(slog.of("task_finished")[-1]["code"], "COMPLETED")

    def test_a_repro_that_passes_now_is_rejected(self) -> None:
        root = _project()
        review, _seen = self._reviewer()
        session, _slog = _run(root, "fix: price() accepts negative amounts", [
            f"REPRO: {PY} -c \"print(1)\"\nEXPECTED: ValueError: negative",
            "FAILED: cannot reproduce",
            "COMPLETED",
        ], {"_reviewer": review})
        self.assertIn("passed on the unmodified code", session.sent[1])

    def test_a_regression_goes_back_to_fix(self) -> None:
        root = _project()
        review, _seen = self._reviewer()
        breaking = ("    return amount * 2\n", "    if amount < 0:\n        raise ValueError('negative')\n    return amount * 3\n")
        session, slog = _run(root, "fix: price() accepts negative amounts, it should raise ValueError", [
            _call("write_files", path=".crit/repro/test_negative.py", content=REPRO_FILE),
            REPRO_LINES,
            _call("edit_file", path="shop.py", old_string=breaking[0], new_string=breaking[1]),
            "COMPLETED",
            _call("edit_file", path="shop.py", old_string="amount * 3", new_string="amount * 2"),
            "COMPLETED",
        ], {"_reviewer": review})
        repair = next(text for text in session.sent if "Not done yet" in text)
        self.assertIn("passed before now fail", repair)
        self.assertIn("tests/test_shop.py", repair)
        runs = slog.of("test_run")
        self.assertEqual(runs[0]["after"]["tests/test_shop.py"]["ok"], False)
        self.assertEqual(runs[-1]["after"]["tests/test_shop.py"]["ok"], True)
        self.assertEqual(_evidence(slog)["regressions"], [])
        self.assertEqual(slog.of("task_finished")[-1]["code"], "COMPLETED")
        # the disk holds the task's version after the before-runs swapped the originals in and out
        self.assertIn("raise ValueError", (root / "shop.py").read_text(encoding="utf-8"))
        self.assertTrue((root / ".crit" / "repro" / "test_negative.py").is_file())

    def test_blocking_review_goes_back_to_fix_once(self) -> None:
        root = _project()
        review, seen = self._reviewer(BLOCKING_REVIEW, BLOCKING_REVIEW)
        session, slog = _run(root, "add a guard: price() must reject negative amounts", [
            _call("edit_file", path="shop.py", old_string=GOOD_FIX[0], new_string=GOOD_FIX[1]),
            "COMPLETED",
            _call("edit_file", path="shop.py", old_string="amount < 0", new_string="amount <= 0"),
            "COMPLETED",
        ], {"_reviewer": review})
        self.assertEqual(slog.of("flow_state")[0]["state"], "FIX")  # a feature: no repro step
        self.assertTrue(any("blocking problems" in text and "zero is also invalid" in text for text in session.sent))
        self.assertEqual(len(seen), 2)
        evidence = _evidence(slog)
        self.assertTrue(any("still reports 1 blocking" in item for item in evidence["caveats"]))
        self.assertEqual(len(slog.of("review_result")), 2)

    def test_reviewer_runs_in_a_fresh_read_only_chat(self) -> None:
        root = _project()
        reviewers: list[_Script] = []

        def factory():
            reviewers.append(_Script([_call("read_files", path="shop.py"), SAFE_REVIEW]))
            return reviewers[-1]

        session, slog = _run(root, "add a guard: price() must reject negative amounts", [
            "I will add the guard. SECRET-REASONING about my approach.\n"
            + _call("edit_file", path="shop.py", old_string=GOOD_FIX[0], new_string=GOOD_FIX[1]),
            "COMPLETED",
        ], reviewer_factory=factory)
        self.assertEqual(len(reviewers), 1)
        first = reviewers[0].sent[0]
        self.assertIn("VERIFY TASK", first)
        self.assertIn("ROLE: Verifier", first)
        self.assertIn("raise ValueError", first)
        self.assertNotIn("SECRET-REASONING", "\n".join(reviewers[0].sent))
        self.assertEqual(_evidence(slog)["review"]["risk"], "safe")

    def test_no_review_without_a_second_chat(self) -> None:
        root = _project()
        _session, slog = _run(root, "add a guard: price() must reject negative amounts", [
            _call("edit_file", path="shop.py", old_string=GOOD_FIX[0], new_string=GOOD_FIX[1]),
            "COMPLETED",
        ])
        self.assertIn("no second chat", _evidence(slog)["review"]["skipped"])

    def test_repair_rounds_then_a_new_attempt_with_rejected_patches(self) -> None:
        root = _project()
        review, _seen = self._reviewer()
        wrong = _call("edit_file", path="shop.py", old_string="    return amount * 2\n",
                      new_string="    # still wrong\n    return amount * 2\n")
        replies = [_call("write_files", path=".crit/repro/test_negative.py", content=REPRO_FILE), REPRO_LINES, wrong]
        replies += ["COMPLETED"] * 4
        replies += [_call("edit_file", path="shop.py", old_string="    # still wrong\n    return amount * 2\n",
                          new_string=GOOD_FIX[1]), "COMPLETED"]
        session, slog = _run(root, "fix: price() accepts negative amounts, it should raise ValueError", replies,
                             {"_reviewer": review})
        repairs = [text for text in session.sent if "Not done yet: the repro still fails" in text]
        self.assertEqual(len(repairs), 3)
        attempt = next(text for text in session.sent if "Start attempt 2" in text)
        self.assertIn("rejected patch 1", attempt)
        self.assertIn("still wrong", attempt)
        evidence = _evidence(slog)
        self.assertEqual((evidence["attempts"], evidence["rejected_patches"]), (2, 1))
        self.assertEqual(slog.of("task_finished")[-1]["code"], "COMPLETED")

    def test_attempts_run_out(self) -> None:
        root = _project()
        replies = [_call("write_files", path=".crit/repro/test_negative.py", content=REPRO_FILE), REPRO_LINES,
                   _call("write_files", path="note.py", content="x = 1\n")]
        replies += ["COMPLETED"] * 8
        _session, slog = _run(root, "fix: price() accepts negative amounts, it should raise ValueError", replies,
                              {"_reviewer": self._reviewer()[0]})
        self.assertEqual(slog.of("task_finished")[-1]["code"], "FAILED")
        self.assertIn("2 fix attempts", _evidence(slog)["stop_reason"])

    def test_plan_trigger_after_two_failed_rounds_in_auto(self) -> None:
        root = _project()
        replies = [_call("write_files", path=".crit/repro/test_negative.py", content=REPRO_FILE), REPRO_LINES,
                   _call("write_files", path="note.py", content="x = 1\n"), "COMPLETED", "COMPLETED",
                   "Plan: 1. edit shop.py price() to raise ValueError on negative amounts. 2. run the repro.",
                   _call("edit_file", path="shop.py", old_string=GOOD_FIX[0], new_string=GOOD_FIX[1]), "COMPLETED"]
        session, slog = _run(root, "fix: price() accepts negative amounts, it should raise ValueError", replies,
                             {"_reviewer": self._reviewer()[0], "plan_triggers": True})
        triggered = [row for row in slog.of("mode_changed") if row.get("trigger")]
        self.assertEqual(len(triggered), 1)
        self.assertIn("failed its checks twice", triggered[0]["trigger"])
        self.assertTrue(any("PLAN MODE" in text or "plan" in text.lower() for text in session.sent[4:6]))
        self.assertEqual(slog.of("task_finished")[-1]["code"], "COMPLETED")

    def test_no_progress_stops_fix(self) -> None:
        root = _project()
        replies = [_call("write_files", path=".crit/repro/test_negative.py", content=REPRO_FILE), REPRO_LINES]
        replies += [_call("read_files", path="shop.py"), _call("list_files", path="."), _call("read_files", path="tests/test_shop.py")]
        replies += ["COMPLETED"] * 3
        _session, slog = _run(root, "fix: price() accepts negative amounts, it should raise ValueError", replies,
                              {"flow_no_progress_turns": 3, "_reviewer": self._reviewer()[0]})
        finished = slog.of("task_finished")[-1]
        self.assertEqual(finished["code"], "STOPPED")
        self.assertIn("no progress", finished["evidence"]["stop_reason"])

    def test_token_budget_per_state(self) -> None:
        root = _project()
        _session, slog = _run(root, "add a guard: price() must reject negative amounts", [
            _call("read_files", path="shop.py"), "COMPLETED"], {"flow_budgets": {"fix": 10}})
        finished = slog.of("task_finished")[-1]
        self.assertEqual(finished["code"], "STOPPED")
        self.assertIn("token budget", finished["evidence"]["stop_reason"])

    def test_plain_tasks_have_no_flow(self) -> None:
        root = _project()
        _session, slog = _run(root, "what does price() return?", ["It doubles the amount."], {"flow": "auto"})
        self.assertEqual(slog.of("flow_state"), [])
        self.assertIsNone(slog.of("task_finished")[-1].get("evidence"))
        _session, slog = _run(root, "fix: price() accepts negative amounts", [
            _call("edit_file", path="shop.py", old_string=GOOD_FIX[0], new_string=GOOD_FIX[1]), "COMPLETED"],
            {"flow": "plain"})
        self.assertEqual(slog.of("flow_state"), [])

    def test_the_task_classifier_is_pluggable(self) -> None:
        root = _project()
        asked: list[str] = []
        _session, slog = _run(root, "tidy things", ["Nothing to do. COMPLETED"],
                              {"flow": "auto", "_task_classifier": lambda task: asked.append(task) or "question"})
        self.assertEqual(asked, ["tidy things"])
        self.assertEqual(slog.of("flow_state"), [])


# --------------------------------------------------------------------------- plan triggers


MULTI_MODULE = [
    "migrate every service from the old logger to structlog",
    "rename UserId to AccountId across all modules",
    "refactor the payment code across the packages to use the new client",
    "port the config loading to pydantic throughout the codebase",
    "replace requests with httpx in all services",
    "update a.py, b.py, c.py, d.py, e.py and f.py to the new API",
    "move the cache helpers into a shared module for every package",
    "migrate the database layer to SQLAlchemy 2 across the project",
    "rename the field total to grand_total in api.py, models.py, views.py, forms.py, tests.py and admin.py",
    "refactor all modules to stop using the global settings object",
]
SINGLE_FILE = [
    "fix the off-by-one in pager.py",
    "add a --verbose flag to cli.py",
    "rename the local variable x to count in parse()",
    "price() should raise on negative amounts",
    "add a test for the empty cart",
    "fix the typo in the README",
    "make the timeout configurable in client.py",
    "the total is wrong when the cart is empty",
    "add logging to the retry loop",
    "fix the crash in Thumbnail.kt when the name is null",
]


class PlanTriggerTest(unittest.TestCase):
    def test_trigger_rates(self) -> None:
        fired = sum(bool(modes.plan_trigger(task, files=modes.estimate_files(task))) for task in MULTI_MODULE)
        quiet = sum(bool(modes.plan_trigger(task, files=modes.estimate_files(task))) for task in SINGLE_FILE)
        self.assertGreaterEqual(fired / len(MULTI_MODULE), 0.8)
        self.assertLessEqual(quiet / len(SINGLE_FILE), 0.1)

    def test_estimate_uses_refs(self) -> None:
        refs = {"parse_total": [f"m{i}.py" for i in range(7)]}
        self.assertEqual(modes.estimate_files("change `parse_total` to take a currency",
                                              refs=lambda name: refs.get(name, [])), 7)

    def test_a_start_trigger_in_auto_mode_enters_plan_mode(self) -> None:
        root = _project()
        session, slog = _run(root, "rename price to unit_price across all modules", [
            "Plan: rename price() in shop.py to unit_price() and update every caller and test that uses it.",
            _call("edit_file", path="shop.py", old_string="def price(", new_string="def unit_price("),
            "COMPLETED",
        ], {"plan_triggers": True, "_reviewer": lambda _r: SAFE_REVIEW})
        self.assertIn("PLAN MODE", session.sent[0])
        self.assertTrue(any(row.get("trigger") for row in slog.of("mode_changed")))
        self.assertIn("unit_price", (root / "shop.py").read_text(encoding="utf-8"))

    def test_outside_auto_the_user_is_asked(self) -> None:
        run = mock.MagicMock()
        run.read_only = False
        run.answer = None
        run._approve_mode.return_value = "ask"
        run.sections = {"PLAN_MODE": "PLAN MODE."}
        run.payload = ""
        with mock.patch("critique_bot.engine.ui._ask_user", return_value="no") as asked:
            self.assertFalse(modes.Modes._suggest_plan(run, "many files"))
        asked.assert_called_once()
        with mock.patch("critique_bot.engine.ui._ask_user", return_value="yes"):
            self.assertTrue(modes.Modes._suggest_plan(run, "many files"))
        self.assertEqual(run.approve_mode, "plan")


# --------------------------------------------------------------------------- impact


class ImpactTest(unittest.TestCase):
    def _tracker(self, root: Path, path: str, old: str, new: str) -> tuple[ImpactTracker, TaskState]:
        state = TaskState(task="t")
        tracker = ImpactTracker(root)
        ctx = mock.MagicMock()
        ctx.workspace = root
        call = ToolCall("edit_file", {"path": path, "old_string": old, "new_string": new})
        with mock.patch("critique_bot.tool_api._paths_of", return_value=[root / path]):
            snapshot = tracker.before(call, ctx)
        target = root / path
        target.write_text(target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        tracker.after(snapshot, state)
        return tracker, state

    def test_python(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "cart.py").write_text("def total(items):\n    return sum(items)\n\n\ndef keep(x):\n    return x\n",
                                      encoding="utf-8")
        (root / "a.py").write_text("from cart import total\nprint(total([1]))\n", encoding="utf-8")
        (root / "b.py").write_text("import cart\n\nx = cart.total([2])\ny = totally = 1\n", encoding="utf-8")
        tracker, state = self._tracker(root, "cart.py", "def total(items):", "def total(items, tax):")
        self.assertEqual(len(tracker.items), 1)
        item = tracker.items[0]
        self.assertEqual(item.name, "total")
        self.assertEqual(sorted(item.files), ["a.py", "b.py"])
        self.assertIn("check 3 call site(s) of `total()` in: ", item.todo)
        self.assertEqual(state.todos[-1]["content"], item.todo)
        self.assertEqual(len(tracker.unchecked(state)), 1)
        state.todos[-1]["status"] = "completed"
        self.assertEqual(tracker.unchecked(state), [])

    def test_java_and_kotlin(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "Fan.java").write_text("public class Fan {\n    public void setSpeed(int rpm) {\n    }\n}\n", encoding="utf-8")
        (root / "Hal.kt").write_text("fun apply(fan: Fan) {\n    fan.setSpeed(3)\n}\n", encoding="utf-8")
        (root / "Ui.java").write_text("class Ui {\n  void go(Fan f) { f.setSpeed(1); }\n}\n", encoding="utf-8")
        tracker, _state = self._tracker(root, "Fan.java", "public void setSpeed(int rpm) {",
                                        "public void setSpeed(int rpm, boolean quiet) {")
        self.assertEqual(sorted(tracker.items[0].files), ["Hal.kt", "Ui.java"])

        (root / "Seat.kt").write_text("class Seat {\n    fun heat(level: Int) {\n    }\n}\n", encoding="utf-8")
        (root / "Panel.kt").write_text("fun show(s: Seat) {\n    s.heat(2)\n}\n", encoding="utf-8")
        tracker, _state = self._tracker(root, "Seat.kt", "fun heat(level: Int) {", "fun heat(level: Int, zone: Int) {")
        self.assertEqual([item.files for item in tracker.items], [["Panel.kt"]])

    def test_body_changes_are_not_impact(self) -> None:
        self.assertEqual(changed_signatures(definitions("def f(a):\n    return a\n", "m.py"),
                                            definitions("def f(a):\n    return a + 1\n", "m.py")), [])
        gone = changed_signatures(definitions("def f(a):\n    pass\n", "m.py"), definitions("x = 1\n", "m.py"))
        self.assertEqual(gone, [("f", "def f(a)", "")])

    def test_the_flow_lists_unchecked_call_sites(self) -> None:
        root = _project()
        (root / "cart.py").write_text("from shop import price\n\nTOTAL = price(3)\n", encoding="utf-8")
        _session, slog = _run(root, "add a currency argument to price()", [
            _call("edit_file", path="shop.py", old_string="def price(amount):", new_string="def price(amount, currency='EUR'):"),
            "COMPLETED",
        ], {"_reviewer": lambda _r: SAFE_REVIEW})
        evidence = _evidence(slog)
        self.assertEqual(len(evidence["impact_unchecked"]), 1)
        self.assertIn("cart.py:3", evidence["impact_unchecked"][0])
        self.assertTrue(any(line.startswith("impact: 1 unchecked") for line in evidence["checklist"]))


# --------------------------------------------------------------------------- evidence out


class EvidenceOutTest(unittest.TestCase):
    def test_checklist(self) -> None:
        evidence = verified.Evidence(task_class="bugfix", repro="fails", repro_command="pytest t.py",
                                     tests_before={"a": {"failing": 0}}, tests_after={"a": {"failing": 2}},
                                     regressions=["a"], review={"risk": "risky", "findings": 2, "blocking": 1})
        lines = evidence.checklist()
        self.assertTrue(lines[0].startswith("repro: ✗"))
        self.assertIn("failing 0 → 2", lines[1])
        self.assertIn("regressions: a", lines[1])
        self.assertIn("1 blocking, 1 other", lines[2])
        self.assertEqual(evidence.as_dict()["failing_after"], 2)

    def test_crit_run_json_ends_with_the_final_object(self) -> None:
        from critique_bot import api_client

        events = [{"type": "notice", "seq": 1},
                  {"type": "task_finished", "seq": 2, "code": "COMPLETED", "changed": ["shop.py"],
                   "evidence": {"repro": "passes", "checklist": ["repro: ✓"]}},
                  {"type": "session_closed", "seq": 3}]
        out = io.StringIO()
        self.assertEqual(api_client.run_headless(iter(events), json_lines=True, out=out), 0)
        final = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(final, {"type": "final", "code": "COMPLETED", "changed": ["shop.py"],
                                 "evidence": {"repro": "passes", "checklist": ["repro: ✓"]}})


if __name__ == "__main__":
    unittest.main()
