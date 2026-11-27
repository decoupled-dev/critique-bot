# SPDX-License-Identifier: Apache-2.0
"""Log analysis for logs of millions of lines: summary, query, around, diff.

The model asks one question ("what went wrong in this log?") and gets a short answer
built by crit in one streaming pass, instead of grepping and paging through raw lines:

- **summary**: the format, size and time span; whole *events* (an app crash with its
  stack, an ANR, a native crash, lmkd kills, SELinux denials, build errors); the error
  and warning lines grouped into *templates* (numbers, ids and paths masked), so a
  million lines become a few hundred groups with counts and line numbers; counts by
  level, tag and pid.
- **query**: lines filtered by level, tag, pid, package, text, and line range, paged.
- **around**: the lines around a line number (an event's context).
- **diff**: templates that are new or much more frequent in a bad log than a good one.

Memory stays flat: the file is streamed, at most ``MAX_TEMPLATES`` groups are kept, and
a summary is cached per file version. Formats: Android logcat (threadtime, brief, with
or without year), kernel (dmesg), ninja/Soong and Gradle build output; anything else is
grouped as plain text.
"""

from __future__ import annotations

import collections
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from critique_bot import big_file

MAX_TEMPLATES = 20_000
MAX_EVENTS = 40
EVENT_LINES = 24
LINE_CHARS = 300
SUMMARY_SECONDS = 600.0

# 11-25 10:03:07.123  1234  1241 E Tag: message   (threadtime, optional year)
_THREADTIME = re.compile(
    r"^(?P<time>(?:\d{4}-)?\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+(?:\S+\s+)?(?P<pid>\d+)\s+(?P<tid>\d+)\s+"
    r"(?P<level>[VDIWEFA])\s+(?P<tag>[^:]*?)\s*:\s(?P<msg>.*)$"
)
# E/Tag( 1234): message   (brief)
_BRIEF = re.compile(r"^(?P<level>[VDIWEFA])/(?P<tag>[^(]+?)\(\s*(?P<pid>\d+)\):\s(?P<msg>.*)$")
_KERNEL = re.compile(r"^(?:<\d+>)?\[\s*(?P<time>\d+\.\d+)\]\s(?P<msg>.*)$")
_NINJA = re.compile(r"^\[\s*\d+% \d+/\d+\]")
_LEVELS = "VDIWEFA"
_RANK = {"V": 0, "D": 1, "I": 2, "W": 3, "E": 4, "F": 5, "A": 5}

_MASKS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"(?:/[\w.@+-]+){2,}"), "<path>"),
    (re.compile(r"'[^']{0,200}'|\"[^\"]{0,200}\""), "<str>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
]

# What starts an event, and how to name it. The pattern is matched on the message.
_EVENT_STARTS = [
    ("app crash", re.compile(r"FATAL EXCEPTION")),
    ("ANR", re.compile(r"\bANR in\b|am_anr")),
    ("native crash", re.compile(r"\*\*\* \*\*\* \*\*\*|Fatal signal \d+")),
    ("app died", re.compile(r"\bam_crash\b|has died\b|Process .* \(pid \d+\) has died")),
    ("low-memory kill", re.compile(r"lowmemorykiller|lmkd.*\bKill\b|\bkilling\b.*\badj\b", re.I)),
    ("watchdog", re.compile(r"\bWatchdog\b.*(?:WATCHDOG|blocked|Killing)", re.I)),
    ("SELinux denial", re.compile(r"avc:\s+denied")),
    ("system server crash", re.compile(r"\*\*\* FATAL EXCEPTION IN SYSTEM PROCESS")),
    ("build failure", re.compile(r"^FAILED: |BUILD FAILED|ninja failed with|failed to build some targets|What went wrong:")),
    ("compiler error", re.compile(r"^\S+:\d+(?::\d+)?: error: |^e: \S+:\d+")),
]
_STACK_LINE = re.compile(r"^\s*(?:at |Caused by:|\.\.\. \d+ more|#\d+ pc |backtrace:|signal \d+|Process:|PID:|java\.|kotlin\.|android\.|Abort message)")


@dataclass
class Line:
    number: int
    text: str
    level: str = ""
    tag: str = ""
    pid: str = ""
    time: str = ""
    msg: str = ""


