# SPDX-License-Identifier: Apache-2.0
"""Native crashes: parse a tombstone (or the ``DEBUG`` lines of a logcat), pick the failing
frame, and symbolize it with the tree's own tools when they are there.

Symbolization uses the unstripped libraries in ``out/target/product/*/symbols`` and
``llvm-addr2line`` from ``prebuilts/clang`` (or ``addr2line`` on PATH), the same data the
tree's ``development/scripts/stack`` uses. Without them the frames are reported as they
are, and the report says so: a function name from the tombstone is a hint, not a line.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 - runs addr2line with an argv list, never a shell
from dataclasses import dataclass, field
from pathlib import Path

_HEADER = re.compile(r"pid: (?P<pid>\d+), tid: (?P<tid>\d+), name: (?P<thread>.*?)\s+>>> (?P<cmd>.*?) <<<")
_SIGNAL = re.compile(r"signal (?P<num>\d+) \((?P<name>\w+)\), code (?P<code>-?\d+) \((?P<codename>[^)]*)\)"
                     r"(?:, fault addr (?P<addr>\S+))?")
_ABORT = re.compile(r"Abort message: '(?P<msg>.*)'")
_CAUSE = re.compile(r"^\s*Cause: (?P<cause>.*)")
_FRAME = re.compile(r"#(?P<n>\d+) pc (?P<pc>[0-9a-fA-F]+)\s+(?P<lib>\S+)(?: \(offset 0x[0-9a-f]+\))?"
                    r"(?: \((?!BuildId: )(?P<func>.+?)\))?(?: \(BuildId: (?P<build>[0-9a-f]+)\))?\s*$")
#: Libraries whose frames are the messenger, not the bug (abort, logging, the C++ runtime).
RUNTIME_LIBS = ("libc.so", "libc++.so", "liblog.so", "libm.so", "libdl.so", "linker64", "linker", "libbase.so",
                "libc++_shared.so", "libutils.so", "libart.so", "libartbase.so", "libunwindstack.so")
_PROGRAMS = ("llvm-addr2line", "addr2line")


@dataclass
class Frame:
    number: int
    pc: str
    lib: str
    func: str = ""
    build_id: str = ""
    source: str = ""  # file:line after symbolization
    symbolized_func: str = ""

    @property
    def runtime(self) -> bool:
        name = self.lib.rsplit("/", 1)[-1]
        return name in RUNTIME_LIBS

    def text(self) -> str:
        func = self.symbolized_func or self.func or "?"
        where = f" at {self.source}" if self.source else ""
        return f"#{self.number:02d} {self.lib} {func}{where}"


@dataclass
class Tombstone:
    pid: str = ""
    thread: str = ""
    cmd: str = ""
    signal: str = ""
    fault: str = ""
    abort: str = ""
    cause: str = ""
    frames: list[Frame] = field(default_factory=list)
    line: int = 0


def parse(lines: list[tuple[int, str]]) -> Tombstone | None:
    """The first crash in ``lines`` (a tombstone file, or logcat DEBUG lines); None when there is none."""
    found = Tombstone()
    seen_backtrace = False
    for number, raw in lines:
        text = _strip_logcat(raw)
        header = _HEADER.search(text)
        if header and not found.pid:
            found.pid, found.thread, found.cmd = header.group("pid"), header.group("thread"), header.group("cmd")
            found.line = found.line or number
            continue
        signal = _SIGNAL.search(text)
        if signal and not found.signal:
            found.signal = f"signal {signal.group('num')} ({signal.group('name')}), {signal.group('codename')}"
            found.fault = signal.group("addr") or ""
            found.line = found.line or number
            continue
        abort = _ABORT.search(text)
        if abort and not found.abort:
            found.abort = abort.group("msg")
            continue
        cause = _CAUSE.match(text)
        if cause and not found.cause:
            found.cause = cause.group("cause")
            continue
        if "backtrace:" in text:
            if seen_backtrace and found.frames:
                break  # the next thread's backtrace (a full tombstone lists every thread)
            seen_backtrace = True
            continue
        frame = _FRAME.search(text)
        if frame:
            number_in = int(frame.group("n"))
            if found.frames and number_in == 0:
                break  # a second backtrace starts
            found.frames.append(Frame(number_in, frame.group("pc"), frame.group("lib"), (frame.group("func") or "").strip(),
                                      frame.group("build") or ""))
            found.line = found.line or number
    if not (found.pid or found.signal or found.frames):
        return None
    return found


def _strip_logcat(text: str) -> str:
    """The message of a logcat line (``F DEBUG   : #00 pc ...``), or the line itself."""
    match = re.match(r"^(?:\d\d-\d\d|\d{4}-\d\d-\d\d)\s[\d:.]+\s+\d+\s+\d+\s+[VDIWEFA]\s+[^:]*:\s?(.*)$", text)
    if match:
        return match.group(1)
    match = re.match(r"^[VDIWEFA]/[^(]+\(\s*\d+\):\s?(.*)$", text)
    return match.group(1) if match else text


def failing_frame(stone: Tombstone) -> Frame | None:
    """The first frame outside the C runtime, logging and abort machinery."""
    for frame in stone.frames:
        if not frame.runtime:
            return frame
    return stone.frames[0] if stone.frames else None


def short_function(func: str) -> str:
    """``Class::method`` from ``ns::Class::method(args) const+52``."""
    text = re.sub(r"\+\d+$", "", func.strip())
    depth = 0
    out = []
    for char in text:  # drop argument lists and template arguments
        if char in "(<":
            depth += 1
        elif char in ")>":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    parts = [part for part in "".join(out).replace(" const", "").strip().split("::") if part]
    return "::".join(parts[-2:])


# --------------------------------------------------------------------------- symbolization


def symbols_dirs(workspace: Path) -> list[Path]:
    return sorted(Path(workspace).glob("out/target/product/*/symbols"))


def addr2line(workspace: Path) -> str:
    """The tree's llvm-addr2line, else one on PATH; "" when there is none."""
    for pattern in ("prebuilts/clang/host/linux-x86/llvm-binutils-stable/llvm-addr2line",
                    "prebuilts/clang/host/linux-x86/clang-*/bin/llvm-addr2line"):
        for candidate in sorted(Path(workspace).glob(pattern), reverse=True):
            if candidate.is_file():
                return str(candidate)
    for name in _PROGRAMS:
        found = shutil.which(name)
        if found:
            return found
    return ""


def symbolize(workspace: Path, stone: Tombstone, *, timeout: float = 20.0) -> str:
    """Fill ``source`` and ``symbolized_func`` of the frames it can; a note saying what was done."""
    dirs = symbols_dirs(workspace)
    if not dirs:
        return ("unsymbolized: no out/target/product/*/symbols in this tree (build the module, or run "
                "`stack` on the tombstone after lunch); function names below come from the tombstone")
    tool = addr2line(workspace)
    if not tool:
        return "unsymbolized: no llvm-addr2line in prebuilts/clang or addr2line on PATH"
    by_lib: dict[Path, list[Frame]] = {}
    missing: set[str] = set()
    for frame in stone.frames[:32]:
        if not frame.lib.startswith("/"):
            continue
        lib = next((d / frame.lib.lstrip("/") for d in dirs if (d / frame.lib.lstrip("/")).is_file()), None)
        if lib is None:
            missing.add(frame.lib)
            continue
        by_lib.setdefault(lib, []).append(frame)
    done = 0
    for lib, frames in by_lib.items():
        argv = [tool, "-C", "-f", "-e", str(lib)] + ["0x" + frame.pc for frame in frames]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)  # nosec B603
        except (OSError, subprocess.TimeoutExpired):
            continue
        rows = proc.stdout.splitlines()
        for index, frame in enumerate(frames):
            if 2 * index + 1 >= len(rows):
                break
            func, place = rows[2 * index].strip(), rows[2 * index + 1].strip()
            if func and func != "??":
                frame.symbolized_func = func
            if place and not place.startswith("??"):
                frame.source = re.sub(r" \(discriminator \d+\)$", "", place)
                done += 1
    note = f"symbolized {done} frame(s) with {Path(tool).name} and {dirs[0].relative_to(workspace)}"
    if missing:
        note += f"; no symbols for {', '.join(sorted(missing)[:3])}"
    return note
