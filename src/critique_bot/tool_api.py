# SPDX-License-Identifier: Apache-2.0
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
from critique_bot import env_snapshot as _env
from critique_bot import tool_output as _output

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
def canonical_tool(name: str) -> str:
    """The tool that actually runs: a built-in (aliases included), or a tool of the installed registry
    (a manifest or MCP tool merged in with :func:`merge`); "" when there is none."""
    found = _impl.canonical_tool(name)
    if found:
        return found
    key = str(name).strip().strip("`\"'")
    return key if key in _registry._specs else ""


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
#: Every built-in tool result goes through this (agent_tools.execute applies it): head and
#: tail within the budget, the whole text under .crit/cache/out, never an empty result.
shape_output = _output.shape_output
output_budget = _output.budget_chars
#: The first-turn environment note: OS, shell, cwd, git, languages, test command, tools, jobs.
environment_snapshot = _env.environment_snapshot


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
_FORMAT = {"type": "string", "enum": ["concise", "detailed"],
           "description": "concise (default) or detailed: more per file, file details"}

# Every description follows one checklist (tests/test_tool_api.py checks the last three):
# what the tool is for; when not to use it ("Not for:"); an argument example ("Example:",
# valid JSON); what comes back ("Returns:"); and its limits ("Limits:").
BUILTIN_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "list_files",
        "List one folder: subfolders first with their file counts, then files. Generated trees (build, out, "
        "node_modules, .git) are skipped. Not for: finding a file by name anywhere (find_files) or searching "
        'contents (search_code). Example: {"path": "app/src", "depth": 2}. Returns: one entry per line, '
        "folders ending in /. Limits: depth 1-4; 200 entries unless max_entries (at most 2,000).",
        _obj({"path": _PATH, "depth": _i("levels to list", 1, 4), "glob": _s("only names matching"),
              "max_entries": _i("cap on entries", 1)}),
        read_only=True,
    ),
    ToolSpec(
        "find_files",
        "Find files by name at any depth, newest first. A glob without a slash matches the file name anywhere. "
        'Not for: searching inside files (search_code). Example: {"glob": "**/*Service.java"}. Returns: one '
        "workspace-relative path per line. Limits: 100 results unless limit (at most 500).",
        _obj({"glob": _s('for example "**/*Service.java" or "build.gradle*"'), "path": _PATH,
              "limit": _i("cap on results", 1)}, ["glob"]),
        read_only=True,
    ),
    ToolSpec(
        "read_files",
        "Read files (several in one call) before changing them; lines come back as N|text (the N| is not part of "
        "the file). A file of up to 300 lines comes back whole; a longer one in a 100-line window (\"lines 1-100 "
        "of 2,431\") with an outline, and the result says the next offset. Pass symbol to read one function or "
        "class. Not for: finding where something is (search_code). Example: {\"paths\": [\"src/app.py\", "
        "{\"path\": \"src/big.py\", \"offset\": 200, \"limit\": 80}]}. Returns: a --- path (lines a-b of N) --- "
        "header per file, then numbered lines; an unchanged span already read says so instead (force true "
        "re-reads). Limits: 20 files, 400 lines per file per call; very long lines are clipped (col reads on).",
        _obj({
            "paths": {"type": "array", "maxItems": 20, "items": {"anyOf": [
                _S, _obj({"path": _S, "offset": _I, "limit": _I, "symbol": _S}, ["path"])]}},
            "path": _PATH,
            "offset": _i("first line, 1-based", 1),
            "limit": _i("lines to read", 1, 400),
            "symbol": _s("a class or function, e.g. OrderService.place"),
            "force": _b("read again even when unchanged since an earlier read"),
            "col": _i("start each line at this character (to read on in a very long, clipped line)", 0),
            "response_format": _FORMAT,
        }),
        read_only=True,
    ),
    ToolSpec(
        "search_code",
        "Search file contents with a regular expression. Put every variant you would try into one call with "
        "patterns. Not for: file names (find_files). Example: {\"patterns\": [\"def apply_discount\", "
        "\"apply_discount\\\\(\"], \"glob\": \"*.py\"}. Returns: \"N matches in M files\", the files with their "
        "match counts, then definitions and path:line: text snippets (grouped per pattern); no match says how "
        "many files were searched. Never paged: narrow with path or glob instead. Limits: 8 patterns; 50 "
        "snippets (head_limit, at most 200), 5 per file (15 detailed); context at most 3 lines.",
        _obj({"pattern": _s("regular expression (or text with literal true)"),
              "patterns": {"type": "array", "maxItems": 8, "items": _S,
                           "description": "several patterns searched in one call, results grouped per pattern"},
              "path": _PATH, "glob": _s("only files matching"), "head_limit": _i("cap on snippets", 1, 200),
              "context": _i("lines around each hit", 0, 3), "literal": _b("match the text exactly"),
              "case_insensitive": _b("ignore case"), "response_format": _FORMAT}),
        read_only=True,
    ),
    ToolSpec(
        "write_files",
        "Create files, or replace a whole file when edit_file failed on it or the change touches most of it; "
        "parent folders are created. Not for: changing part of an existing file (edit_file is cheaper and "
        'safer). Example: {"files": [{"path": "src/util.py", "contents": "def f():\\n    return 1\\n"}]}. '
        "Returns: created/replaced lines per file, the diff, a syntax check and new lint issues. Limits: "
        "contents must be the complete file (no \"...\" placeholders); replacing a file not read in this task, "
        "or shrinking a long file below 40%, needs overwrite true.",
        _obj({"files": {"type": "array", "items": _obj({"path": _S, "contents": _S}, ["path", "contents"])},
              "path": _PATH, "contents": _S, "overwrite": _b("replace a file not read in this task")}),
    ),
    ToolSpec(
        "edit_file",
        "Change part of a file: replace old_string with new_string. Copy old_string from read_files without "
        "the N| prefix, with enough lines to match one place; small slips in whitespace or indentation are "
        "matched and re-indented, and a near match is applied only when it is clearly the one (the result says "
        "so). Several changes to one file go in edits and apply together or not at all. Not for: new files "
        '(write_files). Example: {"path": "shop/pricing.py", "old_string": "    return price - percent", '
        '"new_string": "    return price * (100 - percent) / 100"}. Returns: the changed line range, the diff, a '
        "syntax check and new lint issues; a miss shows the closest text and the line that differs; several "
        "matches list their line numbers. Limits: read the file first (it must not have changed since); after "
        "2 failures on one file, use write_files with the full content.",
        _obj({"path": _PATH, "old_string": _S, "new_string": _S, "replace_all": _b("change every match"),
              "edits": {"type": "array", "items": _obj({"old_string": _S, "new_string": _S, "replace_all": _B},
                                                        ["old_string", "new_string"])}}, ["path"]),
    ),
    ToolSpec(
        "delete_file",
        "Remove one file (crit undo restores it). Not for: folders or symbolic links (run_command). "
        'Example: {"path": "src/old_helper.py"}. Returns: the path deleted. Limits: one file per call.',
        _obj({"path": _PATH}, ["path"]),
    ),
    ToolSpec(
        "move_file",
        "Move or rename one file; a folder destination keeps the name. Not for: folders (run_command, git mv). "
        'Example: {"path": "src/a.py", "destination": "src/util/a.py"}. Returns: old -> new path. Limits: one '
        "file; an existing destination needs overwrite true.",
        _obj({"path": _PATH, "destination": _PATH, "overwrite": _b("replace an existing destination")},
             ["path", "destination"]),
    ),
    ToolSpec(
        "apply_patch",
        "Apply a unified diff or a V4A patch (*** Begin Patch / *** Update File: path / @@ / -old / +new / "
        "*** End Patch) to one or more files. Line numbers are ignored: each hunk is found by its context "
        "lines. Not for: models that edit with edit_file. Example: {\"patch\": \"--- a/app.py\\n+++ b/app.py\\n"
        "@@ -1,2 +1,2 @@\\n def f():\\n-    return 1\\n+    return 2\\n\"}. Returns: the files changed with "
        "their line ranges and diffs. Limits: every hunk must match exactly one place, else nothing is applied "
        "and the result names the hunk.",
        _obj({"patch": _s("unified diff or V4A patch")}, ["patch"]),
    ),
    ToolSpec(
        "run_command",
        "Run one command like a terminal (tests, builds, git, scripts); the working folder and environment "
        "carry over between calls. Not for: reading or searching files (read_files, search_code). Example: "
        "{\"command\": \"python -m pytest -q tests/test_pricing.py\", \"timeout\": 300}. Returns: exit code and "
        "seconds, then stdout and stderr (long output keeps the start and end, the whole is saved to a file the "
        "result names). Limits: 60 s by default, up to 1800 with timeout; a command still running then is not "
        "killed but continues as a background job (its job_id is in the result). Use background true for "
        "servers and watchers.",
        _obj({"command": _S, "timeout": _i("seconds", 1, 1800), "cwd": _PATH,
              "shell": {"type": "string", "enum": ["bash", "sh", "zsh", "pwsh", "powershell", "cmd", "gitbash"]},
              "background": _b("run in the background")}, ["command"]),
    ),
    ToolSpec(
        "command_output",
        "Output and status of a background command; omit job_id to list every background job. crit reports a "
        "job's end on its own; until waits without spending turns. Not for: starting commands (run_command). "
        'Example: {"job_id": "job-1", "until": "BUILD (SUCCESSFUL|FAILED)", "wait": 600}. Returns: running or '
        "exit code, then the new output. Limits: wait at most 1200 s.",
        _obj({"job_id": _S,
              "until": {"type": "string",
                        "description": '"exit", or a regular expression such as "BUILD (SUCCESSFUL|FAILED)|error:"'},
              "wait": _i("seconds to wait (up to 1200 with until; the wait ends when the job exits)", 0, 1200)}),
        read_only=True,
    ),
    ToolSpec(
        "command_input",
        "Type one line into a background command (a shell, REPL, debugger, or device shell). Not for: "
        'commands that take flags such as --yes instead. Example: {"job_id": "job-2", "input": "bt"}. '
        "Returns: what the command printed after it. Limits: one line per call; wait at most 30 s.",
        _obj({"job_id": _S, "input": _S, "enter": _b("press Enter after the input (default true)"),
              "wait": _i("seconds to wait for its answer", 0, 30), "close": _b("end its input, like Ctrl+D")},
             ["job_id"]),
    ),
    ToolSpec(
        "kill_command",
        "Stop a background command and its children. Not for: foreground commands (they end at their timeout). "
        'Example: {"job_id": "job-1"}. Returns: stopped, or that it had already exited (and its exit code). Limits: one job '
        "per call.",
        _obj({"job_id": _S}, ["job_id"]),
        read_only=True,
    ),
    ToolSpec(
        "jira",
        "Read Jira (never changes it): one issue by key (summary, status, description, links, recent comments), "
        "or issues matching a JQL search. Not for: anything when Jira is not set up (the result says so). "
        'Example: {"issue": "CAR-123"}. Returns: the issue as text, or one line per issue. Limits: search '
        "lists 20 issues unless limit (at most 50).",
        _obj({"issue": _s("an issue key such as CAR-123"), "jql": _s("a JQL search, e.g. project = CAR AND status = Open"),
              "limit": _i("search: issues to list", 1, 50)}),
        read_only=True,
    ),
    ToolSpec(
        "log",
        "Analyze a log of any size (logcat, dmesg, bugreport, build output). summary: crashes, ANRs, errors "
        "grouped with counts and line numbers. query: lines by level/tag/pid/package/pattern. around: lines "
        "near a line. diff: what is new in a bad log (path) against a good one (good). Not for: source files "
        '(read_files, search_code). Example: {"action": "query", "path": "logcat.txt", "level": "E", '
        '"package": "com.example.app"}. Returns: the summary or the matching lines with line numbers. Limits: '
        "query returns 100 lines (300 detailed, at most 400 with limit).",
        _obj({"action": {"type": "string", "enum": ["summary", "query", "around", "diff"]},
              "path": _PATH, "good": _s("diff: the good log"),
              "level": _s("query: lowest level, e.g. W or E"), "tag": _S, "pid": _S,
              "package": _s("query: an app package; its processes' lines"),
              "pattern": _s("query: a regular expression"), "line": _i("around: the line", 1),
              "offset": _i("query: first line", 1), "limit": _i("query: lines to return", 1, 400),
              "response_format": _FORMAT},
             ["path"]),
        read_only=True,
    ),
    ToolSpec(
        "git_status",
        "The git status of the workspace: branch and changed files. Not for: the changes themselves (git_diff). "
        "Example: {}. Returns: git status --short --branch. Limits: the output budget of one result.",
        _obj(), read_only=True),
    ToolSpec(
        "git_diff",
        "The uncommitted changes (or staged ones). Not for: a commit's changes (git_show). Example: "
        '{"path": "src", "stat": true}. Returns: a unified diff, or a per-file summary with stat. Limits: the '
        "output budget of one result; narrow with path.",
        _obj({"path": _PATH, "staged": _B, "stat": _b("summary only")}), read_only=True),
    ToolSpec(
        "git_log",
        "Recent commits, optionally for one path. Not for: one commit's diff (git_show). Example: "
        '{"path": "src/app.py", "limit": 10}. Returns: one commit per line (hash, date, subject). '
        "Limits: 20 commits unless limit (at most 50).",
        _obj({"limit": _i("commits", 1, 50), "path": _PATH}), read_only=True),
    ToolSpec(
        "git_show",
        "One commit, branch, or HEAD; with patch true its diff. Not for: uncommitted changes (git_diff). "
        'Example: {"rev": "HEAD~1", "patch": true}. Returns: the commit header and its file list or diff. '
        "Limits: the output budget of one result; narrow with path.",
        _obj({"rev": _S, "path": _PATH, "patch": _b("include the diff")}), read_only=True),
    ToolSpec(
        "todo",
        "The task list for a job with several steps; omit todos to read it back. Not for: a one-step task. "
        'Example: {"todos": [{"content": "fix the parser", "status": "in_progress"}, {"content": "run the tests", '
        '"status": "pending"}]}. Returns: the list. Limits: one item in_progress at a time.',
        _obj({"todos": {"type": "array", "items": _obj({
            "content": _S,
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
        }, ["content", "status"])}}),
        read_only=True,
    ),
    ToolSpec(
        "skill",
        "Load one SKILL.md (instructions for a kind of task in this project); omit name to list the skills. "
        'Not for: project files (read_files). Example: {"name": "aosp"}. Returns: the skill text, or one skill '
        "per line. Limits: about 7,000 characters of a skill.",
        _obj({"name": _S}), read_only=True),
    ToolSpec(
        "web_fetch",
        "Read one documentation page as text, only on the sites the environment allows. Not for: APIs, "
        'downloads, or pages with a query string. Example: {"url": "https://developer.android.com/reference/'
        'android/app/Activity"}. Returns: the page as plain text; offset reads on. Limits: 12,000 characters '
        "unless max_chars; https only; allowed hosts only.",
        _obj({"url": _s("https URL with no query string"), "max_chars": _i("cap on text", 1),
              "offset": _i("characters to skip", 0)}, ["url"]),
    ),
    ToolSpec(
        "ask_user",
        "Ask the user one question when the task cannot go on without their decision. Not for: anything you "
        'can find out with the other tools. Example: {"question": "Keep the old API?", "options": ["keep", '
        '"remove"]}. Returns: the answer. Limits: one question per call.',
        _obj({"question": _S, "options": {"type": "array", "items": _S}}, ["question"]),
        read_only=True,
    ),
    # ---- the index tools (Stage 8, critique_bot.indexing.tools): structure, history, evidence
    ToolSpec(
        "code_nav",
        "Navigate code by structure from crit's index. action find_symbol: where a name is defined; refs: every "
        "use of a name by file; who_calls / callees: likely call edges (matched by name); imports_of / "
        "imported_by: file dependencies; module_of: the build module (Android.bp, Gradle, CMake, Bazel) of a "
        "file, its deps and users; repo_map: a ranked outline of a folder or module. Not for: text inside "
        'bodies (search_code) or vague bug reports (locate). Example: {"action": "who_calls", "name": '
        '"HvacController.setTemp", "depth": 2}. Returns: one line per item (path:line, kind, name, signature) '
        "under a header with counts; starts with partial: while the index is still being built. Limits: 30 "
        "items unless limit (at most 200); depth 1-2; repo_map 1000 tokens unless budget_tokens (at most 8000).",
        _obj({"action": {"type": "string", "enum": ["find_symbol", "refs", "who_calls", "callees", "imports_of",
                                                    "imported_by", "module_of", "repo_map"]},
              "name": _s("a symbol: name, Parent.name, part of a name, or path:line of a definition"),
              "path": _s("a file (imports_of, imported_by, module_of) or a folder/module to search in"),
              "kind": _s("find_symbol: class, interface, function, method, struct, enum, ..."),
              "depth": _i("who_calls/callees: 1 or 2", 1, 2),
              "scope": _s("repo_map: folder or module name (default: the project)"),
              "budget_tokens": _i("repo_map: size of the map", 100, 8000),
              "focus": {"type": "array", "items": _S, "description": "repo_map: files to center the map on"},
              "limit": _i("cap on items", 1, 200)}, ["action"]),
        read_only=True,
    ),
    ToolSpec(
        "code_history",
        "Git history from crit's index. action blame: who last changed some lines (path, lines); log: commits "
        "for a file (path), one function's lines (name), words in commit messages (query) or changes that "
        "added/removed a pattern (changed, git log -G); cochanged: files that usually change in the same "
        "commits as path. Not for: one commit's diff (git_show) or uncommitted work (git_diff). Example: "
        '{"action": "log", "query": "fahrenheit display"}. Returns: one commit or file per line (hash, date, '
        "author, subject; or path, times, share). Limits: 10 commits unless limit (at most 50); blame at most "
        "400 lines.",
        _obj({"action": {"type": "string", "enum": ["blame", "log", "cochanged"]}, "path": _PATH,
              "lines": _s("blame: a range such as 120-160"),
              "name": _s("log: a function or class whose lines' commits to list"),
              "query": _s("log: words to find in commit messages"), "changed": _s("log: regex for git log -G"),
              "limit": _i("commits or files", 1, 50)}, ["action"]),
        read_only=True,
    ),
    ToolSpec(
        "find_strings",
        "Find the code that emits a log line, error message, UI string, log tag, resource or property key "
        "(ro.*, persist.*) or SELinux type; a whole logcat line works and numbers may differ. Not for: "
        'identifiers (code_nav find_symbol) or regular expressions (search_code). Example: {"text": '
        '"E CarSvc: fan level too high: 12"}. Returns: path:line kind (tag, function) "literal" (match %), '
        "best first. Limits: 15 hits unless limit (at most 100).",
        _obj({"text": _s("the message, tag or key as seen"), "path": _s("only under this folder"),
              "kind": {"type": "string", "enum": ["log", "error", "tag", "resource", "property", "selinux", "literal"]},
              "limit": _i("cap", 1, 100)}, ["text"]),
        read_only=True,
    ),
    ToolSpec(
        "locate",
        "Start here for a bug report or vague task: fuses stack frames, log/error strings, test names, identifiers "
        "(split, abbreviations expanded), file names, commit messages and the call graph into ranked file and "
        "function candidates with the evidence for each; a hypothesis to check by reading. Not for: a known name "
        '(code_nav find_symbol) or an exact text (search_code). Example: {"description": "temperature shows in F '
        'after reboot", "evidence": "I ClusterUnits: display units from property: metric"}. Returns: numbered '
        "path:line function [retrievers] with a why line and other likely functions. Limits: 10 candidates "
        "unless limit (at most 30); about 1.5 s of search.",
        _obj({"description": _s("the task or bug report"), "evidence": _s("log lines, stack trace, failing test output"),
              "path": _s("only this module/folder (AOSP: indexes it on first use)"), "limit": _i("candidates", 1, 30)},
             ["description"]),
        read_only=True,
    ),
    ToolSpec(
        "coverage_locate",
        "Rank the functions a failing test executed: runs a Python test command under line tracing, or reads a "
        "coverage report (coverage.py JSON, JaCoCo XML, LCOV from llvm-cov/gcov); passing commands or reports "
        "sharpen it (Ochiai); trace resolves a stack trace to code. Runs commands like run_command. Not for: "
        'a bug without a failing test or trace (locate). Example: {"command": "python -m pytest tests/test_x.py", '
        '"passing": ["python -m pytest tests/test_y.py"]}. Returns: score path:start-end function (lines run), '
        "most suspicious first, and resolved stack frames. Limits: 15 functions unless limit (at most 50); "
        "300 s per run unless timeout (at most 1800); 5 passing runs.",
        _obj({"command": _s("the failing test command"),
              "passing": {"type": "array", "items": _S, "description": "passing test commands"},
              "report": _s("coverage report of the failing run"),
              "passing_reports": {"type": "array", "items": _S, "description": "reports of passing runs"},
              "trace": _s("a stack trace or tombstone backtrace"), "timeout": _i("seconds per run", 5, 1800),
              "limit": _i("functions to list", 1, 50)}),
    ),
    # ---- project memory (Stage 8b, critique_bot.memory.tool)
    ToolSpec(
        "memory",
        "Project memory: what past tasks here changed and why, file and module cards, change recipes, approved "
        "conventions. action search: items like query (words, a symbol, an error text); show: one item by id "
        "(e12, f40, n3, r2) with its sources; propose: a project-specific convention for a person to approve "
        "(text naming the file, symbol or command, plus evidence). Memory is a hunch: verify by reading the code. "
        'Not for: reading code (read_files) or finding it (locate). Example: {"action": "search", "query": '
        '"temperature unit after reboot"}. Returns: one item per line (id, kind, outcome, summary). Limits: 12 '
        "items unless limit; at most 2 proposals per task; generic advice is refused.",
        _obj({"action": {"type": "string", "enum": ["search", "show", "propose"]},
              "query": _s("search: what to look for"), "id": _s("show: an item id such as e12"),
              "kind": {"type": "string", "enum": ["episode", "card", "convention", "recipe", "hotspot"]},
              "text": _s("propose: the convention, naming the file, symbol or command"),
              "evidence": {"type": "array", "items": _S, "description": "propose: command output, file:line, test name"},
              "limit": _i("cap on items", 1, 50)}, ["action"]),
        read_only=True,
    ),
    ToolSpec(
        "delegate",
        "Hand self-contained parts of the task to helper chats that report back. Not for: one read or search, "
        'or a part that needs a result you do not have yet. Example: {"tasks": [{"brief": "Find where orders '
        'are priced and summarize the flow", "name": "pricing"}]}. Returns: each helper\'s report and the files '
        "it changed. Limits: at most 6 briefs; helpers run read-only commands only.",
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

    def with_specs(self, specs: Iterable[ToolSpec]) -> ToolRegistry:
        """The same tools and runners; each of ``specs`` replaces the spec of the same name.

        For a description or schema that depends on the session (delegate listing the agents).
        A spec for a tool this registry does not have is ignored.
        """
        replaced = dict(self._specs)
        for spec in specs:
            if spec.name in replaced:
                replaced[spec.name] = spec
        return ToolRegistry(replaced.values(), execute=self._execute, permission=self._permission)

    def openai_tools(self, *, read_only: bool = False) -> list[dict[str, Any]]:
        return [spec.to_openai() for spec in self.specs(read_only=read_only)]

    def mcp_tools(self, *, read_only: bool = False) -> list[dict[str, Any]]:
        return [spec.to_mcp() for spec in self.specs(read_only=read_only)]


def merge(base: ToolRegistry, *extras: ToolRegistry) -> ToolRegistry:
    """One registry: ``base``'s tools, then each extra's (manifest tools, MCP tools, ...).

    A call goes to the registry that declared its tool, for both the permission check and the
    run, so an extension tool is asked about and run exactly like a built-in. A name ``base``
    already has stays ``base``'s: an extension cannot replace a built-in tool. The order is
    fixed when the registry is made (the declared tool list stays cache-stable).
    """
    owner: dict[str, ToolRegistry] = {name: base for name in base.names()}
    specs = list(base.specs())
    for extra in extras:
        for spec in extra.specs():
            if spec.name in owner:
                continue
            owner[spec.name] = extra
            specs.append(spec)

    def route(name: str) -> ToolRegistry:
        key = str(name).strip().strip("`\"'")
        return owner.get(key) or owner.get(_impl.canonical_tool(name) or key) or base

    return ToolRegistry(
        specs,
        execute=lambda name, arguments, ctx: route(name).execute(name, arguments, ctx),
        permission=lambda name, arguments, ctx: route(name).permission(name, arguments, ctx),
    )


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
    """Run one tool call through the session's registry (``ctx.registry``), else the installed one."""
    return (getattr(ctx, "registry", None) or _registry).execute(name, arguments, ctx)


def permission_for(name: str, arguments: dict[str, Any] | None, ctx: Any) -> Any:
    """What one tool call needs before it runs, from the session's registry, else the installed one."""
    return (getattr(ctx, "registry", None) or _registry).permission(name, arguments, ctx)
