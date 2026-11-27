# SPDX-License-Identifier: Apache-2.0
"""Triage pipelines: one deterministic pass from a device or build log to a compact report.

crash | anr | tombstone | selinux | build. Each pipeline:

1. reads the input (``path`` in the workspace, of any size: big files go through
   ``log_tools``; or pasted ``text``),
2. extracts the failure (the exception and its root-cause frame, the blocked main thread and
   the lock holder, the native backtrace, the blocking denial, the first build error),
3. maps it to source (frames and file:line to files, log messages to the code that prints
   them, symbols to definitions; ``evidence.Resolver``), to the owning Soong module, and to
   suspect commits (blame of the failing lines, ``git log -G`` of a vanished symbol, recent
   changes to the module),
4. returns a short report. When nothing maps to this tree, the report says so and tells the
   model not to draft a fix from the log alone (the refusal guard).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from critique_bot import log_tools
from critique_bot.packs.aosp import evidence, selinux, tombstone
from critique_bot.packs.aosp.evidence import Place

#: Files up to this size are read whole; larger ones through log_tools (events, queries).
WHOLE_FILE_BYTES = 8 * 1024 * 1024
MAX_REPORT_CHARS = 6000
KINDS = ("crash", "anr", "tombstone", "selinux", "build")

_JAVA_FRAME = re.compile(r"^\s*at\s+((?:[\w$]+\.)*[\w$<>-]+)\.([\w$<>-]+)\((?:([\w$.-]+\.(?:java|kt|kts)):(\d+)|[^)]*)\)")
_CAUSED = re.compile(r"^\s*Caused by:\s*(.*)")
_EXCEPTION = re.compile(r"^\s*((?:[a-z_][\w$]*\.)+[A-Z][\w$]*(?:Exception|Error|Throwable)\w*)(?::\s*(.*))?$")


class TriageError(ValueError):
    """The input cannot be read (no path or text, a missing file)."""


# --------------------------------------------------------------------------- input


@dataclass
class Input:
    label: str
    path: Path | None = None
    text: str = ""
    big: bool = False
    kind: str = "text"

    def lines(self) -> Iterator[tuple[int, str]]:
        if self.path is None:
            for number, line in enumerate(self.text.splitlines(), start=1):
                yield number, line
            return
        with open(self.path, "rb") as handle:
            for number, raw in enumerate(handle, start=1):
                yield number, raw.decode("utf-8", errors="replace").rstrip("\r\n")

    def window(self, line: int, before: int = 0, after: int = 200) -> list[tuple[int, str]]:
        if self.path is None:
            rows = self.text.splitlines()
            start = max(1, line - before)
            return [(n, rows[n - 1]) for n in range(start, min(len(rows), line + after) + 1)]
        return [(int(item.split("|", 1)[0]), item.split("|", 1)[1])
                for item in log_tools.around(self.path, line, before=before, after=after)]

    def event_lines(self, kinds: tuple[str, ...]) -> list[int]:
        """Line numbers where log_tools saw events of these kinds (big files)."""
        if self.path is None:
            return []
        summary = log_tools.summarize(self.path)
        return [event.line for event in summary.events if event.kind in kinds]

    def grep(self, pattern: str, limit: int = 2000) -> list[tuple[int, str]]:
        if self.path is None:
            regex = re.compile(pattern)
            return [(n, t) for n, t in self.lines() if regex.search(t)][:limit]
        found, _count, _resume = log_tools.query(self.path, text=re.compile(pattern), limit=limit)
        return [(int(item.split("|", 1)[0]), item.split("|", 1)[1]) for item in found]


def open_input(args: dict[str, Any], ctx: Any) -> Input:
    workspace = Path(getattr(ctx, "workspace", ".") or ".")
    raw = str(args.get("path") or "").strip()
    text = args.get("text")
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace / raw
        if not path.is_file():
            raise TriageError(f"no file {raw}; pass path (a log, tombstone, traces or build log in the workspace) "
                              "or text (the lines themselves)")
        size = path.stat().st_size
        source = Input(raw, path=path, big=size > WHOLE_FILE_BYTES)
        source.kind = log_tools.sniff(path)
        return source
    if isinstance(text, str) and text.strip():
        return Input("the pasted text", text=text)
    raise TriageError("pass path (a log, tombstone, traces file or build log) or text (the lines themselves)")


def _message(text: str) -> tuple[str, str, str]:
    """(tag, pid, message) of a logcat line; ("", "", line) for anything else."""
    for kind in ("logcat", "logcat-brief"):
        item = log_tools.parse(0, text, kind)
        if item.tag or item.pid:
            return item.tag, item.pid, item.msg
    return "", "", text


# --------------------------------------------------------------------------- the report


@dataclass
class Report:
    kind: str
    source: str
    headline: str = ""
    facts: list[str] = field(default_factory=list)
    places: list[Place] = field(default_factory=list)
    place_notes: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    #: (git -G regex, what a hit means): lines whose history explains the failure (a null field's assignments)
    changes: list[tuple[str, str]] = field(default_factory=list)
    blame_weight: float = 3.0
    notes: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    found: bool = True

    def render(self, ctx: Any, resolver: evidence.Resolver) -> str:
        workspace = Path(getattr(ctx, "workspace", ".") or ".")
        out = [f"TRIAGE {self.kind} ({self.source}): {self.headline}"]
        out += self.facts
        if not self.found:
            out.append(f"verdict: no {self.kind} found in {self.source}.")
            out += self.notes
            return _cap("\n".join(out))
        places = _unique_places(self.places)
        if places:
            out.append("source (best first):")
            for place in places[:5]:
                func = place.symbol or resolver.enclosing(place)
                out.append(f"  {place.path}:{place.line}" if place.line else f"  {place.path}")
                if func:
                    out[-1] += f" in {func}"
                out[-1] += f"  [{place.how}]"
            modules = []
            for place in places[:3]:
                owner = evidence.owning_module(workspace, place.path)
                if owner and owner not in modules:
                    modules.append(owner)
            out += [f"module: {item}" for item in modules] or ["module: no Android.bp module owns these files"]
            commits, commit_notes = evidence.suspects(workspace, places, symbols=self.symbols, changes=self.changes,
                                                      blame_weight=self.blame_weight)
            if commits:
                out.append("suspect commits (most likely first; check them, do not trust the ranking blindly):")
                out += [f"  {index}. {item.text()}" for index, item in enumerate(commits, start=1)]
            else:
                out.append("suspect commits: none found (" + ("; ".join(commit_notes) or
                           "the lines are not committed yet or have no history") + ")")
            out.append("verdict: candidates found; read the source lines above before changing anything.")
        else:
            out.append("source: NO CANDIDATE. None of the frames, messages or files of this failure resolve to "
                       "code in this workspace" + (f" ({'; '.join(self.place_notes)})" if self.place_notes else "") + ".")
            out.append("verdict: refuse to guess. Do not draft a fix from the log alone: say the failing code is not "
                       "in this tree (another project, a prebuilt, or the platform), or find it first (locate, "
                       "find_strings, or index the module that owns it).")
        notes = list(dict.fromkeys(self.notes + resolver.notes))
        out += [f"note: {item}" for item in notes[:6]]
        if self.next_steps and places:
            out.append("next: " + "; ".join(self.next_steps[:4]))
        return _cap("\n".join(out))


def _unique_places(places: list[Place]) -> list[Place]:
    seen: set[tuple[str, int]] = set()
    out = []
    for place in places:
        key = (place.path, place.line)
        if key not in seen:
            seen.add(key)
            out.append(place)
    return out


def _cap(text: str) -> str:
    if len(text) <= MAX_REPORT_CHARS:
        return text
    return text[: MAX_REPORT_CHARS - 60].rsplit("\n", 1)[0] + "\n... (report cut; narrow with package or nth)"


# --------------------------------------------------------------------------- java stacks


@dataclass
class JavaFrame:
    qualified: str
    method: str
    file: str
    line: int
    raw: str

    @property
    def framework(self) -> bool:
        return evidence.is_framework(self.qualified)

    def short(self) -> str:
        cls = self.qualified.rpartition(".")[2]
        return f"{cls}.{self.method}"


@dataclass
class Section:
    exception: str
    message: str = ""
    frames: list[JavaFrame] = field(default_factory=list)


def parse_stack(messages: list[str]) -> list[Section]:
    """The exception and each ``Caused by`` with their frames."""
    sections: list[Section] = []
    for text in messages:
        caused = _CAUSED.match(text)
        if caused:
            name, _, message = caused.group(1).partition(": ")
            sections.append(Section(name.strip(), message.strip()))
            continue
        frame = _JAVA_FRAME.match(text)
        if frame:
            if not sections:
                sections.append(Section("unknown exception"))
            sections[-1].frames.append(JavaFrame(frame.group(1), frame.group(2), frame.group(3) or "",
                                                 int(frame.group(4) or 0), text.strip()))
            continue
        exception = _EXCEPTION.match(text)
        if exception and not sections:
            sections.append(Section(exception.group(1), (exception.group(2) or "").strip()))
    return sections


def failing_java_frame(sections: list[Section]) -> tuple[JavaFrame | None, Section | None]:
    """The root cause's first frame in non-framework code; else the first such frame anywhere."""
    for section in reversed(sections):
        for frame in section.frames:
            if not frame.framework:
                return frame, section
    for section in sections:
        if section.frames:
            return section.frames[0], section
    return None, None


