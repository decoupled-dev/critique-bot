# SPDX-License-Identifier: Apache-2.0
"""M3: episodes. What a finished task did, written from its Stage 3 event log.

:func:`build` takes the task's events (plain dicts, blobs resolved by the ``load`` it is given)
and returns the episode as a dict: the task, its class, the files changed, symbols, the root
cause and fix summary when the final message states them (found heuristically), the evidence
(tests and commands run after the last edit), the outcome, and the session/commit ids with the
hashes of the changed files.

- ``verified``: a test or build command passed after the last edit; ``accepted``: the person
  approved the edits (ask mode); ``completed``: finished without either. These are positive.
- ``failed`` / ``abandoned``: kept as **negative** episodes ("tried X, it was not the cause").
- A tainted session's episode is stored with ``trusted = 0`` and is not recalled by default.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - git rev-parse with a fixed argv
from pathlib import Path
from typing import Any, Callable, Iterable

from critique_bot.memory.cards import file_hash

POSITIVE_CODES = frozenset({"COMPLETED", "FINISHED", "DONE"})
NEGATIVE_CODES = frozenset({"FAILED", "BLOCKED"})
ABANDONED_CODES = frozenset({"INTERRUPTED", "STOPPED", "MAX_ROUNDS", "TIMEOUT"})
MUTATING = frozenset({"write_files", "edit_file", "delete_file", "apply_patch", "move_file"})
TEST_COMMAND = re.compile(
    r"(?i)(\btest\b|pytest|unittest|\btox\b|gradlew?\b.*\b(test|check|assemble|build)|\batest\b|\bctest\b|go\s+(test|vet|build)|"
    r"cargo\s+(test|build|check)|npm\s+(run\s+)?test|yarn\s+test|mvn\b|make\b|\bm\s|\bmm\b|mypy|ruff|lint|tsc\b|javac|kotlinc)"
)
_EXIT = re.compile(r"^\s*exit\s+(-?\d+)")
_IDENT = re.compile(r"`([A-Za-z_][\w.:$]*)`|\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w+)+|[a-z]+[A-Z]\w*|[A-Z][a-z0-9]+[A-Z]\w*|[a-z]+_[a-z0-9_]+)\b")

_CAUSE = (
    re.compile(r"(?im)^[\s>*#-]*(?:\*\*)?(?:root cause|cause|the (?:problem|bug|issue) was|why it (?:broke|failed))(?:\*\*)?\s*[:\-–]\s*(.{8,})$"),
    re.compile(r"(?i)\b(?:the (?:root )?cause (?:was|is)|was caused by|the (?:problem|bug|issue) (?:was|is) that|this happened because|it failed because)\s+(.{8,}?)(?:[.!\n]|$)"),
)
_FIX = (
    re.compile(r"(?im)^[\s>*#-]*(?:\*\*)?(?:fix|the fix|fixed|solution|change|changes|what i changed)(?:\*\*)?\s*[:\-–]\s*(.{8,})$"),
    re.compile(r"(?i)\b(?:i fixed (?:it|this|the \w+) by|fixed by|the fix (?:is|was) to|i changed|i made)\s+(.{8,}?)(?:[.!\n]|$)"),
)
_CLASSES = (
    ("question", re.compile(r"(?i)^(what|how|why|where|which|who|explain|describe|summari[sz]e|list)\b|\?\s*$")),
    ("bugfix", re.compile(r"(?i)\b(fix|bug|crash|wrong|broken|error|fail(s|ed|ing)?|regression|incorrect|doesn'?t|not work|exception|npe|anr|leak)\b")),
    ("test", re.compile(r"(?i)\b(add|write)\b.*\btests?\b|\bcoverage\b")),
    ("refactor", re.compile(r"(?i)\b(refactor|rename|move|extract|clean ?up|split|simplif)")),
    ("build", re.compile(r"(?i)\b(build|gradle|cmake|android\.bp|soong|compile|dependency|dependencies|makefile|bazel)\b")),
    ("docs", re.compile(r"(?i)\b(docs?|readme|documentation|comment)\b")),
    ("feature", re.compile(r"(?i)\b(add|implement|support|new|create|introduce|allow|enable)\b")),
)


def classify(task: str, *, edited: bool = True) -> str:
    text = task.strip()
    for name, pattern in _CLASSES:
        if name == "question" and edited:
            continue
        if pattern.search(text):
            return name
    return "question" if not edited else "change"


def _first(patterns: Iterable[re.Pattern[str]], text: str) -> str:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            found = re.sub(r"\s+", " ", match.group(1)).strip(" *_`.")
            return found[:300]
    return ""


def root_cause(text: str) -> str:
    """The cause a final message states ("Root cause: ...", "... was caused by ..."), or ""."""
    return _first(_CAUSE, text or "")


def fix_summary(text: str) -> str:
    """The fix a final message states ("Fix: ...", "I fixed it by ..."), or ""."""
    return _first(_FIX, text or "")


def identifiers(text: str) -> list[str]:
    out = []
    for match in _IDENT.finditer(text or ""):
        name = (match.group(1) or match.group(2) or "").strip(".:")
        if len(name) >= 4 and not re.match(r"(?i)^(e\.g|i\.e|etc)\b", name) and "/" not in name and name not in out:
            if re.search(r"\.(py|java|kt|go|rs|c|cc|cpp|h|ts|js|md|txt|json|xml)$", name):
                continue
            out.append(name)
    return out


def task_events(events: list[dict[str, Any]], task: str = "") -> list[dict[str, Any]]:
    """The events of the last task (from its ``user_message`` to its ``task_finished``)."""
    start = 0
    for index, event in enumerate(events):
        if event.get("type") == "user_message" and event.get("task"):
            start = index
    out = events[start:]
    for index, event in enumerate(out):
        if event.get("type") == "task_finished":
            return out[: index + 1]
    return out


def git_head(workspace: Path) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--short=12", "HEAD"],  # nosec B603 B607
                              capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def build(events: list[dict[str, Any]], *, workspace: Path, session: str = "", code: str = "", task: str = "",
          changed: list[str] | None = None, tainted: bool = False, load: Callable[[Any], Any] = lambda value: value,
          defined: Callable[[str], bool] | None = None) -> dict[str, Any] | None:
    """The episode of the task whose events these are; None for a task that changed nothing and
    asked nothing worth remembering (a pure question)."""
    mine = task_events(events, task)
    finished = next((e for e in reversed(mine) if e.get("type") == "task_finished"), {})
    code = (code or str(finished.get("code") or "")).upper()
    task = task or str(load(next((e.get("text") for e in mine if e.get("type") == "user_message"), "")) or "")
    changed = list(changed if changed is not None else (finished.get("changed") or []))
    final = ""
    last_edit = -1
    commands: list[tuple[int, str, int | None]] = []
    approved = False
    nav_names: list[str] = []
    calls: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(mine):
        kind = event.get("type")
        if kind == "assistant_turn":
            text = str(load(event.get("text")) or "")
            if text.strip():
                final = text
        elif kind == "tool_started":
            args = load(event.get("arguments"))
            args = args if isinstance(args, dict) else {}
            calls[str(event.get("call_id") or index)] = {"tool": event.get("tool"), "args": args}
            if event.get("tool") in {"code_nav", "code_history"} and isinstance(args.get("name"), str):
                nav_names.append(args["name"])
        elif kind == "tool_result":
            tool = str(event.get("tool") or "")
            if (event.get("mutating") or tool in MUTATING) and event.get("ok"):
                last_edit = index
            if tool == "run_command":
                started = calls.get(str(event.get("call_id") or ""), {})
                command = str((started.get("args") or {}).get("command") or "")
                match = _EXIT.match(str(load(event.get("summary")) or ""))
                exit_code = int(match.group(1)) if match else (0 if event.get("ok") else None)
                commands.append((index, command, exit_code))
        elif kind == "permission_decision":
            if event.get("kind") == "edit" and event.get("allowed") and str(event.get("mode") or "") == "ask":
                approved = True
    if not changed and not final:
        return None
    after = [(command, exit_code) for index, command, exit_code in commands if index > last_edit and TEST_COMMAND.search(command)]
    passed = [command for command, exit_code in after if exit_code == 0]
    failing = [command for command, exit_code in after if exit_code not in (0, None)]
    if code in POSITIVE_CODES:
        last_passed = bool(after) and after[-1][1] == 0
        outcome = "verified" if changed and last_passed else ("accepted" if changed and approved else "completed")
        kind = "positive"
    elif code in NEGATIVE_CODES:
        outcome, kind = "failed", "negative"
    else:
        outcome, kind = "abandoned", "negative"
    if not changed and kind == "positive" and not (root_cause(final) or fix_summary(final)):
        return None  # a question answered: nothing to localize next time
    names = identifiers(task + "\n" + final) + [name for name in nav_names if name not in identifiers(final)]
    symbols = [name for name in dict.fromkeys(names) if defined is None or defined(name)][:12]
    hashes = {}
    for rel in changed[:30]:
        digest = file_hash(Path(workspace) / rel)
        hashes[rel] = digest or "deleted"
    evidence: dict[str, Any] = {}
    if passed:
        evidence["tests"] = passed[-3:]
    if failing:
        evidence["failing"] = failing[-3:]
    if commands and not after:
        evidence["commands"] = [command for _i, command, _e in commands[-3:]]
    if approved:
        evidence["approved"] = "edits approved in ask mode"
    episode = {
        "task": task.strip(),
        "class": classify(task, edited=bool(changed)),
        "kind": kind,
        "outcome": outcome,
        "trusted": not tainted,
        "files_changed": changed,
        "symbols": symbols,
        "root_cause": root_cause(final),
        "fix_summary": fix_summary(final),
        "evidence": evidence,
        "session": session,
        "commit": git_head(Path(workspace)),
        "files_hash": hashes,
        "provenance": f"session:{session}" if session else "",
    }
    if kind == "negative" and not episode["fix_summary"]:
        tail = re.sub(r"\s+", " ", final).strip()[:200]
        if tail:
            episode["fix_summary"] = "tried: " + tail
    return episode
