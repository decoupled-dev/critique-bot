from __future__ import annotations

import itertools
import os
import sys
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, TextIO

# Magenta, unused by status lines (yellow / cyan / green / red).
MODEL_COLOR = "35"

_enabled = False
_spinner_lock = threading.Lock()
_active_spinner: _Spinner | None = None


def configure_stdio() -> None:
    """Force UTF-8 on stdout/stderr so Windows cp1252/charmap cannot crash the CLI.

    GitLab Windows runners default to a charmap codec. Printing a review that
    contains U+2011 (non-breaking hyphen) or other Unicode then raises
    UnicodeEncodeError and fails the job before gitlab-post runs.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, OSError, ValueError):
                pass


def paint(text: str, color: str, *, file: TextIO | None = None) -> str:
    """Wrap text in ANSI color when the destination is a terminal.

    ``NO_COLOR`` and a non-tty (pipes, tests, redirected files) leave the
    text unchanged so captured output stays plain.
    """
    if os.environ.get("NO_COLOR"):
        return text
    target = sys.stdout if file is None else file
    try:
        is_tty = bool(target.isatty())
    except Exception:
        is_tty = False
    if not is_tty:
        return text
    return f"\033[{color}m{text}\033[0m"


def print_safe(*args: Any, file: TextIO | None = None, **kwargs: Any) -> None:
    """print() that never raises UnicodeEncodeError on a narrow console codec."""
    target = sys.stdout if file is None else file
    try:
        print(*args, file=target, **kwargs)
        return
    except UnicodeEncodeError:
        pass
    encoding = getattr(target, "encoding", None) or "utf-8"
    sep = str(kwargs.get("sep", " "))
    end = str(kwargs.get("end", "\n"))
    text = sep.join(str(arg) for arg in args) + end
    raw = text.encode(encoding, errors="replace")
    buffer = getattr(target, "buffer", None)
    if buffer is not None:
        buffer.write(raw)
        if kwargs.get("flush"):
            buffer.flush()
        return
    target.write(raw.decode(encoding, errors="replace"))
    if kwargs.get("flush"):
        target.flush()


def configure(*, enabled: bool) -> None:
    global _enabled
    _enabled = bool(enabled)


def enabled() -> bool:
    return _enabled


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write(level: str, message: str) -> None:
    if not _enabled:
        return
    line = f"{_ts()} [{level:<5}] {message}"
    with _spinner_lock:
        spinner = _active_spinner
        if spinner is not None:
            spinner.clear()
        print_safe(line, file=sys.stderr, flush=True)
        if spinner is not None:
            spinner.render()


def debug(message: str) -> None:
    _write("DEBUG", message)


def info(message: str) -> None:
    _write("INFO", message)


def warn(message: str) -> None:
    _write("WARN", message)


def error(message: str) -> None:
    _write("ERROR", message)


def exception(message: str) -> None:
    error(message)
    if not _enabled:
        return
    with _spinner_lock:
        spinner = _active_spinner
        if spinner is not None:
            spinner.clear()
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        if spinner is not None:
            spinner.render()


def preview(text: str, limit: int = 120) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def kv(**fields: Any) -> str:
    parts = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={value!r}")
    return " ".join(parts)


class _Spinner:
    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, message: str) -> None:
        self.message = message
        self._messages = [message]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame = 0
        self._visible = False

    def push(self, message: str) -> None:
        """Show a nested status on this same line until :meth:`pop`."""
        with _spinner_lock:
            self._messages.append(message)
            self.message = message
            self.render()

    def pop(self) -> None:
        """Restore the status that was visible before the matching :meth:`push`."""
        with _spinner_lock:
            if len(self._messages) > 1:
                self._messages.pop()
                self.message = self._messages[-1]
            if not self._stop.is_set():
                self.render()

    def start(self) -> None:
        self.render()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self.clear()

    def clear(self) -> None:
        if not self._visible:
            return
        sys.stderr.write("\r\033[2K")
        sys.stderr.flush()
        self._visible = False

    def render(self) -> None:
        if self._stop.is_set() or not sys.stderr.isatty():
            return
        frames = self._FRAMES
        frame = frames[self._frame % len(frames)]
        try:
            sys.stderr.write(f"\r\033[2K{frame} {self.message}")
            sys.stderr.flush()
        except UnicodeEncodeError:
            self._FRAMES = "|/-\\"
            frame = self._FRAMES[self._frame % len(self._FRAMES)]
            sys.stderr.write(f"\r\033[2K{frame} {self.message}")
            sys.stderr.flush()
        self._visible = True

    def _run(self) -> None:
        frames = itertools.cycle(range(len(self._FRAMES)))
        while not self._stop.wait(0.08):
            with _spinner_lock:
                self._frame = next(frames)
                self.render()


@contextmanager
def loading(message: str) -> Iterator[None]:
    """Animate a status line on stderr until the assistant (or setup) finishes.

    Nested calls share that one line: the inner message replaces the outer one
    and the outer message comes back when the inner block ends. A second
    spinner would repaint the same row and flicker between the two texts.

    Skipped when diagnostic logs are on (those already show progress) or when
    stderr is not a terminal.
    """
    global _active_spinner
    if _enabled or not sys.stderr.isatty():
        yield
        return
    with _spinner_lock:
        current = _active_spinner
    if current is not None and not current._stop.is_set():
        current.push(message)
        try:
            yield
        finally:
            current.pop()
        return
    spinner = _Spinner(message)
    with _spinner_lock:
        _active_spinner = spinner
    spinner.start()
    try:
        yield
    finally:
        spinner.stop()
        with _spinner_lock:
            if _active_spinner is spinner:
                _active_spinner = None
