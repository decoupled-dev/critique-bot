# SPDX-License-Identifier: Apache-2.0
"""``crit eval``: run a coding agent on a suite of real tasks and measure the harness.

A task is a folder with a ``task.json``::

    {
      "prompt": "what to ask the agent",
      "fixture": "files",             # folder copied into a fresh git workspace (optional)
      "workspace": "$AOSP",           # or an existing folder, used as is and never changed by crit eval
      "setup": "python3 gen_log.py",  # run in the workspace before the agent (optional)
      "hidden": "hidden",             # folder copied into the workspace only for the check (optional)
      "check": "python3 -m unittest", # exit 0 means pass (optional)
      "answer": "regex",              # or: the agent's final reply must match this (optional)
      "reference": "solution",        # reference fix: a folder overlaid on the workspace, or a .sh/.patch file
      "reference_answer": "...",      # a reply that a correct agent gives (answer tasks)
      "requires": ["java", "$AOSP"],  # programs on PATH or env vars; the task is skipped when missing
      "timeout": 900,
      "tags": ["gradle", "quick"],
      "category": "bugfix", "difficulty": "medium", "source": "synthetic"
    }

``$NAME`` in ``workspace``, ``setup`` and ``check`` comes from the environment.

Commands::

    crit eval [run] [--suite DIR] [--only a,b] [--tag quick] [--runs 3] [--runner crit|opencode] [--out DIR]
    crit eval list                    which tasks exist and which can run here
    crit eval audit                   every check fails untouched and passes with the reference
    crit eval compare A B             per-task table and the pass@1 difference with a 90% interval

Each trial gets a fresh temporary workspace and, for crit, a fresh ``CRIT_HOME``. Results
go to ``<out>/results.jsonl`` (one line per trial), ``summary.md`` and ``meta.json``; each
trial's output, check output, transcript and diff stay in ``<out>/<task>/<run>/``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PACKAGE_SRC = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_SRC.parent
DEFAULT_SUITE = REPO_ROOT / "tests" / "eval_tasks"
TOOL_NAME = re.compile(r'"name"\s*:\s*"([a-z_]+)"')
CHECK_TIMEOUT = 600

#: Per-trial numbers that come from the agent's own counters (``stats`` in agent.json).
STAT_FIELDS = (
    "model_requests",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "tool_errors",
    "malformed_calls",
    "edit_failures",
    "repeat_breaks",
    "compactions",
    "stale_edit_refusals",
)


# ---------------------------------------------------------------------------- tasks


@dataclass
class Task:
    name: str
    folder: Path
    spec: dict[str, Any]

    @property
    def tags(self) -> set[str]:
        return set(self.spec.get("tags") or [])


def expand(text: str) -> str:
    return re.sub(r"\$([A-Z_][A-Z0-9_]*)", lambda m: os.environ.get(m.group(1), m.group(0)), text)


def load_tasks(folder: Path, only: set[str] | None = None, tags: set[str] | None = None) -> list[Task]:
    out = []
    folder = folder.resolve()  # setup and reference scripts run with the workspace as cwd
    if not folder.is_dir():
        return out
    for item in sorted(folder.iterdir()):
        spec_path = item / "task.json"
        if not spec_path.is_file():
            continue
        if only and item.name not in only:
            continue
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        task = Task(item.name, item, spec)
        if tags and not tags & task.tags:
            continue
        out.append(task)
    return out


def missing(spec: dict[str, Any]) -> list[str]:
    gone = []
    for need in spec.get("requires") or []:
        if need.startswith("$"):
            if not os.environ.get(need[1:]):
                gone.append(need)
        elif shutil.which(need) is None:
            gone.append(need)
    return gone


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "eval",
        "GIT_AUTHOR_EMAIL": "eval@localhost",
        "GIT_COMMITTER_NAME": "eval",
        "GIT_COMMITTER_EMAIL": "eval@localhost",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def git(folder: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True, env=_git_env())


def make_workspace(task: Task, scratch: Path) -> Path:
    """A fresh git repository holding the fixture, or the task's existing folder."""
    if task.spec.get("workspace"):
        return Path(expand(task.spec["workspace"])).resolve()
    work = scratch / "work"
    fixture = task.folder / task.spec.get("fixture", "files")
    if fixture.is_dir():
        shutil.copytree(fixture, work, symlinks=True)
    else:
        work.mkdir(parents=True)
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-q", "--allow-empty", "-m", "task start")
    return work


