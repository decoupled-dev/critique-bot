"""The engine's side of the UI: everything the loop shows or asks goes through here.

The loop never imports a front end. It calls :func:`current`, the object installed with
:func:`use_ui` (process-wide: the terminal, or an IDE over ACP) or :func:`use_thread_ui`
(one session's worker thread: the Engine API's request events). Until one is installed
the loop runs headless on :class:`ui_api.NullUI`.
"""

from __future__ import annotations

import re
import sys
import threading
from typing import Any

from critique_bot import log, ui_api
from critique_bot.engine.parse import _STATUS_OK, ToolCall, _hide_tool_markup
from critique_bot.engine.text import _one_line
from critique_bot.tool_api import canonical_tool

#: The process-wide UI (see ui_api). use_ui sets it; NullUI until then.
_installed: Any = ui_api.NullUI()
#: A session's own UI on its worker thread (use_thread_ui), which wins over _installed there.
_local = threading.local()


def current() -> Any:
    """The UI for the calling thread: its own (use_thread_ui), else the process-wide one."""
    return getattr(_local, "ui", None) or _installed


def use_thread_ui(module: Any) -> None:
    """Give the calling thread its own UI (None: back to the process-wide one).

    The Engine runs each session on a worker thread with an event UI, so several sessions
    in one process ask and show through their own request events.
    """
    _local.ui = module


def __getattr__(name: str) -> Any:
    # ``ui.agent_ui`` (and ``agent.agent_ui``) read the UI installed now.
    if name == "agent_ui":
        return current()
    raise AttributeError(name)


_REVIEW_LINES = 80


