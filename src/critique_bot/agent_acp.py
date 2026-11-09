"""Agent Client Protocol front end for the agent: ``crit --acp``.

Lets an ACP client (Android Studio's Bring Your Own Agent, other JetBrains
IDEs, Zed) run crit as its coding agent. Like :mod:`critique_bot.agent_protocol`
this is a drop-in replacement for :mod:`critique_bot.agent_ui`: the agent loop
calls the same functions, and this module turns them into ACP messages.

The wire format is JSON-RPC 2.0, one message per line on stdin/stdout
(https://agentclientprotocol.com). What is implemented:

* ``initialize`` (protocol version 1, no sign-in methods), ``authenticate`` (no-op)
* ``session/new``: one workspace per process. The first one picks the
  workspace from its ``cwd``; later ones (same ``cwd``) start a fresh chat.
* ``session/prompt``: answered with a ``stopReason`` when the task is done.
* ``session/cancel``: stops the running task, the way Ctrl+C does.
* ``session/set_mode``: the ask / edits / auto / plan modes.
* ``session/update`` out: message chunks, tool calls (with diffs for edits),
  ``current_mode_update`` and ``available_commands_update``.
* ``session/request_permission`` out: approvals, plan review, and model
  questions with fixed answers.

A question without fixed answers ends the turn with the question as the reply;
the user's next prompt is the answer. The same goes for "No, keep planning":
the next prompt is the feedback.

The protocol owns the process's real stdin and stdout (see
:func:`agent_protocol._claim_stdio`); everything else that writes to fd 1 goes
to stderr, which ACP clients keep as the agent's log.
"""

from __future__ import annotations

import _thread
import itertools
import json
import os
import queue
import re
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Iterable

from critique_bot import agent_ui, roles

#: ACP protocol version this agent speaks.
PROTOCOL_VERSION = 1

NEW_CHAT = agent_ui.NEW_CHAT
MODES = agent_ui.MODES
MODE_LABELS = agent_ui.MODE_LABELS
MODE_NAMES = {"ask": "Ask", "edits": "Accept edits", "auto": "Auto", "plan": "Plan"}

#: Longest tool output, result, or diff text sent in one update.
MAX_TEXT = 20_000
#: Largest file read to show the old side of a whole-file write.
MAX_DIFF_FILE = 512_000

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#: Server-defined: the request is fine but crit cannot take it right now.
BUSY = -32000

_UNDO = "\x00undo"

_KINDS = {
    "read_files": "read",
    "list_files": "read",
    "git_status": "read",
    "git_diff": "read",
    "git_log": "read",
    "git_show": "read",
    "skill": "read",
    "command_output": "read",
    "find_files": "search",
    "search_code": "search",
    "write_files": "edit",
    "edit_file": "edit",
    "apply_patch": "edit",
    "delete_file": "delete",
    "move_file": "move",
    "run_command": "execute",
    "command_input": "execute",
    "kill_command": "execute",
    "web_fetch": "fetch",
    "todo": "think",
}

#: Slash commands offered in the IDE's prompt box.
COMMANDS = [
    {"name": "new", "description": "Start a fresh chat with the model"},
    {"name": "undo", "description": "Restore the files the last task changed"},
    {"name": "mode", "description": "Switch mode: ask, edits, auto, or plan", "input": {"hint": "ask | edits | auto | plan"}},
    {"name": "role", "description": "Switch the role crit plays in this project", "input": {"hint": "role id"}},
]

_NUMBERED = re.compile(r"^\s*(\d{1,2})[.)]\s+(.+?)\s*$")


@dataclass
class _State:
    workspace: Path = field(default_factory=Path.cwd)
    model: str = ""
    approve_mode: str = "ask"
    role: str = roles.GENERIC
    role_changed: str | None = None
    cache_dir: Path | None = None
    #: The cwd the client asked for in its first session/new.
    requested_cwd: Path | None = None
    #: What the client offered: initialize's clientInfo/clientCapabilities, session/new's mcpServers.
    client: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    #: session/new requests waiting for the agent to finish starting.
    pending_new: list[Any] = field(default_factory=list)
    #: The JSON-RPC id of the session/prompt in flight.
    prompt_id: Any = None
    cancelled: bool = False
    last_status: str = ""
    ready: bool = False
    busy: bool = False
    closed: bool = False
    #: Open tool calls: (name, args json) -> [toolCallId, ...], oldest first.
    calls: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    #: The tool call an approval opened, waiting for its tool_start / tool_done.
    approved: dict[str, str] = field(default_factory=dict)
    #: The tool call that receives live command output.
    live: str | None = None
    live_text: str = ""