def _map_frames(resolver: evidence.Resolver, frames: list[JavaFrame], report: Report, *, limit: int = 3) -> None:
    for frame in [f for f in frames if not f.framework][:limit]:
        if not frame.file:
            continue
        found = resolver.file(frame.file, frame.qualified)
        if found:
            for place in found[:1]:
                report.places.append(Place(place.path, frame.line, frame.short(), place.how))
        else:
            report.place_notes.append(f"{frame.file} not found")


# --------------------------------------------------------------------------- crash


_FATAL = re.compile(r"FATAL EXCEPTION(?: IN SYSTEM PROCESS)?:?\s*(.*)")
_PROCESS = re.compile(r"Process: ([\w.:-]+), PID: (\d+)")


def _crash_blocks(source: Input) -> list[list[tuple[int, str]]]:
    """Each Java crash as its lines (logcat prefix kept)."""
    starts = source.event_lines(("app crash", "system server crash")) if source.big else []
    rows: list[tuple[int, str]] = []
    if source.big:
        for line in starts[:10]:
            rows += source.window(line, 0, 150)
    else:
        rows = list(source.lines())
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] | None = None
    pid = ""
    for number, text in rows:
        tag, line_pid, message = _message(text)
        if _FATAL.search(message):
            current = [(number, text)]
            blocks.append(current)
            pid = line_pid
            continue
        if current is None:
            continue
        stack_like = bool(_JAVA_FRAME.match(message) or _CAUSED.match(message) or _PROCESS.search(message)
                          or _EXCEPTION.match(message) or message.strip().startswith("..."))
        same = (pid and line_pid == pid and tag in {"AndroidRuntime", ""}) or (not pid and stack_like)
        if same or (stack_like and not line_pid):
            current.append((number, text))
        elif len(current) > 2:
            current = None
    return blocks


