"""The tool layer's interface: what the engine and the front ends may use from the tools.

Two parts:

- :class:`ToolRegistry`: the tools a model can call. Each has a :class:`ToolSpec` (name,
  description, JSON Schema for its arguments, whether it only reads), plus ``permission``
  and ``execute``. The specs export as OpenAI ``tools`` (:meth:`ToolSpec.to_openai`) and as
  MCP tool definitions (:meth:`ToolSpec.to_mcp`), so a backend with native tool calling,
  or an MCP client or server, uses the same definitions as the text protocol. The engine
  dispatches every call through the installed registry (:func:`use_registry`), so the
  built-in tools can be replaced, wrapped, or extended without touching the loop.
- The shared types and helpers the engine needs from the tools (task state, the tool
  context, permissions, secret redaction, skills), re-exported here so callers do not
  import the implementation module :mod:`critique_bot.agent_tools`.

The workspace services the engine drives directly (shell sessions in ``agent_shell``,
edit checkpoints in ``agent_edit``, permission rules in ``agent_rules``, the symbol index
in ``code_index``, build defaults in ``agent_build``, and ``.crit`` state in ``project_home``)
are part of this layer's interface too; see ``tests/test_layers.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from critique_bot import agent_tools as _impl

# --------------------------------------------------------------------------- shared types
# The engine's view of the tool layer. Everything here is defined in agent_tools.
ALLOWED_TOOLS = _impl.ALLOWED_TOOLS
MUTATING = _impl.MUTATING
READ_ONLY = _impl.READ_ONLY
DEFAULT_COMMAND_TIMEOUT = _impl.DEFAULT_COMMAND_TIMEOUT
DEFAULT_TOOL_CHARS = _impl.DEFAULT_TOOL_CHARS
DEFAULT_WEB_HOSTS = _impl.DEFAULT_WEB_HOSTS
OUTSIDE_READ_RISK = _impl.OUTSIDE_READ_RISK
REDACTED = _impl.REDACTED
TaskState = _impl.TaskState
ToolContext = _impl.ToolContext
Permission = _impl.Permission
canonical_tool = _impl.canonical_tool
finished_job_notes = _impl.finished_job_notes
jira_context = _impl.jira_context
normalize_args = _impl.normalize_args
fill = _impl.fill
is_git_repo = _impl.is_git_repo
discover_skills = _impl.discover_skills
select_skills = _impl.select_skills
skill_text = _impl.skill_text
read_only_command = _impl.read_only_command
risky_command = _impl.risky_command
redact_secrets = _impl.redact_secrets
rel = _impl.rel
resolve = _impl.resolve
_paths_of = _impl._paths_of


# --------------------------------------------------------------------------- specs

def _obj(properties: dict[str, Any] | None = None, required: Iterable[str] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = list(required)
    return schema


_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}


def _s(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _i(description: str, minimum: int | None = None, maximum: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "integer", "description": description}
    if minimum is not None:
        item["minimum"] = minimum
    if maximum is not None:
        item["maximum"] = maximum
    return item


def _b(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


@dataclass(frozen=True)
class ToolSpec:
    """One tool as a model sees it. ``parameters`` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=_obj)
    read_only: bool = False

    def to_openai(self) -> dict[str, Any]:
        """An OpenAI Chat Completions / Responses ``tools`` entry."""
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }

    def to_mcp(self) -> dict[str, Any]:
        """An MCP ``tools/list`` entry."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
            "annotations": {"readOnlyHint": self.read_only},
        }


_PATH = _s("path relative to the workspace")

BUILTIN_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "list_files",
        "List one folder, folders first with file counts. Generated trees (build, out, node_modules) are skipped.",
        _obj({"path": _PATH, "depth": _i("levels to list", 1, 4), "glob": _s("only names matching"),
              "max_entries": _i("cap on entries", 1)}),
        read_only=True,
    ),
    ToolSpec(
        "find_files",
        "Find files by name at any depth, newest first. A glob without a slash matches the file name anywhere.",
        _obj({"glob": _s('for example "**/*Service.java" or "build.gradle*"'), "path": _PATH,
              "limit": _i("cap on results", 1)}, ["glob"]),
        read_only=True,
    ),
    ToolSpec(
        "read_files",
        "Read up to 20 files. Lines come back as N|text; at most 400 lines per file per call. "
        "Pass symbol to read one function or class.",
        _obj({
            "paths": {"type": "array", "maxItems": 20, "items": {"anyOf": [
                _S, _obj({"path": _S, "offset": _I, "limit": _I, "symbol": _S}, ["path"])]}},
            "path": _PATH,
            "offset": _i("first line, 1-based", 1),
            "limit": _i("lines to read", 1, 400),
            "symbol": _s("a class or function, e.g. OrderService.place"),
            "force": _b("read again even when unchanged since an earlier read"),
            "col": _i("start each line at this character (to read on in a very long, clipped line)", 0),
        }),
        read_only=True,
    ),
    ToolSpec(
        "search_code",
        "Search code with a regular expression. Definitions come first, then text hits, at most 5 per file.",
        _obj({"pattern": _s("regular expression (or text with literal true)"), "path": _PATH,
              "glob": _s("only files matching"), "head_limit": _i("cap on hits", 1),
              "context": _i("lines around each hit", 0, 3), "literal": _b("match the text exactly"),
              "case_insensitive": _b("ignore case")}, ["pattern"]),
        read_only=True,
    ),
    ToolSpec(
        "write_files",
        "Create or replace whole files; parent folders are created. Replacing a file not read in this task "
        "needs overwrite true.",
        _obj({"files": {"type": "array", "items": _obj({"path": _S, "contents": _S}, ["path", "contents"])},
              "path": _PATH, "contents": _S, "overwrite": _b("replace a file not read in this task")}),
    ),
    ToolSpec(
        "edit_file",
        "Replace text in one file. old_string must match one place (copy it without the N| prefix); "
        "several changes go in edits and apply together.",
        _obj({"path": _PATH, "old_string": _S, "new_string": _S, "replace_all": _B,
              "edits": {"type": "array", "items": _obj({"old_string": _S, "new_string": _S, "replace_all": _B},
                                                        ["old_string", "new_string"])}}, ["path"]),
    ),
    ToolSpec("delete_file", "Remove one file.", _obj({"path": _PATH}, ["path"])),
    ToolSpec(
        "move_file",
        "Move or rename one file; a folder destination keeps the name.",
        _obj({"path": _PATH, "destination": _PATH, "overwrite": _b("replace an existing destination")},
             ["path", "destination"]),
    ),
    ToolSpec("apply_patch", "Apply one unified diff.", _obj({"patch": _s("unified diff")}, ["patch"])),
    ToolSpec(
        "run_command",
        "Run one command like a terminal; the working folder and environment carry over between calls. "
        "background true starts a server or watcher and returns a job_id.",
        _obj({"command": _S, "timeout": _i("seconds", 1, 600), "cwd": _PATH,
              "shell": {"type": "string", "enum": ["bash", "sh", "zsh", "pwsh", "powershell", "cmd", "gitbash"]},
              "background": _b("run in the background")}, ["command"]),
    ),
    ToolSpec(
        "command_output",
        "Output and status of a background command; omit job_id to list jobs. crit reports a job's end "
        "on its own; until waits (no turns spent) for the job to exit or print a matching line.",
        _obj({"job_id": _S,
              "until": {"type": "string",
                        "description": '"exit", or a regular expression such as "BUILD (SUCCESSFUL|FAILED)|error:"'},
              "wait": _i("seconds to wait (up to 1200 with until; the wait ends when the job exits)", 0, 1200)}),
        read_only=True,
    ),
    ToolSpec(
        "command_input",
        "Type one line into a background command (a shell, REPL, debugger, or device shell).",
        _obj({"job_id": _S, "input": _S, "enter": _b("press Enter after the input (default true)"),
              "wait": _i("seconds to wait for its answer", 0, 30), "close": _b("end its input, like Ctrl+D")},
             ["job_id"]),
    ),
    ToolSpec("kill_command", "Stop a background command and its children.", _obj({"job_id": _S}, ["job_id"]),
             read_only=True),
    ToolSpec(
        "jira",
        "Read Jira (never changes it): one issue by key (summary, status, description, links, recent comments), "
        "or issues matching a JQL search.",
        _obj({"issue": _s("an issue key such as CAR-123"), "jql": _s("a JQL search, e.g. project = CAR AND status = Open"),
              "limit": _i("search: issues to list", 1, 50)}),
        read_only=True,
    ),
    ToolSpec(
        "log",
        "Analyze a log of any size (logcat, dmesg, bugreport, build output). summary: crashes, ANRs, errors "
        "grouped with counts and line numbers. query: lines by level/tag/pid/package/pattern. around: lines "
        "near a line. diff: what is new in a bad log (path) against a good one (good).",
        _obj({"action": {"type": "string", "enum": ["summary", "query", "around", "diff"]},
              "path": _PATH, "good": _s("diff: the good log"),
              "level": _s("query: lowest level, e.g. W or E"), "tag": _S, "pid": _S,
              "package": _s("query: an app package; its processes' lines"),
              "pattern": _s("query: a regular expression"), "line": _i("around: the line", 1),
              "offset": _i("query: first line", 1), "limit": _i("query: lines to return", 1, 400)},
             ["path"]),
        read_only=True,
    ),
    ToolSpec("git_status", "The git status of the workspace.", _obj(), read_only=True),
    ToolSpec("git_diff", "The git diff of the workspace.",
             _obj({"path": _PATH, "staged": _B, "stat": _b("summary only")}), read_only=True),
    ToolSpec("git_log", "Recent commits.", _obj({"limit": _i("commits", 1, 50), "path": _PATH}), read_only=True),
    ToolSpec("git_show", "One commit, branch, or HEAD.",
             _obj({"rev": _S, "path": _PATH, "patch": _b("include the diff")}), read_only=True),
    ToolSpec(
        "todo",
        "The task list for a job with several steps; omit todos to read it back. One item is in_progress.",
        _obj({"todos": {"type": "array", "items": _obj({
            "content": _S,
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
        }, ["content", "status"])}}),
        read_only=True,
    ),
    ToolSpec("skill", "Load one SKILL.md; omit name to list the skills.", _obj({"name": _S}), read_only=True),
    ToolSpec(
        "web_fetch",
        "Read one documentation page as text, only on the sites the environment allows.",
        _obj({"url": _s("https URL with no query string"), "max_chars": _i("cap on text", 1),
              "offset": _i("characters to skip", 0)}, ["url"]),
    ),
    ToolSpec(
        "ask_user",
        "Ask the user one question when the task cannot go on without their decision.",
        _obj({"question": _S, "options": {"type": "array", "items": _S}}, ["question"]),
        read_only=True,
    ),
    # ---- the index tools (Stage 8, critique_bot.indexing.tools): structure, history, evidence
    ToolSpec(
        "find_symbol",
        "Where a class, function or method is defined: path:line-end, kind, signature; exact names first, then "
        "names that contain it. Not for text in bodies (search_code). Example: {\"name\": \"CarPropertyService.setProperty\"}.",
        _obj({"name": _s("a name, Parent.name, or part of a name"),
              "kind": _s("class, interface, function, method, struct, enum, ..."),
              "path": _s("only under this folder, or in this build module"), "limit": _i("cap on results", 1, 200)},
             ["name"]),
        read_only=True,
    ),
    ToolSpec(
        "symbol_refs",
        "Every use of a name, grouped by file with line numbers (definitions included). Use before renaming or "
        "changing a signature. Example: {\"name\": \"setFanSpeed\"} or {\"name\": \"src/Fan.kt:42\"}.",
        _obj({"name": _s("a name, Parent.name, or path:line of its definition"), "path": _s("only under this folder"),
              "limit": _i("files to list", 1, 200)}, ["name"]),
        read_only=True,
    ),
    ToolSpec(
        "who_calls",
        "Functions that call a symbol, best matches first; depth 2 adds their callers. Calls match by name, so "
        "check them (symbol_refs lists every use). Example: {\"symbol\": \"HvacController.setTemp\", \"depth\": 2}.",
        _obj({"symbol": _s("function or Parent.method"), "depth": _i("1 or 2", 1, 2), "path": _s("its file or folder"),
              "limit": _i("cap", 1, 200)}, ["symbol"]),
        read_only=True,
    ),
    ToolSpec(
        "callees",
        "What a function calls and where each callee is defined (likely, by name); depth 2 goes one level further. "
        "Example: {\"symbol\": \"Parser.parse\"}.",
        _obj({"symbol": _s("function or Parent.method"), "path": _s("its file"), "depth": _i("1 or 2", 1, 2),
              "limit": _i("cap", 1, 200)}, ["symbol"]),
        read_only=True,
    ),
    ToolSpec(
        "imports_of",
        "The imports/includes of one file and the files they resolve to. Example: {\"path\": \"app/src/main/Foo.kt\"}.",
        _obj({"path": _PATH}, ["path"]),
        read_only=True,
    ),
    ToolSpec(
        "imported_by",
        "Files that import or include one file (who depends on it). Example: {\"path\": \"src/util/units.py\"}.",
        _obj({"path": _PATH, "limit": _i("cap", 1, 200)}, ["path"]),
        read_only=True,
    ),
    ToolSpec(
        "module_of",
        "The build module (Soong Android.bp, Gradle, CMake, Bazel) that builds a file, its deps and the modules that "
        "use it: what to build or test after a change. Example: {\"path\": \"packages/services/Car/service/src/.../CarService.java\"}.",
        _obj({"path": _PATH}, ["path"]),
        read_only=True,
    ),
    ToolSpec(
        "repo_map",
        "A ranked outline of a folder or module (files by import/call rank, with their main definitions), within a "
        "token budget; files read this task rank higher. For orientation, not search. Example: {\"scope\": \"service/src\"}.",
        _obj({"scope": _s("folder or module name (default: the project)"),
              "budget_tokens": _i("size of the map (default 1000)", 100, 8000),
              "focus": {"type": "array", "items": _S, "description": "files to center the map on"}}),
        read_only=True,
    ),
    ToolSpec(
        "git_blame",
        "Who last changed some lines, and in which commit (grouped runs). Example: {\"path\": \"src/a.py\", \"lines\": \"120-160\"}.",
        _obj({"path": _PATH, "lines": _s("a range such as 120-160 (at most 400 lines)")}, ["path", "lines"]),
        read_only=True,
    ),
    ToolSpec(
        "git_history",
        "Commits about a file (path), one function (symbol), words in commit messages (query), or code changes that "
        "added/removed a pattern (changed: git log -G). Example: {\"query\": \"fahrenheit display\"}.",
        _obj({"path": _PATH, "symbol": _s("function or class: commits that changed its lines"),
              "query": _s("words to find in commit messages"), "changed": _s("regex for git log -G"),
              "limit": _i("commits", 1, 50)}),
        read_only=True,
    ),
    ToolSpec(
        "cochanged",
        "Files that usually change in the same commits as one file (what else a change may need). "
        "Example: {\"path\": \"types.hal\"}.",
        _obj({"path": _PATH, "limit": _i("cap", 1, 50)}, ["path"]),
        read_only=True,
    ),
    ToolSpec(
        "find_strings",
        "Find the code that emits a log line, error message, UI string, log tag, resource or property key "
        "(ro.*, persist.*) or SELinux type; a whole logcat line works, numbers may differ. "
        "Example: {\"text\": \"E CarSvc: fan level too high: 12\"}.",
        _obj({"text": _s("the message, tag or key as seen"), "path": _s("only under this folder"),
              "kind": {"type": "string", "enum": ["log", "error", "tag", "resource", "property", "selinux", "literal"]},
              "limit": _i("cap", 1, 100)}, ["text"]),
        read_only=True,
    ),
    ToolSpec(
        "coverage_locate",
        "Run a failing Python test command under line tracing (or read a coverage report: coverage.py JSON, JaCoCo XML, "
        "LCOV from llvm-cov/gcov) and rank the functions it executed; passing commands sharpen it (Ochiai). trace "
        "resolves a stack trace to code. Runs commands like run_command. Example: "
        "{\"command\": \"python -m pytest tests/test_x.py::test_bad\", \"passing\": [\"python -m pytest tests/test_x.py::test_ok\"]}.",
        _obj({"command": _s("the failing test command"),
              "passing": {"type": "array", "items": _S, "description": "passing test commands"},
              "report": _s("coverage report of the failing run"),
              "passing_reports": {"type": "array", "items": _S, "description": "reports of passing runs"},
              "trace": _s("a stack trace or tombstone backtrace"), "timeout": _i("seconds per run", 5, 1800),
              "limit": _i("functions to list", 1, 50)}),
    ),
    ToolSpec(
        "locate",
        "Start here for a bug report or vague task: fuses stack frames, log/error strings, test names, identifiers "
        "(split and abbreviations expanded), file names, commit messages and the call graph into ranked file/function "
        "candidates with reasons. A hypothesis to check by reading. Example: {\"description\": \"temperature shows "
        "in F after reboot\", \"evidence\": \"<log lines or trace>\"}.",
        _obj({"description": _s("the task or bug report"), "evidence": _s("log lines, stack trace, failing test output"),
              "path": _s("only this module/folder (AOSP: indexes it on first use)"), "limit": _i("candidates", 1, 30)},
             ["description"]),
        read_only=True,
    ),
    ToolSpec(
        "delegate",
        "Hand self-contained parts of the task to helper chats that report back; at most 6 briefs.",
        _obj({"tasks": {"type": "array", "maxItems": 6, "items": _obj({
            "brief": _S, "files": {"type": "array", "items": _S}, "name": _S}, ["brief"])}}, ["tasks"]),
    ),
)


# --------------------------------------------------------------------------- registry

Executor = Callable[[str, "dict[str, Any] | None", Any], "dict[str, Any]"]
PermissionCheck = Callable[[str, "dict[str, Any] | None", Any], Any]


class ToolRegistry:
    """A set of tools: their specs, the permission check, and how to run them.

    The built-in registry runs :mod:`critique_bot.agent_tools`. Another registry (a
    hardened tool layer, MCP tools, a test fake) only has to answer the same methods.
    """

    def __init__(
        self,
        specs: Iterable[ToolSpec],
        *,
        execute: Executor,
        permission: PermissionCheck,
    ) -> None:
        self._specs = {spec.name: spec for spec in specs}
        self._execute = execute
        self._permission = permission

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def specs(self, *, read_only: bool = False) -> list[ToolSpec]:
        return [spec for spec in self._specs.values() if spec.read_only or not read_only]

    def spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(canonical_tool(name) or name)

    def is_read_only(self, name: str) -> bool:
        spec = self.spec(name)
        return bool(spec and spec.read_only)

    def permission(self, name: str, arguments: dict[str, Any] | None, ctx: Any) -> Any:
        return self._permission(name, arguments, ctx)

    def execute(self, name: str, arguments: dict[str, Any] | None, ctx: Any) -> dict[str, Any]:
        return self._execute(name, arguments, ctx)

    def only(self, names: Iterable[str]) -> ToolRegistry:
        """The same tools, limited to ``names``; any other call fails as an unknown tool."""
        keep = set(names)
        base = self

        def run(name: str, arguments: dict[str, Any] | None, ctx: Any) -> dict[str, Any]:
            if (canonical_tool(name) or name) not in keep:
                return {"tool": name, "ok": False, "error": f"unknown tool {name!r} here; allowed: {sorted(keep)}"}
            return base.execute(name, arguments, ctx)

        return ToolRegistry(
            [spec for spec in self._specs.values() if spec.name in keep], execute=run, permission=self._permission
        )

    def openai_tools(self, *, read_only: bool = False) -> list[dict[str, Any]]:
        return [spec.to_openai() for spec in self.specs(read_only=read_only)]

    def mcp_tools(self, *, read_only: bool = False) -> list[dict[str, Any]]:
        return [spec.to_mcp() for spec in self.specs(read_only=read_only)]


def builtin_registry() -> ToolRegistry:
    """The tools that ship with crit (agent_tools)."""
    # Looked up at call time, so a patched agent_tools (in tests) is what runs.
    return ToolRegistry(
        BUILTIN_SPECS,
        execute=lambda name, arguments, ctx: _impl.execute(name, arguments, ctx),
        permission=lambda name, arguments, ctx: _impl.permission_for(name, arguments, ctx),
    )


_registry: ToolRegistry = builtin_registry()


def registry() -> ToolRegistry:
    """The installed registry. The engine sends every tool call through it."""
    return _registry


def use_registry(new: ToolRegistry | None) -> ToolRegistry:
    """Install ``new`` (None: the built-in tools) and return the one it replaced."""
    global _registry
    old = _registry
    _registry = new if new is not None else builtin_registry()
    return old


def execute(name: str, arguments: dict[str, Any] | None, ctx: Any) -> dict[str, Any]:
    """Run one tool call through the installed registry."""
    return _registry.execute(name, arguments, ctx)


def permission_for(name: str, arguments: dict[str, Any] | None, ctx: Any) -> Any:
    """What one tool call needs before it runs, from the installed registry."""
    return _registry.permission(name, arguments, ctx)