_state = _State()
_lock = threading.RLock()
_write_lock = threading.Lock()
_out: IO[str] | None = None
_inbox: "queue.Queue[str | None]" = queue.Queue()
_answers: dict[Any, "queue.Queue[dict[str, Any] | None]"] = {}
_ids = itertools.count(1)
_call_ids = itertools.count(1)
_session_asked = threading.Event()
_generation = 0


# --------------------------------------------------------------------------- transport


def start(stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
    """Take over stdin/stdout and start reading messages (tests pass their own streams)."""
    global _out, _generation
    if stdout is None or stdin is None:
        from critique_bot.agent_protocol import _claim_stdio

        stdin, stdout = _claim_stdio()
    _out = stdout
    _state.closed = False
    _generation += 1
    threading.Thread(target=_read_loop, args=(stdin, _generation), name="crit-acp-in", daemon=True).start()


def _write(message: dict[str, Any]) -> None:
    out = _out
    if out is None:
        return
    line = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False, default=str)
    with _write_lock:
        try:
            out.write(line + "\n")
            out.flush()
        except (OSError, ValueError):
            pass


def _respond(request_id: Any, result: Any) -> None:
    _write({"id": request_id, "result": result})


def _fail(request_id: Any, code: int, message: str) -> None:
    _write({"id": request_id, "error": {"code": code, "message": message}})


def _notify(method: str, params: dict[str, Any]) -> None:
    _write({"method": method, "params": params})


def _update(update: dict[str, Any]) -> None:
    """A session/update for the open session. Dropped before the client has a session id."""
    with _lock:
        session = _state.session_id if _state.ready else None
    if session is not None:
        _notify("session/update", {"sessionId": session, "update": update})


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
                _write({"id": None, "error": {"code": PARSE_ERROR, "message": f"not JSON: {exc}"}})
                continue
            if not isinstance(message, dict):
                _write({"id": None, "error": {"code": INVALID_REQUEST, "message": "expected a JSON object"}})
                continue
            try:
                handle(message)
            except Exception as exc:  # one bad message must not stop the reader
                if "id" in message and "method" in message:
                    _fail(message.get("id"), INTERNAL_ERROR, str(exc))
                else:
                    print(f"acp: {message.get('method')}: {exc}", file=sys.stderr)
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
    _session_asked.set()


def handle(message: dict[str, Any]) -> None:
    """Act on one incoming message. Runs on the reader thread."""
    method = message.get("method")
    if not isinstance(method, str):
        # A response to one of our requests.
        with _lock:
            box = _answers.get(message.get("id"))
        if box is not None:
            box.put(message)
        return
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if "id" not in message:
        if method == "session/cancel":
            cancel(str(params.get("sessionId") or ""))
        return
    request_id = message["id"]
    handler = _REQUESTS.get(method)
    if handler is None:
        _fail(request_id, METHOD_NOT_FOUND, f"crit does not support {method}")
        return
    handler(request_id, params)


def _initialize(request_id: Any, params: dict[str, Any]) -> None:
    from critique_bot import __version__

    with _lock:
        _state.client["protocolVersion"] = params.get("protocolVersion")
        _state.client["clientInfo"] = params.get("clientInfo")
        _state.client["clientCapabilities"] = params.get("clientCapabilities")

    _respond(
        request_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
                "mcpCapabilities": {"http": False, "sse": False},
            },
            "authMethods": [],
            "agentInfo": {"name": "crit", "title": "crit", "version": __version__},
        },
    )


def _authenticate(request_id: Any, params: dict[str, Any]) -> None:
    _respond(request_id, {})


