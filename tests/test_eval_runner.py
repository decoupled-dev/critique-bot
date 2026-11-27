"""scripts/eval/run_eval.py (a wrapper for crit eval) with a fake crit: workspace, check, answer, stats, skip."""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("run_eval", ROOT / "scripts" / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_eval)

FAKE_CRIT = r'''
import json, sys
from pathlib import Path
args = sys.argv[1:]
out = Path(args[args.index("--output-dir") + 1])
prompt = args[-1]
if "fix" in prompt:
    Path("value.txt").write_text("42\n")
turns = [
    {"role": "user", "content": prompt},
    {"role": "assistant", "content": '{"output": [{"type": "function_call", "name": "read_files", "arguments": "{}"}]}'},
    {"role": "tool", "content": "value.txt: 41"},
    {"role": "assistant", "content": '{"output": [{"type": "function_call", "name": "edit_file", "arguments": "{}"}]}'},
    {"role": "tool", "content": "ok"},
    {"role": "assistant", "content": "Done: the answer is forty-two."},
]
out.mkdir(parents=True, exist_ok=True)
(out / "agent.json").write_text(json.dumps({"turns": turns}))
'''


class EvalRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.tasks = base / "tasks"
        fix = self.tasks / "fix-value"
        (fix / "files").mkdir(parents=True)
        (fix / "files" / "value.txt").write_text("41\n")
        (fix / "task.json").write_text(json.dumps({
            "prompt": "fix value.txt",
            "check": "grep -qx 42 value.txt",
            "answer": "forty-two",
        }))
        ask = self.tasks / "ask-only"
        ask.mkdir()
        (ask / "task.json").write_text(json.dumps({"prompt": "what is it", "answer": "seventeen"}))
        gone = self.tasks / "needs-tool"
        gone.mkdir()
        (gone / "task.json").write_text(json.dumps({"prompt": "x", "requires": ["no-such-program-xyz"]}))
        self.fake = base / "fake_crit.py"
        self.fake.write_text(FAKE_CRIT)
        self.out = base / "out"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self) -> tuple[int, list[dict]]:
        code = run_eval.main([
            "--tasks", str(self.tasks),
            "--crit", f"{shlex.quote(sys.executable)} {shlex.quote(str(self.fake))}",
            "--out", str(self.out),
        ])
        rows = [json.loads(line) for line in (self.out / "results.jsonl").read_text().splitlines()]
        return code, rows

    def test_records_pass_fail_skip_and_stats(self) -> None:
        code, rows = self._run()
        by_task = {row["task"]: row for row in rows}
        self.assertEqual(by_task["fix-value"]["result"], "pass", by_task["fix-value"])
        self.assertEqual(by_task["fix-value"]["turns"], 3)
        self.assertEqual(by_task["fix-value"]["tool_calls"], {"read_files": 1, "edit_file": 1})
        self.assertEqual(by_task["ask-only"]["result"], "fail")
        self.assertIn("answer", by_task["ask-only"]["why"])
        self.assertEqual(by_task["needs-tool"]["result"], "skipped")
        self.assertEqual(code, 1)
        self.assertIn("42", (self.out / "fix-value" / "1" / "changes.diff").read_text())
        summary = (self.out / "summary.md").read_text()
        self.assertIn("| tasks / trials | 2 / 2 |", summary)
        self.assertIn("| pass@1 | 0.50 |", summary)


class RealTaskSpecTests(unittest.TestCase):
    def test_every_task_spec_is_valid(self) -> None:
        from critique_bot import eval_harness

        tasks = eval_harness.load_tasks(eval_harness.DEFAULT_SUITE)
        self.assertGreaterEqual(len(tasks), 5)
        for task in tasks:
            name, folder, spec = task.name, task.folder, task.spec
            self.assertTrue(spec.get("prompt"), name)
            self.assertTrue(spec.get("check") or spec.get("answer"), f"{name} has no way to pass or fail")
            if not spec.get("workspace"):
                self.assertTrue((folder / spec.get("fixture", "files")).is_dir(), name)


if __name__ == "__main__":
    unittest.main()
