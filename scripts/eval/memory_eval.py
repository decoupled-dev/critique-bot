#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Does crit behave like someone who knows the project? The Stage 8b warm-up-then-test eval.

For one repository, crit runs the warm-up tasks in order (they build the project memory), then
the held-out test tasks, all in the same workspace, once with memory on and once with it off,
``--runs`` times (plan/08b-project-memory.md, "Eval protocol"). Between tasks the tracked files
go back to the starting commit; ``.crit/memory`` stays (memory on) or is never written (off).

    python3 scripts/eval/memory_eval.py run --plan plan.json --config ~/.crit/qwen.json --runs 3 --out out/
    python3 scripts/eval/memory_eval.py report out/

``plan.json``::

    {"repo": "$CAR_REPO",                  # the project (copied, never changed)
     "suite": "tests/eval_tasks",          # where task folders live (crit eval task.json format)
     "warmup": ["fix-a", "fix-b", ...],    # 15 in the protocol
     "test": ["fix-c", ...]}               # 10 held out

Each test task's metrics come from crit's session event log:

- **turns to the first correct file**: model turns until the first read or edit of a file the
  task finally changed;
- **tokens before the first edit**: prompt + completion tokens up to the turn that makes the first edit;
- **predicted-file acc@5**: whether a file of the hunch's top 5 (the ``memory_recall`` event) was
  changed; **wrong hunch**: its top-1 file was not;
- pass@1 (the task's check), total tokens and wall time.

A test task whose reference change overlaps a warm-up task's by more than :data:`LEAK_OVERLAP`
of its lines is reported and left out (the eval measures generalization, not replay).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

MUTATING = frozenset({"write_files", "edit_file", "delete_file", "apply_patch", "move_file"})
READING = frozenset({"read_files", "edit_file", "write_files", "apply_patch"})
LEAK_OVERLAP = 0.5
CONDITIONS = ("on", "off")


# --------------------------------------------------------------------------- metrics (pure)


def _paths(tool: str, args: Any) -> list[str]:
    """Files a read or edit call names."""
    if not isinstance(args, dict):
        return []
    out: list[str] = []
    for key in ("path", "file", "filePath"):
        if isinstance(args.get(key), str):
            out.append(args[key])
    for key in ("paths", "files"):
        value = args.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict) and isinstance(item.get("path"), str):
                    out.append(item["path"])
    if tool == "apply_patch":
        patch = str(args.get("patch") or args.get("input") or "")
        for line in patch.splitlines():
            for marker in ("+++ b/", "*** Update File: ", "*** Add File: "):
                if line.startswith(marker):
                    out.append(line[len(marker):].strip())
    return [item.strip().lstrip("./") for item in out if item.strip()]


def task_metrics(events: list[dict[str, Any]], changed: Iterable[str] | None = None) -> dict[str, Any]:
    """The metrics of one task from its session events (one task per session in this eval)."""
    final = next((e for e in reversed(events) if e.get("type") == "task_finished"), {})
    changed_set = {str(p).lstrip("./") for p in (changed if changed is not None else (final.get("changed") or []))}
    turns = 0
    tokens = 0
    tokens_before_edit: int | None = None
    first_correct: int | None = None
    predicted: list[str] = []
    for event in events:
        kind = event.get("type")
        if kind == "memory_recall" and not predicted:
            predicted = [str(item.get("path")) for item in event.get("files") or [] if isinstance(item, dict)]
        elif kind == "assistant_turn":
            turns += 1
            usage = event.get("usage") or {}
            tokens += int(usage.get("prompt") or 0) + int(usage.get("completion") or 0)
        elif kind == "tool_started":
            tool = str(event.get("tool") or "")
            paths = set(_paths(tool, event.get("arguments")))
            if first_correct is None and tool in READING and paths & changed_set:
                first_correct = int(event.get("turn") or turns)
            if tokens_before_edit is None and tool in MUTATING:
                tokens_before_edit = tokens
    top5 = predicted[:5]
    return {
        "changed": sorted(changed_set),
        "turns": turns,
        "tokens": tokens,
        "turns_to_first_correct_file": first_correct,
        "tokens_before_first_edit": tokens_before_edit,
        "predicted": top5,
        "acc_at_5": (bool(set(top5) & changed_set) if top5 and changed_set else None),
        "wrong_hunch": (top5[0] not in changed_set if top5 and changed_set else None),
    }