def parse(number: int, text: str, kind: str) -> Line:
    if kind in {"logcat", "logcat-brief"}:
        match = (_THREADTIME if kind == "logcat" else _BRIEF).match(text)
        if match:
            groups = match.groupdict()
            return Line(number, text, groups["level"], groups["tag"].strip(), groups.get("pid") or "",
                        groups.get("time") or "", groups["msg"])
    elif kind == "kernel":
        match = _KERNEL.match(text)
        if match:
            msg = match.group("msg")
            level = "E" if re.search(r"\b(error|fail|panic|oops|bug:)\b", msg, re.I) else "I"
            return Line(number, text, level, "kernel", "", match.group("time"), msg)
    level = ""
    if re.search(r"\berror\b|\bFAILED\b|\bfatal\b|exception", text, re.I):
        level = "E"
    elif re.search(r"\bwarning\b|\bwarn\b", text, re.I):
        level = "W"
    return Line(number, text, level, "", "", "", text)


def sniff(path: Path) -> str:
    """The log format, from its first lines: logcat, logcat-brief, kernel, build, or text."""
    votes: collections.Counter[str] = collections.Counter()
    for _number, text in _iter_lines(path, limit=400):
        if _THREADTIME.match(text):
            votes["logcat"] += 1
        elif _BRIEF.match(text):
            votes["logcat-brief"] += 1
        elif _KERNEL.match(text):
            votes["kernel"] += 1
        elif _NINJA.match(text) or text.startswith(("> Task ", "FAILURE: Build failed", "BUILD SUCCESSFUL")):
            votes["build"] += 1
    if not votes:
        return "text"
    kind, count = votes.most_common(1)[0]
    return kind if count >= 3 else "text"


