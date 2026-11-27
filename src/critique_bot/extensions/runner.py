# SPDX-License-Identifier: Apache-2.0
"""Running an extension's program: one argv, never a shell.

Every extension process (a manifest tool, a hook, a stdio MCP server's launch line goes
through :func:`argv_env`) starts here, so Stage 4's sandbox replaces one function
(:func:`use_runner`) instead of finding every caller. Today it uses the same building
blocks as ``run_command`` (``agent_shell``): a scrubbed environment (no tokens or keys),
bounded output, and the whole process tree killed on timeout or cancel.
"""

from __future__ import annotations

import os
# Running the user's tool is the point; argv only, never a shell.
import subprocess  # nosec B404
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from critique_bot import agent_shell

__all__ = ["ArgvResult", "argv_env", "run_argv", "use_popen", "use_runner"]

#: Starts the process for the default runner. ``agent_shell`` installs the session's OS
#: sandbox here (``use_popen``), so extension programs run under the same policy as
#: run_command; None means plain ``subprocess.Popen``.
_popen = None


def use_popen(starter):  # type: ignore[no-untyped-def]
    """Start extension processes with ``starter(argv, **popen_kwargs)``; None restores Popen."""
    global _popen
    old = _popen
    _popen = starter
    return old

#: Bytes kept of stdout and of stderr (head and tail are kept when more arrive).
MAX_CAPTURE = 2_000_000


@dataclass
class ArgvResult:
    exit_code: int | None  # None: timed out or cancelled
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False
    cancelled: bool = False
    error: str = ""  # could not start


def argv_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment an extension process gets: the user's, without secrets, plus ``extra``."""
    return agent_shell.scrubbed_environment(dict(extra or {}))


class _Bounded:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def add(self, chunk: bytes) -> None:
        room = self.limit // 2 - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > self.limit // 2:
                cut = len(self.tail) - self.limit // 2
                self.dropped += cut
                del self.tail[:cut]

    def text(self) -> str:
        middle = f"\n... {self.dropped} bytes dropped ...\n".encode() if self.dropped else b""
        return bytes(self.head + middle + self.tail).decode("utf-8", errors="replace")


def _pump(pipe, sink: _Bounded) -> None:
    try:
        for chunk in iter(lambda: pipe.read(65536), b""):
            sink.add(chunk)
    except (OSError, ValueError):
        pass


def _default_run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdin: bytes | None,
    timeout: float,
    cancel: threading.Event | None,
) -> ArgvResult:
    started = time.monotonic()
    try:
        # Argv list, shell=False; a manifest the user approved.
        proc = (_popen or subprocess.Popen)(  # nosec B603
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
    except (OSError, ValueError) as exc:
        return ArgvResult(None, "", "", 0.0, error=f"could not start {argv[0]!r}: {exc}")
    out, err = _Bounded(MAX_CAPTURE), _Bounded(MAX_CAPTURE)
    readers = [
        threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, err), daemon=True),
    ]
    for reader in readers:
        reader.start()
    if stdin is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin)
        except OSError:
            pass
        try:
            proc.stdin.close()
        except OSError:
            pass
    deadline = started + max(0.1, timeout)
    timed_out = cancelled = False
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        try:
            proc.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            pass
    if timed_out or cancelled:
        agent_shell.kill_tree(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    for reader in readers:
        reader.join(timeout=2)
    for pipe in (proc.stdout, proc.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:
            pass
    code = None if (timed_out or cancelled) else proc.returncode
    return ArgvResult(code, out.text(), err.text(), time.monotonic() - started, timed_out, cancelled)


Runner = Callable[..., ArgvResult]
_runner: Runner = _default_run


def use_runner(new: Runner | None) -> Runner:
    """Install another runner (the Stage 4 sandbox, a test fake); None restores the default."""
    global _runner
    old = _runner
    _runner = new or _default_run
    return old


def run_argv(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> ArgvResult:
    """Run ``argv`` (no shell) in ``cwd`` and return its bounded output."""
    if not argv or not all(isinstance(part, str) for part in argv):
        return ArgvResult(None, "", "", 0.0, error="argv must be a non-empty list of strings")
    return _runner(list(argv), cwd=Path(cwd), env=env if env is not None else argv_env(), stdin=stdin,
                   timeout=timeout, cancel=cancel)