def crash(source: Input, args: dict[str, Any], resolver: evidence.Resolver) -> Report:
    report = Report("crash", source.label)
    blocks = _crash_blocks(source)
    want = str(args.get("package") or "").strip()
    chosen = None
    described = []
    for block in blocks:
        messages = [_message(text)[2] for _n, text in block]
        process = next((m.group(1) for m in map(_PROCESS.search, messages) if m), "")
        described.append(process or "?")
        if chosen is None and (not want or want in process):
            chosen = (block, messages, process)
    nth = args.get("nth")
    if isinstance(nth, int) and 1 <= nth <= len(blocks):
        block = blocks[nth - 1]
        messages = [_message(text)[2] for _n, text in block]
        chosen = (block, messages, described[nth - 1])
    if chosen is None and not blocks and not source.big:
        messages = [_message(text)[2] for _n, text in source.lines()]
        if any(section.frames for section in parse_stack(messages)):
            chosen = ([(1, "")], messages, "")  # a pasted stack trace without the logcat header
    if chosen is None:
        native = tombstone.parse(list(source.lines()) if not source.big else source.grep(r"#\d+ pc |>>> .* <<<|signal \d+"))
        if native is not None and native.frames:
            report = _tombstone_report(source, native, resolver, Path(resolver.workspace))
            report.notes.insert(0, "no Java crash here; this is a native crash, triaged as a tombstone")
            return report
        report.found = False
        report.headline = "no FATAL EXCEPTION" + (f" for {want}" if want else "")
        if described:
            report.notes.append("crashes in the log: " + ", ".join(described[:8]))
        report.notes.append("for an ANR use action anr; for a native crash action tombstone")
        return report
    block, messages, process = chosen
    sections = parse_stack(messages)
    top = sections[0] if sections else Section("unknown exception")
    frame, cause = failing_java_frame(sections)
    report.headline = f"{process or 'a process'} crashed with {top.exception}" + (f": {top.message[:160]}" if top.message else "")
    report.facts.append(f"at line {block[0][0]}" + (f"; {len(blocks)} crash(es) in the log: {', '.join(described[:5])}"
                                                    if len(blocks) > 1 else ""))
    if cause is not None and cause is not top:
        report.facts.append(f"root cause: {cause.exception}" + (f": {cause.message[:160]}" if cause.message else ""))
    if frame is not None:
        report.facts.append(f"failing frame: {frame.raw}")
        if frame.framework:
            report.notes.append("every frame is framework code: the bug is usually in how the app calls it; read "
                                "the app's call site")
    all_frames = [f for section in reversed(sections) for f in section.frames]
    if frame is not None:
        all_frames.remove(frame)
        all_frames.insert(0, frame)
    _map_frames(resolver, all_frames, report)
    message = (cause or top).message
    _null_receiver(resolver, report, cause or top, frame)
    if _searchable(message, (cause or top).exception) and not report.places:
        for place in resolver.strings(message, limit=3):
            report.places.append(place)
            report.facts.append(f"the message is printed at {place.path}:{place.line}")
    report.next_steps = ["read_files the failing frame's lines", "aosp_build action tests_for with the file to find "
                         "its tests"]
    return report


