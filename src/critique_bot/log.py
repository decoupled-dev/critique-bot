from __future__ import annotations

import itertools
import sys
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

_enabled = False
_spinner_lock = threading.Lock()
_active_spinner: _Spinner | None = None


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
        print(line, file=sys.stderr, flush=True)
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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame = 0
        self._visible = False

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

    Skipped when diagnostic logs are on (those already show progress) or when
    stderr is not a terminal.
    """
    global _active_spinner
    if _enabled or not sys.stderr.isatty():
        yield
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
