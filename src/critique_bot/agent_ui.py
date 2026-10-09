"""Terminal UI for ``crit``: a Claude-Code-style screen on top of rich and prompt_toolkit.

One :class:`rich.console.Console` writes everything (stdout). A single live
region shows the spinner while the web model replies and the tail of a running
command. Every print goes through one lock so the spinner never tears a line.
When stdout is not a terminal the same calls print plain text: no spinner, no
ANSI, no prompts. ``NO_COLOR`` keeps the layout and drops the colors.

The input box is a prompt_toolkit session: persistent history in
``.bot/history``, Enter submits, Alt+Enter / Ctrl+J / a trailing backslash add a
line, ``/`` opens the command menu, ``@`` completes workspace paths.
"""

from __future__ import annotations

import itertools
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from critique_bot import log

NEW_CHAT = "/new"

#: Shift+Tab cycles through these, in this order.
MODES = ("ask", "edits", "auto", "plan")
MODE_LABELS = {
    "ask": "ask before edits and commands",
    "edits": "accept edits on (commands still ask)",
    "auto": "auto mode on (asks only for risky commands)",
    "plan": "plan mode on (reads only, then a plan to approve)",
}
_MODE_ALIASES = {
    "default": "ask", "ask": "ask", "normal": "ask", "manual": "ask",
    "edits": "edits", "edit": "edits", "accept": "edits", "accept-edits": "edits", "acceptedits": "edits",
    "accept_edits": "edits",
    "auto": "auto", "yes": "auto", "yolo": "auto", "bypass": "auto", "bypasspermissions": "auto", "allow": "auto",
    "plan": "plan", "planning": "plan",
}


def normalize_mode(raw: Any, default: str = "ask") -> str:
    """``ask``, ``edits``, ``auto``, or ``plan`` from a setting, flag, or command word."""
    key = str(raw or "").strip().lower().replace(" ", "-")
    return _MODE_ALIASES.get(key, default)
_QUIT_WORDS = frozenset({"exit", "quit", "/exit", "/quit", "/q"})

# --------------------------------------------------------------------------- theme

_CODE_THEMES = {
    "auto": "ansi_dark",
    "dark": "monokai",
    "light": "friendly",
    "dark-colorblind": "monokai",
    "light-colorblind": "friendly",
    "dark-ansi": "ansi_dark",
    "light-ansi": "ansi_light",
}

_PALETTES: dict[str, dict[str, str]] = {
    "auto": {
        "accent": "yellow", "ok": "green", "err": "red", "warn": "yellow", "dim": "dim",
        "add": "green", "del": "red", "gutter": "dim", "user": "bold", "title": "bold",
    },
    "dark": {
        "accent": "#d97757", "ok": "#4eba65", "err": "#ff6b80", "warn": "#ffc107", "dim": "#8a8a8a",
        "add": "#bcf0c4 on #1c4830", "del": "#ffc4c4 on #5c2430", "gutter": "#8c8494",
        "user": "bold #dcd6e0", "title": "bold #ffffff",
    },
    "light": {
        "accent": "#c15f3c", "ok": "#2c7a39", "err": "#ab2b3f", "warn": "#966c1e", "dim": "#6e6e78",
        "add": "#145a30 on #d6f4de", "del": "#8c2030 on #ffdce0", "gutter": "#8c8c96",
        "user": "bold #24292f", "title": "bold #000000",
    },
    "dark-colorblind": {
        "accent": "#d97757", "ok": "#5fa8ff", "err": "#ffa040", "warn": "#ffd166", "dim": "#8a8a8a",
        "add": "#ffd6a0 on #604018", "del": "#bed6ff on #1c3460", "gutter": "#8c8494",
        "user": "bold #dcd6e0", "title": "bold #ffffff",
    },
    "light-colorblind": {
        "accent": "#c15f3c", "ok": "#1450aa", "err": "#a05000", "warn": "#966c1e", "dim": "#6e6e78",
        "add": "#8c480a on #ffe6c4", "del": "#143c8c on #d6e4ff", "gutter": "#8c8c96",
        "user": "bold #24292f", "title": "bold #000000",
    },
    "dark-ansi": {
        "accent": "yellow", "ok": "green", "err": "red", "warn": "yellow", "dim": "dim",
        "add": "black on green", "del": "bright_white on red", "gutter": "dim",
        "user": "bold white", "title": "bold",
    },
    "light-ansi": {
        "accent": "red", "ok": "green", "err": "red", "warn": "yellow", "dim": "dim",
        "add": "black on green", "del": "bright_white on red", "gutter": "dim",
        "user": "bold black", "title": "bold",
    },
}


def theme_id_for(raw: Any) -> str:
    """A known welcome-screen theme id; anything else is ``dark``."""
    return raw if isinstance(raw, str) and raw in _PALETTES else "dark"


def rich_theme(theme_id: str) -> Theme:
    """Map the welcome-screen style (``.bot/settings.json`` ``theme``) to rich styles."""
    palette = _PALETTES[theme_id_for(theme_id)]
    return Theme({f"crit.{name}": style for name, style in palette.items()}, inherit=True)


# --------------------------------------------------------------------------- glyphs


@dataclass(frozen=True)
class Glyphs:
    bullet: str = "●"
    elbow: str = "⎿"
    star: str = "✻"
    check: str = "✓"
    cross: str = "✗"
    stop: str = "⏹"
    ellipsis: str = "…"
    todo: str = "☐"
    doing: str = "◐"
    done: str = "☒"
    pointer: str = "❯"
    frames: str = "·✢✳✶✻✽✻✶✳✢"


ASCII_GLYPHS = Glyphs(
    bullet="*", elbow="L", star="*", check="+", cross="x", stop="#", ellipsis="...",
    todo="[ ]", doing="[~]", done="[x]", pointer=">", frames="|/-\\",
)

_THINKING_VERBS = (
    "Thinking", "Pondering", "Working", "Mulling", "Reasoning", "Considering",
    "Brewing", "Crafting", "Noodling", "Cogitating",
)

# --------------------------------------------------------------------------- tool titles

_TOOL_TITLES = {
    "read_files": "Read",
    "write_files": "Write",
    "edit_file": "Update",
    "apply_patch": "Patch",
    "delete_file": "Delete",
    "move_file": "Move",
    "search_code": "Search",
    "find_files": "Glob",
    "list_files": "List",
    "web_fetch": "Fetch",
    "todo": "Update Todos",
    "git_status": "Git",
    "git_diff": "Git",
    "git_log": "Git",
    "git_show": "Git",
    "code_graph": "Graph",
    "skill": "Skill",
    "ask_user": "Ask",
    "delegate": "Delegate",
    "command_output": "Output",
    "kill_command": "Kill",
}
_SHELL_TITLES = {
    "powershell": "PowerShell",
    "pwsh": "PowerShell",
    "cmd": "Cmd",
    "bash": "Bash",
    "gitbash": "Bash",
    "sh": "Bash",
    "zsh": "Bash",
}

SLASH_COMMANDS: dict[str, str] = {
    "/help": "Show commands and shortcuts",
    "/new": "Start a fresh chat",
    "/clear": "Clear the screen",
    "/undo": "Restore the files the last task changed",
    "/mode": "Show or switch the mode: ask, edits, auto, plan (Shift+Tab cycles)",
    "/plan": "Plan mode: read and propose a plan; you approve before anything changes",
    "/auto": "Auto mode: edits and commands run without asking (risky ones still ask)",
    "/permissions": "Same as /mode",
    "/skills": "List skills; /skills <name> pins one for this session, /skills off <name> unpins",
    "/status": "Model, shell, folder, and background commands",
    "/shell": "Show or switch the default shell",
    "/tools": "List the tools the model can call",
    "/commands": "The shell commands run this session, with exit codes and times",
    "/theme": "Change the text style",
    "/exit": "Leave crit",
}

SHORTCUTS = (
    ("Enter", "send the message"),
    ("Alt+Enter, Ctrl+J, or \\ at line end", "new line"),
    ("Shift+Tab or Alt+M", "switch mode: ask, accept edits, auto, plan"),
    ("/", "commands"),
    ("@", "mention a file"),
    ("Up / Down", "history"),
    ("Ctrl+C", "clear the input; twice on an empty line to exit; interrupts a task"),
    ("Ctrl+D", "exit"),
)

_SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".bot", "node_modules", "build", "dist", "out", "target", "bin", "obj",
        "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".gradle", ".idea", ".next", ".nuxt", ".cache", ".codegraph", "graphify-out", "coverage",
    }
)
_MAX_INDEX_FILES = 20_000
_MENTION_RE = re.compile(r"(?<![\w.@])@([^\s@]+)")

# --------------------------------------------------------------------------- state