def _new_session(request_id: Any, params: dict[str, Any]) -> None:
    raw = str(params.get("cwd") or "").strip()
    cwd = Path(raw) if raw else Path.cwd()
    if not cwd.is_absolute() or not cwd.is_dir():
        _fail(request_id, INVALID_PARAMS, f"cwd must be an existing absolute folder, got {raw!r}")
        return
    cwd = cwd.resolve()
    with _lock:
        _state.client["mcpServers"] = redact_servers(params.get("mcpServers"))
        if _state.requested_cwd is None:
            _state.requested_cwd = cwd
            _state.pending_new.append(request_id)
            _session_asked.set()
            return
        # The workspace can be a parent of the folder the client asked for (a .bot found by
        # walking upward), so a later chat for that same folder must still match.
        served = _same_folder(cwd, _state.requested_cwd) or (
            _state.ready and _same_folder(cwd, _state.workspace)
        )
        if not served:
            _fail(
                request_id,
                INVALID_PARAMS,
                f"this crit process serves {_state.requested_cwd}; start another one for a different folder",
            )
            return
        if not _state.ready:
            _state.pending_new.append(request_id)
            return
        busy = _state.busy
    if busy:
        interrupt()
    session = _open_session()
    _inbox.put(NEW_CHAT)
    _respond(request_id, _session_result(session))
    _send_commands()


def _prompt(request_id: Any, params: dict[str, Any]) -> None:
    with _lock:
        if not _state.ready or params.get("sessionId") != _state.session_id:
            _fail(request_id, INVALID_PARAMS, f"unknown session {params.get('sessionId')!r}")
            return
        if _state.prompt_id is not None:
            _fail(request_id, BUSY, "a turn is already running; cancel it first")
            return
        text = prompt_text(params.get("prompt"))
        if not text.strip():
            _respond(request_id, {"stopReason": "end_turn"})
            return
        _state.prompt_id = request_id
        _state.cancelled = False
        _state.last_status = ""
    _inbox.put(text)


def _set_mode_request(request_id: Any, params: dict[str, Any]) -> None:
    mode = str(params.get("modeId") or "")
    if mode not in MODES:
        _fail(request_id, INVALID_PARAMS, f"unknown mode {mode!r}; use one of: {', '.join(MODES)}")
        return
    set_mode(mode)
    _respond(request_id, {})


_REQUESTS = {
    "initialize": _initialize,
    "authenticate": _authenticate,
    "session/new": _new_session,
    "session/prompt": _prompt,
    "session/set_mode": _set_mode_request,
}


def _same_folder(a: Path, b: Path | None) -> bool:
    if b is None:
        return False
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return False


def _open_session() -> str:
    session = f"crit-{uuid.uuid4().hex[:12]}"
    with _lock:
        _state.session_id = session
    return session


def _session_result(session: str) -> dict[str, Any]:
    return {
        "sessionId": session,
        "modes": {
            "currentModeId": _state.approve_mode,
            "availableModes": [
                {"id": name, "name": MODE_NAMES[name], "description": MODE_LABELS[name]} for name in MODES
            ],
        },
    }


def _send_commands() -> None:
    _update({"sessionUpdate": "available_commands_update", "availableCommands": COMMANDS})


_SECRET_KEY = re.compile(r"key|token|secret|pass|auth|cookie|credential", re.I)


def redact_servers(servers: Any) -> list[Any]:
    """session/new's mcpServers with env values and header values hidden (names kept)."""
    out: list[Any] = []
    for server in servers if isinstance(servers, list) else []:
        if not isinstance(server, dict):
            continue
        item = dict(server)
        for key in ("env", "headers"):
            entries = item.get(key)
            if isinstance(entries, list):
                item[key] = [
                    {**entry, "value": "***"} if isinstance(entry, dict) else "***" for entry in entries
                ]
        args = item.get("args")
        if isinstance(args, list):
            item["args"] = [
                "***"
                if _SECRET_KEY.search(str(prev)) or ("=" in str(arg) and _SECRET_KEY.search(str(arg).split("=", 1)[0]))
                else arg
                for prev, arg in zip([""] + args, args)
            ]
        url = item.get("url")
        if isinstance(url, str) and "?" in url:
            item["url"] = url.split("?", 1)[0] + "?***"
        out.append(item)
    return out