def _ui_approve_mode() -> str:
    """The approval mode the terminal UI holds now, or "" when there is no UI."""
    getter = getattr(current(), "approve_mode", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:
        return ""


def _show_diff_in_ui(diff: str) -> bool:
    """Print the diff through the terminal UI's colored renderer. False when that is not available."""
    ui = current()
    show = getattr(ui, "show_diff", None)
    if callable(show):
        return bool(show(diff))
    render = getattr(ui, "render_diff", None)
    printer = getattr(ui, "_print", None)
    if not callable(render) or not callable(printer):
        return False
    chunks: list[tuple[str, list[str]]] = []
    lines = diff.splitlines()
    for number, line in enumerate(lines):
        if line.startswith("--- ") and number + 1 < len(lines) and lines[number + 1].startswith("+++ "):
            name = re.sub(r"^\+\+\+ (?:b/)?", "", lines[number + 1]).strip()
            if name == "/dev/null":
                name = re.sub(r"^--- (?:a/)?", "", line).strip()
            chunks.append((name, []))
        if not chunks:
            chunks.append(("", []))
        chunks[-1][1].append(line)
    try:
        budget = _REVIEW_LINES
        for name, body in chunks:
            if budget <= 0:
                break
            rows = render("\n".join(body), max_lines=min(budget, 40))
            if name:
                _ui("good", name)
            if rows:
                printer(*rows, sep="\n")
            budget -= len(rows) or 1
    except Exception as exc:
        log.debug(f"colored diff failed: {exc}")
        return False
    return True


def use_ui(module: Any) -> None:
    """Install the UI: ``agent_ui`` for the terminal, ``agent_acp`` for an IDE over ACP.

    Every hook below looks ``agent_ui`` up at call time, so this one assignment moves
    all output, approvals, and questions to ``module``. ``None`` goes back to NullUI.
    """
    global _installed
    _installed = module if module is not None else ui_api.NullUI()


def _paint(text: str, color: str) -> str:
    return log.paint(text, color, file=sys.stderr)


def _present_text(text: str) -> str:
    return _hide_tool_markup(text)


def _ui(kind: str, message: str) -> None:
    message = _present_text(message)
    if not message:
        return
    current().note(kind, _one_line(message, 160))


def _print_status(code: str) -> None:
    current().final_status(code, ok=code in _STATUS_OK)


def _call_paths(call: ToolCall) -> str:
    args = call.arguments
    path = args.get("path")
    paths = args.get("paths")
    if not path and isinstance(paths, list) and paths:
        path = ", ".join(str(item) for item in paths[:3])
    files = args.get("files")
    if not path and isinstance(files, list) and files and isinstance(files[0], dict):
        path = files[0].get("path", "")
    if not path and args.get("symbol"):
        path = str(args.get("symbol"))
    text = str(path or "").strip()
    return "the project" if text in {"", "."} else text


def _activity(call: ToolCall) -> str:
    args = call.arguments
    where = _call_paths(call)
    name = canonical_tool(call.tool) or call.tool
    messages = {
        "list_files": f"Looking through {where}",
        "find_files": f"Finding files matching {_one_line(str(args.get('glob') or args.get('pattern') or ''), 40)}",
        "read_files": f"Reading {where}",
        "search_code": f"Searching for {_one_line(str(args.get('pattern') or 'the code'), 40)}",
        "write_files": f"Writing {where}",
        "edit_file": f"Updating {where}",
        "delete_file": f"Removing {where}",
        "run_command": f"Running {_one_line(str(args.get('command') or 'a command'), 60)}",
        "git_status": "Checking what changed",
        "git_diff": "Reviewing the changes",
        "git_log": "Reading recent commits",
        "git_show": f"Showing {_one_line(str(args.get('rev') or args.get('commit') or 'HEAD'), 40)}",
        "apply_patch": "Applying the changes",
        "todo": "Updating the task list",
        "skill": f"Loading {_one_line(str(args.get('name') or args.get('skill') or 'skills'), 40)}",
    }
    return messages.get(name, "Working on the next step")


def _friendly_error(error: str) -> str:
    text = error.lower()
    if "denied" in text:
        return "You denied that step."
    if "invalid tool json" in text or "delimiter" in text or "tool call" in text:
        return "That step wasn't readable. Trying again."
    if "same text" in text:
        return "That change was already in the file."
    if "syntax" in text and "not applied" in text:
        return "That edit would break the file, so it was not applied."
    if "old_string" in text:
        return "Couldn't find the exact text to change."
    if "task is read-only" in text:
        return "This task only reads; that step was skipped."
    if "not run:" in text:
        return "That command used the wrong shell syntax."
    if "unknown tool" in text:
        return "That step isn't available."
    if "timed out" in text:
        return "That took too long and was stopped."
    if "command failed" in text:
        return "That command didn't succeed."
    if "not found" in text:
        return "Couldn't find that file."
    if "directory" in text or "folder" in text:
        return "That path is a folder, not a file."
    return "That step didn't work. Trying another way."


def _ui_result(result: dict[str, Any]) -> None:
    if result.get("ok"):
        return
    _ui("bad", _friendly_error(str(result.get("error") or "failed")))


def _emit(text: str) -> None:
    visible = _present_text(text)
    if not visible:
        return
    current().assistant(visible)


def _read_message() -> str | None:
    """The next task. ``None`` exits; ``"/new"`` (agent_ui.NEW_CHAT) asks for a fresh chat."""
    return current().read_message()


def _tool_start(call: ToolCall) -> None:
    name = canonical_tool(call.tool) or call.tool
    current().tool_start(name, call.arguments)


def _tool_done(call: ToolCall, result: dict[str, Any]) -> None:
    name = canonical_tool(call.tool) or call.tool
    friendly = "" if result.get("ok") else _friendly_error(str(result.get("error") or "failed"))
    current().tool_done(name, call.arguments, result, friendly=friendly)


def _tool_output(text: str) -> None:
    current().tool_output(text)


def _set_active(name: str, count: int) -> None:
    setter = getattr(current(), "set_active", None)
    if callable(setter):
        setter(name, count)


def _waiting(active: bool, label: str = "Thinking") -> None:
    current().waiting(active, label)


def _approve_prompt(permission: Any, *, risky: str = "") -> tuple[str, str]:
    return current().approve(permission, risky=risky)


def _review_plan() -> tuple[str, str]:
    return current().review_plan()


def _ask_user(question: str, options: list[str] | None = None) -> str | None:
    """Ask the person a question from the model. None when there is no terminal."""
    return current().ask_question(_present_text(question), options)