@dataclass
class UIState:
    workspace: Path = field(default_factory=Path.cwd)
    model: str = ""
    shell_label: str = ""
    shell_kind: str = ""
    approve_mode: str = "ask"
    theme_id: str = "dark"
    history_path: Path | None = None
    cache_dir: Path | None = None
    session_shell: Any = None
    #: A shell chosen with /shell that the model has not been told about yet.
    shell_changed: Any = None
    tools: tuple[str, ...] = ()
    #: Skills /skills pinned for this session; they go with every task.
    pinned_skills: list[str] = field(default_factory=list)
    last_error: str = ""
    # live region
    waiting_explicit: bool = False
    waiting_depth: int = 0
    waiting_label: str = "Thinking"
    waiting_since: float = 0.0
    tool_title: Text | None = None
    tool_tail: list[str] = field(default_factory=list)
    tool_partial: str = ""
    #: Tools running now, by name, and when the first of them started (for "Running 1 shell command… 12s").
    active: dict[str, int] = field(default_factory=dict)
    active_since: float = 0.0


_lock = threading.RLock()
_state = UIState()
_console: Console | None = None
_live: Live | None = None
_verbs = itertools.cycle(_THINKING_VERBS)
_prompt_session: Any = None
_file_index: _FileIndex | None = None
_bridge: _LogBridge | None = None


def state() -> UIState:
    return _state


def take_shell_change() -> Any:
    """The shell /shell switched to since the last call, or None. The agent loop tells the model."""
    with _lock:
        chosen, _state.shell_changed = _state.shell_changed, None
    return chosen


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
) -> UIState:
    """Set what the header, toolbar, and slash commands show. ``None`` keeps a value."""
    global _console, _prompt_session, _file_index
    with _lock:
        if workspace is not None:
            _state.workspace = Path(workspace)
            _file_index = None
        if model is not None:
            _state.model = model
        if shell is not None:
            _state.shell_label = str(getattr(shell, "label", "") or shell)
            _state.shell_kind = str(getattr(shell, "kind", "") or "")
        if approve_mode is not None:
            _state.approve_mode = normalize_mode(approve_mode)
        if theme is not None:
            _state.theme_id = theme_id_for(theme)
            if _console is not None:
                _console.push_theme(rich_theme(_state.theme_id))
        if history_path is not None:
            _state.history_path = Path(history_path)
            _prompt_session = None
        if cache_dir is not None:
            _state.cache_dir = Path(cache_dir)
        if session_shell is not None:
            _state.session_shell = session_shell
        if tools is not None:
            _state.tools = tuple(tools)
    return _state


def approve_mode() -> str:
    """``ask``, ``edits``, ``auto``, or ``plan``. Shift+Tab and /mode change it during a session."""
    return _state.approve_mode


def set_mode(mode: str, *, announce: bool = True) -> str:
    chosen = normalize_mode(mode, _state.approve_mode)
    with _lock:
        _state.approve_mode = chosen
    if announce:
        note("note", f"Mode: {MODE_LABELS[chosen]}.")
    return chosen


def cycle_mode() -> str:
    """The next mode in :data:`MODES`."""
    with _lock:
        index = MODES.index(_state.approve_mode) if _state.approve_mode in MODES else 0
        _state.approve_mode = MODES[(index + 1) % len(MODES)]
        return _state.approve_mode


def pinned_skills() -> list[str]:
    return list(_state.pinned_skills)


def set_console(console: Console | None) -> None:
    """Use ``console`` for all output (tests pass a recording console)."""
    global _console
    with _lock:
        _console = console


def console() -> Console:
    global _console
    current = _console
    if current is not None:
        return current
    with _lock:
        if _console is None:
            _console = Console(
                theme=rich_theme(_state.theme_id),
                highlight=False,
                emoji=False,
                markup=False,
            )
        return _console


def reset() -> None:
    """Forget all session state (tests)."""
    global _state, _console, _live, _prompt_session, _file_index
    shutdown()
    with _lock:
        _state = UIState()
        _console = None
        _live = None
        _prompt_session = None
        _file_index = None


def is_tty() -> bool:
    return console().is_terminal


def glyphs() -> Glyphs:
    con = console()
    encoding = (getattr(con, "encoding", "") or "").lower().replace("-", "")
    if con.legacy_windows or not encoding.startswith("utf"):
        return ASCII_GLYPHS
    return Glyphs()


def _print(*renderables: RenderableType, **kwargs: Any) -> None:
    con = console()
    if not con.is_terminal:
        kwargs.setdefault("soft_wrap", True)
    with _lock:
        try:
            con.print(*renderables, **kwargs)
        except UnicodeEncodeError:
            log.print_safe(*(str(item) for item in renderables), flush=True)


def _style(name: str) -> str:
    return f"crit.{name}"


# --------------------------------------------------------------------------- header and notes


def welcome_header(*, version: str | None = None) -> None:
    """The boxed banner at the top of a session."""
    if version is None:
        from critique_bot import __version__ as version
    g = glyphs()
    st = _state
    mode = MODE_LABELS.get(st.approve_mode, st.approve_mode)
    if not is_tty():
        parts = [f"crit v{version}", str(st.workspace)]
        if st.model:
            parts.append(f"model: {st.model}")
        if st.shell_label:
            parts.append(f"shell: {st.shell_label}")
        parts.append(f"mode: {st.approve_mode}")
        _print(Text(" · ".join(parts)))
        return
    body = Text()
    body.append(f"{g.star} ", style=_style("accent"))
    body.append("Welcome to crit", style="bold")
    body.append(f" v{version}", style=_style("dim"))
    body.append("\n\n")
    body.append("  /help for commands · @ to mention a file · ? for shortcuts\n\n", style=_style("dim"))
    rows = [("cwd", str(st.workspace))]
    if st.model:
        rows.append(("model", st.model))
    if st.shell_label:
        rows.append(("shell", st.shell_label))
    rows.append(("mode", mode + " · shift+tab to switch"))
    for index, (label, value) in enumerate(rows):
        body.append(f"  {label}: ", style=_style("dim"))
        body.append(value)
        if index < len(rows) - 1:
            body.append("\n")
    _print(Panel(body, border_style=_style("accent"), expand=False, padding=(0, 1)))
    _print(Text(" Tips: describe a task, ask a question, or type / for commands.", style=_style("dim")))
    _print()


def note(kind: str, message: str) -> None:
    """Status lines from the agent loop (kinds: task, note, work, good, bad)."""
    if not message:
        return
    g = glyphs()
    if kind == "bad":
        with _lock:
            _state.last_error = message
    line = Text()
    if kind == "task":
        line.append("> ", style=_style("accent"))
        line.append(message, style="bold")
    elif kind == "work":
        line.append(f"{g.bullet} ", style=_style("dim"))
        line.append(message, style=_style("dim"))
    elif kind == "good":
        line.append("  ")
        line.append(message, style=_style("ok"))
    elif kind == "bad":
        line.append("  ")
        line.append(message, style=_style("err"))
    else:
        line.append("  ")
        line.append(message, style=_style("dim"))
    _print(line)


def assistant(text: str) -> None:
    """Assistant text as markdown, with a white bullet like Claude Code."""
    if not text or not text.strip():
        return
    if not is_tty():
        _print(Text(text.rstrip()))
        _print()
        return
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(ratio=1)
    code_theme = _CODE_THEMES.get(_state.theme_id, "monokai")
    grid.add_row(Text(glyphs().bullet), Markdown(text.strip(), code_theme=code_theme))
    _print(grid)
    _print()


def final_status(code: str, *, ok: bool, reason: str = "") -> None:
    """``✓ Done`` / ``✗ Failed: reason`` / ``⏹ Interrupted``."""
    g = glyphs()
    code = (code or "").upper()
    _print()
    if ok:
        _print(Text(f"{g.check} Done", style=f"bold {_style('ok')}"))
    elif code in {"INTERRUPTED", "STOPPED"}:
        label = "Interrupted" if code == "INTERRUPTED" else "Stopped"
        text = f"{g.stop} {label}"
        if reason and code == "STOPPED":
            text += f": {reason}"
        _print(Text(text, style=f"bold {_style('warn')}"))
    else:
        label = "Blocked" if code == "BLOCKED" else "Failed"
        reason = reason or _state.last_error
        text = f"{g.cross} {label}" + (f": {reason}" if reason else "")
        _print(Text(text, style=f"bold {_style('err')}"))
    with _lock:
        _state.last_error = ""


def clear_screen() -> None:
    if is_tty():
        console().clear()


# --------------------------------------------------------------------------- tool lines


def tool_title(name: str, args: dict[str, Any] | None = None) -> str:
    args = args or {}
    if name == "run_command":
        kind = str(args.get("shell") or _state.shell_kind or "").lower()
        return _SHELL_TITLES.get(kind, "Bash")
    return _TOOL_TITLES.get(name, name.replace("_", " ").title().replace(" ", ""))