_NPE_METHOD = re.compile(r"Attempt to invoke (?:virtual|interface|direct|static)? ?method '[^']*?\.?([\w$<>]+)\([^']*\)' "
                         r"on a null object reference")
_NPE_FIELD = re.compile(r"Attempt to (?:read from|write to) field '[^']*?\.([\w$]+)' on a null object reference")


#: Exceptions whose message the runtime writes (no string in the app's code to find).
_RUNTIME_MESSAGES = ("NullPointerException", "ArrayIndexOutOfBoundsException", "IndexOutOfBoundsException",
                     "StringIndexOutOfBoundsException", "ClassCastException", "ArithmeticException",
                     "NegativeArraySizeException", "ConcurrentModificationException", "OutOfMemoryError",
                     "StackOverflowError", "DeadObjectException")


def _searchable(message: str, exception: str) -> bool:
    """A message worth looking for in the code: written by the app (not the runtime), with some words."""
    if not message or exception.rsplit(".", 1)[-1] in _RUNTIME_MESSAGES:
        return False
    fragment = evidence._longest_fragment(message)
    return len(fragment) >= 10 and " " in fragment.strip()


def _null_receiver(resolver: evidence.Resolver, report: Report, section: Section, frame: JavaFrame | None) -> None:
    """A null dereference: name the null expression on the failing line, and look for the commits that
    changed its assignments (the null comes from where it is set, not from where it is used)."""
    if "NullPointerException" not in section.exception or frame is None or not report.places:
        return
    match = _NPE_METHOD.search(section.message) or _NPE_FIELD.search(section.message)
    member = match.group(1) if match else ""
    place = report.places[0]
    try:
        lines = (resolver.workspace / place.path).read_text(encoding="utf-8", errors="replace").splitlines()
        text = lines[place.line - 1] if 0 < place.line <= len(lines) else ""
    except OSError:
        text = ""
    receiver = ""
    if member and text:
        found = re.search(r"([A-Za-z_][\w]*)\s*(?:\(\s*\))?\s*\.\s*" + re.escape(member) + r"\b", text)
        receiver = found.group(1) if found else ""
    if not receiver or receiver in {"this", "super"}:
        return
    report.blame_weight = 1.0
    report.facts.append(f"null: {receiver} on the failing line" + (f" (calling {member})" if member else ""))
    if re.fullmatch(r"m[A-Z]\w*|s[A-Z]\w*|[a-z]\w*", receiver):
        report.changes.append((r"\b" + receiver + r" *=[^=]", f"changed how {receiver} is set"))
        report.next_steps.insert(0, f"find where {receiver} is set (code_nav refs, code_history log changed)")


# --------------------------------------------------------------------------- ANR


_ANR = re.compile(r"ANR in ([\w.:-]+)(?:\s+\(([^)]*)\))?")
_REASON = re.compile(r"Reason: (.*)")
_THREAD = re.compile(r'^"(?P<name>[^"]+)".*?\btid=(?P<tid>\d+)\s+(?P<state>\w+)')
_WAITING = re.compile(r"waiting to lock <(0x[0-9a-f]+)>.*?held by (?:thread|tid=)\s*(\d+)")
_LOCKED = re.compile(r"- locked <(0x[0-9a-f]+)>")
_CMDLINE = re.compile(r"^Cmd line: (\S+)")
_PID_BLOCK = re.compile(r"^----- pid (\d+) at ")


@dataclass
class Thread:
    name: str
    tid: str
    state: str
    lines: list[str] = field(default_factory=list)

    def frames(self) -> list[JavaFrame]:
        out = []
        for text in self.lines:
            match = _JAVA_FRAME.match(text)
            if match:
                out.append(JavaFrame(match.group(1), match.group(2), match.group(3) or "", int(match.group(4) or 0),
                                     text.strip()))
        return out


def parse_traces(rows: list[tuple[int, str]], package: str = "") -> tuple[str, dict[str, Thread]]:
    """The threads of ``package``'s block in an ANR traces dump (or the first block)."""
    blocks: list[tuple[str, dict[str, Thread]]] = []
    threads: dict[str, Thread] = {}
    current: Thread | None = None
    cmd = ""
    for _number, raw in rows:
        text = _message(raw)[2] if re.match(r"^\d\d-\d\d ", raw) else raw
        if _PID_BLOCK.match(text):
            threads = {}
            blocks.append(("", threads))
            current = None
            continue
        cmdline = _CMDLINE.match(text)
        if cmdline:
            cmd = cmdline.group(1)
            if blocks:
                blocks[-1] = (cmd, blocks[-1][1])
            continue
        thread = _THREAD.match(text)
        if thread:
            if not blocks:
                blocks.append(("", threads))
            current = Thread(thread.group("name"), thread.group("tid"), thread.group("state"))
            blocks[-1][1][current.tid] = current
            continue
        if current is not None and text.strip():
            current.lines.append(text.strip())
        elif not text.strip():
            current = None
    for name, found in blocks:
        if package and name == package:
            return name, found
    return (blocks[0] if blocks else ("", {}))


