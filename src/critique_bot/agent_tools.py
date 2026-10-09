"""Tool handlers for the local agent and the per-task working memory.

Every handler takes the parsed arguments and a :class:`ToolContext` and
returns ``{"tool", "ok", "output"/"error"}`` plus a ``"ui"`` dict for the
terminal (``summary``, ``diff``, ``lines``). Output is bounded so one call
cannot push the task out of the chat's view. :func:`permission_for` says what
the user must approve before a call runs.

The model behind the chat sends imperfect calls: other tools' argument names,
``./`` or backslash paths, numbers and booleans as strings, and lists as JSON
text. :func:`normalize_args` and :func:`resolve` accept all of those.
"""

from __future__ import annotations

import difflib
import fnmatch
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Callable

from critique_bot import agent_build, agent_edit, agent_shell, code_graph, code_index
from critique_bot.code_index import SKIP_DIR_NAMES, refresh_path
from critique_bot.patch import looks_binary_bytes, looks_binary_path

ALLOWED_TOOLS = (
    "list_files",
    "find_files",
    "read_files",
    "search_code",
    "write_files",
    "edit_file",
    "delete_file",
    "run_command",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "apply_patch",
    "todo",
    "skill",
    "code_graph",
    "move_file",
    "web_fetch",
    "ask_user",
    "command_output",
    "kill_command",
    "delegate",
)
MUTATING = frozenset({"write_files", "edit_file", "delete_file", "apply_patch", "move_file"})
READ_ONLY = frozenset(
    {
        "list_files",
        "find_files",
        "read_files",
        "search_code",
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "todo",
        "skill",
        "code_graph",
        "ask_user",
        "command_output",
        "kill_command",
    }
)
SHELL_NAMES = ("bash", "sh", "zsh", "pwsh", "powershell", "cmd", "gitbash")

DEFAULT_TOOL_CHARS = 16_000
DEFAULT_COMMAND_TIMEOUT = 120
MAX_COMMAND_TIMEOUT = 600
DEFAULT_READ_LINES = 200
MAX_READ_LINES = 400
MAX_READ_CHARS = 12_000
OUTLINE_MIN_LINES = 400
MAX_LIST_ENTRIES = 2_000
SNIPPET_CHARS = 200
HITS_PER_FILE = 5
_SCAN_MAX_BYTES = 1_000_000
_SCAN_LINE_CHARS = 2_000
_SCAN_SECONDS = 20.0
MAX_READ_PATHS = 20
FETCH_TIMEOUT = 20.0
FETCH_MAX_BYTES = 2_000_000
FETCH_MAX_REDIRECTS = 5
DEFAULT_FETCH_CHARS = 12_000
_CASE_FOLD = sys.platform == "win32"


@dataclass
class ReadRecord:
    version: str
    step: int
    spans: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class TaskState:
    """What the harness knows about the current task, repeated to the model."""

    task: str = ""
    step: int = 0
    reads: dict[str, ReadRecord] = field(default_factory=dict)
    edits: dict[str, int] = field(default_factory=dict)
    last_command: str = ""
    last_exit: str = ""
    failures_in_row: int = 0
    failed_calls: dict[str, int] = field(default_factory=dict)
    last_path: str = ""
    todos: list[dict[str, str]] = field(default_factory=list)
    commands: int = 0
    commands_failed: int = 0

    @property
    def mutated(self) -> bool:
        return bool(self.edits)

    def note_read(self, rel: str, version: str, start: int, end: int) -> None:
        record = self.reads.get(rel)
        if record is None or record.version != version:
            record = ReadRecord(version=version, step=self.step)
            self.reads[rel] = record
        record.spans.append((start, end))
        self.last_path = rel

    def seen_span(self, rel: str, version: str, start: int, end: int) -> int | None:
        """Step of an earlier read that already covered ``start``-``end`` unchanged."""
        record = self.reads.get(rel)
        if record is None or record.version != version:
            return None
        for begin, finish in record.spans:
            if begin <= start and end <= finish:
                return record.step
        return None

    def note_write(self, rel: str, version: str) -> None:
        self.edits[rel] = self.edits.get(rel, 0) + 1
        record = self.reads.get(rel)
        if record is not None:
            record.version = version
            record.spans = []
        self.last_path = rel

    def render(self, template: str) -> str:
        reads = ", ".join(
            f"{path} ({_spans_text(record.spans) or 'outline'})"
            for path, record in list(self.reads.items())[-8:]
        ) or "none"
        edits = ", ".join(f"{path} x{count}" for path, count in self.edits.items()) or "none"
        command = f"{_one_line(self.last_command, 80)} -> {self.last_exit}" if self.last_command else "none"
        if self.commands > 1:
            command += f" ({self.commands} commands in this task, {self.commands_failed} failed)"
        todos = ", ".join(
            f"[{item['status']}] {item['content']}" for item in self.todos[:8]
        ) or "none"
        return fill(
            template,
            task=_one_line(self.task, 600),
            step=str(self.step),
            reads=reads,
            edits=edits,
            command=command,
            todos=todos,
            failures=str(self.failures_in_row),
            path=self.last_path or "path/to/file",
        )


def fill(template: str, **values: str) -> str:
    """Replace ``{name}`` markers. Prompt text holds JSON braces, so not ``str.format``."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def _spans_text(spans: list[tuple[int, int]]) -> str:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return ",".join(f"{a}-{b}" for a, b in merged[:4])


@dataclass
class ToolContext:
    workspace: Path
    index_path: Path | None = None
    cache_dir: Path | None = None
    max_chars: int = DEFAULT_TOOL_CHARS
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT
    #: A build (gradle, mvn, m, npm install, ...) runs at least this long before it is moved to the background.
    build_timeout: float = agent_build.DEFAULT_BUILD_TIMEOUT
    #: Automatic retries of a build that failed for a passing reason (locked files, a dropped download).
    command_retries: int = 1
    runner: Callable[..., Any] | None = None
    state: TaskState | None = None
    checkpoints: agent_edit.Checkpoints | None = None
    shell: agent_shell.Shell | None = None
    session: Any = None  # agent_shell.ShellSession
    on_output: Callable[[str], None] | None = None
    ask_user: Callable[[str], str] | None = None
    cancel: threading.Event | None = None
    #: Commands that can send data off this machine: "block" (the default) or "ask".
    network_commands: str = "block"
    #: Sites web_fetch may read (documentation). None means DEFAULT_WEB_HOSTS; empty turns web_fetch off.
    web_hosts: tuple[str, ...] | None = None
    #: Runs briefs in helper chat tabs at the same time (see agent_helpers). None when there are none.
    delegate: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()


@dataclass
class Permission:
    """What a call needs from the user before it runs.

    ``kind`` is read, edit, command, network, or outside. ``key`` is what an
    "always allow" answer remembers; an empty key is never remembered.
    """

    kind: str
    summary: str
    key: str
    detail: str = ""
    #: Why this needs a person's yes in every mode, auto included ("" when it does not).
    risk: str = ""


def execute(name: str, arguments: dict[str, Any] | None, ctx: ToolContext) -> dict[str, Any]:
    canonical = canonical_tool(name)
    if not canonical:
        hint = _did_you_mean(name)
        error = "unknown tool" + (f"; did you mean {hint}?" if hint else "")
        return {
            "tool": name,
            "ok": False,
            "error": error,
            "allowed": list(ALLOWED_TOOLS),
            "ui": _ui(f"Unknown tool {name}"),
        }
    args = normalize_args(canonical, arguments if isinstance(arguments, (dict, str)) else {})
    try:
        result = _HANDLERS[canonical](args, ctx)
    except Exception as exc:  # a tool bug must come back as a result, not end the task
        result = {"tool": canonical, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result.setdefault("tool", canonical)
    if "ui" not in result:
        result["ui"] = _default_ui(canonical, result)
    return result


def _ui(summary: str, diff: str | None = None, lines: list[str] | None = None) -> dict[str, Any]:
    return {"summary": summary, "diff": diff or None, "lines": lines or None}


def _preview(text: str, count: int = 5, *, tail: bool = False) -> list[str] | None:
    rows = [line for line in str(text or "").split("\n") if line.strip()]
    if not rows:
        return None
    picked = rows[-count:] if tail else rows[:count]
    return [_one_line(line, 160) for line in picked]


def _default_ui(name: str, result: dict[str, Any]) -> dict[str, Any]:
    output = str(result.get("output") or "")
    if not result.get("ok"):
        return _ui("Error: " + _one_line(str(result.get("error") or "failed"), 140), lines=_preview(output, 3))
    label = name.replace("_", " ")
    if name == "read_files":
        count = sum(1 for line in output.split("\n") if _NUMBERED_RE.match(line))
        return _ui(f"Read {count} lines")
    if name == "todo":
        rows = [line for line in output.split("\n") if line.strip() and line != "no todos"]
        return _ui(f"Todos ({len(rows)})", lines=rows[:8])
    if name == "skill":
        return _ui("Loaded skill" if "\n" in output else "Listed skills", lines=_preview(output, 3))
    if name == "code_graph":
        return _ui("Queried code graph", lines=_preview(output, 3))
    if name.startswith("git_"):
        body = output.split("\n", 1)[1] if output.startswith("exit ") and "\n" in output else output
        return _ui(f"Ran git {name[4:]}", lines=_preview(body))
    return _ui(f"Ran {label}", lines=_preview(output))


def _diff_counts(diff: str) -> tuple[int, int]:
    added = removed = 0
    for line in diff.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


_ALIASES = {
    "grep": "search_code",
    "rg": "search_code",
    "search": "search_code",
    "glob": "find_files",
    "glob_files": "find_files",
    "find": "find_files",
    "ls": "list_files",
    "list": "list_files",
    "read": "read_files",
    "read_file": "read_files",
    "cat": "read_files",
    "write": "write_files",
    "write_file": "write_files",
    "create_file": "write_files",
    "edit": "edit_file",
    "search_replace": "edit_file",
    "multi_edit": "edit_file",
    "str_replace": "edit_file",
    "bash": "run_command",
    "shell": "run_command",
    "powershell": "run_command",
    "terminal": "run_command",
    "todowrite": "todo",
    "todoread": "todo",
    "todos": "todo",
    "patch": "apply_patch",
    "codegraph": "code_graph",
    "codegraph_explore": "code_graph",
    "graphify": "code_graph",
    "query_graph": "code_graph",
    "mv": "move_file",
    "move": "move_file",
    "rename_file": "move_file",
    "rename": "move_file",
    "fetch": "web_fetch",
    "webfetch": "web_fetch",
    "web": "web_fetch",
    "ask": "ask_user",
    "question": "ask_user",
    "ask_question": "ask_user",
    "bash_output": "command_output",
    "bashoutput": "command_output",
    "get_output": "command_output",
    "kill_shell": "kill_command",
    "killshell": "kill_command",
    "kill": "kill_command",
    "task": "delegate",
    "spawn": "delegate",
    "subagent": "delegate",
    "sub_agent": "delegate",
    "parallel": "delegate",
    "dispatch_agent": "delegate",
}


def canonical_tool(name: str) -> str:
    """The tool that actually runs. OpenCode names map onto these."""
    key = str(name).strip().strip("`\"'")
    if key in ALLOWED_TOOLS:
        return key
    lowered = key.lower().replace("-", "_")
    if lowered in ALLOWED_TOOLS:
        return lowered
    return _ALIASES.get(lowered, "") or _ALIASES.get(lowered.replace("_", ""), "")


_BOOL_KEYS = frozenset(
    {
        "overwrite", "replace_all", "force", "literal", "fixed_strings", "case_insensitive",
        "staged", "stat", "background", "run_in_background", "create_dirs",
    }
)
_NUMBER_KEYS = frozenset(
    {
        "offset", "limit", "count", "end_line", "start_line", "line", "max_entries", "depth",
        "head_limit", "context", "timeout", "max_chars", "wait", "lines",
    }
)
_JSON_KEYS = frozenset({"paths", "files", "edits", "todos", "items", "options", "arguments", "args"})
_TRUE = {"true", "yes", "y", "1", "on"}
_FALSE = {"false", "no", "n", "0", "off", "none", "null", ""}


def _coerce_bool(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    return value


def _coerce_number(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip().lower()
        scale = 1.0
        if text.endswith("ms"):
            text, scale = text[:-2], 0.001
        elif text.endswith("s"):
            text = text[:-1]
        try:
            number = float(text.strip()) * scale
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "[{" and text[-1:] in "]}":
            try:
                return json.loads(text)
            except ValueError:
                return value
    return value


def normalize_args(tool: str, args: dict[str, Any] | str) -> dict[str, Any]:
    """Accept the argument names and shapes OpenCode, Claude Code, and a chat model send.

    Nested ``arguments``/``input`` objects are unwrapped, JSON text in a list
    field is parsed, ``"true"``/``"12"`` become a bool/number, and the other
    tools' names for the same argument are mapped.
    """
    if isinstance(args, str):
        parsed = _maybe_json(args)
        args = parsed if isinstance(parsed, dict) else ({"command": args} if tool == "run_command" else {"path": args})
    out = dict(args)
    for wrapper in ("arguments", "args", "input", "parameters", "params"):
        inner = _maybe_json(out.get(wrapper))
        if isinstance(inner, dict) and len(out) == 1:
            out = dict(inner)
            break
    for key in list(out):
        if key in _JSON_KEYS:
            out[key] = _maybe_json(out[key])
        elif key in _BOOL_KEYS:
            out[key] = _coerce_bool(out[key])
        elif key in _NUMBER_KEYS:
            out[key] = _coerce_number(out[key])
    for src, dest in (
        ("filePath", "path"),
        ("file_path", "path"),
        ("filepath", "path"),
        ("filename", "path"),
        ("file", "path"),
        ("workdir", "cwd"),
        ("working_directory", "cwd"),
        ("workingDirectory", "cwd"),
        ("dir", "cwd") if tool == "run_command" else ("dir", "path"),
        ("directory", "cwd") if tool == "run_command" else ("directory", "path"),
        ("folder", "path"),
    ):
        if dest not in out and src in out:
            out[dest] = out[src]
    if isinstance(out.get("path"), list) and "paths" not in out:
        out["paths"] = out.pop("path")
    if tool == "edit_file":
        for src, dest in (
            ("oldString", "old_string"), ("newString", "new_string"), ("replaceAll", "replace_all"),
            ("old_str", "old_string"), ("new_str", "new_string"), ("search", "old_string"), ("replace", "new_string"),
        ):
            if dest not in out and src in out:
                out[dest] = out[src]
        if isinstance(out.get("edits"), list):
            out["edits"] = [_normalize_edit(item) for item in out["edits"]]
        if "replace_all" in out:
            out["replace_all"] = _coerce_bool(out["replace_all"])
    if tool == "write_files":
        if "contents" not in out:
            for src in ("content", "text", "body", "data"):
                if src in out:
                    out["contents"] = out[src]
                    break
        if isinstance(out.get("files"), dict):
            files = out["files"]
            out["files"] = [files] if "path" in files or "filePath" in files else [
                {"path": key, "contents": value} for key, value in files.items()
            ]
        if isinstance(out.get("files"), list):
            out["files"] = [_normalize_file(item) for item in out["files"]]
    if tool == "search_code":
        if "glob" not in out and isinstance(out.get("include"), str):
            out["glob"] = out["include"]
        if "pattern" not in out:
            for src in ("query", "regex", "text", "search"):
                if isinstance(out.get(src), str):
                    out["pattern"] = out[src]
                    break
        if "case_insensitive" not in out:
            if "caseSensitive" in out:
                out["case_insensitive"] = not _coerce_bool(out["caseSensitive"])
            elif "-i" in out:
                out["case_insensitive"] = _coerce_bool(out["-i"])
    if tool == "read_files" and "paths" not in out and isinstance(out.get("files"), list):
        out["paths"] = out["files"]
    if tool == "run_command":
        if "command" not in out:
            for src in ("cmd", "script", "commands"):
                if src in out:
                    value = out[src]
                    out["command"] = "\n".join(map(str, value)) if isinstance(value, list) else value
                    break
        if "background" not in out and "run_in_background" in out:
            out["background"] = out["run_in_background"]
        timeout = out.get("timeout")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > MAX_COMMAND_TIMEOUT:
            out["timeout"] = float(timeout) / 1000.0
    if tool in {"command_output", "kill_command"}:
        for src in ("job_id", "id", "bash_id", "shell_id", "job"):
            if src in out and "job_id" not in out:
                out["job_id"] = str(out[src])
    if tool == "move_file":
        for src in ("source", "src", "from", "old_path", "oldPath"):
            if "path" not in out and src in out:
                out["path"] = out[src]
        for src in ("dest", "to", "target", "new_path", "newPath", "destination_path"):
            if "destination" not in out and src in out:
                out["destination"] = out[src]
    if tool == "web_fetch":
        for src in ("uri", "link", "href", "address"):
            if "url" not in out and src in out:
                out["url"] = out[src]
        if "max_chars" not in out and "limit" in out:
            out["max_chars"] = out["limit"]
        if "max_chars" in out:
            out["max_chars"] = _coerce_number(out["max_chars"])
    if tool == "ask_user":
        for src in ("prompt", "text", "message", "q"):
            if "question" not in out and src in out:
                out["question"] = out[src]
        for src in ("choices", "answers"):
            if "options" not in out and src in out:
                out["options"] = _maybe_json(out[src])
    if tool == "git_show" and "rev" not in out:
        for src in ("commit", "ref", "sha", "revision"):
            if src in out:
                out["rev"] = out[src]
                break
    if tool == "git_show" and "patch" in out:
        out["patch"] = _coerce_bool(out["patch"])
    if tool == "skill" and "name" not in out and "skill" in out:
        out["name"] = out["skill"]
    return out


def _normalize_edit(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    edit = dict(item)
    for src, dest in (
        ("oldString", "old_string"), ("newString", "new_string"), ("replaceAll", "replace_all"),
        ("old_str", "old_string"), ("new_str", "new_string"), ("old", "old_string"), ("new", "new_string"),
        ("search", "old_string"), ("replace", "new_string"),
    ):
        if dest not in edit and src in edit:
            edit[dest] = edit[src]
    if "replace_all" in edit:
        edit["replace_all"] = _coerce_bool(edit["replace_all"])
    return edit


def _normalize_file(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    entry = dict(item)
    for src in ("filePath", "file_path", "filename", "file", "name"):
        if "path" not in entry and src in entry:
            entry["path"] = entry[src]
    for src in ("content", "text", "body", "data"):
        if "contents" not in entry and src in entry:
            entry["contents"] = entry[src]
    if "overwrite" in entry:
        entry["overwrite"] = _coerce_bool(entry["overwrite"])
    return entry


def _did_you_mean(name: str) -> str:
    import difflib

    pool = list(ALLOWED_TOOLS) + list(_ALIASES)
    matches = difflib.get_close_matches(str(name).strip().lower(), [item.lower() for item in pool], n=1, cutoff=0.8)
    if not matches:
        return ""
    return canonical_tool(matches[0]) or matches[0]


# --------------------------------------------------------------------------- paths


def clean_path(raw: Any) -> str:
    """Undo the usual ways a chat model mangles a path.

    Surrounding quotes, backticks, and spaces; ``file://`` URLs; ``@`` and
    ``./`` prefixes; trailing slashes; and, off Windows, backslashes.
    """
    text = str(raw if raw is not None else "").strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    text = text.strip("`").strip()
    if text.lower().startswith("file://"):
        text = urllib.parse.unquote(text[7:])
        if re.match(r"^/[A-Za-z]:[/\\]", text):
            text = text[1:]
    if text.startswith("@") and len(text) > 1:
        text = text[1:]
    if os.sep == "/" and "\\" in text and not re.match(r"^[A-Za-z]:", text):
        text = text.replace("\\", "/")
    while text.startswith(("./", ".\\")) and len(text) > 2:
        text = text[2:]
    if text not in {"/", "\\"} and len(text) > 1:
        text = text.rstrip("/\\") or text
    return text or "."


_LINE_SUFFIX_RE = re.compile(r"^(.+\.\w+)(?:#L\d+(?:-L?\d+)?|:\d+(?::\d+)?(?:-\d+)?)$")


def resolve(workspace: Path, raw: Any, *, follow: bool = True) -> Path:
    """Absolute path for ``raw``, following symbolic links (``follow`` False keeps a final link).

    Relative paths are under the workspace. A rooted path that does not
    exist but names a file under the workspace (``/src/app.py``) is taken as
    workspace-relative, and so is a path that starts with the workspace's own
    folder name. Paths outside stay outside: :func:`permission_for` gates them.
    """
    workspace = Path(workspace)
    text = clean_path(raw)
    path = _resolve_text(workspace, text, follow)
    if not os.path.lexists(path):
        suffix = _LINE_SUFFIX_RE.match(text)
        if suffix:
            stripped = _resolve_text(workspace, suffix.group(1), follow)
            if stripped.is_file():
                return stripped
    return path


def _final(path: Path, follow: bool) -> Path:
    path = Path(os.path.abspath(path))
    if follow or not path.name:
        return path.resolve()
    return path.parent.resolve() / path.name


def _resolve_text(workspace: Path, text: str, follow: bool = True) -> Path:
    if text.startswith("~"):
        text = os.path.expanduser(text)
    path = Path(text)
    rooted = path.is_absolute() or text[:1] in "/\\"
    if not rooted:
        parts = path.parts
        if parts and parts[0] == workspace.name and not (workspace / text).exists():
            rest = Path(*parts[1:]) if len(parts) > 1 else Path(".")
            if (workspace / rest).exists():
                return _final(workspace / rest, follow)
        return _final(workspace / path, follow)
    unc = text[:2] in ("//", "\\\\")
    if not path.is_absolute() or (not os.path.lexists(path) and not unc and text[:1] in "/\\"):
        inner = workspace / text.lstrip("/\\")
        outer = Path(os.path.abspath(text))
        if inner.exists() or inner.parent.exists() or not outer.parent.exists():
            return _final(inner, follow)
        if not path.is_absolute():
            return _final(outer, follow)
    return _final(path, follow)


def inside(workspace: Path, path: Path) -> bool:
    """True when ``path`` (already resolved) is the workspace or under it."""
    try:
        Path(path).relative_to(workspace)
        return True
    except ValueError:
        pass
    if _CASE_FOLD:
        left = os.path.normcase(str(path))
        root = os.path.normcase(str(workspace)).rstrip("\\/")
        return left == root or left.startswith(root + os.sep)
    return False


def rel(workspace: Path, path: Path) -> str:
    """Workspace-relative POSIX path, or the absolute path for a file outside."""
    resolved = Path(os.path.abspath(path))
    try:
        return resolved.relative_to(workspace).as_posix()
    except ValueError:
        pass
    if _CASE_FOLD:
        left = os.path.normcase(str(resolved))
        root = os.path.normcase(str(workspace))
        if left.startswith(root.rstrip("\\/") + os.sep):
            return str(resolved)[len(str(workspace).rstrip("\\/")) + 1 :].replace("\\", "/")
    return str(resolved)


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Glob to regex. ``**`` spans directories; ``*`` and ``?`` stay inside one.

    A pattern without a slash matches the file name at any depth, the way
    ripgrep's ``-g`` and ``.gitignore`` do.
    """
    text = pattern.replace("\\", "/").strip()
    if text.startswith("./"):
        text = text[2:]
    anywhere = "/" not in text.rstrip("/")
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if text.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
            continue
        if text.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            close = text.find("]", index + 1)
            if close < 0:
                out.append(re.escape(char))
            else:
                body = text[index + 1 : close].replace("!", "^", 1) if text[index + 1 : index + 2] == "!" else text[index + 1 : close]
                out.append("[" + body + "]")
                index = close
        elif char == "{":
            close = text.find("}", index + 1)
            if close < 0:
                out.append(re.escape(char))
            else:
                options = text[index + 1 : close].split(",")
                out.append("(?:" + "|".join(re.escape(item) for item in options) + ")")
                index = close
        else:
            out.append(re.escape(char))
        index += 1
    body = "".join(out)
    prefix = "(?:.*/)?" if anywhere else ""
    flags = re.IGNORECASE if _CASE_FOLD else 0
    return re.compile("^" + prefix + body + "$", flags)