def _short(text: Any, limit: int = 80) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)] + "…"


def arg_summary(name: str, args: dict[str, Any] | None) -> str:
    args = args or {}
    path = args.get("path")
    paths = args.get("paths")
    if not path and isinstance(paths, list) and paths:
        shown = ", ".join(str(item) for item in paths[:3])
        path = shown + (f", +{len(paths) - 3}" if len(paths) > 3 else "")
    files = args.get("files")
    if not path and isinstance(files, list) and files:
        first = files[0].get("path", "") if isinstance(files[0], dict) else str(files[0])
        path = str(first) + (f", +{len(files) - 1}" if len(files) > 1 else "")
    if name == "delegate":
        tasks = args.get("tasks")
        count = len(tasks) if isinstance(tasks, list) else 1
        return f"{count} brief{'s' if count != 1 else ''} in parallel"
    if name == "run_command":
        return _short(args.get("command") or "", 100)
    if name == "search_code":
        text = f'pattern: "{_short(args.get("pattern") or args.get("query") or "", 50)}"'
        if path and str(path) not in {"", "."}:
            text += f', path: "{_short(path, 40)}"'
        return text
    if name == "find_files":
        return _short(args.get("glob") or args.get("pattern") or "*", 60)
    if name == "list_files":
        return _short(path or ".", 60)
    if name == "web_fetch":
        return _short(args.get("url") or "", 80)
    if name == "todo":
        return ""
    if name.startswith("git_"):
        extra = args.get("rev") or args.get("commit") or args.get("path") or ""
        return name[4:] + (f" {_short(extra, 40)}" if extra else "")
    if name == "move_file":
        src = args.get("source") or args.get("src") or args.get("from") or path or ""
        dst = args.get("destination") or args.get("dst") or args.get("to") or ""
        return f"{src} → {dst}" if dst else str(src)
    if name in {"command_output", "kill_command"}:
        return str(args.get("id") or args.get("job_id") or "")
    if name == "code_graph":
        return _short(args.get("query") or args.get("symbol") or "", 60)
    if name == "skill":
        return _short(args.get("name") or args.get("skill") or "", 40)
    if name == "ask_user":
        return _short(args.get("question") or "", 80)
    if path:
        return _short(path, 80)
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return _short(value, 60)
    return ""


def tool_header(name: str, args: dict[str, Any] | None, *, status: str) -> Text:
    """``● Title(args)``; ``status`` is ``ok``, ``err``, or ``pending``."""
    g = glyphs()
    bullet_style = {"ok": _style("ok"), "err": _style("err")}.get(status, _style("dim"))
    line = Text()
    line.append(f"{g.bullet} ", style=bullet_style)
    line.append(tool_title(name, args), style="bold")
    summary = arg_summary(name, args)
    if summary:
        line.append(f"({summary})")
    return line


def _elbow(text: Text | str, *, style: str = "", first: bool = True) -> Text:
    g = glyphs()
    line = Text()
    line.append(f"  {g.elbow}  " if first else "     ", style=_style("dim"))
    if isinstance(text, Text):
        line.append_text(text)
    else:
        line.append(text, style=style)
    return line


def render_diff(diff: str, *, max_lines: int = 30) -> list[Text]:
    """A compact numbered diff: removed lines red, added green, context dim."""
    rows: list[Text] = []
    old_no = new_no = 0
    first_hunk = True
    hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    body: list[tuple[str, int, str]] = []
    for raw in diff.splitlines():
        if raw.startswith(("--- ", "+++ ", "diff ", "index ", "new file", "deleted file")):
            continue
        match = hunk_re.match(raw)
        if match:
            old_no, new_no = int(match.group(1)), int(match.group(2))
            if not first_hunk:
                body.append(("sep", 0, ""))
            first_hunk = False
            continue
        if raw.startswith("\\"):
            continue
        mark, text = (raw[:1], raw[1:]) if raw else (" ", "")
        if mark == "-":
            body.append(("del", old_no, text))
            old_no += 1
        elif mark == "+":
            body.append(("add", new_no, text))
            new_no += 1
        else:
            body.append(("ctx", new_no, text))
            old_no += 1
            new_no += 1
    width = max([len(str(number)) for _kind, number, _text in body] + [3])
    for kind, number, text in body[:max_lines]:
        line = Text("     ")
        text = text.expandtabs(4)
        if kind == "sep":
            dots = "..." if glyphs() is ASCII_GLYPHS else "⋮"
            line.append(" " * width + "  " + dots, style=_style("dim"))
        elif kind == "del":
            line.append(f"{number:>{width}} ", style=_style("gutter"))
            line.append(f"- {text}", style=_style("del"))
        elif kind == "add":
            line.append(f"{number:>{width}} ", style=_style("gutter"))
            line.append(f"+ {text}", style=_style("add"))
        else:
            line.append(f"{number:>{width}} ", style=_style("gutter"))
            line.append(f"  {text}", style=_style("dim"))
        rows.append(line)
    if len(body) > max_lines:
        rows.append(Text(f"     {glyphs().ellipsis} +{len(body) - max_lines} lines", style=_style("dim")))
    return rows


def render_todos(items: Any) -> list[Text]:
    g = glyphs()
    rows: list[Text] = []
    if not isinstance(items, list):
        return rows
    for index, item in enumerate(items):
        if isinstance(item, dict):
            content = str(item.get("content") or item.get("text") or item.get("title") or "")
            status = str(item.get("status") or "pending").lower()
        else:
            content, status = str(item), "pending"
        if status in {"completed", "done"}:
            mark, style = g.done, f"strike {_style('dim')}"
        elif status == "cancelled":
            mark, style = g.done, f"strike {_style('dim')}"
        elif status == "in_progress":
            mark, style = g.doing, "bold"
        else:
            mark, style = g.todo, ""
        rows.append(_elbow(Text(f"{mark} {content}", style=style), first=index == 0))
    return rows


def tool_result_lines(
    name: str,
    args: dict[str, Any] | None,
    result: dict[str, Any],
    *,
    friendly: str = "",
) -> list[Text]:
    """The ``⎿`` lines under a finished tool."""
    args = args or {}
    ui = result.get("ui") if isinstance(result.get("ui"), dict) else {}
    rows: list[Text] = []
    if not result.get("ok"):
        raw = str(result.get("error") or "failed")
        first = raw.strip().splitlines()[0] if raw.strip() else "failed"
        message = friendly or first
        rows.append(_elbow(f"Error: {_short(message, 200)}", style=_style("err")))
        if friendly and _short(first, 200) != _short(friendly, 200):
            rows.append(_elbow(_short(first, 200), style=_style("dim"), first=False))
        return rows
    output = str(result.get("output") or "")
    summary = str(ui.get("summary") or "").strip()
    if name == "todo":
        todos = render_todos(args.get("todos"))
        if todos:
            return todos
    if not summary:
        summary = next((line.strip() for line in output.splitlines() if line.strip()), "Done")
    rows.append(_elbow(_short(summary, 200)))
    diff = ui.get("diff")
    if isinstance(diff, str) and diff.strip():
        rows.extend(render_diff(diff))
        return rows
    if name == "run_command":
        lines = ui.get("lines")
        if not isinstance(lines, list):
            lines = [line for line in output.splitlines() if line.strip()]
            if lines and lines[0].strip() == summary:
                lines = lines[1:]
        shown = [str(line) for line in lines[:5]]
        for line in shown:
            rows.append(_elbow(_short(line, 200), style=_style("dim"), first=False))
        extra = len(lines) - len(shown)
        if extra > 0:
            rows.append(_elbow(f"{glyphs().ellipsis} +{extra} lines", style=_style("dim"), first=False))
    elif isinstance(ui.get("lines"), list):
        for line in ui["lines"][:5]:
            rows.append(_elbow(_short(line, 200), style=_style("dim"), first=False))
    return rows


# (verb, singular, plural) for the "Running 1 shell command…" line.
_ACTIVITY = {
    "run_command": ("Running", "shell command", "shell commands"),
    "command_output": ("Waiting for", "background command", "background commands"),
    "kill_command": ("Stopping", "background command", "background commands"),
    "read_files": ("Reading", "file", "files"),
    "list_files": ("Listing", "folder", "folders"),
    "find_files": ("Finding", "file pattern", "file patterns"),
    "search_code": ("Searching", "pattern", "patterns"),
    "edit_file": ("Editing", "file", "files"),
    "write_files": ("Writing", "file", "files"),
    "apply_patch": ("Patching", "file", "files"),
    "delete_file": ("Deleting", "file", "files"),
    "move_file": ("Moving", "file", "files"),
    "web_fetch": ("Fetching", "page", "pages"),
    "code_graph": ("Querying", "code graph", "code graph"),
    "git_status": ("Running", "git command", "git commands"),
    "git_diff": ("Running", "git command", "git commands"),
    "git_log": ("Running", "git command", "git commands"),
    "git_show": ("Running", "git command", "git commands"),
    "delegate": ("Running", "helper tab", "helper tabs"),
}


