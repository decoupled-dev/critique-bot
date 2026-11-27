"""``coverage_locate``: rank functions by what a failing run executed (spectrum-based).

Sources of coverage, per run:

- a Python command (``python -m unittest ...``, ``python -m pytest ...``, ``pytest ...``,
  ``python script.py``): run under :mod:`.pytrace` (standard library, no coverage.py needed);
- a report another tool already wrote: coverage.py JSON, JaCoCo XML, or LCOV (``llvm-cov
  export -format=lcov``, ``genhtml`` input, ``gcovr --lcov``). For Gradle/JaCoCo or native
  code, run the build's coverage task, then pass its report.

With passing runs as well, each line gets the Ochiai score
``failed(line) / sqrt(total_failed * (failed(line) + passed(line)))``; a function scores its
best line. Without passing runs every executed line scores the same, and functions are
ordered by how few lines they executed (small, specific functions first).
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

Lines = dict[str, set[int]]


@dataclass
class Run:
    label: str
    lines: Lines
    exit: int | None = None
    output: str = ""
    error: str = ""


# --------------------------------------------------------------------------- reports


def read_report(path: Path, workspace: Path, known: set[str]) -> Lines:
    """Executed lines per workspace-relative file from a coverage.py JSON, JaCoCo XML or LCOV file."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        data = json.loads(text)
        files = data.get("files", {})
        out: Lines = {}
        for name, item in files.items():
            lines = item.get("executed_lines", item) if isinstance(item, dict) else item
            out[_rel(name, workspace, known)] = set(int(n) for n in lines)
        return out
    if stripped.startswith("<"):
        return _jacoco(text, known)
    return _lcov(text, workspace, known)


def _rel(name: str, workspace: Path, known: set[str]) -> str:
    candidate = Path(name)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(Path(workspace).resolve()).as_posix()
        except (OSError, ValueError):
            pass
    name = name.replace("\\", "/").lstrip("./")
    if name in known:
        return name
    parts = name.split("/")
    for start in range(len(parts)):  # /build/native/fan.c -> native/fan.c -> fan.c
        tail = "/".join(parts[start:])
        matches = [item for item in known if item == tail or item.endswith("/" + tail)]
        if matches:
            return min(matches, key=len)
    return name


def _jacoco(text: str, known: set[str]) -> Lines:
    out: Lines = {}
    if re.search(r"<!(?:DOCTYPE|ENTITY)", text, re.I):  # no entity expansion: drop the DTD JaCoCo writes
        text = re.sub(r"<!DOCTYPE[^>]*>", "", text)
        if re.search(r"<!ENTITY", text, re.I):
            raise ValueError("coverage report declares XML entities; refusing to parse it")
    root = ET.fromstring(text)  # nosec B314: DTD removed and entities refused above
    for package in root.iter("package"):
        folder = package.get("name", "")
        for source in package.iter("sourcefile"):
            tail = f"{folder}/{source.get('name', '')}".strip("/")
            matches = [item for item in known if item == tail or item.endswith("/" + tail)]
            rel = min(matches, key=len) if matches else tail
            lines = {int(line.get("nr", "0")) for line in source.iter("line") if int(line.get("ci", "0")) > 0}
            if lines:
                out.setdefault(rel, set()).update(lines)
    return out


def _lcov(text: str, workspace: Path, known: set[str]) -> Lines:
    out: Lines = {}
    current = ""
    for raw in text.splitlines():
        if raw.startswith("SF:"):
            current = _rel(raw[3:].strip(), workspace, known)
        elif raw.startswith("DA:") and current:
            number, _, count = raw[3:].partition(",")
            if count.split(",")[0].strip() not in {"0", ""}:
                out.setdefault(current, set()).add(int(number))
        elif raw.startswith("end_of_record"):
            current = ""
    return out


# --------------------------------------------------------------------------- running