def glob_match(rel_path: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    return glob_regex(pattern).match(rel_path) is not None


def walk_files(workspace: Path, root: Path) -> list[str]:
    """Workspace-relative file paths under ``root``, skipping generated trees."""
    if root.is_file():
        return [rel(workspace, root)]
    prefix = rel(workspace, root)
    if prefix in {".", ""} or root == workspace:
        prefix = ""
    listed = code_index._git_files(workspace)
    if listed is not None:
        out = []
        for item in listed:
            if prefix and not (item == prefix or item.startswith(prefix + "/")):
                continue
            if any(part in SKIP_DIR_NAMES for part in item.split("/")[:-1]):
                continue
            out.append(item)
        return out
    rows: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIR_NAMES)
        for name in sorted(filenames):
            rows.append(rel(workspace, Path(dirpath) / name))
    return rows


# --------------------------------------------------------------------------- results


def _cap(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    note = "\n...[truncated; narrow with path, offset, or a tighter pattern]"
    return text[: max(0, limit - len(note))] + note


def ok(name: str, output: str, ctx: ToolContext, ui: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"tool": name, "ok": True, "output": _cap(output, ctx.max_chars)}
    if ui is not None:
        result["ui"] = ui
    return result


def err(
    name: str,
    error: str,
    *,
    output: str = "",
    ctx: ToolContext | None = None,
    ui: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"tool": name, "ok": False, "error": error}
    if output:
        result["output"] = _cap(output, ctx.max_chars) if ctx else output
    if ui is not None:
        result["ui"] = ui
    return result


def _one_line(text: str, limit: int = 120) -> str:
    compact = " ".join(str(text).split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _int_arg(args: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = args.get(name)
        if value is None or value == "":
            continue
        return int(float(value))
    return None


# --------------------------------------------------------------------------- list / find


def _list_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw = args.get("path") or "."
    if not isinstance(raw, str):
        return err("list_files", "path must be a string")
    target = resolve(ctx.workspace, raw.strip() or ".")
    if not target.exists():
        return err("list_files", f"not found: {raw}" + _path_hint(ctx, raw))
    glob = args.get("glob")
    if glob is not None and not isinstance(glob, str):
        return err("list_files", "glob must be a string")
    try:
        max_entries = int(args.get("max_entries") or 200)
        depth = int(args.get("depth") or 1)
    except (TypeError, ValueError):
        return err("list_files", "max_entries and depth must be integers")
    max_entries = min(max(max_entries, 1), MAX_LIST_ENTRIES)
    depth = min(max(depth, 1), 4)
    if target.is_file():
        return ok("list_files", f"f {rel(ctx.workspace, target)}", ctx, _ui("Listed 1 file"))
    recursive = bool(glob and ("**" in glob or "/" in glob))
    if recursive:
        files = [item for item in walk_files(ctx.workspace, target) if glob_match(item, glob)]
        rows = [f"f {item}" for item in files[:max_entries]]
        if len(files) > max_entries:
            rows.append(f"... {len(files) - max_entries} more; narrow path or glob")
        return ok(
            "list_files",
            "\n".join(rows) if rows else "(no files match)" + _name_hint(ctx, glob or ""),
            ctx,
            _ui(f"Listed {len(files)} files"),
        )
    rows = []
    total = 0
    for line in _tree(ctx.workspace, target, depth, glob):
        total += 1
        if len(rows) < max_entries:
            rows.append(line)
    if total > max_entries:
        rows.append(f"... {total - max_entries} more; narrow path, glob, or depth")
    rows.append("Generated trees (out, build, prebuilts, intermediates, node_modules) are skipped.")
    return ok(
        "list_files",
        "\n".join(rows) if len(rows) > 1 else "(empty)",
        ctx,
        _ui(f"Listed {total} entries"),
    )


def _tree(workspace: Path, folder: Path, depth: int, glob: str | None, level: int = 0):
    try:
        children = sorted(folder.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    except OSError as exc:
        yield f"! {exc}"
        return
    pad = "  " * level
    for child in children:
        if child.is_dir():
            if child.name in SKIP_DIR_NAMES:
                continue
            count = _count_files(child)
            yield f"{pad}d {rel(workspace, child)}/ ({count} files)"
            if level + 1 < depth:
                yield from _tree(workspace, child, depth, glob, level + 1)
        else:
            if glob and not fnmatch.fnmatch(child.name.lower() if _CASE_FOLD else child.name, glob.lower() if _CASE_FOLD else glob):
                continue
            yield f"{pad}f {rel(workspace, child)}"


def _count_files(folder: Path, limit: int = 10_000) -> str:
    count = 0
    for _dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        count += len(filenames)
        if count >= limit:
            return f"{limit}+"
    return str(count)


def _find_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pattern = args.get("glob") or args.get("pattern") or args.get("name")
    if not isinstance(pattern, str) or not pattern.strip():
        return err("find_files", "glob is required, for example **/*Service.java or *.kt")
    raw = args.get("path") or "."
    if not isinstance(raw, str):
        return err("find_files", "path must be a string")
    root = resolve(ctx.workspace, raw)
    if not root.exists():
        return err("find_files", f"not found: {raw}" + _path_hint(ctx, raw))
    try:
        limit = min(max(int(args.get("limit") or args.get("head_limit") or 100), 1), 500)
    except (TypeError, ValueError):
        return err("find_files", "limit must be an integer")
    files = [item for item in walk_files(ctx.workspace, root) if glob_match(item, pattern.strip())]
    if not files and "*" not in pattern and "?" not in pattern:
        needle = pattern.strip().lower()
        files = [item for item in walk_files(ctx.workspace, root) if needle in item.lower()]

    def mtime(item: str) -> float:
        try:
            return (ctx.workspace / item).stat().st_mtime
        except OSError:
            return 0.0

    files.sort(key=lambda item: -mtime(item))
    rows = files[:limit]
    if len(files) > limit:
        rows.append(f"... {len(files) - limit} more; narrow the glob or path")
    if not rows:
        return ok("find_files", "(no files match)" + _name_hint(ctx, pattern.strip()), ctx, _ui("Found 0 files"))
    return ok("find_files", "\n".join(rows), ctx, _ui(f"Found {len(files)} files", lines=rows[:5]))


def _name_hint(ctx: ToolContext, pattern: str) -> str:
    """Names close to what a glob was after, for a search that found nothing."""
    stem = re.sub(r"[*?\[\]{}]", "", pattern.replace("\\", "/").rsplit("/", 1)[-1]).strip(".").lower()
    if len(stem) < 3:
        return ""
    files = walk_files(ctx.workspace, ctx.workspace)
    names: dict[str, str] = {}
    for item in files:
        names.setdefault(item.rsplit("/", 1)[-1].lower(), item)
    close = difflib.get_close_matches(stem, list(names), n=5, cutoff=0.6)
    if not close:
        base = stem.rsplit(".", 1)[0]
        close = [name for name in names if base and base in name][:5]
    if not close:
        return ""
    return "; similar names: " + ", ".join(names[name] for name in close)


def _path_hint(ctx: ToolContext, raw: str) -> str:
    name = Path(str(raw)).name
    if not name:
        return ""
    regex = glob_regex(name)
    hits = [item for item in walk_files(ctx.workspace, ctx.workspace) if regex.match(item)][:5]
    if not hits:
        lowered = name.lower()
        hits = [item for item in walk_files(ctx.workspace, ctx.workspace) if lowered in item.lower()][:5]
    return ("; files with that name: " + ", ".join(hits)) if hits else ""


# --------------------------------------------------------------------------- read


def _read_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    symbol = args.get("symbol")
    paths = args.get("paths")
    if paths is None and args.get("path"):
        paths = [args.get("path")]
    if isinstance(paths, str):
        paths = [part for part in re.split(r"[\n,]", paths) if part.strip()] if "\n" in paths else [paths]
    if symbol and isinstance(symbol, str):
        first = paths[0] if paths else None
        return _read_symbol(symbol.strip(), first.get("path") if isinstance(first, dict) else first, args, ctx)
    if not isinstance(paths, list) or not paths:
        return err("read_files", 'paths must be a list of files, for example {"paths": ["src/a.py"]}, or pass symbol')
    try:
        offset = _int_arg(args, "offset", "start_line", "line")
        limit = _int_arg(args, "limit", "count")
        end_line = _int_arg(args, "end_line")
    except (TypeError, ValueError):
        return err("read_files", "offset and limit must be integers")
    start = offset if offset is not None else 1
    if start < 1:
        return err("read_files", "offset starts at 1")
    if limit is None and end_line is not None:
        limit = end_line - start + 1
    if limit is not None and limit < 1:
        return err("read_files", "limit must be at least 1")
    force = bool(args.get("force"))
    wanted = paths[:MAX_READ_PATHS]
    budget = max(800, min(MAX_READ_CHARS, (ctx.max_chars - 200) // max(len(wanted), 1)))
    parts: list[str] = []
    for raw in wanted:
        item_start, item_limit, item_explicit = start, limit, offset is not None
        if isinstance(raw, dict):
            try:
                own_offset = _int_arg(raw, "offset", "start_line", "line")
                own_limit = _int_arg(raw, "limit", "count")
            except (TypeError, ValueError):
                return err("read_files", "offset and limit must be integers")
            if own_offset is not None:
                item_start, item_explicit = max(1, own_offset), True
            if own_limit is not None:
                item_limit = max(1, own_limit)
            raw = raw.get("path") or raw.get("file") or raw.get("filePath")
        if not isinstance(raw, str) or not raw.strip():
            return err("read_files", "each path must be a string")
        parts.append(_read_one(raw, item_start, item_limit, item_explicit, budget, force, ctx))
    if len(paths) > MAX_READ_PATHS:
        parts.append(f"... {len(paths) - MAX_READ_PATHS} more paths not read; read at most {MAX_READ_PATHS} per call")
    output = "\n".join(parts)
    shown = sum(1 for line in output.split("\n") if _NUMBERED_RE.match(line))
    files = sum(1 for line in output.split("\n") if line.startswith("--- "))
    summary = f"Read {shown} lines" + (f" from {files} files" if files > 1 else "")
    return ok("read_files", output, ctx, _ui(summary))


_NUMBERED_RE = re.compile(r"^\d+\|")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svgz", ".tif", ".tiff"}


def _binary_note(path: Path, head: bytes) -> str:
    """One line about a binary file instead of its bytes."""
    try:
        size = path.stat().st_size
    except OSError:
        size = len(head)
    kind = path.suffix.lower().lstrip(".") or "binary"
    detail = ""
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        detail = f", {int.from_bytes(head[16:20], 'big')}x{int.from_bytes(head[20:24], 'big')}"
    elif head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
        detail = f", {int.from_bytes(head[6:8], 'little')}x{int.from_bytes(head[8:10], 'little')}"
    what = "image" if path.suffix.lower() in _IMAGE_SUFFIXES or detail else "binary file"
    return f"{what} ({kind}{detail}, {_size_text(size)}); contents not shown"


def _size_text(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _read_one(raw: str, start: int, limit: int | None, explicit: bool, budget: int, force: bool, ctx: ToolContext) -> str:
    path = resolve(ctx.workspace, raw)
    name = rel(ctx.workspace, path)
    if path.is_dir():
        rows = list(_tree(ctx.workspace, path, 1, None))
        listing = "\n".join(rows[:100]) or "(empty)"
        if len(rows) > 100:
            listing += f"\n... {len(rows) - 100} more; use list_files with a glob"
        return f"--- {name}/ (a folder; its entries) ---\n{listing}"
    if not path.is_file():
        return f"--- {name} ---\nnot found: {raw}{_path_hint(ctx, raw)}"
    try:
        with open(path, "rb") as handle:
            head = handle.read(8192)
    except OSError as exc:
        return f"--- {name} ---\n{exc}"
    if looks_binary_path(str(path)) or looks_binary_bytes(head):
        return f"--- {name} ---\n{_binary_note(path, head)}"
    loaded = agent_edit.load_text(path)
    lines = loaded.text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    total = len(lines)
    version = agent_edit.file_version(path)
    window = min(limit if limit is not None else DEFAULT_READ_LINES, MAX_READ_LINES)
    capped_note = ""
    if limit is not None and limit > MAX_READ_LINES:
        capped_note = f" (limit capped at {MAX_READ_LINES})"
    end = min(total, start + window - 1)
    state = ctx.state
    if state is not None and not force and total:
        seen = state.seen_span(name, version, start, end)
        if seen is not None:
            return (
                f"--- {name} ---\nlines {start}-{end} are unchanged since step {seen}; "
                "use that text. Pass force true if you no longer have it."
            )
    eol = "crlf" if loaded.eol == "\r\n" else "lf"
    header = f"--- {name} (lines {start}-{end} of {total}, eol {eol}, version {version}) ---"
    out: list[str] = [header]
    if not explicit and total > OUTLINE_MIN_LINES and ctx.index_path is not None:
        items = code_index.outline(ctx.index_path, name, limit=80)
        if items:
            out.append("outline (line-end kind name):")
            out.extend(f"  {item.line}-{item.end_line} {item.kind} {item.qualified}" for item in items)
            out.append("first lines:")
    numbered: list[str] = []
    used = 0
    for index in range(start - 1, end):
        piece = f"{index + 1}|{lines[index]}"
        if numbered and used + len(piece) + 1 > budget:
            break
        numbered.append(piece)
        used += len(piece) + 1
    if start > total and total:
        out.append(f"offset {start} is past the end; the file has {total} lines")
    elif total == 0:
        out.append("(empty file)")
    out.extend(numbered)
    shown_end = start + len(numbered) - 1
    if numbered and state is not None:
        state.note_read(name, version, start, shown_end)
    if numbered and shown_end < total:
        out.append(
            f"... {total} lines; next offset={shown_end + 1} limit={DEFAULT_READ_LINES}{capped_note}. "
            "Do not edit a span you have not seen."
        )
    return "\n".join(out)


def _read_symbol(symbol: str, raw_path: Any, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.index_path is None:
        return err("read_files", "symbol reads need the index; run crit in this project, or pass paths and offset")
    hit: code_index.Symbol | None = None
    if isinstance(raw_path, str) and raw_path.strip():
        name = rel(ctx.workspace, resolve(ctx.workspace, raw_path))
        hit = code_index.symbol_at(ctx.index_path, name, symbol)
    if hit is None:
        short = symbol.rpartition(".")[2]
        pattern = "^" + re.escape(short) + "$"
        found = code_index.find_symbols(ctx.index_path, pattern, limit=20)
        if "." in symbol:
            parent = symbol.rpartition(".")[0].rpartition(".")[2]
            narrowed = [item for item in found if item.parent == parent]
            found = narrowed or found
        if len(found) > 1:
            listing = "\n".join(
                f"{item.path}:{item.line}-{item.end_line} {item.kind} {item.qualified}" for item in found[:15]
            )
            return err("read_files", f"symbol {symbol} is defined {len(found)} times; pass path", output=listing)
        hit = found[0] if found else None
    if hit is None:
        close = code_index.close_symbols(ctx.index_path, symbol.rpartition(".")[2])
        hint = f"; did you mean {', '.join(close)}" if close else ""
        return err("read_files", f"symbol not found: {symbol}{hint}")
    start = max(1, hit.line - 2)
    count = min(MAX_READ_LINES, hit.end_line - start + 1)
    text = _read_one(hit.path, start, count, True, MAX_READ_CHARS, bool(args.get("force")), ctx)
    return ok("read_files", f"{hit.kind} {hit.qualified} at {hit.path}:{hit.line}-{hit.end_line}\n{text}", ctx)


# --------------------------------------------------------------------------- search


def _search_code(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pattern = args.get("pattern") or args.get("query")
    if not isinstance(pattern, str) or not pattern:
        return err("search_code", "pattern is required")
    raw_path = args.get("path") or "."
    if not isinstance(raw_path, str):
        return err("search_code", "path must be a string")
    root = resolve(ctx.workspace, raw_path)
    if not root.exists():
        return err("search_code", f"not found: {raw_path}")
    glob = args.get("glob")
    if glob is not None and not isinstance(glob, str):
        return err("search_code", "glob must be a string")
    try:
        limit = min(max(int(args.get("head_limit") or args.get("limit") or 50), 1), 200)
        context = min(max(int(args.get("context") or 0), 0), 3)
    except (TypeError, ValueError):
        return err("search_code", "head_limit and context must be integers")
    literal = bool(args.get("literal") or args.get("fixed_strings"))
    notes: list[str] = []
    source = re.escape(pattern) if literal else pattern
    case = args.get("case_insensitive")
    ignore_case = bool(case) if case is not None else pattern == pattern.lower()
    try:
        matcher = re.compile(source, re.IGNORECASE if ignore_case else 0)
    except re.error:
        source = re.escape(pattern)
        literal = True
        matcher = re.compile(source, re.IGNORECASE if ignore_case else 0)
        notes.append("pattern was not a valid regular expression; searched it as literal text")
    prefix = "" if root == ctx.workspace else rel(ctx.workspace, root)
    lines: list[str] = []
    seen: set[tuple[str, int]] = set()
    if ctx.index_path is not None and (not literal or re.fullmatch(r"[\w.]+", pattern)):
        symbol_pattern = re.escape(pattern) if literal or risky_regex(pattern) else pattern
        for item in code_index.find_symbols(ctx.index_path, symbol_pattern, limit=min(limit, 20), path_prefix=prefix):
            if glob and not glob_match(item.path, glob):
                continue
            signature = _one_line(item.signature, SNIPPET_CHARS)
            lines.append(f"{item.path}:{item.line}: {item.kind} {item.qualified} (lines {item.line}-{item.end_line}) {signature}".rstrip())
            seen.add((item.path, item.line))
    hits, total, files_hit = _text_hits(ctx, root, prefix, source, matcher, ignore_case, glob, literal, pattern, notes)
    touched = set(ctx.state.reads) | set(ctx.state.edits) if ctx.state else set()
    order = sorted(hits, key=lambda item: (item not in touched, item.count("/"), item))
    shown = 0
    for path in order:
        for line_no, text in hits[path][:HITS_PER_FILE]:
            if (path, line_no) in seen:
                continue
            if len(lines) >= limit:
                break
            lines.append(f"{path}:{line_no}: {_one_line(text, SNIPPET_CHARS)}")
            shown += 1
            if context:
                lines.extend(_context_lines(ctx.workspace / path, line_no, context))
    remaining = total - shown
    if remaining > 0:
        notes.append(f"{remaining} more hits in {files_hit} files; narrow with path, glob, or a longer pattern")
    body = "\n".join(lines) if lines else "(no matches)"
    if notes:
        body += "\n" + "\n".join(notes)
    found = sum(1 for line in lines if not line.startswith("    "))
    return ok(
        "search_code",
        body,
        ctx,
        _ui(f"Found {total + len(seen)} matches" if total or seen else "No matches", lines=lines[:5] if found else None),
    )


# A group that repeats something which itself repeats, e.g. (a+)+ or (\w*\s?)*:
# Python's re can take exponential time on such patterns.
_NESTED_REPEAT_RE = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)[+*{]")


def risky_regex(pattern: str) -> bool:
    """True for nested repetition, which can hang Python's backtracking engine."""
    return bool(_NESTED_REPEAT_RE.search(pattern))


def _context_lines(path: Path, line_no: int, context: int) -> list[str]:
    try:
        text = agent_edit.load_text(path).text.split("\n")
    except OSError:
        return []
    out = []
    for number in range(max(1, line_no - context), min(len(text), line_no + context) + 1):
        if number != line_no:
            out.append(f"    {number}| {_one_line(text[number - 1], SNIPPET_CHARS)}")
    return out


def _text_hits(ctx, root, prefix, source, matcher, ignore_case, glob, literal, pattern, notes=None):
    """``{path: [(line, text)]}``, total hit count, and number of files hit.

    ripgrep when it is installed. The Python fallback searches long lines only
    in their first 2000 characters, stops after 20 seconds, and searches a
    pattern with nested repetition as literal text, since Python's regex
    engine cannot be interrupted once it starts backtracking.
    """
    notes = notes if notes is not None else []
    via_rg = _rg_hits(ctx, root, source, ignore_case, glob, literal, pattern)
    if via_rg is not None:
        return via_rg
    if not literal and risky_regex(source):
        matcher = re.compile(re.escape(pattern), re.IGNORECASE if ignore_case else 0)
        literal = True
        notes.append("pattern repeats a repeated group, which can hang; searched it as literal text. Simplify it")
    deadline = time.monotonic() + _SCAN_SECONDS
    files: list[str] | None = None
    if ctx.index_path is not None:
        needle = pattern if literal else code_index.longest_literal(pattern)
        files = code_index.candidate_files(ctx.index_path, needle, path_prefix=prefix)
    if files is None:
        files = walk_files(ctx.workspace, root)
    hits: dict[str, list[tuple[int, str]]] = {}
    total = 0
    for item in files:
        if glob and not glob_match(item, glob):
            continue
        if looks_binary_path(item):
            continue
        path = ctx.workspace / item
        try:
            if path.stat().st_size > _SCAN_MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if looks_binary_bytes(data[:8192]):
            continue
        if time.monotonic() > deadline:
            notes.append(f"search stopped after {_SCAN_SECONDS:.0f}s; narrow with path or glob")
            break
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), start=1):
            if matcher.search(line[:_SCAN_LINE_CHARS]) is None:
                continue
            total += 1
            bucket = hits.setdefault(item, [])
            if len(bucket) < HITS_PER_FILE:
                bucket.append((number, line.strip()))
        if total > 5_000:
            break
    return hits, total, len(hits)


def _rg_hits(ctx, root, source, ignore_case, glob, literal, pattern):
    exe = shutil.which("rg")
    if exe is None or ctx.runner is not None:
        return None
    argv = [exe, "--json", "--hidden", "--max-columns", "400", "--max-filesize", "1M"]
    if ignore_case:
        argv.append("-i")
    if literal:
        argv += ["-F", "-e", pattern]
    else:
        argv += ["-e", source]
    for name in sorted(SKIP_DIR_NAMES):
        argv += ["-g", f"!{name}/"]
    if glob:
        argv += ["-g", glob]
    target = rel(ctx.workspace, root) if root != ctx.workspace else "."
    argv += ["--", target]
    try:
        proc = subprocess.run(argv, cwd=str(ctx.workspace), capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        return None
    hits: dict[str, list[tuple[int, str]]] = {}
    total = 0
    for raw in proc.stdout.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data", {})
        path_text = (data.get("path") or {}).get("text", "")
        if not path_text:
            continue
        item = rel(ctx.workspace, ctx.workspace / path_text)
        text = (data.get("lines") or {}).get("text", "")
        total += 1
        bucket = hits.setdefault(item, [])
        if len(bucket) < HITS_PER_FILE:
            bucket.append((int(data.get("line_number") or 0), text.strip()))
    return hits, total, len(hits)


# --------------------------------------------------------------------------- write / edit / delete


def _after_write(ctx: ToolContext, path: Path) -> str:
    name = rel(ctx.workspace, path)
    version = agent_edit.file_version(path)
    if ctx.state is not None:
        ctx.state.note_write(name, version)
    with _INDEX_LOCK:  # helper tabs write at the same time; the index and graph are shared
        if ctx.index_path is not None:
            refresh_path(ctx.workspace, ctx.index_path, path)
        try:
            code_graph.sync_after_edit(ctx.workspace)
        except Exception:
            pass
    return version


_INDEX_LOCK = threading.Lock()


def _guard_target(ctx: ToolContext, path: Path, raw: str) -> str:
    """Why a mutating tool must not touch ``path``, or an empty string.

    A path outside the workspace is allowed here: :func:`permission_for`
    marks it "outside", the user approved it, and the checkpoint saves it by
    its absolute path so undo restores it.
    """
    if path == ctx.workspace:
        return "refusing to change the workspace root"
    if path.is_dir():
        return f"path is a directory: {raw}"
    if inside(ctx.workspace, path) and ".git" in Path(rel(ctx.workspace, path)).parts:
        return f"refusing to change files inside .git: {raw}"
    return ""


def _write_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    files = args.get("files")
    if files is None and args.get("path") is not None:
        files = [{"path": args.get("path"), "contents": args.get("contents", args.get("content", ""))}]
    if not isinstance(files, list) or not files:
        return err("write_files", 'files must be a list of {"path", "contents"}, or pass path and contents')
    overwrite = bool(args.get("overwrite"))
    written: list[str] = []
    errors: list[str] = []
    diffs: list[str] = []
    titles: list[str] = []
    added = removed = 0
    for item in files:
        if not isinstance(item, dict):
            errors.append("each file must be an object")
            continue
        raw = item.get("path")
        contents = item.get("contents", item.get("content"))
        if not isinstance(raw, str) or not raw:
            errors.append("file path is required")
            continue
        if not isinstance(contents, str):
            errors.append(f"contents must be a string: {raw}")
            continue
        path = resolve(ctx.workspace, raw)
        name = rel(ctx.workspace, path)
        problem = _guard_target(ctx, path, raw)
        if problem:
            errors.append(problem)
            continue
        existed = path.is_file()
        if (
            existed
            and ctx.state is not None
            and not (overwrite or item.get("overwrite") is True)
            and name not in ctx.state.reads
            and name not in ctx.state.edits
        ):
            errors.append(
                f"{name} already exists and was not read in this task. Read it and use edit_file, "
                "or pass overwrite true to replace the whole file"
            )
            continue
        like = agent_edit.load_text(path) if existed else None
        before = like.text if like is not None else ""
        if REDACTED in contents and REDACTED not in before:
            errors.append(
                f"{name} not written: the contents hold a {REDACTED}...] marker, which stands for a secret the chat "
                "never sees. Edit the other lines with edit_file instead of rewriting the file"
            )
            continue
        if existed and Path(name).suffix.lower() in agent_edit.STRICT_SUFFIXES:
            new_check = agent_edit.syntax_check(name, contents)
            if new_check.startswith("error") and agent_edit.syntax_check(name, before) == "ok":
                errors.append(f"{name} not replaced: the new contents have a syntax {new_check}")
                continue
        if ctx.checkpoints is not None:
            ctx.checkpoints.save(path)
        try:
            agent_edit.save_text(path, contents, like)
        except (ValueError, OSError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        _after_write(ctx, path)
        line_count = contents.count("\n") + (0 if contents.endswith("\n") or not contents else 1)
        summary = f"{'replaced' if existed else 'created'} {name} ({line_count} lines)"
        check = agent_edit.syntax_check(name, contents)
        if check:
            summary += f"; syntax {check}"
        diff = agent_edit.hunk_diff(before, contents.replace("\r\n", "\n"), name)
        if existed and diff:
            summary += "\n" + diff
        plus, minus = _diff_counts(diff)
        added += plus
        removed += minus
        if diff:
            diffs.append(diff)
        titles.append(f"{'Updated' if existed else 'Created'} {name}" + (f" (+{plus} -{minus})" if existed else f" ({line_count} lines)"))
        written.append(summary)
    output = "\n".join(written) if written else "wrote nothing"
    if len(titles) == 1:
        title = titles[0]
    else:
        title = f"Wrote {len(titles)} files (+{added} -{removed})"
    ui = _ui(title, "\n".join(diffs)) if titles else None
    if errors:
        if ui is not None:
            ui["summary"] += "; " + _one_line("; ".join(errors), 100)
        return err("write_files", "; ".join(errors), output=output, ctx=ctx, ui=ui)
    return ok("write_files", output, ctx, ui)


def _edit_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw = args.get("path") or args.get("file")
    if not isinstance(raw, str) or not raw:
        return err("edit_file", "path is required")
    edits = args.get("edits")
    if edits is None:
        edits = [
            {
                "old_string": args.get("old_string", args.get("old")),
                "new_string": args.get("new_string", args.get("new")),
                "replace_all": args.get("replace_all"),
            }
        ]
    if isinstance(edits, dict):
        edits = [_normalize_edit(edits)]
    if not isinstance(edits, list) or not edits:
        return err("edit_file", 'edits must be a list of {"old_string", "new_string"}')
    path = resolve(ctx.workspace, raw)
    name = rel(ctx.workspace, path)
    if not path.is_file():
        return err(
            "edit_file",
            f"not found: {raw}. Use write_files to create a new file{_path_hint(ctx, raw)}",
        )
    problem = _guard_target(ctx, path, raw)
    if problem:
        return err("edit_file", problem)
    expected = args.get("version")
    if isinstance(expected, str) and expected and expected != agent_edit.file_version(path):
        return err("edit_file", f"{name} changed since you read it (version {expected}); read it again")
    loaded = agent_edit.load_text(path)
    text = loaded.text
    notes: list[str] = []
    total = 0
    for number, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            return err("edit_file", f"edit {number} must be an object")
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(old, str) or old == "":
            return err("edit_file", f"edit {number}: old_string is empty. Use write_files for a new file")
        if not isinstance(new, str):
            return err("edit_file", f"edit {number}: new_string must be a string")
        result = agent_edit.replace_span(text, old, new, replace_all=bool(edit.get("replace_all")))
        if result.text is None:
            label = f"edit {number} of {len(edits)}: " if len(edits) > 1 else ""
            output = ""
            if result.candidate:
                output = (
                    "closest text in the file. Copy old_string from it exactly, without the N| prefix:\n"
                    + result.candidate
                )
            suffix = "; no edit was applied" if len(edits) > 1 else ""
            return err("edit_file", label + result.note + suffix, output=output, ctx=ctx)
        text = result.text
        total += result.count
        if result.note:
            notes.append(result.note)
    if REDACTED in text and REDACTED not in loaded.text:
        return err(
            "edit_file",
            f"edit not applied: it would write a {REDACTED}...] marker into {name}. That marker stands for a secret "
            "the chat never sees; leave the lines that hold it unchanged",
        )
    before_check = agent_edit.syntax_check(name, loaded.text)
    after_check = agent_edit.syntax_check(name, text)
    diff = agent_edit.hunk_diff(loaded.text, text, name)
    strict = Path(name).suffix.lower() in agent_edit.STRICT_SUFFIXES
    if strict and before_check == "ok" and after_check.startswith("error"):
        return err(
            "edit_file",
            f"edit not applied: it would leave {name} with a syntax {after_check}",
            output=diff,
            ctx=ctx,
        )
    if ctx.checkpoints is not None:
        ctx.checkpoints.save(path)
    try:
        agent_edit.save_text(path, text, loaded)
    except ValueError as exc:
        return err("edit_file", f"edit not applied: {exc}")
    version = _after_write(ctx, path)
    message = f"updated {name} ({total})"
    if notes:
        message += "; " + "; ".join(dict.fromkeys(notes))
    if after_check:
        message += f"; syntax {after_check}"
    message += f"; version {version}"
    if diff:
        message += "\n" + diff
    plus, minus = _diff_counts(diff)
    return ok("edit_file", message, ctx, _ui(f"Updated {name} (+{plus} -{minus})", diff))


def _delete_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw = args.get("path")
    if not isinstance(raw, str) or not raw:
        return err("delete_file", "path is required")
    link = resolve(ctx.workspace, raw, follow=False)
    if link.is_symlink():
        return err("delete_file", f"{raw} is a symbolic link; remove links with run_command")
    path = resolve(ctx.workspace, raw)
    problem = _guard_target(ctx, path, raw)
    if problem:
        return err("delete_file", problem.replace("change", "delete", 1))
    if not path.is_file():
        return err("delete_file", f"not found: {raw}" + _path_hint(ctx, raw))
    if ctx.checkpoints is not None:
        ctx.checkpoints.save(path)
    path.unlink()
    _after_write(ctx, path)
    name = rel(ctx.workspace, path)
    return ok("delete_file", f"deleted {name}", ctx, _ui(f"Deleted {name}"))


def _move_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw = args.get("path")
    raw_dest = args.get("destination")
    if not isinstance(raw, str) or not raw.strip():
        return err("move_file", "path (the file to move) is required")
    if not isinstance(raw_dest, str) or not raw_dest.strip():
        return err("move_file", "destination is required, for example src/new_name.py")
    if resolve(ctx.workspace, raw, follow=False).is_symlink():
        return err("move_file", f"{raw} is a symbolic link; move links with run_command")
    source = resolve(ctx.workspace, raw)
    if source.is_dir():
        return err("move_file", f"{raw} is a folder; move_file moves one file. Use run_command (git mv) for folders")
    if not source.is_file():
        return err("move_file", f"not found: {raw}" + _path_hint(ctx, raw))
    problem = _guard_target(ctx, source, raw)
    if problem:
        return err("move_file", problem)
    dest = resolve(ctx.workspace, raw_dest)
    if dest.is_dir() or clean_path(raw_dest).endswith(("/", "\\")) or str(raw_dest).rstrip().endswith(("/", "\\")):
        dest = dest / source.name
    problem = _guard_target(ctx, dest, raw_dest)
    if problem:
        return err("move_file", problem)
    if dest == source:
        return err("move_file", "path and destination are the same file")
    overwrite = bool(args.get("overwrite"))
    if (dest.exists() or dest.is_symlink()) and not overwrite:
        return err("move_file", f"{rel(ctx.workspace, dest)} already exists; pass overwrite true to replace it")
    if ctx.checkpoints is not None:
        ctx.checkpoints.save(source)
        ctx.checkpoints.save(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, dest)
    except OSError:
        shutil.move(str(source), str(dest))
    _after_write(ctx, source)
    _after_write(ctx, dest)
    left, right = rel(ctx.workspace, source), rel(ctx.workspace, dest)
    return ok("move_file", f"moved {left} -> {right}", ctx, _ui(f"Moved {left} → {right}"))


def _patch_text(args: dict[str, Any]) -> Any:
    """The diff an apply_patch call carries, under any of the names models use for it."""
    return args.get("patch") or args.get("diff") or args.get("input")


def _apply_patch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    patch = _patch_text(args)
    if not isinstance(patch, str) or not patch.strip():
        return err("apply_patch", "patch is required: one unified diff as text")
    patch = patch.replace("\r\n", "\n")
    if not patch.endswith("\n"):
        patch += "\n"
    folder = ctx.cache_dir or Path(tempfile.mkdtemp(prefix="bot-patch-"))
    folder.mkdir(parents=True, exist_ok=True)
    patch_path = folder / "apply.patch"
    patch_path.write_text(patch, encoding="utf-8")
    prefixed = _patch_has_prefixes(patch)
    paths = patched_paths(patch)
    strip = [] if prefixed else ["-p0"]
    attempts = (
        ["apply", "--whitespace=nowarn", *strip],
        ["apply", "--whitespace=nowarn", *strip, "--ignore-whitespace", "--recount"],
    )
    check_error = ""
    for flags in attempts:
        checked = _git(ctx, [*flags, "--check", str(patch_path)], tool="apply_patch")
        if checked.get("ok"):
            break
        check_error = checked.get("error") or checked.get("output") or ""
    else:
        return err(
            "apply_patch",
            "patch does not apply: " + _one_line(check_error, 400)
            + ". Read the file again and fix the context lines, or use edit_file",
        )
    if ctx.checkpoints is not None:
        for item in paths:
            ctx.checkpoints.save(resolve(ctx.workspace, item))
    result = _git(ctx, [*flags, str(patch_path)], tool="apply_patch")
    if result.get("ok"):
        checks = []
        for item in paths:
            target = resolve(ctx.workspace, item)
            _after_write(ctx, target)
            if target.is_file():
                check = agent_edit.syntax_check(item, agent_edit.load_text(target).text)
                if check:
                    checks.append(f"{item}: syntax {check}")
        if checks or paths:
            result["output"] = (
                "applied to " + ", ".join(paths) + ("\n" + "\n".join(checks) if checks else "")
            )
        plus, minus = _diff_counts(patch)
        shown = patch if len(patch) <= 2_500 else patch[:2_500] + "\n... diff truncated"
        result["ui"] = _ui(f"Patched {', '.join(paths[:3])}" + (" ..." if len(paths) > 3 else "") + f" (+{plus} -{minus})", shown)
    return result


def _patch_has_prefixes(patch: str) -> bool:
    """True for git-style ``a/`` ``b/`` paths (``git apply`` strips one component)."""
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            return True
        if line.startswith(("--- ", "+++ ")):
            raw = _patch_path(line[4:])
            if raw == "/dev/null":
                continue
            return raw.startswith(("a/", "b/"))
    return True


def _patch_path(text: str) -> str:
    raw = text.split("\t", 1)[0].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = raw[1:-1]
    return raw


def patched_paths(patch: str) -> list[str]:
    """Every path a patch touches: changed, created, deleted, and both sides of a rename."""
    prefixed = _patch_has_prefixes(patch)
    paths: list[str] = []

    def add(raw: str, prefix: str) -> None:
        if not raw or raw == "/dev/null":
            return
        if prefixed and raw.startswith(prefix):
            raw = raw[len(prefix) :]
        if raw not in paths:
            paths.append(raw)

    for line in patch.splitlines():
        if line.startswith("--- "):
            add(_patch_path(line[4:]), "a/")
        elif line.startswith("+++ "):
            add(_patch_path(line[4:]), "b/")
        elif line.startswith(("rename from ", "copy from ")):
            add(line.split(" ", 2)[2].strip(), "")
        elif line.startswith(("rename to ", "copy to ")):
            add(line.split(" ", 2)[2].strip(), "")
    return paths


# --------------------------------------------------------------------------- commands and git


def _run_command(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = args.get("command") or args.get("cmd")
    if not isinstance(command, str) or not command.strip():
        return err("run_command", "command is required")
    try:
        timeout = float(args.get("timeout") or ctx.command_timeout)
    except (TypeError, ValueError):
        return err("run_command", "timeout must be a number")
    timeout = min(max(timeout, 1), MAX_COMMAND_TIMEOUT)
    if agent_build.is_build_command(command):
        # A first Gradle or AOSP build downloads and compiles for a long time; a short
        # timeout killed it half way and left locks behind.
        floor = min(max(float(ctx.build_timeout or 0), 1.0), agent_build.MAX_BUILD_TIMEOUT)
        timeout = max(timeout, floor)
    wanted = _shell_arg(args.get("shell"))
    if wanted is None:
        return err("run_command", "shell must be one of " + ", ".join(SHELL_NAMES) + ", or omitted for the session shell")
    background = args.get("background")
    if background is not None and not isinstance(background, bool):
        return err("run_command", "background must be true or false")
    reach = network_command(command)
    if not reach:
        current = Path(getattr(ctx.session, "cwd", ctx.workspace) or ctx.workspace)
        script = network_script(command, ctx.workspace, current)
        if script:
            reach = f"runs {script}, which opens network connections"
    if reach and str(ctx.network_commands or "block").lower() != "ask":
        return err(
            "run_command",
            f"not run: this command {reach}, and nothing may leave this machine except the chat. "
            "Read documentation with web_fetch; builds may still download their dependencies",
        )
    if ctx.session is None:
        return _run_plain(args, ctx, command, timeout, wanted, bool(background))
    return _run_in_session(args, ctx, command, timeout, wanted, bool(background))


def _session_cwd(ctx: ToolContext, raw: Any) -> tuple[Path | None, str]:
    """The folder a call starts in: relative to the session's cwd first, then the workspace."""
    if not isinstance(raw, str) or not raw.strip():
        return None, ""
    current = Path(getattr(ctx.session, "cwd", ctx.workspace) or ctx.workspace)
    text = clean_path(raw)
    if not Path(text).is_absolute() and text[:1] not in "/\\":
        candidate = Path(os.path.abspath(current / text))
        if candidate.is_dir():
            return candidate, ""
    path = resolve(ctx.workspace, raw)
    if not path.is_dir():
        return None, f"cwd is not a folder: {raw}" + _path_hint(ctx, raw)
    return path, ""


def _run_in_session(
    args: dict[str, Any], ctx: ToolContext, command: str, timeout: float, wanted: str, background: bool
) -> dict[str, Any]:
    session = ctx.session
    try:
        shell = session.resolve(wanted or None)
    except ValueError as exc:
        return err("run_command", str(exc))
    problem = agent_shell.lint_command(command, shell, getattr(session, "available", None))
    if problem:
        return err("run_command", problem)
    cwd, problem = _session_cwd(ctx, args.get("cwd"))
    if problem:
        return err("run_command", problem)
    shell_name = getattr(shell, "name", shell.kind)
    if background:
        try:
            job_id = session.start_background(command, shell=wanted or None, cwd=cwd)
            first, running, code = session.read_background(job_id, wait=1.0)
        except (ValueError, KeyError, OSError) as exc:
            return err("run_command", f"could not start it: {exc}")
        state = "running" if running else f"already exited ({'exit ' + str(code) if code is not None else 'killed'})"
        body = (
            f"started background job {job_id} ({shell_name}): {state}\n"
            + (first.strip() or "(no output yet)")
            + f"\nRead more with command_output {{\"job_id\": \"{job_id}\"}}; stop it with kill_command."
        )
        if ctx.state is not None:
            ctx.state.last_command = command
            ctx.state.last_exit = f"background {job_id}"
        return ok(
            "run_command",
            agent_shell.head_tail(body, max_chars=ctx.max_chars),
            ctx,
            _ui(f"Started {job_id} in background ({shell_name})", lines=_preview(first, tail=True)),
        )
    before = Path(session.cwd)
    started_wall = time.time()

    def once() -> Any:
        return session.run(
            command,
            timeout=timeout,
            shell=wanted or None,
            cwd=cwd,
            on_output=ctx.on_output,
            cancel=ctx.cancel,
        )

    try:
        result = once()
    except ValueError as exc:
        return err("run_command", str(exc))
    retried: list[str] = []
    for _attempt in range(max(0, int(ctx.command_retries or 0))):
        if result.exit_code in (0, None) or result.timed_out or result.interrupted:
            break
        transient = agent_build.transient_failure(command, result.stdout + "\n" + result.stderr)
        if transient is None:
            break
        reason, stop_first = transient
        retried.append(reason)
        if hasattr(session, "note_retry"):
            session.note_retry(reason)
        if ctx.on_output is not None:
            ctx.on_output(f"\n[crit] {reason}; trying once more\n")
        if stop_first:
            wrapper = agent_build.gradle_wrapper(Path(cwd or session.cwd), windows=_windows_shell(shell))
            if wrapper:
                try:
                    session.run(f"{wrapper} --stop", timeout=120, shell=wanted or None, cwd=cwd)
                except ValueError:
                    pass
        else:
            time.sleep(5)
        try:
            result = once()
        except ValueError as exc:
            return err("run_command", str(exc))
    ended = Path(result.cwd)
    if cwd is not None and _same_dir(ended, cwd):
        # A cwd argument is for this call only (OpenCode's workdir); a cd inside the command still sticks.
        session.cwd = before
    out = result.stdout
    errs = result.stderr
    code = result.exit_code
    seconds = result.seconds
    if ctx.state is not None:
        ctx.state.last_command = command
        ctx.state.last_exit = (
            "interrupted" if result.interrupted
            else f"still running as {result.job_id}" if getattr(result, "job_id", None)
            else "timeout" if result.timed_out else f"exit {code}"
        )
        if hasattr(ctx.state, "commands"):
            ctx.state.commands += 1
            if code not in (0, None) or result.timed_out:
                ctx.state.commands_failed += 1
    head = [f"exit {code if code is not None else '-'} ({seconds:.1f}s)"]
    if wanted:
        head.append(f"shell: {shell.label}")
    if retried:
        head.append("retried once: " + "; ".join(retried))
    moved = not _same_dir(session.cwd, before)
    if moved:
        head.append(f"cwd: {session.cwd}")
    elif cwd is not None:
        head.append(f"ran in: {ended} (session cwd stays {before})")
    combined = out + "\n" + errs
    preview = _preview(combined, tail=True)
    if result.interrupted:
        body = "\n".join(part for part in ("interrupted by the user", *head[1:], out, errs) if part)
        return err(
            "run_command",
            "interrupted by the user; the command was stopped",
            output=agent_shell.head_tail(body, max_chars=ctx.max_chars),
            ctx=ctx,
            ui=_ui(f"interrupted · {seconds:.1f}s · {shell_name}", lines=preview),
        )
    job_id = getattr(result, "job_id", None)
    if job_id:
        note = (
            f"still running after {timeout:.0f}s, so it was NOT stopped: it continues as background job {job_id}. "
            f'Wait for it with command_output {{"job_id": "{job_id}", "wait": 30}} (repeat until it exits) '
            "and read its result there; stop it with kill_command only if it is stuck"
        )
        body = "\n".join(part for part in (note, *head[1:], out, errs) if part)
        return ok(
            "run_command",
            agent_shell.head_tail(body, max_chars=ctx.max_chars),
            ctx,
            _ui(f"still running after {timeout:.0f}s · continues as {job_id}", lines=preview),
        )
    if result.timed_out or code is None:
        hint = ""
        if agent_shell.waiting_for_input(combined):
            hint = "; the command was waiting for input. Pass a flag such as -y, --yes, or -Force"
        else:
            hint = "; pass a longer timeout, or background true for a server or watcher"
        body = "\n".join(part for part in (f"timed out after {timeout:.0f}s", *head[1:], out, errs) if part)
        return err(
            "run_command",
            f"timed out after {timeout:.0f}s{hint}",
            output=agent_shell.head_tail(body, max_chars=ctx.max_chars),
            ctx=ctx,
            ui=_ui(f"timed out after {timeout:.0f}s · {shell_name}", lines=preview),
        )
    parts = list(head)
    summary_lines = agent_build.summarize(combined) if agent_build.is_build_command(command) or code != 0 else []
    built = agent_build.artifacts(Path(session.cwd), since=started_wall) if code == 0 and agent_build.is_build_command(command) else []
    if built:
        parts.append("ARTIFACTS (written by this command):\n" + "\n".join(
            f"  {path} ({agent_build.size_text(size)})" for path, size in built
        ))
    if summary_lines and (code != 0 or len(combined) > 6_000):
        parts.append("SUMMARY (the key lines of the full output below):\n" + "\n".join(summary_lines))
    if code != 0:
        found = agent_build.hints(combined, _build_context(ctx, session, shell))
        if found:
            parts.append("HINTS (from crit, for this failure):\n" + "\n".join(f"- {line}" for line in found))
    if out:
        parts.append(out)
    if errs:
        parts.append(errs)
    if not out and not errs:
        parts.append("(no output)")
    body = agent_shell.head_tail("\n".join(parts), max_chars=ctx.max_chars)
    summary = f"exit {code} · {_duration(seconds)} · {shell_name}" + (f" · cwd {rel(ctx.workspace, session.cwd)}" if moved else "")
    if retried:
        summary += " · retried once"
    ui_lines = preview
    if built:
        ui_lines = [f"{path} ({agent_build.size_text(size)})" for path, size in built[:3]]
    elif summary_lines and code != 0:
        ui_lines = summary_lines[:5]
    ui = _ui(summary, lines=ui_lines)
    if code == 0:
        return ok("run_command", body, ctx, ui)
    return err("run_command", f"command failed with exit {code}", output=body, ctx=ctx, ui=ui)


def _duration(seconds: float) -> str:
    if seconds >= 60:
        minutes, rest = divmod(int(round(seconds)), 60)
        return f"{minutes}m {rest:02d}s"
    return f"{seconds:.1f}s"


def _windows_shell(shell: Any) -> bool:
    return bool(getattr(shell, "is_powershell", False) or getattr(shell, "kind", "") in {"cmd", "gitbash"}) and (
        sys.platform == "win32" or str(getattr(shell, "exe", "")).lower().endswith(".exe")
    )


def _build_context(ctx: ToolContext, session: Any, shell: Any) -> agent_build.Context:
    windows = _windows_shell(shell)
    try:
        env = session.environment() if hasattr(session, "environment") else dict(os.environ)
    except Exception:  # noqa: BLE001
        env = dict(os.environ)
    try:
        jdks = tuple((jdk.version, str(jdk.home)) for jdk in agent_shell.installed_jdks())
    except Exception:  # noqa: BLE001
        jdks = ()
    sdk = ""
    try:
        sdk = agent_shell.android_sdk(env, scan=True, workspace=ctx.workspace)
    except Exception:  # noqa: BLE001
        pass
    return agent_build.Context(
        windows=windows,
        wrapper=agent_build.gradle_wrapper(Path(getattr(session, "cwd", ctx.workspace)), windows=windows)
        or agent_build.gradle_wrapper(ctx.workspace, windows=windows),
        android_sdk=sdk,
        jdks=jdks,
        java_home=str(env.get("JAVA_HOME") or ""),
    )


def _same_dir(left: Any, right: Any) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return str(left) == str(right)


def _run_plain(args: dict[str, Any], ctx: ToolContext, command: str, timeout: float, wanted: str, background: bool) -> dict[str, Any]:
    """One process per call (no ShellSession): the runner= fakes and old callers."""
    shell = ctx.shell or agent_shell.detect_shell()
    if wanted and not _same_shell(wanted, shell):
        return err(
            "run_command",
            f"shell {wanted} is not available in this session; the shell is {shell.label}. Omit shell",
        )
    if background:
        return err(
            "run_command",
            "background commands are not available in this session; run it in the foreground with a timeout",
        )
    problem = agent_shell.lint_command(command, shell)
    if problem:
        return err("run_command", problem)
    cwd = ctx.workspace
    raw_cwd = args.get("cwd")
    if isinstance(raw_cwd, str) and raw_cwd.strip():
        cwd = resolve(ctx.workspace, raw_cwd)
        if not cwd.is_dir():
            return err("run_command", f"cwd is not a folder: {raw_cwd}" + _path_hint(ctx, raw_cwd))
    extra = {} if ctx.runner is not None else {"on_output": ctx.on_output, "cancel": ctx.cancel}
    code, stdout, stderr, seconds = agent_shell.run(
        command, cwd=cwd, timeout=timeout, shell=shell, runner=ctx.runner, **extra
    )
    out = agent_shell.tidy(stdout)
    errs = agent_shell.tidy(stderr)
    if ctx.state is not None:
        ctx.state.last_command = command
        ctx.state.last_exit = "timeout" if code is None else f"exit {code}"
    if code is None:
        body = "\n".join(part for part in (f"timed out after {timeout:.0f}s", f"cwd: {cwd}", out, errs) if part)
        hint = ""
        if agent_shell.waiting_for_input(out + "\n" + errs):
            hint = "; the command was waiting for input. Pass a flag such as -y, --yes, or -Force"
        return err(
            "run_command",
            f"timed out after {timeout:.0f}s{hint}",
            output=agent_shell.head_tail(body, max_chars=ctx.max_chars),
            ctx=ctx,
            ui=_ui(f"timed out after {timeout:.0f}s", lines=_preview(out + "\n" + errs, tail=True)),
        )
    parts = [f"exit {code} ({seconds:.1f}s)", f"cwd: {cwd}"]
    if out:
        parts.append(out)
    if errs:
        parts.append(errs)
    body = agent_shell.head_tail("\n".join(parts), max_chars=ctx.max_chars)
    ui = _ui(f"exit {code} · {seconds:.1f}s", lines=_preview(out + "\n" + errs, tail=True))
    if code == 0:
        return ok("run_command", body, ctx, ui)
    return err("run_command", "command failed", output=body, ctx=ctx, ui=ui)


def _shell_arg(value: Any) -> str | None:
    """Lower-case shell name, "" for none, or None when the value is not a shell."""
    if value is None or value is False:
        return ""
    if not isinstance(value, str):
        return None
    name = value.strip().lower().removesuffix(".exe")
    name = {"git bash": "gitbash", "git-bash": "gitbash", "windows powershell": "powershell", "cmd.exe": "cmd",
            "default": "", "auto": ""}.get(name, name)
    if name and name not in SHELL_NAMES:
        return None
    return name


def _same_shell(wanted: str, shell: agent_shell.Shell) -> bool:
    kind = getattr(shell, "kind", "")
    if wanted == kind:
        return True
    return wanted == "bash" and kind == "gitbash"


def _job_id(session: Any, raw: Any) -> str:
    text = str(raw or "").strip()
    known = {str(row.get("id")) for row in session.jobs()}
    if text not in known and text.isdigit() and f"b{text}" in known:
        return f"b{text}"
    return text


def _format_jobs(rows: list[dict]) -> str:
    if not rows:
        return "no background jobs"
    out = []
    for row in rows:
        state = "running" if row.get("running") else (
            f"exit {row.get('exit_code')}" if row.get("exit_code") is not None else "stopped"
        )
        out.append(f"{row.get('id')} [{state}] {_one_line(str(row.get('command') or ''), 100)}")
    return "\n".join(out)


def _command_output(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    session = ctx.session
    if session is None:
        return err("command_output", "there are no background jobs in this session; run_command with background true starts one")
    if not args.get("job_id"):
        rows = session.jobs()
        return ok("command_output", _format_jobs(rows), ctx, _ui(f"{len(rows)} background jobs", lines=_format_jobs(rows).split("\n")[:5]))
    try:
        wait = float(args.get("wait") or 0)
    except (TypeError, ValueError):
        return err("command_output", "wait must be a number of seconds")
    wait = min(max(wait, 0.0), 30.0)
    job_id = _job_id(session, args.get("job_id"))
    try:
        text, running, code = session.read_background(job_id, wait=wait)
    except KeyError as exc:
        return err("command_output", str(exc.args[0] if exc.args else exc))
    state = "running" if running else (f"exit {code}" if code is not None else "stopped")
    body = f"{job_id}: {state}\n" + (text.strip() or "(no new output)")
    return ok(
        "command_output",
        agent_shell.head_tail(body, max_chars=ctx.max_chars),
        ctx,
        _ui(f"{job_id} {state}", lines=_preview(text, tail=True)),
    )


def _kill_command(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    session = ctx.session
    if session is None:
        return err("kill_command", "there are no background jobs in this session")
    if not args.get("job_id"):
        return err("kill_command", "job_id is required. Jobs:\n" + _format_jobs(session.jobs()))
    job_id = _job_id(session, args.get("job_id"))
    try:
        stopped = session.kill_background(job_id)
    except KeyError as exc:
        return err("kill_command", str(exc.args[0] if exc.args else exc))
    if stopped:
        return ok("kill_command", f"stopped {job_id}", ctx, _ui(f"Stopped {job_id}"))
    row = next((item for item in session.jobs() if item.get("id") == job_id), {})
    code = row.get("exit_code")
    return ok("kill_command", f"{job_id} had already exited (exit {code})", ctx, _ui(f"{job_id} had already exited"))


def _code_graph(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = args.get("query") or args.get("question") or args.get("symbol") or args.get("name")
    if not isinstance(query, str) or not query.strip():
        return err("code_graph", "query is required")
    action = args.get("action") or args.get("mode") or "explore"
    if not isinstance(action, str):
        return err("code_graph", "action must be a string")
    target = args.get("target") or args.get("to") or ""
    if not isinstance(target, str):
        return err("code_graph", "target must be a string")
    try:
        timeout = float(args.get("timeout") or 60)
    except (TypeError, ValueError):
        return err("code_graph", "timeout must be a number")
    timeout = min(max(timeout, 1), 120)
    code, text = code_graph.query(
        ctx.workspace,
        query,
        action=action,
        target=target,
        runner=ctx.runner,
        timeout=timeout,
    )
    body = agent_shell.head_tail(text, max_chars=ctx.max_chars)
    if code == 0:
        return ok("code_graph", body or "exit 0", ctx)
    if code == 127:
        return err("code_graph", body or "code graph is not installed", ctx=ctx)
    return err("code_graph", "graph query failed", output=body, ctx=ctx)


def _git_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    del args
    return _git(ctx, ["status", "--short", "--branch"], tool="git_status")


def _git_diff(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    git_args = ["diff"]
    if args.get("staged"):
        git_args.append("--cached")
    if args.get("stat"):
        git_args.append("--stat")
    raw = args.get("path")
    if raw:
        if not isinstance(raw, str):
            return err("git_diff", "path must be a string")
        git_args.extend(["--", raw])
    return _git(ctx, git_args, tool="git_diff")


def _git(ctx: ToolContext, git_args: list[str], *, tool: str) -> dict[str, Any]:
    run = ctx.runner or subprocess.run
    try:
        proc = run(
            ["git", "-c", "core.quotepath=off", "-C", str(ctx.workspace), *git_args],
            capture_output=True,
            check=False,
            shell=False,
            **({} if ctx.runner else {"env": _git_environment(), "timeout": 120}),
        )
    except FileNotFoundError:
        return err(tool, "git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        return err(tool, "git timed out")
    code = int(getattr(proc, "returncode", 1) or 0)
    stdout = agent_shell.tidy(agent_shell.decode(getattr(proc, "stdout", b"")))
    stderr = agent_shell.tidy(agent_shell.decode(getattr(proc, "stderr", b"")))
    output = "\n".join(part for part in (f"exit {code}", stdout, stderr) if part)
    if code != 0:
        return err(tool, stderr or stdout or "git failed", output=output, ctx=ctx)
    return ok(tool, output if stdout or stderr else "exit 0\n(empty)", ctx)


_SECRET_NAME_RE = re.compile(
    r"TOKEN|SECRET|PASSW|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL|AUTH(?!OR)|COOKIE|SESSION_?KEY", re.IGNORECASE
)


def _git_environment() -> dict[str, str]:
    """Environment for the tools' own git calls, without credentials."""
    scrubbed = getattr(agent_shell, "scrubbed_environment", None)
    if callable(scrubbed):
        env = dict(scrubbed())
    else:
        env = {key: value for key, value in agent_shell.environment().items() if not _SECRET_NAME_RE.search(key)}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def is_git_repo(workspace: Path) -> bool:
    return (Path(workspace) / ".git").exists()


_REV_RE = re.compile(r"^[A-Za-z0-9_./~^@{}:-]+$")
_TODO_STATUS = {"pending", "in_progress", "completed", "cancelled"}
_SKILL_ROOTS = (".bot/skills", ".agents/skills", ".opencode/skills")


def _git_log(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        limit = min(max(int(args.get("limit") or 20), 1), 50)
    except (TypeError, ValueError):
        return err("git_log", "limit must be an integer")
    git_args = ["log", f"-n{limit}", "--date=short", "--pretty=format:%h %ad %s"]
    raw = args.get("path")
    if raw:
        shown = _git_path(ctx, raw)
        if shown is None:
            return err("git_log", "path must be a file inside the workspace")
        git_args.extend(["--", shown])
    return _git(ctx, git_args, tool="git_log")


def _git_show(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    rev = args.get("rev") or "HEAD"
    if not isinstance(rev, str) or not _REV_RE.fullmatch(rev) or rev.startswith("-"):
        return err("git_show", "rev must be a commit, branch, or HEAD")
    git_args = ["show", "--stat", "--format=medium", "--no-color", rev]
    if args.get("patch"):
        git_args = ["show", "--format=medium", "--no-color", rev]
    raw = args.get("path")
    if raw:
        shown = _git_path(ctx, raw)
        if shown is None:
            return err("git_show", "path must be a file inside the workspace")
        git_args.extend(["--", shown])
    return _git(ctx, git_args, tool="git_show")


def _git_path(ctx: ToolContext, raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.strip() or raw.startswith("-") or "\n" in raw:
        return None
    path = resolve(ctx.workspace, raw)
    try:
        return path.resolve().relative_to(ctx.workspace).as_posix()
    except ValueError:
        return None


def _todo(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    items = args.get("todos")
    if items is None:
        items = args.get("items")
    if items is None:
        current = ctx.state.todos if ctx.state is not None else []
        return ok("todo", _format_todos(current), ctx)
    if not isinstance(items, list):
        return err("todo", "todos must be a list of {content, status}")
    cleaned: list[dict[str, str]] = []
    in_progress = 0
    for index, item in enumerate(items[:20]):
        if isinstance(item, str):
            item = {"content": item, "status": "pending"}
        if not isinstance(item, dict):
            return err("todo", "each todo must be an object or a string")
        content = str(item.get("content") or item.get("text") or "").strip()
        if not content:
            return err("todo", "each todo needs content")
        status = str(item.get("status") or "pending")
        if status not in _TODO_STATUS:
            return err("todo", "status must be pending, in_progress, completed, or cancelled")
        if status == "in_progress":
            in_progress += 1
        cleaned.append({"id": str(item.get("id") or index + 1), "content": content[:200], "status": status})
    if in_progress > 1:
        return err("todo", "only one todo can be in_progress")
    if ctx.state is not None:
        ctx.state.todos = cleaned
    return ok("todo", _format_todos(cleaned), ctx)


def _format_todos(items: list[dict[str, str]]) -> str:
    if not items:
        return "no todos"
    return "\n".join(f"{item['id']}. [{item['status']}] {item['content']}" for item in items)


def _skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    found = discover_skills(ctx.workspace)
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        if not found:
            return ok("skill", "no skills. Add SKILL.md under .bot/skills/<name>/.", ctx)
        return ok(
            "skill",
            "\n".join(f"{item['name']} ({item['source']}): {item['description']}" for item in found),
            ctx,
        )
    key = name.strip().lower()
    match = next((item for item in found if item["name"].lower() == key), None)
    if match is None:
        names = ", ".join(item["name"] for item in found) or "none"
        return err("skill", f"unknown skill {name.strip()}. Available: {names}")
    try:
        text = Path(match["path"]).read_text(encoding="utf-8")
    except OSError as exc:
        return err("skill", str(exc))
    return ok("skill", text, ctx)


def builtin_skills_dir() -> Path | None:
    """The skill packs shipped with crit (android, aosp, aaos, kotlin, aspice, ...)."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "skills", Path(sys.executable).resolve().parent / "skills"):
        if candidate.is_dir():
            return candidate
    return None


def discover_skills(workspace: Path) -> list[dict[str, Any]]:
    """Project skills (.bot/skills, .agents/skills, .opencode/skills), then the built-in ones.

    A project skill with the same name as a built-in one replaces it.
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    roots: list[tuple[Path, str]] = [(Path(workspace) / rel, "project") for rel in _SKILL_ROOTS]
    builtin = builtin_skills_dir()
    if builtin is not None:
        roots.append((builtin, "built-in"))
    for root, source in roots:
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            name = skill_md.parent.name
            if name.lower() in seen:
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            meta = skill_meta(text)
            seen.add(name.lower())
            found.append(
                {
                    "name": name,
                    "description": meta.get("description") or _skill_description(text),
                    "path": str(skill_md),
                    "source": source,
                    "keywords": _split_list(meta.get("keywords", "")),
                    "files": _split_list(meta.get("files", ""), lower=False),
                }
            )
    return found


def _discover_skills(workspace: Path) -> list[dict[str, Any]]:
    return discover_skills(workspace)


def skill_meta(text: str) -> dict[str, str]:
    """The ``key: value`` lines of a SKILL.md front matter (between the first two ``---`` lines)."""
    match = re.match(r"﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not match:
        return {}
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            meta[key.strip().lower()] = value.strip().strip("\"'")
    return meta


def skill_body(text: str) -> str:
    """A SKILL.md without its front matter."""
    return re.sub(r"\A﻿?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|$)", "", text, count=1, flags=re.S).strip()


def _split_list(raw: str, *, lower: bool = True) -> list[str]:
    items = [item.strip() for item in re.split(r"[,;]", raw or "") if item.strip()]
    return [item.lower() for item in items] if lower else items


# Which project-type skill to load when only the repo's files point at one (no keyword in the task).
_MARKER_PREFERENCE = ("aosp", "aaos", "android", "jetpack-compose", "kotlin", "java", "cpp-native", "gradle")
_marker_cache: dict[tuple[str, str], bool] = {}


def _has_marker(workspace: Path, pattern: str) -> bool:
    """True when ``pattern`` exists at the workspace root or one folder down. Cheap on huge trees."""
    pattern = pattern.strip().replace("\\", "/").strip("/")
    while pattern.endswith("/**") or pattern.endswith("/*"):
        pattern = pattern.rsplit("/", 1)[0]
    if not pattern or "**" in pattern or pattern.startswith(".."):
        return False
    key = (str(workspace), pattern)
    if key not in _marker_cache:
        hit = False
        for glob in (pattern, "*/" + pattern):
            try:
                if next(iter(Path(workspace).glob(glob)), None) is not None:
                    hit = True
                    break
            except (OSError, ValueError, NotImplementedError):
                continue
        _marker_cache[key] = hit
    return _marker_cache[key]


def _mentions(text: str, phrase: str) -> bool:
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    # "android" is not a mention inside "android.bp"; a dot joins words in file and package names.
    pattern = r"(?<![a-z0-9_])(?<![a-z0-9_]\.)" + re.escape(phrase) + r"(?![a-z0-9_])(?!\.[a-z0-9_])"
    return re.search(pattern, text) is not None


def select_skills(
    task: str,
    workspace: Path,
    *,
    pinned: Iterable[str] = (),
    limit: int = 2,
) -> list[dict[str, Any]]:
    """The skills that fit ``task``: pinned ones, then by keywords in the task, then by the repo's files.

    A skill named or matched by the task's words ranks first. When fewer than
    ``limit`` match that way, one project-type skill found from the repo's
    files (for example ``AndroidManifest.xml`` -> android) fills a slot.
    """
    found = discover_skills(workspace)
    wanted = {str(name).strip().lower() for name in pinned if str(name).strip()}
    text = " ".join(str(task or "").lower().split())
    chosen: list[dict[str, Any]] = [item for item in found if item["name"].lower() in wanted]
    scored: list[tuple[int, int, dict[str, Any]]] = []
    by_marker: list[dict[str, Any]] = []
    for order, item in enumerate(found):
        if item in chosen:
            continue
        name = item["name"].lower()
        score = 0
        if _mentions(text, name) or _mentions(text, name.replace("-", " ")):
            score += 6
        hits = sum(1 for word in item["keywords"] if _mentions(text, word))
        score += 3 * min(hits, 4)
        if score >= 3:
            scored.append((score, -order, item))
        elif item["files"] and any(_has_marker(workspace, pattern) for pattern in item["files"]):
            by_marker.append(item)
    scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    chosen.extend(item for _score, _order, item in scored)
    room = max(limit, len([item for item in chosen if item["name"].lower() in wanted]))
    if len(chosen) < limit and by_marker:
        rank = {name: index for index, name in enumerate(_MARKER_PREFERENCE)}
        by_marker.sort(key=lambda item: rank.get(item["name"].lower(), len(rank)))
        chosen.append(by_marker[0])
    return chosen[:room]


def skill_text(item: dict[str, Any], *, max_chars: int = 7_000) -> str:
    """The body of one skill for the model, capped."""
    try:
        text = Path(item["path"]).read_text(encoding="utf-8")
    except OSError:
        return ""
    body = skill_body(text)
    if len(body) > max_chars:
        body = body[:max_chars].rsplit("\n", 1)[0] + "\n..."
    return body


def _skill_description(text: str) -> str:
    match = re.search(r"(?m)^description:\s*(.+)$", text)
    if match:
        return match.group(1).strip().strip("\"'")[:160]
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("---") and not line.startswith("#"):
            return line[:160]
    return ""


# --------------------------------------------------------------------------- delegate

MAX_DELEGATE_BRIEFS = 6


def delegate_briefs(args: dict[str, Any], ctx: ToolContext) -> tuple[list[dict[str, Any]], str]:
    """``[{name, brief, files}]`` from a delegate call, or a problem for the model.

    Accepts ``tasks`` (or ``briefs``/``subtasks``) as a list of objects
    ``{brief, files, name}`` or of plain strings, or one ``brief`` at the top
    level. ``files`` are the files that helper may change; a file can belong to
    one helper only. Paths must be inside the workspace.
    """
    raw = args.get("tasks")
    for key in ("briefs", "subtasks", "agents", "jobs"):
        if raw is None:
            raw = args.get(key)
    if isinstance(raw, str):
        parsed = _maybe_json(raw)
        raw = parsed if isinstance(parsed, list) else [raw]
    if raw is None and (args.get("brief") or args.get("prompt") or args.get("description")):
        raw = [args]
    if not isinstance(raw, list) or not raw:
        return [], 'tasks is required: a list of {"brief": "...", "files": ["path"]}'
    if len(raw) > MAX_DELEGATE_BRIEFS:
        return [], f"at most {MAX_DELEGATE_BRIEFS} briefs per delegate call"
    briefs: list[dict[str, Any]] = []
    owner: dict[str, str] = {}
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            item = {"brief": item}
        if not isinstance(item, dict):
            return [], "each task must be an object with brief (and files)"
        text = item.get("brief") or item.get("prompt") or item.get("description") or item.get("task") or ""
        if not isinstance(text, str) or len(text.strip()) < 15:
            return [], f"task {index} needs a self-contained brief (the helper cannot see this chat)"
        name = str(item.get("name") or f"helper {index}").strip()[:40] or f"helper {index}"
        files = item.get("files") or item.get("paths") or []
        if isinstance(files, str):
            files = [part.strip() for part in re.split(r"[,\n]", files) if part.strip()]
        if not isinstance(files, list):
            return [], f"task {index}: files must be a list of paths"
        cleaned: list[str] = []
        for raw_path in files:
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            path = resolve(ctx.workspace, raw_path)
            if not inside(ctx.workspace, path):
                return [], f"task {index}: {raw_path} is outside the workspace; helpers change workspace files only"
            shown = rel(ctx.workspace, path)
            if shown in owner:
                return [], f"{shown} is given to both {owner[shown]} and {name}; give each file to one helper"
            owner[shown] = name
            cleaned.append(shown)
        briefs.append({"name": name, "brief": text.strip(), "files": cleaned})
    return briefs, ""


def _delegate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    briefs, problem = delegate_briefs(args, ctx)
    if problem:
        return err("delegate", problem)
    if ctx.delegate is None:
        return err(
            "delegate",
            "no helper tabs in this session (helper_sessions is 0, or the browser has no remote debugging); "
            "do the work here with the other tools",
        )
    return ctx.delegate(briefs)


# --------------------------------------------------------------------------- web_fetch


# --------------------------------------------------------------------------- secrets

#: What a redacted secret looks like in a message to the chat.
REDACTED = "[REDACTED:"
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")),
    ("aws key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("gitlab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("api key", re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{32,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("storage key", re.compile(r"(?i)(?<=AccountKey=)[A-Za-z0-9+/=]{40,}")),
    ("url password", re.compile(r"(?<=://)[^/\s:@'\"<>]+:[^/\s@'\"<>]{3,}(?=@)")),
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?im)\b([\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
    r"client[_-]?secret|auth[_-]?key|storepass|keypass)[\w.-]*)(\s*[:=]\s*)([\"']?)([^\s\"'<>{}()$;,`]{6,})\3(?=$|[\s\"',;])"
)
_SECRET_XML_RE = re.compile(
    r"(?i)(name=\"[^\"]*(?:password|secret|token|api[_-]?key|apikey|client[_-]?secret)[^\"]*\"[^>]*>)([^<\s]{8,})(<)"
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*$")


def redact_secrets(text: str) -> tuple[str, int]:
    """``text`` with credentials replaced by ``[REDACTED:kind]``, and how many were replaced.

    Applied to every message before it goes to the chat. A value that reads
    like code (``BuildConfig.API_KEY``, ``getToken``) is left alone; a quoted
    literal or a properties value with digits or symbols is not.
    """
    if not text:
        return text, 0
    count = 0
    for kind, pattern in _SECRET_PATTERNS:
        text, hits = pattern.subn(f"{REDACTED}{kind}]", text)
        count += hits

    def assignment(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group(4)
        if value.startswith(REDACTED) or (not match.group(3) and _IDENTIFIER_RE.match(value)):
            return match.group(0)
        if value.lower() in {"true", "false", "null", "none", "required", "optional", "password", "changeit!"}:
            return match.group(0)
        count += 1
        return f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}{match.group(1).lower()}]{match.group(3)}"

    text = _SECRET_ASSIGN_RE.sub(assignment, text)

    def xml_value(match: re.Match[str]) -> str:
        nonlocal count
        if match.group(2).startswith(("@string/", "@", REDACTED)):
            return match.group(0)
        count += 1
        return f"{match.group(1)}{REDACTED}secret]{match.group(3)}"

    text = _SECRET_XML_RE.sub(xml_value, text)
    return text, count


# web_fetch reads documentation; it must never carry data out. A request can only
# reach these sites (and subdomains), as a plain https GET with no query string,
# no credentials, no cookies, and no body. A path that looks like it encodes data
# (a long token, base64, or hex run, or anything that looks like a secret) is refused.
DEFAULT_WEB_HOSTS = (
    "developer.android.com", "source.android.com", "android.googlesource.com", "android-developers.googleblog.com",
    "kotlinlang.org", "docs.gradle.org", "docs.oracle.com", "openjdk.org", "junit.org", "mockk.io",
    "maven.apache.org", "docs.python.org", "peps.python.org", "learn.microsoft.com", "developer.mozilla.org",
    "docs.github.com", "git-scm.com", "cmake.org", "en.cppreference.com", "stackoverflow.com",
)
FETCH_MAX_URL = 300
_DATA_RUN_RE = re.compile(r"[A-Za-z0-9+/=_%-]{48,}|[0-9a-fA-F]{32,}")


def web_hosts(ctx: ToolContext) -> tuple[str, ...]:
    return DEFAULT_WEB_HOSTS if ctx.web_hosts is None else tuple(ctx.web_hosts)


def outbound_problem(url: str, hosts: tuple[str, ...]) -> str:
    """Why fetching ``url`` could send data out, or "" when it is a plain read of an allowed site."""
    if not hosts:
        return "web_fetch is turned off (web_fetch_hosts is empty); nothing leaves this machine but the chat"
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        return f"not a valid URL: {exc}"
    if parts.scheme.lower() != "https":
        return "only https URLs are fetched"
    host = (parts.hostname or "").lower().rstrip(".")
    if not any(host == allowed or host.endswith("." + allowed) for allowed in hosts):
        return (
            f"{host or url} is not one of the documentation sites web_fetch may read ({', '.join(hosts[:8])}"
            + (", ..." if len(hosts) > 8 else "")
            + "); add it to web_fetch_hosts in .bot/settings.json if it is needed"
        )
    if parts.username or parts.password:
        return "a URL with a user name or password is not fetched"
    if port not in (None, 443):
        return "only the standard https port is used"
    if parts.query:
        return "a URL with a query string (?...) is not fetched: it could carry data out. Use the plain page address"
    if len(url) > FETCH_MAX_URL:
        return f"the URL is longer than {FETCH_MAX_URL} characters; use the plain page address"
    path = urllib.parse.unquote(parts.path or "")
    if _DATA_RUN_RE.search(path) or redact_secrets(path)[1]:
        return "the URL path looks like it carries data (a long token or encoded text); it is not fetched"
    return ""


class _CrossHostRedirect(urllib.error.URLError):
    """A redirect to another host: the approval covered one host, so the model must ask again."""

    def __init__(self, target: str) -> None:
        super().__init__(f"redirected to another host: {target}")
        self.target = target


class _LimitedRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = FETCH_MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - stdlib signature
        target = urllib.parse.urljoin(req.full_url, newurl)
        parts = urllib.parse.urlsplit(target)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            raise urllib.error.URLError(f"redirected to a {scheme or 'relative'} URL, which is not fetched")
        here = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
        if (parts.hostname or "").lower() != here:
            raise _CrossHostRedirect(target)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _TextExtractor(HTMLParser):
    """Readable text from HTML: headings, paragraphs, lists, links, and code blocks."""

    _DROP = {"script", "style", "nav", "noscript", "svg", "template", "iframe", "head", "form", "button"}
    _BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "aside", "table", "tr", "ul", "ol",
              "dl", "dt", "dd", "blockquote", "figure", "figcaption", "br", "hr", "li", "h1", "h2", "h3", "h4",
              "h5", "h6", "pre", "title", "summary", "details"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.title = ""
        self._drop = 0
        self._pre = 0
        self._in_title = False
        self._href: list[str | None] = []
        self._link_start: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._DROP and tag != "head":
            self._drop += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if self._drop:
            return
        if tag in self._BLOCK:
            self.parts.append("\n")
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("- ")
        elif tag == "pre":
            self._pre += 1
            self.parts.append("```\n")
        elif tag == "code" and not self._pre:
            self.parts.append("`")
        elif tag == "a":
            href = dict(attrs).get("href")
            self._href.append(href)
            self._link_start.append(len(self.parts))
        elif tag == "td" or tag == "th":
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._DROP and tag != "head":
            self._drop = max(0, self._drop - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if self._drop:
            return
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
            self.parts.append("\n```\n")
        elif tag == "code" and not self._pre:
            self.parts.append("`")
        elif tag == "a" and self._href:
            href = self._href.pop()
            start = self._link_start.pop()
            label = "".join(self.parts[start:]).strip()
            if href and not href.startswith(("#", "javascript:", "mailto:")) and label:
                target = urllib.parse.urljoin(self.base_url, href)
                if target != label:
                    self.parts.append(f" ({target})")
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._drop:
            return
        if self._pre:
            self.parts.append(data)
        else:
            self.parts.append(re.sub(r"\s+", " ", data))

    def text(self) -> str:
        raw = "".join(self.parts)
        out: list[str] = []
        in_code = False
        for line in raw.split("\n"):
            if line.strip() == "```":
                in_code = not in_code
                out.append("```")
                continue
            out.append(line.rstrip() if in_code else line.strip())
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
        title = " ".join(self.title.split())
        return (f"# {title}\n\n" if title and not text.startswith("# ") else "") + text


def html_to_text(markup: str, base_url: str = "") -> str:
    parser = _TextExtractor(base_url)
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed markup: keep what was parsed
        pass
    return parser.text()


def _web_fetch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return err("web_fetch", "url is required, for example https://docs.python.org/3/")
    url = url.strip().strip("<>\"'` ")
    if "://" not in url:
        url = "https://" + url
    url = url.split("#", 1)[0]
    problem = outbound_problem(url, web_hosts(ctx))
    if problem:
        return err("web_fetch", problem)
    try:
        limit = int(args.get("max_chars") or DEFAULT_FETCH_CHARS)
        start = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        return err("web_fetch", "max_chars and offset must be integers")
    limit = min(max(limit, 500), ctx.max_chars)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; crit-agent/1.0)",
            "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.5",
        },
    )
    opener = urllib.request.build_opener(_LimitedRedirects())
    started = time.monotonic()
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT) as response:
            final_url = response.geturl()
            status = getattr(response, "status", 200)
            content_type = response.headers.get("Content-Type", "")
            charset = response.headers.get_content_charset() or ""
            data = response.read(FETCH_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(2000).decode("utf-8", "replace")
            if "html" in (exc.headers.get("Content-Type") or ""):
                body = html_to_text(body, url)
        except Exception:
            pass
        return err(
            "web_fetch",
            f"HTTP {exc.code} {exc.reason} for {url}",
            output=_one_line(body, 400) if body else "",
            ctx=ctx,
        )
    except _CrossHostRedirect as exc:
        return err(
            "web_fetch",
            f"{url} redirects to another host: {exc.target}. It was not followed; "
            "call web_fetch with that URL if you need it (it needs its own approval)",
        )
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            return err("web_fetch", f"timed out after {FETCH_TIMEOUT:.0f}s: {url}")
        if "redirect" in str(reason).lower() or "redirect" in str(exc).lower():
            return err("web_fetch", f"too many redirects or a bad redirect: {_one_line(str(reason), 200)}")
        return err("web_fetch", f"could not fetch {url}: {_one_line(str(reason), 200)}")
    except TimeoutError:
        return err("web_fetch", f"timed out after {FETCH_TIMEOUT:.0f}s: {url}")
    except (OSError, ValueError) as exc:
        return err("web_fetch", f"could not fetch {url}: {_one_line(str(exc), 200)}")
    seconds = time.monotonic() - started
    clipped = len(data) > FETCH_MAX_BYTES
    data = data[:FETCH_MAX_BYTES]
    kind = content_type.split(";", 1)[0].strip().lower()
    if not kind:
        kind = "text/html" if data.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")) else "text/plain"
    textual = kind.startswith("text/") or "json" in kind or "xml" in kind or "javascript" in kind
    if not textual or looks_binary_bytes(data[:8192]):
        return ok(
            "web_fetch",
            f"{final_url}\n{kind or 'unknown type'}, {_size_text(len(data))}: binary content is not shown",
            ctx,
            _ui(f"Fetched {_size_text(len(data))} ({kind})"),
        )
    text = data.decode(charset or "utf-8", "replace") if _known_codec(charset) else data.decode("utf-8", "replace")
    if "html" in kind or "xhtml" in kind:
        text = html_to_text(text, final_url)
    elif "json" in kind:
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    total = len(text)
    piece = text[start : start + limit]
    header = f"{final_url} (HTTP {status}, {kind}, {total} chars"
    header += f", showing {start}-{start + len(piece)}" if total > len(piece) or start else ""
    header += ")"
    lines = [header, piece]
    if start + len(piece) < total:
        lines.append(f"... {total - start - len(piece)} more chars; pass offset {start + len(piece)} to read on")
    if clipped:
        lines.append(f"download stopped at {_size_text(FETCH_MAX_BYTES)}")
    return ok(
        "web_fetch",
        "\n".join(lines),
        ctx,
        _ui(f"Fetched {_size_text(len(data))} ({kind}) in {seconds:.1f}s"),
    )


def _known_codec(name: str) -> bool:
    if not name:
        return False
    import codecs

    try:
        codecs.lookup(name)
        return True
    except LookupError:
        return False


# --------------------------------------------------------------------------- ask_user


def _ask_user(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return err("ask_user", "question is required")
    options = args.get("options")
    if isinstance(options, str):
        options = [item.strip() for item in re.split(r"[\n|]", options) if item.strip()]
    if options is not None and not isinstance(options, list):
        return err("ask_user", "options must be a list of short answers")
    choices = [
        str(item.get("label") or item.get("text") or item.get("value") or "") if isinstance(item, dict) else str(item)
        for item in (options or [])
    ]
    choices = [item for item in choices if item.strip()][:9]
    if ctx.ask_user is None:
        return err("ask_user", "no user available; decide yourself and continue")
    prompt = question.strip()
    if choices:
        prompt += "\n" + "\n".join(f"{index}. {item}" for index, item in enumerate(choices, start=1))
    try:
        answer = ctx.ask_user(prompt)
    except (EOFError, KeyboardInterrupt):
        answer = ""
    answer = str(answer or "").strip()
    if not answer:
        return err("ask_user", "the user gave no answer; decide yourself and continue")
    if choices and answer.isdigit() and 1 <= int(answer) <= len(choices):
        answer = choices[int(answer) - 1]
    return ok("ask_user", f"user answered: {answer}", ctx, _ui(f"User answered: {_one_line(answer, 80)}"))


# --------------------------------------------------------------------------- permissions


def permission_for(name: str, args: dict[str, Any] | None, ctx: ToolContext) -> Permission:
    """What the user must approve before ``name`` runs with ``args``.

    read: never asks. edit: key "edit". command: key "command:<program>".
    network: key "network:<host>". outside: any path outside the workspace;
    its key is empty, so it is asked every time.
    """
    tool = canonical_tool(name)
    if not tool:
        return Permission("read", f"Unknown tool {name}", "")
    args = normalize_args(tool, args if isinstance(args, (dict, str)) else {})
    outside = [item for item in _paths_of(tool, args, ctx) if not inside(ctx.workspace, item)]
    if tool in READ_ONLY:
        if outside:
            shown = ", ".join(str(item) for item in outside[:3])
            return Permission("outside", f"Read outside the workspace: {shown}", "", _tool_label(tool, args))
        if tool == "read_files":
            secret = [item for item in _paths_of(tool, args, ctx) if sensitive_file(item)]
            if secret:
                shown = ", ".join(rel(ctx.workspace, item) for item in secret[:3])
                return Permission(
                    "outside",
                    f"Read a file that usually holds secrets: {shown}",
                    "",
                    _tool_label(tool, args),
                    risk="reads a file that usually holds secrets (its contents go to the chat, with known secret formats redacted)",
                )
        return Permission("read", _tool_label(tool, args), tool)
    if tool in MUTATING:
        summary = _tool_label(tool, args)
        detail = ""
        if tool == "apply_patch":
            detail = str(_patch_text(args) or "")[:4000]
        elif tool == "edit_file":
            detail = _edit_preview(args)
        if outside:
            shown = ", ".join(str(item) for item in outside[:3])
            return Permission("outside", f"{summary} (outside the workspace: {shown})", "", detail)
        return Permission("edit", summary, "edit", detail)
    if tool == "run_command":
        command = str(args.get("command") or "").strip()
        label = ("Run in background: " if args.get("background") is True else "Run: ") + _one_line(command, 100)
        if not outside and ctx.session is not None and not args.get("cwd"):
            current = Path(getattr(ctx.session, "cwd", ctx.workspace) or ctx.workspace)
            if not inside(ctx.workspace, current.resolve()):
                outside = [current]
        if outside:
            return Permission("outside", f"{label} (cwd outside the workspace: {outside[0]})", "", command)
        reach = network_command(command)
        if reach:
            return Permission("network", f"{label} (it {reach})", "", command, risk=f"{reach}, which can send data off this machine")
        program = command_key(command)
        return Permission("command", label, f"command:{program}" if program else "", command)
    if tool == "delegate":
        briefs, problem = delegate_briefs(args, ctx)
        if problem or not briefs:
            return Permission("read", "Delegate", "")
        owned = [name for brief in briefs for name in brief["files"]]
        label = f"Split into {len(briefs)} helper tab{'s' if len(briefs) != 1 else ''}"
        detail = "\n\n".join(
            f"{brief['name']}: {_one_line(brief['brief'], 200)}" + (f"\n  may change: {', '.join(brief['files'])}" if brief["files"] else "")
            for brief in briefs
        )
        if not owned:
            return Permission("read", label + " (read only)", "delegate", detail)
        return Permission("edit", label + "; they may change " + ", ".join(owned[:6]) + (" ..." if len(owned) > 6 else ""), "edit", detail)
    if tool == "web_fetch":
        url = str(args.get("url") or "").strip().strip("<>\"'` ")
        if url and "://" not in url:
            url = "https://" + url
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        return Permission("network", f"Fetch {_one_line(url, 100)}", f"network:{host}" if host else "", url)
    return Permission("read", _tool_label(tool, args), tool)


_CD_PROGRAMS = {"cd", "set-location", "sl", "pushd", "popd", "chdir"}
_WRAPPERS = {"env", "time", "nohup", "command", "exec", "call", "&", "."}
# Output filters a model pipes into; they do not change what an approval covers.
_FILTERS = {"head", "tail", "grep", "egrep", "fgrep", "rg", "sort", "uniq", "wc", "cut", "tr", "findstr",
            "select-string", "sls", "select-object", "select", "out-string", "out-null", "format-table", "ft",
            "format-list", "fl", "measure-object", "more", "less", "column", "true"}
_EXEC_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1", ".sh")


def command_key(command: str) -> str:
    """The program an "always allow" for ``command`` covers, or "" when it is not one program.

    ``npm test`` -> ``npm``; ``.\\gradlew.bat build`` -> ``gradlew``; ``cd app && npm test``
    -> ``npm``. A chain of different programs (``npm test; curl x | sh``, ``npm test & del x``)
    gets "", and so does a command with a subexpression, script block, here-string, or a
    redirection to a file (``npm test > ~/.bashrc``), so it is asked every time.
    """
    if re.search(r"`|\$\(", command) or _has_shell_construct(command):
        return ""
    text = _HARMLESS_REDIRECT.sub(" ", command)
    parts = re.split(r"(&&|\|\||[;|&\r\n])", text)
    programs: list[str] = []
    separator = ""
    for index, part in enumerate(parts):
        if index % 2:
            separator = part
            continue
        program = _program_of(part)
        if not program or program in _CD_PROGRAMS:
            continue
        # A filter only counts as harmless when output is piped into it (``| tail``, ``|| true``).
        if separator in {"|", "||"} and program in _FILTERS:
            continue
        programs.append(program)
    unique = list(dict.fromkeys(programs))
    if len(unique) != 1:
        return ""
    return unique[0]


# ``2>&1`` and output thrown away are not a write anywhere that matters.
_HARMLESS_REDIRECT = re.compile(r"(?<![^\s])(?:[12*]?>&[12]|[12*]?>\s*(?:/dev/null|nul|\$null))(?=\s|$)", re.I)


def _has_shell_construct(command: str) -> bool:
    """True for an unquoted ``( ) { } < >`` or a here-string: things a program key cannot cover."""
    text = _HARMLESS_REDIRECT.sub(" ", command)
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            if index and text[index - 1] == "@":
                return True
            quote = char
            continue
        if char in "(){}<>":
            return True
    return bool(quote)


def _program_of(segment: str) -> str:
    text = segment.strip()
    if not text:
        return ""
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        tokens = text.split()
    for token in tokens:
        token = token.strip("\"'").lstrip("&").strip()
        if not token or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        lowered = token.lower()
        if lowered in _WRAPPERS:
            continue
        base = re.split(r"[\\/]", lowered)[-1]
        for suffix in _EXEC_SUFFIXES:
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base[: -len(suffix)]
                break
        return base
    return ""


# --------------------------------------------------------------------------- command risk

_DELETE_PROGRAMS = {"rm", "remove-item", "ri", "del", "erase", "rd", "rmdir"}
_BROAD_TARGETS = {
    "/", "/*", "~", "~/", "~/*", "*", "*.*", ".", "./", "./*", "..", "../", "../*", ".git", "./.git",
    "$home", "${home}", "$env:userprofile", "%userprofile%", "%homedrive%", "$env:homedrive",
    "/home", "/usr", "/etc", "/var", "/opt", "/bin", "/lib", "/system", "/vendor", "/data", "/sdcard",
}
_RISKY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.I), reason)
    for pattern, reason in (
        (r"\bgit\b[^;&|\n]*\bpush\b[^;&|\n]*(?:\s--force\b|\s-f\b|\s--force-with-lease\b|\s\+\S)", "force-pushes to a remote"),
        (r"\bgit\b[^;&|\n]*\bpush\b", "pushes to a remote"),
        (r"\bgit\b[^;&|\n]*\breset\b[^;&|\n]*--hard\b", "throws away uncommitted changes (git reset --hard)"),
        (r"\bgit\b[^;&|\n]*\bclean\b[^;&|\n]*\s-[a-z]*f", "deletes untracked files (git clean)"),
        (r"\bgit\b[^;&|\n]*\b(?:checkout|restore)\b[^;&|\n]*\s(?:--\s+)?\.(?:\s|$)", "discards uncommitted changes"),
        (r"\bgit\b[^;&|\n]*\bbranch\b[^;&|\n]*\s-D\b", "force-deletes a branch"),
        (r"\bgit\b[^;&|\n]*\bstash\b[^;&|\n]*\b(?:drop|clear)\b", "deletes stashed work"),
        (r"\bgit\b[^;&|\n]*\bfilter-(?:branch|repo)\b", "rewrites history"),
        (r"\brepo\s+upload\b", "uploads changes for review"),
        (r"\bfastboot\b[^;&|\n]*\b(?:flash|flashall|erase|format|oem|flashing|update|-w)\b", "writes to a device's partitions"),
        (r"\b(?:mkfs(?:\.\w+)?|fdisk|parted|diskpart|format-volume|clear-disk|initialize-disk)\b", "formats or partitions a disk"),
        (r"(?:^|[\s;&|])format\s+[a-z]:", "formats a drive"),
        (r"\bdd\b[^;&|\n]*\bof=", "writes raw data with dd"),
        (r"(?:^|[\s;&|])(?:shutdown|reboot|halt|poweroff|stop-computer|restart-computer)\b", "shuts down or restarts the computer"),
        (r"(?:^|[\s;&|])(?:sudo|su|doas|runas)\s", "runs with elevated rights"),
        (r"start-process\b[^;&|\n]*-verb\s+runas", "runs with elevated rights"),
        (r"\b(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|python3?|iex|invoke-expression|pwsh|powershell)\b", "runs a script straight from the internet"),
        (r"\b(?:iex|invoke-expression)\b[^\n]*(?:downloadstring|irm|iwr|invoke-restmethod|invoke-webrequest)", "runs a script straight from the internet"),
        (r"\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bdocker\s+push\b|\bmvn\b[^;&|\n]*\bdeploy\b|\bgradlew?(?:\.bat)?\b[^;&|\n]*\bpublish", "publishes a package"),
        (r"\breg(?:\.exe)?\s+delete\b|\bremove-itemproperty\b[^;&|\n]*hklm", "deletes registry keys"),
        (r"\bkill\s+-9\s+-1\b|\bkillall\b", "kills many processes"),
        (r"\b(?:chmod|chown)\b[^;&|\n]*\s-r\b[^;&|\n]*\s(?:/|~)(?:\s|$)", "changes permissions on a whole tree"),
        (r">\s*/dev/(?:sd[a-z]|nvme|disk)", "writes to a raw disk"),
        (r"\bsetx\b[^;&|\n]*\s/m\b|setenvironmentvariable\([^)]*['\"]machine['\"]", "changes machine-wide settings"),
    )
)


# Commands that can send data off this machine. Only the chat may receive data, so
# these do not run (or, with "network_commands": "ask", run only after a yes).
# Builds and package managers may still download their dependencies; uploads and
# arbitrary-URL tools, remote shells, file servers, and scripts that open sockets may not.
_NETWORK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.I), reason)
    for pattern, reason in (
        (r"(?:^|[\s;&|({])(?:curl|wget|wget2|aria2c|httpie|http|xh)(?:\.exe)?(?=\s|$)", "makes web requests"),
        (r"\b(?:invoke-webrequest|invoke-restmethod|iwr|irm|start-bitstransfer|bitsadmin)\b", "makes web requests"),
        (r"\b(?:net\.webclient|system\.net\.webclient|system\.net\.http|httpclient|webrequest\]|downloadstring|downloadfile|uploadstring|uploadfile|uploaddata|uploadvalues)\b", "makes web requests"),
        (r"\bcertutil\b[^;&|\n]*-urlcache", "downloads with certutil"),
        (r"(?:^|[\s;&|({])(?:scp|sftp|ftp|tftp|ssh|telnet|rsh|nc|ncat|netcat|socat|plink|pscp|psftp)(?:\.exe)?(?=\s|$)", "opens a remote connection"),
        (r"\brsync\b[^;&|\n]*(?:\S+@)?[\w.-]+::?[\w/~.-]", "copies to a remote host"),
        (r"\b(?:send-mailmessage|sendmail|mailx?|mutt)\b", "sends mail"),
        (r"\bgit\b[^;&|\n]*\b(?:push|send-email|request-pull)\b", "sends code to a remote"),
        (r"(?:^|[\s;&|({])(?:gh|glab|hub|az|aws|gcloud|gsutil|azcopy|rclone|s3cmd|oci|doctl|heroku|vercel|netlify|firebase)(?:\.exe|\.cmd)?\s", "talks to a cloud service"),
        (r"\bdocker\b[^;&|\n]*\b(?:push|login)\b", "pushes to a registry"),
        (r"\b(?:npm|pnpm|yarn)\s+(?:publish|login|adduser)\b|\btwine\s+upload\b|\bmvnw?\b[^;&|\n]*\bdeploy\b|\bgradlew?(?:\.bat)?\b[^;&|\n]*(?:\bpublish(?!ToMavenLocal\b)\w*|(?<!\w)--scan\b)|\brepo\s+upload\b", "publishes or uploads"),
        (r"\b(?:python3?|py)\b[^;&|\n]*-m\s+http\.server\b|\bphp\s+-S\b|\b(?:npx\s+)?(?:http-server|serve)\s|\bsimplehttpserver\b", "serves files on the network"),
        (r"\b(?:nslookup|dig|host|resolve-dnsname)\s", "sends DNS queries"),
        (r"(?:python3?|py|node|ruby|perl|php|pwsh|powershell)\b[^\n]*\s-(?:c|e|command)\s[^\n]*(?:urllib|requests\.|http\.client|socket\.|fetch\(|https?\.request|net\.connect|xmlhttprequest|invoke-webrequest|net\.webclient)", "opens a network connection from a script"),
        (r"\b(?:send-|test-)?netconnection\b|\btest-connection\b[^;&|\n]*-tcpport", "opens a network connection"),
    )
)


_SENSITIVE_NAMES = re.compile(
    r"(?i)^(?:\.env(?:\..+)?|.+\.(?:pem|key|p8|p12|pfx|jks|keystore|bks|kdbx|ovpn|asc|gpg)|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|"
    r"\.netrc|_netrc|\.npmrc|\.pypirc|\.git-credentials|\.htpasswd|credentials(?:\.json|\.xml)?|"
    r"(?:keystore|signing|secrets?|release-signing)\.properties|secrets?\.(?:json|ya?ml|toml|xml)|service-account.*\.json)$"
)


def sensitive_file(path: Path | str) -> bool:
    """True for a file that usually holds credentials: .env, keys and keystores, .netrc, signing properties."""
    target = Path(path)
    if any(part in {".ssh", ".aws", ".gnupg", ".azure", ".kube"} for part in target.parts):
        return True
    return bool(_SENSITIVE_NAMES.match(target.name))


_SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".psm1", ".sh", ".bash", ".rb", ".pl", ".php", ".bat", ".cmd")
_SCRIPT_NET_RE = re.compile(
    r"(?i)\b(?:urllib\.request|urllib3|requests\.(?:get|post|put|patch|request|session)|http\.client|httpx|aiohttp|"
    r"socket\.(?:socket|create_connection)|smtplib|ftplib|paramiko|fetch\(|axios|https?\.(?:request|get)\(|net\.(?:connect|socket)|"
    r"xmlhttprequest|websocket|invoke-webrequest|invoke-restmethod|net\.webclient|system\.net\.http|"
    r"(?:^|[\s;&|(])(?:curl|wget|scp|ssh|nc|ncat|ftp)\s)"
)


def network_script(command: str, workspace: Path, cwd: Path | None = None) -> str:
    """The script a command runs, when that script opens network connections; "" otherwise.

    The command text can look harmless (``python send.py``) while the file it
    runs talks to the network, so the file itself is read (first 256 KB).
    """
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = command.split()
    base = Path(cwd or workspace)
    for token in tokens:
        name = token.strip("\"'").lstrip("&").strip()
        if not name.lower().endswith(_SCRIPT_SUFFIXES):
            continue
        path = Path(name)
        if not path.is_absolute():
            path = base / path
        try:
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                text = handle.read(256 * 1024).decode("utf-8", "replace")
        except OSError:
            continue
        if _SCRIPT_NET_RE.search(text):
            return name
    return ""


def network_command(command: str) -> str:
    """Why ``command`` could send data off this machine, or "" when it does not look like it can."""
    text = " ".join(str(command or "").split())
    for pattern, reason in _NETWORK_PATTERNS:
        if pattern.search(text):
            return reason
    return ""


def risky_command(command: str) -> str:
    """Why ``command`` deserves a person's yes even in auto mode, or "" when it does not.

    Deletes of broad trees (``rm -rf /``, ``Remove-Item -Recurse ~``, ``rd /s .``),
    history and work loss (``git reset --hard``, ``git clean -f``, force pushes),
    pushes and publishes, disk and device writes (``dd``, ``mkfs``, ``fastboot flash``),
    elevation, restarts, and piping a download into a shell.
    """
    text = " ".join(str(command or "").split())
    if not text:
        return ""
    lowered = text.lower()
    deleting = _broad_delete(text)
    if deleting:
        return deleting
    for pattern, reason in _RISKY_PATTERNS:
        if pattern.search(lowered):
            return reason
    return ""


def _broad_delete(command: str) -> str:
    for segment in re.split(r"&&|\|\||[;|&\n]", command):
        try:
            tokens = shlex.split(segment, posix=False)
        except ValueError:
            tokens = segment.split()
        tokens = [token.strip("\"'") for token in tokens if token.strip("\"'")]
        while tokens and tokens[0].lower() in _WRAPPERS | {"sudo", "cmd", "/c", "cmd.exe"}:
            tokens = tokens[1:]
        if not tokens:
            continue
        program = re.split(r"[\\/]", tokens[0].lower())[-1].removesuffix(".exe")
        if program not in _DELETE_PROGRAMS:
            continue
        flags = [token.lower() for token in tokens[1:] if token.startswith("-") or token.startswith("/") and len(token) <= 3]
        recursive = any(
            flag in {"/s", "--recursive", "-r", "-recurse"}
            or (flag.startswith("-rec") and "-recurse".startswith(flag))
            or (re.fullmatch(r"-[a-z]+", flag) and "r" in flag and program in {"rm", "rmdir"})
            for flag in flags
        )
        targets = [
            token.lower().rstrip("\\/") or token.lower()
            for token in tokens[1:]
            if not (token.startswith("-") or (token.startswith("/") and len(token) <= 3 and token.lower() not in {"/", "/*"}))
        ]
        for target in targets:
            bare = target.replace("\\", "/")
            if (
                bare in _BROAD_TARGETS
                or re.fullmatch(r"[a-z]:/?\*?", bare)
                or re.fullmatch(r"/[^/\s]*", bare) and recursive
                or bare.endswith("/.git")
            ):
                return f"deletes {target}" + (" and everything under it" if recursive else "")
    return ""


# Programs that only look. In plan mode these run; anything else waits until the plan is approved.
_LOOK_PROGRAMS = {
    "ls", "dir", "gci", "get-childitem", "cat", "type", "gc", "get-content", "head", "tail", "less", "more",
    "grep", "egrep", "fgrep", "rg", "ag", "findstr", "select-string", "sls", "find", "fd", "wc", "pwd",
    "get-location", "gl", "which", "where", "get-command", "gcm", "tree", "file", "stat", "du", "df",
    "echo", "write-output", "printenv", "env", "uname", "hostname", "whoami", "test-path", "get-item", "gi",
    "resolve-path", "sort", "uniq", "cut", "diff", "cmp", "md5sum", "sha1sum", "sha256sum", "get-filehash",
    "ps", "get-process", "tasklist", "readlink", "realpath", "basename", "dirname", "jq", "nl", "od", "xxd",
    "hexdump", "strings", "objdump", "readelf", "nm", "aapt", "aapt2", "apkanalyzer", "javap", "ver",
}
_GIT_LOOK = {
    "status", "log", "diff", "show", "blame", "grep", "ls-files", "ls-tree", "rev-parse", "describe",
    "shortlog", "reflog", "cat-file", "merge-base", "show-ref", "whatchanged", "rev-list", "name-rev",
}
_VERSION_FLAGS = {"--version", "-version", "-v", "version", "--help", "-h", "help"}


def read_only_command(command: str) -> bool:
    """True when ``command`` only reads: ``ls``, ``git log``, ``grep``, ``Get-Content``, ``x --version``."""
    text = str(command or "").strip()
    if not text or risky_command(text):
        return False
    program = command_key(text)
    if not program:
        return False
    segments = [seg for seg in re.split(r"&&|\|\||[;|\n]", text) if _program_of(seg) == program]
    for segment in segments:
        try:
            tokens = shlex.split(segment, posix=False)
        except ValueError:
            tokens = segment.split()
        args = [token.strip("\"'").lower() for token in tokens[1:]]
        if args and all(arg in _VERSION_FLAGS for arg in args):
            continue
        if program == "git":
            words = [arg for arg in args if not arg.startswith("-")]
            sub = words[0] if words else ""
            if sub in _GIT_LOOK:
                if sub == "reflog" and len(words) > 1 and words[1] in {"delete", "expire"}:
                    return False
                continue
            if sub in {"branch", "tag", "remote", "stash"} and not set(args) & {"-d", "-D", "--delete", "-m", "-M", "-f", "drop", "clear", "pop", "apply", "add", "remove", "rm", "rename", "set-url", "push", "save"}:
                if sub == "stash" and (len(words) < 2 or words[1] != "list"):
                    return False
                if sub == "tag" and len(words) > 1 and not set(args) & {"-l", "--list"}:
                    return False
                if sub == "branch" and len(words) > 1 and not set(args) & {"-l", "--list", "--contains", "--merged"}:
                    return False
                continue
            if sub == "config" and set(args) & {"--get", "--list", "-l", "--get-all", "--show-origin"}:
                continue
            return False
        if program == "repo":
            words = [arg for arg in args if not arg.startswith("-")]
            if words and words[0] in {"status", "info", "branches", "list", "diff", "overview", "manifest"}:
                continue
            return False
        if program == "adb":
            joined = " ".join(args)
            if re.match(r"^(?:-s \S+ )?(?:devices|get-state|get-serialno|logcat -d\b|shell (?:getprop|dumpsys|pm list|cmd package list|ls|cat|ps|df|id|uname|wm size|wm density|settings get))", joined):
                continue
            return False
        if program in {"gradlew", "gradle"}:
            if args and all(arg in {"tasks", "projects", "properties", "dependencies", "help", "--console=plain", "-q", "--quiet", "--all", "--offline"} or arg.startswith(("--configuration", ":")) for arg in args):
                continue
            return False
        if program == "find" and set(args) & {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"}:
            return False
        if program in {"sort"} and "-o" in args:
            return False
        if program not in _LOOK_PROGRAMS:
            return False
    return True


def _paths_of(tool: str, args: dict[str, Any], ctx: ToolContext) -> list[Path]:
    """Resolved paths a call would read or change (for the outside-workspace check)."""
    raws: list[Any] = []
    if tool in {"list_files", "find_files", "search_code", "delete_file", "edit_file", "git_diff", "git_log", "git_show"}:
        raws.append(args.get("path"))
    elif tool == "read_files":
        paths = args.get("paths")
        if paths is None:
            paths = [args.get("path")]
        if isinstance(paths, str):
            paths = [paths]
        for item in paths if isinstance(paths, list) else []:
            raws.append(item.get("path") if isinstance(item, dict) else item)
    elif tool == "write_files":
        files = args.get("files")
        if files is None:
            files = [{"path": args.get("path")}]
        for item in files if isinstance(files, list) else []:
            if isinstance(item, dict):
                raws.append(item.get("path"))
    elif tool == "move_file":
        raws += [args.get("path"), args.get("destination")]
    elif tool == "apply_patch":
        patch = _patch_text(args) or ""
        raws += patched_paths(patch.replace("\r\n", "\n")) if isinstance(patch, str) else []
    elif tool == "run_command":
        raw = args.get("cwd")
        if ctx.session is not None and isinstance(raw, str) and raw.strip():
            # Same resolution as the run: relative to the session's cwd first.
            started, _problem = _session_cwd(ctx, raw)
            if started is not None:
                return [started.resolve()]
        raws.append(raw)
    out: list[Path] = []
    for raw in raws:
        if isinstance(raw, str) and raw.strip():
            try:
                out.append(resolve(ctx.workspace, raw))
            except (OSError, ValueError, RuntimeError):
                continue
    return out


def _tool_label(tool: str, args: dict[str, Any]) -> str:
    def path_of(value: Any) -> str:
        return clean_path(value) if isinstance(value, str) and value.strip() else "?"

    if tool == "delegate":
        count = len(args.get("tasks") or []) if isinstance(args.get("tasks"), list) else 1
        return f"Split into {count} helper tab{'s' if count != 1 else ''}"
    if tool == "edit_file":
        return f"Edit {path_of(args.get('path'))}"
    if tool == "write_files":
        files = args.get("files")
        if isinstance(files, list) and len(files) > 1:
            names = [path_of(item.get("path")) for item in files if isinstance(item, dict)]
            return f"Write {len(names)} files: " + ", ".join(names[:4]) + (" ..." if len(names) > 4 else "")
        if isinstance(files, list) and files and isinstance(files[0], dict):
            return f"Write {path_of(files[0].get('path'))}"
        return f"Write {path_of(args.get('path'))}"
    if tool == "delete_file":
        return f"Delete {path_of(args.get('path'))}"
    if tool == "move_file":
        return f"Move {path_of(args.get('path'))} → {path_of(args.get('destination'))}"
    if tool == "apply_patch":
        patch = _patch_text(args) or ""
        names = patched_paths(patch) if isinstance(patch, str) else []
        return "Patch " + (", ".join(names[:4]) or "files") + (" ..." if len(names) > 4 else "")
    if tool == "read_files":
        paths = args.get("paths") or [args.get("path")]
        if isinstance(paths, str):
            paths = [paths]
        names = [path_of(item.get("path") if isinstance(item, dict) else item) for item in paths if item]
        return "Read " + (", ".join(names[:4]) or str(args.get("symbol") or "?"))
    if tool in {"list_files", "find_files", "search_code"}:
        what = args.get("pattern") or args.get("glob") or ""
        return f"{tool.replace('_', ' ').capitalize()} {_one_line(str(what), 60)} in {path_of(args.get('path') or '.')}".replace("  ", " ")
    return tool.replace("_", " ").capitalize()


def _edit_preview(args: dict[str, Any]) -> str:
    edits = args.get("edits")
    if not isinstance(edits, list):
        edits = [{"old_string": args.get("old_string"), "new_string": args.get("new_string")}]
    chunks = []
    for edit in edits[:5]:
        if isinstance(edit, dict) and isinstance(edit.get("old_string"), str) and isinstance(edit.get("new_string"), str):
            chunks.append(agent_edit.hunk_diff(edit["old_string"], edit["new_string"], "edit", context=1))
    return "\n".join(chunks)[:4000]


_HANDLERS: dict[str, Callable[[dict[str, Any], ToolContext], dict[str, Any]]] = {
    "list_files": _list_files,
    "find_files": _find_files,
    "read_files": _read_files,
    "search_code": _search_code,
    "write_files": _write_files,
    "edit_file": _edit_file,
    "delete_file": _delete_file,
    "run_command": _run_command,
    "git_status": _git_status,
    "git_diff": _git_diff,
    "git_log": _git_log,
    "git_show": _git_show,
    "apply_patch": _apply_patch,
    "todo": _todo,
    "skill": _skill,
    "code_graph": _code_graph,
    "move_file": _move_file,
    "web_fetch": _web_fetch,
    "ask_user": _ask_user,
    "command_output": _command_output,
    "kill_command": _kill_command,
    "delegate": _delegate,
}