def activity_text(active: dict[str, int]) -> str:
    """``Running 1 shell command`` / ``Reading 3 files, searching 1 pattern`` for the tools running now."""
    groups: dict[tuple[str, str, str], int] = {}
    for name, count in active.items():
        if count <= 0:
            continue
        key = _ACTIVITY.get(name, ("Running", name.replace("_", " "), name.replace("_", " ")))
        groups[key] = groups.get(key, 0) + count
    phrases = [f"{verb} {count} {one if count == 1 else many}" for (verb, one, many), count in groups.items()]
    if not phrases:
        return ""
    text = ", ".join([phrases[0]] + [phrase[:1].lower() + phrase[1:] for phrase in phrases[1:]])
    return text


def set_active(name: str, count: int) -> None:
    """Show ``count`` of ``name`` as running (a delegate call runs one helper tab per brief)."""
    with _lock:
        if name in _state.active:
            _state.active[name] = max(0, int(count))
        _refresh_live()


def tool_start(name: str, args: dict[str, Any] | None) -> None:
    """Show the running tool in the live region (TTY) — nothing is printed for pipes."""
    with _lock:
        _state.tool_title = tool_header(name, args, status="pending")
        _state.tool_tail = []
        _state.tool_partial = ""
        if not any(count > 0 for count in _state.active.values()):
            _state.active_since = time.monotonic()
        _state.active[name] = _state.active.get(name, 0) + 1
        _refresh_live()


def tool_output(chunk: str) -> None:
    """A chunk of live command output; the last five lines show dimmed under the tool."""
    if not chunk:
        return
    with _lock:
        if _state.tool_title is None:
            return
        text = _state.tool_partial + chunk.replace("\r\n", "\n").replace("\r", "\n")
        parts = text.split("\n")
        _state.tool_partial = parts.pop()
        tail = _state.tool_tail + [part for part in parts if part.strip()]
        _state.tool_tail = tail[-5:]
        _refresh_live()


def tool_done(name: str, args: dict[str, Any] | None, result: dict[str, Any], *, friendly: str = "") -> None:
    with _lock:
        if _state.active.get(name, 0) > 0:
            _state.active[name] -= 1
        if not any(count > 0 for count in _state.active.values()):
            _state.active.clear()
            _state.tool_title = None
            _state.tool_tail = []
            _state.tool_partial = ""
        _refresh_live()
        status = "ok" if result.get("ok") else "err"
        _print(tool_header(name, args, status=status))
        for row in tool_result_lines(name, args, result, friendly=friendly):
            _print(row)


# --------------------------------------------------------------------------- live region


class _LiveView:
    def __rich__(self) -> RenderableType:
        return live_renderable()


def _waiting_active() -> bool:
    return _state.waiting_explicit or _state.waiting_depth > 0


def live_renderable(now: float | None = None) -> RenderableType:
    """What the live region shows right now: the running tool and/or the spinner.

    Called from rich's refresh thread while it holds the Live lock, so this
    must not take ``_lock`` (the main thread takes ``_lock`` then the Live
    lock). It reads a snapshot of plain attributes instead.
    """
    g = glyphs()
    now = time.monotonic() if now is None else now
    st = _state
    parts: list[RenderableType] = []
    title = st.tool_title
    if title is not None:
        parts.append(title)
        tail = list(st.tool_tail)
        partial = st.tool_partial
        if partial.strip():
            tail = (tail + [partial])[-5:]
        for index, line in enumerate(tail):
            parts.append(_elbow(_short(line, 160), style=_style("dim"), first=index == 0))
    doing = activity_text(dict(st.active))
    if doing and not (st.waiting_explicit or st.waiting_depth > 0):
        elapsed = max(0, int(now - st.active_since))
        frame = g.frames[int(now * 8) % len(g.frames)]
        line = Text()
        line.append(f"{frame} ", style=_style("accent"))
        line.append(f"{doing}{g.ellipsis} ", style=_style("accent"))
        line.append(f"({_elapsed(elapsed)} · ctrl+c to interrupt)", style=_style("dim"))
        parts.append(line)
    if st.waiting_explicit or st.waiting_depth > 0:
        elapsed = max(0, int(now - st.waiting_since))
        frame = g.frames[int(now * 8) % len(g.frames)]
        line = Text()
        line.append(f"{frame} ", style=_style("accent"))
        line.append(f"{st.waiting_label}{g.ellipsis} ", style=_style("accent"))
        line.append(f"({elapsed}s · ctrl+c to interrupt)", style=_style("dim"))
        if parts:
            parts.append(Text())
        parts.append(line)
    return Group(*parts)


