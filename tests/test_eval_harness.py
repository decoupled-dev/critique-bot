"""crit eval: metrics, grading, the grader audit, and runs with fake agents (no model needed)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

from critique_bot import eval_harness as ev
from critique_bot import run_stats


def _write(folder: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")


def _bug_task(root: Path, name: str = "fix-add") -> ev.Task:
    """A tiny Python task: add() subtracts; the hidden test checks it adds."""
    folder = root / name
    _write(folder, {
        "files/calc.py": "def add(a, b):\n    return a - b\n",
        "hidden/hidden_check.py": (
            "import sys, calc\n"
            "sys.exit(0 if calc.add(2, 3) == 5 else 1)\n"
        ),
        "solution/calc.py": "def add(a, b):\n    return a + b\n",
    })
    spec = {
        "prompt": "add() is wrong; fix it",
        "fixture": "files",
        "hidden": "hidden",
        "reference": "solution",
        "check": f"{sys.executable} hidden_check.py",
        "category": "bugfix",
        "tags": ["quick"],
    }
    (folder / "task.json").write_text(json.dumps(spec), encoding="utf-8")
    return ev.Task(name, folder, spec)


class _FixingRunner(ev.Runner):
    """Applies the right fix, and records whether the hidden files were visible."""

    name = "fake"

    def __init__(self, fix: bool = True) -> None:
        self.fix = fix
        self.saw_hidden = False

    def run(self, task: ev.Task, work: Path, out_dir: Path, env: dict[str, str], timeout: float) -> ev.RunOutput:
        self.saw_hidden = (work / "hidden_check.py").exists()
        if self.fix:
            (work / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return ev.RunOutput(0, "done", "fixed it", {"model_requests": 3, "prompt_tokens": 300, "completion_tokens": 40})


class MetricTests(unittest.TestCase):
    def test_pass_at_k_and_pass_hat_k(self) -> None:
        self.assertAlmostEqual(ev.pass_at_k(3, 1, 1), 1 / 3)
        self.assertAlmostEqual(ev.pass_at_k(3, 1, 3), 1.0)
        self.assertAlmostEqual(ev.pass_at_k(3, 0, 3), 0.0)
        self.assertAlmostEqual(ev.pass_hat_k(3, 3, 3), 1.0)
        self.assertAlmostEqual(ev.pass_hat_k(3, 2, 3), 0.0)
        self.assertAlmostEqual(ev.pass_hat_k(4, 3, 2), 3 / 6)
        self.assertAlmostEqual(ev.pass_at_k(4, 1, 2), 1 - 3 / 6)

    def test_aggregate_counts_only_real_trials(self) -> None:
        records = [
            {"task": "a", "run": 1, "result": "pass", "model_requests": 10, "malformed_calls": 1,
             "prompt_tokens": 1000, "cached_tokens": 800, "completion_tokens": 100,
             "edits": {"edit_file": {"calls": 4, "ok": 3}}},
            {"task": "a", "run": 2, "result": "fail", "model_requests": 10, "prompt_tokens": 1000,
             "cached_tokens": 600, "completion_tokens": 100},
            {"task": "b", "run": 1, "result": "pass", "model_requests": 5},
            {"task": "b", "run": 2, "result": "pass", "model_requests": 5},
            {"task": "c", "run": 1, "result": "skipped"},
            {"task": "d", "run": 1, "result": "error"},
        ]
        agg = ev.aggregate(records)
        suite = agg["suite"]
        self.assertEqual(suite["tasks"], 2)
        self.assertEqual(suite["k"], 2)
        self.assertAlmostEqual(suite["pass@1"], (0.5 + 1.0) / 2)
        self.assertAlmostEqual(suite["pass^2"], (0.0 + 1.0) / 2)
        self.assertAlmostEqual(suite["cache_hit_ratio"], 1400 / 2000)
        self.assertAlmostEqual(suite["malformed_per_turn"], 1 / 30)
        self.assertAlmostEqual(suite["edit_success_rate"], 0.75)
        self.assertAlmostEqual(suite["completion_tokens_per_solved"], 200 / 3)

    def test_bootstrap_is_paired_deterministic_and_brackets_the_mean(self) -> None:
        a = {f"t{i}": 0.0 for i in range(20)}
        b = {f"t{i}": (1.0 if i < 10 else 0.0) for i in range(20)}
        b["only-in-b"] = 1.0
        first = ev.bootstrap_diff(a, b)
        self.assertEqual(first, ev.bootstrap_diff(a, b))
        diff, low, high, n = first
        self.assertEqual(n, 20)
        self.assertAlmostEqual(diff, 0.5)
        self.assertLessEqual(low, diff)
        self.assertGreaterEqual(high, diff)
        self.assertGreater(low, 0.2)

    def test_summary_and_compare_render(self) -> None:
        a = [{"task": "x", "run": 1, "result": "fail"}, {"task": "y", "run": 1, "result": "pass"}]
        b = [{"task": "x", "run": 1, "result": "pass"}, {"task": "y", "run": 1, "result": "pass"}]
        text = ev.render_summary(b, {"runner": "crit"})
        self.assertIn("| pass@1 | 1.00 |", text)
        self.assertIn("- runner: crit", text)
        table = ev.render_compare(a, b, "base", "new")
        self.assertIn("+50.0 points", table)
        self.assertIn("| x | 0.00 | 1.00 | +100 |", table)


class GradingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_audit_accepts_a_good_task(self) -> None:
        self.assertEqual(ev.audit_task(_bug_task(self.root)), [])

    def test_audit_catches_a_check_that_always_passes_and_a_broken_reference(self) -> None:
        task = _bug_task(self.root)
        task.spec["check"] = "true"
        self.assertIn("check passes on the untouched workspace", ev.audit_task(task))
        task = _bug_task(self.root, "broken-ref")
        (task.folder / "solution" / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
        self.assertTrue(any("fails with the reference" in p for p in ev.audit_task(task)))

    def test_audit_of_answer_tasks(self) -> None:
        folder = self.root / "q"
        folder.mkdir()
        task = ev.Task("q", folder, {"prompt": "which?", "answer": "(?=.*Foo)(?=.*:42)", "reference_answer": "Foo.java:42"})
        self.assertEqual(ev.audit_task(task), [])
        task.spec["reference_answer"] = "Bar.java:1"
        self.assertIn("reference_answer does not match answer", ev.audit_task(task))
        task.spec["answer"] = ".*"
        self.assertIn("answer matches an empty reply", ev.audit_task(task))

    def test_reference_script_and_missing_requirements(self) -> None:
        task = _bug_task(self.root, "script-ref")
        (task.folder / "fix.sh").write_text("printf 'def add(a, b):\\n    return a + b\\n' > calc.py\n", encoding="utf-8")
        task.spec["reference"] = "fix.sh"
        self.assertEqual(ev.audit_task(task), [])
        self.assertEqual(ev.missing({"requires": ["$CRIT_EVAL_NOT_SET_ANYWHERE", "no-such-program-xyz"]}),
                         ["$CRIT_EVAL_NOT_SET_ANYWHERE", "no-such-program-xyz"])

    def test_trial_passes_with_a_fix_and_hides_the_check_from_the_agent(self) -> None:
        task = _bug_task(self.root)
        runner = _FixingRunner()
        record = ev.run_trial(task, 1, runner, self.root / "out")
        self.assertEqual(record["result"], "pass", record)
        self.assertFalse(runner.saw_hidden)
        self.assertEqual(record["model_requests"], 3)
        self.assertEqual(record["category"], "bugfix")
        self.assertIn("return a + b", (self.root / "out" / "fix-add" / "1" / "changes.diff").read_text())

    def test_trial_fails_without_a_fix_and_skips_when_requirements_are_missing(self) -> None:
        task = _bug_task(self.root)
        record = ev.run_trial(task, 1, _FixingRunner(fix=False), self.root / "out")
        self.assertEqual(record["result"], "fail")
        self.assertIn("check exit", record["why"])
        task.spec["requires"] = ["no-such-program-xyz"]
        self.assertEqual(ev.run_trial(task, 2, _FixingRunner(), self.root / "out")["result"], "skipped")

    def test_a_relative_suite_path_still_finds_reference_scripts(self) -> None:
        import os

        task = _bug_task(self.root, "rel")
        (task.folder / "fix.sh").write_text("printf 'def add(a, b):\\n    return a + b\\n' > calc.py\n", encoding="utf-8")
        spec = dict(task.spec, reference="fix.sh")
        (task.folder / "task.json").write_text(json.dumps(spec), encoding="utf-8")
        here = os.getcwd()
        os.chdir(self.root)
        try:
            loaded = ev.load_tasks(Path("."), only={"rel"})
        finally:
            os.chdir(here)
        self.assertTrue(loaded[0].folder.is_absolute())
        self.assertEqual(ev.audit_task(loaded[0]), [])

    def test_load_tasks_filters_by_name_and_tag(self) -> None:
        _bug_task(self.root, "one")
        other = _bug_task(self.root, "two")
        spec = dict(other.spec, tags=["slow"])
        (other.folder / "task.json").write_text(json.dumps(spec), encoding="utf-8")
        self.assertEqual([t.name for t in ev.load_tasks(self.root)], ["one", "two"])
        self.assertEqual([t.name for t in ev.load_tasks(self.root, tags={"quick"})], ["one"])
        self.assertEqual([t.name for t in ev.load_tasks(self.root, only={"two"})], ["two"])


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_crit_runner_reads_the_fix_and_the_counters_from_agent_json(self) -> None:
        fake = self.root / "fake_crit.py"
        fake.write_text(textwrap.dedent("""
            import json, os, sys
            args = sys.argv[1:]
            out = args[args.index("--output-dir") + 1]
            assert os.environ["CRIT_HOME"].endswith(".crit-home-1")
            open("calc.py", "w").write("def add(a, b):\\n    return a + b\\n")
            turns = [{"role": "user", "content": args[-1]}, {"role": "assistant", "content": "fixed add"}]
            stats = {"model_requests": 2, "prompt_tokens": 50, "malformed_calls": 0, "tool_calls": {"edit_file": 1}}
            json.dump({"turns": turns, "stats": stats}, open(os.path.join(out, "agent.json"), "w"))
        """), encoding="utf-8")
        runner = ev.CritRunner(f"{sys.executable} {fake}")
        record = ev.run_trial(_bug_task(self.root), 1, runner, self.root / "out")
        self.assertEqual(record["result"], "pass", record)
        self.assertEqual(record["tool_calls"], {"edit_file": 1})
        self.assertEqual(record["prompt_tokens"], 50)
        self.assertEqual(record["turns"], 1)

    def test_opencode_argv_and_json_events(self) -> None:
        runner = ev.OpencodeRunner(model="local/qwen3.6-27b")
        self.assertEqual(runner.argv("fix it"),
                         ["opencode", "run", "--format", "json", "--model", "local/qwen3.6-27b", "fix it"])
        output = "\n".join([
            "starting",
            json.dumps({"type": "tool", "tool": "edit", "part": {"tokens": {"input": 100, "output": 10}}}),
            json.dumps({"type": "text", "text": "Done: add fixed", "tokens": {"input": 50, "output": 5,
                                                                              "cache": {"read": 40}}}),
        ])
        final, stats = ev.parse_opencode_output(output)
        self.assertEqual(final, "Done: add fixed")
        self.assertEqual((stats["prompt_tokens"], stats["completion_tokens"], stats["cached_tokens"]), (150, 15, 40))
        self.assertEqual(stats["tool_calls"], {"edit": 1})
        self.assertEqual(ev.parse_opencode_output("plain answer\n")[0], "plain answer")


class RunStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        run_stats.reset()

    def test_counters(self) -> None:
        run_stats.add_usage(100, 10, 60)
        run_stats.add_usage(None, None)
        run_stats.tool_result("read_files", {"ok": True})
        run_stats.tool_result("edit_file", {"ok": False, "error": "no match"})
        run_stats.tool_result("run_command", {"ok": False, "denied": True})
        run_stats.tool_result("x", None, malformed=True)
        run_stats.edit("edit_file", ok=False, seconds=0.25, argument_chars=400)
        run_stats.count("compactions")
        run_stats.set_value("exit_reason", "COMPLETED")
        snap = run_stats.snapshot()
        self.assertEqual(snap["model_requests"], 2)
        self.assertEqual(snap["requests_without_usage"], 1)
        self.assertEqual((snap["prompt_tokens"], snap["completion_tokens"], snap["cached_tokens"]), (100, 10, 60))
        self.assertEqual(snap["tool_calls"], {"read_files": 1, "edit_file": 1, "run_command": 1})
        self.assertEqual((snap["tool_errors"], snap["edit_failures"], snap["malformed_calls"]), (1, 1, 1))
        self.assertEqual(snap["edits"]["edit_file"], {"calls": 1, "ok": 0, "seconds": 0.25, "out_tokens": 100})
        self.assertEqual(snap["compactions"], 1)
        self.assertEqual(snap["exit_reason"], "COMPLETED")
        run_stats.reset()
        self.assertEqual(run_stats.snapshot()["model_requests"], 0)


class CommandLineTests(unittest.TestCase):
    def test_crit_eval_is_a_subcommand(self) -> None:
        from critique_bot import cli

        self.assertIn("eval", cli.SUBCOMMANDS)

    def test_list_and_audit_on_a_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _bug_task(root)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ev.main(["list", "--suite", str(root)]), 0)
                self.assertEqual(ev.main(["audit", "--suite", str(root)]), 0)
                self.assertEqual(ev.main(["list", "--suite", str(root / "none")]), 2)


if __name__ == "__main__":
    unittest.main()