def _iter_lines(path: Path, *, start: int = 1, limit: int | None = None) -> Iterator[tuple[int, str]]:
    if start > 1:
        index = big_file.line_index(path)
        checkpoint = min((start - 1) // big_file.STEP, len(index.offsets) - 1)
        number = checkpoint * big_file.STEP + 1
        offset = index.offsets[checkpoint]
    else:
        number, offset = 1, 0
    with open(path, "rb") as handle:
        handle.seek(offset)
        for raw in handle:
            if number >= start:
                yield number, raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if limit is not None and number - start + 1 >= limit:
                    return
            number += 1


def template(msg: str) -> str:
    text = msg[:400]
    for pattern, mark in _MASKS:
        text = pattern.sub(mark, text)
    return text


@dataclass
class Group:
    template: str
    level: str
    tag: str
    count: int = 0
    first: int = 0
    last: int = 0
    example: str = ""


@dataclass
class Event:
    kind: str
    line: int
    lines: list[str] = field(default_factory=list)
    pid: str = ""


@dataclass
class Summary:
    path: str
    kind: str
    lines: int = 0
    size: int = 0
    first_time: str = ""
    last_time: str = ""
    levels: collections.Counter = field(default_factory=collections.Counter)
    tags: collections.Counter = field(default_factory=collections.Counter)
    error_tags: collections.Counter = field(default_factory=collections.Counter)
    pids: collections.Counter = field(default_factory=collections.Counter)
    groups: dict[tuple[str, str, str], Group] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    event_counts: collections.Counter = field(default_factory=collections.Counter)
    overflow: int = 0
    partial: str = ""


_CACHE: dict[tuple[str, int, int], Summary] = {}
_CACHE_LOCK = threading.Lock()


def summarize(path: Path, *, cancel: threading.Event | None = None) -> Summary:
    """One streaming pass over ``path``; cached per file version."""
    path = Path(path)
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    kind = sniff(path)
    result = Summary(str(path), kind, size=stat.st_size)
    open_event: Event | None = None
    deadline = time.monotonic() + SUMMARY_SECONDS
    for number, text in _iter_lines(path):
        result.lines = number
        if number % 100_000 == 0 and ((cancel is not None and cancel.is_set()) or time.monotonic() > deadline):
            result.partial = f"stopped at line {number}"
            break
        item = parse(number, text, kind)
        if item.time:
            if not result.first_time:
                result.first_time = item.time
            result.last_time = item.time
        if item.level:
            result.levels[item.level] += 1
        if item.tag:
            result.tags[item.tag] += 1
        if item.pid:
            result.pids[item.pid] += 1
        # an event collects the lines that belong to it (a stack under a crash)
        if open_event is not None:
            if len(open_event.lines) < EVENT_LINES and _continues(open_event, item):
                open_event.lines.append(_clip(text))
                continue
            open_event = None
        kind_name = _event_kind(item.msg if item.msg else text)
        if kind_name:
            result.event_counts[kind_name] += 1
            if len(result.events) < MAX_EVENTS and _new_event(result, kind_name, item):
                open_event = Event(kind_name, number, [_clip(text)], item.pid)
                result.events.append(open_event)
        if _RANK.get(item.level, 0) >= 3:
            if item.level in "EFA" and item.tag:
                result.error_tags[item.tag] += 1
            group_key = (item.level, item.tag, template(item.msg or text))
            group = result.groups.get(group_key)
            if group is None:
                if len(result.groups) >= MAX_TEMPLATES:
                    result.overflow += 1
                    continue
                group = Group(group_key[2], item.level, item.tag, first=number, example=_clip(text))
                result.groups[group_key] = group
            group.count += 1
            group.last = number
    with _CACHE_LOCK:
        if len(_CACHE) > 8:
            _CACHE.pop(next(iter(_CACHE)))
        if not result.partial:
            _CACHE[key] = result
    return result


def _clip(text: str) -> str:
    return text if len(text) <= LINE_CHARS else text[:LINE_CHARS] + f" …[+{len(text) - LINE_CHARS} chars]"


def _event_kind(msg: str) -> str:
    for name, pattern in _EVENT_STARTS:
        if pattern.search(msg):
            return name
    return ""


def _new_event(result: Summary, kind: str, item: Line) -> bool:
    """Same kind and pid as the last event within 3 lines is the same event (a crash logs
    "FATAL EXCEPTION" and "am_crash" together)."""
    if not result.events:
        return True
    last = result.events[-1]
    return not (last.kind == kind and last.pid == item.pid and item.number - last.line <= 3)


def _continues(event: Event, item: Line) -> bool:
    if event.pid and item.pid and item.pid == event.pid and item.level in {"E", "F", "A"}:
        return True
    if _STACK_LINE.match(item.msg or item.text):
        return True
    if event.kind in {"compiler error", "build failure"}:
        return item.text.startswith((" ", "\t", "  symbol:", "  location:")) or item.text.strip() in {"^", "1 error"}
    return False


# --------------------------------------------------------------------------- rendering


def render_summary(summary: Summary, *, max_chars: int = 12_000, name: str = "") -> str:
    """The summary as compact text, most useful first; fits ``max_chars``."""
    out: list[str] = []
    span = f", {summary.first_time} .. {summary.last_time}" if summary.first_time else ""
    out.append(
        f"LOG {name or summary.path}: {summary.kind}, {summary.lines} lines, {big_size(summary.size)}{span}"
        + (f" ({summary.partial})" if summary.partial else "")
    )
    if summary.levels:
        out.append("levels: " + ", ".join(f"{level}={summary.levels[level]}" for level in _LEVELS if summary.levels[level]))
    if summary.event_counts:
        out.append("EVENTS: " + ", ".join(f"{kind} x{count}" for kind, count in summary.event_counts.most_common()))
        for event in summary.events[:12]:
            out.append(f"- {event.kind} at line {event.line}:")
            out.extend("    " + line for line in event.lines[:EVENT_LINES])
        if len(summary.events) > 12:
            out.append(f"  ... {len(summary.events) - 12} more events: log action \"query\" with text, or \"around\" a line")
    groups = sorted(
        summary.groups.values(),
        key=lambda g: (-_RANK.get(g.level, 0), g.count > 50, g.first),
    )
    errors = [g for g in groups if g.level in "EFA"]
    warnings = [g for g in groups if g.level == "W"]
    if errors:
        out.append(f"ERRORS ({len(errors)} kinds; rare ones first, count · first line · tag · template):")
        for group in sorted(errors, key=lambda g: (g.count, g.first))[:25]:
            out.append(f"  x{group.count} · L{group.first} · {group.tag or '-'} · {group.template[:180]}")
    if warnings:
        out.append(f"WARNINGS ({len(warnings)} kinds; most frequent first):")
        for group in sorted(warnings, key=lambda g: -g.count)[:10]:
            out.append(f"  x{group.count} · L{group.first} · {group.tag or '-'} · {group.template[:160]}")
    if summary.overflow:
        out.append(f"({summary.overflow} more error/warning lines did not fit the {MAX_TEMPLATES} groups)")
    if summary.error_tags:
        out.append("error tags: " + ", ".join(f"{tag} {count}" for tag, count in summary.error_tags.most_common(10)))
    if summary.tags:
        out.append("busiest tags: " + ", ".join(f"{tag} {count}" for tag, count in summary.tags.most_common(8)))
    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[: max_chars - 80] + "\n...[summary cut; ask with query for one kind of line]"
    return text


def big_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size}B"