def _elapsed(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


def _refresh_live() -> None:
    """Start, update, or stop the one live region to match the state."""
    global _live
    with _lock:
        wanted = _state.tool_title is not None or _waiting_active() or any(c > 0 for c in _state.active.values())
        if not wanted or not is_tty():
            if _live is not None:
                live, _live = _live, None
                try:
                    live.stop()
                except Exception:
                    pass
            return
        if _live is None:
            _live = Live(
                get_renderable=lambda: _LiveView(),
                console=console(),
                transient=True,
                refresh_per_second=8,
                redirect_stdout=True,
                redirect_stderr=True,
            )
            try:
                _live.start()
            except Exception:
                _live = None
            return
        try:
            _live.refresh()
        except Exception:
            pass


def _pause_live() -> None:
    global _live
    with _lock:
        if _live is not None:
            live, _live = _live, None
            try:
                live.stop()
            except Exception:
                pass


def waiting(active: bool, label: str = "Thinking") -> None:
    """Spinner while the web model replies: ``✻ Thinking… (23s · ctrl+c to interrupt)``."""
    with _lock:
        if active:
            if not _waiting_active():
                _state.waiting_since = time.monotonic()
                _state.waiting_label = _verb(label)
            elif label and _verb_base(label) != "Thinking":
                _state.waiting_label = _verb(label)
            _state.waiting_explicit = True
        else:
            _state.waiting_explicit = False
            _state.waiting_depth = 0
        _refresh_live()


def _verb_base(label: str) -> str:
    return (label or "Thinking").strip().rstrip(".…").strip() or "Thinking"


def _verb(label: str) -> str:
    base = _verb_base(label)
    if base == "Thinking":
        return next(_verbs)
    return base


class _LogBridge:
    """Stands in for ``log``'s spinner so ``log.loading`` drives our live region.

    ``chat_client.send_turn`` and the agent loop wrap the slow web reply in
    ``log.loading("Thinking...")``. With this bridge installed, those calls
    become :func:`waiting` instead of a second spinner on stderr.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self.message = "Thinking"

    def push(self, message: str) -> None:
        with _lock:
            if not _waiting_active():
                _state.waiting_since = time.monotonic()
                _state.waiting_label = _verb(message)
            elif _verb_base(message) != "Thinking":
                _state.waiting_label = _verb(message)
            _state.waiting_depth += 1
            _refresh_live()

    def pop(self) -> None:
        with _lock:
            _state.waiting_depth = max(0, _state.waiting_depth - 1)
            _refresh_live()

    def clear(self) -> None:
        return None

    def render(self) -> None:
        return None


def install_log_bridge() -> None:
    """Route ``log.loading`` through the live region while a TTY session runs."""
    global _bridge
    if not is_tty() or log.enabled():
        return
    with log._spinner_lock:
        if _bridge is None:
            _bridge = _LogBridge()
        log._active_spinner = _bridge  # type: ignore[assignment]


def remove_log_bridge() -> None:
    global _bridge
    with log._spinner_lock:
        if _bridge is not None and log._active_spinner is _bridge:
            log._active_spinner = None
        _bridge = None


def shutdown() -> None:
    """Stop the spinner/live region and give ``log`` its spinner back."""
    with _lock:
        _state.waiting_explicit = False
        _state.waiting_depth = 0
        _state.tool_title = None
        _state.tool_tail = []
    _pause_live()
    remove_log_bridge()


# --------------------------------------------------------------------------- approvals

_APPROVAL_TITLES = {
    "command": "Bash command",
    "edit": "Edit file",
    "network": "Fetch",
    "outside": "Outside workspace",
    "read": "Read file",
}


def _approval_title(permission: Any) -> str:
    kind = str(getattr(permission, "kind", "") or "")
    if kind == "command":
        shell = tool_title("run_command", {})
        return f"{shell} command"
    return _APPROVAL_TITLES.get(kind, "Permission")


def _always_label(permission: Any) -> str:
    key = str(getattr(permission, "key", "") or "")
    if key == "edit":
        return "file edits"
    if key.startswith("command:"):
        return f"{key.split(':', 1)[1]} commands"
    if key.startswith("network:"):
        return key.split(":", 1)[1]
    if key == "command":
        return "commands"
    if key == "network":
        return "web requests"
    return key


def approval_options(permission: Any, *, risky: str = "") -> list[tuple[str, str]]:
    """``[(answer, label)]`` in display order. A risky command gets only yes and no.

    Yes, Yes-and-don't-ask-again (when the call has a key), No, then Yes-and-switch-to-auto.
    """
    options = [("yes", "Yes")]
    key = str(getattr(permission, "key", "") or "")
    if not risky and key and getattr(permission, "kind", "") != "outside":
        options.append(("always", f"Yes, and don't ask again for {_always_label(permission)} this session"))
    options.append(("no", "No, and tell crit what to do differently (esc)"))
    if not risky:
        # Last, so the numbers of yes / always / no stay what people are used to.
        options.append(("auto", "Yes, and switch to auto mode (stop asking this session)"))
    return options


def approval_panel(permission: Any, *, risky: str = "") -> Panel:
    summary = str(getattr(permission, "summary", "") or permission)
    detail = str(getattr(permission, "detail", "") or "")
    kind = str(getattr(permission, "kind", "") or "")
    parts: list[RenderableType] = [Text(_approval_title(permission), style=f"bold {_style('accent')}"), Text()]
    if kind == "edit" and detail and ("@@" in detail or detail.startswith(("---", "+++"))):
        parts.append(Text(summary, style="bold"))
        parts.extend(render_diff(detail, max_lines=20))
    else:
        parts.append(Padding(Text(summary, style="bold"), (0, 0, 0, 2)))
        if detail and detail.strip() and detail.strip() != summary.strip():
            lines = detail.strip().splitlines()
            shown = "\n".join(lines[:12]) + (f"\n{glyphs().ellipsis} +{len(lines) - 12} lines" if len(lines) > 12 else "")
            parts.append(Padding(Text(shown, style=_style("dim")), (0, 0, 0, 2)))
    if risky:
        parts.extend([Text(), Text(f"! This command {risky}.", style=f"bold {_style('warn')}")])
        if _state.approve_mode == "auto":
            parts.append(Text("Auto mode still asks before risky commands.", style=_style("dim")))
    parts.extend([Text(), Text("Do you want to proceed?")])
    return Panel(Group(*parts), border_style=_style("accent"), expand=True, padding=(0, 1))


# Words a person might type for each approval answer. Longest phrase wins, so
# "yes, always" is "always" and "don't ask again" is not "don't".
_ANSWER_WORDS: dict[str, tuple[str, ...]] = {
    "yes": (
        "y", "yes", "yeah", "yep", "yup", "ya", "ok", "okay", "k", "sure", "proceed", "go", "go ahead",
        "approve", "approved", "allow", "continue", "do it", "run it", "accept", "confirm", "true",
    ),
    "always": (
        "a", "always", "always allow", "allow always", "yes always", "yes and always", "yes, always",
        "yes, and don't ask again", "yes and don't ask again", "don't ask again", "dont ask again",
        "yes to all", "all",
    ),
    "auto": (
        "auto", "auto mode", "switch to auto", "switch to auto mode", "yes auto", "yes, auto", "yes and auto",
        "yes, auto mode", "start in auto mode", "yes, start in auto mode",
    ),
    "edits": ("edits", "accept edits", "auto-accept edits", "yes, accept edits", "accept"),
    "ask": ("ask", "ask me", "ask first", "yes, ask", "yes but ask", "ask before"),
    "no": (
        "n", "no", "nope", "nah", "deny", "denied", "decline", "reject", "cancel", "stop", "abort",
        "don't", "dont", "do not", "false", "skip",
    ),
}
_CHOICE_NUMBER = re.compile(r"(?:option|choice|#|\()?\s*(\d{1,2})(?!\d)\s*[.):\]]*\s*[,;:\-]?\s*", re.I)


def parse_choice(text: str, options: list[tuple[str, str]]) -> tuple[int, str]:
    """Read a typed answer: ``1``, ``1.``, ``(2)``, ``yes``, ``Yes``, ``always``, ``no, use X``, or a label.

    Returns ``(index, rest)`` where ``rest`` is whatever followed the answer
    (``"use X"`` above, the reason for a no). ``(-1, "")`` when nothing matches.
    """
    raw = " ".join(str(text or "").split())
    if not raw:
        return (-1, "")
    number = _CHOICE_NUMBER.match(raw)
    if number:
        index = int(number.group(1)) - 1
        if 0 <= index < len(options):
            return (index, raw[number.end():].strip())
        return (-1, "")
    low = raw.lower().replace("\u2019", "'")
    bare = low.rstrip(".!?")
    for index, (_answer, label) in enumerate(options):
        if bare == " ".join(label.lower().split()).rstrip(".!?"):
            return (index, "")
    phrases = sorted(
        ((phrase, answer) for answer, words in _ANSWER_WORDS.items() for phrase in words),
        key=lambda item: -len(item[0]),
    )
    answers = [answer for answer, _label in options]
    for phrase, answer in phrases:
        if low == phrase or (low.startswith(phrase) and not low[len(phrase)].isalnum()):
            if answer not in answers and answer == "yes" and answers and answers[0] != "no":
                answer = answers[0]  # "yes" to a list without a plain yes picks the first choice
            if answer not in answers:
                return (-1, "")
            rest = raw[len(phrase):].strip().lstrip(",;:.!-").strip()
            return (answers.index(answer), rest)
    return (-1, "")


def _choice_hint(options: list[tuple[str, str]]) -> str:
    answers = {answer for answer, _label in options}
    words = [word for word in ("yes", "always", "auto", "no") if word in answers]
    numbers = f"1-{len(options)}" if len(options) > 1 else "1"
    return f"type {numbers}" + (f" or {', '.join(words)}" if words else "")


def _choose(
    options: list[tuple[str, str]],
    *,
    pt_input: Any = None,
    pt_output: Any = None,
    free_text: int | None = None,
) -> tuple[int, str]:
    """Pick an option with the arrows, or type ``1``, ``2.``, ``yes``, ``No``, ``always`` and press Enter.

    Returns ``(index, rest)``; ``rest`` is text typed after the answer. Esc or
    Ctrl+C gives ``(-1, "")``. With ``free_text`` set, typing that matches no
    option picks that row and returns the typed text as ``rest``.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import Style

    pointer = glyphs().pointer
    cursor = [0]
    typed = [""]
    note = [""]
    bindings = KeyBindings()

    def _sync() -> None:
        note[0] = ""
        index, _rest = parse_choice(typed[0], options)
        if index >= 0:
            cursor[0] = index

    @bindings.add("up")
    @bindings.add("s-tab")
    def _up(event: Any) -> None:
        typed[0] = ""
        note[0] = ""
        cursor[0] = (cursor[0] - 1) % len(options)

    @bindings.add("down")
    @bindings.add("tab")
    def _down(event: Any) -> None:
        typed[0] = ""
        note[0] = ""
        cursor[0] = (cursor[0] + 1) % len(options)

    @bindings.add("backspace")
    def _back(event: Any) -> None:
        typed[0] = typed[0][:-1]
        _sync()

    @bindings.add("c-u")
    def _clear(event: Any) -> None:
        typed[0] = ""
        note[0] = ""

    @bindings.add("enter")
    @bindings.add("c-j")
    def _enter(event: Any) -> None:
        if not typed[0].strip():
            event.app.exit(result=(cursor[0], ""))
            return
        index, rest = parse_choice(typed[0], options)
        if index >= 0:
            event.app.exit(result=(index, rest))
        elif free_text is not None:
            event.app.exit(result=(free_text, typed[0].strip()))
        else:
            note[0] = f"Didn't understand {typed[0].strip()!r}: {_choice_hint(options)}"
            typed[0] = ""

    @bindings.add("escape", eager=True)
    @bindings.add("c-c")
    def _cancel(event: Any) -> None:
        event.app.exit(result=(-1, ""))

    @bindings.add("<any>")
    def _type(event: Any) -> None:
        data = str(getattr(event, "data", "") or "")
        if data.isprintable():
            typed[0] += data
            _sync()

    def text() -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for index, (_answer, label) in enumerate(options):
            if index == cursor[0]:
                rows.append(("class:selected", f" {pointer} {index + 1}. {label}\n"))
            else:
                rows.append(("", f"   {index + 1}. {label}\n"))
        if note[0]:
            rows.append(("class:error", f"   {note[0]}\n"))
        elif typed[0]:
            rows.append(("", f"   > {typed[0]}\n"))
        else:
            rows.append(("class:hint", f"   Enter to confirm \u00b7 {_choice_hint(options)} \u00b7 Esc to cancel\n"))
        return rows

    accent = _pt_color(_PALETTES[_state.theme_id]["accent"])
    app: Application[tuple[int, str]] = Application(
        layout=Layout(Window(FormattedTextControl(text, show_cursor=False), dont_extend_height=True)),
        key_bindings=bindings,
        full_screen=False,
        erase_when_done=True,
        style=Style.from_dict({"selected": f"bold {accent}".strip(), "hint": "#888888", "error": "ansired"}),
        input=pt_input,
        output=pt_output,
    )
    result = app.run()
    if not result:
        return (-1, "")
    return (int(result[0]), str(result[1] or ""))


def _pt_color(rich_style: str) -> str:
    first = rich_style.split(" on ")[0].replace("bold", "").strip()
    if first.startswith("#"):
        return first
    names = {"yellow": "ansiyellow", "red": "ansired", "green": "ansigreen", "dim": ""}
    return names.get(first, "")


def approve(
    permission: Any, *, risky: str = "", pt_input: Any = None, pt_output: Any = None
) -> tuple[str, str]:
    """Ask before an edit, command, fetch, or outside-workspace step.

    Returns ``("yes" | "always" | "no", reason)``. Auto mode answers yes,
    except for a ``risky`` command. "Yes, and switch to auto mode" switches
    the mode and answers yes. No terminal answers no.
    """
    if _state.approve_mode == "auto" and not risky:
        return ("yes", "")
    interactive = pt_input is not None or (_stdin_tty() and is_tty())
    if not interactive:
        return ("no", "no terminal to approve" + (f" a command that {risky}" if risky else ""))
    _pause_live()
    options = approval_options(permission, risky=risky)
    _print(approval_panel(permission, risky=risky))
    try:
        index, rest = _choose(options, pt_input=pt_input, pt_output=pt_output)
    except (EOFError, KeyboardInterrupt):
        index, rest = -1, ""
    except Exception as exc:  # no usable console (e.g. mintty without winpty)
        log.warn(f"approval prompt failed: {exc}")
        index, rest = _choose_plain(options)
    g = glyphs()
    if index < 0:
        _print(_elbow("Declined", style=_style("err")))
        _refresh_live()
        return ("no", "")
    answer, _label = options[index]
    if answer == "no":
        reason = rest or _ask_reason(pt_input=pt_input, pt_output=pt_output)
        _print(_elbow("Declined" + (f": {reason}" if reason else ""), style=_style("err")))
        _refresh_live()
        return ("no", reason)
    if answer == "auto":
        set_mode("auto", announce=False)
        _print(_elbow(f"{g.check} Approved. Auto mode on: edits and commands run without asking.", style=_style("ok")))
        _refresh_live()
        return ("yes", "")
    _print(_elbow(f"{g.check} " + ("Approved for this session" if answer == "always" else "Approved"), style=_style("ok")))
    _refresh_live()
    return (answer, "")


PLAN_OPTIONS = [
    ("auto", "Yes, start now in auto mode (no more prompts, risky commands still ask)"),
    ("edits", "Yes, start and auto-accept edits (commands ask)"),
    ("ask", "Yes, start and ask before each edit and command"),
    ("no", "No, keep planning (tell crit what to change)"),
]


def review_plan(*, pt_input: Any = None, pt_output: Any = None) -> tuple[str, str]:
    """After a plan in plan mode: ``("auto" | "edits" | "ask", "")`` to start, ``("no", feedback)``
    to plan again, ``("stop", "")`` on Esc, ``("none", "")`` with no terminal."""
    interactive = pt_input is not None or (_stdin_tty() and is_tty())
    if not interactive:
        return ("none", "")
    _pause_live()
    _print(
        Panel(
            Text("Ready to code? crit will carry out the plan above.", style="bold"),
            title=Text("Plan ready", style=_style("accent")),
            title_align="left",
            border_style=_style("accent"),
            expand=True,
            padding=(0, 1),
        )
    )
    try:
        index, rest = _choose(PLAN_OPTIONS, pt_input=pt_input, pt_output=pt_output)
    except (EOFError, KeyboardInterrupt):
        index, rest = -1, ""
    except Exception as exc:
        log.warn(f"plan prompt failed: {exc}")
        index, rest = _choose_plain(PLAN_OPTIONS)
    g = glyphs()
    if index < 0:
        _print(_elbow("Kept the plan; nothing was changed", style=_style("warn")))
        _refresh_live()
        return ("stop", "")
    answer = PLAN_OPTIONS[index][0]
    if answer == "no":
        feedback = rest or _free_text("  What should change in the plan? ", pt_input=pt_input, pt_output=pt_output)
        if not feedback:
            _print(_elbow("Kept the plan; nothing was changed", style=_style("warn")))
            _refresh_live()
            return ("stop", "")
        _print(_elbow(f"Revising: {feedback}", style=_style("accent")))
        _refresh_live()
        return ("no", feedback)
    set_mode(answer, announce=False)
    _print(_elbow(f"{g.check} Plan approved. {MODE_LABELS[answer][:1].upper()}{MODE_LABELS[answer][1:]}.", style=_style("ok")))
    _refresh_live()
    return (answer, "")


def _choose_plain(options: list[tuple[str, str]], *, tries: int = 3) -> tuple[int, str]:
    """Numbered list and ``input()``, for consoles prompt_toolkit cannot drive."""
    for index, (_answer, label) in enumerate(options):
        log.print_safe(f"   {index + 1}. {label}", flush=True)
    for _ in range(max(1, tries)):
        try:
            line = input(f"  Choose ({_choice_hint(options)}): ")
        except (EOFError, KeyboardInterrupt):
            return (-1, "")
        if not line.strip():
            continue
        index, rest = parse_choice(line, options)
        if index >= 0:
            return (index, rest)
        log.print_safe(f"   Didn't understand {line.strip()!r}.", flush=True)
    return (-1, "")


def _ask_reason(*, pt_input: Any = None, pt_output: Any = None) -> str:
    try:
        from prompt_toolkit import PromptSession

        session: Any = PromptSession(input=pt_input, output=pt_output)
        return str(session.prompt("  Tell crit what to do differently (Enter to skip): ") or "").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    except Exception:
        try:
            return input("  Tell crit what to do differently (Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""


def ask_question(
    question: str,
    options: list[str] | None = None,
    *,
    pt_input: Any = None,
    pt_output: Any = None,
) -> str | None:
    """A question from the model. Options become a picker with a free-text last row.

    ``None`` when there is no terminal or the person cancels.
    """
    interactive = pt_input is not None or (_stdin_tty() and is_tty())
    if not interactive:
        return None
    _pause_live()
    g = glyphs()
    _print(
        Panel(
            Markdown(question.strip() or "?"),
            title=Text("crit has a question", style=_style("accent")),
            title_align="left",
            border_style=_style("accent"),
        )
    )
    choices = [str(item).strip() for item in options or [] if str(item).strip()]
    try:
        if choices:
            rows = [(f"option{index}", label) for index, label in enumerate(choices)]
            rows.append(("other", "Type something else"))
            index, rest = _choose(rows, pt_input=pt_input, pt_output=pt_output, free_text=len(choices))
            if index < 0:
                answer = None
            elif index < len(choices):
                answer = choices[index]
            else:
                answer = rest or _free_text("  Answer: ", pt_input=pt_input, pt_output=pt_output)
        else:
            answer = _free_text("  Answer: ", pt_input=pt_input, pt_output=pt_output)
    except (EOFError, KeyboardInterrupt):
        answer = None
    if answer:
        _print(_elbow(f"{g.check} {answer}", style=_style("ok")))
    else:
        _print(_elbow("No answer", style=_style("err")))
    _refresh_live()
    return answer or None


def _free_text(label: str, *, pt_input: Any = None, pt_output: Any = None) -> str:
    try:
        from prompt_toolkit import PromptSession

        session: Any = PromptSession(input=pt_input, output=pt_output)
        return str(session.prompt(label) or "").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    except Exception:
        try:
            return input(label).strip()
        except (EOFError, KeyboardInterrupt):
            return ""


def _stdin_tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


# --------------------------------------------------------------------------- input


class _FileIndex:
    """Workspace paths for ``@`` completion, cached for 30 seconds."""

    def __init__(self, root: Path, *, ttl: float = 30.0) -> None:
        self.root = Path(root)
        self.ttl = ttl
        self._paths: list[str] = []
        self._built = -1e9

    def paths(self) -> list[str]:
        if time.monotonic() - self._built > self.ttl:
            self._paths = self._scan()
            self._built = time.monotonic()
        return self._paths

    def _skip_names(self) -> set[str]:
        names = set(_SKIP_DIRS)
        try:
            text = (self.root / ".gitignore").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return names
        for line in text.splitlines():
            line = line.strip().strip("/")
            if line and not line.startswith(("#", "!")) and not any(ch in line for ch in "*?[/"):
                names.add(line)
        return names

    def _scan(self) -> list[str]:
        skip = self._skip_names()
        found: list[str] = []
        root = str(self.root)
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in skip and not (d.startswith(".") and d not in {".github", ".vscode"}))
            rel_dir = os.path.relpath(current, root)
            prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
            for name in dirs:
                found.append(prefix + name + "/")
            for name in sorted(files):
                found.append(prefix + name)
            if len(found) >= _MAX_INDEX_FILES:
                break
        return found[:_MAX_INDEX_FILES]

    def match(self, fragment: str, limit: int = 30) -> list[str]:
        frag = fragment.lower().replace("\\", "/")
        scored: list[tuple[int, int, str]] = []
        for path in self.paths():
            low = path.lower()
            base = low.rstrip("/").rsplit("/", 1)[-1]
            if not frag:
                rank = 0 if "/" not in path.rstrip("/") else 5
            elif base.startswith(frag):
                rank = 0
            elif low.startswith(frag):
                rank = 1
            elif frag in base:
                rank = 2
            elif frag in low:
                rank = 3
            elif _subsequence(frag, base):
                rank = 4
            else:
                continue
            scored.append((rank, len(path), path))
        scored.sort()
        return [path for _rank, _len, path in scored[:limit]]


def _subsequence(needle: str, hay: str) -> bool:
    it = iter(hay)
    return all(ch in it for ch in needle)


def file_index() -> _FileIndex:
    global _file_index
    with _lock:
        if _file_index is None or _file_index.root != _state.workspace:
            _file_index = _FileIndex(_state.workspace)
        return _file_index


def _make_completer() -> Any:
    from prompt_toolkit.completion import Completer, Completion

    class CritCompleter(Completer):
        def get_completions(self, document: Any, complete_event: Any) -> Iterable[Any]:
            before = document.text_before_cursor
            if before.startswith("/") and " " not in before and "\n" not in before:
                for name, meta in SLASH_COMMANDS.items():
                    if name.startswith(before.lower()):
                        yield Completion(name, start_position=-len(before), display_meta=meta)
                return
            match = re.search(r"(?:^|\s)@([^\s@]*)$", before)
            if match is None:
                return
            fragment = match.group(1)
            for path in file_index().match(fragment):
                yield Completion(path, start_position=-len(fragment), display=path)

    return CritCompleter()


def referenced_files(text: str, workspace: Path | None = None) -> list[str]:
    """``@path`` mentions in ``text`` that name a file or folder in the workspace."""
    root = Path(workspace or _state.workspace)
    found: list[str] = []
    for match in _MENTION_RE.finditer(text):
        raw = match.group(1).rstrip(",.;:!?)]}'\"")
        if not raw or raw in found:
            continue
        try:
            if (root / raw).exists():
                found.append(raw)
        except OSError:
            continue
    return found


def with_references(text: str, workspace: Path | None = None) -> str:
    files = referenced_files(text, workspace)
    if not files:
        return text
    return f"{text}\n\nReferenced files: {', '.join(files)}"


def _toolbar_text(armed_until: list[float]) -> Any:
    def render() -> list[tuple[str, str]]:
        if time.monotonic() < armed_until[0]:
            return [("class:hint.warn", "  Press Ctrl+C again to exit")]
        mode = _state.approve_mode
        badge = {"ask": "", "edits": "\u23f5\u23f5 accept edits on", "auto": "\u23f5\u23f5 auto mode on", "plan": "\u23f8 plan mode on"}
        if glyphs() is ASCII_GLYPHS:
            badge = {"ask": "", "edits": ">> accept edits on", "auto": ">> auto mode on", "plan": "|| plan mode on"}
        rows: list[tuple[str, str]] = []
        if badge.get(mode):
            rows.append((f"class:mode.{mode}", f"  {badge[mode]}"))
            rows.append(("class:hint", " (shift+tab to cycle)"))
        else:
            rows.append(("class:hint", "  ask before edits · shift+tab to cycle"))
        parts = [p for p in (_state.shell_label, "? for shortcuts") if p]
        rows.append(("class:hint", " · " + " · ".join(parts)))
        return rows

    return render


def _build_prompt_session(*, pt_input: Any = None, pt_output: Any = None) -> Any:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style

    history: Any = InMemoryHistory()
    if _state.history_path is not None:
        try:
            _state.history_path.parent.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(_state.history_path))
        except OSError:
            pass
    armed_until = [0.0]
    bindings = KeyBindings()

    @bindings.add("enter")
    def _submit(event: Any) -> None:
        buffer = event.current_buffer
        if buffer.complete_state and buffer.complete_state.current_completion is not None:
            buffer.apply_completion(buffer.complete_state.current_completion)
            return
        before = buffer.document.text_before_cursor
        if before.endswith("\\") and not before.endswith("\\\\"):
            buffer.delete_before_cursor(1)
            buffer.insert_text("\n")
            return
        buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    @bindings.add("c-j")
    def _newline(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("c-c")
    def _ctrl_c(event: Any) -> None:
        buffer = event.current_buffer
        if buffer.text:
            buffer.reset()
            armed_until[0] = 0.0
            return
        if time.monotonic() < armed_until[0]:
            event.app.exit(exception=EOFError())
            return
        armed_until[0] = time.monotonic() + 2.5
        event.app.invalidate()

        def _disarm() -> None:
            time.sleep(2.6)
            try:
                event.app.invalidate()
            except Exception:
                pass

        threading.Thread(target=_disarm, daemon=True).start()

    @bindings.add("s-tab", filter=Condition(lambda: not _completing()))
    @bindings.add("escape", "m")
    def _mode(event: Any) -> None:
        cycle_mode()
        event.app.invalidate()

    @bindings.add("?", filter=Condition(lambda: not _current_text()))
    def _shortcuts(event: Any) -> None:
        from prompt_toolkit.application import run_in_terminal

        run_in_terminal(print_shortcuts)

    accent = _pt_color(_PALETTES[_state.theme_id]["accent"]) or "ansiyellow"
    style = Style.from_dict(
        {
            "prompt": f"bold {accent}",
            "bottom-toolbar": "noreverse",
            "hint": "#888888",
            "hint.warn": "ansiyellow",
            "mode.edits": "ansimagenta",
            "mode.auto": f"bold {accent}",
            "mode.plan": "ansicyan",
            "continuation": "#888888",
        }
    )
    session: Any = PromptSession(
        history=history,
        completer=_make_completer(),
        complete_while_typing=True,
        multiline=True,
        key_bindings=bindings,
        bottom_toolbar=_toolbar_text(armed_until),
        prompt_continuation=lambda width, line_number, wrap_count: [("class:continuation", "  ")],
        style=style,
        enable_history_search=False,
        input=pt_input,
        output=pt_output,
    )
    return session


def _completing() -> bool:
    try:
        from prompt_toolkit.application import get_app

        return get_app().current_buffer.complete_state is not None
    except Exception:
        return False


def _current_text() -> str:
    try:
        from prompt_toolkit.application import get_app

        return get_app().current_buffer.text
    except Exception:
        return ""


def print_shortcuts() -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style=_style("accent"), no_wrap=True)
    table.add_column(style=_style("dim"))
    for keys, what in SHORTCUTS:
        table.add_row(keys, what)
    _print(Padding(table, (0, 0, 0, 2)))