def run_shell(command: str, cwd: Path, timeout: float, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        # setup/check commands are shell lines written by the suite's author, like a Makefile recipe.
        proc = subprocess.run(  # nosec B602
            expand(command), shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env={**_git_env(), **(env or {})},
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:]


def prepare(task: Task, scratch: Path) -> tuple[Path, str]:
    """Workspace plus setup. Returns (workspace, error); error is "" when it worked."""
    work = make_workspace(task, scratch)
    if task.spec.get("setup"):
        code, text = run_shell(task.spec["setup"], work, float(task.spec.get("timeout") or 900))
        if code != 0:
            return work, f"setup failed (exit {code}): {text[-500:]}"
    return work, ""


def add_hidden(task: Task, work: Path) -> None:
    """Copy the hidden check files in, just before the check runs."""
    name = task.spec.get("hidden")
    if not name:
        return
    source = task.folder / name
    if source.is_dir():
        shutil.copytree(source, work, dirs_exist_ok=True)


def apply_reference(task: Task, work: Path) -> str:
    """Apply the task's reference fix. Returns "" or what went wrong."""
    name = task.spec.get("reference")
    if not name:
        return "no reference"
    source = task.folder / name
    if source.is_dir():
        shutil.copytree(source, work, dirs_exist_ok=True)
        return ""
    if source.is_file() and source.suffix == ".sh":
        code, text = run_shell(f"sh {shlex.quote(str(source))}", work, 300)
        return "" if code == 0 else f"reference script failed (exit {code}): {text[-300:]}"
    if source.is_file() and source.suffix in {".patch", ".diff"}:
        code, text = run_shell(f"git apply {shlex.quote(str(source))}", work, 60)
        return "" if code == 0 else f"reference patch does not apply: {text[-300:]}"
    return f"reference {name!r} not found"


def grade(task: Task, work: Path, final: str, out_dir: Path | None = None) -> tuple[bool, list[str]]:
    """Run the check and match the answer. Returns (passed, reasons for a fail)."""
    why: list[str] = []
    if task.spec.get("check"):
        add_hidden(task, work)
        code, text = run_shell(task.spec["check"], work, CHECK_TIMEOUT)
        if out_dir is not None:
            (out_dir / "check-output.txt").write_text(text, encoding="utf-8")
        if code != 0:
            why.append(f"check exit {code}")
    if task.spec.get("answer"):
        if not re.search(task.spec["answer"], final or "", re.IGNORECASE | re.DOTALL):
            why.append("final reply does not match the answer")
    if not task.spec.get("check") and not task.spec.get("answer"):
        why.append("task has no check and no answer")
    return not why, why


# ---------------------------------------------------------------------------- runners


@dataclass
class RunOutput:
    code: int
    output: str
    final: str
    stats: dict[str, Any]


class Runner:
    name = "runner"

    def version(self) -> str:
        return ""

    def run(self, task: Task, work: Path, out_dir: Path, env: dict[str, str], timeout: float) -> RunOutput:
        raise NotImplementedError


def _run_process(command: list[str], work: Path, env: dict[str, str], timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            command, cwd=work, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        return 124, out.decode(errors="replace") if isinstance(out, bytes) else out
    except OSError as exc:
        return 127, f"could not start {command[0]}: {exc}"


class CritRunner(Runner):
    """crit itself: this checkout by default (``python -m critique_bot --as-crit``)."""

    name = "crit"

    def __init__(self, command: str = "", config: str = "", keep_home: bool = False, ask_commands: bool = False,
                 packs: tuple[str, ...] = ()) -> None:
        self.command = command or f"{shlex.quote(sys.executable)} -m critique_bot --as-crit"
        #: Domain packs turned on in every trial's workspace (``--packs aosp``), for with/without comparisons.
        self.packs = tuple(packs)
        self.config = config
        self.keep_home = keep_home
        #: False (the default): the throwaway workspace allows every command, so nothing waits
        #: for an approval no one can give. Network commands stay under the egress policy.
        self.ask_commands = ask_commands

    def version(self) -> str:
        try:
            head = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True
            ).stdout.strip()
        except OSError:
            head = ""
        from critique_bot import __version__

        return f"{__version__} {head}".strip()

    def run(self, task: Task, work: Path, out_dir: Path, env: dict[str, str], timeout: float) -> RunOutput:
        env = dict(env)
        if not self.keep_home:
            env["CRIT_HOME"] = str(out_dir.parent / f".crit-home-{out_dir.name}")
        env["PYTHONPATH"] = str(PACKAGE_SRC) + os.pathsep + env.get("PYTHONPATH", "")
        command = [*shlex.split(self.command), "--yes", "--output-dir", str(out_dir)]
        if self.config:
            command += ["--config", str(Path(self.config).expanduser().resolve())]
        command.append(task.spec["prompt"])
        # Settings for this run go through the environment of the crit process, never into the
        # workspace: a task's "workspace" is the user's real project and must stay as it was.
        values: dict[str, Any] = {}
        if not self.ask_commands:
            values.update(_ALLOW_EVERYTHING)
        if self.packs:
            values["packs"] = list(self.packs)
        if values:
            from critique_bot.project_home import ENV_EVAL_SETTINGS

            env[ENV_EVAL_SETTINGS] = json.dumps(values)
        code, output = _run_process(command, work, env, timeout)
        if not self.keep_home:
            shutil.rmtree(env["CRIT_HOME"], ignore_errors=True)
        final, stats = crit_transcript(out_dir)
        return RunOutput(code, output, final, stats)


#: Lets crit run any command without asking (an eval has no terminal to answer). Passed as
#: CRIT_EVAL_SETTINGS to the crit process only; network commands stay under the egress policy.
_ALLOW_EVERYTHING: dict[str, Any] = {"allow": ["*"], "confirm_risky": False}


def crit_transcript(out_dir: Path) -> tuple[str, dict[str, Any]]:
    """The final reply and the counters from crit's ``agent.json``."""
    found = out_dir / "agent.json"
    if not found.is_file():
        return "", {}
    try:
        payload = json.loads(found.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", {}
    turns = payload.get("turns") or []
    assistant = [str(t.get("content") or "") for t in turns if t.get("role") == "assistant"]
    stats = dict(payload.get("stats") or {})
    if not stats.get("tool_calls"):
        calls: dict[str, int] = {}
        for text in assistant:
            for name in TOOL_NAME.findall(text):
                calls[name] = calls.get(name, 0) + 1
        stats["tool_calls"] = calls
    stats.setdefault("turns", len(assistant))
    stats["chars_out"] = sum(map(len, assistant))
    stats["chars_in"] = sum(len(str(t.get("content") or "")) for t in turns if t.get("role") != "assistant")
    return (assistant[-1] if assistant else ""), stats


class OpencodeRunner(Runner):
    """opencode, for a baseline on the same model and server.

    ``command`` is a template: ``{prompt}`` is replaced by the task prompt (one argument)
    and ``{model}`` by ``--model``. Point opencode's own config (``opencode.json``) at the
    same OpenAI-compatible server crit uses. Token counts are read from JSON events when the
    command prints them (``--format json``); otherwise they stay empty.
    """

    name = "opencode"

    def __init__(self, command: str = "", model: str = "") -> None:
        self.command = command or "opencode run --format json {model_flag} {prompt}"
        self.model = model

    def version(self) -> str:
        binary = shlex.split(self.command)[0]
        try:
            return subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"

    def argv(self, prompt: str) -> list[str]:
        out = []
        for part in shlex.split(self.command):
            if part == "{prompt}":
                out.append(prompt)
            elif part == "{model_flag}":
                if self.model:
                    out += ["--model", self.model]
            else:
                out.append(part.replace("{model}", self.model))
        return out

    def run(self, task: Task, work: Path, out_dir: Path, env: dict[str, str], timeout: float) -> RunOutput:
        code, output = _run_process(self.argv(task.spec["prompt"]), work, env, timeout)
        final, stats = parse_opencode_output(output)
        return RunOutput(code, output, final, stats)


def parse_opencode_output(output: str) -> tuple[str, dict[str, Any]]:
    """Best effort: sum token fields and keep the last text from JSON event lines."""
    stats: dict[str, Any] = {"tool_calls": {}}
    texts: list[str] = []
    tokens = {"input": 0, "output": 0, "cache_read": 0}
    saw_json = False
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        saw_json = True
        _walk_opencode(event, texts, tokens, stats["tool_calls"])
    if saw_json:
        stats["prompt_tokens"] = tokens["input"] or None
        stats["completion_tokens"] = tokens["output"] or None
        stats["cached_tokens"] = tokens["cache_read"] or None
        return (texts[-1] if texts else ""), stats
    return output.strip()[-4000:], stats


def _walk_opencode(node: Any, texts: list[str], tokens: dict[str, int], tools: dict[str, int]) -> None:
    if isinstance(node, dict):
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            texts.append(node["text"])
        if node.get("type") == "tool" and isinstance(node.get("tool"), str):
            tools[node["tool"]] = tools.get(node["tool"], 0) + 1
        found = node.get("tokens")
        if isinstance(found, dict):
            for key, target in (("input", "input"), ("output", "output")):
                if isinstance(found.get(key), int):
                    tokens[target] += found[key]
            cache = found.get("cache")
            if isinstance(cache, dict) and isinstance(cache.get("read"), int):
                tokens["cache_read"] += cache["read"]
        for value in node.values():
            _walk_opencode(value, texts, tokens, tools)
    elif isinstance(node, list):
        for value in node:
            _walk_opencode(value, texts, tokens, tools)


# ---------------------------------------------------------------------------- trials


def run_trial(task: Task, run: int, runner: Runner, out_root: Path, *, timeout: float | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {"task": task.name, "run": run, "runner": runner.name}
    for key in ("category", "difficulty"):
        if task.spec.get(key):
            record[key] = task.spec[key]
    gone = missing(task.spec)
    if gone:
        return {**record, "result": "skipped", "why": "missing " + ", ".join(gone)}
    out_dir = out_root / task.name / str(run)
    out_dir.mkdir(parents=True, exist_ok=True)
    limit = float(task.spec.get("timeout") or timeout or 900)
    with tempfile.TemporaryDirectory(prefix=f"crit-eval-{task.name}-") as tmp:
        work, error = prepare(task, Path(tmp))
        if error:
            return {**record, "result": "error", "why": error}
        started = time.monotonic()
        result = runner.run(task, work, out_dir, dict(os.environ), limit)
        record["seconds"] = round(time.monotonic() - started, 1)
        record["exit"] = result.code
        (out_dir / "output.txt").write_text(result.output, encoding="utf-8")
        for key, value in result.stats.items():
            record[key] = value
        if not task.spec.get("workspace"):
            diff = subprocess.run(["git", "-C", str(work), "diff", "HEAD"], capture_output=True, text=True)
            (out_dir / "changes.diff").write_text(diff.stdout, encoding="utf-8")
        passed, why = grade(task, work, result.final, out_dir)
        if result.code == 124:
            passed = False
            why.append("timed out")
        record["result"] = "pass" if passed else "fail"
        if why:
            record["why"] = "; ".join(why)
    return record


# ---------------------------------------------------------------------------- metrics


def pass_at_k(n: int, c: int, k: int) -> float:
    """Chance that at least one of k trials passes, from n trials with c passes (unbiased)."""
    if n <= 0 or k <= 0:
        return 0.0
    k = min(k, n)
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """Chance that all of k trials pass (tau-bench's pass^k), from n trials with c passes."""
    if n <= 0 or k <= 0:
        return 0.0
    k = min(k, n)
    if c < k:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


def _median(values: list[float]) -> float | None:
    values = [v for v in values if isinstance(v, (int, float))]
    return statistics.median(values) if values else None


def aggregate(records: list[dict[str, Any]], k: int | None = None) -> dict[str, Any]:
    """Per-task and suite numbers. Skipped and errored trials do not count as runs."""
    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        if r.get("result") in {"pass", "fail"}:
            by_task.setdefault(r["task"], []).append(r)
    runs = max((len(v) for v in by_task.values()), default=0)
    k = k or max(1, runs)
    tasks = {}
    for name, rows in sorted(by_task.items()):
        n = len(rows)
        c = sum(1 for r in rows if r["result"] == "pass")
        tasks[name] = {
            "n": n,
            "passes": c,
            "pass@1": c / n,
            f"pass@{k}": pass_at_k(n, c, k),
            f"pass^{k}": pass_hat_k(n, c, k),
        }
    ran = [r for rows in by_task.values() for r in rows]
    solved = [r for r in ran if r["result"] == "pass"]
    turns_total = sum(int(r.get("model_requests") or r.get("turns") or 0) for r in ran)

    def rate(field: str) -> float | None:
        if not turns_total:
            return None
        return sum(int(r.get(field) or 0) for r in ran) / turns_total

    prompt_total = sum(int(r.get("prompt_tokens") or 0) for r in ran)
    cached_total = sum(int(r.get("cached_tokens") or 0) for r in ran)
    edit_calls = sum(sum(int(row.get("calls", 0)) for row in (r.get("edits") or {}).values()) for r in ran)
    edit_ok = sum(sum(int(row.get("ok", 0)) for row in (r.get("edits") or {}).values()) for r in ran)
    suite = {
        "tasks": len(tasks),
        "trials": len(ran),
        "k": k,
        "pass@1": statistics.mean(t["pass@1"] for t in tasks.values()) if tasks else 0.0,
        f"pass@{k}": statistics.mean(t[f"pass@{k}"] for t in tasks.values()) if tasks else 0.0,
        f"pass^{k}": statistics.mean(t[f"pass^{k}"] for t in tasks.values()) if tasks else 0.0,
        "median_seconds": _median([r.get("seconds") for r in ran]),
        "median_turns": _median([r.get("model_requests") or r.get("turns") for r in ran]),
        "median_prompt_tokens": _median([r.get("prompt_tokens") for r in ran if r.get("prompt_tokens")]),
        "median_completion_tokens": _median([r.get("completion_tokens") for r in ran if r.get("completion_tokens")]),
        "completion_tokens_per_solved": (
            sum(int(r.get("completion_tokens") or 0) for r in ran) / len(solved) if solved else None
        ),
        "cache_hit_ratio": cached_total / prompt_total if prompt_total else None,
        "malformed_per_turn": rate("malformed_calls"),
        "tool_errors_per_turn": rate("tool_errors"),
        "edit_success_rate": edit_ok / edit_calls if edit_calls else None,
        "repeat_breaks": sum(int(r.get("repeat_breaks") or 0) for r in ran),
        "compactions": sum(int(r.get("compactions") or 0) for r in ran),
        "exit_reasons": _count(r.get("exit_reason") or ("timeout" if r.get("exit") == 124 else "?") for r in ran),
    }
    return {"suite": suite, "tasks": tasks}


def _count(items: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[str(item)] = out.get(str(item), 0) + 1
    return out


def bootstrap_diff(
    a: dict[str, float], b: dict[str, float], *, iterations: int = 2000, seed: int = 0, level: float = 0.90
) -> tuple[float, float, float, int]:
    """Mean of b - a over the tasks both have, with a paired bootstrap interval.

    Returns (difference, low, high, tasks compared).
    """
    common = sorted(set(a) & set(b))
    if not common:
        return 0.0, 0.0, 0.0, 0
    diffs = [b[name] - a[name] for name in common]
    mean = statistics.mean(diffs)
    rng = random.Random(seed)
    samples = sorted(statistics.mean(rng.choice(diffs) for _ in diffs) for _ in range(iterations))
    tail = (1.0 - level) / 2
    low = samples[int(tail * (iterations - 1))]
    high = samples[int((1.0 - tail) * (iterations - 1))]
    return mean, low, high, len(common)


# ---------------------------------------------------------------------------- reports


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_summary(records: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> str:
    agg = aggregate(records)
    suite, k = agg["suite"], agg["suite"]["k"]
    lines = ["# crit eval summary", ""]
    if meta:
        lines += [f"- {key}: {value}" for key, value in meta.items() if key != "argv"]
        lines.append("")
    lines += [
        "| metric | value |",
        "|---|---|",
        f"| tasks / trials | {suite['tasks']} / {suite['trials']} |",
        f"| pass@1 | {_fmt(suite['pass@1'])} |",
        *([f"| pass@{k} | {_fmt(suite[f'pass@{k}'])} |", f"| pass^{k} | {_fmt(suite[f'pass^{k}'])} |"] if k > 1 else []),
        f"| median seconds | {_fmt(suite['median_seconds'], 0)} |",
        f"| median turns | {_fmt(suite['median_turns'], 0)} |",
        f"| median prompt / completion tokens | {_fmt(suite['median_prompt_tokens'], 0)} / {_fmt(suite['median_completion_tokens'], 0)} |",
        f"| completion tokens per solved task | {_fmt(suite['completion_tokens_per_solved'], 0)} |",
        f"| cache hit ratio | {_fmt(suite['cache_hit_ratio'])} |",
        f"| malformed calls per turn | {_fmt(suite['malformed_per_turn'], 3)} |",
        f"| tool errors per turn | {_fmt(suite['tool_errors_per_turn'], 3)} |",
        f"| edit success rate | {_fmt(suite['edit_success_rate'])} |",
        f"| repeat breaks / compactions | {suite['repeat_breaks']} / {suite['compactions']} |",
        f"| exit reasons | {', '.join(f'{key} {n}' for key, n in sorted(suite['exit_reasons'].items())) or '–'} |",
        "",
        f"| task | passes | pass@1 | pass^{k} |",
        "|---|---|---|---|",
    ]
    for name, row in agg["tasks"].items():
        lines.append(f"| {name} | {row['passes']}/{row['n']} | {_fmt(row['pass@1'])} | {_fmt(row[f'pass^{k}'])} |")
    skipped = sorted({r["task"] for r in records if r.get("result") == "skipped"})
    errors = [r for r in records if r.get("result") == "error"]
    if skipped:
        lines += ["", "Skipped: " + ", ".join(skipped)]
    if errors:
        lines += ["", "Errors:"] + [f"- {r['task']} #{r['run']}: {r.get('why', '')}" for r in errors]
    failures = [r for r in records if r.get("result") == "fail"]
    if failures:
        lines += ["", "Failures:"] + [f"- {r['task']} #{r['run']}: {r.get('why', '')}" for r in failures]
    return "\n".join(lines) + "\n"


def read_results(folder: Path) -> list[dict[str, Any]]:
    path = folder / "results.jsonl" if folder.is_dir() else folder
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def render_compare(a_records: list[dict[str, Any]], b_records: list[dict[str, Any]], a_name: str, b_name: str) -> str:
    a, b = aggregate(a_records), aggregate(b_records)
    a_rate = {name: row["pass@1"] for name, row in a["tasks"].items()}
    b_rate = {name: row["pass@1"] for name, row in b["tasks"].items()}
    diff, low, high, n = bootstrap_diff(a_rate, b_rate)
    lines = [
        f"# {b_name} vs {a_name}",
        "",
        f"pass@1 difference ({b_name} − {a_name}) over {n} shared tasks: "
        f"{diff * 100:+.1f} points (90% interval {low * 100:+.1f} to {high * 100:+.1f})",
        "",
        f"| metric | {a_name} | {b_name} |",
        "|---|---|---|",
    ]
    for key in ("pass@1", "median_seconds", "median_turns", "completion_tokens_per_solved", "cache_hit_ratio",
                "malformed_per_turn", "tool_errors_per_turn", "edit_success_rate"):
        lines.append(f"| {key} | {_fmt(a['suite'].get(key))} | {_fmt(b['suite'].get(key))} |")
    lines += ["", f"| task | {a_name} pass@1 | {b_name} pass@1 | change |", "|---|---|---|---|"]
    for name in sorted(set(a_rate) | set(b_rate)):
        left, right = a_rate.get(name), b_rate.get(name)
        change = "" if left is None or right is None else f"{(right - left) * 100:+.0f}"
        lines.append(f"| {name} | {_fmt(left)} | {_fmt(right)} | {change} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------- audit


def audit_task(task: Task) -> list[str]:
    """Problems with a task's grader: it must fail untouched and pass with the reference."""
    spec = task.spec
    problems: list[str] = []
    if not spec.get("prompt"):
        problems.append("no prompt")
    if not spec.get("check") and not spec.get("answer"):
        problems.append("no check and no answer")
    if spec.get("answer"):
        try:
            pattern = re.compile(spec["answer"], re.IGNORECASE | re.DOTALL)
        except re.error as exc:
            return problems + [f"answer is not a valid regex: {exc}"]
        if pattern.search(""):
            problems.append("answer matches an empty reply")
        reference = spec.get("reference_answer")
        if not reference:
            problems.append("answer task without reference_answer")
        elif not pattern.search(reference):
            problems.append("reference_answer does not match answer")
    if not spec.get("check"):
        return problems
    if missing(spec):
        return problems
    if not spec.get("reference"):
        return problems + ["check task without reference"]
    with tempfile.TemporaryDirectory(prefix=f"crit-audit-{task.name}-") as tmp:
        if spec.get("workspace"):
            return problems  # an existing tree: nothing to set up or reset
        work, error = prepare(task, Path(tmp) / "untouched")
        if error:
            return problems + [error]
        add_hidden(task, work)
        code, _ = run_shell(spec["check"], work, CHECK_TIMEOUT)
        if code == 0:
            problems.append("check passes on the untouched workspace")
        work, error = prepare(task, Path(tmp) / "reference")
        if error:
            return problems + [error]
        trouble = apply_reference(task, work)
        if trouble:
            return problems + [trouble]
        add_hidden(task, work)
        code, text = run_shell(spec["check"], work, CHECK_TIMEOUT)
        if code != 0:
            problems.append(f"check fails with the reference (exit {code}): {text[-300:]}")
    return problems


# ---------------------------------------------------------------------------- command line


def _meta(runner: Runner, args: argparse.Namespace) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "runner": runner.name,
        "runner_version": runner.version(),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "runs": args.runs,
        "suite": str(args.suite),
    }
    if getattr(args, "model", ""):
        meta["model"] = args.model
    if getattr(args, "config", ""):
        meta["config"] = str(args.config)
    if getattr(args, "packs", ""):
        meta["packs"] = args.packs
    gpu = shutil.which("nvidia-smi")
    if gpu:
        try:
            names = subprocess.run(
                [gpu, "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True, timeout=10
            ).stdout.strip()
            if names:
                meta["gpu"] = ", ".join(names.splitlines())
        except (OSError, subprocess.TimeoutExpired):
            pass
    if args.note:
        meta["note"] = args.note
    return meta


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crit eval", description="Measure a coding agent on a suite of real tasks.")
    sub = parser.add_subparsers(dest="command")

    def suite_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--suite", "--tasks", type=Path, default=DEFAULT_SUITE, help="folder of task folders")
        p.add_argument("--only", default="", help="comma-separated task names")
        p.add_argument("--tag", action="append", default=[], help="only tasks with this tag (repeatable)")

    run = sub.add_parser("run", help="run the suite (the default)")
    suite_args(run)
    run.add_argument("--runs", type=int, default=1, help="trials per task (3 gives pass^3)")
    run.add_argument("--runner", choices=("crit", "opencode"), default="crit")
    run.add_argument("--config", default="", help="crit config file naming the model server")
    run.add_argument("--crit", default="", help="the crit command (default: this checkout)")
    run.add_argument("--opencode", default="", help="opencode command template ({prompt}, {model_flag})")
    run.add_argument("--model", default="", help="model name for the runner (opencode: provider/model)")
    run.add_argument("--timeout", type=float, default=900, help="seconds per trial unless the task says")
    run.add_argument("--out", type=Path, default=None, help="results folder (default: .crit/eval/<time>)")
    run.add_argument("--keep-home", action="store_true", help="crit: use your CRIT_HOME instead of a fresh one")
    run.add_argument("--ask-commands", action="store_true",
                     help="crit: keep asking before commands (default: the throwaway workspace allows them all)")
    run.add_argument("--note", default="", help="free text saved in meta.json (server, quant, flags)")
    run.add_argument("--packs", default="", help="crit: domain packs to turn on in each trial, e.g. aosp")

    listing = sub.add_parser("list", help="list the tasks and whether they can run here")
    suite_args(listing)
    audit = sub.add_parser("audit", help="check every grader against the untouched workspace and the reference")
    suite_args(audit)
    compare = sub.add_parser("compare", help="compare two result folders")
    compare.add_argument("a", type=Path)
    compare.add_argument("b", type=Path)
    compare.add_argument("--out", type=Path, default=None, help="also write the table to this file")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in {"run", "list", "audit", "compare", "-h", "--help"}:
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)
    if args.command == "compare":
        text = render_compare(read_results(args.a), read_results(args.b), args.a.name, args.b.name)
        if args.out:
            args.out.write_text(text, encoding="utf-8")
        print(text)
        return 0
    tasks = load_tasks(args.suite, {i for i in args.only.split(",") if i}, set(args.tag))
    if not tasks:
        print(f"no tasks in {args.suite}" + (" matching the filters" if args.only or args.tag else ""))
        return 2
    if args.command == "list":
        for task in tasks:
            gone = missing(task.spec)
            tags = ",".join(sorted(task.tags))
            state = "missing " + ", ".join(gone) if gone else "ready"
            print(f"{task.name:30} {task.spec.get('category', ''):9} {task.spec.get('difficulty', ''):7} {tags:28} {state}")
        return 0
    if args.command == "audit":
        bad = skipped = 0
        for task in tasks:
            if missing(task.spec):
                print(f"{task.name}: skipped (missing {', '.join(missing(task.spec))})")
                skipped += 1
                continue
            problems = audit_task(task)
            print(f"{task.name}: {'ok' if not problems else '; '.join(problems)}", flush=True)
            bad += bool(problems)
        print(f"\n{len(tasks) - bad - skipped} ok, {bad} with problems, {skipped} skipped")
        return 1 if bad else 0
    runner: Runner
    if args.runner == "opencode":
        runner = OpencodeRunner(args.opencode, args.model)
    else:
        runner = CritRunner(args.crit, args.config, args.keep_home, args.ask_commands,
                            tuple(item.strip() for item in args.packs.split(",") if item.strip()))
    out_root = args.out or (Path.cwd() / ".crit" / "eval" / time.strftime("%Y%m%d-%H%M%S"))
    out_root.mkdir(parents=True, exist_ok=True)
    meta = _meta(runner, args)
    (out_root / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    records = []
    with open(out_root / "results.jsonl", "a", encoding="utf-8") as sink:
        for task in tasks:
            for run_number in range(1, args.runs + 1):
                record = run_trial(task, run_number, runner, out_root, timeout=args.timeout)
                records.append(record)
                sink.write(json.dumps(record) + "\n")
                sink.flush()
                print(
                    f"{task.name} #{run_number}: {record['result']} {record.get('seconds', '')}s "
                    f"turns={record.get('model_requests') or record.get('turns', '')} {record.get('why', '')}",
                    flush=True,
                )
    text = render_summary(records, meta)
    (out_root / "summary.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"results in {out_root}")
    return 0 if all(r["result"] != "fail" for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