def anr(source: Input, args: dict[str, Any], resolver: evidence.Resolver, ctx: Any) -> Report:
    report = Report("anr", source.label)
    rows = source.grep(r"ANR in |Reason: |am_anr") if source.big else list(source.lines())
    want = str(args.get("package") or "").strip()
    process = reason = ""
    for number, text in rows:
        message = _message(text)[2]
        match = _ANR.search(message)
        if match and (not want or want in match.group(1)) and not process:
            process = match.group(1)
            report.facts.append(f"ANR at line {number}" + (f" ({match.group(2)})" if match.group(2) else ""))
            continue
        found = _REASON.search(message)
        if found and process and not reason:
            reason = found.group(1)
    traces_rows: list[tuple[int, str]] = []
    traces_arg = str(args.get("traces") or "").strip()
    if traces_arg:
        traces = open_input({"path": traces_arg}, ctx)
        traces_rows = list(traces.lines()) if not traces.big else traces.grep(r'^"|^\s+at |waiting to lock|- locked|'
                                                                             r'^----- pid|^Cmd line', 20000)
    elif not source.big:
        traces_rows = rows
    else:
        traces_rows = source.grep(r'^"|^\s+at |waiting to lock|- locked|^----- pid|^Cmd line', 20000)
    name, threads = parse_traces(traces_rows, want or process)
    process = process or name
    main = next((t for t in threads.values() if t.name == "main"), None)
    if not process and main is None:
        report.found = False
        report.headline = "no 'ANR in' line and no main-thread stack"
        report.notes.append("pass traces (the /data/anr/ file or the bugreport's VM TRACES section) with the log")
        return report
    report.headline = f"{process or 'the app'} did not respond" + (f": {reason[:160]}" if reason else "")
    if main is None:
        report.facts.append("no main-thread stack: pass traces (the /data/anr/anr_* file) to see where it blocked")
        report.places = []
        report.place_notes.append("no thread dump to map")
        return report
    report.facts.append(f"main thread: {main.state}")
    main_frames = main.frames()
    frame = next((f for f in main_frames if not f.framework), main_frames[0] if main_frames else None)
    if frame is not None:
        report.facts.append(f"main thread at: {frame.raw}")
    _map_frames(resolver, main_frames, report, limit=2)
    waiting = next((m for m in map(_WAITING.search, main.lines) if m), None)
    if waiting:
        lock, holder_tid = waiting.group(1), waiting.group(2)
        holder = threads.get(holder_tid)
        if holder is not None:
            holder_frames = holder.frames()
            top = next((f for f in holder_frames if not f.framework), holder_frames[0] if holder_frames else None)
            report.facts.append(f"main waits for lock {lock} held by thread {holder_tid} \"{holder.name}\" ({holder.state})"
                                + (f": {top.raw}" if top else ""))
            holder_waits = next((m for m in map(_WAITING.search, holder.lines) if m), None)
            if holder_waits and holder_waits.group(2) == next(
                    (t.tid for t in threads.values() if t.name == "main"), ""):
                report.facts.append("DEADLOCK: that thread waits for a lock the main thread holds")
            _map_frames(resolver, holder_frames, report, limit=2)
        else:
            report.facts.append(f"main waits for lock {lock} held by thread {holder_tid} (not in the dump)")
    elif main.state.lower() in {"native", "waiting", "timed_waiting", "sleeping", "runnable"}:
        report.notes.append(f"the main thread is {main.state}: slow work on the main thread (I/O, binder call, "
                            "sleep) rather than a lock")
    report.next_steps = ["read the main-thread frame and the lock holder's frame", "move the blocking work off the "
                         "main thread or shorten the lock's critical section"]
    return report


# --------------------------------------------------------------------------- tombstone