def print_help() -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", no_wrap=True)
    table.add_column(style=_style("dim"))
    for name, meta in SLASH_COMMANDS.items():
        table.add_row(name, meta)
    _print(Text("Commands", style="bold"))
    _print(Padding(table, (0, 0, 0, 2)))
    _print(Text("Shortcuts", style="bold"))
    print_shortcuts()


def _rule() -> None:
    if is_tty():
        char = "-" if glyphs() is ASCII_GLYPHS else "─"
        _print(Text(char * max(10, console().width), style=_style("dim")))


def prompt_line(*, pt_input: Any = None, pt_output: Any = None) -> str | None:
    """One raw message from the input box. ``None`` = Ctrl+D or double Ctrl+C."""
    global _prompt_session
    _pause_live()
    if pt_input is not None:
        session = _build_prompt_session(pt_input=pt_input, pt_output=pt_output)
    else:
        if _prompt_session is None:
            _prompt_session = _build_prompt_session()
        session = _prompt_session
    _rule()
    try:
        text = session.prompt([("class:prompt", "> ")])
    except EOFError:
        return None
    except KeyboardInterrupt:
        return None
    return text


def _fallback_line() -> str | None:
    chunks: list[str] = []
    while True:
        prefix = "> " if not chunks else "  "
        try:
            line = input(prefix)
        except EOFError:
            log.print_safe(flush=True)
            return "\n".join(chunks).rstrip() if chunks else None
        except KeyboardInterrupt:
            log.print_safe(flush=True)
            return None
        if line.endswith("\\") and not line.endswith("\\\\"):
            chunks.append(line[:-1])
            continue
        chunks.append(line)
        return "\n".join(chunks).rstrip()