def python_argv(command: str) -> list[str] | None:
    """The arguments after ``python`` for a Python test command, or None for other commands."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not words:
        return None
    program = Path(words[0]).name
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", program):
        rest = words[1:]
        while rest and rest[0] in {"-u", "-B", "-I", "-E", "-s", "-q"}:
            rest = rest[1:]
        return rest or None
    if program in {"pytest", "py.test"}:
        return ["-m", "pytest", *words[1:]]
    return None


def run_python(command: str, workspace: Path, *, timeout: float, env: dict[str, str] | None = None) -> Run:
    argv = python_argv(command)
    if argv is None:
        return Run(command, {}, error="not a Python command; pass a coverage report (report=...) for other languages")
    tracer = Path(__file__).with_name("pytrace.py")
    with tempfile.TemporaryDirectory(prefix="crit-cov-") as tmp:
        out = Path(tmp) / "lines.json"
        python = sys.executable
        words = shlex.split(command)
        if Path(words[0]).name.startswith("python") and "/" in words[0]:
            python = words[0]
        try:
            proc = subprocess.run([python, str(tracer), str(out), str(workspace), "--", *argv], cwd=str(workspace),
                                  capture_output=True, text=True, errors="replace", timeout=timeout,
                                  env={**os.environ, **(env or {}), "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
        except subprocess.TimeoutExpired:
            return Run(command, {}, error=f"timed out after {int(timeout)}s")
        except OSError as exc:
            return Run(command, {}, error=str(exc))
        if not out.is_file():
            return Run(command, {}, proc.returncode, (proc.stdout + proc.stderr)[-2000:], "the run recorded no lines")
        data = json.loads(out.read_text(encoding="utf-8"))
    lines = {name: set(values) for name, values in data.get("files", {}).items()}
    return Run(command, lines, int(data.get("exit", proc.returncode) or 0), (proc.stdout + proc.stderr)[-2000:])


# --------------------------------------------------------------------------- scoring


@dataclass
class Ranked:
    path: str
    symbol: str
    line: int
    end_line: int
    score: float
    executed: int


def _is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return bool(re.search(r"(^|/)(tests?|testing|androidTest)/", path)
                or re.search(r"(^|_)tests?(_|\.)|Tests?\.\w+$|_test\.\w+$|^conftest\.py$", name))


def ochiai(failing: list[Lines], passing: list[Lines]) -> dict[tuple[str, int], float]:
    total_failed = max(1, len(failing))
    fail_count: dict[tuple[str, int], int] = {}
    pass_count: dict[tuple[str, int], int] = {}
    for run in failing:
        for path, lines in run.items():
            for line in lines:
                fail_count[(path, line)] = fail_count.get((path, line), 0) + 1
    for run in passing:
        for path, lines in run.items():
            for line in lines:
                pass_count[(path, line)] = pass_count.get((path, line), 0) + 1
    return {
        key: failed / math.sqrt(total_failed * (failed + pass_count.get(key, 0)))
        for key, failed in fail_count.items()
    }


def rank_functions(conn: sqlite3.Connection, scores: dict[tuple[str, int], float], *, include_tests: bool = False,
                   limit: int = 15) -> list[Ranked]:
    by_file: dict[str, dict[int, float]] = {}
    for (path, line), score in scores.items():
        by_file.setdefault(path, {})[line] = score
    out: list[Ranked] = []
    for path, lines in by_file.items():
        if not include_tests and _is_test(path):
            continue
        rows = conn.execute(
            "SELECT name, parent, line, end_line, kind FROM symbols WHERE path = ? "
            "AND kind NOT IN ('class','interface','struct','enum','trait','object','impl','union') ORDER BY line",
            (path,),
        ).fetchall()
        claimed: set[int] = set()
        # innermost first: a nested function claims its own lines
        for name, parent, start, end, _kind in sorted(rows, key=lambda r: (int(r[3]) - int(r[2]))):
            inside = [line for line in lines if int(start) <= line <= int(end) and line not in claimed]
            if not inside:
                continue
            claimed.update(inside)
            best = max(lines[line] for line in inside)
            symbol = f"{parent}.{name}" if parent else str(name)
            out.append(Ranked(path, symbol, int(start), int(end), round(best, 3), len(inside)))
        rest = [line for line in lines if line not in claimed]
        if rest and not rows:
            out.append(Ranked(path, "", min(rest), max(rest), round(max(lines[line] for line in rest), 3), len(rest)))
    # module-level code (imports, constants) runs in every test: functions first
    out.sort(key=lambda item: (-item.score, not item.symbol, item.executed, item.path, item.line))
    return out[:limit]


def stamp() -> float:
    return time.monotonic()