def _tombstone_report(source: Input, stone: tombstone.Tombstone, resolver: evidence.Resolver, workspace: Path) -> Report:
    report = Report("tombstone", source.label)
    report.headline = f"{stone.cmd or 'a native process'} (pid {stone.pid or '?'}, thread {stone.thread or '?'}) died: " \
                      f"{stone.signal or 'no signal line'}"
    if stone.fault:
        report.facts.append(f"fault addr {stone.fault}" + (f"; cause: {stone.cause}" if stone.cause else ""))
    if stone.abort:
        report.facts.append(f"abort message: {stone.abort[:200]}")
    note = tombstone.symbolize(workspace, stone)
    report.facts.append(note)
    frame = tombstone.failing_frame(stone)
    if frame is not None:
        report.facts.append(f"failing frame: {frame.text()}" + (" (first frame outside libc/liblog/libc++)"
                                                                if frame.number else ""))
    report.facts.append("backtrace: " + " | ".join(f.text() for f in stone.frames[:6]))
    candidates = [f for f in stone.frames if not f.runtime][:3]
    for item in candidates:
        if item.source:
            path, _, line = item.source.rpartition(":")
            found = resolver.file(path)
            if found:
                report.places.append(Place(found[0].path, int(line) if line.isdigit() else 0,
                                           tombstone.short_function(item.symbolized_func or item.func), found[0].how))
                continue
        func = tombstone.short_function(item.symbolized_func or item.func)
        if func:
            hits = resolver.symbol(func.replace("::", "."), limit=2)
            for hit in hits:
                report.places.append(Place(hit.path, hit.line, func, hit.how + ", by function name only"))
            if not hits:
                report.place_notes.append(f"{func} not found")
            if "::" in func:
                report.symbols.append(func.rsplit("::", 1)[-1])
        else:
            report.place_notes.append(f"{item.lib} pc {item.pc} has no symbol")
    located = re.search(r"([\w.+-]+\.(?:cpp|cc|cxx|c|h|hpp|rs)):(\d+)\]?", stone.abort or "")
    if located:  # CHECK/LOG(FATAL) put file:line in the abort message: better than a function name
        found = resolver.file(located.group(1))
        if found:
            report.places.insert(0, Place(found[0].path, int(located.group(2)), "", "abort message"))
    if stone.abort and not report.places:
        for place in resolver.strings(stone.abort, limit=2):
            report.places.append(place)
            report.facts.append(f"the abort message is printed at {place.path}:{place.line}")
    report.next_steps = ["read the failing frame's function", "symbolize with `stack` after lunch if lines are missing"]
    return report


def native(source: Input, args: dict[str, Any], resolver: evidence.Resolver) -> Report:
    rows = list(source.lines()) if not source.big else source.grep(r"#\d+ pc |>>> .* <<<|signal \d+|Abort message|"
                                                                   r"backtrace:|Cause: ")
    stone = tombstone.parse(rows)
    if stone is None:
        report = Report("tombstone", source.label, "no native crash (no 'pid: .. >>> .. <<<' or '#00 pc' lines)")
        report.found = False
        report.notes.append("for a Java crash use action crash")
        return report
    return _tombstone_report(source, stone, resolver, Path(resolver.workspace))


# --------------------------------------------------------------------------- SELinux


def sepolicy(source: Input, args: dict[str, Any], resolver: evidence.Resolver) -> Report:
    report = Report("selinux", source.label)
    rows = source.grep(r"avc:\s+denied|Service '", 20000) if source.big else list(source.lines())
    denials, exited = selinux.collect(rows)
    if not denials:
        report.found = False
        report.headline = "no 'avc: denied' line"
        report.notes.append("SELinux denials are in dmesg (kernel) and logcat (auditd, tag 'avc'); pass that log")
        return report
    groups = selinux.group(denials, exited, str(args.get("package") or args.get("domain") or ""))
    best = groups[0]
    enforced = sum(1 for g in groups if g.enforced)
    report.headline = f"{len(denials)} denial(s) in {len(groups)} kind(s), {enforced} enforced; the blocking one: " \
                      f"{best.context()}"
    report.facts.append(f"why this one: {'; '.join(best.why)} (first at line {best.first}, x{best.count}"
                        + (f", process {', '.join(best.comms[:2])}" if best.comms else "")
                        + (f", object {', '.join(best.names[:2])}" if best.names else "") + ")")
    generic = best.target in selinux.GENERIC_TARGETS
    if generic:
        report.facts.append(f"minimal rule would be {best.rule()}, but do NOT add it: {best.target} is a generic label "
                            "that covers far more than this object")
        for step in selinux.relabel(best):
            report.facts.append(step)
    else:
        report.facts.append(f"minimal rule: {best.rule()}")
    files = resolver.files()
    declared = selinux.te_files(resolver.workspace, files, best.source)
    if declared:
        te, line = declared[0]
        report.facts.append(f"edit: {te} (declares type {best.source} at line {line}); the allow rule goes there, "
                            "nothing broader")
        if selinux.already_allowed(resolver.workspace, te, best):
            report.notes.append(f"{te} already allows this: the object is probably labeled differently than "
                                "you think, or a neverallow/dontaudit is involved; check the labels")
        report.places.append(Place(te, line, f"type {best.source}", "scan"))
    else:
        report.place_notes.append(f"no .te file declares type {best.source}")
    for hint in selinux.hints(best):
        report.notes.append(hint)
    others = [g for g in groups[1:6]]
    if others:
        report.facts.append("other denials (likely harmless unless enforced): " + "; ".join(
            f"{g.source}->{g.target}:{g.tclass} {{{' '.join(g.perms)}}}{' enforced' if g.enforced else ''} x{g.count}"
            for g in others))
    if generic:
        report.next_steps = ["give the object its own type as above", "allow that type, never the generic one"]
    else:
        report.next_steps = [f"add exactly `{best.rule()}` to the .te file", "no wildcards, no permissive, no extra "
                             "permissions or classes"]
    return report