def read_message(
    *,
    reader: Callable[[], str | None] | None = None,
) -> str | None:
    """The next task from the input box, after handling slash commands.

    Returns ``None`` to exit, :data:`NEW_CHAT` (``"/new"``) to start a fresh
    chat, or the task text (with a ``Referenced files:`` note for ``@`` paths).
    """
    if reader is None:
        if not (_stdin_tty() and is_tty()):
            return None
        reader = _interactive_line
    while True:
        raw = reader()
        if raw is None:
            return None
        text = raw.strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered in _QUIT_WORDS:
            return None
        if text.startswith("/") and "\n" not in text:
            word, _, rest = text.partition(" ")
            word = word.lower()
            if word == NEW_CHAT:
                return NEW_CHAT
            if word in SLASH_COMMANDS:
                handle_command(word, rest.strip())
                continue
            if "/" not in word[1:]:
                note("bad", f"Unknown command {word}. Type /help for the list.")
                continue
        return with_references(text)


def _interactive_line() -> str | None:
    try:
        return prompt_line()
    except Exception as exc:  # e.g. NoConsoleScreenBufferError under mintty
        log.warn(f"input box unavailable, using plain input: {exc}")
        return _fallback_line()


def handle_command(word: str, rest: str = "") -> None:
    """Run a UI-level slash command (everything except /new and /exit)."""
    if word == "/help":
        print_help()
    elif word == "/clear":
        clear_screen()
    elif word == "/undo":
        _undo()
    elif word == "/theme":
        from critique_bot.welcome import reopen_theme

        _pause_live()
        reopen_theme(_state.workspace)
        _reload_theme()
    elif word in {"/mode", "/permissions"}:
        _mode_command(rest)
    elif word == "/plan":
        set_mode("plan")
    elif word == "/auto":
        set_mode("auto")
    elif word == "/skills":
        _skills_command(rest)
    elif word == "/status":
        print_status_table()
    elif word == "/shell":
        _shell_command(rest)
    elif word == "/tools":
        _print_tools()
    elif word == "/commands":
        print_commands()