def _record_client() -> None:
    """Keep what the client offered in .bot/acp-client.json (and one log line), to see which IDE tools exist."""
    with _lock:
        info = json.loads(json.dumps(_state.client, default=str))
    servers = info.get("mcpServers") or []
    names = ", ".join(str(s.get("name") or s.get("command") or s.get("url") or "?") for s in servers if isinstance(s, dict))
    caps = info.get("clientCapabilities") or {}
    print(
        f"acp: client {json.dumps(info.get('clientInfo'))}, capabilities {json.dumps(caps)}, "
        f"{len(servers)} MCP server(s){': ' + names if names else ''}",
        file=sys.stderr,
    )
    bot = _state.workspace / ".bot"
    if bot.is_dir():
        try:
            (bot / "acp-client.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"acp: could not write {bot / 'acp-client.json'}: {exc}", file=sys.stderr)


def wait_for_session() -> Path | None:
    """Block until the client's first session/new names a folder. None when the client went away."""
    _session_asked.wait()
    with _lock:
        return None if _state.closed and _state.requested_cwd is None else _state.requested_cwd


def finish(code: int, reason: str = "") -> None:
    """The agent ended: answer whatever is still waiting so the client is not left hanging."""
    with _lock:
        waiting_new, _state.pending_new = list(_state.pending_new), []
        prompt, _state.prompt_id = _state.prompt_id, None
        _state.busy = False
    why = reason.strip() or f"crit stopped (exit code {code}); see the agent log"
    for request_id in waiting_new:
        _fail(request_id, INTERNAL_ERROR, why)
    if prompt is not None:
        if code:
            _say(f"crit stopped (exit code {code}).")
        _respond(prompt, {"stopReason": "end_turn"})


def cancel(session: str) -> None:
    with _lock:
        if session != _state.session_id or _state.prompt_id is None:
            return
        _state.cancelled = True
        pending = list(_answers.values())
    for box in pending:
        box.put(None)
    interrupt()


def interrupt() -> bool:
    """Stop the running task the way Ctrl+C does. Ignored while idle (it would end the session)."""
    with _lock:
        busy = _state.busy
    if busy:
        _thread.interrupt_main()
    return busy


def _client_request(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Send a request to the client and wait for its result. None on error, cancel, or a closed pipe."""
    request_id = f"crit-{next(_ids)}"
    box: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
    with _lock:
        if _state.closed or _state.cancelled:
            return None
        _answers[request_id] = box
    try:
        _write({"id": request_id, "method": method, "params": params})
        while True:
            try:
                # A short timeout keeps the main thread able to take an interrupt.
                reply = box.get(timeout=0.2)
            except queue.Empty:
                continue
            if reply is None or not isinstance(reply.get("result"), dict):
                return None
            return reply["result"]
    finally:
        with _lock:
            _answers.pop(request_id, None)


def _choose(title: str, options: list[tuple[str, str, str]], *, call_id: str | None = None, kind: str = "other",
            content: list[dict[str, Any]] | None = None) -> str | None:
    """Ask the client to pick one of ``[(id, label, kind)]``. The chosen id, or None."""
    if call_id is None:
        call_id = _new_call_id()
        _update({"sessionUpdate": "tool_call", "toolCallId": call_id, "title": title, "kind": kind,
                 "status": "pending", **({"content": content} if content else {})})
    tool_call: dict[str, Any] = {"toolCallId": call_id, "title": title}
    if content:
        tool_call["content"] = content
    result = _client_request(
        "session/request_permission",
        {
            "sessionId": _state.session_id,
            "toolCall": tool_call,
            "options": [{"optionId": oid, "name": label, "kind": okind} for oid, label, okind in options],
        },
    )
    outcome = (result or {}).get("outcome")
    if isinstance(outcome, dict) and outcome.get("outcome") == "selected":
        chosen = str(outcome.get("optionId") or "")
        if chosen in {oid for oid, _, _ in options}:
            return chosen
    return None


def _wait_for_reply(question: str) -> str | None:
    """End the turn with ``question`` and take the user's next prompt as the answer."""
    _say(question)
    _end_turn("end_turn")
    inbox = _inbox
    while True:
        try:
            item = inbox.get(timeout=0.2)
        except queue.Empty:
            continue
        if item is None or item == NEW_CHAT:
            if item == NEW_CHAT:
                _inbox.put(NEW_CHAT)  # the loop starts the new chat once this task is over
            return None
        if item == _UNDO:
            continue
        with _lock:
            _state.busy = True
        return item


def _end_turn(default: str) -> None:
    with _lock:
        prompt, _state.prompt_id = _state.prompt_id, None
        reason = "cancelled" if _state.cancelled else default
        _state.cancelled = False
        _state.busy = False
        _state.live, _state.live_text = None, ""
        _state.calls.clear()
        _state.approved.clear()
    if prompt is not None:
        _respond(prompt, {"stopReason": reason})


def prompt_text(blocks: Any) -> str:
    """The prompt's content blocks as one task text. Attached files become paths or inline text."""
    parts: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "resource_link":
            parts.append(f"@{_uri_path(str(block.get('uri') or ''))}")
        elif kind == "resource":
            resource = block.get("resource") if isinstance(block.get("resource"), dict) else {}
            where = _uri_path(str(resource.get("uri") or ""))
            text = resource.get("text")
            if isinstance(text, str):
                parts.append(f"Contents of {where}:\n```\n{text}\n```")
            else:
                parts.append(f"@{where}")
        elif kind in {"image", "audio"}:
            parts.append(f"[{kind} attachment left out: crit takes text only]")
    return "\n\n".join(part for part in parts if part.strip())


def _uri_path(uri: str) -> str:
    if not uri.startswith("file://"):
        return uri
    from urllib.parse import unquote, urlparse

    path = unquote(urlparse(uri).path)
    if re.match(r"^/[A-Za-z]:/", path):  # file:///C:/x on Windows
        path = path[1:]
    try:
        return str(Path(path).resolve().relative_to(_state.workspace.resolve()))
    except (ValueError, OSError):
        return path


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
        if approve_mode is not None:
            _state.approve_mode = normalize_mode(approve_mode)
        if cache_dir is not None:
            _state.cache_dir = Path(cache_dir)
        if role is not None:
            _state.role = roles.normalize_role(role) or roles.GENERIC
    return _state


def welcome_header(*, version: str | None = None) -> None:
    """The session/new answer carries this; it goes out when the agent first waits for a task."""


def install_log_bridge() -> None:
    """Logs already go to stderr, which the client keeps as the agent log."""


def shutdown() -> None:
    pass


def approve_mode() -> str:
    return _state.approve_mode


def set_mode(mode: str, *, announce: bool = True) -> str:
    key = str(mode or "").strip().lower()
    if key not in MODES and normalize_mode(key, "") == "":
        note("bad", f"Unknown mode {mode!r}; use one of: {', '.join(MODES)}.")
        return _state.approve_mode
    chosen = normalize_mode(key, _state.approve_mode)
    with _lock:
        _state.approve_mode = chosen
    _update({"sessionUpdate": "current_mode_update", "currentModeId": chosen})
    return chosen


def _set_role(word: str) -> None:
    chosen = roles.normalize_role(word)
    if chosen is None:
        note("bad", f"Unknown role {word!r}; use one of: {', '.join(roles.ROLE_IDS)}.")
        return
    with _lock:
        if chosen == _state.role:
            return
        _state.role = chosen
        _state.role_changed = chosen
    try:
        from critique_bot.bot_home import find_bot_home, update_settings

        home = find_bot_home(_state.workspace)
        if home is not None:
            update_settings(home, role=chosen)
    except Exception as exc:
        print(f"acp: could not save the role: {exc}", file=sys.stderr)
    note("good", f"Role: {roles.role_label(chosen)}")


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
    elif word in {"/plan", "/auto", "/ask", "/edits"}:
        set_mode(word[1:])
    else:
        note("bad", f"{word} is not available here.")


_IDLE_COMMANDS = {"/mode", "/permissions", "/role", "/undo", "/plan", "/auto", "/ask", "/edits"}


def read_message(**_ignored: Any) -> str | None:
    """The next task. Ends the turn in flight, then waits for the next session/prompt."""
    with _lock:
        first = not _state.ready
        if first:
            _state.ready = True
            waiting_new, _state.pending_new = list(_state.pending_new), []
    if first:
        session = _open_session()
        _record_client()
        for request_id in waiting_new:
            _respond(request_id, _session_result(session))
        _send_commands()
    _end_turn(_stop_reason(_state.last_status))
    inbox = _inbox
    while True:
        try:
            item = inbox.get(timeout=0.2)
        except queue.Empty:
            continue
        if item is None:
            return None
        if item == NEW_CHAT:
            return NEW_CHAT
        if item == _UNDO:
            _undo()
            continue
        text = item.strip()
        if text.startswith("/") and "\n" not in text:
            word, _, rest = text.partition(" ")
            word = word.lower()
            if word == NEW_CHAT:
                return NEW_CHAT  # the loop restarts the chat and calls back, which ends this turn
            if word in _IDLE_COMMANDS:
                handle_command(word, rest.strip())
                _end_turn("end_turn")
                continue
        with _lock:
            _state.busy = True
        return text


def _stop_reason(status: str) -> str:
    """ACP stopReason for the task's final status (``final_status`` stores ``CODE`` or ``CODE:reason``)."""
    code, _, reason = (status or "").partition(":")
    if code == "INTERRUPTED":
        return "cancelled"
    if code == "STOPPED" and "tool rounds" in reason:
        return "max_turn_requests"
    return "end_turn"


def _undo() -> None:
    if _state.cache_dir is None:
        note("bad", "Nothing to undo.")
        return
    from critique_bot.agent_edit import undo_last

    restored = undo_last(_state.cache_dir, _state.workspace)
    if not restored:
        note("note", "Nothing to undo.")
        return
    _say("Restored:\n" + "\n".join(f"- {path}" for path in restored))


def _say(text: str) -> None:
    if text and text.strip():
        _update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text.strip() + "\n\n"}})