# --------------------------------------------------------------------------- build


_COMPILER = re.compile(r"^(?P<file>[^\s:]+\.(?:java|kt|c|cc|cpp|cxx|h|hpp|aidl|rs|go|py)):(?P<line>\d+)(?::(?P<col>\d+))?:"
                       r"\s*(?:fatal )?error:\s*(?P<msg>.*)")
_KOTLIN = re.compile(r"^e: (?:file://)?(?P<file>\S+?\.kts?):(?P<line>\d+):(?P<col>\d+):?\s+(?P<msg>.*)")
_SOONG = re.compile(r"^error: (?P<file>[^\s:]+(?:Android\.bp|\.mk)):(?P<line>\d+):(?P<col>\d+): (?P<msg>.*)")
_LINKER = re.compile(r"(?:ld\.lld|ld): error: (?P<msg>undefined symbol: (?P<sym>.+)|.*)")
_REFERENCED = re.compile(r">>> referenced by (?P<file>[^\s:]+):(?P<line>\d+)")
_FAILED = re.compile(r"^FAILED: (?P<target>\S+)")
_SYMBOL_LINE = re.compile(r"^\s*symbol:\s+(?:method|variable|class|constructor|static)?\s*(?P<name>[\w$]+)")
_MISSING = re.compile(r"cannot find symbol|Unresolved reference:?\s*'?(\w+)|no member named '(\w+)'|"
                      r"use of undeclared identifier '(\w+)'|has no member '(\w+)'|"
                      r"depends on undefined module \"([\w.-]+)\"|unknown type name '(\w+)'")
_ERRORISH = r"error:|^e: |^FAILED: |ld(?:\.lld)?: error|>>> referenced by|^\s+symbol:|^\s+location:"


@dataclass
class BuildError:
    line: int
    file: str
    at: int
    message: str
    symbol: str = ""
    target: str = ""


def _intermediates_module(target: str) -> str:
    parts = target.split("/")
    if ".intermediates" in parts:
        rest = parts[parts.index(".intermediates") + 1:]
        for index, part in enumerate(rest):
            if re.match(r"^(?:android|linux_glibc|linux_musl|windows|darwin)_", part) and index > 0:
                return rest[index - 1]
    return ""


def parse_build(rows: list[tuple[int, str]]) -> tuple[list[BuildError], list[str]]:
    errors: list[BuildError] = []
    failed: list[str] = []
    for index, (number, text) in enumerate(rows):
        failed_match = _FAILED.match(text)
        if failed_match:
            failed.append(failed_match.group("target"))
            continue
        match = _COMPILER.match(text) or _KOTLIN.match(text) or _SOONG.match(text)
        if match:
            item = BuildError(number, match.group("file"), int(match.group("line")), match.group("msg").strip())
            item.target = failed[-1] if failed else ""
            missing = _MISSING.search(item.message)
            if missing:
                item.symbol = next((g for g in missing.groups() if g), "")
            if not item.symbol and "cannot find symbol" in item.message:
                for _n, follow in rows[index + 1: index + 6]:
                    found = _SYMBOL_LINE.match(follow)
                    if found:
                        item.symbol = found.group("name")
                        break
            errors.append(item)
            continue
        linker = _LINKER.search(text)
        if linker:
            item = BuildError(number, "", 0, linker.group("msg").strip())
            symbol = linker.group("sym") or ""
            item.symbol = tombstone.short_function(symbol).rsplit("::", 1)[-1] if symbol else ""
            for _n, follow in rows[index + 1: index + 6]:
                ref = _REFERENCED.search(follow)
                if ref:
                    item.file, item.at = ref.group("file"), int(ref.group("line"))
                    break
            item.target = failed[-1] if failed else ""
            errors.append(item)
    return errors, failed


