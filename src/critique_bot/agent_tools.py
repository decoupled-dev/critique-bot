# SPDX-License-Identifier: Apache-2.0
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
import functools
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
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

from critique_bot import agent_build, agent_edit, agent_patch, agent_shell, code_index
from critique_bot import big_file, edit_guard, file_catalog, gitlab_access, integrations, log_tools, run_stats, soong
from critique_bot import tool_output
from critique_bot import audit, sandbox, taint
from critique_bot.code_index import SKIP_DIR_NAMES, refresh_path
from critique_bot.patch import looks_binary_bytes, looks_binary_path
from critique_bot.redaction import REDACTED, redact_secrets, sensitive_file  # noqa: F401 (re-exported)

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
    "move_file",
    "web_fetch",
    "ask_user",
    "command_output",
    "command_input",
    "kill_command",
    "delegate",
    "log",
    "jira",
    # the index tools (Stage 8; critique_bot.indexing.tools)
    "code_nav",
    "code_history",
    "find_strings",
    "locate",
    "coverage_locate",
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
            "ask_user",
        "command_output",
        "kill_command",
        "log",
        "jira",
        # the index tools, except coverage_locate (it runs the failing test)
        "code_nav",
        "code_history",
        "find_strings",
        "locate",
    }
)
SHELL_NAMES = ("bash", "sh", "zsh", "pwsh", "powershell", "cmd", "gitbash")