def _think(text: str) -> None:
    if text and text.strip():
        _update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text.strip() + "\n"}})


def note(kind: str, message: str) -> None:
    if not message or kind == "task" or message.startswith("Ready. Type a task"):
        return
    if kind == "bad":
        _say(message)
    else:
        _think(message)


def assistant(text: str) -> None:
    _say(text)


def final_status(code: str, *, ok: bool, reason: str = "") -> None:
    code = (code or "").upper()
    with _lock:
        _state.last_status = f"{code}:{reason}"
    if not ok and code and code != "INTERRUPTED":
        _say(f"**{code}**" + (f": {reason}" if reason else ""))


def waiting(active: bool, label: str = "Thinking") -> None:
    """ACP clients draw their own busy indicator while a turn runs."""


def set_active(name: str, count: int) -> None:
    if count:
        _think(f"{count} helper chat{'s' if count != 1 else ''} working")


def show_diff(diff: str) -> bool:
    """The changes a task made: the IDE already shows each edit, so this is a short summary."""
    if not diff.strip():
        return False
    files = re.findall(r"^\+\+\+ (?:b/)?(.+)$", diff, flags=re.M)
    files = [name.strip() for name in files if name.strip() != "/dev/null"]
    if files:
        _think("Changed: " + ", ".join(files[:12]) + (" ..." if len(files) > 12 else ""))
    return True