# --------------------------------------------------------------------------- query / around / diff


def query(
    path: Path,
    *,
    level: str = "",
    tag: str = "",
    pid: str = "",
    pids: set[str] | None = None,
    text: "re.Pattern[str] | None" = None,
    start: int = 1,
    end: int | None = None,
    limit: int = 100,
    cancel: threading.Event | None = None,
) -> tuple[list[str], int, int | None]:
    """Matching lines as ``N|line``, how many matched, and where to continue (or None)."""
    kind = sniff(path)
    wanted_pids = set(pids or ())
    if pid:
        wanted_pids.add(pid)
    floor = _RANK.get(level.upper()[:1], -1) if level else -1
    tag_lower = tag.lower()
    out: list[str] = []
    matched = 0
    resume: int | None = None
    for number, line in _iter_lines(path, start=max(1, start)):
        if end is not None and number > end:
            break
        if number % 100_000 == 0 and cancel is not None and cancel.is_set():
            resume = number
            break
        if text is not None and not text.search(line):
            continue
        if floor >= 0 or tag or wanted_pids:
            item = parse(number, line, kind)
            if floor >= 0 and _RANK.get(item.level, -1) < floor:
                continue
            if tag and tag_lower not in item.tag.lower():
                continue
            if wanted_pids and item.pid not in wanted_pids:
                continue
        matched += 1
        if len(out) < limit:
            out.append(f"{number}|{_clip(line)}")
        elif resume is None:
            resume = number
            break
    return out, matched, resume


def pids_for_package(path: Path, package: str) -> set[str]:
    """Process ids of ``package`` in a logcat: from "Start proc 123:pkg", "Process: pkg, PID: 123",
    and am_proc_start lines."""
    name = re.escape(package)
    patterns = [
        re.compile(r"Start proc (\d+):" + name + r"(?:[:/\s]|$)"),
        re.compile(r"Process: " + name + r", PID: (\d+)"),
        re.compile(r"am_proc_start: \[\d+,(\d+),\d+," + name + r"[,\]]"),
    ]
    found: set[str] = set()
    needle = package.encode()
    with open(path, "rb") as handle:
        for raw in handle:
            if needle not in raw:
                continue
            text = raw.decode("utf-8", errors="replace")
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    found.add(match.group(1))
    return found


def around(path: Path, line: int, *, before: int = 20, after: int = 30) -> list[str]:
    first = max(1, line - before)
    return [f"{number}|{_clip(text)}" for number, text in _iter_lines(path, start=first, limit=before + after + 1)]


def diff(good: Path, bad: Path, *, limit: int = 30) -> list[str]:
    """Templates new in ``bad``, then ones at least 3x more frequent (per line) than in ``good``."""
    left = summarize(good)
    right = summarize(bad)
    scale = max(left.lines, 1) / max(right.lines, 1)
    new: list[Group] = []
    grown: list[tuple[float, Group]] = []
    for key, group in right.groups.items():
        before = left.groups.get(key)
        if before is None:
            new.append(group)
        elif group.count * scale >= 3 * before.count and group.count >= 3:
            grown.append((group.count * scale / before.count, group))
    out = []
    if right.event_counts or left.event_counts:
        kinds = sorted(set(right.event_counts) | set(left.event_counts))
        out.append("events (good -> bad): " + ", ".join(f"{k} {left.event_counts[k]}->{right.event_counts[k]}" for k in kinds))
    out.append(f"NEW in the bad log ({len(new)} kinds; errors first):")
    for group in sorted(new, key=lambda g: (-_RANK.get(g.level, 0), g.first))[:limit]:
        out.append(f"  x{group.count} · L{group.first} · {group.level} {group.tag or '-'} · {group.template[:180]}")
    if grown:
        out.append(f"MORE FREQUENT in the bad log ({len(grown)} kinds):")
        for ratio, group in sorted(grown, key=lambda item: -item[0])[: limit // 2]:
            out.append(f"  {ratio:.0f}x · x{group.count} · L{group.first} · {group.level} {group.tag or '-'} · {group.template[:160]}")
    return out


def looks_like_log(path: Path) -> bool:
    """A file to suggest the log tool for: big, or named like a log."""
    name = path.name.lower()
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    return size > 2 * 1024 * 1024 and (
        name.endswith((".log", ".txt", ".logcat")) or "logcat" in name or "bugreport" in name or "dmesg" in name
    )