def build(source: Input, args: dict[str, Any], resolver: evidence.Resolver) -> Report:
    report = Report("build", source.label)
    rows = source.grep(_ERRORISH, 4000) if source.big else list(source.lines())
    if source.big:  # the lines after each error (javac's symbol:/location:)
        extra: list[tuple[int, str]] = []
        for number, _text in rows[:20]:
            extra += source.window(number, 0, 6)
        rows = sorted(set(rows) | set(extra))
    errors, failed = parse_build(rows)
    if not errors:
        report.found = False
        report.headline = "no compiler, Soong or linker error line" + (f"; failed targets: {', '.join(failed[:3])}"
                                                                     if failed else "")
        report.notes.append("look at the lines before FAILED: with the log tool (action around)")
        return report
    first = errors[0]
    distinct = list(dict.fromkeys(f"{e.file}:{e.at}: {e.message[:120]}" if e.file else e.message[:140] for e in errors))
    report.headline = (f"{first.file}:{first.at}: {first.message[:180]}" if first.file else first.message[:200])
    report.facts.append(f"{len(errors)} error line(s), {len(distinct)} distinct; the first one breaks the build")
    module = _intermediates_module(first.target) if first.target else ""
    if module:
        report.facts.append(f"failed target belongs to module {module}")
    if first.symbol:
        report.facts.append(f"missing symbol: {first.symbol}")
        report.symbols.append(first.symbol)
        report.blame_weight = 1.0  # the line that uses it is fine; what changed is where it went
    suggestion = next((re.search(r"Or did you mean \[([^\]]*)\]", text) for _n, text in rows
                       if "Or did you mean" in text), None)
    if suggestion:
        report.facts.append(f"Soong suggests: {suggestion.group(1)}")
    package = re.search(r"package ([\w.]+) does not exist", first.message)
    if package:
        _package_owner(resolver, report, package.group(1), module)
    for item in errors[:4]:
        if not item.file:
            continue
        rel = item.file
        if not (resolver.workspace / rel).is_file():
            found = resolver.file(rel)
            if not found:
                report.place_notes.append(f"{rel} not found")
                continue
            rel = found[0].path
        report.places.append(Place(rel, item.at, "", "path"))
    if first.symbol and len(report.places) < 4:
        for place in resolver.symbol(first.symbol, limit=2):
            report.places.append(Place(place.path, place.line, f"{first.symbol} defined here", place.how))
    if len(distinct) > 1:
        report.facts.append("other errors: " + " | ".join(distinct[1:4]))
    report.next_steps = ["fix the first error (later ones often follow from it)",
                         f"rebuild only the module (aosp_build action m{' with ' + module if module else ''})"]
    return report


def _package_owner(resolver: evidence.Resolver, report: Report, package: str, failing: str) -> None:
    """``package x.y does not exist``: the module that builds that package, to add as a dependency."""
    declared = re.compile(r"^\s*package\s+" + re.escape(package) + r"\s*;?\s*$", re.M)
    want = package.replace(".", "/")
    for rel in [r for r in resolver.files() if r.endswith((".java", ".kt"))]:
        if want not in rel:
            continue
        try:
            head = (resolver.workspace / rel).read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        if not declared.search(head):
            continue
        names = evidence.module_names(resolver.workspace, rel)
        if names:
            report.facts.append(f"package {package} is built by module {names[0]} ({rel}): add \"{names[0]}\" to "
                                f"static_libs of {failing or 'the failing module'} (libs only for compile-time APIs)")
            report.places.append(Place(rel, 1, f"package {package}", "scan"))
        else:
            report.facts.append(f"package {package} is in {rel}, but no Android.bp module builds it")
        return
    report.facts.append(f"package {package} is not in this tree: a prebuilt or SDK library, or a typo")


# --------------------------------------------------------------------------- entry


def run(kind: str, args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    """Run one pipeline; (ok, report or error)."""
    if kind not in KINDS:
        return False, f"action must be one of: {', '.join(KINDS)}"
    try:
        source = open_input(args, ctx)
    except TriageError as exc:
        return False, str(exc)
    resolver = evidence.Resolver(ctx)
    try:
        if kind == "crash":
            report = crash(source, args, resolver)
        elif kind == "anr":
            report = anr(source, args, resolver, ctx)
        elif kind == "tombstone":
            report = native(source, args, resolver)
        elif kind == "selinux":
            report = sepolicy(source, args, resolver)
        else:
            report = build(source, args, resolver)
        return True, report.render(ctx, resolver)
    except TriageError as exc:
        return False, str(exc)
    finally:
        resolver.close()


def rel_of(ctx: Any, raw: str) -> str:
    workspace = Path(getattr(ctx, "workspace", ".") or ".")
    path = Path(raw)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(workspace.resolve()).as_posix()
        except (OSError, ValueError):
            return raw
    return PurePosixPath(raw).as_posix()