# --------------------------------------------------------------------------- tool calls


def _new_call_id() -> str:
    return f"call-{next(_call_ids)}"


def _key(name: str, args: dict[str, Any] | None) -> tuple[str, str]:
    return (name, json.dumps(args or {}, sort_keys=True, default=str))


def _title(name: str, args: dict[str, Any]) -> str:
    try:
        from critique_bot.agent import ToolCall, _activity

        return _activity(ToolCall(name, dict(args)))
    except Exception:
        return name


def _abs(path: Any) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    target = Path(path)
    if not target.is_absolute():
        target = _state.workspace / target
    return str(target)


def _locations(args: dict[str, Any]) -> list[dict[str, Any]]:
    raw: list[Any] = [args.get("path"), args.get("file"), args.get("destination")]
    for key in ("paths", "files"):
        items = args.get(key)
        if isinstance(items, list):
            raw.extend(item.get("path") if isinstance(item, dict) else item for item in items[:20])
    seen: list[dict[str, Any]] = []
    for item in raw:
        path = _abs(item)
        if path and all(loc["path"] != path for loc in seen):
            seen.append({"path": path})
    return seen


def _text(text: str) -> dict[str, Any]:
    return {"type": "content", "content": {"type": "text", "text": text}}


def _edit_content(name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    """ACP diff blocks for an edit, so the IDE can show it as a diff."""
    out: list[dict[str, Any]] = []
    if name == "edit_file":
        path = _abs(args.get("path") or args.get("file"))
        edits = args.get("edits")
        if not isinstance(edits, list):
            edits = [edits] if isinstance(edits, dict) else [args]
        for edit in edits[:20]:
            if not isinstance(edit, dict):
                continue
            old = edit.get("old_string", edit.get("old"))
            new = edit.get("new_string", edit.get("new"))
            if path and isinstance(old, str) and isinstance(new, str):
                out.append({"type": "diff", "path": path, "oldText": old[:MAX_TEXT], "newText": new[:MAX_TEXT]})
    elif name == "write_files":
        files = args.get("files")
        if files is None and args.get("path") is not None:
            files = [{"path": args.get("path"), "contents": args.get("contents", args.get("content", ""))}]
        for item in files if isinstance(files, list) else []:
            if not isinstance(item, dict):
                continue
            path = _abs(item.get("path"))
            new = item.get("contents", item.get("content", ""))
            if not path or not isinstance(new, str):
                continue
            old: str | None = None
            try:
                target = Path(path)
                if target.is_file() and target.stat().st_size <= MAX_DIFF_FILE:
                    old = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                old = None
            out.append({"type": "diff", "path": path, "oldText": old, "newText": new[:MAX_DIFF_FILE]})
    elif name == "run_command":
        command = args.get("command") or args.get("cmd")
        if isinstance(command, str) and command.strip():
            out.append(_text(f"```\n$ {command.strip()}\n```"))
    return out


def tool_start(name: str, args: dict[str, Any] | None) -> None:
    args = dict(args or {})
    with _lock:
        call_id = _state.approved.pop(name, None) or _state.approved.pop("", None)
        fresh = call_id is None
        if fresh:
            call_id = _new_call_id()
        _state.calls.setdefault(_key(name, args), []).append(call_id)
        if name in {"run_command", "command_input"}:
            _state.live, _state.live_text = call_id, ""
    update: dict[str, Any] = {
        "toolCallId": call_id,
        "title": _title(name, args),
        "kind": _KINDS.get(name, "other"),
        "status": "in_progress",
        "locations": _locations(args),
        "rawInput": _safe(args),
    }
    content = _edit_content(name, args)
    if content:
        update["content"] = content
    _update({"sessionUpdate": "tool_call" if fresh else "tool_call_update", **update})


def tool_output(chunk: str) -> None:
    if not chunk:
        return
    with _lock:
        call_id = _state.live
        if call_id is None:
            return
        _state.live_text = (_state.live_text + chunk)[-MAX_TEXT:]
        text = _state.live_text
    _update({"sessionUpdate": "tool_call_update", "toolCallId": call_id, "content": [_text(f"```\n{text}\n```")]})


def tool_done(name: str, args: dict[str, Any] | None, result: dict[str, Any], *, friendly: str = "") -> None:
    args = dict(args or {})
    with _lock:
        open_ids = _state.calls.get(_key(name, args)) or []
        call_id = open_ids.pop(0) if open_ids else (_state.approved.pop(name, None) or _state.approved.pop("", None))
        if call_id == _state.live:
            _state.live, _state.live_text = None, ""
    ok = bool(result.get("ok"))
    error = str(result.get("error") or "")
    body = _result_text(result)
    if not ok:
        body = "\n\n".join(part for part in (friendly, error, body) if part)
    update: dict[str, Any] = {"status": "completed" if ok else "failed"}
    content: list[dict[str, Any]] = []
    if body and not (ok and name in {"edit_file", "write_files"}):  # an edit keeps the diff its start showed
        content = [_text(f"```\n{body[:MAX_TEXT]}\n```" if ok and name not in {"todo", "ask_user"} else body[:MAX_TEXT])]
    if content:
        update["content"] = content
    if call_id is None:
        # Ended without a start (a call refused before it ran): one call that is already over.
        _update({"sessionUpdate": "tool_call", "toolCallId": _new_call_id(), "title": _title(name, args),
                 "kind": _KINDS.get(name, "other"), "locations": _locations(args), "rawInput": _safe(args), **update})
        return
    _update({"sessionUpdate": "tool_call_update", "toolCallId": call_id, **update})


# --------------------------------------------------------------------------- questions


def approve(permission: Any, *, risky: str = "", **_ignored: Any) -> tuple[str, str]:
    """``("yes" | "always" | "no", reason)``, like :func:`agent_ui.approve`."""
    choices = agent_ui.approval_options(permission, risky=risky)
    kinds = {"yes": "allow_once", "always": "allow_always", "no": "reject_once", "auto": "allow_once"}
    labels = {"yes": "Yes", "no": "No"}
    options = [(value, labels.get(value, label), kinds.get(value, "allow_once")) for value, label in choices]
    summary = str(getattr(permission, "summary", "") or permission)
    detail = str(getattr(permission, "detail", "") or "")[:MAX_TEXT]
    perm_kind = str(getattr(permission, "kind", "") or "")
    tool = str(getattr(permission, "tool", "") or "")
    content: list[dict[str, Any]] = []
    if risky:
        content.append(_text(f"**Risky:** {risky}"))
    if detail:
        fence = "diff" if perm_kind == "edit" else ""
        content.append(_text(f"```{fence}\n{detail}\n```"))
    call_id = _new_call_id()
    kind = _KINDS.get(tool) or {"edit": "edit", "command": "execute", "network": "fetch"}.get(perm_kind, "other")
    locations = [{"path": p} for p in (_abs(item) for item in getattr(permission, "paths", ()) or ()) if p]
    _update({"sessionUpdate": "tool_call", "toolCallId": call_id, "title": summary, "kind": kind,
             "status": "pending", "locations": locations, **({"content": content} if content else {})})
    with _lock:
        _state.approved[tool] = call_id
    chosen = _choose(summary, options, call_id=call_id, content=content or None)
    if chosen is None or chosen == "no":
        return ("no", "")
    if chosen == "auto":
        set_mode("auto")
        return ("yes", "")
    return (chosen, "")


def review_plan(**_ignored: Any) -> tuple[str, str]:
    """``("auto" | "edits" | "ask", "")``, ``("no", feedback)``, or ``("stop", "")``."""
    options = [
        (value, label, "reject_once" if value == "no" else "allow_once") for value, label in agent_ui.PLAN_OPTIONS
    ]
    chosen = _choose("Start on this plan?", options, kind="switch_mode")
    if chosen in {"auto", "edits", "ask"}:
        set_mode(chosen)
        return (chosen, "")
    if chosen == "no":
        feedback = (_wait_for_reply("What should change in the plan?") or "").strip()
        if feedback:
            return ("no", feedback)
    return ("stop", "")


def ask_question(question: str, options: list[str] | None = None, **_ignored: Any) -> str | None:
    """The model asks the user. Fixed answers become a choice; otherwise the next prompt answers it."""
    text = question.strip()
    choices = [str(item).strip() for item in options or [] if str(item).strip()]
    if not choices:
        text, choices = _split_numbered(text)
    if choices:
        picks = [(str(index), label[:200], "allow_once") for index, label in enumerate(choices, start=1)]
        picks.append(("chat", "Something else (answer in chat)", "reject_once"))
        chosen = _choose(text or "crit has a question", picks, kind="other")
        if chosen is None:
            return None
        if chosen != "chat":
            return choices[int(chosen) - 1]
        return _wait_for_reply(question.strip())
    return _wait_for_reply(text)


def _split_numbered(text: str) -> tuple[str, list[str]]:
    """``"Which?\\n1. a\\n2. b"`` -> ``("Which?", ["a", "b"])`` when the lines number 1..n at the end."""
    lines = text.splitlines()
    numbered: list[tuple[int, str]] = []
    while lines:
        match = _NUMBERED.match(lines[-1])
        if not match:
            break
        numbered.insert(0, (int(match.group(1)), match.group(2)))
        lines.pop()
    if len(numbered) < 2 or [number for number, _ in numbered] != list(range(1, len(numbered) + 1)):
        return text, []
    return "\n".join(lines).strip(), [label for _, label in numbered]


# --------------------------------------------------------------------------- helpers


def _safe(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
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
    global _out, _state, _inbox, _answers, _generation, _session_asked
    _generation += 1
    _out = None
    _state = _State()
    _inbox = queue.Queue()
    _answers = {}
    _session_asked = threading.Event()
