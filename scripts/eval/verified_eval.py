#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Stage 7 eval: the verified flow against plain runs on the suite's bugfix tasks, and the
reviewer on seeded-defect fixtures. Needs a model; the math is unit-tested without one
(tests/test_verified_eval.py).

    python3 scripts/eval/verified_eval.py run --config ~/.crit/qwen.json --runs 3 --out plan/results/07
    python3 scripts/eval/verified_eval.py reviewer --config ~/.crit/qwen.json --out plan/results/07
    python3 scripts/eval/verified_eval.py report plan/results/07

``run`` runs every task whose category is ``bugfix`` with ``"flow": "verified"`` and with
``"flow": "plain"`` (the same CritRunner as ``crit eval``), N runs each, and writes
``verified.jsonl``, ``plain.jsonl`` and ``summary.md``. The numbers (plan/07 targets):

- precision: of the trials where crit claimed the fix (it finished COMPLETED), the share
  whose hidden tests pass; verified should be >= 1.5x plain;
- pass@1 (hidden tests pass); verified should be >= plain;
- tokens per solved task (prompt + completion); verified should be <= 1.6x plain;
- false claims: trials where the verified flow finished COMPLETED while its own repro or
  selected tests still failed (``flow`` in the run's stats); should be 0.

``reviewer`` sends each fixture under tests/verifier_fixtures/ (a diff with one planted bug)
to the verifier prompt on the model and counts a catch when a finding names the planted
file within 3 lines of the planted line (any severity); the target is >= 50%.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = ROOT / "tests" / "verifier_fixtures"
CLAIMED_CODES = frozenset({"COMPLETED", "FINISHED", "DONE"})
TARGETS = {"precision_ratio": 1.5, "tokens_ratio": 1.6, "reviewer_catch_rate": 0.5}
LINE_SLACK = 3


# --------------------------------------------------------------------------- the math


def claimed(record: dict[str, Any]) -> bool:
    """crit said it fixed it: the run finished with a success code."""
    return str(record.get("exit_reason") or "").upper() in CLAIMED_CODES and record.get("exit") in (0, None)


def ran(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in records if row.get("result") in {"pass", "fail"}]


def precision(records: Iterable[dict[str, Any]]) -> float | None:
    """Claimed fixes that pass the hidden tests / claimed fixes. None when nothing was claimed."""
    said = [row for row in ran(records) if claimed(row)]
    if not said:
        return None
    return sum(1 for row in said if row["result"] == "pass") / len(said)


def pass_at_1(records: Iterable[dict[str, Any]]) -> float | None:
    """The mean over tasks of each task's pass rate."""
    by_task: dict[str, list[bool]] = {}
    for row in ran(records):
        by_task.setdefault(str(row["task"]), []).append(row["result"] == "pass")
    if not by_task:
        return None
    return statistics.mean(sum(rows) / len(rows) for rows in by_task.values())


def tokens(record: dict[str, Any]) -> int:
    return int(record.get("prompt_tokens") or 0) + int(record.get("completion_tokens") or 0)


def tokens_per_solved(records: Iterable[dict[str, Any]]) -> float | None:
    rows = ran(records)
    solved = sum(1 for row in rows if row["result"] == "pass")
    return sum(tokens(row) for row in rows) / solved if solved else None


def false_claims(records: Iterable[dict[str, Any]]) -> int:
    """Verified runs that claimed a fix while their own repro or selected tests still failed."""
    count = 0
    for row in ran(records):
        flow = row.get("flow") if isinstance(row.get("flow"), dict) else None
        if not flow or not claimed(row):
            continue
        if flow.get("repro") == "fails" or flow.get("regressions"):
            count += 1
    return count


def _ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def compare(verified: list[dict[str, Any]], plain: list[dict[str, Any]]) -> dict[str, Any]:
    """The plan/07 numbers for the two modes, and whether each target holds (None: not measurable)."""
    numbers = {
        "verified": {"precision": precision(verified), "pass@1": pass_at_1(verified),
                     "tokens_per_solved": tokens_per_solved(verified), "trials": len(ran(verified)),
                     "false_claims": false_claims(verified)},
        "plain": {"precision": precision(plain), "pass@1": pass_at_1(plain),
                  "tokens_per_solved": tokens_per_solved(plain), "trials": len(ran(plain))},
    }
    precision_ratio = _ratio(numbers["verified"]["precision"], numbers["plain"]["precision"])
    tokens_ratio = _ratio(numbers["verified"]["tokens_per_solved"], numbers["plain"]["tokens_per_solved"])
    v_pass, p_pass = numbers["verified"]["pass@1"], numbers["plain"]["pass@1"]
    numbers["ratios"] = {"precision": precision_ratio, "tokens_per_solved": tokens_ratio}
    numbers["targets"] = {
        "precision >= 1.5x plain": None if precision_ratio is None else precision_ratio >= TARGETS["precision_ratio"],
        "pass@1 >= plain": None if v_pass is None or p_pass is None else v_pass >= p_pass,
        "tokens per solved <= 1.6x plain": None if tokens_ratio is None else tokens_ratio <= TARGETS["tokens_ratio"],
        "0 false claims": numbers["verified"]["false_claims"] == 0,
    }
    return numbers


def added_lines(diff: str) -> dict[str, set[int]]:
    """New-side line numbers (added and context) per file in a unified diff."""
    out: dict[str, set[int]] = {}
    path = ""
    line = 0
    for row in diff.splitlines():
        if row.startswith("+++ "):
            path = row[4:].strip()
            path = path[2:] if path.startswith("b/") else path
            out.setdefault(path, set())
            continue
        if row.startswith("--- "):
            continue
        if row.startswith("@@"):
            try:
                line = int(row.split("+", 1)[1].split(",", 1)[0].split(" ", 1)[0])
            except (IndexError, ValueError):
                line = 0
            continue
        if not path or not line:
            continue
        if row.startswith("-"):
            continue
        out[path].add(line)
        line += 1
    return out


def caught(defect: dict[str, Any], findings: Iterable[dict[str, Any]], slack: int = LINE_SLACK) -> bool:
    """A finding on the planted file within ``slack`` lines of the planted line."""
    want = str(defect.get("path") or "")
    for item in findings:
        path = str(item.get("path") or "")
        if not path or not (path == want or path.endswith("/" + want) or want.endswith("/" + path)):
            continue
        try:
            line = int(item.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        if not line or abs(line - int(defect.get("line") or 0)) <= slack:
            return True
    return False


def catch_rate(results: list[dict[str, Any]]) -> float | None:
    """``results``: one {"caught": bool} per fixture."""
    if not results:
        return None
    return sum(1 for row in results if row.get("caught")) / len(results)


def load_fixtures(folder: Path = FIXTURES) -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.json"))]


# --------------------------------------------------------------------------- running (needs a model)


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "NO"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def render(numbers: dict[str, Any], reviewer: float | None = None) -> str:
    lines = ["| | verified | plain |", "|---|---|---|"]
    for key in ("trials", "precision", "pass@1", "tokens_per_solved"):
        lines.append(f"| {key} | {_fmt(numbers['verified'].get(key))} | {_fmt(numbers['plain'].get(key))} |")
    lines.append(f"| false claims | {_fmt(numbers['verified']['false_claims'])} | |")
    lines.append("")
    lines += [f"- {name}: {_fmt(ok)}" for name, ok in numbers["targets"].items()]
    if reviewer is not None:
        lines.append(f"- reviewer catches >= 50% of seeded defects: {_fmt(reviewer >= TARGETS['reviewer_catch_rate'])} "
                     f"({reviewer:.0%})")
    return "\n".join(lines) + "\n"


def _run_suite(args: argparse.Namespace) -> int:
    from critique_bot import eval_harness

    class FlowRunner(eval_harness.CritRunner):
        def __init__(self, flow: str, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.flow = flow
            self.name = f"crit-{flow}"

        def run(self, task, work, out_dir, env, timeout):  # type: ignore[override]
            eval_harness._merge_settings(work, {"flow": self.flow})
            return super().run(task, work, out_dir, env, timeout)

    tasks = [task for task in eval_harness.load_tasks(Path(args.suite)) if task.spec.get("category") == "bugfix"]
    if not tasks:
        print(f"no bugfix tasks in {args.suite}")
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for flow in ("verified", "plain"):
        runner = FlowRunner(flow, command=args.crit, config=args.config)
        with (out / f"{flow}.jsonl").open("w", encoding="utf-8") as handle:
            for task in tasks:
                for number in range(1, args.runs + 1):
                    record = eval_harness.run_trial(task, number, runner, out / flow)
                    handle.write(json.dumps(record) + "\n")
                    handle.flush()
                    print(f"{flow} {task.name} #{number}: {record.get('result')}", flush=True)
    return _report(argparse.Namespace(folder=str(out)))


def _review_fixtures(args: argparse.Namespace) -> int:
    from critique_bot import model_api
    from critique_bot.config import load_config
    from critique_bot.engine.flows import verified

    config = load_config(Path(args.config).expanduser())
    sections = verified.flow_sections()
    results = []
    with model_api.open_provider(config, headed=False) as provider:
        for fixture in load_fixtures():
            context = "\n\n".join(f"{path} (as it is now):\n{body}" for path, body in fixture.get("context", {}).items())
            prompt = (sections["REVIEW_TASK"].replace("{task}", fixture["task"]).replace("{diff}", fixture["diff"])
                      .replace("{evidence}", "No repro and no tests were run." + ("\n\n" + context if context else ""))
                      + "\n\nTools are off for this review: judge from the diff and the files above.")
            with provider.session() as session:
                answer = session.send(prompt)
            review = verified.review_from_text(answer)
            results.append({"name": fixture["name"], "caught": caught(fixture["defect"], review.findings),
                            "risk": review.risk, "findings": len(review.findings)})
            print(f"{fixture['name']}: {'caught' if results[-1]['caught'] else 'missed'}", flush=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "reviewer.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    rate = catch_rate(results)
    print(f"catch rate: {_fmt(rate)}")
    return 0


def _report(args: argparse.Namespace) -> int:
    folder = Path(args.folder)
    numbers = compare(_records(folder / "verified.jsonl"), _records(folder / "plain.jsonl"))
    reviewer_file = folder / "reviewer.json"
    rate = catch_rate(json.loads(reviewer_file.read_text(encoding="utf-8"))) if reviewer_file.is_file() else None
    text = render(numbers, rate)
    (folder / "summary.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verified_eval", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="bugfix tasks with --flow verified and plain")
    run.add_argument("--suite", default=str(ROOT / "tests" / "eval_tasks"))
    run.add_argument("--runs", type=int, default=3)
    run.add_argument("--config", default="")
    run.add_argument("--crit", default="")
    run.add_argument("--out", default=str(ROOT / "plan" / "results" / "07"))
    review = sub.add_parser("reviewer", help="the verifier on the seeded-defect fixtures")
    review.add_argument("--config", required=True, help="crit config file naming the model server")
    review.add_argument("--out", default=str(ROOT / "plan" / "results" / "07"))
    report = sub.add_parser("report", help="summary.md from a results folder")
    report.add_argument("folder")
    args = parser.parse_args(argv)
    return {"run": _run_suite, "reviewer": _review_fixtures, "report": _report}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