def _median(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _mean(values: list[Any]) -> float | None:
    values = [float(v) for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _change(on: float | None, off: float | None) -> float | None:
    if on is None or off is None or off == 0:
        return None
    return (on - off) / off


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per condition: medians, acc@5, wrong-hunch rate, pass@1; and the on-vs-off changes the
    completion targets name (turns to first correct file -40%, tokens before first edit -30%,
    pass@1 not lower, pass@1 on wrong-hunch tasks not lower)."""
    tests = [r for r in records if r.get("phase") == "test" and not r.get("leak")]
    out: dict[str, Any] = {}
    for condition in CONDITIONS:
        rows = [r for r in tests if r.get("condition") == condition]
        out[condition] = {
            "trials": len(rows),
            "pass_at_1": _mean([1.0 if r.get("passed") else 0.0 for r in rows]),
            "turns_to_first_correct_file": _median([r.get("turns_to_first_correct_file") for r in rows]),
            "tokens_before_first_edit": _median([r.get("tokens_before_first_edit") for r in rows]),
            "tokens": _median([r.get("tokens") for r in rows]),
            "seconds": _median([r.get("seconds") for r in rows]),
            "acc_at_5": _mean([1.0 if r["acc_at_5"] else 0.0 for r in rows if r.get("acc_at_5") is not None]),
            "wrong_hunch_rate": _mean([1.0 if r["wrong_hunch"] else 0.0 for r in rows if r.get("wrong_hunch") is not None]),
        }
    on, off = out["on"], out["off"]
    wrong = {(r["task"], r.get("run")) for r in tests if r.get("condition") == "on" and r.get("wrong_hunch")}
    on_wrong = [1.0 if r.get("passed") else 0.0 for r in tests if r.get("condition") == "on" and (r["task"], r.get("run")) in wrong]
    off_same = [1.0 if r.get("passed") else 0.0 for r in tests if r.get("condition") == "off" and (r["task"], r.get("run")) in wrong]
    out["delta"] = {
        "turns_to_first_correct_file": _change(on["turns_to_first_correct_file"], off["turns_to_first_correct_file"]),
        "tokens_before_first_edit": _change(on["tokens_before_first_edit"], off["tokens_before_first_edit"]),
        "pass_at_1": (on["pass_at_1"] - off["pass_at_1"]) if on["pass_at_1"] is not None and off["pass_at_1"] is not None else None,
        "pass_at_1_wrong_hunch_on": _mean(on_wrong),
        "pass_at_1_wrong_hunch_off": _mean(off_same),
    }
    out["leaks"] = sorted({r["task"] for r in records if r.get("leak")})
    return out


def _fmt(value: Any, pct: bool = False) -> str:
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:+.0f}%"
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def render(summary: dict[str, Any]) -> str:
    lines = ["# Project memory eval (warm-up then held-out)", "",
             "| metric | memory on | memory off | change |", "|---|---|---|---|"]
    for key in ("turns_to_first_correct_file", "tokens_before_first_edit", "pass_at_1", "acc_at_5", "wrong_hunch_rate",
                "tokens", "seconds", "trials"):
        change = summary["delta"].get(key)
        shown = _fmt(change, pct=key not in {"pass_at_1"}) if change is not None else "-"
        lines.append(f"| {key} | {_fmt(summary['on'][key])} | {_fmt(summary['off'][key])} | {shown} |")
    delta = summary["delta"]
    lines += ["", f"pass@1 on wrong-hunch tasks: on {_fmt(delta['pass_at_1_wrong_hunch_on'])}, "
                  f"off {_fmt(delta['pass_at_1_wrong_hunch_off'])}"]
    if summary["leaks"]:
        lines.append("left out (reference overlaps a warm-up task): " + ", ".join(summary["leaks"]))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- leakage guard


def reference_lines(task_dir: Path, spec: dict[str, Any]) -> set[str]:
    """The lines a task's reference change adds (a folder overlay or a patch), stripped."""
    name = spec.get("reference")
    if not name:
        return set()
    source = Path(task_dir) / name
    lines: set[str] = set()
    if source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.is_file():
                try:
                    lines |= {line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()}
                except OSError:
                    continue
    elif source.is_file() and source.suffix in {".patch", ".diff"}:
        for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                lines.add(line[1:].strip())
    return {line for line in lines if len(line) >= 8}


def leaks(warmup: dict[str, set[str]], tests: dict[str, set[str]], *, limit: float = LEAK_OVERLAP) -> dict[str, str]:
    """Test tasks whose reference lines overlap one warm-up task's by more than ``limit`` -> that task."""
    out = {}
    for name, lines in tests.items():
        if not lines:
            continue
        for other, seen in warmup.items():
            if len(lines & seen) / len(lines) > limit:
                out[name] = other
                break
    return out


# --------------------------------------------------------------------------- running (needs a model)


def _git(folder: Path, *args: str) -> str:
    from critique_bot.eval_harness import _git_env

    return subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True, text=True,
                          env=_git_env()).stdout


def _workspace(repo: Path, scratch: Path) -> Path:
    work = scratch / "work"
    shutil.copytree(repo, work, symlinks=True, ignore=shutil.ignore_patterns(".crit"))
    if not (work / ".git").exists():
        _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "--allow-empty", "-m", "memory eval start")
    return work


def _reset(work: Path) -> None:
    _git(work, "checkout", "-q", "--", ".")
    _git(work, "clean", "-fdq", "-e", ".crit")


