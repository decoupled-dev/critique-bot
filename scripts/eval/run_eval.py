#!/usr/bin/env python3
"""Run crit on a set of real tasks and record how it did.

Each task is a folder under ``tests/eval_tasks/`` with a ``task.json``:

    {
      "prompt": "what to ask crit",
      "fixture": "files",            # folder copied into a fresh git workspace (optional)
      "workspace": "$AOSP",          # or an existing folder, used read-only (optional)
      "setup": "python3 gen_log.py", # run in the workspace before crit (optional)
      "check": "python3 -m unittest",# exit 0 means pass (optional)
      "answer": "regex",             # or: crit's final reply must match this (optional)
      "requires": ["java", "$AOSP"], # programs on PATH or env vars; skipped when missing
      "timeout": 900,
      "tags": ["gradle"]
    }

``$NAME`` in ``workspace``, ``setup``, and ``check`` is taken from the environment.

Per run it records: pass or fail, wall time, model turns, tool calls by name, characters
sent and received, and crit's exit code. Results go to ``<out>/results.jsonl`` with a
``summary.md`` table, and each run's transcript stays in ``<out>/<task>/<n>/``.

    python3 scripts/eval/run_eval.py --config ~/.crit/qwen.json --runs 3
    python3 scripts/eval/run_eval.py --only py-fix-test,log-crash --tag logs

The default crit is this checkout (``python -m critique_bot --as-crit`` with ``src`` on
the path); ``--crit`` runs another one, for example an installed ``crit``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "tests" / "eval_tasks"
TOOL_NAME = re.compile(r'"name"\s*:\s*"([a-z_]+)"')


def expand(text: str) -> str:
    return re.sub(r"\$([A-Z_][A-Z0-9_]*)", lambda m: os.environ.get(m.group(1), m.group(0)), text)


def load_tasks(folder: Path, only: set[str], tags: set[str]) -> list[tuple[str, Path, dict]]:
    out = []
    for item in sorted(folder.iterdir()):
        spec_path = item / "task.json"
        if not spec_path.is_file():
            continue
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        if only and item.name not in only:
            continue
        if tags and not tags & set(spec.get("tags") or []):
            continue
        out.append((item.name, item, spec))
    return out


def missing(spec: dict) -> list[str]:
    gone = []
    for need in spec.get("requires") or []:
        if need.startswith("$"):
            if not os.environ.get(need[1:]):
                gone.append(need)
        elif shutil.which(need) is None:
            gone.append(need)
    return gone


def git(folder: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@localhost",
           "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@localhost"}
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True, env=env)


def make_workspace(task_dir: Path, spec: dict, scratch: Path) -> Path:
    if spec.get("workspace"):
        return Path(expand(spec["workspace"])).resolve()
    work = scratch / "work"
    fixture = task_dir / spec.get("fixture", "files")
    if fixture.is_dir():
        shutil.copytree(fixture, work)
    else:
        work.mkdir(parents=True)
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-q", "--allow-empty", "-m", "task start")
    return work


def run_shell(command: str, cwd: Path, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(expand(command), shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:]


def transcript_stats(out_dir: Path) -> dict:
    found = out_dir / "agent.json"
    if not found.is_file():
        return {"turns": 0, "tool_calls": {}, "chars_out": 0, "chars_in": 0, "final": ""}
    payload = json.loads(found.read_text(encoding="utf-8"))
    turns = payload.get("turns") or []
    assistant = [str(t.get("content") or "") for t in turns if t.get("role") == "assistant"]
    sent = [str(t.get("content") or "") for t in turns if t.get("role") != "assistant"]
    calls: Counter[str] = Counter()
    for text in assistant:
        calls.update(TOOL_NAME.findall(text))
    return {
        "turns": len(assistant),
        "tool_calls": dict(calls),
        "chars_out": sum(map(len, assistant)),
        "chars_in": sum(map(len, sent)),
        "final": assistant[-1] if assistant else "",
    }


def run_once(name: str, task_dir: Path, spec: dict, run: int, args: argparse.Namespace, out_root: Path) -> dict:
    record: dict = {"task": name, "run": run}
    gone = missing(spec)
    if gone:
        return {**record, "result": "skipped", "why": "missing " + ", ".join(gone)}
    out_dir = out_root / name / str(run)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"crit-eval-{name}-") as tmp:
        scratch = Path(tmp)
        work = make_workspace(task_dir, spec, scratch)
        timeout = float(spec.get("timeout") or args.timeout)
        if spec.get("setup"):
            code, text = run_shell(spec["setup"], work, timeout)
            if code != 0:
                return {**record, "result": "error", "why": f"setup failed: {text[-500:]}"}
        env = dict(os.environ)
        if not args.keep_home:
            env["CRIT_HOME"] = str(scratch / "crit-home")
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        command = [*shlex.split(args.crit), "--yes", "--output-dir", str(out_dir)]
        if args.config:
            command += ["--config", str(Path(args.config).expanduser().resolve())]
        command.append(spec["prompt"])
        started = time.monotonic()
        try:
            proc = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            code, output = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            code = 124
            output = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        record["seconds"] = round(time.monotonic() - started, 1)
        record["crit_exit"] = code
        (out_dir / "crit-output.txt").write_text(output, encoding="utf-8")
        stats = transcript_stats(out_dir)
        final = stats.pop("final")
        record.update(stats)
        passed = True
        why = []
        if spec.get("check"):
            check_code, check_text = run_shell(spec["check"], work, 600)
            (out_dir / "check-output.txt").write_text(check_text, encoding="utf-8")
            if check_code != 0:
                passed = False
                why.append(f"check exit {check_code}")
        if spec.get("answer"):
            if not re.search(spec["answer"], final, re.IGNORECASE | re.DOTALL):
                passed = False
                why.append("final reply does not match the answer")
        if code == 124:
            passed = False
            why.append("crit timed out")
        record["result"] = "pass" if passed else "fail"
        if why:
            record["why"] = "; ".join(why)
        if not spec.get("workspace"):
            diff = subprocess.run(["git", "-C", str(work), "diff", "HEAD"], capture_output=True, text=True)
            (out_dir / "changes.diff").write_text(diff.stdout, encoding="utf-8")
    return record


def summary(records: list[dict]) -> str:
    rows = ["| task | run | result | seconds | turns | tool calls | why |", "|---|---|---|---|---|---|---|"]
    for r in records:
        calls = sum((r.get("tool_calls") or {}).values())
        rows.append(f"| {r['task']} | {r['run']} | {r['result']} | {r.get('seconds', '')} | {r.get('turns', '')} | {calls or ''} | {r.get('why', '')} |")
    ran = [r for r in records if r["result"] in {"pass", "fail"}]
    passed = sum(1 for r in ran if r["result"] == "pass")
    rows.append("")
    rows.append(f"passed {passed}/{len(ran)}; skipped {sum(1 for r in records if r['result'] == 'skipped')}")
    if ran:
        rows.append(f"mean turns {sum(r.get('turns', 0) for r in ran) / len(ran):.1f}; mean seconds {sum(r.get('seconds', 0) for r in ran) / len(ran):.0f}")
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tasks", default=str(TASKS), help="folder of task folders")
    parser.add_argument("--only", default="", help="comma-separated task names")
    parser.add_argument("--tag", action="append", default=[], help="run tasks with this tag (repeatable)")
    parser.add_argument("--runs", type=int, default=1, help="runs per task (models vary; 3 gives a rate)")
    parser.add_argument("--config", help="crit settings file naming the backend (for example a local Qwen)")
    parser.add_argument("--crit", default=f"{shlex.quote(sys.executable)} -m critique_bot --as-crit", help="the crit command")
    parser.add_argument("--timeout", type=float, default=900, help="seconds per run unless the task says")
    parser.add_argument("--out", default=str(ROOT / ".crit" / "eval" / time.strftime("%Y%m%d-%H%M%S")))
    parser.add_argument("--keep-home", action="store_true", help="use your own CRIT_HOME instead of a fresh one per run")
    parser.add_argument("--list", action="store_true", help="list the tasks and exit")
    args = parser.parse_args(argv)
    tasks = load_tasks(Path(args.tasks), {item for item in args.only.split(",") if item}, set(args.tag))
    if args.list:
        for name, _, spec in tasks:
            gone = missing(spec)
            print(f"{name:28} {','.join(spec.get('tags') or []):20} {'missing ' + ', '.join(gone) if gone else 'ready'}")
        return 0
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    records = []
    with open(out_root / "results.jsonl", "a", encoding="utf-8") as sink:
        for name, task_dir, spec in tasks:
            for run in range(1, args.runs + 1):
                record = run_once(name, task_dir, spec, run, args, out_root)
                records.append(record)
                sink.write(json.dumps(record) + "\n")
                sink.flush()
                print(f"{name} #{run}: {record['result']} {record.get('seconds', '')}s turns={record.get('turns', '')} {record.get('why', '')}", flush=True)
    text = summary(records)
    (out_root / "summary.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"results in {out_root}")
    return 0 if all(r["result"] != "fail" for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
