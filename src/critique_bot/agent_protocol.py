"""JSON-lines front end for the agent: ``crit --protocol``.

A drop-in replacement for :mod:`critique_bot.agent_ui`. The agent loop calls
the same functions; instead of drawing a terminal, each one writes an event as
one JSON object per line on stdout, and questions (approvals, plan review,
model questions) wait for an answer line on stdin. A GUI (the VS Code
extension, later the Electron app) spawns ``crit --protocol`` and speaks this.

Events out (``{"type": ...}``): hello, ready, task, note, assistant, thinking,
tool_start, tool_output, tool_done, active, approval, plan_review, question,
cancel, diff, mode, role, status, error, bye.

Messages in: prompt {text}, answer {id, value, reason?}, set_mode {mode},
set_role {role}, new_chat, undo, interrupt, shutdown.

The protocol owns the process's real stdin and stdout. Everything else that
writes to fd 1 (a stray print, a child process) goes to stderr, and children
get an empty stdin, so neither can corrupt or swallow protocol lines.
"""

from __future__ import annotations

import _thread
import itertools
import json
import os
import queue
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Iterable

from critique_bot import agent_ui, roles

#: Bumped on any incompatible change to the events or messages above.
PROTOCOL_VERSION = 1

NEW_CHAT = agent_ui.NEW_CHAT
MODES = agent_ui.MODES
MODE_LABELS = agent_ui.MODE_LABELS

#: Longest tool output or result text sent in one event.
MAX_TEXT = 20_000

_UNDO = "\x00undo"


@dataclass
class _State:
    workspace: Path = field(default_factory=Path.cwd)
    model: str = ""
    shell_label: str = ""
    approve_mode: str = "ask"
    role: str = roles.GENERIC
    role_changed: str | None = None
    cache_dir: Path | None = None
    tools: list[str] = field(default_factory=list)
    busy: bool = False
    closed: bool = False


_state = _State()
_lock = threading.RLock()
_write_lock = threading.Lock()
_out: IO[str] | None = None
_inbox: "queue.Queue[str | None]" = queue.Queue()
_answers: dict[str, "queue.Queue[dict[str, Any] | None]"] = {}
_ids = itertools.count(1)
_reader: threading.Thread | None = None
#: Bumped by each start(), so a reader from an earlier start cannot close a later one.
_generation = 0


# --------------------------------------------------------------------------- transport