def _settings(work: Path, condition: str) -> None:
    from critique_bot.eval_harness import _allow_everything

    _allow_everything(work)
    path = work / ".crit" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["memory"] = "full" if condition == "on" else "off"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _last_session(work: Path) -> tuple[list[dict[str, Any]], str]:
    from critique_bot import session_log

    folder = work / ".crit" / "sessions"
    rows = session_log.list_sessions(folder) if folder.is_dir() else []
    if not rows:
        return [], ""
    path = folder / rows[0].id
    events = session_log.read_events(path)
    return [session_log.resolve(path, event) for event in events], rows[0].id


def run_plan(plan: dict[str, Any], *, config: str, runs: int, out: Path, command: str = "",
             conditions: Iterable[str] = CONDITIONS, timeout: float = 1800) -> list[dict[str, Any]]:
    from critique_bot.eval_harness import expand, grade, load_tasks

    suite = Path(expand(str(plan.get("suite") or ROOT / "tests" / "eval_tasks")))
    names = list(plan.get("warmup") or []) + list(plan.get("test") or [])
    tasks = {task.name: task for task in load_tasks(suite, only=set(names))}
    missing = [name for name in names if name not in tasks]
    if missing:
        raise SystemExit(f"tasks not found in {suite}: {', '.join(missing)}")
    warm = {name: reference_lines(tasks[name].folder, tasks[name].spec) for name in plan.get("warmup") or []}
    held = {name: reference_lines(tasks[name].folder, tasks[name].spec) for name in plan.get("test") or []}
    leaked = leaks(warm, held)
    repo = Path(expand(str(plan["repo"]))).resolve()
    base = command or f"{shlex.quote(sys.executable)} -m critique_bot --as-crit"
    records: list[dict[str, Any]] = []
    out.mkdir(parents=True, exist_ok=True)
    for run in range(1, runs + 1):
        for condition in conditions:
            with tempfile.TemporaryDirectory(prefix="crit-memeval-") as scratch_name:
                scratch = Path(scratch_name)
                work = _workspace(repo, scratch)
                env = {**os.environ, "CRIT_HOME": str(scratch / "crit-home"),
                       "PYTHONPATH": str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")}
                for phase, group in (("warmup", plan.get("warmup") or []), ("test", plan.get("test") or [])):
                    for name in group:
                        task = tasks[name]
                        _reset(work)
                        _settings(work, condition)
                        trial = out / condition / f"run{run}" / name
                        trial.mkdir(parents=True, exist_ok=True)
                        argv = [*shlex.split(base), "--yes", "--output-dir", str(trial), "--config",
                                str(Path(config).expanduser().resolve()), task.spec["prompt"]]
                        started = time.monotonic()
                        try:
                            proc = subprocess.run(argv, cwd=work, env=env, capture_output=True, text=True, timeout=timeout,
                                                  stdin=subprocess.DEVNULL)
                            output = proc.stdout + proc.stderr
                        except subprocess.TimeoutExpired:
                            output = "timeout"
                        seconds = round(time.monotonic() - started, 1)
                        (trial / "output.txt").write_text(output[-20000:], encoding="utf-8")
                        events, session = _last_session(work)
                        final = next((str(e.get("text") or "") for e in reversed(events) if e.get("type") == "assistant_turn"), "")
                        changed = [line[3:] for line in _git(work, "status", "--porcelain").splitlines()
                                   if line[3:] and not line[3:].startswith(".crit")]
                        passed, why = grade(task, work, final, trial)
                        record = {"task": name, "phase": phase, "condition": condition, "run": run, "session": session,
                                  "passed": passed, "why": why, "seconds": seconds, "leak": leaked.get(name, ""),
                                  **task_metrics(events, changed)}
                        records.append(record)
                        with open(out / "results.jsonl", "a", encoding="utf-8") as handle:
                            handle.write(json.dumps(record) + "\n")
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory_eval.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run", help="run the warm-up and test tasks with memory on and off")
    run.add_argument("--plan", required=True)
    run.add_argument("--config", required=True, help="crit config for the model under test")
    run.add_argument("--runs", type=int, default=3)
    run.add_argument("--out", default="memory-eval")
    run.add_argument("--command", default="", help="how to start crit (default: this checkout)")
    run.add_argument("--conditions", default="on,off")
    report = sub.add_parser("report", help="summarize results.jsonl")
    report.add_argument("folder")
    args = parser.parse_args(argv)
    if args.action == "run":
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        records = run_plan(plan, config=args.config, runs=args.runs, out=Path(args.out), command=args.command,
                           conditions=[c for c in args.conditions.split(",") if c in CONDITIONS])
        text = render(aggregate(records))
        (Path(args.out) / "summary.md").write_text(text, encoding="utf-8")
        print(text)
        return 0
    path = Path(args.folder) / "results.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(render(aggregate(records)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