DEFAULT_TOOL_CHARS = 16_000
#: A command's timeout when the call gives none (setting shell_timeout_s). A command still
#: running then is not killed: it carries on as a background job (ShellSession.detach_on_timeout).
DEFAULT_COMMAND_TIMEOUT = 60
#: The longest timeout a call may ask for: 30 minutes.
MAX_COMMAND_TIMEOUT = 1800
#: A timeout above MAX_COMMAND_TIMEOUT at least this big was given in milliseconds (Claude Code, opencode).
_MILLISECOND_TIMEOUT = 10_000
#: A file this short is read whole when no range is given...
WHOLE_READ_LINES = 300
#: ...a longer one in a window of this many lines ("lines 1-100 of 2,431").
DEFAULT_READ_LINES = 100
MAX_READ_LINES = 400
MAX_READ_CHARS = 12_000
OUTLINE_MIN_LINES = WHOLE_READ_LINES
RESPONSE_FORMATS = ("concise", "detailed")
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
    #: An AOSP full build (m alone) after edits was questioned once already.
    full_build_warned: bool = False
    last_command: str = ""
    last_exit: str = ""
    failures_in_row: int = 0
    failed_calls: dict[str, int] = field(default_factory=dict)
    last_path: str = ""
    todos: list[dict[str, str]] = field(default_factory=list)
    commands: int = 0
    commands_failed: int = 0
    #: Content digest of each file as the model last saw it (read or written): stale-edit protection.
    versions: dict[str, str] = field(default_factory=dict)
    #: Failed edits per file since its last successful edit (the whole-file hint counts these).
    edit_failures: dict[str, int] = field(default_factory=dict)

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
    #: Asks the person directly, in every mode (auto included): sandbox escapes. None when no one can answer.
    confirm: Callable[[str], str] | None = None
    cancel: threading.Event | None = None
    #: Commands that can send data off this machine: "block" (the default) or "ask".
    network_commands: str = "block"
    #: Sites web_fetch may read. Empty (the default) means web_fetch is off; "web_fetch": true sets the list.
    web_hosts: tuple[str, ...] = ()
    #: Runs briefs in helper chat tabs at the same time (see agent_helpers). None when there are none.
    delegate: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None
    #: Saved "allow" and "deny" rules from .crit/settings.json (agent_rules.Rules); None when there are none.
    rules: Any = None
    #: The effective .crit/settings.json (tool_output_cap_tokens, edit_fail_limit, whole_file_max_lines,
    #: edit_guard, lint, lint_timeout_s, shell_timeout_s, ...). None: every default.
    settings: dict[str, Any] | None = None
    #: The session's own ToolRegistry (tool_api.execute and permission_for use it), or None for
    #: the installed one. The Engine gives each session its own, so sessions in one process
    #: with different extensions do not share a global.
    registry: Any = None

    def setting(self, key: str, default: Any = None) -> Any:
        """One setting, or ``default`` when it is not set."""
        if isinstance(self.settings, dict) and self.settings.get(key) is not None:
            return self.settings[key]
        return default

    def int_setting(self, key: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
        """A whole-number setting within bounds; ``default`` when it is missing or not a number."""
        raw = self.setting(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < minimum:
            return default
        value = int(raw)
        return min(value, maximum) if maximum is not None else value

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()
        # Resolved like the workspace (no 8.3 short names on Windows), so a saved output
        # under .crit/cache is reported workspace-relative, not as an outside path.
        if self.cache_dir is not None:
            self.cache_dir = Path(self.cache_dir).resolve()


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
    #: The tool, and the paths it would read or change: what saved rules are matched against.
    tool: str = ""
    paths: tuple[str, ...] = ()
    #: The allow rule a "don't ask again" answer saves for a command ("" when there is none).
    rule: str = ""
    #: The command runs inside the OS sandbox (workspace writes only, network through the egress
    #: proxy), so auto mode runs it without asking unless it is risky.
    contained: bool = False


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
    args = normalize_args(canonical, with_action(name, arguments) if isinstance(arguments, (dict, str)) else {})
    try:
        result = _HANDLERS[canonical](args, ctx)
    except Exception as exc:  # a tool bug must come back as a result, not end the task
        result = {"tool": canonical, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _after_tool(canonical, args, result, ctx)
    result.setdefault("tool", canonical)
    if "ui" not in result:
        result["ui"] = _default_ui(canonical, result)
    return shape(result, ctx)


def shape(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """``result`` within this context's output budget (tool_output.shape_output), never empty."""
    budget = tool_output.budget_chars(getattr(ctx, "max_chars", None), getattr(ctx, "settings", None))
    return tool_output.shape_output(
        result, budget, cache_dir=getattr(ctx, "cache_dir", None), workspace=getattr(ctx, "workspace", None)
    )


def _after_tool(tool: str, args: dict[str, Any], result: dict[str, Any], ctx: ToolContext) -> None:
    """Audit file writes, and taint the session when the result carries untrusted text."""
    try:
        if tool in MUTATING and result.get("ok"):
            paths = [rel(ctx.workspace, item) for item in _paths_of(tool, args, ctx)]
            audit.record("write", tool=tool, paths=paths[:50], workspace=str(ctx.workspace))
        taint.observe(tool, args, result, ctx.workspace)
    except Exception:  # noqa: BLE001 - bookkeeping never breaks a tool result
        pass


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


#: Index tool names of the first Stage 8 build: they run as code_nav / code_history with this action.
LEGACY_ACTIONS = {
    "find_symbol": ("code_nav", "find_symbol"),
    "symbol_refs": ("code_nav", "refs"),
    "who_calls": ("code_nav", "who_calls"),
    "callees": ("code_nav", "callees"),
    "imports_of": ("code_nav", "imports_of"),
    "imported_by": ("code_nav", "imported_by"),
    "module_of": ("code_nav", "module_of"),
    "repo_map": ("code_nav", "repo_map"),
    "git_blame": ("code_history", "blame"),
    "git_history": ("code_history", "log"),
    "cochanged": ("code_history", "cochanged"),
}

_ALIASES = {
    **{old: new for old, (new, _action) in LEGACY_ACTIONS.items()},
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
    "send_input": "command_input",
    "write_input": "command_input",
    "terminal_input": "command_input",
    "stdin": "command_input",
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


def with_action(name: str, args: Any) -> Any:
    """Arguments for ``name`` with the action an old index tool name implies (find_symbol ->
    code_nav action find_symbol). Other names and calls that already have an action pass through."""
    key = str(name).strip().strip("`\"'").lower().replace("-", "_")
    legacy = LEGACY_ACTIONS.get(key)
    if legacy is None or not isinstance(args, dict) or args.get("action"):
        return args
    return {**args, "action": legacy[1]}


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
        "head_limit", "context", "timeout", "max_chars", "wait", "lines", "timeout_s",
    }
)
_JSON_KEYS = frozenset({"paths", "files", "edits", "todos", "items", "options", "arguments", "args", "patterns"})
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
            out["timeout"] = float(timeout) / 1000.0 if timeout >= _MILLISECOND_TIMEOUT else MAX_COMMAND_TIMEOUT
        if "timeout" not in out:
            for src in ("timeout_s", "timeout_seconds", "timeout_sec"):
                if isinstance(out.get(src), (int, float)) and not isinstance(out.get(src), bool):
                    out["timeout"] = out[src]
                    break
    if tool == "command_input":
        for src in ("text", "data", "line", "keys", "command"):
            if "input" not in out and src in out:
                out["input"] = out[src]
    if tool in {"command_output", "kill_command", "command_input"}:
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


@functools.lru_cache(maxsize=256)
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
    """Workspace-relative file paths under ``root``, skipping generated trees.

    Served from the cached :mod:`file_catalog`, so repeated calls on a big tree (an AOSP
    ``repo`` checkout) cost a few ``stat`` calls, not a fresh walk.
    """
    if root.is_file():
        return [rel(workspace, root)]
    prefix = rel(workspace, root)
    if prefix in {".", ""} or root == workspace:
        prefix = ""
    if Path(prefix).is_absolute():  # outside the workspace: walk it directly
        rows: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIR_NAMES)
            for name in sorted(filenames):
                rows.append(rel(workspace, Path(dirpath) / name))
        return rows
    return file_catalog.catalog_for(workspace).files(prefix)


# --------------------------------------------------------------------------- results


#: A result is held in memory up to this size; execute() then shapes it to the context budget
#: (head and tail, the whole text saved under .crit/cache/out).
_HARD_CAP = 4_000_000


def _cap(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    note = "\n...[truncated; narrow with path, offset, or a tighter pattern]"
    return text[: max(0, limit - len(note))] + note


def ok(name: str, output: str, ctx: ToolContext, ui: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"tool": name, "ok": True, "output": _cap(output, _HARD_CAP)}
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
        result["output"] = _cap(output, _HARD_CAP)
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
    try:
        column = max(0, int(args.get("col") or args.get("column") or 0))
    except (TypeError, ValueError):
        return err("read_files", "col must be an integer (a character position in a long line)")
    if limit is not None and limit < 1:
        return err("read_files", "limit must be at least 1")
    force = bool(args.get("force"))
    detailed, problem = _response_format(args)
    if problem:
        return err("read_files", problem)
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
        parts.append(_read_one(raw, item_start, item_limit, item_explicit, budget, force, ctx, column=column,
                               detailed=detailed))
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


def _redact_lines(lines: list[str]) -> list[str]:
    """``lines`` with known secret formats hidden, judged on the whole text, and the same number of lines.

    A window of a file (or one search hit) can cut a secret in half, say a private
    key's first lines without its END line, and the redaction of the outgoing message
    would not know it any more. So the whole file is redacted first and windows are
    taken from that. A secret that spanned lines leaves its marker on its first line
    and :data:`REDACTED` on the rest, so line numbers still match the file.
    """
    clean, hidden = redact_secrets("\n".join(lines))
    if not hidden:
        return lines
    new = clean.split("\n")
    if len(new) == len(lines):
        return new
    out = list(lines)
    matcher = difflib.SequenceMatcher(None, lines, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or i1 == i2:
            continue
        size = i2 - i1
        block = new[j1:j2]
        if len(block) > size:
            block = block[: size - 1] + [" ".join(block[size - 1 :])]
        out[i1:i2] = block + [REDACTED + "continued]"] * (size - len(block))
    return out


def _redacted_file_lines(path: Path) -> list[str] | None:
    """A text file's lines after :func:`_redact_lines`, or None when it cannot be read."""
    try:
        return _redact_lines(agent_edit.load_text(path).text.split("\n"))
    except (OSError, ValueError):
        return None


def _response_format(args: dict[str, Any]) -> tuple[bool, str]:
    """(detailed, problem) from a ``response_format`` argument; concise when it is left out."""
    raw = args.get("response_format")
    if raw is None or raw == "":
        return False, ""
    value = str(raw).strip().lower()
    if value not in RESPONSE_FORMATS:
        return False, 'response_format is "concise" (the default) or "detailed"'
    return value == "detailed", ""


def _window(limit: int | None, total: int) -> int:
    """Lines to show from ``start``: the limit asked for (at most 400), else the whole of a short
    file, else the default window."""
    if limit is not None:
        return min(limit, MAX_READ_LINES)
    return max(1, total) if total <= WHOLE_READ_LINES else DEFAULT_READ_LINES


def _read_one(
    raw: str, start: int, limit: int | None, explicit: bool, budget: int, force: bool, ctx: ToolContext, *,
    column: int = 0, detailed: bool = False,
) -> str:
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
    widen_index(ctx, name)
    try:
        with open(path, "rb") as handle:
            head = handle.read(8192)
    except OSError as exc:
        return f"--- {name} ---\n{exc}"
    if looks_binary_path(str(path)) or looks_binary_bytes(head):
        return f"--- {name} ---\n{_binary_note(path, head)}"
    if big_file.is_large(path) or column:
        return _read_window(path, name, start, limit, budget, force, ctx, column, detailed=detailed)
    loaded = agent_edit.load_text(path)
    lines = _redact_lines(loaded.text.split("\n"))
    if lines and lines[-1] == "":
        lines = lines[:-1]
    total = len(lines)
    version = agent_edit.file_version(path)
    window = _window(limit, total)
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
    if detailed:
        header = f"--- {name} (lines {start}-{end} of {total:,}, eol {eol}, version {version}) ---"
    else:
        header = f"--- {name} (lines {start}-{end} of {total:,}{', crlf' if eol == 'crlf' else ''}) ---"
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
        if state is not None:
            state.versions[name] = agent_edit.content_digest(path)
    out.extend(numbered)
    shown_end = start + len(numbered) - 1
    if numbered and state is not None:
        state.note_read(name, version, start, shown_end)
        state.versions[name] = agent_edit.content_digest(path)
    if numbered and shown_end < total:
        out.append(
            f"... {total:,} lines; next offset={shown_end + 1} limit={DEFAULT_READ_LINES}{capped_note}. "
            "Do not edit a span you have not seen."
        )
    return "\n".join(out)


#: Lines around a window that are redacted with it, so a secret cut by the window's edge
#: (a private key's first lines without its END line) is still recognized.
_REDACT_MARGIN = 60


def _read_window(
    path: Path, name: str, start: int, limit: int | None, budget: int, force: bool, ctx: ToolContext, column: int,
    *, detailed: bool = False,
) -> str:
    """A window of a very large file: read by seeking, not loading; long lines clipped."""
    try:
        index = big_file.line_index(path)
    except OSError as exc:
        return f"--- {name} ---\n{exc}"
    total = index.total
    window = _window(limit, total)
    end = min(total, start + window - 1)
    version = agent_edit.file_version(path)
    state = ctx.state
    if state is not None and not force and total and not column:
        seen = state.seen_span(name, version, start, end)
        if seen is not None:
            return (
                f"--- {name} ---\nlines {start}-{end} are unchanged since step {seen}; "
                "use that text. Pass force true if you no longer have it."
            )
    size = os.path.getsize(path)
    if detailed:
        header = (
            f"--- {name} (lines {start}-{end} of {total:,}, {agent_build.size_text(size)}, "
            f"eol {'crlf' if index.crlf else 'lf'}, version {version}) ---"
        )
    else:
        header = f"--- {name} (lines {start}-{end} of {total:,}, {agent_build.size_text(size)}) ---"
    if start > total:
        return f"{header}\noffset {start} is past the end; the file has {total} lines"
    lead = min(_REDACT_MARGIN, start - 1)
    raw_lines = big_file.read_lines(path, start - lead, lead + (end - start + 1) + _REDACT_MARGIN)
    lines = _redact_lines(raw_lines)[lead : lead + (end - start + 1)]
    out = [header]
    if size > big_file.LARGE_FILE_BYTES and start == 1 and not column:
        if log_tools.looks_like_log(path):
            out.append(f'(a large log: log {{"path": "{name}"}} lists its crashes and errors at once; '
                       "read it by window with offset and limit)")
        else:
            out.append("(a large file: read it by window with offset and limit, or search it with search_code and path)")
    numbered: list[str] = []
    used = 0
    for number, text in enumerate(lines, start):
        piece = f"{number}|{big_file.clip_line(text, column=column)}"
        if numbered and used + len(piece) + 1 > budget:
            break
        numbered.append(piece)
        used += len(piece) + 1
    out.extend(numbered)
    shown_end = start + len(numbered) - 1
    if numbered and state is not None and not column:
        state.note_read(name, version, start, shown_end)
        state.versions[name] = agent_edit.content_digest(path)
    if numbered and shown_end < total:
        out.append(f"... {total:,} lines; next offset={shown_end + 1} limit={DEFAULT_READ_LINES}.")
    return "\n".join(out)


def jira_context(text: str, *, limit: int = 3) -> str:
    """The Jira issues ``text`` names (up to ``limit``), for the task message; "" when Jira is
    not set up or none is found. Fetching never stops a task."""
    from critique_bot import jira

    if not jira.ready():
        return ""
    keys = jira.keys_in(text, limit=limit)
    if not keys:
        return ""
    try:
        return jira.issues_text(keys)
    except Exception:  # noqa: BLE001 - a ticket lookup is a convenience
        return ""


def _jira(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from critique_bot import jira

    if not jira.ready():
        return err("jira", "Jira is not set up; the user runs crit config --jira-url URL and crit config --jira-token")
    issue = str(args.get("issue") or args.get("key") or "").strip()
    jql = str(args.get("jql") or args.get("query") or "").strip()
    try:
        if issue:
            body = jira.format_issue(jira.get_issue(issue))
            return ok("jira", body, ctx, _ui(f"Read {issue.upper()}", lines=body.split("\n")[:2]))
        if jql:
            try:
                limit = int(args.get("limit") or 20)
            except (TypeError, ValueError):
                return err("jira", "limit must be an integer")
            issues = jira.search(jql, limit=limit)
            return ok("jira", jira.format_search(issues), ctx, _ui(f"{len(issues)} Jira issues"))
    except jira.JiraError as exc:
        return err("jira", str(exc))
    return err("jira", "pass issue (a key such as CAR-123) or jql (a search)")


_LOG_ACTIONS = ("summary", "query", "around", "diff")


def _log_path(ctx: ToolContext, raw: Any, what: str = "path") -> tuple[Path | None, str]:
    if not isinstance(raw, str) or not raw.strip():
        return None, f"{what} is required: the log file"
    path = resolve(ctx.workspace, raw)
    if not path.is_file():
        return None, f"not found: {raw}" + _path_hint(ctx, raw)
    if sensitive_file(path):
        return None, f"{raw} usually holds secrets; it is not read as a log"
    return path, ""


def _log(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Logs of any size: what went wrong (summary), filtered lines (query), context (around), good vs bad (diff)."""
    action = str(args.get("action") or "summary").strip().lower()
    action = {"summarize": "summary", "overview": "summary", "grep": "query", "filter": "query", "search": "query",
              "context": "around", "compare": "diff"}.get(action, action)
    if action not in _LOG_ACTIONS:
        return err("log", f"action must be one of {', '.join(_LOG_ACTIONS)}")
    path, problem = _log_path(ctx, args.get("path"))
    if problem:
        return err("log", problem)
    detailed, problem = _response_format(args)
    if problem:
        return err("log", problem)
    name = rel(ctx.workspace, path)
    try:
        if action == "summary":
            summary = log_tools.summarize(path, cancel=ctx.cancel)
            room = max(2_000, ctx.max_chars - 400) if detailed else max(2_000, min(6_000, ctx.max_chars - 400))
            body = log_tools.render_summary(summary, max_chars=room, name=name)
            body += '\nNext: log action "around" with line N for the context of an event, or "query" with level, tag, pid, package or pattern.'
            return ok("log", redact_secrets(body)[0], ctx, _ui(f"Summarized {name}", lines=body.split("\n")[1:4]))
        if action == "around":
            try:
                line = int(args.get("line") or args.get("offset") or 0)
                before = min(max(int(args.get("before") or 20), 0), 200)
                after = min(max(int(args.get("after") or 30), 0), 300)
            except (TypeError, ValueError):
                return err("log", "line, before and after must be integers")
            if line < 1:
                return err("log", "line is required (1-based), for example an event's line from the summary")
            rows = log_tools.around(path, line, before=before, after=after)
            body = f"--- {name} lines {max(1, line - before)}-{line + after} ---\n" + "\n".join(rows)
            return ok("log", _clip_output(redact_secrets(body)[0], ctx), ctx, _ui(f"{name} around line {line}"))
        if action == "diff":
            good, problem = _log_path(ctx, args.get("good"), "good")
            if problem:
                return err("log", problem + " (diff compares a good log, good, with a bad one, path)")
            rows = log_tools.diff(good, path)
            body = f"--- {rel(ctx.workspace, good)} (good) vs {name} (bad) ---\n" + "\n".join(rows)
            return ok("log", _clip_output(redact_secrets(body)[0], ctx), ctx, _ui(f"Compared {name} with a good log"))
        # query
        raw_pattern = args.get("pattern") or args.get("text")
        matcher = None
        if raw_pattern:
            try:
                matcher = re.compile(str(raw_pattern), re.IGNORECASE)
            except re.error:
                matcher = re.compile(re.escape(str(raw_pattern)), re.IGNORECASE)
        pid = str(args.get("pid") or "").strip()
        package = str(args.get("package") or "").strip()
        pids: set[str] = {pid} if pid else set()
        if package:
            pids |= log_tools.pids_for_package(path, package)
            if not pids:
                return ok("log", f"no process of {package} found in {name} (no 'Start proc' or 'PID:' line names it)", ctx)
        try:
            start = max(1, int(args.get("offset") or args.get("start") or 1))
            end = int(args["end"]) if args.get("end") else None
            limit = min(max(int(args.get("limit") or (300 if detailed else 100)), 1), 400)
        except (TypeError, ValueError):
            return err("log", "offset, end and limit must be integers")
        rows, matched, resume = log_tools.query(
            path, level=str(args.get("level") or ""), tag=str(args.get("tag") or ""), pids=pids, text=matcher,
            start=start, end=end, limit=limit, cancel=ctx.cancel,
        )
        head = (
            f"--- {name}: first {len(rows)} matching lines from line {start}; more follow ---" if resume
            else f"--- {name}: {matched} matching lines from line {start} ---"
        )
        tail = f"\n... more: offset={resume}" if resume else ""
        body = head + "\n" + ("\n".join(rows) if rows else "(no matching lines)") + tail
        return ok("log", _clip_output(redact_secrets(body)[0], ctx), ctx, _ui(f"{matched} lines in {name}"))
    except OSError as exc:
        return err("log", f"could not read {name}: {exc}")


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


#: Patterns one search_code call takes (each is searched, results grouped by pattern).
MAX_SEARCH_PATTERNS = 8
#: Files listed with their match counts before the snippets.
SEARCH_FILE_COUNTS = 15
#: Snippets per file with response_format "detailed" (concise keeps HITS_PER_FILE).
DETAILED_HITS_PER_FILE = 15


def _search_code(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_patterns = args.get("patterns")
    if isinstance(raw_patterns, str):
        raw_patterns = [raw_patterns]
    patterns = [item for item in raw_patterns if isinstance(item, str) and item] if isinstance(raw_patterns, list) else []
    single = args.get("pattern") or args.get("query")
    if isinstance(single, str) and single and single not in patterns:
        patterns.insert(0, single)
    if not patterns:
        return err("search_code", 'pattern is required (or patterns: ["a", "b"] to search several at once)')
    dropped = patterns[MAX_SEARCH_PATTERNS:]
    patterns = list(dict.fromkeys(patterns[:MAX_SEARCH_PATTERNS]))
    raw_path = args.get("path") or "."
    if not isinstance(raw_path, str):
        return err("search_code", "path must be a string")
    root = resolve(ctx.workspace, raw_path)
    if not root.exists():
        return err("search_code", f"not found: {raw_path}{_path_hint(ctx, raw_path)}")
    glob = args.get("glob")
    if glob is not None and not isinstance(glob, str):
        return err("search_code", "glob must be a string")
    detailed, problem = _response_format(args)
    if problem:
        return err("search_code", problem)
    try:
        default_limit = 150 if detailed else 50
        limit = min(max(int(args.get("head_limit") or args.get("limit") or default_limit), 1), 200)
        context = min(max(int(args.get("context") or 0), 0), 3)
    except (TypeError, ValueError):
        return err("search_code", "head_limit and context must be integers")
    if len(patterns) == 1:
        found = _search_one(patterns[0], args, ctx, root, raw_path, glob, limit, context, detailed)
        body = found["text"]
        if dropped:
            body += f"\nonly {MAX_SEARCH_PATTERNS} patterns are searched per call"
        return ok("search_code", body, ctx, found["ui"])
    share = max(10, limit // len(patterns))
    sections: list[str] = []
    summary: list[str] = []
    totals = 0
    preview: list[str] = []
    for pattern in patterns:
        found = _search_one(pattern, args, ctx, root, raw_path, glob, share, context, detailed)
        totals += found["total"]
        count, files = found["total"], found["files"]
        summary.append(f"  {pattern!r}: " + (
            f"{count} {'match' if count == 1 else 'matches'} in {files} {'file' if files == 1 else 'files'}"
            if count else "no matches"))
        sections.append(f"== {pattern!r} ==\n{found['text']}")
        preview += [line for line in found["lines"] if not line.startswith("    ")][:2]
    head = f"{len(patterns)} patterns searched:\n" + "\n".join(summary)
    if dropped:
        head += f"\n  (only {MAX_SEARCH_PATTERNS} patterns per call; left out: {', '.join(map(repr, dropped))})"
    return ok(
        "search_code",
        head + "\n\n" + "\n\n".join(sections),
        ctx,
        _ui(f"Found {totals} matches for {len(patterns)} patterns" if totals else f"No matches for {len(patterns)} patterns",
            lines=preview[:5] or None),
    )


def _search_one(
    pattern: str, args: dict[str, Any], ctx: ToolContext, root: Path, raw_path: str, glob: str | None,
    limit: int, context: int, detailed: bool,
) -> dict[str, Any]:
    """One pattern: files and match counts first, then definitions and snippets."""
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
    widen_index(ctx, prefix)
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
    per_file_cap = DETAILED_HITS_PER_FILE if detailed else HITS_PER_FILE
    stats: dict[str, Any] = {"keep": per_file_cap}
    hits, total, files_hit = _text_hits(ctx, root, prefix, source, matcher, ignore_case, glob, literal, pattern, notes,
                                        stats)
    touched = set(ctx.state.reads) | set(ctx.state.edits) if ctx.state else set()
    order = sorted(hits, key=lambda item: (item not in touched, item.count("/"), item))
    shown = 0
    for path in order:
        # Hit lines come from the file after redaction, so a hit inside a key shows no part of it.
        wanted = [line_no for line_no, _text in hits[path][:per_file_cap]]
        full = _redacted_hit_lines(ctx.workspace / path, wanted, context)
        for line_no, text in hits[path][:per_file_cap]:
            if (path, line_no) in seen:
                continue
            if len(lines) >= limit:
                break
            if full is not None and line_no in full:
                text = full[line_no].strip()
            lines.append(f"{path}:{line_no}: {_one_line(text, SNIPPET_CHARS)}")
            shown += 1
            if context and full is not None:
                lines.extend(_context_lines(line_no, context, full))
    remaining = total - shown
    if remaining > 0:
        notes.append(f"{remaining} more hits in {files_hit} files are not shown; narrow with path, glob, or a longer pattern")
    out: list[str] = []
    if total or seen:
        counts = stats.get("per_file") or {}
        head = f"{total} {'match' if total == 1 else 'matches'} in {files_hit} {'file' if files_hit == 1 else 'files'}"
        if seen:
            head += f", {len(seen)} {'definition' if len(seen) == 1 else 'definitions'} (listed first)"
        out.append(head + ":" if files_hit > 1 else head)
        if files_hit > 1:
            listed = sorted(order, key=lambda item: -counts.get(item, len(hits[item])))[:SEARCH_FILE_COUNTS]
            out.extend(f"  {item} ({counts.get(item, len(hits[item]))})" for item in listed)
            if files_hit > len(listed):
                out.append(f"  ... {files_hit - len(listed)} more files")
        out.extend(lines)
    else:
        searched = stats.get("searched")
        where = "" if root == ctx.workspace else f" under {rel(ctx.workspace, root)}"
        scope = f" in {searched:,} files{where}" if isinstance(searched, int) else where
        out.append(f"no matches for {pattern!r}{scope}" + (f" (glob {glob})" if glob else ""))
    body = "\n".join(out + notes)
    found = sum(1 for line in lines if not line.startswith("    "))
    return {
        "text": body,
        "total": total + len(seen),
        "files": files_hit,
        "lines": lines,
        "ui": _ui(f"Found {total + len(seen)} matches" if total or seen else "No matches",
                  lines=lines[:5] if found else None),
    }


# A group that repeats something which itself repeats, e.g. (a+)+ or (\w*\s?)*:
# Python's re can take exponential time on such patterns.
_NESTED_REPEAT_RE = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)[+*{]")


def risky_regex(pattern: str) -> bool:
    """True for nested repetition, which can hang Python's backtracking engine."""
    return bool(_NESTED_REPEAT_RE.search(pattern))


def _context_lines(line_no: int, context: int, text: dict[int, str]) -> list[str]:
    out = []
    for number in range(max(1, line_no - context), line_no + context + 1):
        if number != line_no and number in text:
            out.append(f"    {number}| {_one_line(text[number], SNIPPET_CHARS)}")
    return out


def _redacted_hit_lines(path: Path, line_numbers: list[int], context: int) -> dict[int, str] | None:
    """The hit lines (and their context) of ``path`` after redaction, by line number.

    A normal file is redacted whole. A very large one (a log) is redacted in a window
    around each hit, with the same margin as windowed reads, so it is never loaded whole.
    """
    if not big_file.is_large(path):
        full = _redacted_file_lines(path)
        return None if full is None else {number: text for number, text in enumerate(full, 1)}
    out: dict[int, str] = {}
    try:
        for line_no in sorted(set(line_numbers)):
            if line_no in out:
                continue
            first = max(1, line_no - context - _REDACT_MARGIN)
            raw = big_file.read_lines(path, first, (line_no - first) + context + _REDACT_MARGIN + 1)
            for number, text in enumerate(_redact_lines(raw), first):
                if abs(number - line_no) <= context:
                    out[number] = text
    except OSError:
        return None
    return out


def _text_hits(ctx, root, prefix, source, matcher, ignore_case, glob, literal, pattern, notes=None, stats=None):
    """``{path: [(line, text)]}``, total hit count, and number of files hit.

    ripgrep when it is installed. The Python fallback searches long lines only
    in their first 2000 characters, stops after 20 seconds, and searches a
    pattern with nested repetition as literal text, since Python's regex
    engine cannot be interrupted once it starts backtracking.
    """
    notes = notes if notes is not None else []
    found = _text_hits_all(ctx, root, prefix, source, matcher, ignore_case, glob, literal, pattern, notes, stats)
    if sensitive_file(root):
        return found  # the user said yes to searching this one file
    hits, total, _count = found
    hidden = [item for item in hits if sensitive_file(item)]
    if not hidden:
        return found
    for item in hidden:
        total -= len(hits.pop(item))
        if stats is not None:
            stats.setdefault("per_file", {}).pop(item, None)
    notes.append(
        f"left out matches in {len(hidden)} file(s) that usually hold secrets ({', '.join(hidden[:3])}); "
        "search that file by its path to ask the user"
    )
    return hits, max(total, 0), len(hits)


def _text_hits_all(ctx, root, prefix, source, matcher, ignore_case, glob, literal, pattern, notes, stats=None):
    """Like :func:`_text_hits`; ``stats`` (when given) gets "searched" (files looked at) and
    "per_file" (matches per file, beyond the few kept as snippets)."""
    stats = stats if stats is not None else {}
    per_file = stats.setdefault("per_file", {})
    via_rg = _rg_hits(ctx, root, source, ignore_case, glob, literal, pattern, notes, stats)
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
    searched = 0
    for item in files:
        if glob and not glob_match(item, glob):
            continue
        if looks_binary_path(item):
            continue
        path = ctx.workspace / item
        try:
            size = path.stat().st_size
            if size > _SCAN_MAX_BYTES and not root.is_file():
                continue  # a big file is searched only when named on purpose
            with open(path, "rb") as handle:
                head = handle.read(8192)
        except OSError:
            continue
        if looks_binary_bytes(head):
            continue
        searched += 1
        if time.monotonic() > deadline:
            notes.append(f"search stopped after {_SCAN_SECONDS:.0f}s; narrow with path or glob")
            break
        for number, line in enumerate(_scan_lines(path, size), start=1):
            if number % 50_000 == 0 and time.monotonic() > deadline:
                notes.append(f"search stopped after {_SCAN_SECONDS:.0f}s at line {number}; narrow the pattern")
                break
            if matcher.search(line[:_SCAN_LINE_CHARS]) is None:
                continue
            total += 1
            per_file[item] = per_file.get(item, 0) + 1
            bucket = hits.setdefault(item, [])
            if len(bucket) < stats.get("keep", HITS_PER_FILE):
                bucket.append((number, line.strip()))
        if total > 5_000:
            break
    stats["searched"] = searched
    return hits, total, len(hits)


def _scan_lines(path: Path, size: int):
    """The lines of a file for the Python search: read whole when small, streamed when big."""
    if size <= _SCAN_MAX_BYTES:
        yield from path.read_bytes().decode("utf-8", "replace").splitlines()
        return
    with open(path, "rb") as handle:
        for raw in handle:
            yield raw.decode("utf-8", "replace").rstrip("\r\n")


#: Matches read from rg before it is stopped: enough to rank and show, not to drown.
RG_MAX_MATCHES = 4000
#: Matches taken from one file (a log can match a pattern a million times).
RG_PER_FILE = 200
RG_SECONDS = 90.0


def _rg_skips(ctx: ToolContext) -> list[str]:
    """rg globs for generated trees. A repo tree skips its top-level out/ and prebuilts/
    only: its build/ is source (build/soong), and each project's .gitignore still applies."""
    if file_catalog.catalog_for(ctx.workspace).kind == "repo":
        names = sorted(file_catalog.GIT_SKIP)
        return [arg for name in names for arg in ("-g", f"!{name}/")] + [
            arg for top in file_catalog.REPO_ROOT_SKIP for arg in ("-g", f"!/{top}/")
        ]
    return [arg for name in sorted(SKIP_DIR_NAMES) for arg in ("-g", f"!{name}/")]


def _rg_hits(ctx, root, source, ignore_case, glob, literal, pattern, notes=None, stats=None):
    exe = shutil.which("rg")
    if exe is None or ctx.runner is not None:
        return None
    argv = [exe, "--json", "--hidden", "--max-columns", "400", "--max-count", str(RG_PER_FILE)]
    if not root.is_file():
        argv += ["--max-filesize", "1M"]  # a file named on purpose is searched whatever its size
    if ignore_case:
        argv.append("-i")
    if literal:
        argv += ["-F", "-e", pattern]
    else:
        argv += ["-e", source]
    argv += _rg_skips(ctx)
    if glob:
        argv += ["-g", glob]
    target = rel(ctx.workspace, root) if root != ctx.workspace else "."
    argv += ["--", target]
    try:
        proc = subprocess.Popen(argv, cwd=str(ctx.workspace), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    hits: dict[str, list[tuple[int, str]]] = {}
    total = 0
    stopped = ""
    deadline = time.monotonic() + RG_SECONDS
    try:
        assert proc.stdout is not None
        stats = stats if stats is not None else {}
        per_file = stats.setdefault("per_file", {})
        for raw in proc.stdout:
            if raw.rstrip().endswith(b'"type":"summary"}'):
                try:
                    stats["searched"] = int(json.loads(raw)["data"]["stats"]["searches"])
                except (ValueError, KeyError, TypeError):
                    pass
                continue
            if b'"type":"match"' not in raw[:40]:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            data = event.get("data", {})
            path_text = (data.get("path") or {}).get("text", "")
            if not path_text:
                continue
            item = rel(ctx.workspace, ctx.workspace / path_text)
            text = (data.get("lines") or {}).get("text", "")
            total += 1
            per_file[item] = per_file.get(item, 0) + 1
            bucket = hits.setdefault(item, [])
            if len(bucket) < stats.get("keep", HITS_PER_FILE):
                bucket.append((int(data.get("line_number") or 0), text.strip()))
            if total >= RG_MAX_MATCHES:
                stopped = f"stopped after {total} matches"
                break
            if ctx.cancel is not None and ctx.cancel.is_set():
                stopped = "stopped by the user"
                break
            if time.monotonic() > deadline:
                stopped = f"stopped after {RG_SECONDS:.0f}s"
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait()
    if not stopped and proc.returncode not in (0, 1):
        return None
    if stopped and notes is not None:
        notes.append(f"search {stopped}; there are more. Narrow it with path, glob, or a tighter pattern")
    if notes is not None and any(len(rows) >= HITS_PER_FILE for rows in hits.values()) and root.is_file():
        notes.append(f"at most {RG_PER_FILE} matches are read per file; for logs, filter by a narrower pattern")
    return hits, total, len(hits)


# --------------------------------------------------------------------------- write / edit / delete


def _after_write(ctx: ToolContext, path: Path) -> str:
    name = rel(ctx.workspace, path)
    version = agent_edit.file_version(path)
    if ctx.state is not None:
        ctx.state.note_write(name, version)
        if path.is_file():
            ctx.state.versions[name] = agent_edit.content_digest(path)
        else:
            ctx.state.versions.pop(name, None)
    if not Path(name).is_absolute():
        widen_index(ctx, name)
        catalog = file_catalog.catalog_for(ctx.workspace)
        if path.is_file():
            catalog.note_write(name)
        else:
            catalog.note_delete(name)
    with _INDEX_LOCK:  # helper tabs write at the same time; the index is shared
        if ctx.index_path is not None:
            refresh_path(ctx.workspace, ctx.index_path, path)
    return version


_INDEX_LOCK = threading.Lock()
_WIDENING: set[tuple[str, str]] = set()
_WIDEN_LOCK = threading.Lock()


def widen_index(ctx: ToolContext, rel_path: str, *, wait: bool = False) -> None:
    """On a repo tree, index the project holding ``rel_path`` if it is not yet indexed.

    The index of an AOSP tree covers only the projects the agent works in. The first
    read, search, or edit in a project starts indexing it in the background, so symbol
    lookups there work from the next call on.
    """
    if ctx.index_path is None or not rel_path or Path(rel_path).is_absolute():
        return
    catalog = file_catalog.catalog_for(ctx.workspace)
    if catalog.kind != "repo":
        return
    project = catalog.project_of(rel_path.strip("/"))
    if not project:
        return
    key = (str(ctx.index_path), project)
    with _WIDEN_LOCK:
        if key in _WIDENING:
            return
        _WIDENING.add(key)
    workspace, index_path = ctx.workspace, ctx.index_path

    def run() -> None:
        try:
            code_index.add_scope(workspace, index_path, project)
        except Exception:  # the index is a hint; never break a tool call over it
            with _WIDEN_LOCK:
                _WIDENING.discard(key)

    if wait:
        run()
    else:
        threading.Thread(target=run, name=f"crit-index-{project}", daemon=True).start()


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
    for protected in _protected_paths(ctx):
        if path == protected or inside(protected, path):
            return (
                f"refusing to change {raw}: it holds crit's own settings (permissions, url, allowed hosts). "
                "Ask the user to change it"
            )
    return ""


def _protected_paths(ctx: ToolContext) -> list[Path]:
    """crit's settings, skills, extension files, and the config file named in them: the model may not
    rewrite its own rules."""
    from critique_bot.project_home import legacy_project_dir, project_dir

    from critique_bot.config import crit_home

    own = project_dir(ctx.workspace, migrate=False)
    out = [own / "settings.json", own / "skills"]
    # Extension files (tools, agents, commands, hooks, MCP servers) and the approvals of them,
    # in the project and in ~/.crit: a model that wrote one could give itself a new power.
    extension_names = ("tools", "agents", "commands", "hooks.toml", "hooks.json", "mcp.json")
    out += [own / name for name in extension_names]
    user = crit_home()
    out += [user / name for name in (*extension_names, "trust.json", "mcp-lock.json")]
    # A leftover .bot folder from 1.0.x is protected the same way.
    legacy = legacy_project_dir(ctx.workspace)
    out += [legacy / "settings.json", legacy / "skills"]
    for folder in (own, legacy):
        try:
            raw = json.loads((folder / "settings.json").read_text(encoding="utf-8")).get("config")
        except (OSError, ValueError, AttributeError):
            raw = None
        if isinstance(raw, str) and raw.strip():
            config = Path(raw)
            out.append((config if config.is_absolute() else ctx.workspace / config).resolve())
    return out


def _guard_refusal(ctx: ToolContext, name: str, before: str | None, after: str) -> tuple[str, str]:
    """The parse half of the edit guard, before anything is written.

    Returns ``(refusal, flagged)``: a refusal means "do not write" (a Python/JSON/XML file
    that parsed and would not, or any new syntax error with ``edit_guard: reject``);
    ``flagged`` is a new syntax error to report after writing (``edit_guard: flag``).
    """
    strict = Path(name).suffix.lower() in agent_edit.STRICT_SUFFIXES
    if strict and before is not None:
        after_check = agent_edit.syntax_check(name, after)
        if after_check.startswith("error") and agent_edit.syntax_check(name, before) == "ok":
            return f"it would leave {name} with a syntax {after_check}", ""
    mode = edit_guard.mode(ctx.settings)
    if mode == "off":
        return "", ""
    _parse, problem = edit_guard.new_syntax_error(name, before, after)
    if problem and mode == "reject":
        return f"it would introduce a syntax error in {name}: {problem} (edit_guard is reject)", ""
    return "", problem


def _guard_report(ctx: ToolContext, path: Path, name: str, before: str | None, flagged: str,
                  like: agent_edit.TextFile | None) -> str:
    """The rest of the guard after a write: the flagged syntax error and new lint issues."""
    if edit_guard.mode(ctx.settings) == "off":
        return ""
    report = edit_guard.Report(name, edit_guard.Parse(""), new_syntax=flagged)
    if path.is_file() and not sensitive_file(path):
        report.lint, report.lint_note = edit_guard.lint_new(path, name, before, ctx.settings, ctx.workspace, like=like)
    return report.text()


#: A whole-file write that leaves fewer than this share of a file this long needs overwrite true.
SHRINK_GUARD_LINES = 40
SHRINK_GUARD_SHARE = 0.4


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
        notes: list[str] = []
        contents, numbered = agent_edit.strip_numbered_lines(contents)
        if numbered:
            notes.append("removed the N| line-number prefixes from the contents (they are not part of a file)")
        placeholder = agent_edit.placeholder_line(contents)
        if placeholder is not None and (not existed or agent_edit.placeholder_line(before) != placeholder):
            errors.append(
                f"{name} not written: line {placeholder[0]} is a placeholder for left-out code ({placeholder[1][:60]}). "
                "write_files replaces the whole file, so it must hold every line; change part of a file with edit_file"
            )
            continue
        if (
            existed
            and ctx.state is not None
            and not (overwrite or item.get("overwrite") is True)
            and name in ctx.state.reads
            and ctx.state.reads[name].version != agent_edit.file_version(path)
        ):
            errors.append(
                f"{name} changed on disk since you read it (a helper, a build, or the user changed it). "
                "Read it again and use edit_file, or pass overwrite true to replace it anyway"
            )
            continue
        old_lines = before.count("\n") + (0 if before.endswith("\n") or not before else 1)
        new_lines = contents.count("\n") + (0 if contents.endswith("\n") or not contents else 1)
        if (
            existed
            and not (overwrite or item.get("overwrite") is True)
            and old_lines >= SHRINK_GUARD_LINES
            and new_lines < old_lines * SHRINK_GUARD_SHARE
        ):
            errors.append(
                f"{name} not written: the new contents have {new_lines} lines, the file has {old_lines}. "
                "A whole-file write must hold the whole file (a cut-off or shortened rewrite loses code). "
                "Change part of it with edit_file, or pass overwrite true if the file really should shrink"
            )
            continue
        if REDACTED in contents and REDACTED not in before:
            errors.append(
                f"{name} not written: the contents hold a {REDACTED}...] marker, which stands for a secret the model "
                "never sees. Edit the other lines with edit_file instead of rewriting the file"
            )
            continue
        refusal, flagged = _guard_refusal(ctx, name, before if existed else None, contents)
        if refusal:
            errors.append(f"{name} not {'replaced' if existed else 'written'}: {refusal}")
            continue
        if existed and before and contents:
            # The file's own convention for the last line wins over the model's habit.
            if before.endswith("\n") and not contents.endswith("\n"):
                contents += "\n"
            elif not before.endswith("\n") and contents.endswith("\n") and not contents.endswith("\n\n"):
                contents = contents[:-1]
        if ctx.checkpoints is not None and not ctx.checkpoints.save(path) and existed and sensitive_file(path):
            notes.append(_NO_UNDO_NOTE)
        try:
            agent_edit.save_text(path, contents, like)
        except (ValueError, OSError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        _after_write(ctx, path)
        line_count = new_lines
        summary = f"{'replaced' if existed else 'created'} {name} ({line_count} lines)"
        if existed and old_lines and line_count < old_lines * SHRINK_GUARD_SHARE:
            notes.append(f"the file shrank from {old_lines} to {line_count} lines; undo with crit undo if that was not meant")
        check = agent_edit.syntax_check(name, contents)
        if check:
            summary += f"; syntax {check}"
        if notes:
            summary += "; " + "; ".join(notes)
        guarded = _guard_report(ctx, path, name, before if existed else None, flagged, like)
        if guarded:
            summary += "\n" + guarded
        diff = agent_edit.hunk_diff(before, contents.replace("\r\n", "\n"), name)
        if diff and existed and sensitive_file(path):
            diff = _SECRET_DIFF_NOTE
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


_SECRET_DIFF_NOTE = "(diff not shown: this file usually holds secrets)"
_NO_UNDO_NOTE = "crit undo will not restore it (files that hold secrets are not copied)"


#: After this many failed edits on one file, the result suggests a whole-file write (setting edit_fail_limit).
DEFAULT_EDIT_FAIL_LIMIT = 2
#: Files longer than this get "make smaller edits" instead (setting whole_file_max_lines).
DEFAULT_WHOLE_FILE_MAX_LINES = 800


UNREAD = "unread"


def stale_problem(ctx: ToolContext, path: Path, name: str, old: str = "") -> str:
    """Why an edit of the existing file ``path`` must wait for a (re-)read, or "".

    A file whose bytes changed since the model last read or wrote it (the user, a
    formatter, a build, a command) is refused; the message names the lines to read again.
    A file never read in this task gives :data:`UNREAD`: the caller allows it only when
    every edit matches the file exactly (that proves the model has the text, say from a
    search hit, and saves a turn on a slow local model) and refuses it otherwise, with
    :func:`unread_message`. Without task state (a direct tool call) nothing is refused.
    """
    state = ctx.state
    if state is None or not path.is_file():
        return ""
    known = state.versions.get(name)
    if known is None:
        if name in state.reads or name in state.edits:
            return ""  # read before digests were kept (an old state): trust it
        return UNREAD
    current = agent_edit.content_digest(path)
    if current == known:
        return ""
    run_stats.count("stale_edit_refusals")
    return (
        f"{name} changed on disk since you read it (the user, a formatter, a build, or a command changed it); "
        f"nothing was applied. Re-read {_reread_range(ctx, path, name, old)} and edit again"
    )


def unread_message(name: str) -> str:
    run_stats.count("unread_edit_refusals")
    return (
        f"{name} was not read in this task and old_string does not match its text exactly, so nothing was applied. "
        "Read it first (read_files) and copy old_string from what you read"
    )


def _reread_range(ctx: ToolContext, path: Path, name: str, old: str) -> str:
    """"lines X-Y" around where ``old`` is now, else the spans read before, else the file."""
    try:
        lines = agent_edit.load_text(path).text.split("\n")
    except (OSError, ValueError):
        lines = []
    needle = agent_edit.strip_read_prefix(old or "").strip("\n")
    if needle and lines:
        first = needle.split("\n")[0].strip()
        for index, line in enumerate(lines):
            if first and first in line:
                width = needle.count("\n") + 1
                return f"lines {max(1, index + 1 - 5)}-{min(len(lines), index + width + 5)}"
    record = ctx.state.reads.get(name) if ctx.state is not None else None
    if record is not None and record.spans:
        return "lines " + _spans_text(record.spans)
    return f"{name}" if not lines else f"lines 1-{min(len(lines), 100)}"


def _note_edit_outcome(ctx: ToolContext, name: str, path: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Count failed edits per file; past ``edit_fail_limit`` the result says what to do instead."""
    state = ctx.state
    if state is None or not name:
        return result
    if result.get("ok"):
        state.edit_failures.pop(name, None)
        return result
    count = state.edit_failures.get(name, 0) + 1
    state.edit_failures[name] = count
    limit = ctx.int_setting("edit_fail_limit", DEFAULT_EDIT_FAIL_LIMIT, 1)
    if count < limit:
        return result
    longest = ctx.int_setting("whole_file_max_lines", DEFAULT_WHOLE_FILE_MAX_LINES, 1)
    try:
        lines = agent_edit.load_text(path).text.count("\n") + 1 if path.is_file() else 0
    except (OSError, ValueError):
        lines = 0
    if lines > longest:
        hint = (
            f"{count} edits of {name} have failed. It has {lines} lines, too many to rewrite whole: make smaller "
            "edits, one function or block at a time, with an old_string of 2-6 lines copied from a fresh read_files"
        )
    else:
        hint = (
            f"{count} edits of {name} have failed. Use write_files with the full new content of this file instead "
            "(every line, not only the changed part)"
        )
    result["error"] = f"{result.get('error') or 'edit failed'}. {hint}"
    result["hint"] = "whole_file" if lines <= longest else "smaller_edits"
    return result


def _changed_lines(before: str, after: str) -> str:
    """The new file's changed line ranges, for example "lines 12-18" or "lines 4-5, 40"."""
    matcher = difflib.SequenceMatcher(None, before.split("\n"), after.split("\n"), autojunk=False)
    spans: list[tuple[int, int]] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if j2 > j1:
            spans.append((j1 + 1, j2))
        else:
            spans.append((j1 + 1, j1 + 1) if j1 else (1, 1))
    if not spans:
        return ""
    parts = [f"{a}" if a == b else f"{a}-{b}" for a, b in spans[:6]]
    more = f", ... ({len(spans)} places)" if len(spans) > 6 else ""
    return ("line " if len(spans) == 1 and spans[0][0] == spans[0][1] else "lines ") + ", ".join(parts) + more


def _edit_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw = args.get("path") or args.get("file")
    if not isinstance(raw, str) or not raw:
        return err("edit_file", "path is required")
    path = resolve(ctx.workspace, raw)
    name = rel(ctx.workspace, path)
    result = _edit_file_once(args, ctx, raw, path, name)
    if result.get("counts_as_failure", True):
        result = _note_edit_outcome(ctx, name, path, result)
    result.pop("counts_as_failure", None)
    return result


def _edit_file_once(args: dict[str, Any], ctx: ToolContext, raw: str, path: Path, name: str) -> dict[str, Any]:
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
    if not path.is_file():
        result = err(
            "edit_file",
            f"not found: {raw}. Use write_files to create a new file{_path_hint(ctx, raw)}",
        )
        result["counts_as_failure"] = False
        return result
    problem = _guard_target(ctx, path, raw)
    if problem:
        return err("edit_file", problem)
    expected = args.get("version")
    if isinstance(expected, str) and expected and expected != agent_edit.file_version(path):
        return err("edit_file", f"{name} changed since you read it (version {expected}); read it again")
    first_old = next((str(item.get("old_string") or "") for item in edits if isinstance(item, dict)), "")
    stale = stale_problem(ctx, path, name, first_old)
    if stale and stale != UNREAD:
        result = err("edit_file", stale)
        result["stale"] = True
        result["counts_as_failure"] = False
        return result
    loaded = agent_edit.load_text(path)
    text = loaded.text
    notes: list[str] = []
    steps: list[str] = []
    total = 0
    for number, edit in enumerate(edits, start=1):
        label = f"edit {number} of {len(edits)}: " if len(edits) > 1 else ""
        suffix = "; no edit was applied (the edits in one call apply together or not at all)" if len(edits) > 1 else ""
        if not isinstance(edit, dict):
            return err("edit_file", f"{label or f'edit {number}: '}must be an object{suffix}")
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(old, str) or old == "":
            return err("edit_file", f"{label or f'edit {number}: '}old_string is empty. Use write_files for a new file{suffix}")
        if not isinstance(new, str):
            return err("edit_file", f"{label or f'edit {number}: '}new_string must be a string{suffix}")
        placeholder = agent_edit.placeholder_line(new)
        if placeholder is not None and agent_edit.placeholder_line(old) is None and placeholder[1] not in loaded.text:
            return err(
                "edit_file",
                f"{label or f'edit {number}: '}new_string holds a placeholder for left-out code ({placeholder[1][:60]}). "
                f"Write the real lines; new_string replaces old_string exactly as sent{suffix}",
            )
        result = agent_edit.replace_span(text, old, new, replace_all=bool(edit.get("replace_all")))
        if stale == UNREAD and (result.text is None or result.step != "exact"):
            refused = err("edit_file", label + unread_message(name) + suffix)
            refused["stale"] = True
            refused["counts_as_failure"] = False
            return refused
        if result.text is None:
            output = ""
            if result.candidate and sensitive_file(path):
                output = "(the closest text is not shown: this file usually holds secrets; read it to see it)"
            elif result.candidate:
                output = (
                    "closest text in the file. Copy old_string from it exactly, without the N| prefix:\n"
                    + result.candidate
                )
            run_stats.edit_step("edit_file", "ambiguous" if result.hits else "refused")
            failed = err("edit_file", label + result.note + suffix, output=output, ctx=ctx)
            if result.hits:
                failed["hits"] = list(result.hits)
            return failed
        text = result.text
        total += result.count
        steps.append(result.step)
        if result.note:
            notes.append(result.note)
    if text == loaded.text:
        return err("edit_file", "the edit leaves the file exactly as it was; nothing to change")
    if REDACTED in text and REDACTED not in loaded.text:
        return err(
            "edit_file",
            f"edit not applied: it would write a {REDACTED}...] marker into {name}. That marker stands for a secret "
            "the model never sees; leave the lines that hold it unchanged",
        )
    after_check = agent_edit.syntax_check(name, text)
    diff = agent_edit.hunk_diff(loaded.text, text, name)
    if sensitive_file(path):
        diff = _SECRET_DIFF_NOTE if diff else ""  # its lines would carry the file's secrets to the chat
    refusal, flagged = _guard_refusal(ctx, name, loaded.text, text)
    if refusal:
        run_stats.edit_step("edit_file", "guard_rejected")
        return err("edit_file", f"edit not applied: {refusal}", output=diff, ctx=ctx)
    if ctx.checkpoints is not None and not ctx.checkpoints.save(path) and sensitive_file(path):
        notes.append(_NO_UNDO_NOTE)
    try:
        agent_edit.save_text(path, text, loaded)
    except ValueError as exc:
        return err("edit_file", f"edit not applied: {exc}")
    except OSError as exc:
        return err("edit_file", f"edit not applied: could not write {name} ({exc}); the file is unchanged")
    version = _after_write(ctx, path)
    for step in steps:
        run_stats.edit_step("edit_file", step)
    where = _changed_lines(loaded.text, text)
    message = f"updated {name} ({total} {'change' if total == 1 else 'changes'}" + (f"; now {where}" if where else "") + ")"
    if notes:
        message += "; " + "; ".join(dict.fromkeys(notes))
    if after_check:
        message += f"; syntax {after_check}"
    message += f"; version {version}"
    guarded = _guard_report(ctx, path, name, loaded.text, flagged, loaded)
    if guarded:
        message += "\n" + guarded
    if diff:
        message += "\n" + diff
    plus, minus = _diff_counts(diff)
    done = ok("edit_file", message, ctx, _ui(f"Updated {name} (+{plus} -{minus})", diff))
    done["match"] = steps[0] if len(set(steps)) == 1 else ",".join(steps)
    return done


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
    """Apply a unified diff or V4A patch with crit's own parser (agent_patch), all files or none.

    Line numbers are ignored: each hunk is found by its context through the edit match
    ladder. Nothing is written until every hunk of every file matched; a write that fails
    part way restores the files already written.
    """
    patch = _patch_text(args)
    if not isinstance(patch, str) or not patch.strip():
        return err("apply_patch", "patch is required: one unified diff (or V4A patch) as text")
    patch = patch.replace("\r\n", "\n")
    try:
        files = agent_patch.parse_patch(patch)
    except agent_patch.PatchError as exc:
        return err("apply_patch", str(exc))
    staged: list[dict[str, Any]] = []
    steps: list[str] = []
    for item in files:
        path = resolve(ctx.workspace, item.path)
        name = rel(ctx.workspace, path)
        problem = _guard_target(ctx, path, item.path)
        if problem:
            return err("apply_patch", problem)
        dest = resolve(ctx.workspace, item.move_to) if item.move_to else None
        if dest is not None:
            problem = _guard_target(ctx, dest, item.move_to)
            if problem:
                return err("apply_patch", problem)
            if dest.exists() and dest != path:
                return err("apply_patch", f"{rel(ctx.workspace, dest)} already exists; the patch would move {name} onto it")
        if item.kind == "add":
            if path.is_file() and path.stat().st_size:
                return err("apply_patch", f"{name} already exists; the patch adds it as a new file. Update it instead "
                           "(*** Update File, or a diff against the current text)")
            staged.append({"path": path, "name": name, "like": None, "before": "",
                           "after": agent_patch.new_file_text(item), "existed": False, "dest": None})
            continue
        if not path.is_file():
            return err("apply_patch", f"not found: {item.path}{_path_hint(ctx, item.path)}")
        stale = stale_problem(ctx, path, name, "\n".join(item.hunks[0].old_lines) if item.hunks else "")
        if stale and stale != UNREAD:
            result = err("apply_patch", stale)
            result["stale"] = True
            return result
        loaded = agent_edit.load_text(path)
        if item.kind == "delete":
            if item.hunks:
                try:
                    left = agent_patch.apply_hunks(loaded.text, item)
                except agent_patch.PatchError as exc:
                    return _patch_failure(ctx, path, name, exc)
                if left.text.strip():
                    return err("apply_patch", f"{name}: the patch deletes the file, but its '-' lines do not cover "
                               "all of it; read it again")
            staged.append({"path": path, "name": name, "like": loaded, "before": loaded.text, "after": None,
                           "existed": True, "dest": None})
            continue
        try:
            applied = agent_patch.apply_hunks(loaded.text, item)
        except agent_patch.PatchError as exc:
            if stale == UNREAD:
                return err("apply_patch", unread_message(name))
            return _patch_failure(ctx, path, name, exc)
        if stale == UNREAD and any(step not in {"exact", "insert"} for step in applied.steps):
            return err("apply_patch", unread_message(name))
        steps.extend(applied.steps)
        staged.append({"path": path, "name": name, "like": loaded, "before": loaded.text, "after": applied.text,
                       "existed": True, "dest": dest})
    for entry in staged:
        after = entry["after"]
        if after is None:
            continue
        name = entry["name"]
        if REDACTED in after and REDACTED not in entry["before"]:
            return err("apply_patch", f"patch not applied: it would write a {REDACTED}...] marker into {name}. That "
                       "marker stands for a secret the model never sees; leave the lines that hold it unchanged")
        target_name = rel(ctx.workspace, entry["dest"]) if entry["dest"] is not None else name
        refusal, entry["flagged"] = _guard_refusal(ctx, target_name, entry["before"] if entry["existed"] else None, after)
        if refusal:
            run_stats.edit_step("apply_patch", "guard_rejected")
            return err("apply_patch", f"patch not applied: {refusal}")
    written: list[dict[str, Any]] = []
    try:
        for entry in staged:
            if ctx.checkpoints is not None:
                ctx.checkpoints.save(entry["path"])
                if entry["dest"] is not None:
                    ctx.checkpoints.save(entry["dest"])
            if entry["after"] is None:
                entry["path"].unlink()
            else:
                target = entry["dest"] or entry["path"]
                agent_edit.save_text(target, entry["after"], entry["like"])
                if entry["dest"] is not None and entry["dest"] != entry["path"]:
                    entry["path"].unlink()
            written.append(entry)
    except (OSError, ValueError) as exc:
        for entry in reversed(written):  # put back what was already written
            try:
                if entry["dest"] is not None and entry["dest"] != entry["path"]:
                    entry["dest"].unlink(missing_ok=True)
                if entry["existed"]:
                    agent_edit.save_text(entry["path"], entry["before"], entry["like"])
                else:
                    entry["path"].unlink(missing_ok=True)
            except OSError:
                pass
        return err("apply_patch", f"patch not applied: {exc}; every file is as it was")
    lines: list[str] = []
    diffs: list[str] = []
    for entry in staged:
        _after_write(ctx, entry["path"])
        name = entry["name"]
        if entry["dest"] is not None:
            _after_write(ctx, entry["dest"])
        if entry["after"] is None:
            lines.append(f"deleted {name}")
            continue
        shown = rel(ctx.workspace, entry["dest"]) if entry["dest"] is not None else name
        diff = agent_edit.hunk_diff(entry["before"], entry["after"], shown)
        if sensitive_file(entry["path"]):
            diff = _SECRET_DIFF_NOTE if diff else ""
        where = _changed_lines(entry["before"], entry["after"])
        verb = "created" if not entry["existed"] else ("moved " + name + " ->" if entry["dest"] is not None else "updated")
        line = f"{verb} {shown}" + (f" (now {where})" if where and entry["existed"] else "")
        check = agent_edit.syntax_check(shown, entry["after"])
        if check:
            line += f"; syntax {check}"
        guarded = _guard_report(ctx, entry["dest"] or entry["path"], shown, entry["before"] if entry["existed"] else None,
                                entry.get("flagged", ""), entry["like"])
        if guarded:
            line += "\n" + guarded
        lines.append(line)
        if diff:
            diffs.append(diff)
        if ctx.state is not None:
            ctx.state.edit_failures.pop(name, None)
    for step in steps:
        run_stats.edit_step("apply_patch", step)
    loose = sorted({step for step in steps if step not in {"exact", "insert"}})
    if loose:
        lines.append("some hunks matched loosely (" + ", ".join(loose) + "); check the diff")
    output = "\n".join(lines) + ("\n" + "\n".join(diffs) if diffs else "")
    plus, minus = _diff_counts("\n".join(diffs))
    names = [entry["name"] for entry in staged]
    return ok("apply_patch", output, ctx,
              _ui(f"Patched {', '.join(names[:3])}" + (" ..." if len(names) > 3 else "") + f" (+{plus} -{minus})",
                  "\n".join(diffs)))


def _patch_failure(ctx: ToolContext, path: Path, name: str, exc: "agent_patch.PatchError") -> dict[str, Any]:
    run_stats.edit_step("apply_patch", "refused")
    output = ""
    if exc.candidate and sensitive_file(path):
        output = "(the closest text is not shown: this file usually holds secrets; read it to see it)"
    elif exc.candidate:
        output = "closest text in the file:\n" + exc.candidate
    result = err("apply_patch", f"{exc}; no file was changed", output=output, ctx=ctx)
    return _note_edit_outcome(ctx, name, path, result)


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
    for raw in agent_patch.paths_in(patch):  # V4A headers, and whatever the parser reads
        add(raw, "")
    return paths


# --------------------------------------------------------------------------- commands and git


#: Full outputs kept in .crit/cache/out; older ones are removed.
KEEP_OUTPUTS = tool_output.KEEP_FILES


def _clip_output(body: str, ctx: ToolContext, full_log: str | None = None) -> str:
    """``body`` cut to fit one tool result. When something was cut, the whole text is saved
    under ``.crit/cache/output`` and the result says where, so the middle is not lost.

    ``full_log`` is a complete log the shell already kept (its captured text dropped lines);
    the result then points there, since ``body`` itself is missing the middle.
    """
    clipped = agent_shell.head_tail(body, max_chars=tool_output.budget_chars(ctx.max_chars, ctx.settings) - 300)
    if full_log:
        shown = rel(ctx.workspace, Path(full_log)) if inside(ctx.workspace, Path(full_log)) else full_log
        return (
            f"FULL OUTPUT saved to {shown}: the output below is shortened. "
            "Read the middle with read_files (offset, limit) or search it with search_code (path).\n" + clipped
        )
    if clipped == body or ctx.cache_dir is None:
        return clipped
    saved = _save_output(body, ctx)
    if not saved:
        return clipped
    lines = body.count("\n") + 1
    return (
        f"FULL OUTPUT ({lines} lines) saved to {saved}: only the start and end are shown below. "
        "Read the middle with read_files (offset, limit) or search it with search_code (path).\n" + clipped
    )


def _save_output(body: str, ctx: ToolContext) -> str:
    return tool_output.save_full(body, ctx.cache_dir, ctx.workspace)


def _run_command(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = args.get("command") or args.get("cmd")
    if not isinstance(command, str) or not command.strip():
        return err("run_command", "command is required")
    try:
        timeout = float(args.get("timeout") or ctx.int_setting("shell_timeout_s", int(ctx.command_timeout), 1))
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
    extra_env: dict[str, str] | None = None
    gitlab_note = ""
    if reach and command.lstrip().startswith("git ") and ctx.session is not None:
        # git to one of the GitLab token's projects may use the network even under strict egress
        current = Path(getattr(ctx.session, "cwd", ctx.workspace) or ctx.workspace)
        decision = gitlab_access.git_network_command(command, current)
        if decision.allowed:
            command, extra_env, reach = decision.command, decision.env, ""
        elif integrations.current().gitlab_ready:
            gitlab_note = f" (GitLab: {decision.reason})"
    if reach and str(ctx.network_commands or "block").lower() != "ask":
        return err(
            "run_command",
            f"not run: this command {reach}, and nothing may leave this machine except what crit sends to the model"
            f"{gitlab_note}. git clone, fetch, pull, and push work for the projects your GitLab token can reach. "
            "Read documentation with web_fetch; builds may still download their dependencies",
        )
    if ctx.session is None:
        return _run_plain(args, ctx, command, timeout, wanted, bool(background))
    command, refused = _aosp_defaults(ctx, command)
    if refused is not None:
        return refused
    if not background and agent_build.is_logcat_stream(command):
        background = True  # it never ends by itself; the log tool reads its output file
    return _run_in_session(args, ctx, command, timeout, wanted, bool(background), extra_env=extra_env)


def _yes(answer: Any) -> bool:
    """A person's clear yes ("y", "yes", "allow", "always", "ok"); anything else is no."""
    words = str(answer or "").strip().lower().replace(",", " ").replace(".", " ").split()
    return bool(words) and words[0] in {"y", "yes", "allow", "always", "ok"}


def _sandbox_escapes(ctx: ToolContext, session: Any, result: Any, since: float) -> tuple[bool, str]:
    """After a sandboxed command: ask once per folder or host the sandbox blocked; (run again?, note).

    The answer holds for the rest of the session. Nothing is offered while the session is
    tainted in auto mode, or when no one can answer (the note then says how to allow it in settings).
    """
    policy = session.sandbox
    text = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}"
    folders: list[Path] = []
    for path in sandbox.blocked_paths(text):
        target = sandbox.escape_target(path, policy)
        if target is not None and target not in folders and not policy.can_write(target):
            folders.append(target)
    hosts: list[str] = []
    if getattr(policy, "network", "none") == "proxy":
        from critique_bot import egress_proxy

        proxy = egress_proxy.current()
        if proxy is not None:
            hosts = [item.host for item in proxy.denied_since(since) if "loopback" not in item.reason
                     and "never reachable" not in item.reason and not item.reason.startswith("port ")]
    if not folders and not hosts:
        return False, ""
    blocked = ", ".join([str(item) for item in folders] + hosts)
    if taint.restricted():
        return False, (f"sandbox: blocked {blocked}; not offered to the user because the session is tainted "
                       "(untrusted content was read) and in auto mode")
    # The person decides an escape, also in auto mode (ask_user is answered by the model there).
    ask = ctx.confirm or (ctx.ask_user if taint.state().mode != "auto" else None)
    if ask is None:
        return False, (f'sandbox: blocked {blocked}. To allow it, add the folder to "sandbox": {{"writable": [...]}} '
                       'in .crit/settings.json (hosts: allowed_hosts in config.json)')
    allowed: list[str] = []
    for folder in folders:
        try:
            answer = ask(f"The sandbox stopped this command writing to {folder}. Allow writes to {folder} "
                         "for the rest of this session (crit then runs the command again)? (yes/no)")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        audit.record("permission", tool="sandbox", perm_kind="escape", summary=f"write {folder}", allowed=_yes(answer))
        if _yes(answer) and session.allow_write(folder):
            allowed.append(str(folder))
    for host in hosts:
        try:
            answer = ask(f"The sandbox stopped this command connecting to {host}. Allow {host} "
                         "for the rest of this session (crit then runs the command again)? (yes/no)")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        audit.record("permission", tool="sandbox", perm_kind="escape", summary=f"connect {host}", allowed=_yes(answer))
        if _yes(answer):
            from critique_bot import egress_proxy

            proxy = egress_proxy.current()
            if proxy is not None:
                proxy.allow_host(host)
                allowed.append(host)
    if allowed:
        return True, f"sandbox: the user allowed {', '.join(allowed)} for this session; ran again"
    return False, f"sandbox: blocked {blocked} (the user did not allow it)"


def _aosp_defaults(ctx: ToolContext, command: str) -> tuple[str, dict[str, Any] | None]:
    """In an AOSP tree, make m, mm, mmm and atest just work.

    - envsetup.sh is sourced for them (its functions do not outlive one command; the
      variables lunch sets do, through the session).
    - No lunch target yet: say which targets the tree has, instead of a cryptic failure.
    - ``m`` alone after edits builds the whole platform (hours): name the modules that
      build the changed files first. Running ``m`` again goes ahead.
    """
    program = agent_build.aosp_program(command)
    session = ctx.session
    if not program or session is None:
        return command, None
    envsetup = ctx.workspace / "build" / "envsetup.sh"
    if not envsetup.is_file() or file_catalog.catalog_for(ctx.workspace).kind != "repo":
        return command, None
    if getattr(getattr(session, "shell", None), "kind", "") not in {"bash", "zsh"}:
        return command, None
    try:
        env = session.environment()
    except Exception:  # noqa: BLE001
        env = {}
    if program not in {"lunch", "croot", "printconfig"} and not env.get("TARGET_PRODUCT"):
        choices = soong.lunch_choices(ctx.workspace)
        listed = (" Targets in this tree (car first): " + ", ".join(choices[:8]) + ".") if choices else ""
        return command, err(
            "run_command",
            "not run: no build target is chosen yet. Run lunch <target> once, for example "
            f"lunch {choices[0] if choices else 'sdk_car_x86_64-trunk_staging-userdebug'}; crit keeps it for later "
            f"commands.{listed}",
        )
    if program == "m" and re.fullmatch(r"\s*m(?:\s+-j\s*\d+)?\s*", command) and ctx.state is not None:
        state = ctx.state
        if state.edits and not getattr(state, "full_build_warned", False):
            state.full_build_warned = True
            names: list[str] = []
            for path in list(state.edits)[:8]:
                owner = soong.modules_for_file(ctx.workspace, path)
                if owner is not None and owner.modules and owner.modules[0].name not in names:
                    names.append(owner.modules[0].name)
            if names:
                return command, err(
                    "run_command",
                    "not run: m alone builds the whole platform, which takes hours. The files you changed are built by "
                    f"{', '.join(names)} (from Android.bp): run m {' '.join(names)} instead. "
                    "Run m alone again only if the whole platform is really needed.",
                )
    if "envsetup.sh" in command:
        return command, None
    return f"source {shlex.quote(str(envsetup))} >/dev/null 2>&1 && {command}", None


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


def _session_logs(ctx: ToolContext) -> None:
    """Complete logs of long outputs and jobs go to .crit/cache/output, next to saved outputs."""
    session = ctx.session
    if session is not None and ctx.cache_dir is not None and getattr(session, "log_dir", "unset") is None:
        session.log_dir = Path(ctx.cache_dir) / "output"


def _run_in_session(
    args: dict[str, Any], ctx: ToolContext, command: str, timeout: float, wanted: str, background: bool,
    *, extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    session = ctx.session
    _session_logs(ctx)
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
        if not running and hasattr(session, "mark_reported"):
            session.mark_reported(job_id)  # this result says how it ended; no note later
        body = (
            f"started background job {job_id} ({shell_name}): {state}\n"
            + (first.strip() or "(no output yet)")
            + f"\n{_JOB_NOTE.format(job=job_id)}"
        )
        if agent_build.is_logcat_stream(command) and hasattr(session, "job_info"):
            log_file = session.job_info(job_id).get("log") or ""
            if log_file:
                body += (
                    f'\nlogcat streams until stopped, so it runs in the background. Its output goes to {log_file}: '
                    f'log {{"path": "{log_file}"}} lists crashes and errors so far; stop it with kill_command when done.'
                )
        if ctx.state is not None:
            ctx.state.last_command = command
            ctx.state.last_exit = f"background {job_id}"
        return ok(
            "run_command",
            _clip_output(body, ctx),
            ctx,
            _ui(f"Started {job_id} in background ({shell_name})", lines=_preview(first, tail=True)),
        )
    before = Path(session.cwd)
    started_wall = time.time()

    def once() -> Any:
        kwargs = {"extra_env": extra_env} if extra_env else {}
        return session.run(
            command,
            timeout=timeout,
            shell=wanted or None,
            cwd=cwd,
            on_output=ctx.on_output,
            cancel=ctx.cancel,
            **kwargs,
        )

    escape_mark = time.monotonic()
    try:
        result = once()
    except ValueError as exc:
        return err("run_command", str(exc))
    escape_note = ""
    if getattr(session, "sandbox", None) is not None and not result.interrupted and not getattr(result, "job_id", None):
        again, escape_note = _sandbox_escapes(ctx, session, result, escape_mark)
        if again:
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
    if escape_note:
        head.append(escape_note)
    if retried:
        head.append("retried once: " + "; ".join(retried))
    moved = not _same_dir(session.cwd, before)
    if moved:
        head.append(f"cwd: {session.cwd}")
    elif cwd is not None:
        head.append(f"ran in: {ended} (session cwd stays {before})")
    env_set = tuple(getattr(result, "env_set", ()) or ())
    env_unset = tuple(getattr(result, "env_unset", ()) or ())
    if env_set or env_unset:
        changed = []
        if env_set:
            changed.append("set " + ", ".join(env_set[:12]) + (" ..." if len(env_set) > 12 else ""))
        if env_unset:
            changed.append("unset " + ", ".join(env_unset[:12]))
        head.append("env: " + "; ".join(changed) + " (kept for the next commands)")
    combined = out + "\n" + errs
    preview = _preview(combined, tail=True)
    if result.interrupted:
        body = "\n".join(part for part in ("interrupted by the user", *head[1:], out, errs) if part)
        return err(
            "run_command",
            "interrupted by the user; the command was stopped",
            output=_clip_output(body, ctx),
            ctx=ctx,
            ui=_ui(f"interrupted · {seconds:.1f}s · {shell_name}", lines=preview),
        )
    job_id = getattr(result, "job_id", None)
    if job_id:
        note = (
            f"still running after {timeout:.0f}s, so it was NOT stopped: it continues as background job {job_id}. "
            + _JOB_NOTE.format(job=job_id)
            + " Stop it with kill_command only if it is stuck"
        )
        body = "\n".join(part for part in (note, *head[1:], out, errs) if part)
        return ok(
            "run_command",
            _clip_output(body, ctx),
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
            output=_clip_output(body, ctx),
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
        conflicts = _vcs_conflicts(command, combined, Path(session.cwd))
        if conflicts:
            parts.append(conflicts)
    if out:
        parts.append(out)
    if errs:
        parts.append(errs)
    if not out and not errs:
        parts.append(f"(no output; exit {code})")
    body = _clip_output("\n".join(parts), ctx, getattr(result, "full_log", None))
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


_VCS_CONFLICTING = re.compile(r"\bgit\b.*\b(?:rebase|merge|cherry-pick|pull|revert|stash\s+(?:pop|apply)|am)\b")


def _vcs_conflicts(command: str, output: str, cwd: Path) -> str:
    """After a git command stopped on conflicts: the files, and how to go on."""
    if "CONFLICT" not in output and "could not apply" not in output and "Unmerged" not in output:
        return ""
    if not _VCS_CONFLICTING.search(command):
        return ""
    try:
        proc = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=str(cwd),
                              capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    files = [line for line in proc.stdout.splitlines() if line.strip()]
    if not files:
        return ""
    step = ("git rebase --continue" if "rebase" in command or "pull" in command
            else "git cherry-pick --continue" if "cherry-pick" in command
            else "git revert --continue" if "revert" in command
            else "git am --continue" if " am" in command else "git commit")
    return (
        f"CONFLICTS ({len(files)} files): " + ", ".join(files[:20])
        + "\nEdit each file: keep the right content and remove the <<<<<<<, =======, >>>>>>> lines; then git add "
        f"the files and run {step} (no editor opens). To give up instead: the same command with --abort."
    )


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
            output=_clip_output(body, ctx),
            ctx=ctx,
            ui=_ui(f"timed out after {timeout:.0f}s", lines=_preview(out + "\n" + errs, tail=True)),
        )
    parts = [f"exit {code} ({seconds:.1f}s)", f"cwd: {cwd}"]
    if out:
        parts.append(out)
    if errs:
        parts.append(errs)
    if not out and not errs:
        parts.append(f"(no output; exit {code})")
    body = _clip_output("\n".join(parts), ctx)
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


#: How the model hears about jobs; crit tells it when one ends, so it need not poll.
_JOB_NOTE = (
    "crit will tell you when {job} ends: keep working on something else meanwhile. "
    'To wait for it now, use command_output {{"job_id": "{job}", "until": "exit"}}.'
)
#: Longest single wait in command_output (seconds); the wait ends at once when the job exits.
MAX_JOB_WAIT = 1200.0
DEFAULT_UNTIL_WAIT = 600.0


def _job_ids(session: Any, raw: Any) -> list[str]:
    """``"b2"``, ``2``, ``["b1", "b2"]``, or ``"any"`` (every running job) as job ids."""
    if isinstance(raw, (list, tuple)):
        return [_job_id(session, item) for item in raw if str(item).strip()]
    text = str(raw or "").strip()
    if text.lower() in {"any", "all", "*"}:
        return [str(row.get("id")) for row in session.jobs() if row.get("running")]
    if "," in text:
        return [_job_id(session, item) for item in text.split(",") if item.strip()]
    return [_job_id(session, text)]


def _until_pattern(raw: Any) -> tuple[re.Pattern[str] | None, str]:
    """``until`` as a pattern (None for "exit"), or an error for the model."""
    if raw is None or raw is False:
        return None, ""
    if not isinstance(raw, str):
        return None, 'until must be "exit" or a regular expression such as "BUILD (SUCCESSFUL|FAILED)|error:"'
    text = raw.strip()
    if text.lower() in {"", "exit", "done", "end", "finish", "finished", "exits"}:
        return None, ""
    try:
        return re.compile(text, re.IGNORECASE), ""
    except re.error:
        return re.compile(re.escape(text), re.IGNORECASE), ""


def _job_result(info: dict, ctx: ToolContext, *, why: str = "") -> str:
    """How a finished job went: status, then the key lines (errors, summary, hints), then
    its output shortened, with the complete log for anything cut."""
    code = info.get("exit_code")
    state = f"exit {code}" if code is not None else "stopped"
    command = str(info.get("command") or "")
    combined = (str(info.get("stdout") or "") + "\n" + str(info.get("stderr") or "")).strip()
    parts = [f"{info.get('id')}: {state} after {_duration(float(info.get('seconds') or 0))} · {_one_line(command, 120)}"]
    if why:
        parts.append(why)
    if code not in (0, None) or agent_build.is_build_command(command) or len(combined) > 6_000:
        summary_lines = agent_build.summarize(combined)
        if summary_lines and (code != 0 or len(combined) > 6_000):
            parts.append("SUMMARY (the key lines of the output below):\n" + "\n".join(summary_lines))
        if code not in (0, None):
            found = agent_build.hints(combined)
            if found:
                parts.append("HINTS (from crit, for this failure):\n" + "\n".join(f"- {line}" for line in found))
    parts.append(combined or "(no output)")
    log = info.get("log") if info.get("truncated") else None
    return _clip_output("\n".join(parts), ctx, log)


def finished_job_notes(ctx: ToolContext) -> list[str]:
    """One line per background job that ended since the model last heard of it.

    The engine adds these to the model's next message, so a job never has to be polled.
    """
    session = ctx.session
    if session is None or not hasattr(session, "finished_unreported"):
        return []
    notes = []
    for info in session.finished_unreported():
        code = info.get("exit_code")
        state = f"exit {code}" if code is not None else "stopped"
        combined = str(info.get("stdout") or "") + "\n" + str(info.get("stderr") or "")
        key = agent_build.summarize(combined, max_lines=3) if code not in (0, None) else []
        last = key or [line for line in combined.strip().splitlines() if line.strip()][-2:]
        detail = " | ".join(_one_line(line, 160) for line in last)
        notes.append(
            f"[crit] background job {info.get('id')} finished: {state} after {_duration(float(info.get('seconds') or 0))}"
            f" · {_one_line(str(info.get('command') or ''), 80)}"
            + (f" · {detail}" if detail else "")
            + f' · full result: command_output {{"job_id": "{info.get("id")}"}}'
        )
    return notes


def _command_output(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    session = ctx.session
    if session is None:
        return err("command_output", "there are no background jobs in this session; run_command with background true starts one")
    _session_logs(ctx)
    raw_job = args.get("job_id") if args.get("job_id") is not None else args.get("job_ids")
    if not raw_job:
        rows = session.jobs()
        return ok("command_output", _format_jobs(rows), ctx, _ui(f"{len(rows)} background jobs", lines=_format_jobs(rows).split("\n")[:5]))
    until, problem = _until_pattern(args.get("until"))
    if problem:
        return err("command_output", problem)
    waiting_for = args.get("until") is not None
    try:
        default_wait = DEFAULT_UNTIL_WAIT if waiting_for else 0
        wait = float(args.get("wait") if args.get("wait") is not None else default_wait)
    except (TypeError, ValueError):
        return err("command_output", "wait must be a number of seconds")
    wait = min(max(wait, 0.0), MAX_JOB_WAIT)
    job_ids = _job_ids(session, raw_job)
    if not job_ids:
        return ok("command_output", "no background job is running\n" + _format_jobs(session.jobs()), ctx, _ui("no running jobs"))
    try:
        if waiting_for or len(job_ids) > 1 or wait > 30:
            job_id, why, line = session.wait_jobs(job_ids, until=until, timeout=wait, cancel=ctx.cancel)
        else:
            job_id, why, line = job_ids[0], "", ""
        info = session.job_info(job_id)
    except KeyError as exc:
        return err("command_output", str(exc.args[0] if exc.args else exc))
    if not info.get("running"):
        session.read_background(job_id)  # consume it: the result below has everything
        session.mark_reported(job_id)
        body = _job_result(info, ctx, why=f"matched: {line}" if why == "match" else "")
        state = f"exit {info.get('exit_code')}" if info.get("exit_code") is not None else "stopped"
        return ok("command_output", body, ctx, _ui(f"{job_id} {state}", lines=_preview(str(info.get("stdout") or "") + "\n" + str(info.get("stderr") or ""), tail=True)))
    text, running, code = session.read_background(job_id, wait=0 if why else wait)
    if why == "match":
        head = f"{job_id}: still running; matched: {line}"
    elif why == "timeout":
        head = f"{job_id}: still running after waiting {wait:.0f}s. " + _JOB_NOTE.format(job=job_id)
    elif why == "cancel":
        head = f"{job_id}: still running (the wait was stopped by the user)"
    else:
        head = f"{job_id}: " + ("running" if running else (f"exit {code}" if code is not None else "stopped"))
    body = head + "\n" + (text.strip() or "(no new output)")
    return ok(
        "command_output",
        _clip_output(body, ctx),
        ctx,
        _ui(f"{job_id} running" + (" · matched" if why == "match" else ""), lines=_preview(text, tail=True)),
    )


def _input_reach(text: str) -> str:
    """Why a line sent to a running shell or REPL could send data out, or ""."""
    reach = network_command(text)
    if reach:
        return reach
    if _SCRIPT_NET_RE.search(text):
        return "opens a network connection"
    return ""


def _command_input(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Type into a background job (a shell, REPL, or device shell) and return what it printed."""
    session = ctx.session
    if session is None:
        return err("command_input", "there are no background jobs in this session; start one with run_command and background true")
    if not args.get("job_id"):
        return err("command_input", "job_id is required. Jobs:\n" + _format_jobs(session.jobs()))
    text = args.get("input")
    if text is None:
        text = ""
    if not isinstance(text, str):
        return err("command_input", "input must be text")
    reach = _input_reach(text)
    if reach and str(ctx.network_commands or "block").lower() != "ask":
        return err(
            "command_input",
            f"not sent: this input {reach}, and nothing may leave this machine except what crit sends to the model",
        )
    enter = args.get("enter")
    close = args.get("close") is True or args.get("eof") is True
    try:
        wait = float(args.get("wait") if args.get("wait") is not None else 2)
    except (TypeError, ValueError):
        return err("command_input", "wait must be a number of seconds")
    wait = min(max(wait, 0.0), 30.0)
    job_id = _job_id(session, args.get("job_id"))
    payload = text if (enter is False or not text and close) else text + "\n"
    try:
        session.write_background(job_id, payload, close=close)
        output, running, code = session.read_background(job_id, wait=wait)
        # Closing input usually ends the program: wait for that, not just its first line.
        deadline = time.monotonic() + wait
        while close and running and time.monotonic() < deadline:
            more, running, code = session.read_background(job_id, wait=max(0.0, deadline - time.monotonic()))
            output += more
    except KeyError as exc:
        return err("command_input", str(exc.args[0] if exc.args else exc))
    except ValueError as exc:
        return err("command_input", str(exc))
    state = "running" if running else (f"exit {code}" if code is not None else "stopped")
    body = f"{job_id}: {state}\n" + (output.strip() or "(no new output yet; read more with command_output)")
    return ok("command_input", _clip_output(body, ctx), ctx, _ui(f"{job_id} {state}", lines=_preview(output, tail=True)))


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


# Subcommands that take --no-ext-diff and --no-textconv.
_GIT_DIFFING = {"diff", "show", "log"}


def git_argv(workspace: Path, git_args: list[str]) -> list[str]:
    """The argv for a git call crit makes itself, in ``workspace``.

    The repository's own config could name programs that git runs on a plain
    ``git status`` or ``git diff``: an fsmonitor hook, hooks, a pager, an external
    diff, or a textconv filter. Each is switched off here, so reading the repo
    never runs code from it. Pair it with :func:`_git_environment`.
    """
    hooks = "NUL" if sys.platform == "win32" else "/dev/null"
    argv = [
        "git", "-c", "core.quotepath=off", "-c", "core.fsmonitor=", "-c", f"core.hooksPath={hooks}",
        "-c", "diff.external=", "-c", "core.pager=cat", "--no-pager", "-C", str(workspace),
    ]
    if git_args and git_args[0] in _GIT_DIFFING:
        git_args = [git_args[0], "--no-ext-diff", "--no-textconv", *git_args[1:]]
    return argv + list(git_args)


def _git_names_blob(ctx: ToolContext, rev: Any) -> bool:
    """True when ``rev`` (no ``:path`` part) names a file's contents (a blob), not a commit."""
    if not isinstance(rev, str) or not rev or ":" in rev or rev.startswith("-") or not _REV_RE.fullmatch(rev):
        return False
    try:
        proc = subprocess.run(
            git_argv(ctx.workspace, ["cat-file", "-t", rev]),
            capture_output=True, check=False, timeout=10, env=_git_environment(), stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == b"blob"


def _git(ctx: ToolContext, git_args: list[str], *, tool: str) -> dict[str, Any]:
    run = ctx.runner or subprocess.run
    try:
        proc = run(
            git_argv(ctx.workspace, git_args),
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
    for key in list(env):
        # Set outside crit, these would still run a program or read other config.
        if key.upper() in {"GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_DIR", "GIT_WORK_TREE"}:
            del env[key]
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def is_git_repo(workspace: Path) -> bool:
    return (Path(workspace) / ".git").exists()


_REV_RE = re.compile(r"^[A-Za-z0-9_./~^@{}:-]+$")
_TODO_STATUS = {"pending", "in_progress", "completed", "cancelled"}
_SKILL_ROOTS = (".crit/skills", ".bot/skills", ".agents/skills", ".opencode/skills")


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
            return ok("skill", "no skills. Add SKILL.md under .crit/skills/<name>/.", ctx)
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
    """Project skills (.crit/skills, .agents/skills, .opencode/skills), then the built-in ones.

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
    ``{brief, files, name}`` or of plain strings, or one ``brief`` (or ``task``) at
    the top level. ``files`` (or ``scope``) are the files that helper may change; a
    file can belong to one helper only. Paths must be inside the workspace.
    ``agent`` names a file-defined agent to run the brief (see agent_helpers).
    """
    raw = args.get("tasks")
    for key in ("briefs", "subtasks", "agents", "jobs"):
        if raw is None:
            raw = args.get(key)
    if isinstance(raw, str):
        parsed = _maybe_json(raw)
        raw = parsed if isinstance(parsed, list) else [raw]
    if raw is None and (args.get("brief") or args.get("prompt") or args.get("description") or args.get("task")):
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
            return [], f"task {index} needs a self-contained brief (the helper cannot see this conversation)"
        name = str(item.get("name") or f"helper {index}").strip()[:40] or f"helper {index}"
        files = item.get("files") or item.get("paths") or item.get("scope") or []
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
        brief = {"name": name, "brief": text.strip(), "files": cleaned}
        agent = item.get("agent")
        if isinstance(agent, str) and agent.strip():
            # A file-defined agent runs this brief (agent_helpers): its tools, steps and instructions.
            brief["agent"] = agent.strip()
            if not item.get("name"):
                brief["name"] = f"{agent.strip()[:30]} {index}"
        briefs.append(brief)
    return briefs, ""


def _delegate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    briefs, problem = delegate_briefs(args, ctx)
    if problem:
        return err("delegate", problem)
    if ctx.delegate is None:
        return err(
            "delegate",
            "no helper chats in this session (helper_sessions is 0, or the browser has no remote debugging); "
            "do the work here with the other tools",
        )
    return ctx.delegate(briefs)


# --------------------------------------------------------------------------- web_fetch


# --------------------------------------------------------------------------- secrets

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
    return tuple(ctx.web_hosts or ())


def outbound_problem(url: str, hosts: tuple[str, ...]) -> str:
    """Why fetching ``url`` could send data out, or "" when it is a plain read of an allowed site."""
    if not hosts:
        return (
            "web_fetch is off: nothing leaves this machine but what crit sends to the model. "
            'It reads documentation sites only when .crit/settings.json has "web_fetch": true'
        )
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
            + "); add it to web_fetch_hosts in .crit/settings.json if it is needed"
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
        if urllib.parse.urlsplit(req.full_url).scheme.lower() == "https" and scheme != "https":
            raise urllib.error.URLError(f"redirected from https to plain http ({target}), which is not fetched")
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
    """What the user must approve before ``name`` runs with ``args`` (see :func:`_permission_for`).

    The result also names the tool and its paths, for saved rules, and for a command the
    allow rule a "don't ask again" answer would save.
    """
    args = with_action(name, args)
    perm = _permission_for(name, args, ctx)
    tool = canonical_tool(name) or ""
    perm.tool = tool
    if tool:
        normalized = normalize_args(tool, args if isinstance(args, (dict, str)) else {})
        if tool != "run_command":
            perm.paths = tuple(str(item) for item in _paths_of(tool, normalized, ctx))
        elif perm.kind == "command":
            from critique_bot.agent_rules import suggest_rule

            perm.rule = suggest_rule(str(normalized.get("command") or ""))
        if perm.kind == "command" and tool in {"run_command", "command_input"}:
            policy = getattr(ctx.session, "sandbox", None)
            perm.contained = bool(policy is not None and getattr(policy, "sandboxed", False))
    return perm


def _permission_for(name: str, args: dict[str, Any] | None, ctx: ToolContext) -> Permission:
    """What the user must approve before ``name`` runs with ``args``.

    read: never asks. edit: key "edit". command: key "command:<program>".
    network: key "network:<host>". outside: any path outside the workspace;
    its key is empty, so it is asked every time.
    """
    tool = canonical_tool(name)
    if not tool:
        return Permission("read", f"Unknown tool {name}", "")
    args = normalize_args(tool, args if isinstance(args, (dict, str)) else {})
    paths = _paths_of(tool, args, ctx)
    outside = [item for item in paths if not inside(ctx.workspace, item)]
    if tool in READ_ONLY and ctx.cache_dir is not None:
        # crit's own saved outputs and job logs (of commands already allowed) read freely
        saved = Path(ctx.cache_dir) / "output"
        outside = [item for item in outside if not inside(saved, item)]
    if tool in READ_ONLY:
        # The secret check comes first: ~/.ssh/id_rsa is outside the workspace *and* a secret.
        secret = [item for item in paths if sensitive_file(item)]
        if tool == "git_show":
            secret += [Path(item) for item in _rev_paths(args.get("rev")) if sensitive_file(item)]
            if not secret and _git_names_blob(ctx, args.get("rev")):
                # A bare blob id shows a file's contents with no name to check, so it asks like a secret file.
                secret.append(Path(str(args.get("rev"))))
        if secret:
            shown = ", ".join(rel(ctx.workspace, item) for item in secret[:3])
            return Permission(
                "outside",
                f"Read a file that usually holds secrets: {shown}",
                "",
                _tool_label(tool, args),
                risk=SECRET_RISK,
            )
        if outside:
            shown = ", ".join(str(item) for item in outside[:3])
            return Permission(
                "outside", f"Read outside the workspace: {shown}", "", _tool_label(tool, args), risk=OUTSIDE_READ_RISK
            )
        return Permission("read", _tool_label(tool, args), tool)
    if tool in MUTATING:
        summary = _tool_label(tool, args)
        detail = ""
        if tool == "apply_patch":
            detail = str(_patch_text(args) or "")[:4000]
        elif tool == "edit_file":
            detail = _edit_preview(args)
        secret = [item for item in paths if sensitive_file(item)]
        if secret:
            shown = ", ".join(rel(ctx.workspace, item) for item in secret[:3])
            return Permission("outside", f"{summary} (a file that usually holds secrets: {shown})", "", detail, risk=SECRET_EDIT_RISK)
        if outside:
            shown = ", ".join(str(item) for item in outside[:3])
            return Permission(
                "outside", f"{summary} (outside the workspace: {shown})", "", detail, risk="changes files outside the workspace"
            )
        return Permission("edit", summary, "edit", detail)
    if tool == "run_command":
        command = str(args.get("command") or "").strip()
        label = ("Run in background: " if args.get("background") is True else "Run: ") + _one_line(command, 100)
        if not outside and ctx.session is not None and not args.get("cwd"):
            current = Path(getattr(ctx.session, "cwd", ctx.workspace) or ctx.workspace)
            if not inside(ctx.workspace, current.resolve()):
                outside = [current]
        if outside:
            return Permission(
                "outside", f"{label} (cwd outside the workspace: {outside[0]})", "", command, risk="runs outside the workspace"
            )
        reach = network_command(command)
        if reach:
            return Permission("network", f"{label} (it {reach})", "", command, risk=f"{reach}, which can send data off this machine")
        base = paths[0] if paths else None
        named = _command_secret_paths(command, ctx, base)
        if named:
            return Permission("outside", f"{label} (it names {named[0]})", "", command, risk=SECRET_RISK)
        if read_only_command(command):
            far = _command_outside_paths(command, ctx, base)
            if far:
                return Permission("outside", f"{label} (it reads {far[0]}, outside the workspace)", "", command, risk=OUTSIDE_READ_RISK)
        written = _command_runs_written(command, ctx)
        if written:
            program = command_key(command)
            return Permission(
                "command",
                label,
                f"command:{program}" if program else "",
                command,
                risk=f"runs {written}, which this task wrote or changed; code the model wrote can reach the network",
            )
        program = command_key(command)
        return Permission("command", label, f"command:{program}" if program else "", command)
    if tool == "command_input":
        text = str(args.get("input") or "")
        label = f"Type into {args.get('job_id') or '?'}: " + (_one_line(text, 100) if text.strip() else "(Enter)")
        if not text.strip():
            return Permission("read", label, "")  # Enter or closing input runs nothing new
        reach = _input_reach(text)
        if reach:
            return Permission("network", f"{label} (it {reach})", "", text, risk=f"{reach}, which can send data off this machine")
        named = _command_secret_paths(text, ctx)
        if named:
            return Permission("outside", f"{label} (it names {named[0]})", "", text, risk=SECRET_RISK)
        if read_only_command(text):
            far = _command_outside_paths(text, ctx)
            if far:
                return Permission("outside", f"{label} (it reads {far[0]}, outside the workspace)", "", text, risk=OUTSIDE_READ_RISK)
        program = command_key(text)
        return Permission("command", label, f"command:{program}" if program else "", text)
    if tool == "delegate":
        briefs, problem = delegate_briefs(args, ctx)
        if problem or not briefs:
            return Permission("read", "Delegate", "")
        owned = [name for brief in briefs for name in brief["files"]]
        label = f"Hand to {len(briefs)} helper{'s' if len(briefs) != 1 else ''}"
        detail = "\n\n".join(
            f"{brief['name']}: {_one_line(brief['brief'], 200)}" + (f"\n  may change: {', '.join(brief['files'])}" if brief["files"] else "")
            for brief in briefs
        )
        if not owned:
            return Permission("read", label + " (read only)", "delegate", detail)
        return Permission("edit", label + "; they may change " + ", ".join(owned[:6]) + (" ..." if len(owned) > 6 else ""), "edit", detail)
    if tool == "coverage_locate":
        commands = [str(args.get("command") or "").strip()] + [str(item).strip() for item in (args.get("passing") or [])
                                                               if isinstance(args.get("passing"), list)]
        if isinstance(args.get("passing"), str):
            commands.append(str(args.get("passing")).strip())
        commands = [item for item in commands if item]
        if outside:
            shown = ", ".join(str(item) for item in outside[:3])
            return Permission("outside", f"Coverage: read outside the workspace: {shown}", "", risk=OUTSIDE_READ_RISK)
        if not commands:
            return Permission("read", "Coverage: rank a report or trace", tool)
        perm = _permission_for("run_command", {"command": " && ".join(commands)}, ctx)
        perm.summary = "Coverage: " + perm.summary
        return perm
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


SECRET_RISK = "reads a file that usually holds secrets (its contents go to the model, with known secret formats redacted)"
OUTSIDE_READ_RISK = "reads outside the workspace (the contents go to the model)"
SECRET_EDIT_RISK = "changes a file that usually holds secrets"


def _rev_paths(rev: Any) -> list[str]:
    """``HEAD:.env`` -> ``[".env"]``: the file a git revision names, if it names one."""
    if not isinstance(rev, str) or ":" not in rev or "://" in rev or re.match(r"^[A-Za-z]:[\\/]", rev):
        return []
    return [rev.split(":", 1)[1]] if rev.split(":", 1)[1] else []


def _command_words(command: str) -> list[str]:
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = command.split()
    words: list[str] = []
    for token in tokens:
        for piece in re.split(r"[;&|()<>]+|(?<=\S)=", token):
            piece = piece.strip().strip("\"'`").strip()
            if piece and not piece.startswith("-") and "://" not in piece:
                words.append(piece)
    return words


_GLOB_CHARS = re.compile(r"[*?\[]")
_GLOB_LIMIT = 5000


def _command_cwd(ctx: ToolContext, cwd: Path | None = None) -> Path:
    """The folder a command's relative words are read from: ``cwd``, else the session's cwd."""
    return Path(cwd or getattr(ctx.session, "cwd", ctx.workspace) or ctx.workspace)


def _word_path(word: str, base: Path) -> Path | None:
    """``word`` as an absolute path (``~`` expanded, relative to ``base``), links followed; None if it cannot be one."""
    text = os.path.expanduser(word) if word.startswith("~") else word
    try:
        return Path(os.path.realpath(os.path.join(str(base), text)))
    except (OSError, ValueError):
        return None


def _glob_hits(pattern: str, base: Path) -> list[Path] | None:
    """What a shell glob in ``pattern`` could expand to, hidden files included; None if too many to check.

    Shells and PowerShell differ on whether ``*`` matches dotfiles, so this one does.
    """
    text = os.path.expanduser(pattern) if pattern.startswith("~") else pattern
    path = Path(text)
    current = [Path(path.anchor)] if path.is_absolute() else [base]
    parts = path.parts[1:] if path.is_absolute() else path.parts
    match = fnmatch.fnmatch if _CASE_FOLD else fnmatch.fnmatchcase
    checked = 0
    for part in parts:
        following: list[Path] = []
        for folder in current:
            if not _GLOB_CHARS.search(part):
                if os.path.lexists(folder / part):
                    following.append(folder / part)
                continue
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            checked += len(entries)
            if checked > _GLOB_LIMIT:
                return None
            following.extend(Path(entry.path) for entry in entries if match(entry.name, part))
        current = following
        if not current:
            break
    return current


def _command_secret_paths(command: str, ctx: ToolContext, cwd: Path | None = None) -> list[str]:
    """Words of ``command`` that name, or could name, a file which usually holds secrets.

    ``cat .env``, ``cp ~/.aws/credentials x``, ``git show HEAD:.env``, a link to a
    secret file, and a glob that matches one (``cat .e*``). A glob after a ``cd`` in
    the same command is read from a folder that cannot be known here, so it counts.
    """
    base = _command_cwd(ctx, cwd)
    changes_folder = any(_program_of(part) in _CD_PROGRAMS for part in _SEGMENT_RE.split(command)[::2])
    found: list[str] = []
    for word in _command_words(command):
        text = os.path.expanduser(word) if word.startswith("~") else word
        if sensitive_file(text) or any(sensitive_file(item) for item in _rev_paths(word)):
            found.append(word)
            continue
        if _GLOB_CHARS.search(word):
            hits = None if changes_folder else _glob_hits(word, base)
            if hits is None or any(sensitive_file(item) or sensitive_file(os.path.realpath(item)) for item in hits):
                found.append(word)
            continue
        resolved = _word_path(word, base)
        if resolved is not None and resolved.exists() and sensitive_file(resolved):
            found.append(word)
    return found


def _module_files(command: str) -> list[str]:
    """Files ``python -m pkg.mod`` runs: ``pkg/mod.py``, ``pkg/mod/__main__.py``, and the packages' ``__init__.py``."""
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = command.split()
    out: list[str] = []
    for index, token in enumerate(tokens):
        token = token.strip("\"'")
        if token == "-m" and index + 1 < len(tokens):
            module = tokens[index + 1].strip("\"'")
        elif token.startswith("-m") and len(token) > 2 and re.fullmatch(r"-m[\w.]+", token):
            module = token[2:]
        else:
            continue
        if not re.fullmatch(r"[\w.]+", module):
            continue
        parts = [part for part in module.split(".") if part]
        if not parts:
            continue
        stem = "/".join(parts)
        out += [stem + ".py", stem + "/__main__.py", stem + "/__init__.py"]
        out += ["/".join(parts[:depth]) + "/__init__.py" for depth in range(1, len(parts))]
    return out


def _command_runs_written(command: str, ctx: ToolContext) -> str:
    """A file this task wrote or changed that ``command`` names (``python check.py``, ``python -m check``), or ""."""
    state = ctx.state
    if state is None or not state.edits:
        return ""
    current = Path(getattr(ctx.session, "cwd", ctx.workspace) or ctx.workspace)
    for word in _command_words(command) + _module_files(command):
        try:
            candidates = [resolve(ctx.workspace, word)]
            if not Path(word).is_absolute():
                candidates.append(Path(os.path.abspath(current / word)))
        except (OSError, ValueError, RuntimeError):
            continue
        for path in candidates:
            if inside(ctx.workspace, path) and rel(ctx.workspace, path) in state.edits:
                return word
    return ""


def _command_outside_paths(command: str, ctx: ToolContext, cwd: Path | None = None) -> list[str]:
    """Words of a read-only ``command`` that point outside the workspace (``cat /etc/passwd``, ``cd ..``).

    Flags are skipped, and so are a flag's ``=value`` and words for adb, which name
    paths on the device. A ``/x`` switch that names nothing on disk (``findstr /s``) is a switch.
    """
    base = _command_cwd(ctx, cwd)
    found: list[str] = []
    for segment in _SEGMENT_RE.split(_HARMLESS_REDIRECT.sub(" ", command))[::2]:
        program, args = _split_program(segment)
        if not program or program == "adb":
            continue
        for arg in args:
            value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
            if not value or value.startswith("-") or "://" in value:
                continue
            if re.fullmatch(r"/[A-Za-z?]{1,3}(?::.*)?", value) and not os.path.lexists(value):
                continue
            words = [value] + _rev_paths(value)
            for word in words:
                path = _word_path(word, base)
                if path is not None and not inside(ctx.workspace, path):
                    found.append(word)
                    break
    return found


_CD_PROGRAMS = {"cd", "set-location", "sl", "pushd", "popd", "chdir"}
_WRAPPERS = {"env", "time", "nohup", "command", "exec", "call", "&", "."}
# Programs that run another program; with a flag after them (``env -S``, ``xargs -I``) no key can say what runs.
_FLAG_WRAPPERS = {"env", "time", "nice", "nohup", "timeout", "sudo", "doas", "xargs", "command", "stdbuf", "ionice"}
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEGMENT_RE = re.compile(r"(&&|\|\||[;|&\r\n])")
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
    redirection to a file (``npm test > ~/.bashrc``), so it is asked every time. So does a
    part no program name covers: ``PATH=x ls``, ``env -S ...``, or a filter with an option
    that writes or runs something (``| sort -o ~/.bashrc``).
    """
    if re.search(r"`|\$\(", command) or _has_shell_construct(command):
        return ""
    text = _HARMLESS_REDIRECT.sub(" ", command)
    parts = _SEGMENT_RE.split(text)
    programs: list[str] = []
    separator = ""
    for index, part in enumerate(parts):
        if index % 2:
            separator = part
            continue
        program, args = _split_program(part)
        if not program:
            if part.strip():
                return ""
            continue
        if program in _CD_PROGRAMS:
            continue
        # A filter only counts as harmless when output is piped into it (``| tail``, ``|| true``).
        if separator in {"|", "||"} and program in _FILTERS and _segment_read_only(program, args, part):
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


def _split_program(segment: str) -> tuple[str, list[str]]:
    """``"/usr/bin/git log -5"`` -> ``("git", ["log", "-5"])``: the program and the words after it.

    The program is "" when the segment sets a variable in front (``PATH=tools:/usr/bin ls``,
    ``LD_PRELOAD=x.so cat``), or puts a flag after a wrapper (``env -S``, ``xargs -I``):
    either changes what actually runs, so no program name can stand for it.
    """
    text = segment.strip()
    if not text:
        return "", []
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        tokens = text.split()
    for index, raw in enumerate(tokens):
        token = raw.strip("\"'").lstrip("&").strip()
        if not token:
            continue
        if _ASSIGNMENT_RE.match(token):
            return "", []
        lowered = token.lower()
        base = re.split(r"[\\/]", lowered)[-1]
        for suffix in _EXEC_SUFFIXES:
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base[: -len(suffix)]
                break
        if lowered in _WRAPPERS or base in _FLAG_WRAPPERS:
            following = tokens[index + 1].strip("\"'") if index + 1 < len(tokens) else ""
            if following.startswith("-"):
                return "", []
            if lowered in _WRAPPERS:
                continue
        return base, [item.strip("\"'") for item in tokens[index + 1 :]]
    return "", []


def _program_of(segment: str) -> str:
    return _split_program(segment)[0]


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
        # Host names carry data too: a lookup of <secret>.attacker.example reaches the attacker's DNS server.
        (r"(?:^|[\s;&|({])(?:ping6?|getent|traceroute6?|tracert|tracepath|mtr|pathping|whois|drill|kdig|arping|tnc)(?:\.exe)?(?=\s|$)", "looks up or contacts another host"),
        (r"\[\s*(?:system\.)?net\.dns\s*\]|\bgethostbyname\b|\bgetaddrinfo\b|\bdns\.(?:lookup|resolve)", "sends DNS queries"),
        (r"/dev/(?:tcp|udp)/|\bopenssl\b[^;&|\n]*\bs_client\b|\bnet\.sockets\.|\b(?:tcp|udp)client\b|\bio::socket\b|-msocket\b|\bfsockopen\b|\bnet::http\b|\bopen-uri\b", "opens a network connection"),
        (r"\bfile_get_contents\s*\(\s*['\"]?(?:https?|ftp)://|\bwebbrowser\.", "opens a network connection from a script"),
        (r"(?:python3?|py|node|deno|bun|ruby|perl|php|pwsh|powershell)\b[^\n]*\s-(?:c|e|r|p|command|m\w*)\s[^\n]*(?:__import__|importlib|\bsocket\b|\bhttps?\.(?:get|request)\b|require\(\s*['\"](?:node:)?(?:https?|net|dgram|tls|dns|http2|child_process)['\"])", "opens a network connection from a script"),
        # A web address handed to a browser leaves through the user's own, unfiltered browser.
        (r"(?:^|[\s;&|({])(?:xdg-open|gio\s+open|gnome-open|kde-open\d?|open|start|explorer|sensible-browser|x-www-browser|www-browser|firefox|chrome|google-chrome[\w-]*|chromium[\w-]*|msedge|microsoft-edge[\w-]*|brave[\w-]*|opera|safari|iexplore)(?:\.exe)?\s[^;&|\n]*(?:\w+://|\bwww\.)", "opens a web address in a browser"),
        (r"\bstart-process\b[^;&|\n]*\w+://|\brundll32\b[^;&|\n]*\b(?:url|ieframe|shdocvw)\.dll|(?:^|[\s;&|({])(?:lynx|w3m|elinks|links2?)(?:\.exe)?(?=\s|$)", "opens a web address in a browser"),
        # Windows file shares and network mounts.
        (r"(?:^|[\s\"'=(])\\\\[\w.$-]+\\|\bnet\s+use\b|\bsmbclient\b|\bnew-psdrive\b|\bmount(?:\.cifs|\.nfs|_smbfs)?\b[^;&|\n]*(?:-t\s+(?:cifs|nfs|smb)|//[\w.-]+/)", "reaches a network share"),
        # git to an address the model chose, or with a command git runs to connect.
        (r"\bgit\b[^;&|\n]*(?:\w+://|\s[\w.-]+@[\w.-]+:|\bext::|--upload-pack|--receive-pack|--exec\b|\s-c\s|--config-env|\bremote\s+(?:add|set-url)\b|\bsubmodule\b|\barchive\b[^;&|\n]*--remote|\bconfig\b[^;&|\n]*(?:sshcommand|gitproxy|insteadof|\.url\b|credential|\.proxy|askpass))", "lets git reach an address the model chose"),
        (r"\bgit_(?:ssh|ssh_command|proxy_command|askpass|config_parameters|config_count)\b|\bgit_config_key_", "lets git reach an address the model chose"),
        # Package managers pointed at an address, or installing a package the model named
        # (its install scripts run and can reach anything).
        (r"\b(?:pip3?|pipx|uv|poetry|pdm|conda|mamba)\b[^;&|\n]*(?:\w+://|--index-url|--extra-index-url|(?<!\w)-i\s|--find-links|(?<!\w)-f\s|--trusted-host|\bconfig\s+set\b)", "points a package manager at another address"),
        (r"\b(?:npm|pnpm|yarn|bun)\b[^;&|\n]*(?:\w+://|--registry|\bconfig\s+set\b|\bset\s+registry\b)", "points a package manager at another address"),
        (r"(?:^|[\s;&|({])(?:npx|bunx|pnpx)(?:\.cmd)?\s|\b(?:pnpm|yarn|bun)\s+dlx\b|\bnpm\s+exec\b", "downloads and runs a package"),
        (r"\b(?:npm|pnpm|yarn|bun)(?:\.cmd)?\s+(?:install|i|add)\s+(?:-\S+\s+)*[@\w][^\s]*", "installs a package the model named (its install scripts can reach the network)"),
        (r"\b(?:pip3?|pipx|uv(?:\s+pip)?|python3?\s+-m\s+pip)\s+install\s+(?:-(?!r\b|c\b|e\b|-requirement|-constraint|-editable)\S+\s+)*(?![-.]|/|\\)[A-Za-z][\w.\[\],<>=!~-]*(?:\s|$)", "installs a package the model named (its install scripts can reach the network)"),
        (r"\bgradlew?(?:\.bat)?\b[^;&|\n]*(?:--init-script|(?<!\w)-I\s)|\bmvnw?\b[^;&|\n]*(?:-dremoterepositories|-drepo\.url|-daltdeploymentrepository|dependency:get)|\bgo\s+(?:get|install|mod\s+download)\s+[\w-]+\.[\w.-]+/|\bcargo\s+install\b[^;&|\n]*--git", "fetches from an address the model chose"),
        # Commands that hide what they run cannot be checked.
        (r"\b(?:pwsh|powershell)(?:\.exe)?\b[^\n]*\s-(?:e|ec|en|enc|enco|encod|encode|encoded|encodedcommand)\s", "runs an encoded command that cannot be checked"),
        (r"\bbase64\b[^\n]*\s(?:-d|--decode|-D)\b[^\n]*(?:\|\s*(?:sh|bash|zsh|dash|ksh|python3?|perl|ruby|node|php|pwsh|powershell)\b)|\$\([^)]*\bbase64\b[^)]*(?:-d|--decode|-D)\b|\bfrombase64string\b", "runs decoded text that cannot be checked"),
    )
)


_SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".psm1", ".sh", ".bash", ".rb", ".pl", ".php", ".bat", ".cmd")
_SCRIPT_NET_RE = re.compile(
    r"(?im)\b(?:urllib\.request|urllib3|requests\.(?:get|post|put|patch|request|session)|http\.client|httpx|aiohttp|"
    r"socket\.(?:socket|create_connection)|smtplib|ftplib|paramiko|fetch\(|axios|https?\.(?:request|get)\(|net\.(?:connect|socket|createconnection)|"
    r"xmlhttprequest|websocket|invoke-webrequest|invoke-restmethod|net\.webclient|system\.net\.http|"
    r"(?:^|[\s;&|(])(?:import|from)\s+(?:socket|requests|urllib|urllib3|http|httpx|aiohttp|smtplib|ftplib|telnetlib|paramiko|webbrowser|asyncssh|websockets?)\b|"
    r"importlib|__import__|webbrowser\.|"
    r"require\(\s*['\"](?:node:)?(?:https?|http2|net|dgram|tls|dns)['\"]\s*\)|from\s+['\"](?:node:)?(?:https?|http2|net|dgram|tls|dns)['\"]|"
    r"openssl\s+s_client\b|net\.sockets\.)"
    # These start with a non-word character, so they sit outside the leading \b.
    r"|/dev/(?:tcp|udp)/|\[\s*(?:system\.)?net\.dns\s*\]"
    r"|(?:^|[\s;&|(/\"'])(?:curl|wget|scp|ssh|nc|ncat|ftp|ping|nslookup|dig|getent|xdg-open)\s"
)


def network_script(command: str, workspace: Path, cwd: Path | None = None) -> str:
    """The script a command runs, when that script opens network connections; "" otherwise.

    The command text can look harmless (``python send.py``) while the file it
    runs talks to the network, so the file itself is read (first 256 KB).
    ``python -m tool`` runs ``tool.py`` or ``tool/__main__.py``, and those are read too.
    """
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = command.split()
    base = Path(cwd or workspace)
    for token in tokens + _module_files(command):
        name = token.strip("\"'").lstrip("&<").rstrip(";&|)").strip()
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


def _program_view(text: str) -> str:
    """``text`` with quotes, escapes, and directory prefixes taken off each word.

    ``/usr/bin/curl``, ``"curl"``, ``c\\url``, and ``C:\\Windows\\System32\\curl.exe`` all read as ``curl``.
    """
    words = []
    for word in text.split(" "):
        bare = re.sub(r"[\"'`]", "", word)
        if "://" not in bare and not bare.startswith("\\\\"):
            bare = re.split(r"[\\/]", bare)[-1] if re.search(r"[\\/]", bare.rstrip("\\/")) else bare
            bare = bare.replace("\\", "")
        words.append(bare)
    return " ".join(words)


def network_command(command: str) -> str:
    """Why ``command`` could send data off this machine, or "" when it does not look like it can.

    This is a check for the obvious cases, not a boundary: a command line can hide
    what it does in ways no pattern sees. Commands that are not read-only ask for a
    yes in every mode (see ``Agent._approved``), and an outbound firewall rule is the
    real guarantee.
    """
    text = " ".join(str(command or "").split())
    # A POSIX shell drops quotes and backslash escapes: ``c\\url`` and ``cu''rl`` run curl.
    views = (text, _program_view(text), re.sub(r"[\\\"'`]", "", text))
    for pattern, reason in _NETWORK_PATTERNS:
        if any(pattern.search(view) for view in views):
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
# A program with options that write, run, or widen a search is checked by _OPTION_CHECKS too.
_LOOK_PROGRAMS = {
    "ls", "dir", "gci", "get-childitem", "cat", "type", "gc", "get-content", "head", "tail", "less", "more",
    "grep", "egrep", "fgrep", "rg", "findstr", "select-string", "sls", "find", "fd", "wc", "pwd",
    "get-location", "gl", "which", "where", "get-command", "gcm", "tree", "file", "stat", "du", "df",
    "echo", "write-output", "printenv", "uname", "hostname", "whoami", "test-path", "get-item", "gi",
    "resolve-path", "sort", "uniq", "cut", "diff", "cmp", "md5sum", "sha1sum", "sha256sum", "get-filehash",
    "ps", "get-process", "tasklist", "readlink", "realpath", "basename", "dirname", "jq", "nl", "od", "xxd",
    "hexdump", "strings", "objdump", "readelf", "nm", "aapt", "aapt2", "apkanalyzer", "javap", "ver",
    "tr", "column", "true", "select-object", "select", "out-string", "out-null", "format-table", "ft",
    "format-list", "fl", "measure-object",
}
_GIT_LOOK = {
    "status", "log", "diff", "show", "blame", "grep", "ls-files", "ls-tree", "rev-parse", "describe",
    "shortlog", "reflog", "merge-base", "show-ref", "whatchanged", "rev-list", "name-rev",
}
# Options before the git subcommand that only change how output looks.
_GIT_GLOBAL_OK = {"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs", "--no-replace-objects"}
# Options that write a file, run a program, or point git at another repository. git takes
# unambiguous abbreviations (``--outp=x``), so a prefix of one of these counts too.
_GIT_BAD_OPTIONS = (
    "--output", "--output-directory", "--git-dir", "--work-tree", "--exec-path", "--ext-diff", "--textconv",
    "--open-files-in-pager", "--exec", "--upload-pack", "--receive-pack", "--paginate", "--config-env",
)
_GIT_HARMLESS_SHORT = {"--text"}  # a real option, not an abbreviation of --textconv
_VERSION_FLAGS = {"--version", "-version", "-v", "--help", "-h"}
# Programs that list files: PowerShell pipes their file objects into Select-String and Get-Content, which read them.
_LISTERS = {"ls", "dir", "gci", "get-childitem", "gi", "get-item", "resolve-path", "find", "fd"}
_PIPE_READERS = {"select-string", "sls", "get-content", "gc", "cat", "type"}


def _short_bundle(arg: str) -> str:
    """``-rn`` -> ``"rn"``; "" for a long option, a lone ``-``, or a non-option."""
    if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
        return arg[1:]
    return ""


def _long_name(arg: str) -> str:
    return arg.split("=", 1)[0] if arg.startswith("--") else ""


def _allowed_options(args: list[str], short: str, short_value: str, long: set[str], long_value: set[str]) -> bool:
    """True when every option in ``args`` is a known harmless one (exact long names; no abbreviations).

    ``short_value`` letters take a value (``-k2`` or ``-k 2``); ``long_value`` names take one too
    (``--key=2`` or ``--key 2``). Anything else that starts with ``-`` makes the answer False.
    """
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "--":
            return True
        if arg.startswith("--"):
            name = _long_name(arg)
            if name in long_value:
                skip = "=" not in arg
            elif name not in long:
                return False
            continue
        bundle = _short_bundle(arg)
        if not bundle:
            continue
        for position, letter in enumerate(bundle):
            if letter in short_value:
                skip = position == len(bundle) - 1
                break
            if letter not in short:
                return False
    return True


def _positionals(args: list[str], short_value: str = "", long_value: set[str] | None = None) -> list[str]:
    """The words of ``args`` that are not options or an option's separate value.

    ``short_value`` letters and ``long_value`` names take a value: ``-k 2``, ``-k2``,
    ``--key 2``, ``--key=2``. A single-dash word listed in ``long_value`` (``-cols``) does too.
    """
    long_value = long_value or set()
    out: list[str] = []
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg == "--":
            out.extend(args[index + 1 :])
            break
        if arg.startswith("--"):
            skip = "=" not in arg and _long_name(arg) in long_value
            continue
        if arg.startswith("-") and len(arg) > 1:
            if arg in long_value:
                skip = True
                continue
            bundle = arg[1:]
            for position, letter in enumerate(bundle):
                if letter in short_value:
                    skip = position == len(bundle) - 1
                    break
            continue
        out.append(arg)
    return out


def _looks_like_file(word: str) -> bool:
    """True for a path that names one file (``app.py``), not a folder a search would walk."""
    if os.path.isdir(word) or word.rstrip("\\/") in {"", ".", ".."} or word.endswith(("/", "\\")):
        return False
    return os.path.isfile(word) or bool(re.search(r"[^\\/]\.\w{1,10}$", word))


_RG_SHORT = "iSsnNlcwvFxoHIaU0qbhV"
_RG_SHORT_VALUE = "ABCmegtTEjdrM"
_RG_LONG = {
    "--ignore-case", "--smart-case", "--case-sensitive", "--line-number", "--no-line-number", "--files-with-matches",
    "--files-without-match", "--count", "--count-matches", "--word-regexp", "--invert-match", "--fixed-strings",
    "--line-regexp", "--only-matching", "--with-filename", "--no-filename", "--no-heading", "--heading", "--vimgrep",
    "--column", "--no-column", "--trim", "--json", "--stats", "--multiline", "--multiline-dotall", "--pcre2",
    "--byte-offset", "--null", "--quiet", "--files", "--type-list", "--text", "--binary", "--passthru",
    "--sort-files", "--no-messages", "--follow", "--crlf", "--null-data", "--auto-hybrid-regex", "--line-buffered",
    "--block-buffered", "--no-config", "--debug", "--trace", "--version", "--help", "--engine",
}
_RG_LONG_VALUE = {
    "--after-context", "--before-context", "--context", "--max-count", "--regexp", "--glob", "--iglob", "--type",
    "--type-not", "--max-columns", "--max-depth", "--max-filesize", "--sort", "--sortr", "--color", "--colors",
    "--encoding", "--threads", "--replace", "--context-separator", "--field-match-separator",
    "--field-context-separator", "--path-separator", "--dfa-size-limit", "--regex-size-limit", "--engine",
}
_SORT_SHORT = "bdfgiMhnRrVsuzcCm"
_SORT_SHORT_VALUE = "ktS"
_SORT_LONG = {
    "--ignore-leading-blanks", "--dictionary-order", "--ignore-case", "--general-numeric-sort", "--ignore-nonprinting",
    "--month-sort", "--human-numeric-sort", "--numeric-sort", "--random-sort", "--reverse", "--version-sort",
    "--stable", "--unique", "--zero-terminated", "--check", "--merge", "--debug", "--help", "--version",
}
_SORT_LONG_VALUE = {"--key", "--field-separator", "--buffer-size", "--parallel", "--sort"}
_UNIQ_SHORT = "cdDiuz"
_UNIQ_SHORT_VALUE = "fsw"
_UNIQ_LONG = {"--count", "--repeated", "--all-repeated", "--ignore-case", "--unique", "--zero-terminated", "--group"}
_UNIQ_LONG_VALUE = {"--skip-fields", "--skip-chars", "--check-chars"}
_XXD_VALUE = {"-c", "-cols", "-g", "-groupsize", "-l", "-len", "-n", "-name", "-o", "-offset", "-s", "-seek", "-R"}


def _rg_read_only(args: list[str]) -> bool:
    # --pre runs a program on every file; -z runs decompressors; -u/--hidden/--no-ignore reach .env and
    # ignored files. A search of a folder (or of the cwd, when no path is given) reads every file in it.
    if not _allowed_options(args, _RG_SHORT, _RG_SHORT_VALUE, _RG_LONG, _RG_LONG_VALUE):
        return False
    if "--files" in args or "--type-list" in args:
        return False
    words = _positionals(args, _RG_SHORT_VALUE, _RG_LONG_VALUE)
    pattern_given = any(
        arg in {"-e", "--regexp"} or arg.startswith("--regexp=") or (_short_bundle(arg) and "e" in _short_bundle(arg))
        for arg in args
    )
    paths = words if pattern_given else words[1:]
    return bool(paths) and all(_looks_like_file(item) for item in paths)


_GREP_SHORT_VALUE = "efmABCdD"
_GREP_RECURSIVE = ("--recursive", "--dereference-recursive", "--directories", "--devices")


def _grep_read_only(args: list[str]) -> bool:
    """False for a recursive grep (``-r``, ``-R``, ``-d recurse``): it would read secret files in the tree."""
    for arg in args:
        if arg == "--":
            break
        name = _long_name(arg)
        if name and len(name) > 2 and any(full.startswith(name) for full in _GREP_RECURSIVE):
            return False
        for letter in _short_bundle(arg):
            if letter in "rRd":
                return False
            if letter in _GREP_SHORT_VALUE:
                break
    return True


def _segment_options_ok(program: str, args: list[str]) -> bool:
    """False when ``args`` hold an option that makes ``program`` write, run something, or read a whole tree."""
    if program == "rg":
        return _rg_read_only(args)
    if program in {"grep", "egrep", "fgrep"}:
        return _grep_read_only(args)
    if program == "findstr":
        return not any(re.match(r"^[/-][a-z]*s", arg, re.I) for arg in args)
    if program == "sort":
        return _allowed_options(args, _SORT_SHORT, _SORT_SHORT_VALUE, _SORT_LONG, _SORT_LONG_VALUE)
    if program == "uniq":
        # A second file name is where uniq writes.
        return _allowed_options(args, _UNIQ_SHORT, _UNIQ_SHORT_VALUE, _UNIQ_LONG, _UNIQ_LONG_VALUE) and len(
            _positionals(args, _UNIQ_SHORT_VALUE, _UNIQ_LONG_VALUE)
        ) <= 1
    if program == "find":
        return not any(arg.lower().startswith(("-exec", "-ok", "-fprint", "-fls", "-delete")) for arg in args)
    if program == "fd":
        return not any(
            _long_name(arg).startswith("--exec") or re.search(r"[xX]", _short_bundle(arg)) for arg in args
        )
    if program == "xxd":
        # -r turns a dump back into bytes, and a second file name is where xxd writes.
        return not any(re.match(r"^-+r", arg) for arg in args) and len(_positionals(args, "", _XXD_VALUE)) <= 1
    if program == "tree":
        # -o writes the listing to a file; -R with -H writes an HTML page into every folder.
        return not any(
            arg.startswith(("--o", "--fromfile", "--fromtabfile")) or re.search(r"[oRH]", _short_bundle(arg)) for arg in args
        )
    if program == "less":
        return not any(arg.startswith("+") or re.search(r"[oO]", _short_bundle(arg)) or _long_name(arg).lower().startswith("--log") for arg in args)
    if program == "diff":
        # -r and -N print whole files, folders of them with -r.
        return not any(
            re.search(r"[rN]", _short_bundle(arg))
            or any(name.startswith(_long_name(arg)) for name in ("--recursive", "--new-file", "--unidirectional-new-file") if len(_long_name(arg)) > 2)
            for arg in args
        )
    if program == "file":
        return not any(re.search(r"C", _short_bundle(arg)) or _long_name(arg).startswith("--comp") for arg in args)
    if program in {"aapt", "aapt2"}:
        words = _positionals(args)
        return bool(words) and words[0] in {"dump", "version", "d", "v"}
    return True


def _git_read_only(args: list[str]) -> bool:
    """True for a git command that only reads this repository (``git log``, ``git config --get x``)."""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] not in _GIT_GLOBAL_OK:
            return False  # -C, -c, --git-dir, --work-tree, --exec-path, -p, ...
        index += 1
    if index >= len(args):
        return False
    sub, rest = args[index].lower(), args[index + 1 :]
    for arg in rest:
        if arg == "--":
            break
        name = _long_name(arg)
        if name and name not in _GIT_HARMLESS_SHORT and len(name) > 2 and any(
            name == bad or bad.startswith(name) for bad in _GIT_BAD_OPTIONS
        ):
            return False
        if arg.startswith("-O"):
            return False  # git grep -O opens a pager; diff -O reads an order file
    words = [arg.lower() for arg in rest if not arg.startswith("-")]
    flags = set(rest)
    if sub in _GIT_LOOK:
        return not (sub == "reflog" and words and words[0] in {"delete", "expire"})
    if sub == "config":
        return rest in (["--list"], ["-l"]) or (
            len(rest) == 2 and rest[0] in {"--get", "--get-all"} and not rest[1].startswith("-")
        )
    if sub == "remote":
        return not words or (words[0] == "get-url" and len(words) == 2)
    if sub == "stash":
        return bool(words) and words[0] == "list"
    if sub in {"branch", "tag"}:
        names = {arg.split("=", 1)[0] for arg in flags}
        if names & {"-d", "-D", "--delete", "-m", "-M", "-c", "-C", "-f", "--force", "-u", "--set-upstream-to",
                    "--unset-upstream", "--edit-description", "-s", "-e", "--copy", "--move"}:
            return False
        if sub == "tag" and words and not flags & {"-l", "--list"}:
            return False
        if sub == "branch" and words and not flags & {"-l", "--list", "--contains", "--merged", "--no-merged", "--points-at"}:
            return False
        return True
    return False


def _dumps_environment(program: str, args: list[str]) -> bool:
    """True for commands that print environment variables: ``printenv``, ``gci env:``, ``jq -n env``, ``/proc/*/environ``."""
    lowered = [arg.lower() for arg in args]
    if program == "printenv":
        return True
    if any(re.match(r"^(?:/proc/[^/]+/environ|env:.*|\$env:.*)$", arg) for arg in lowered):
        return True
    if program == "jq" and any(re.search(r"\benv\b|\$env\b|\$__loc__", arg) for arg in lowered):
        return True
    return False


def _segment_read_only(program: str, args: list[str], segment: str = "") -> bool:
    """True when one part of a command (``program`` and its ``args``) only reads."""
    if any("$" in arg or "`" in arg for arg in args):
        return False  # a variable can expand to any path or option
    if _dumps_environment(program, args):
        return False  # environment variables hold tokens (CI, cloud, GitLab); ask first
    bare =not re.search(r"[\\/]", (segment.strip().split() or [""])[0])
    if args and bare and all(arg.lower() in _VERSION_FLAGS for arg in args):
        return True
    if program == "git":
        return _git_read_only(args)
    if program == "repo":
        words = [arg.lower() for arg in args if not arg.startswith("-")]
        return bool(words) and words[0] in {"status", "info", "branches", "list", "diff", "overview", "manifest"} and not any(
            arg == "-o" or arg.startswith("--output") for arg in args
        )
    if program == "adb":
        joined = " ".join(arg.lower() for arg in args)
        return bool(
            re.match(r"^(?:-s \S+ )?(?:devices|get-state|get-serialno|logcat -d\b|shell (?:getprop|dumpsys|pm list|cmd package list|ls|cat|ps|df|id|uname|wm size|wm density|settings get))", joined)
        )
    if program not in _LOOK_PROGRAMS:
        return False
    return _segment_options_ok(program, args)


def read_only_command(command: str) -> bool:
    """True when ``command`` only reads: ``ls``, ``git log``, ``grep x a.py``, ``Get-Content a.txt``, ``x --version``.

    Every part of a chain is checked, filters after a pipe included, and each program's
    options too: ``rg --pre``, ``sort -o``, ``find -exec``, ``git log --output``, and a
    variable in front (``PATH=x ls``) are not read-only. A recursive search (``grep -r``,
    ``rg`` on a folder) is not either: it would read secret files too. Paths are not
    checked here; :func:`permission_for` asks for ones outside the workspace or secret.
    """
    text = str(command or "").strip()
    if not text or risky_command(text):
        return False
    if not command_key(text):
        return False
    previous = ""
    separator = ""
    parts = _SEGMENT_RE.split(_HARMLESS_REDIRECT.sub(" ", text))
    for index, part in enumerate(parts):
        if index % 2:
            separator = part
            continue
        if not part.strip():
            continue
        program, args = _split_program(part)
        if not program:
            return False
        if program in _CD_PROGRAMS:
            if any("$" in arg for arg in args):
                return False
            previous = program
            continue
        if separator == "|" and program in _PIPE_READERS and previous in _LISTERS:
            return False  # Get-ChildItem -Recurse | Select-String reads every file it lists
        if not _segment_read_only(program, args, part):
            return False
        previous = program
    return True


_INDEX_PATH_TOOLS = frozenset({"code_nav", "code_history", "find_strings", "coverage_locate", "locate"})


def _paths_of(tool: str, args: dict[str, Any], ctx: ToolContext) -> list[Path]:
    """Resolved paths a call would read or change (for the outside-workspace check)."""
    raws: list[Any] = []
    if tool in {"list_files", "find_files", "search_code", "delete_file", "edit_file", "git_diff", "git_log", "git_show"}:
        raws.append(args.get("path"))
    elif tool == "log":
        raws += [args.get("path"), args.get("good")]
    elif tool in _INDEX_PATH_TOOLS:
        from critique_bot.indexing import tools as index_tools

        raws += index_tools.paths_of(tool, args)
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
        return f"Hand to {count} helper{'s' if count != 1 else ''}"
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
    "move_file": _move_file,
    "web_fetch": _web_fetch,
    "ask_user": _ask_user,
    "command_output": _command_output,
    "command_input": _command_input,
    "kill_command": _kill_command,
    "delegate": _delegate,
    "log": _log,
    "jira": _jira,
}


def _index_tool(name: str) -> Callable[[dict[str, Any], ToolContext], dict[str, Any]]:
    """A handler that runs one index tool (critique_bot.indexing.tools) and wraps its text."""

    def run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        from critique_bot.indexing import tools as index_tools

        done, text = index_tools.run(name, args, ctx)
        return ok(name, text, ctx) if done else err(name, text, ctx=ctx)

    return run


_HANDLERS.update({name: _index_tool(name) for name in ALLOWED_TOOLS if name not in _HANDLERS})