def start(stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
    """Take over stdin/stdout for the protocol and start reading messages.

    With no arguments, the process's fd 0 and fd 1 are moved to private
    descriptors and fd 0/1 are pointed at the null device and stderr. Tests
    pass their own streams instead.
    """
    global _out, _reader, _generation
    if stdout is None or stdin is None:
        stdin, stdout = _claim_stdio()
    _out = stdout
    _state.closed = False
    _generation += 1
    _reader = threading.Thread(
        target=_read_loop, args=(stdin, _generation), name="crit-protocol-in", daemon=True
    )
    _reader.start()


def _claim_stdio() -> tuple[IO[str], IO[str]]:
    sys.stdout.flush()
    out_fd = os.dup(1)
    in_fd = os.dup(0)
    os.dup2(2, 1)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    sys.stdout = sys.stderr
    try:
        sys.stdin = open(os.devnull, encoding="utf-8")
    except OSError:
        pass
    out = os.fdopen(out_fd, "w", encoding="utf-8", newline="\n", buffering=1)
    inp = os.fdopen(in_fd, "r", encoding="utf-8", errors="replace")
    return inp, out


def send(event: str, /, **fields: Any) -> None:
    """Write one event. Safe from any thread; a closed pipe is ignored."""
    out = _out
    if out is None:
        return
    line = json.dumps({"type": event, **fields}, ensure_ascii=False, default=str)
    with _write_lock:
        try:
            out.write(line + "\n")
            out.flush()
        except (OSError, ValueError):
            pass


def _read_loop(stream: IO[str], generation: int) -> None:
    try:
        for raw in stream:
            if generation != _generation:
                return
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError as exc:
                send("error", message=f"not JSON: {exc}")
                continue
            if not isinstance(message, dict):
                send("error", message="each message must be a JSON object")
                continue
            try:
                handle(message)
            except Exception as exc:  # one bad message must not stop the reader
                send("error", message=f"{message.get('type')}: {exc}")
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass
    if generation == _generation:
        _close_input()


def _close_input() -> None:
    with _lock:
        _state.closed = True
        pending = list(_answers.values())
    for box in pending:
        box.put(None)
    _inbox.put(None)


def handle(message: dict[str, Any]) -> None:
    """Act on one incoming message. Runs on the reader thread."""
    kind = str(message.get("type") or "")
    if kind == "prompt":
        text = str(message.get("text") or "")
        if text.strip():
            _inbox.put(text)
    elif kind == "answer":
        with _lock:
            box = _answers.get(str(message.get("id") or ""))
        if box is None:
            send("error", message=f"no open request {message.get('id')!r}")
            return
        box.put(message)
    elif kind == "set_mode":
        set_mode(str(message.get("mode") or ""))
    elif kind == "set_role":
        _set_role(str(message.get("role") or ""))
    elif kind == "new_chat":
        _inbox.put(NEW_CHAT)
    elif kind == "undo":
        _inbox.put(_UNDO)
    elif kind == "interrupt":
        interrupt()
    elif kind == "shutdown":
        _close_input()
    else:
        send("error", message=f"unknown message type {kind!r}")


def interrupt() -> bool:
    """Stop the running task the way Ctrl+C does. Ignored while idle (it would end the session)."""
    with _lock:
        busy = _state.busy
    if busy:
        _thread.interrupt_main()
    return busy


def _request(event: str, /, **fields: Any) -> dict[str, Any] | None:
    """Send a request and wait for its answer. None when the GUI went away."""
    request_id = f"r{next(_ids)}"
    box: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
    with _lock:
        if _state.closed:
            return None
        _answers[request_id] = box
    answered = False
    try:
        send(event, id=request_id, **fields)
        while True:
            try:
                # A short timeout keeps the main thread able to take an interrupt.
                reply = box.get(timeout=0.2)
            except queue.Empty:
                continue
            answered = reply is not None
            return reply
    finally:
        with _lock:
            _answers.pop(request_id, None)
        if not answered:
            send("cancel", id=request_id)


# --------------------------------------------------------------------------- agent_ui surface


def normalize_mode(raw: Any, default: str = "ask") -> str:
    return agent_ui.normalize_mode(raw, default)


def configure(
    *,
    workspace: Path | None = None,
    model: str | None = None,
    shell: Any = None,
    approve_mode: str | None = None,
    theme: Any = None,
    history_path: Path | None = None,
    cache_dir: Path | None = None,
    session_shell: Any = None,
    tools: Iterable[str] | None = None,
    role: Any = None,
) -> _State:
    with _lock:
        if workspace is not None:
            _state.workspace = Path(workspace)
        if model is not None:
            _state.model = model
        if shell is not None:
            _state.shell_label = str(getattr(shell, "label", "") or shell)
        if approve_mode is not None:
            _state.approve_mode = normalize_mode(approve_mode)
        if cache_dir is not None:
            _state.cache_dir = Path(cache_dir)
        if tools is not None:
            _state.tools = list(tools)
        if role is not None:
            _state.role = roles.normalize_role(role) or roles.GENERIC
    return _state


def welcome_header(*, version: str | None = None) -> None:
    if version is None:
        from critique_bot import __version__ as version
    send(
        "hello",
        protocol=PROTOCOL_VERSION,
        version=version,
        workspace=str(_state.workspace),
        model=_state.model,
        shell=_state.shell_label,
        mode=_state.approve_mode,
        modes=[{"id": name, "label": MODE_LABELS[name]} for name in MODES],
        role=_state.role,
        roles=[{"id": role.id, "label": roles.role_label(role.id), "summary": role.summary} for role in roles.ROLES],
    )


def install_log_bridge() -> None:
    """Logs already go to stderr; the GUI shows them in its output channel."""


def shutdown() -> None:
    send("thinking", active=False, label="")


def approve_mode() -> str:
    return _state.approve_mode


def set_mode(mode: str, *, announce: bool = True) -> str:
    key = str(mode or "").strip().lower()
    if key not in MODES and normalize_mode(key, "") == "":
        send("error", message=f"unknown mode {mode!r}; use one of: {', '.join(MODES)}")
        return _state.approve_mode
    chosen = normalize_mode(key, _state.approve_mode)
    with _lock:
        _state.approve_mode = chosen
    send("mode", mode=chosen, label=MODE_LABELS[chosen])
    return chosen


def _set_role(word: str) -> None:
    chosen = roles.normalize_role(word)
    if chosen is None:
        send("error", message=f"unknown role {word!r}; use one of: {', '.join(roles.ROLE_IDS)}")
        return
    with _lock:
        if chosen == _state.role:
            return
        _state.role = chosen
        _state.role_changed = chosen
    saved = agent_ui._save_setting(role=chosen) if _state.workspace else False
    send("role", role=chosen, label=roles.role_label(chosen), saved=bool(saved))


def take_role_change() -> str | None:
    with _lock:
        chosen, _state.role_changed = _state.role_changed, None
    return chosen


def take_shell_change() -> Any:
    return None


def pinned_skills() -> list[str]:
    return []


def handle_command(word: str, rest: str = "") -> None:
    if word in {"/mode", "/permissions"}:
        set_mode(rest)
    elif word == "/role":
        _set_role(rest)
    elif word == "/undo":
        _undo()
    elif word in {"/plan", "/auto"}:
        set_mode(word[1:])
    else:
        note("bad", f"{word} is not available here.")


def read_message(**_ignored: Any) -> str | None:
    """The next task. Waits for a prompt; handles undo and typed slash commands while idle."""
    with _lock:
        _state.busy = False
    while True:
        send("ready", mode=_state.approve_mode, role=_state.role)
        while True:
            try:
                item = _inbox.get(timeout=0.2)
                break
            except queue.Empty:
                continue
        if item is None:
            return None
        if item == _UNDO:
            _undo()
            continue
        text = item.strip()
        if text.startswith("/") and "\n" not in text:
            word, _, rest = text.partition(" ")
            word = word.lower()
            if word in {"/exit", "/quit"}:
                return None
            if word == NEW_CHAT:
                return NEW_CHAT
            if word in {"/mode", "/permissions", "/role", "/undo", "/plan", "/auto"}:
                handle_command(word, rest)
                continue
        with _lock:
            _state.busy = True
        return text


def _undo() -> None:
    if _state.cache_dir is None:
        note("bad", "Nothing to undo.")
        return
    from critique_bot.agent_edit import undo_last

    restored = undo_last(_state.cache_dir, _state.workspace)
    if not restored:
        note("note", "Nothing to undo.")
        return
    send("undone", paths=[str(path) for path in restored])
    for path in restored:
        note("good", f"Restored {path}")


def note(kind: str, message: str) -> None:
    if message:
        send("note", kind=kind, text=message)


def assistant(text: str) -> None:
    if text and text.strip():
        send("assistant", text=text.strip())


def final_status(code: str, *, ok: bool, reason: str = "") -> None:
    send("status", code=(code or "").upper(), ok=bool(ok), reason=reason)


def waiting(active: bool, label: str = "Thinking") -> None:
    send("thinking", active=bool(active), label=label if active else "")


def tool_start(name: str, args: dict[str, Any] | None) -> None:
    send("tool_start", name=name, args=_safe_args(args))


def tool_output(chunk: str) -> None:
    if chunk:
        send("tool_output", text=chunk[-MAX_TEXT:])


def tool_done(name: str, args: dict[str, Any] | None, result: dict[str, Any], *, friendly: str = "") -> None:
    send(
        "tool_done",
        name=name,
        args=_safe_args(args),
        ok=bool(result.get("ok")),
        error=str(result.get("error") or ""),
        friendly=friendly,
        result=_result_text(result),
    )


def set_active(name: str, count: int) -> None:
    send("active", name=name, count=max(0, int(count)))


def show_diff(diff: str) -> bool:
    """The changes a task made, as a unified diff."""
    if not diff.strip():
        return False
    send("diff", diff=diff[: MAX_TEXT * 5])
    return True


def approve(permission: Any, *, risky: str = "", **_ignored: Any) -> tuple[str, str]:
    """``("yes" | "always" | "no", reason)``, like :func:`agent_ui.approve`."""
    if _state.approve_mode == "auto" and not risky:
        return ("yes", "")
    options = agent_ui.approval_options(permission, risky=risky)
    reply = _request(
        "approval",
        kind=str(getattr(permission, "kind", "") or ""),
        summary=str(getattr(permission, "summary", "") or permission),
        detail=str(getattr(permission, "detail", "") or "")[:MAX_TEXT],
        risky=risky,
        options=[{"value": value, "label": label} for value, label in options],
    )
    if reply is None:
        return ("no", "the GUI closed before answering")
    value = str(reply.get("value") or "no")
    allowed = {item[0] for item in options}
    if value not in allowed:
        value = "no"
    if value == "auto":
        set_mode("auto")
        return ("yes", "")
    if value == "no":
        return ("no", str(reply.get("reason") or "").strip())
    return (value, "")


def review_plan(**_ignored: Any) -> tuple[str, str]:
    """``("auto" | "edits" | "ask", "")``, ``("no", feedback)``, or ``("stop", "")``."""
    reply = _request(
        "plan_review",
        options=[{"value": value, "label": label} for value, label in agent_ui.PLAN_OPTIONS],
    )
    if reply is None:
        return ("stop", "")
    value = str(reply.get("value") or "stop")
    if value in {"auto", "edits", "ask"}:
        set_mode(value)
        return (value, "")
    feedback = str(reply.get("reason") or "").strip()
    if value == "no" and feedback:
        return ("no", feedback)
    return ("stop", "")


def ask_question(question: str, options: list[str] | None = None, **_ignored: Any) -> str | None:
    choices = [str(item).strip() for item in options or [] if str(item).strip()]
    reply = _request("question", text=question.strip(), options=choices)
    if reply is None:
        return None
    answer = str(reply.get("value") or "").strip()
    return answer or None


# --------------------------------------------------------------------------- helpers


def _safe_args(args: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if isinstance(value, str) and len(value) > MAX_TEXT:
            value = value[:MAX_TEXT] + "\n...[truncated]"
        out[str(key)] = value
    return json.loads(json.dumps(out, default=str))


def _result_text(result: dict[str, Any]) -> str:
    for key in ("output", "content", "text", "stdout", "result"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value[:MAX_TEXT]
    return ""


def reset_for_tests() -> None:
    """Forget all state (tests only)."""
    global _out, _state, _inbox, _answers, _generation
    _generation += 1  # an earlier reader that ends now must not close the fresh state
    _out = None
    _state = _State()
    _inbox = queue.Queue()
    _answers = {}