def _mode_command(rest: str) -> None:
    word = rest.strip().lower()
    if not word:
        for name in MODES:
            mark = glyphs().pointer if name == _state.approve_mode else " "
            _print(Text(f" {mark} {name:<6} {MODE_LABELS[name]}", style="bold" if name == _state.approve_mode else _style("dim")))
        _print(Text("   /mode <name> or Shift+Tab to switch", style=_style("dim")))
        return
    if word not in _MODE_ALIASES:
        note("bad", f"Unknown mode {word}. Use one of: {', '.join(MODES)}.")
        return
    set_mode(word)


def _skills_command(rest: str) -> None:
    from critique_bot.agent_tools import discover_skills

    found = discover_skills(_state.workspace)
    names = {item["name"].lower(): item["name"] for item in found}
    words = rest.split()
    if words and words[0].lower() in {"off", "unpin", "remove"}:
        for word in words[1:] or list(_state.pinned_skills):
            name = names.get(word.lower(), word)
            with _lock:
                if name in _state.pinned_skills:
                    _state.pinned_skills.remove(name)
            note("note", f"Unpinned {name}.")
        return
    if words:
        for word in words:
            name = names.get(word.lower())
            if name is None:
                note("bad", f"No skill named {word}. Type /skills for the list.")
                continue
            with _lock:
                if name not in _state.pinned_skills:
                    _state.pinned_skills.append(name)
            note("note", f"Pinned {name}: it goes with every task this session.")
        return
    if not found:
        note("note", "No skills found.")
        return
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", no_wrap=True)
    table.add_column(style=_style("dim"), no_wrap=True)
    table.add_column(style=_style("dim"))
    for item in found:
        pinned = " (pinned)" if item["name"] in _state.pinned_skills else ""
        table.add_row(item["name"] + pinned, item.get("source", ""), _short(item.get("description", ""), 90))
    _print(Text("Skills (matched to each task automatically; /skills <name> to pin)", style="bold"))
    _print(Padding(table, (0, 0, 0, 2)))


def print_commands(limit: int = 20) -> None:
    """The last shell commands of this session: exit code, time, shell, and how each ended."""
    history = list(getattr(_state.session_shell, "history", None) or [])
    if not history:
        note("note", "No shell commands have run in this session.")
        return
    g = glyphs()
    shown = history[-limit:]
    failed = sum(1 for item in history if item.get("exit") not in (0,) )
    _print(Text(f"Shell commands ({len(history)} run, {failed} not exit 0)", style="bold"))
    for item in shown:
        code = item.get("exit")
        ok = code == 0
        mark = g.check if ok else g.cross
        line = Text("  ")
        line.append(f"{mark} ", style=_style("ok") if ok else _style("err"))
        line.append(_short(str(item.get("command") or ""), 70))
        line.append(f"  {item.get('result')} · {item.get('seconds')}s · {item.get('shell')}", style=_style("dim"))
        if item.get("retried"):
            line.append(f" · retried: {item['retried']}", style=_style("warn"))
        _print(line)


def _undo() -> None:
    if _state.cache_dir is None:
        note("bad", "Nothing to undo.")
        return
    from critique_bot.agent_edit import undo_last

    restored = undo_last(_state.cache_dir, _state.workspace)
    if not restored:
        note("note", "Nothing to undo.")
        return
    for path in restored:
        note("good", f"Restored {path}")


def _reload_theme() -> None:
    try:
        from critique_bot.bot_home import find_bot_home

        home = find_bot_home(_state.workspace)
    except Exception:
        return
    if home is not None:
        configure(theme=home.settings.get("theme"))


def print_status_table() -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style=_style("dim"), no_wrap=True)
    table.add_column()
    table.add_row("model", _state.model or "(default)")
    table.add_row("shell", _state.shell_label or "(default)")
    table.add_row("cwd", str(getattr(_state.session_shell, "cwd", None) or _state.workspace))
    table.add_row("workspace", str(_state.workspace))
    table.add_row("mode", MODE_LABELS.get(_state.approve_mode, _state.approve_mode))
    if _state.pinned_skills:
        table.add_row("skills", ", ".join(_state.pinned_skills))
    history = list(getattr(_state.session_shell, "history", None) or [])
    if history:
        failed = sum(1 for item in history if item.get("exit") != 0)
        table.add_row("commands", f"{len(history)} run, {failed} not exit 0 (/commands)")
    jobs = _jobs()
    if jobs is not None:
        running = [job for job in jobs if job.get("running")]
        table.add_row("background", f"{len(running)} running, {len(jobs)} total")
        for job in running[:5]:
            table.add_row("", f"{job.get('id')}: {_short(job.get('command', ''), 60)}")
    _print(Padding(table, (0, 0, 0, 2)))


def _jobs() -> list[dict] | None:
    session = _state.session_shell
    if session is None or not hasattr(session, "jobs"):
        return None
    try:
        return list(session.jobs())
    except Exception:
        return None


def _shell_command(name: str) -> None:
    from critique_bot import agent_shell

    if not hasattr(agent_shell, "available_shells"):
        note("note", f"Shell: {_state.shell_label or 'default'}")
        return
    try:
        shells = agent_shell.available_shells()
    except Exception as exc:
        note("bad", f"Could not list shells: {exc}")
        return
    if not name:
        current = _state.shell_label
        for key, shell in shells.items():
            mark = "*" if getattr(shell, "label", "") == current else " "
            note("note", f"{mark} {key:<11} {getattr(shell, 'label', '')}")
        note("note", "Type /shell <name> to switch the default for this session.")
        return
    chosen = shells.get(name.lower())
    if chosen is None:
        note("bad", f"No shell named {name}. Available: {', '.join(shells) or 'none'}")
        return
    session = _state.session_shell
    if session is not None and hasattr(session, "shell"):
        try:
            session.shell = chosen
        except Exception:
            pass
    configure(shell=chosen)
    with _lock:
        _state.shell_changed = chosen
    note("good", f"Default shell is now {getattr(chosen, 'label', name)} (this session).")


def _print_tools() -> None:
    names = _state.tools
    if not names:
        try:
            from critique_bot.agent_tools import ALLOWED_TOOLS

            names = tuple(ALLOWED_TOOLS)
        except Exception:
            names = ()
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", no_wrap=True)
    table.add_column(style=_style("dim"))
    for name in names:
        table.add_row(tool_title(name, {}), name)
    _print(Padding(table, (0, 0, 0, 2)))
