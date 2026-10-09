"""Local coding agent driven through a web chat page.

The chat model prints ``<tool_call>`` blocks. This module parses them, runs
the tool from :mod:`critique_bot.agent_tools`, and sends ``<tool_result>``
blocks back on the same chat, each followed by a short STATE block so the
task stays in view however long the conversation gets. Model-facing text
lives in ``prompts/agent.txt``.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from critique_bot import agent_build, agent_edit, agent_shell, agent_tools, code_graph, code_index, log
from critique_bot.agent_tools import (
    ALLOWED_TOOLS,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_TOOL_CHARS,
    MUTATING,
    TaskState,
    ToolContext,
    canonical_tool,
    fill,
    is_git_repo,
    normalize_args,
)
from critique_bot.agent_tools import execute as _execute
from critique_bot.bot_home import BotHome, resolve_check_command
from critique_bot.chat_client import COMPLETION_IDLE, ChatError
from critique_bot.config import BotConfig
from critique_bot.output import isoformat, write_output

__all__ = [
    "ALLOWED_TOOLS",
    "ToolCall",
    "command_argv",
    "execute_tool",
    "format_tool_result",
    "parse_tool_calls",
    "run_agent",
    "run_agent_loop",
]

# The canonical tag is <tool_call>. Models also print <tool_use>,
# <function_call>, and <tool_call name="read_files"> with the arguments as
# the body; all of them are read the same way.
_CALL_TAGS = r"(?:tool_call|tool_use|function_call|toolcall)"
_OPEN_RE = re.compile(r"<\s*" + _CALL_TAGS + r"\b[^>]*>", re.IGNORECASE)
_BLOCK_RE = re.compile(
    r"<\s*(" + _CALL_TAGS + r")\b([^>]*)>\s*(.*?)\s*<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_CLOSED_TOOL_MARKUP_RE = re.compile(
    r"<\s*(" + _CALL_TAGS + r"|tool_result)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_TAG_RE = re.compile(r"<\s*/?\s*(?:" + _CALL_TAGS + r"|tool_result)\b[^>]*>", re.IGNORECASE)
_UNCLOSED_TOOL_RE = re.compile(r"<\s*(?:" + _CALL_TAGS + r"|tool_result)\b[^>]*>[\s\S]*\Z", re.IGNORECASE)
_RESULT_OPEN_RE = re.compile(r"<\s*tool_result\b[^>]*>", re.IGNORECASE)
# A result written out as bare JSON: {"tool": "read_files", "ok": true, ...}
_BARE_RESULT_RE = re.compile(r"""\{\s*["']?tool["']?\s*:\s*["'][^"'\n]*["']\s*,\s*["']?ok["']?\s*:""")
_ATTR_RE = re.compile(r"""\b(name|tool|function)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_INNER_NAME_RE = re.compile(
    r"<\s*(?:name|tool|tool_name|function)\s*>\s*([\w.\-]+)\s*<\s*/\s*(?:name|tool|tool_name|function)\s*>",
    re.IGNORECASE,
)
_INNER_ARGS_RE = re.compile(
    r"<\s*(arguments|args|parameters|input)\s*>\s*(.*?)\s*<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_TAIL_TOOL_RE = re.compile(r"""["']?(?:tool|name)["']?\s*:\s*["']([\w.\-]+)["']""")
_TAIL_PATH_RE = re.compile(r"""["']?(?:path|filePath|file_path)["']?\s*:\s*["']([^"'\n]{1,160})["']""")
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*)\n```$", re.DOTALL)
_FENCED_JSON_RE = re.compile(
    r"```(?:json|jsonc|json5|tool|tool_call)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE
)
# The label and buttons a chat page draws on a code block ("json", "Copy code", "Edit").
_CHROME_LINES = frozenset(
    {
        "json", "jsonc", "json5", "copy", "copy code", "code", "edit", "run", "tool", "tool_call",
        "javascript", "js", "xml", "html", "text", "plaintext", "python", "bash", "sh", "shell",
        "powershell", "ps1", "copied!", "copied",
    }
)
# Words just before a JSON object that mean it is quoted, not a call.
_QUOTED_CONTEXT_RE = re.compile(
    r"(?i)(\b[\w./\\-]+\.(?:json|jsonc|json5|ya?ml|toml|txt|md|log|cfg|conf|ini)\b"
    r"|for example|e\.g\.|such as|\bexample\b|contents? of|the file (?:contains|has|is|looks|reads)"
    r"|currently (?:contains|has|reads)|looks like|is not a tool call|<\s*tool_result)"
)
# A Windows path written with single backslashes: C:\new\tools, .\src\app.py
_RAW_WIN_PATH_RE = re.compile(r"""(?:\b[A-Za-z]:|(?<![\w\\])\.{1,2})\\(?![\\"'/])""")
_BARE_TOOL_KEY_RE = re.compile(
    r"""(?:"tool"|'tool'|"name"|'name'|(?<![A-Za-z0-9_$])(?:tool|name)\s*:)"""
)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_PLAN_RE = re.compile(r"<plan>\s*(.*?)\s*</plan>", re.IGNORECASE | re.DOTALL)
_UNTAGGED_PLAN_RE = re.compile(r"(?is)\bfiles?\s*:.{8,}?\bchange\s*:.{8,}?\bcheck\s*:")
_EDIT_TASK_RE = re.compile(
    r"\b(add|create|update|fix|change|remove|delete|rename|write|implement|refactor|"
    r"replace|edit|modify|insert|append|move|convert|migrate|bump)\b",
    re.IGNORECASE,
)
_ANSWER_TASK_RE = re.compile(
    r"(?is)^(?:please\s+|can you\s+|could you\s+|would you\s+)?"
    r"(?:what|why|how|who|when|where|which|explain|describe|summarize|summarise|"
    r"tell me|define|compare|is|are|does|do|can|could|should|would|what's|whats)\b"
)
_PROMISE_START_RE = re.compile(
    r"(?i)^(i'll|i will|let me|next i|i'm going to|i am going to)\s+"
    r"(look|read|check|search|edit|update|fix|open|find|run|start|try|use|inspect|review)\b"
)
_SECTION_RE = re.compile(r"^<<<([A-Z_]+)>>>\s*$", re.MULTILINE)
_QUIT = {"exit", "quit", "/exit", "/quit", "/q"}
_STATUS_WORDS = {
    "completed": "COMPLETED",
    "finished": "FINISHED",
    "done": "DONE",
    "failed": "FAILED",
    "blocked": "BLOCKED",
}
_STATUS_OK = frozenset({"COMPLETED", "FINISHED", "DONE"})
#: Helper chat tabs a task can be split across (the delegate tool). 0 turns it off.
DEFAULT_HELPER_SESSIONS = 2
MAX_HELPER_SESSIONS = 4
MAX_PLAN_REVISIONS = 5
_FAIL_HEAD = re.compile(r"^(FAILED|BLOCKED)\b\s*[:\-—]?\s*(.*)$")
MAX_REFUSALS = 3
MAX_FAILED_ROUNDS = 8
#: Replies in a row whose every call the user denied end the task as BLOCKED.
MAX_DENIED_ROUNDS = 3
MAX_TRUNCATIONS = 4
MAX_CHECK_CYCLES = 2
MAX_USER_QUESTIONS = 2
#: An identical read-only call at the same disk version is not run a third time.
MAX_SAME_CALL = 2
#: The same set of calls in this many replies in a row is a loop.
LOOP_REPLIES = 3
#: Protocol violations in a row that make the chat start over.
AMNESIA_VIOLATIONS = 2
DEFAULT_CHECK_TIMEOUT = 600
DEFAULT_COMPACT_AFTER_CHARS = 300_000
#: A chat also moves to a new one after this many messages or minutes: a long chat
#: slows the page down and the model loses track of early instructions.
DEFAULT_COMPACT_AFTER_TURNS = 60
DEFAULT_COMPACT_AFTER_MINUTES = 60
#: After this many failed tries in one chat, the next try goes to a new chat.
RETRIES_BEFORE_NEW_CHAT = 1
DEFAULT_REPLY_RETRIES = 3
#: Seconds between resends when the chat page shows an error instead of a reply.
RETRY_DELAYS = (5.0, 15.0, 45.0)
PARALLEL_WORKERS = 4
_REVIEW_LINES = 80
REPO_MAP_CHARS = 2_500
READY_LINE = "Reply with exactly READY."
#: Read-only tools that may run side by side within one reply.
_PARALLEL_SAFE = frozenset(
    {"list_files", "find_files", "read_files", "search_code", "git_status", "git_diff", "git_log", "git_show"}
)
_READ_ONLY_FALLBACK = frozenset(
    _PARALLEL_SAFE | {"skill", "code_graph", "todo", "command_output", "ask_user", "web_fetch"}
)
#: Tools whose result changes over time, so repeating them is not a loop.
_POLLING = frozenset({"command_output", "ask_user"})
#: Error text the chat page shows in place of a reply.
_PROVIDER_ERROR_RE = re.compile(
    r"(?i)(something went wrong|network error|there was an error generating|"
    r"an error occurred|error in (?:the )?message stream|you'?ve reached (?:our|the|your)|"
    r"unusual activity|too many requests|rate limit|conversation not found|"
    r"request timed out|load failed|failed to fetch|please try again later|"
    r"hmm\.\.\.\s*something seems to have gone wrong)"
)
_PROVIDER_ERROR_MAX = 400
_PERMISSION_MARKERS = (
    "may i", "can i proceed", "should i proceed", "should i go ahead", "shall i proceed",
    "is it ok", "is that ok", "okay to", "ok to proceed", "permission", "confirm", "approve",
)
_TEST_MARKERS = (
    "tests", "test", "__tests__", "spec", "pytest.ini", "tox.ini", "package.json", "gradlew",
    "gradlew.bat", "build.gradle", "build.gradle.kts", "pom.xml", "Cargo.toml", "go.mod", "Makefile",
    "CMakeLists.txt", "setup.py", "pyproject.toml",
)

# Overridable in tests so a retry does not really sleep.
_sleep = time.sleep

_FALLBACKS = {
    "TASK_PREFIX": "Print tool_call blocks to act. Do not refuse.",
    "PLAN_MODE": (
        "PLAN MODE. Do not change anything yet. Investigate with read-only tools, then reply with a plan "
        "and no tool_call: the goal, what you found (with paths), the steps (files and the change in each), "
        "risks, and how you will verify. Do not reply COMPLETED."
    ),
    "PLAN_NEEDED": "Plan mode is on. Reply with the plan in words now, with no tool_call and no COMPLETED.",
    "PLAN_ONLY": (
        "Plan mode is on, so nothing that changes files or runs a build was run. Use read-only tools only, "
        "then reply with the plan and no tool_call."
    ),
    "PLAN_APPROVED": (
        "The user approved the plan. Plan mode is off. Carry it out now: write the todo list from the plan, "
        "then send the tool_call blocks for the first steps. Verify, then reply COMPLETED."
    ),
    "PLAN_REVISE": "The user wants the plan changed:\n{feedback}\nInvestigate if needed, then reply with the revised plan and no tool_call.",
    "HAND_BACK": (
        "Do not hand the work to the user. run_command runs on the user's machine, so run the build or "
        "command yourself now. If it fails, read SUMMARY and HINTS in its result, fix the cause, and run it again."
    ),
    "HELPER": (
        "You are {name}, a helper tab working on one part of a larger task that another chat coordinates. "
        "It cannot see this chat; your final reply is all it gets.\nBrief:\n{brief}\n"
        "Files you may change: {files}. You may read any file. Do not build or run tests. "
        "When done, reply COMPLETED and then a short report: what you found or changed, with paths and line numbers, "
        "and what the coordinator still has to do."
    ),
    "HANDOFF": (
        "This chat is about to be replaced by a new one, which will not see this conversation. Write a handoff "
        "note for it now, with no tool_call: the task and what done means; what you learned about the code "
        "(files, symbols, line numbers, conventions); what you changed and why; what is left, in order; and "
        "what failed or must be avoided. At most 300 words."
    ),
    "AUTO_DECIDE": (
        "Auto mode is on and the user is not answering questions. Decide yourself: pick the safest reasonable "
        "option, say which in one line, and continue the task."
    ),
    "NUDGE": (
        "No tool_call block was found, so nothing was run. "
        "A bare JSON object such as {tool:\"write_file\"} is not a tool call. "
        "Reply with a tool_call block now.\n"
        "<tool_call>\n"
        '{"tool": "list_files", "arguments": {"path": "."}}\n'
        "</tool_call>"
    ),
    "RECOVER": (
        "The previous step did not finish. Do not ask what to change. Do not say the tools "
        "are unavailable. A bare JSON object is not a tool call. Send the next tool_call now, for example:\n"
        "<tool_call>\n"
        '{"tool": "read_files", "arguments": {"path": "{path}"}}\n'
        "</tool_call>"
    ),
    "ANSWER": (
        "The task is a question. Reply with the answer in words and no tool_call when you "
        "already know it or a tool result above shows it. If you still need a file or a command, "
        "send one tool_call. Do not ask what to change."
    ),
    "STATE": (
        "STATE step {step}\nTask: {task}\nRead: {reads}\nChanged: {edits}\n"
        "Last command: {command}\n"
        "Next: the next tool_call, or the answer when the task is only a question, or COMPLETED. "
        "FAILED or BLOCKED must say what failed and why."
    ),
    "PLAN_NOTED": "Plan noted. Send the tool_call for its first step now, with no other words.",
    "NOTHING_CHANGED": (
        "No file changed in this task. If the task needs a change, send the edit now. "
        "If no change is needed, reply COMPLETED again."
    ),
    "CHECK_FAILED": (
        "The project check above failed after your edits. Read the failure, fix it, and "
        "reply COMPLETED when it passes. If it cannot be fixed, reply FAILED and say what failed and why."
    ),
    "WHY_FAILED": (
        "That reply was only the word. Say what failed and why, copied from the tool result. "
        "Start with FAILED or BLOCKED, then one or two sentences. "
        "Send a tool_call instead if you can still fix it."
    ),
    "TRUNCATED": (
        "Your last reply was truncated (cut off) inside a tool_call{call}, so that call did not run. "
        "Continue from there: resend only that one call, complete, and nothing before it. "
        "If it writes a long file, split it: write_files with the first part, then edit_file to add the rest."
    ),
    "FABRICATED": (
        "Your reply contained a tool_result. Only the program writes tool_result blocks; you never do. "
        "Everything from that point on was ignored. The real results of the calls before it are above."
    ),
    "LOOP": (
        "You sent the same tool calls {count} times in a row. They were not run again: the results are above. "
        "Use them and take a different next step, or finish."
    ),
    "VERIFY": (
        "You changed files but ran nothing to check them. Run the relevant tests or build with run_command now. "
        "If there is truly nothing to run, reply COMPLETED again."
    ),
    "DENIED": (
        "The user denied that call, so it did not run. Do not send it again unchanged. "
        "Change the approach, or use ask_user if you need the user's decision."
    ),
    "USER_ANSWER": "The user answered your question:\n{answer}\nContinue the task.",
    "RESUME": (
        "CONTINUING IN A NEW CHAT. The previous chat grew long, so this is a fresh one. "
        "The rules above apply. Summary of the work so far:"
    ),
}


@dataclass(frozen=True)
class ToolCall:
    tool: str
    arguments: dict[str, Any]
    error: str | None = None


def command_argv(command: str, *, platform_name: str | None = None, cwd: Path | None = None) -> list[str]:
    """Build the argv for ``run_command``. The command is one argument."""
    return agent_shell.command_argv(command, platform_name=platform_name, cwd=cwd)


def execute_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    workspace: Path,
    index_path: Path | None = None,
    cache_dir: Path | None = None,
    max_chars: int = DEFAULT_TOOL_CHARS,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    runner: Callable[..., Any] | None = None,
    state: TaskState | None = None,
) -> dict[str, Any]:
    ctx = ToolContext(
        workspace=Path(workspace),
        index_path=index_path,
        cache_dir=cache_dir,
        max_chars=max_chars,
        command_timeout=command_timeout,
        runner=runner,
        state=state,
    )
    return _execute(name, arguments, ctx)


# --------------------------------------------------------------------------- parsing


def parse_tool_calls(
    text: str, *, complete: bool = False, allow_bare: bool = True
) -> tuple[list[ToolCall], bool]:
    """Parse tool calls. The bool is True only when the reply has an open tag and no call.

    Complete blocks always run. Text left after them is checked for a
    dangling ``<tool_call>``: one followed by JSON means a block was cut off;
    a bare tag (page rendering often leaves one) is ignored. When the page
    said the reply finished (``complete``), a dangling block whose JSON is
    whole only lost its closing tag, and it runs.

    A reply with no tags is searched for fenced or bare JSON objects naming a
    tool, unless ``allow_bare`` is False. JSON introduced as a quote ("the
    file contains", "for example", a ``.json`` file name) is not a call.
    """
    calls: list[ToolCall] = []
    for match in _BLOCK_RE.finditer(text):
        calls.extend(_parse_tagged(match.group(2), match.group(3)))
    rest = _BLOCK_RE.sub("", text)
    dangling = _OPEN_RE.search(rest)
    if dangling is not None:
        tail = rest[dangling.end() :]
        if complete:
            recovered = _whole_tail_calls(_TOOL_TAG_RE.sub("", tail))
            if recovered:
                return calls + recovered, False
        if not calls:
            return [], True
        if "{" in tail:
            name = _cut_call_name(tail)
            calls.append(
                ToolCall(
                    tool=name,
                    arguments={},
                    error=(
                        "this tool_call was truncated (cut off) before it closed and did not run; "
                        "the complete calls before it ran. Resend only this call, complete"
                    ),
                )
            )
        return calls, False
    if calls:
        return calls, False
    if not allow_bare:
        return [], False
    for match in _FENCED_JSON_RE.finditer(text):
        if _looks_quoted(text, match.start()):
            continue
        for call in _parse_block(match.group(1)):
            if call.tool and not call.error:
                calls.append(call)
    if calls:
        return calls, False
    return _bare_json_calls(text), False


def _parse_tagged(attrs: str, body: str) -> list[ToolCall]:
    """One block. ``<tool_call name="x">{args}</tool_call>`` and inner XML tags are accepted."""
    attr = _ATTR_RE.search(attrs or "")
    inner_name = _INNER_NAME_RE.search(body)
    name = attr.group(2) if attr else (inner_name.group(1) if inner_name else "")
    if not name:
        return _parse_block(body)
    inner_args = _INNER_ARGS_RE.search(body)
    raw = inner_args.group(2) if inner_args else (body if not inner_name else "")
    raw = _strip_chrome(raw.strip())
    fenced = _FENCE_RE.match(raw)
    if fenced:
        raw = fenced.group(1).strip()
    if not raw.strip():
        return [_call_from_data({"tool": name, "arguments": {}})]
    try:
        data = _loads_lenient(raw)
    except json.JSONDecodeError as exc:
        return [ToolCall(tool=name, arguments={}, error=f"invalid tool JSON: {exc}")]
    if isinstance(data, dict) and any(key in data for key in ("tool", "name", "function")):
        return _calls_from_data(data)
    return [_call_from_data({"tool": name, "arguments": data})]


def _whole_tail_calls(tail: str) -> list[ToolCall]:
    """Calls in a block that lost only its closing tag: whole JSON, nothing cut."""
    stripped = _strip_chrome(tail.strip()).rstrip("`").rstrip()
    if not stripped.endswith("}"):
        return []
    blobs = _json_objects(stripped)
    if not blobs or not stripped.endswith(blobs[-1]):
        return []
    calls: list[ToolCall] = []
    for blob in blobs:
        try:
            found = _calls_from_data(_loads_lenient(blob))
        except json.JSONDecodeError:
            return []
        if any(call.error or not canonical_tool(call.tool) for call in found):
            return []
        calls.extend(found)
    return calls


def _cut_call_name(tail: str) -> str:
    match = _TAIL_TOOL_RE.search(tail)
    return match.group(1) if match else ""


def _cut_call_label(tail: str) -> str:
    """`` for write_files (path a.py)`` from the text of a cut-off block."""
    name = _cut_call_name(tail)
    if not name:
        return ""
    path = _TAIL_PATH_RE.search(tail)
    return f" for {name}" + (f" (path {path.group(1)})" if path else "")


def _looks_quoted(text: str, start: int) -> bool:
    """True when the words just before ``start`` introduce a quote, not a call."""
    lines = [
        line.strip()
        for line in text[:start].splitlines()
        if line.strip() and line.strip().lower() not in _CHROME_LINES and not line.strip().startswith("```")
    ]
    context = " ".join(lines[-2:])
    return bool(context and _QUOTED_CONTEXT_RE.search(context))


def _cut_fabricated(reply: str) -> tuple[str, bool]:
    """Cut the reply at the first tool_result the model wrote itself.

    Only the program writes results. A result inside a tool_call block (an
    edit that mentions the tag) is file text and stays.
    """
    spans = [match.span() for match in _BLOCK_RE.finditer(reply)]
    starts = [match.start() for match in _RESULT_OPEN_RE.finditer(reply)]
    starts += [match.start() for match in _BARE_RESULT_RE.finditer(reply)]
    for start in sorted(starts):
        if any(begin <= start < end for begin, end in spans):
            continue
        return reply[:start].rstrip(), True
    return reply, False


def _bare_json_calls(text: str) -> list[ToolCall]:
    """JSON objects naming a known tool, including ``{tool:"write_file", ...}``."""
    calls: list[ToolCall] = []
    for blob in _bare_tool_blobs(text):
        position = text.find(blob)
        if position >= 0 and _looks_quoted(text, position):
            continue
        try:
            data = _loads_lenient(blob)
        except json.JSONDecodeError:
            continue
        for call in _calls_from_data(data):
            if not call.error and canonical_tool(call.tool):
                calls.append(call)
    return calls


def _bare_tool_blobs(text: str) -> list[str]:
    """Raw JSON objects whose tool name this program can run."""
    if not _BARE_TOOL_KEY_RE.search(text):
        return []
    blobs: list[str] = []
    for blob in _json_objects(text):
        try:
            data = _loads_lenient(blob)
        except json.JSONDecodeError:
            continue
        if any(not call.error and canonical_tool(call.tool) for call in _calls_from_data(data)):
            blobs.append(blob)
    return blobs


def _hide_tool_markup(text: str) -> str:
    """Drop tool protocol from text that is about to be printed."""
    cleaned = _CLOSED_TOOL_MARKUP_RE.sub("", text)
    unclosed = _UNCLOSED_TOOL_RE.search(cleaned)
    if unclosed:
        after = _TOOL_TAG_RE.sub("", cleaned[unclosed.start():], count=1).lstrip()
        if not after or after[0] in "{[":
            cleaned = cleaned[: unclosed.start()]
        else:
            cleaned = _TOOL_TAG_RE.sub("", cleaned)
    else:
        cleaned = _TOOL_TAG_RE.sub("", cleaned)
    for blob in _bare_tool_blobs(cleaned):
        cleaned = cleaned.replace(blob, "", 1)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def commentary(text: str) -> str:
    return _hide_tool_markup(text)


def _answer_text(text: str) -> str:
    """The words the user should see, without a trailing COMPLETED or DONE."""
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    last = re.sub(r"[^a-z]+", " ", lines[-1].lower()).strip()
    if last in _STATUS_WORDS:
        lines = lines[:-1]
    body = "\n".join(lines).strip()
    if not body or body.upper() in {"READY", *_STATUS_WORDS.values()}:
        return ""
    return body


def format_tool_result(result: dict[str, Any]) -> str:
    payload: dict[str, Any] = {"tool": result.get("tool", ""), "ok": bool(result.get("ok"))}
    if payload["ok"]:
        payload["output"] = result.get("output", "")
    else:
        payload["error"] = result.get("error") or "tool failed"
        if result.get("output"):
            payload["output"] = result["output"]
        if result.get("allowed"):
            payload["allowed"] = list(result["allowed"])
    return "<tool_result>\n" + json.dumps(payload, ensure_ascii=False) + "\n</tool_result>"


def _strip_chrome(text: str) -> str:
    """Drop the language label and copy-button text a chat page puts above code."""
    lines = text.strip().split("\n")
    while lines and lines[0].strip().lower() in _CHROME_LINES:
        lines.pop(0)
    while lines and lines[-1].strip().lower() in _CHROME_LINES:
        lines.pop()
    return "\n".join(lines)


_TRAILING_PROSE_RE = re.compile(r"[}\]]\s*([A-Za-z][^{}\[\]\"`]*)\Z")


def _drop_trailing_prose(text: str) -> str:
    """``{...} I will read it now.`` -> ``{...}``. A JSON value never ends in a sentence."""
    match = _TRAILING_PROSE_RE.search(text)
    if match and " " in match.group(1).strip():
        return text[: match.start(1)].rstrip()
    return text


def _parse_block(body: str) -> list[ToolCall]:
    text = body.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    text = _drop_trailing_prose(_strip_chrome(text))
    objects = _json_objects(_repair_json(_normalize_json_text(text))) or _json_objects(text)
    if not objects:
        try:
            data = _loads_lenient(text)
        except json.JSONDecodeError as exc:
            return [ToolCall(tool="", arguments={}, error=f"invalid tool JSON: {exc}")]
        return _calls_from_data(data)
    calls: list[ToolCall] = []
    for blob in objects:
        try:
            data = _loads_lenient(blob)
        except json.JSONDecodeError as exc:
            calls.append(ToolCall(tool="", arguments={}, error=f"invalid tool JSON: {exc}"))
            continue
        calls.extend(_calls_from_data(data))
    return calls


_NAME_KEYS = ("tool", "name", "tool_name", "recipient_name")
_ARG_KEYS = ("arguments", "args", "parameters", "params", "input", "tool_input")
_LIST_KEYS = ("tool_calls", "calls", "tool_uses", "actions")
_WRAPPER_TYPES = {"function", "tool", "tool_call", "tool_use", "function_call"}


def _calls_from_data(data: Any) -> list[ToolCall]:
    """Calls from one parsed value: an array of calls, ``{"tool_calls": [...]}``,
    the OpenAI ``{"function": {"name", "arguments"}}`` shape, or one call."""
    if isinstance(data, list):
        calls = [call for item in data for call in _calls_from_data(item)]
        return calls or [ToolCall(tool="", arguments={}, error="tool call must be a JSON object")]
    if isinstance(data, dict):
        named = any(isinstance(data.get(key), str) for key in _NAME_KEYS)
        if not named:
            for key in _LIST_KEYS:
                if isinstance(data.get(key), list):
                    return _calls_from_data(data[key])
            function = data.get("function")
            if isinstance(function, dict):
                return [_call_from_data(function)]
            if isinstance(function, str):
                data = {**data, "tool": function}
                data.pop("function", None)
            tool = data.get("tool")
            if isinstance(tool, dict):
                return [_call_from_data(tool)]
    return [_call_from_data(data)]


def _call_from_data(data: Any) -> ToolCall:
    if not isinstance(data, dict):
        return ToolCall(tool="", arguments={}, error="tool call must be a JSON object")
    name: Any = None
    name_key = ""
    for key in _NAME_KEYS:
        if isinstance(data.get(key), str) and data[key].strip():
            name, name_key = data[key], key
            break
    if not isinstance(name, str) or not name.strip():
        return ToolCall(tool="", arguments={}, error="tool call needs a tool name")
    raw_name = name
    name = re.sub(r"^(?:functions|tools|tool)\.", "", name.strip())
    if "ok" in data and ("output" in data or "error" in data) and not any(key in data for key in _ARG_KEYS):
        return ToolCall(
            tool=name, arguments={}, error="that is a tool_result; only the program writes results"
        )
    arg_key = next((key for key in _ARG_KEYS if key in data), None)
    if arg_key is not None:
        arguments = data.get(arg_key)
    else:
        arguments = {
            key: value
            for key, value in data.items()
            if key != name_key
            and not (key in _NAME_KEYS and value == raw_name)
            and not (key == "type" and str(value).lower() in _WRAPPER_TYPES)
            and key not in {"function"}
        }
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        if not arguments.strip():
            arguments = {}
        else:
            try:
                arguments = _loads_lenient(arguments)
            except json.JSONDecodeError as exc:
                return ToolCall(tool=name, arguments={}, error=f"arguments must be a JSON object: {exc}")
    if not isinstance(arguments, dict):
        return ToolCall(tool=name, arguments={}, error="arguments must be a JSON object")
    return ToolCall(tool=name, arguments=arguments)


def _loads_lenient(text: str) -> Any:
    cleaned = _normalize_json_text(text)
    candidates = [cleaned, _TRAILING_COMMA_RE.sub(r"\1", cleaned)]
    repaired = _repair_json(cleaned)
    if _RAW_WIN_PATH_RE.search(cleaned):
        # C:\new\tools is valid JSON with a newline and a tab in it. The
        # repair keeps those backslashes, so it goes first.
        candidates.insert(0, repaired)
    elif repaired not in candidates:
        candidates.append(repaired)
    for blob in _json_objects(repaired):
        if blob not in candidates:
            candidates.append(blob)
    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise json.JSONDecodeError("invalid tool JSON", cleaned, 0)


def _normalize_json_text(text: str) -> str:
    cleaned = text.strip().lstrip("\ufeff")
    return (
        cleaned.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )


def _repair_json(text: str) -> str:
    """Turn the JSON models actually emit into text json.loads can read.

    The usual breaks are a missing comma, a raw newline inside a string,
    an unquoted key, single quotes, and a quote inside a string that the
    model did not escape (``name = "test-app"``). Those come back as
    ``Expecting ',' delimiter`` or ``Expecting value``.
    """
    source = text
    out: list[str] = []
    index = 0
    length = len(source)
    stack: list[str] = []
    after_value = False
    expect_key = False

    def skip_space(pos: int) -> int:
        while pos < length and source[pos] in " \t\r\n":
            pos += 1
        return pos

    while index < length:
        char = source[index]
        if char in " \t\r\n":
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "/":
            index += 2
            while index < length and source[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "*":
            index += 2
            while index + 1 < length and source[index : index + 2] != "*/":
                index += 1
            index = min(length, index + 2)
            continue
        triple = source[index : index + 3]
        if triple in ('"""', "'''") or char == "`":
            # Python triple quotes or a JS template string: raw text to the closer.
            closer = triple if triple in ('"""', "'''") else ("```" if triple == "```" else "`")
            start = index + len(closer)
            end = source.find(closer, start)
            if end < 0:
                end = length
            if after_value and stack:
                out.append(",")
            body = source[start:end]
            if closer == "```" and "\n" in body:
                first, rest = body.split("\n", 1)
                if first.strip().isalnum() or not first.strip():
                    body = rest
            out.append(json.dumps(body.replace("\r\n", "\n"), ensure_ascii=False))
            index = min(length, end + len(closer))
            if expect_key:
                expect_key = False
                after_value = False
            else:
                after_value = True
            continue
        if char in "\"'":
            if after_value and stack:
                out.append(",")
                expect_key = bool(stack and stack[-1] == "{")
                after_value = False
            literal, index = _read_json_string(
                source, index, char, key=expect_key, container=stack[-1] if stack else ""
            )
            out.append(literal)
            if expect_key:
                nxt = skip_space(index)
                if nxt < length and source[nxt] != ":":
                    out.append(":")
                expect_key = False
                after_value = False
            else:
                after_value = True
            continue
        if char in "{[":
            if after_value and stack:
                out.append(",")
            out.append(char)
            stack.append(char)
            after_value = False
            expect_key = char == "{"
            index += 1
            continue
        if char in "}]":
            while out and out[-1] in " \t\r\n":
                out.pop()
            if out and out[-1] == ",":
                out.pop()
            out.append(char)
            if stack:
                stack.pop()
            after_value = True
            expect_key = False
            index += 1
            continue
        if char == ":":
            out.append(char)
            after_value = False
            expect_key = False
            index += 1
            continue
        if char == ",":
            if not after_value:
                index += 1
                continue
            out.append(char)
            after_value = False
            expect_key = bool(stack and stack[-1] == "{")
            index += 1
            continue
        if char.isalpha() or char in "_$":
            end = index + 1
            while end < length and (source[end].isalnum() or source[end] in "_$"):
                end += 1
            word = source[index:end]
            if after_value and stack:
                out.append(",")
                after_value = False
            if word in {"true", "false", "null"}:
                out.append(word)
            elif word in {"True", "False", "None", "undefined"}:
                out.append({"True": "true", "False": "false", "None": "null", "undefined": "null"}[word])
            else:
                out.append(json.dumps(word))
            after_value = True
            index = end
            continue
        if char == "-" or char.isdigit():
            if after_value and stack:
                out.append(",")
            end = index + 1
            while end < length and source[end] in "0123456789.eE+-":
                end += 1
            out.append(source[index:end])
            after_value = True
            index = end
            continue
        index += 1
    # A reply that ends before its last braces: close what is still open.
    while out and out[-1] in " \t\r\n,:":
        out.pop()
    for opener in reversed(stack):
        out.append("}" if opener == "{" else "]")
    return _TRAILING_COMMA_RE.sub(r"\1", "".join(out))


_JSON_STRING_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "'": "'",
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
}


_PATH_LEAD = frozenset(" \t\"'=(,;[{")


def _raw_path_start(chars: list[str]) -> bool:
    """True when the text so far ends in ``C:`` or ``.``/``..`` that starts a path."""
    if len(chars) >= 2 and chars[-1] == ":" and chars[-2].isalpha():
        return len(chars) == 2 or chars[-3] in _PATH_LEAD
    if chars and chars[-1] == ".":
        before = chars[:-1]
        if before and before[-1] == ".":
            before = before[:-1]
        return not before or before[-1] in _PATH_LEAD
    return False


def _read_json_string(
    source: str, index: int, quote: str, *, key: bool = False, container: str = ""
) -> tuple[str, int]:
    """Read one quoted string, keeping interior quotes and raw newlines."""
    index += 1
    length = len(source)
    chars: list[str] = []
    raw_paths = False
    while index < length:
        char = source[index]
        if char == "\\":
            if index + 1 >= length:
                break
            escaped = source[index + 1]
            if not raw_paths and escaped not in "\\\"'" and _raw_path_start(chars):
                # C:\new\tools written without doubled backslashes: from here
                # on a backslash is part of the path, not an escape.
                raw_paths = True
            if raw_paths and escaped not in "\\\"'":
                chars.append("\\")
                index += 1
                continue
            if escaped == "u" and index + 5 < length:
                try:
                    chars.append(chr(int(source[index + 2 : index + 6], 16)))
                    index += 6
                    continue
                except ValueError:
                    pass
            # A single backslash in a Windows path (\gradle, \bin) is not a
            # JSON escape. Dropping it glues the folders together. \b and \f
            # before a letter are the same: \bin, not a backspace.
            if escaped in {"b", "f"} and index + 2 < length and (
                source[index + 2].isalnum() or source[index + 2] in "._"
            ):
                chars.append("\\")
                chars.append(escaped)
            elif escaped in _JSON_STRING_ESCAPES:
                chars.append(_JSON_STRING_ESCAPES[escaped])
            else:
                chars.append("\\")
                chars.append(escaped)
            index += 2
            continue
        if char == quote and _string_ends(source, index, quote, key=key, container=container):
            return json.dumps("".join(chars), ensure_ascii=False), index + 1
        if char == "\r":
            index += 1
            continue
        chars.append(char)
        index += 1
    return json.dumps("".join(chars), ensure_ascii=False), index


def _string_ends(source: str, index: int, quote: str, *, key: bool, container: str = "") -> bool:
    """True when the quote at index closes the string rather than sitting inside it.

    A value may contain source text such as ``name = "test-app"`` or
    ``include(":app")``. A following letter, digit, or colon is that text.
    The quote closes the value when the next token continues the JSON
    (``,``, ``}``, ``]``) or is the next object key. Inside an object, a
    comma closes the value only when a key follows it, so source text such
    as ``print("a", "b")`` stays in the string.
    """
    pos = index + 1
    length = len(source)
    while pos < length and source[pos] in " \t\r\n":
        pos += 1
    if pos >= length:
        return True
    nxt = source[pos]
    if nxt == ",":
        if key or container != "{":
            return True
        return _key_follows(source, pos + 1)
    if nxt in "}]":
        return _closer_is_json(source, pos)
    if nxt == ":":
        return key
    if nxt != quote:
        return (not key) and _json_value_at(source, pos)
    end = pos + 1
    escaped = False
    while end < length:
        char = source[end]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            break
        end += 1
    end += 1
    while end < length and source[end] in " \t\r\n":
        end += 1
    return end < length and source[end] == ":"


def _key_follows(source: str, pos: int) -> bool:
    """True when ``source[pos:]`` starts with an object key and its colon (or the object ends)."""
    length = len(source)
    while pos < length and source[pos] in " \t\r\n":
        pos += 1
    if pos >= length or source[pos] in "}]":
        return True
    char = source[pos]
    if char in "\"'":
        end = source.find(char, pos + 1)
        if end < 0 or "\n" in source[pos + 1 : end]:
            return False
        pos = end + 1
    elif char.isalpha() or char in "_$":
        while pos < length and (source[pos].isalnum() or source[pos] in "_$"):
            pos += 1
    else:
        return False
    while pos < length and source[pos] in " \t":
        pos += 1
    return pos < length and source[pos] == ":"


def _closer_is_json(source: str, pos: int) -> bool:
    """True when a ``}`` or ``]`` after a quote closes JSON, not source text.

    ``text = "Hello" }`` is Kotlin. The brace is JSON only when what follows
    it continues the tool call (a comma, another closer, or the end).
    """
    index = pos
    length = len(source)
    while index < length and source[index] in "}]":
        index += 1
    while index < length and source[index] in " \t\r\n":
        index += 1
    if index >= length:
        return True
    char = source[index]
    if char in ",:\"'{[":
        return True
    if char.isdigit() or char == "-":
        return True
    for word in ("true", "false", "null"):
        if source.startswith(word, index):
            return True
    return False


def _json_value_at(source: str, pos: int) -> bool:
    """True when ``source[pos:]`` is a JSON value that can follow a string."""
    if source[pos] in "{[":
        return True
    for word in ("true", "false", "null"):
        if not source.startswith(word, pos):
            continue
        after = pos + len(word)
        return after >= len(source) or source[after] in ",}] \t\r\n"
    return False


def _json_objects(text: str) -> list[str]:
    found: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    start: int | None = None
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
            continue
        if char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                found.append(text[start : index + 1])
                start = None
    return found


# --------------------------------------------------------------------------- reply classification


def _status_code(text: str) -> str | None:
    """Return a finish code when the reply is that word, starts with FAILED or BLOCKED, or ends with it."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    compact = re.sub(r"[^a-z]+", " ", text.lower()).strip()
    if compact in _STATUS_WORDS:
        return _STATUS_WORDS[compact]
    head = _FAIL_HEAD.match(lines[0])
    if head:
        return _STATUS_WORDS[head.group(1).lower()]
    if len(lines) > 4:
        return None
    last = re.sub(r"[^a-z]+", " ", lines[-1].lower()).strip()
    return _STATUS_WORDS.get(last)


def _failure_reason(text: str) -> str:
    """The sentences after FAILED or BLOCKED. Empty when the reply is only the word."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    kept: list[str] = []
    for line in lines:
        match = _FAIL_HEAD.match(line)
        if match:
            if match.group(2).strip():
                kept.append(match.group(2).strip())
            continue
        if re.sub(r"[^a-z]+", " ", line.lower()).strip() in _STATUS_WORDS:
            continue
        kept.append(line)
    return " ".join(kept).strip()


def _normalized(text: str) -> str:
    return text.lower().replace("\u2019", "'").replace("\u2018", "'")


def _no_edit_needed(text: str) -> bool:
    """True when the model says the task needs no further edit."""
    if _status_code(text) in _STATUS_OK:
        return True
    normalized = _normalized(text)
    if any(marker in normalized for marker in ("i'll ", "i will ", "let me ", "going to ", "next i")):
        return False
    markers = (
        "no edit needed", "no edits needed", "no edit is needed", "no further edit",
        "no further change", "no change needed", "no changes needed", "nothing to change",
        "nothing to edit", "already present", "already done", "already applied",
        "already contains", "already in the file", "no modification", "does not need an edit",
        "does not need to edit", "do not need to edit", "don't need to edit", "no additional change",
    )
    return any(marker in normalized for marker in markers)


_REFUSAL_MARKERS = (
    "i can't", "i cannot", "unable", "not able", "aren't available", "are not available",
    "not available", "isn't available", "not exposed", "not expose", "tool interface",
    "no tool", "no repository",
    "file-operation", "file operation", "don't have", "do not have", "i won't", "i will not",
)
_QUESTION_MARKERS = (
    "what would you like", "what should i", "would you like", "let me know", "for example",
    "shall i", "do you want", "what do you want",
)
_PROMISE_MARKERS = ("i'll ", "i will ", "let me ", "next i", "going to ")
#: The model handing the work back: "build it yourself", "run this in Android Studio".
_HAND_BACK_RE = re.compile(
    r"(?:\b(?:yourself|manually)\b"
    r"|\bon your (?:machine|computer|system|side|end|local)\b"
    r"|\b(?:please|you can|you could|you should|you(?:'ll| will)? need to|you may need to|you must|kindly)\s+"
    r"(?:\w+\s+){0,2}(?:run|build|execute|install|open|launch|rebuild|sync|try running)\b"
    r"|\b(?:open|build|run|sync)\b[^.\n]{0,40}\bin android studio\b"
    r"|\brun (?:the following|these|this) commands?\b"
    r"|\bi (?:can(?:no|')t|am unable to|'m unable to|do not have the ability to|don't have the ability to) (?:run|execute|build)\b)",
    re.I,
)
MAX_HAND_BACKS = 2


def _hands_back(text: str) -> bool:
    """True when the reply tells the user to run, build, or install something themselves."""
    return bool(_HAND_BACK_RE.search(_hide_tool_markup(text or "")))


def _refuses(text: str) -> bool:
    normalized = _normalized(text)
    return any(marker in normalized for marker in _REFUSAL_MARKERS)


def _stalls(text: str) -> bool:
    """True when the reply asks the user, refuses, or only promises work."""
    normalized = _normalized(text)
    if "?" in normalized:
        return True
    markers = _QUESTION_MARKERS + _REFUSAL_MARKERS + _PROMISE_MARKERS
    return any(marker in normalized for marker in markers)


def _asks_user(text: str) -> bool:
    normalized = _normalized(text)
    return any(marker in normalized for marker in _QUESTION_MARKERS)


def _only_promises(text: str) -> bool:
    """True when the reply only says it will go look, and has not answered yet."""
    return bool(_PROMISE_START_RE.match(_normalized(text).strip()))


def _answer_only(task: str) -> bool:
    """True when the user asked a question and did not ask for a file change.

    A question that also says to fix or add something is still a task: the
    answer and the tool calls belong in the same reply.
    """
    text = " ".join(task.split())
    if not text or _asks_for_change(text):
        return False
    if text.endswith("?"):
        return True
    return bool(_ANSWER_TASK_RE.match(text))


def _plan_text(reply: str) -> str | None:
    parts = [part.strip() for part in _PLAN_RE.findall(reply)]
    if parts:
        return "\n".join(part for part in parts if part)
    bare = _BLOCK_RE.sub("", reply).strip()
    if bare and _UNTAGGED_PLAN_RE.search(bare):
        return bare
    return None


def _idle_tool_reply(detail: Any, reply: str) -> bool:
    """True when an idle reply contains a tool tag that did not finish."""
    if not isinstance(detail, dict) or detail.get("completion") != COMPLETION_IDLE:
        return False
    if "<tool_call" not in reply.lower():
        return False
    calls, unclosed = parse_tool_calls(reply)
    return unclosed or not calls


def _asks_for_change(task: str) -> bool:
    return bool(_EDIT_TASK_RE.search(task))


def _provider_error(reply: str) -> str | None:
    """The chat page's own error text (or nothing) in place of a model reply."""
    text = (reply or "").strip()
    if not text:
        return "the chat page returned an empty reply"
    if len(text) > _PROVIDER_ERROR_MAX or _OPEN_RE.search(text):
        return None
    match = _PROVIDER_ERROR_RE.search(text)
    if match is None:
        return None
    return f"the chat page said: {_one_line(text, 120)}"


def _real_question(reply: str) -> str:
    """The question when a reply genuinely asks the user something, else "".

    Questions that only stall (what should I change, shall I proceed, may I)
    and refusals are not real questions: the task already says what to do.
    """
    text = _hide_tool_markup(reply).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[-1].endswith("?") or len(text) > 1_500:
        return ""
    normalized = _normalized(text)
    if _refuses(text) or any(marker in normalized for marker in _QUESTION_MARKERS + _PERMISSION_MARKERS):
        return ""
    return text


def _diffstat(diff: str) -> str:
    """``a.py +3 -1, b.py +10 -0`` from a unified diff."""
    counts: dict[str, list[int]] = {}
    current = ""
    old = ""
    lines = diff.splitlines()
    for number, line in enumerate(lines):
        header = number + 1 < len(lines) and lines[number + 1].startswith("+++ ")
        if line.startswith("--- ") and header:
            old = re.sub(r"^--- (?:a/)?", "", line).strip()
            current = ""
        elif line.startswith("+++ ") and number > 0 and lines[number - 1].startswith("--- "):
            current = re.sub(r"^\+\+\+ (?:b/)?", "", line).strip()
            if current == "/dev/null":
                current = old if old != "/dev/null" else ""
            if current:
                counts.setdefault(current, [0, 0])
        elif current and line.startswith("+"):
            counts[current][0] += 1
        elif current and line.startswith("-"):
            counts[current][1] += 1
    return ", ".join(f"{path} +{add} -{rem}" for path, (add, rem) in counts.items())


def _has_checks(workspace: Path) -> bool:
    """True when the workspace has tests or a build the model could run."""
    try:
        return any((workspace / marker).exists() for marker in _TEST_MARKERS)
    except OSError:
        return False


def _call_key(call: ToolCall) -> str:
    name = canonical_tool(call.tool) or call.tool
    return name + ":" + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)


def _keywords(task: str) -> list[str]:
    words: list[str] = []
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.\-/]{2,}", task):
        words.append(token)
        words.extend(code_index.split_ident(Path(token).stem))
    stop = {"the", "and", "for", "with", "this", "that", "from", "into", "file", "files", "code", "test", "tests"}
    seen: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in stop or len(lowered) < 3 or lowered in seen:
            continue
        seen.append(lowered)
    return seen[:20]


# --------------------------------------------------------------------------- prompt file


def load_prompt_sections() -> dict[str, str]:
    """Read prompts/agent.txt. Model-facing text lives only in that file."""
    path = _agent_prompt_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError(f"could not read agent instructions {path}: {exc}") from exc
    if not text.strip():
        raise OSError(f"agent instructions are empty: {path}")
    parts = _SECTION_RE.split(text)
    sections: dict[str, str] = {}
    if len(parts) >= 3:
        for index in range(1, len(parts), 2):
            sections[parts[index]] = parts[index + 1].strip()
    if "SYSTEM" not in sections:
        sections["SYSTEM"] = text.strip()
    for key, value in _FALLBACKS.items():
        sections.setdefault(key, value)
    return sections


def load_agent_instructions() -> str:
    return load_prompt_sections()["SYSTEM"]


def task_message(task: str, *, repo_map: str = "", skills: str = "", mode_note: str = "") -> str:
    """The task with the short protocol reminder, the repo map, skills, and the mode note."""
    body = task.strip()
    prefix = load_prompt_sections()["TASK_PREFIX"].rstrip()
    if body.startswith(prefix.splitlines()[0]):
        return body
    parts = [prefix]
    if mode_note.strip():
        parts.append(mode_note.strip())
    if skills.strip():
        parts.append(skills.strip())
    if repo_map.strip():
        parts.append(
            "REPO MAP (files ranked for this task; numbers are definition lines; "
            "read before editing)\n" + repo_map.strip()
        )
    parts.append("Task:\n" + body)
    return "\n\n".join(parts)


def seed_message(
    workspace: Path,
    instructions: str,
    *,
    shell: agent_shell.Shell | None = None,
    notes: str = "",
    platform_name: str | None = None,
    persistent_cwd: bool = True,
    available: dict[str, agent_shell.Shell] | None = None,
    skills: list[str] | None = None,
    helpers: int = 0,
    web_hosts: tuple[str, ...] | None = None,
) -> str:
    chosen = shell or agent_shell.detect_shell(platform_name)
    lines = [
        "ENVIRONMENT",
        f"os: {platform_name or sys.platform}",
        f"shell: {chosen.label}",
        f"workspace: {workspace}",
    ]
    lines.extend(agent_shell.tool_hints(workspace, platform_name))
    lines.append(code_graph.hint(workspace))
    sites = agent_tools.DEFAULT_WEB_HOSTS if web_hosts is None else web_hosts
    if sites:
        lines.append("web_fetch sites (https, plain page addresses only): " + ", ".join(sites))
    else:
        lines.append("web_fetch: off (nothing leaves this machine but the chat)")
    if helpers > 0:
        lines.append(
            f"helper tabs: {helpers}. Split a task with independent parts across them with one delegate call; "
            "they work at the same time."
        )
    else:
        lines.append("helper tabs: none (do not call delegate; do the work here)")
    if skills:
        lines.append(
            "skills: " + ", ".join(skills) + ". The ones that fit a task come with it under SKILLS; "
            "load another with the skill tool when the work turns to its domain."
        )
    if persistent_cwd:
        lines.append(
            "run_command starts in the workspace above. cd persists between run_command calls, "
            "as in a terminal; a result shows cwd when it changed. "
            "The result starts with exit, the code and seconds, then stdout, then stderr."
        )
    else:
        lines.append(
            "Each run_command is a new process whose starting folder is the workspace above, "
            "not C:\\ and not the user profile; pass cwd to start somewhere else. "
            "Its result starts with exit, the code and seconds, then cwd, then stdout, then stderr."
        )
    text = agent_shell.shell_preamble(chosen, available) + "\n\n"
    if instructions.strip():
        text += instructions.rstrip() + "\n\n"
    text += "\n".join(lines) + "\n"
    if notes.strip():
        text += "\nPROJECT NOTES (from .bot/AGENT.md; follow them)\n" + notes.strip() + "\n"
    if READY_LINE not in text:
        text += "\n" + READY_LINE + "\n"
    return text


def _without_ready(seed: str) -> str:
    """The seed for a message that also carries the task: no READY handshake."""
    return re.sub(r"\n*[^\n]*\bexactly READY\b[^\n]*", "", seed).strip()


MAX_SKILL_CHARS = 14_000


def skills_block(items: list[dict[str, Any]], *, max_chars: int = MAX_SKILL_CHARS) -> str:
    """The SKILLS part of a task message: the body of each chosen skill, within ``max_chars``."""
    text_of = getattr(agent_tools, "skill_text", None)
    if not items or not callable(text_of):
        return ""
    parts: list[str] = []
    room = max_chars
    for item in items:
        body = text_of(item, max_chars=min(7_000, room))
        if not body:
            continue
        chunk = f"=== skill: {item['name']} ===\n{body}"
        if len(chunk) > room:
            break
        parts.append(chunk)
        room -= len(chunk)
    if not parts:
        return ""
    return (
        "SKILLS (expert guidance for this task's domain; apply it unless the task or PROJECT NOTES say otherwise)\n"
        + "\n\n".join(parts)
    )


def _web_hosts(settings: dict[str, Any]) -> tuple[str, ...]:
    """web_fetch's sites: the documentation defaults plus web_fetch_hosts; "web_fetch": false turns it off."""
    if settings.get("web_fetch") is False:
        return ()
    extra = [host.lower().strip().strip(".") for host in _setting_list(settings, "web_fetch_hosts")]
    return tuple(dict.fromkeys([*agent_tools.DEFAULT_WEB_HOSTS, *[host for host in extra if host]]))


def _setting_list(settings: dict[str, Any], key: str) -> list[str]:
    raw = settings.get(key)
    if isinstance(raw, str):
        raw = [raw]
    return [str(item).strip() for item in raw or [] if str(item).strip()] if isinstance(raw, list) else []


def _agent_prompt_path() -> Path:
    package_dir = Path(__file__).resolve().parent
    candidates = [
        package_dir / "prompts" / "agent.txt",
        Path(sys.executable).resolve().parent / "prompts" / "agent.txt",
    ]
    try:
        candidates.append(package_dir.parents[2] / "prompts" / "agent.txt")
    except IndexError:
        pass
    candidates.append(Path.cwd() / "prompts" / "agent.txt")
    for path in candidates:
        if path.is_file():
            return path
    raise OSError("agent instructions not found (prompts/agent.txt)")


# --------------------------------------------------------------------------- the loop


@dataclass
class _Perm:
    """Stand-in for agent_tools.Permission when that is not available."""

    kind: str = "read"
    summary: str = ""
    key: str = ""
    detail: str = ""


class _ProviderFailed(RuntimeError):
    """The chat page kept showing an error instead of a reply."""


def _tool_context(**values: Any) -> ToolContext:
    """A ToolContext with only the fields this version of agent_tools has."""
    names = {item.name for item in dataclasses.fields(ToolContext)}
    return ToolContext(**{key: value for key, value in values.items() if key in names})


def _int_setting(settings: dict[str, Any], key: str, default: int, minimum: int) -> int:
    raw = settings.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < minimum:
        return default
    return int(raw)


_MODES = frozenset({"ask", "edits", "auto", "plan"})


def _ui_approve_mode() -> str:
    """The approval mode the terminal UI holds now, or "" when there is no UI."""
    getter = getattr(globals().get("agent_ui"), "approve_mode", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:
        return ""


def _read_only_tools() -> frozenset[str]:
    return getattr(agent_tools, "READ_ONLY", None) or _READ_ONLY_FALLBACK


def _stop_reply(session: Any) -> None:
    """Ask the chat page to stop a reply it is still writing. Best effort."""
    stop = getattr(session, "stop_generation", None)
    if not callable(stop):
        return
    try:
        stop()
    except Exception as exc:  # the next send waits for (or stops) it again
        log.debug(f"stopping the reply failed: {exc}")


class _Chat:
    """The open conversation: sends with retries, tracks its size, starts over when asked.

    A web chat slows down and forgets the protocol as it grows. Past
    ``compact_after`` characters, or after the model breaks the protocol
    ``AMNESIA_VIOLATIONS`` times in a row, the next message goes to a new
    chat with the instructions and a summary in front of it.
    """

    def __init__(
        self,
        session: Any,
        *,
        seed: str = "",
        retries: int = DEFAULT_REPLY_RETRIES,
        compact_after: int = DEFAULT_COMPACT_AFTER_CHARS,
        quiet: bool = False,
        compact_turns: int = DEFAULT_COMPACT_AFTER_TURNS,
        compact_minutes: int = DEFAULT_COMPACT_AFTER_MINUTES,
    ) -> None:
        self.session = session
        #: A helper tab's chat: no spinner and no notes in the terminal.
        self.quiet = quiet
        self.seed = (seed or "").strip()
        self.retries = max(0, retries)
        self.compact_after = compact_after
        self.chars = 0
        self.violations = 0
        self.compact_turns = max(1, int(compact_turns or DEFAULT_COMPACT_AFTER_TURNS))
        self.compact_seconds = max(60, int(compact_minutes or DEFAULT_COMPACT_AFTER_MINUTES) * 60)
        #: Messages answered in this chat, and when it started.
        self.sends = 0
        self.born = time.monotonic()
        #: Set by the running task: what a new chat needs to carry on (see _TaskRun._resume_text).
        self.resume: Callable[[], str] | None = None
        self.map_sent = False
        #: Skills already sent in this chat; a new chat sends them again.
        self.skills_sent: set[str] = set()
        #: Sent ahead of the next message: the instructions, in a fresh chat.
        self.prefix = ""
        self.can_restart = callable(getattr(session, "new_chat", None))
        #: "task -> outcome" for each task in this session, for the summary.
        self.history: list[str] = []

    @property
    def last_detail(self) -> Any:
        return getattr(self.session, "last_detail", None)

    def send(self, payload: str) -> str:
        """Send and return the reply, retrying when the page fails or never answers.

        The first retry goes to the same chat. After that the conversation
        itself may be broken, so the next try opens a new chat with the
        instructions and the running task's summary in front of the message.
        """
        attempt = 0
        moved = False
        # A retry in the same chat resends exactly the same text, instructions included:
        # a message that never went out must not lose them.
        prefix, self.prefix = self.prefix, ""
        while True:
            outgoing = prefix + "\n\n" + payload if prefix else payload
            outgoing, hidden = agent_tools.redact_secrets(outgoing)
            if hidden:
                self._note("note", f"Kept {hidden} secret{'s' if hidden != 1 else ''} out of the message to the chat.")
            if not self.quiet:
                _waiting(True)
            try:
                reply = self.session.send(outgoing)
                problem = _provider_error(reply)
            except ChatError as exc:
                reply, problem = "", f"the chat page failed: {_one_line(str(exc), 160)}"
            finally:
                if not self.quiet:
                    _waiting(False)
            self.chars += len(outgoing) + len(reply or "")
            if problem is None:
                self.sends += 1
                return reply
            if attempt >= self.retries:
                raise _ProviderFailed(problem)
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            attempt += 1
            # A timed-out reply may still be streaming; resending over it
            # duplicates the message and crosses the two answers.
            _stop_reply(self.session)
            if attempt > RETRIES_BEFORE_NEW_CHAT and not moved and self.can_restart:
                if self.restart("the chat stopped answering"):
                    moved = True
                    prefix, self.prefix = self.prefix, ""
                    summary = ""
                    if callable(self.resume):
                        try:
                            summary = self.resume() or ""
                        except Exception as exc:  # the retry still goes out without the summary
                            log.debug(f"could not build the summary for the new chat: {exc}")
                    if summary:
                        payload = summary + "\n\n" + payload
                    self._note("note", f"{problem[:1].upper()}{problem[1:]}. Trying again in a new chat ({attempt}/{self.retries}).")
                    _sleep(min(delay, 5.0))
                    continue
            self._note("note", f"{problem[:1].upper()}{problem[1:]}. Sending again in {delay:.0f}s ({attempt}/{self.retries}).")
            _sleep(delay)

    def _note(self, kind: str, message: str) -> None:
        if not self.quiet:
            _ui(kind, message)

    def too_long(self) -> bool:
        return self.can_restart and bool(self.rotation_reason(size_only=True))

    def rotation_reason(self, *, size_only: bool = False) -> str:
        """Why this chat should move to a new one now, or ""."""
        if self.chars > self.compact_after:
            return "the chat grew long"
        if self.sends >= self.compact_turns:
            return f"the chat reached {self.sends} messages"
        minutes = (time.monotonic() - self.born) / 60
        if self.sends and minutes * 60 >= self.compact_seconds:
            return f"the chat has been open {minutes:.0f} minutes"
        if not size_only and self.violations >= AMNESIA_VIOLATIONS:
            return "the model lost track of the instructions"
        return ""

    def wants_restart(self) -> bool:
        return self.can_restart and (self.too_long() or self.violations >= AMNESIA_VIOLATIONS)

    def restart(self, reason: str) -> bool:
        """Open a new conversation. The instructions go in front of the next message."""
        if not self.can_restart:
            return False
        try:
            started = self.session.new_chat()
        except Exception as exc:  # the old chat still works; keep using it
            log.warn(f"could not open a new chat: {exc}")
            started = False
        if started is False:
            self.can_restart = False
            return False
        self._note("note", f"Starting a new chat: {reason}.")
        self.chars = 0
        self.violations = 0
        self.sends = 0
        self.born = time.monotonic()
        self.map_sent = False
        self.skills_sent = set()
        self.prefix = _without_ready(self.seed) if self.seed else ""
        if self.history:
            earlier = "EARLIER IN THIS SESSION (tasks done in the previous chat, newest last):\n- " + "\n- ".join(
                self.history[-8:]
            )
            self.prefix = (self.prefix + "\n\n" + earlier).strip()
        return True


class _Hooks:
    """Where a task run reports to. The terminal by default; helper tabs pass quiet ones."""

    def ui(self, kind: str, message: str) -> None:
        _ui(kind, message)

    def tool_start(self, call: ToolCall) -> None:
        _tool_start(call)

    def tool_done(self, call: ToolCall, result: dict[str, Any]) -> None:
        _tool_done(call, result)

    def status(self, code: str) -> None:
        _print_status(code)


class _TaskRun:
    """One task on an open chat: send, parse, run tools, repeat until a finish."""

    def __init__(
        self,
        chat: _Chat,
        task: str,
        *,
        ctx: ToolContext,
        sections: dict[str, str],
        turns: list[dict[str, str]],
        show: Callable[[str], None],
        max_rounds: int | None,
        max_result_chars: int,
        check_command: str | None,
        check_timeout: float = DEFAULT_CHECK_TIMEOUT,
        approve_mode: str = "ask",
        always: set[str] | None = None,
        ask: Callable[[str], str | None] | None = None,
        ui_mode_at_start: str | None = None,
        confirm_risky: bool = True,
        hooks: "_Hooks | None" = None,
    ) -> None:
        self.hooks = hooks or _Hooks()
        self.chat = chat
        self.task = task
        self.ctx = ctx
        self.state = ctx.state or TaskState(task=task)
        self.sections = sections
        self.turns = turns
        self.show = show
        self.max_rounds = max_rounds
        self.max_result_chars = max_result_chars
        self.check_command = (check_command or "").strip() or None
        self.check_timeout = check_timeout
        self.approve_mode = approve_mode
        self.ui_mode_at_start = ui_mode_at_start if ui_mode_at_start is not None else _ui_approve_mode()
        self.always = always if always is not None else set()
        self.ask = ask
        self.confirm_risky = confirm_risky
        #: Plan mode: the plan was approved (or the task was not planned), so work goes ahead.
        self.plan_done = False
        self.plan_asks = 0
        self.plans = 0
        self.hand_backs = 0
        #: Ask the old chat for a handoff note before moving to a new one.
        self.handoff = True
        self.tools_ran = False
        self.last_failed = False
        self.nudges = 0
        self.refusals = 0
        self.recoveries = 0
        self.truncations = 0
        self.plan_notes = 0
        self.check_cycles = 0
        self.check_passed = False
        self.nothing_nudged = False
        self.reason_asked = False
        self.verify_asked = False
        self.verified = True
        self.questions = 0
        self.reviewed = False
        self.payload = ""
        #: Bumped by each successful change, so a repeat can be told from a retry.
        self.world = 0
        self.history: dict[str, dict[str, Any]] = {}
        self.signatures: list[tuple[str, ...]] = []
        self.findings: list[str] = []
        self.last_output = ""
        #: Calls the user denied in this task (call key -> reason); the same call is not asked twice.
        self.denials: dict[str, str] = {}
        self.denied_rounds = 0
        self.denied_reason = ""

    def _present(self, text: str) -> None:
        visible = _present_text(text)
        if visible:
            self.show(visible)

    def run(self, first_payload: str) -> str:
        self.payload = first_payload
        self.chat.resume = self._resume_text
        rounds = 0
        while self.max_rounds is None or rounds < self.max_rounds:
            rounds += 1
            if self.chat.wants_restart():
                reason = self.chat.rotation_reason() or "the chat grew long"
                # A chat that is merely long still works: ask it for a handoff note first.
                note = self._handoff_note() if self.chat.too_long() and self.handoff else ""
                if self.chat.restart(reason):
                    self.payload = self._resume_text(note) + "\n\n" + self.payload
            self._note_shell_change()
            try:
                reply = self.chat.send(self.payload)
            except _ProviderFailed as exc:
                return self._end("FAILED", str(exc))
            self.turns.append({"role": "assistant", "content": reply})
            code = self._handle(reply)
            if code is not None:
                return code
        return self._end("STOPPED", f"stopped after {self.max_rounds} tool rounds")

    def _handle(self, raw_reply: str) -> str | None:
        detail = self.chat.last_detail
        reply, fabricated = _cut_fabricated(raw_reply)
        complete = isinstance(detail, dict) and detail.get("complete") is True
        answer_task = _answer_only(self.task)
        calls, unclosed = parse_tool_calls(reply, complete=complete)
        note = commentary(reply)
        if calls and not _OPEN_RE.search(reply) and answer_task and len(note) > 80:
            # JSON quoted inside an answer, not a call.
            calls = []
        status = _status_code(reply)
        plan = _plan_text(reply)
        shown = _answer_text(note)
        if note:
            self.findings = (self.findings + [_one_line(note, 300)])[-4:]
        if calls and shown:
            self._present(shown)
        elif shown and not status:
            self._present(plan if plan else shown)
        elif shown and status in _STATUS_OK:
            self._present(shown)
        if fabricated:
            self.chat.violations += 1
            self.hooks.ui("note", "The reply made up a tool result. Only real results are used.")
        if unclosed or _idle_tool_reply(detail, reply):
            self.truncations += 1
            if self.truncations > MAX_TRUNCATIONS:
                return self._end("FAILED", "the reply was cut off before it finished")
            self.hooks.ui("note", "The reply was cut off. Asking for the rest.")
            dangling = _OPEN_RE.search(_BLOCK_RE.sub("", reply))
            label = _cut_call_label(_BLOCK_RE.sub("", reply)[dangling.end() :]) if dangling else ""
            self._send_message("TRUNCATED", call=label)
            self.last_failed = True
            return None
        if calls:
            self.refusals = 0
            self.plan_notes = 0
            if all(call.error for call in calls):
                self.chat.violations += 1
            elif not fabricated:
                self.chat.violations = 0
            self._run_calls(calls, extra=self.sections["FABRICATED"] if fabricated else "")
            if self.state.failures_in_row >= MAX_FAILED_ROUNDS:
                return self._end("BLOCKED", f"{MAX_FAILED_ROUNDS} tool rounds in a row failed")
            if self.denied_rounds >= MAX_DENIED_ROUNDS:
                why = f": {self.denied_reason}" if self.denied_reason else ""
                return self._end("BLOCKED", f"the user denied the tool calls of {self.denied_rounds} replies in a row{why}")
            return None
        if fabricated and not status:
            self._send_message("FABRICATED")
            self.last_failed = True
            return None
        if self._planning() and not answer_task:
            return self._plan_reply(reply, status)
        if (
            not answer_task
            and self.hand_backs < MAX_HAND_BACKS
            and _hands_back(reply)
            and (status not in _STATUS_OK or self.last_failed or not self.tools_ran)
        ):
            self.hand_backs += 1
            self.chat.violations += 1
            self.hooks.ui("note", "The model asked you to do it yourself. Sending it back to run it.")
            last = ""
            if self.state.last_command and self.last_failed:
                last = f"The last command was {_one_line(self.state.last_command, 160)} ({self.state.last_exit}). "
            self._send_message("HAND_BACK", last=last)
            return None
        if status in {"FAILED", "BLOCKED"} and _refuses(reply):
            return self._refuse(reply)
        if status:
            self.chat.violations = 0
            return self._finish(status, reply=reply)
        if _no_edit_needed(reply):
            return self._finish("COMPLETED", unchanged_ok=True)
        if answer_task:
            if _refuses(reply) or _asks_user(reply) or _only_promises(reply):
                self.refusals += 1
                if self.refusals > MAX_REFUSALS:
                    return self._end(
                        "FAILED", f"stopped working on the task: {_one_line(_hide_tool_markup(reply), 160)}"
                    )
                self.hooks.ui("note", "Still working on it.")
                self._send_message("ANSWER")
                return None
            self.chat.violations = 0
            return self._finish("COMPLETED")
        if plan and self.plan_notes < 2:
            self.plan_notes += 1
            self.hooks.ui("note", "About to make that change.")
            self._send_message("PLAN_NOTED")
            return None
        question = _real_question(reply)
        if question and self.ask is not None and self.questions < MAX_USER_QUESTIONS:
            self.questions += 1
            answer = (self.ask(question) or "").strip()
            if answer:
                self.chat.violations = 0
                self._send_message("USER_ANSWER", answer=answer)
                return None
        if _stalls(reply):
            if self.state.mutated and not self.last_failed and not _refuses(reply):
                return self._finish("COMPLETED")
            return self._refuse(reply)
        if not self.tools_ran and self.nudges < 2:
            self.nudges += 1
            if reply.strip().upper() != "READY":
                self.chat.violations += 1
            self.hooks.ui("note", "Still working on it.")
            self._send_message("NUDGE")
            return None
        if self.last_failed and self.recoveries < 2:
            self.recoveries += 1
            self.chat.violations += 1
            self.hooks.ui("note", "Picking up where it left off.")
            self._send_message("RECOVER")
            return None
        return self._finish("COMPLETED")

    def _plan_reply(self, reply: str, status: str | None) -> str | None:
        """Plan mode: a reply with no calls is the plan. The user approves it, asks for changes, or stops."""
        plan = _hide_tool_markup(reply).strip()
        if status in {"FAILED", "BLOCKED"} and len(plan) > 40:
            return self._finish(status, reply=reply)
        words = re.sub(r"\b(?:COMPLETED|FINISHED|DONE)\b\.?", "", plan).strip()
        if len(words) < 40:
            self.plan_asks += 1
            if self.plan_asks > 2:
                return self._end("FAILED", "plan mode: the model did not send a plan")
            self.hooks.ui("note", "Asking for the plan.")
            self._send_message("PLAN_NEEDED")
            return None
        self.plans += 1
        choice, feedback = _review_plan()
        if choice in {"auto", "edits", "ask"}:
            self.plan_done = True
            self.approve_mode = choice
            self._send_message("PLAN_APPROVED")
            return None
        if choice == "no" and feedback and self.plans <= MAX_PLAN_REVISIONS:
            self._send_message("PLAN_REVISE", feedback=feedback)
            return None
        self.hooks.ui("note", "Plan only; no files were changed. Switch modes with Shift+Tab or /mode to carry it out.")
        self.hooks.status("COMPLETED")
        return "COMPLETED"

    def _refuse(self, reply: str) -> str | None:
        """Send a refusal back. FAILED/BLOCKED does not finish the task when the model only claims the tools are missing."""
        self.refusals += 1
        self.chat.violations += 1
        if self.refusals > MAX_REFUSALS:
            return self._end("FAILED", f"stopped working on the task: {_one_line(_hide_tool_markup(reply), 160)}")
        self.hooks.ui("note", "Still working on it.")
        self._send_message("RECOVER" if self.tools_ran else "NUDGE")
        return None

    # ----------------------------------------------------------------- running calls

    def _run_calls(self, calls: list[ToolCall], *, extra: str = "") -> None:
        """Run one reply's calls in order. Neighbouring read-only calls run side by side."""
        signature = tuple(_call_key(call) for call in calls if not call.error)
        extras = [extra] if extra else []
        if self._is_loop(signature) and all((self.history.get(key) or {}).get("ok") for key in signature):
            self.chat.violations += 1
            self.hooks.ui("note", "The same calls came back again. Not running them.")
            results = [
                {"tool": canonical_tool(call.tool) or call.tool, "ok": False, "error": "not run: the same calls as your previous replies"}
                for call in calls
            ]
            self.state.step += 1
            self._send_results(results, extra="\n\n".join(extras + [fill(self.sections["LOOP"], count=str(LOOP_REPLIES))]))
            return
        slots: list[dict[str, Any] | None] = [None] * len(calls)
        batch: list[tuple[int, ToolCall]] = []

        def flush() -> None:
            if len(batch) == 1:
                index, call = batch[0]
                slots[index] = self._execute_one(call)
            elif batch:
                for _, call in batch:
                    self.hooks.tool_start(call)
                with ThreadPoolExecutor(max_workers=min(PARALLEL_WORKERS, len(batch))) as pool:
                    futures = [
                        (index, call, pool.submit(_execute, call.tool, call.arguments, self.ctx))
                        for index, call in batch
                    ]
                    for index, call, future in futures:
                        result = future.result()
                        self.hooks.tool_done(call, result)
                        self._record(call, result)
                        slots[index] = result
            batch.clear()

        denied = False
        plan_blocked = False
        for index, call in enumerate(calls):
            if call.error:
                flush()
                slots[index] = {"tool": call.tool, "ok": False, "error": call.error}
                self.hooks.ui("bad", _friendly_error(call.error))
                continue
            repeat = self._repeat_result(call)
            if repeat is not None:
                flush()
                self.hooks.tool_start(call)
                self.hooks.tool_done(call, repeat)
                slots[index] = repeat
                continue
            perm = self._permission(call)
            kind = getattr(perm, "kind", "read") or "read"
            if kind == "read" and canonical_tool(call.tool) in _PARALLEL_SAFE:
                batch.append((index, call))
                continue
            flush()
            if self._planning() and not self._plan_allows(call, perm):
                result = {
                    "tool": canonical_tool(call.tool) or call.tool,
                    "ok": False,
                    "error": "not run: plan mode is on. Read only for now, then reply with the plan",
                    "planned": True,
                }
                self.hooks.tool_start(call)
                self.hooks.tool_done(call, result)
                slots[index] = result
                plan_blocked = True
                continue
            key = _call_key(call)
            if key in self.denials:
                # Asked and denied already in this task: do not ask the user again.
                reason = self.denials[key]
                denied = True
                error = (
                    "not run: you already sent this exact call and the user denied it"
                    + (f" ({reason})" if reason else "")
                    + ". Do not send it again; change the approach or use ask_user"
                )
                result = {"tool": canonical_tool(call.tool) or call.tool, "ok": False, "error": error, "denied": True}
                self.hooks.tool_start(call)
                self.hooks.tool_done(call, result)
                slots[index] = result
                continue
            allowed, reason = self._approved(perm)
            if not allowed:
                denied = True
                self.denials[key] = reason
                if reason:
                    self.denied_reason = reason
                error = "denied by the user" + (f": {reason}" if reason else "")
                result = {"tool": canonical_tool(call.tool) or call.tool, "ok": False, "error": error, "denied": True}
                self.hooks.tool_done(call, result)
                slots[index] = result
                continue
            slots[index] = self._execute_one(call)
        flush()
        results = [item for item in slots if item is not None]
        self.state.step += 1
        counted = [item for item in results if not item.get("denied") and not item.get("planned")]
        self.denied_rounds = self.denied_rounds + 1 if denied and not counted else 0
        self.last_failed = any(not item.get("ok") for item in counted)
        if not self.last_failed:
            self.recoveries = 0
        if counted and all(not item.get("ok") for item in counted):
            self.state.failures_in_row += 1
        elif counted:
            self.state.failures_in_row = 0
        if denied:
            extras.append(self.sections["DENIED"])
        if plan_blocked:
            extras.append(self.sections["PLAN_ONLY"])
        self._send_results(results, extra="\n\n".join(extras))

    def _execute_one(self, call: ToolCall) -> dict[str, Any]:
        self.hooks.tool_start(call)
        result = _execute(call.tool, call.arguments, self.ctx)
        self.hooks.tool_done(call, result)
        self._record(call, result)
        return result

    def _permission(self, call: ToolCall) -> Any:
        name = canonical_tool(call.tool)
        check = getattr(agent_tools, "permission_for", None)
        if check is None or not name:
            return _Perm("read", _activity(call), "")
        try:
            return check(name, call.arguments, self.ctx)
        except Exception as exc:  # a broken check must not let an edit through unasked
            log.debug(f"permission check failed for {name}: {exc}")
            if name in _read_only_tools():
                return _Perm("read", _activity(call), "")
            return _Perm("command" if name == "run_command" else "edit", _activity(call), "")

    def _approved(self, perm: Any) -> tuple[bool, str]:
        """Ask the user unless the call only reads, the mode allows it, or it was always-allowed.

        ask: every edit, command, fetch, and outside path. edits: file edits in
        the workspace run. auto: everything runs. A risky command (a broad
        delete, git reset --hard, a push, a flash) asks in every mode, unless
        ``"confirm_risky": false``.
        """
        kind = getattr(perm, "kind", "read") or "read"
        if kind == "read":
            return True, ""
        mode = self._approve_mode()
        risky = self._risk(perm)
        if risky and not self.confirm_risky and mode == "auto":
            risky = ""
        if not risky:
            if mode == "auto" or (mode == "edits" and kind == "edit"):
                return True, ""
        key = getattr(perm, "key", "") or ""
        rememberable = bool(key) and kind != "outside"
        if rememberable and key in self.always and not risky:
            return True, ""
        decision, reason = _approve_prompt(perm, risky=risky) if risky else _approve_prompt(perm)
        if decision == "always":
            if rememberable:
                self.always.add(key)
            return True, ""
        if decision == "yes":
            return True, ""
        return False, (reason or "").strip()

    def _risk(self, perm: Any) -> str:
        declared = str(getattr(perm, "risk", "") or "")
        if declared:
            return declared
        if getattr(perm, "kind", "") not in {"command", "outside"}:
            return ""
        check = getattr(agent_tools, "risky_command", None)
        command = str(getattr(perm, "detail", "") or "")
        if not callable(check) or not command:
            return ""
        try:
            return str(check(command) or "")
        except Exception as exc:
            log.debug(f"risk check failed: {exc}")
            return ""

    def _planning(self) -> bool:
        return not self.plan_done and self._approve_mode() == "plan"

    def _plan_allows(self, call: ToolCall, perm: Any) -> bool:
        """In plan mode: reads, read-only commands, and fetches run; edits and builds wait for the plan."""
        kind = getattr(perm, "kind", "read") or "read"
        name = canonical_tool(call.tool)
        if kind == "read" or kind == "network":
            return True
        if name in _read_only_tools():
            return True  # a read outside the workspace still asks
        if name == "run_command":
            check = getattr(agent_tools, "read_only_command", None)
            command = str(call.arguments.get("command") or "")
            return bool(callable(check) and check(command))
        return False

    def _approve_mode(self) -> str:
        """The mode now. /permissions in the UI can flip it mid-session; that change wins."""
        live = _ui_approve_mode()
        if live in _MODES and live != self.ui_mode_at_start:
            return live
        return self.approve_mode

    def _record(self, call: ToolCall, result: dict[str, Any]) -> None:
        """Remember what this call did, so an identical repeat can be answered without running it."""
        name = canonical_tool(call.tool) or call.tool
        ok = bool(result.get("ok"))
        if name == "run_command":
            self.verified = True
            self.last_output = str(result.get("output") or result.get("error") or "")[-1_500:]
        if ok:
            self.tools_ran = True
            if name in MUTATING or (name == "delegate" and result.get("changed")):
                self.check_passed = False
                self.verified = False
            if name not in _read_only_tools():
                self.world += 1
        else:
            self._note_failure(call, result)
        key = _call_key(call)
        previous = self.history.get(key)
        same = previous is not None and previous["world"] == self.world and previous["ok"] == ok
        self.history[key] = {
            "world": self.world,
            "ok": ok,
            "count": previous["count"] + 1 if same else 1,
            "fails": (previous["fails"] + 1 if same else 1) if not ok else 0,
            "step": self.state.step,
            "error": str(result.get("error") or ""),
            "output": str(result.get("output") or ""),
        }

    def _repeat_result(self, call: ToolCall) -> dict[str, Any] | None:
        """The answer to an identical call made when nothing has changed since, or None to run it.

        A failed call is not run again (a command gets one retry); a
        read-only call is not run a third time.
        """
        record = self.history.get(_call_key(call))
        if record is None or record["world"] != self.world:
            return None
        name = canonical_tool(call.tool) or call.tool
        if not record["ok"]:
            if record["fails"] < (2 if name == "run_command" else 1):
                return None
            record["fails"] += 1
            result: dict[str, Any] = {
                "tool": name,
                "ok": False,
                "error": (
                    f"{record['error'] or 'tool failed'}; not run again: this exact call already failed "
                    f"at step {record['step']} and nothing has changed since. Change the arguments or take another approach"
                ),
            }
            if record["output"]:
                result["output"] = record["output"]
            self._note_failure(call, result)
            return result
        if name in _read_only_tools() and name not in _POLLING and record["count"] >= MAX_SAME_CALL:
            record["count"] += 1
            return {
                "tool": name,
                "ok": True,
                "output": (
                    f"not run again: this exact call already ran {record['count'] - 1} times since the last change "
                    f"(last at step {record['step']}) and its result is above. Use it, or do something different."
                ),
            }
        return None

    def _is_loop(self, signature: tuple[str, ...]) -> bool:
        """True when this reply repeats the calls of the previous LOOP_REPLIES - 1 replies."""
        polling = all(key.split(":", 1)[0] in _POLLING for key in signature)
        self.signatures = (self.signatures + [signature])[-LOOP_REPLIES:]
        if not signature or polling or len(self.signatures) < LOOP_REPLIES:
            return False
        if len(set(self.signatures)) == 1:
            self.signatures = []
            return True
        return False

    def _note_failure(self, call: ToolCall, result: dict[str, Any]) -> None:
        key = call.tool + ":" + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
        count = self.state.failed_calls.get(key, 0) + 1
        self.state.failed_calls[key] = count
        if count < 2:
            return
        result["error"] = (
            str(result.get("error") or "tool failed")
            + f"; this exact call has now failed {count} times. Do not send it again unchanged"
        )
        if count >= 3 and canonical_tool(call.tool) == "edit_file":
            region = self._wide_region(normalize_args("edit_file", call.arguments))
            if region:
                result["output"] = (
                    "The program read the closest region of the file for you. Copy old_string "
                    "from these lines exactly, without the N| prefix, or use write_files:\n" + region
                )

    def _wide_region(self, arguments: dict[str, Any]) -> str:
        raw = arguments.get("path")
        old = arguments.get("old_string")
        if not isinstance(raw, str) or not isinstance(old, str):
            return ""
        path = self.ctx.workspace / raw
        if not path.is_file():
            return ""
        try:
            text = agent_edit.load_text(path).text
        except OSError:
            return ""
        return agent_edit.best_candidate(text, agent_edit.strip_read_prefix(old), context=15)

    # ----------------------------------------------------------------- finishing

    def _finish(self, status: str, *, reply: str = "", unchanged_ok: bool = False) -> str | None:
        if status in {"FAILED", "BLOCKED"}:
            reason = _failure_reason(reply)
            if not reason and not self.reason_asked:
                self.reason_asked = True
                self.hooks.ui("note", "Asking why that failed.")
                self._send_message("WHY_FAILED")
                return None
            if reason:
                self.hooks.ui("bad", reason)
        if status in _STATUS_OK:
            changed = self._files_changed()
            if changed and self.check_command and not self.check_passed:
                result = self._run_check()
                if result.get("ok"):
                    self.check_passed = True
                    self.hooks.ui("good", "The check passed.")
                else:
                    self.check_cycles += 1
                    if self.check_cycles > MAX_CHECK_CYCLES:
                        return self._end("FAILED", f"the check still fails: {self.check_command}")
                    self.hooks.ui("bad", "The check failed. Sending the failure back.")
                    self._send_results([result], extra=self.sections["CHECK_FAILED"])
                    return None
            elif (
                changed
                and not self.check_command
                and not self.verified
                and not self.verify_asked
                and _has_checks(self.ctx.workspace)
            ):
                self.verify_asked = True
                self.hooks.ui("note", "Nothing was run after the edits. Asking for a check.")
                self._send_message("VERIFY")
                return None
            if (
                not changed
                and not unchanged_ok
                and not self.nothing_nudged
                and _asks_for_change(self.task)
            ):
                self.nothing_nudged = True
                self.hooks.ui("note", "Nothing changed yet. Asking once more.")
                self._send_message("NOTHING_CHANGED")
                return None
        self._show_disk_once()
        self.hooks.status(status)
        return status

    def _note_shell_change(self) -> None:
        """After /shell, use the new shell and tell the model in the next message."""
        take = getattr(globals().get("agent_ui"), "take_shell_change", None)
        chosen = take() if callable(take) else None
        if chosen is None:
            return
        self.ctx.shell = chosen
        available = getattr(self.ctx.session, "available", None)
        try:
            preamble = agent_shell.shell_preamble(chosen, available)
        except Exception:  # a stand-in shell without the usual fields
            preamble = f"SHELL: {getattr(chosen, 'label', chosen)}."
        label = getattr(chosen, "label", "") or str(chosen)
        self.payload = f"Shell changed to {label}: {preamble}\n\n" + self.payload

    def _run_check(self) -> dict[str, Any]:
        """The project check. It always runs again after a change, with its own long timeout."""
        self.hooks.ui("work", f"Checking with {_one_line(self.check_command or '', 60)}")
        call = ToolCall(
            "run_command",
            {"command": self.check_command, "timeout": self.check_timeout, "cwd": str(self.ctx.workspace)},
        )
        session = self.ctx.session
        before = getattr(session, "cwd", None)
        self.hooks.tool_start(call)
        try:
            result = _execute(call.tool, call.arguments, self.ctx)
        finally:
            if before is not None:
                # The check runs in the workspace and leaves the session's cwd where the model put it.
                session.cwd = before
        self.hooks.tool_done(call, result)
        self.last_output = str(result.get("output") or result.get("error") or "")[-1_500:]
        return result

    def _files_changed(self) -> bool:
        """True when this task's bytes on disk differ from the pre-edit copies.

        With no checkpoint, the successful mutating tools are the record.
        """
        checkpoints = self.ctx.checkpoints
        if checkpoints is not None and checkpoints.task_dir is not None:
            diff = checkpoints.disk_diff()
            if diff is not None:
                return bool(diff.strip())
        return self.state.mutated

    def _show_disk_once(self) -> None:
        """Print this task's on-disk diff a single time, for the person watching."""
        if self.reviewed:
            return
        self.reviewed = True
        checkpoints = self.ctx.checkpoints
        diff = checkpoints.disk_diff() if checkpoints is not None else None
        if diff is None:
            if self.state.mutated:
                self._show_diffstat()
            return
        if not diff.strip():
            return
        self.hooks.ui("good", "On disk, this task changed:")
        lines = diff.splitlines()
        if not _show_diff_in_ui(diff):
            for line in lines[:_REVIEW_LINES]:
                log.print_safe(f"  {line}", file=sys.stderr, flush=True)
        if len(lines) > _REVIEW_LINES:
            self.hooks.ui("note", f"{len(lines) - _REVIEW_LINES} more lines in the diff")

    def _show_diffstat(self) -> None:
        changed = ", ".join(self.state.edits) or "none"
        self.hooks.ui("good", f"Changed: {_one_line(changed, 160)}")
        if not is_git_repo(self.ctx.workspace):
            return
        result = _execute("git_diff", {"stat": True}, self.ctx)
        if result.get("ok"):
            for line in str(result.get("output", "")).splitlines()[1:21]:
                self.hooks.ui("good", line)

    def _end(self, code: str, message: str) -> str:
        log.warn(message)
        self.hooks.ui("bad", message)
        self.turns.append({"role": "assistant", "content": message})
        self._show_disk_once()
        self.hooks.status(code)
        return code

    # ----------------------------------------------------------------- messages

    def _state_text(self) -> str:
        return self.state.render(self.sections["STATE"])

    def _handoff_note(self) -> str:
        """The old chat's own summary for its successor (one extra round trip), or "" if it fails."""
        self.hooks.ui("note", "Asking this chat for a handoff note before moving to a new one.")
        try:
            reply = self.chat.send(self.sections["HANDOFF"] + "\n\n" + self._state_text())
        except _ProviderFailed as exc:
            log.debug(f"no handoff note: {exc}")
            return ""
        self.turns.append({"role": "assistant", "content": reply})
        note = _hide_tool_markup(reply).strip()
        if _status_code(note) and len(note) < 40:
            return ""
        return note[:5_000]

    def _resume_text(self, note: str = "") -> str:
        """What a fresh chat needs to carry on: the task, progress, the last result, and the old chat's note."""
        lines = [self.sections["RESUME"], f"Task: {self.task.strip()}"]
        if self.state.todos:
            lines.append(
                "Todos: " + "; ".join(f"[{item.get('status')}] {item.get('content')}" for item in self.state.todos[:12])
            )
        changed = ""
        checkpoints = self.ctx.checkpoints
        if checkpoints is not None and checkpoints.task_dir is not None:
            changed = _diffstat(checkpoints.disk_diff() or "")
        if not changed and self.state.edits:
            changed = ", ".join(self.state.edits)
        lines.append(f"Files changed in this task: {changed or 'none yet'}")
        if self.state.reads:
            lines.append("Files read: " + ", ".join(list(self.state.reads)[-12:]) + " (read again before editing)")
        if self.findings:
            lines.append("Your notes so far:\n- " + "\n- ".join(self.findings))
        if self.state.last_command:
            lines.append(f"Last command: {self.state.last_command} -> {self.state.last_exit}")
            if self.last_output.strip():
                lines.append("Its output ended with:\n" + self.last_output.strip())
        if note:
            lines.append("Handoff note from the previous chat (its own words):\n" + note)
        lines.append("The latest message from the program follows.")
        return "\n".join(lines)

    def _send_results(self, results: list[dict[str, Any]], *, extra: str = "") -> None:
        state_text = self._state_text()
        room = max(2_000, self.max_result_chars - len(state_text) - len(extra) - 4)
        body = _cap("\n".join(format_tool_result(item) for item in results), room)
        if extra:
            body += "\n\n" + extra
        self.payload = body + "\n\n" + state_text
        self.turns.append({"role": "tool", "content": self.payload})

    def _send_message(self, section: str, **values: str) -> None:
        text = fill(self.sections[section], path=self.state.last_path or "path/to/file", **values)
        self.payload = text + "\n\n" + self._state_text()
        self.turns.append({"role": "user", "content": self.payload})


def run_agent_loop(
    session: Any,
    *,
    workspace: Path,
    index_path: Path | None,
    cache_dir: Path | None,
    first_task: str,
    max_rounds: int | None,
    max_result_chars: int,
    seed: str | None = None,
    read_message: Callable[[], str | None] | None = None,
    emit: Callable[[str], None] | None = None,
    outcome: list[str] | None = None,
    check_command: str | None = None,
    shell: agent_shell.Shell | None = None,
    runner: Callable[..., Any] | None = None,
    approve_mode: str = "ask",
    session_shell: Any = None,
    turns: list[dict[str, str]] | None = None,
    ask_user: Callable[[str], str | None] | None = None,
    settings: dict[str, Any] | None = None,
    seed_first: bool = False,
    helper_factory: Callable[[], Any] | None = None,
    helper_count: int = 0,
) -> list[dict[str, str]]:
    """Talk to ``session.send`` until each task reaches a finish code.

    ``seed`` (the instructions) goes in front of the first task, so the first
    reply already works on it; ``seed_first`` sends it alone first instead.
    Each task gets fresh working memory and an undo checkpoint. COMPLETED,
    FINISHED, DONE, FAILED, BLOCKED, a direct answer to a question, or a plain
    answer after a tool has run ends that task; the same chat waits for the
    next one. Ctrl+C ends the running task as INTERRUPTED; ``/new`` starts a
    new chat. ``turns`` (when given) is filled as the session goes.

    ``settings`` keys: ``check_timeout`` (seconds, default 600),
    ``compact_after_chars`` (default 300000), ``reply_retries`` (default 3).
    """
    turns = turns if turns is not None else []
    reader = read_message or _read_message
    show = emit or _emit
    ask_person = ask_user or (lambda question: _ask_user(question, None))
    sections = load_prompt_sections()

    def ask(question: str) -> str | None:
        # Auto mode does not stop for questions: the model decides and says what it chose.
        if decide_in_auto and _current_mode(approve_mode, ui_mode_at_start) == "auto":
            _ui("note", f"Auto mode: the model decides ({_one_line(question, 100)})")
            return sections["AUTO_DECIDE"]
        return ask_person(question)

    workspace = Path(workspace).resolve()
    checkpoints = agent_edit.Checkpoints(cache_dir, workspace)
    chosen = shell or getattr(session_shell, "shell", None) or agent_shell.detect_shell()
    options = settings if isinstance(settings, dict) else {}
    chat = _Chat(
        session,
        seed=seed or "",
        retries=_int_setting(options, "reply_retries", DEFAULT_REPLY_RETRIES, 0),
        compact_after=_int_setting(options, "compact_after_chars", DEFAULT_COMPACT_AFTER_CHARS, 10_000),
        compact_turns=_int_setting(options, "compact_after_turns", DEFAULT_COMPACT_AFTER_TURNS, 5),
        compact_minutes=_int_setting(options, "compact_after_minutes", DEFAULT_COMPACT_AFTER_MINUTES, 5),
    )
    handoff_notes = options.get("handoff_notes") is not False
    check_timeout = _int_setting(options, "check_timeout", DEFAULT_CHECK_TIMEOUT, 1)
    build_timeout = _int_setting(options, "build_timeout", int(agent_build.DEFAULT_BUILD_TIMEOUT), 60)
    command_retries = _int_setting(options, "command_retries", 1, 0)
    if session_shell is not None and options.get("background_on_timeout") is False:
        try:
            session_shell.detach_on_timeout = False
        except Exception:
            pass
    confirm_risky = options.get("confirm_risky") is not False
    decide_in_auto = str(options.get("auto_questions") or "decide").lower() != "ask"
    auto_skills = options.get("auto_skills") is not False
    max_skills = _int_setting(options, "max_skills", 2, 0)
    always: set[str] = set()
    ui_mode_at_start = _ui_approve_mode()
    base = dict(
        workspace=workspace,
        index_path=index_path,
        cache_dir=cache_dir,
        max_chars=min(DEFAULT_TOOL_CHARS, max_result_chars),
        runner=runner,
        shell=chosen,
        session=session_shell,
        on_output=_tool_output,
        ask_user=lambda question: ask(question) or "",
        build_timeout=float(build_timeout),
        command_retries=command_retries,
        network_commands="ask" if str(options.get("network_commands") or "").lower() == "ask" else "block",
        web_hosts=_web_hosts(options),
    )
    outcome_code = "COMPLETED"
    pool_box: list[Any] = []

    def make_delegate(
        cancel: threading.Event, checkpoints_now: Any, state: TaskState
    ) -> Callable[[list[dict[str, Any]]], dict[str, Any]] | None:
        """The delegate tool's runner for one task: briefs go to helper tabs, all at once."""
        if helper_factory is None or helper_count < 1:
            return None
        from critique_bot import agent_helpers

        def delegate(briefs: list[dict[str, Any]]) -> dict[str, Any]:
            if not pool_box:
                pool_box.append(
                    agent_helpers.HelperPool(
                        helper_factory,
                        helper_count,
                        seed=seed or "",
                        sections=sections,
                        base=base,
                        max_result_chars=max_result_chars,
                        retries=_int_setting(options, "reply_retries", DEFAULT_REPLY_RETRIES, 0),
                        compact_after=_int_setting(options, "compact_after_chars", DEFAULT_COMPACT_AFTER_CHARS, 10_000),
                    )
                )
            started = time.monotonic()
            _set_active("delegate", len(briefs))
            try:
                reports = pool_box[0].run(
                    briefs,
                    cancel=cancel,
                    progress=lambda line: _tool_output(line + "\n"),
                    context={"checkpoints": checkpoints_now},
                )
            finally:
                _set_active("delegate", 1)
            result = agent_helpers.format_reports(reports, time.monotonic() - started)
            for name in result.get("changed") or []:
                # The coordinator's task owns these changes: the diff, the check, and undo cover them.
                state.edits[name] = state.edits.get(name, 0) + 1
            return result

        return delegate

    if seed and seed.strip():
        if seed_first:
            _seed_session(
                chat, seed.strip(), turns, ctx=_tool_context(**base), max_result_chars=max_result_chars, show=show
            )
        else:
            chat.prefix = _without_ready(seed)
    pending = first_task.strip()
    announced = False
    carry = ""
    while True:
        if not pending:
            if not announced:
                _ui("note", "Ready. Type a task, or exit.")
                announced = True
            try:
                pending = reader() or ""
            except (KeyboardInterrupt, EOFError):
                break
            if not pending.strip():
                break
        task = pending.strip()
        pending = ""
        if task.lower() == "/theme":
            from critique_bot.welcome import reopen_theme

            reopen_theme(workspace)
            continue
        if task.lower() == "/new":
            if not chat.restart("you asked for one"):
                _ui("note", "This chat page cannot open a new conversation; staying in this one.")
            continue
        _ui("task", f"Working on: {_one_line(task, 100)}")
        turns.append({"role": "user", "content": task})
        if chat.too_long():
            chat.restart(chat.rotation_reason() or "the chat grew long")
        repo_map = _prepare_index(workspace, index_path, task)
        if chat.map_sent:
            repo_map = ""
        elif repo_map.strip():
            chat.map_sent = True
        skills = _task_skills(task, workspace, chat, options, auto=auto_skills, limit=max_skills)
        mode_now = _current_mode(approve_mode, ui_mode_at_start)
        mode_note = sections["PLAN_MODE"] if mode_now == "plan" and not _answer_only(task) else ""
        checkpoints.start_task()
        cancel = threading.Event()
        task_state = TaskState(task=task)
        ctx = _tool_context(
            **base,
            state=task_state,
            checkpoints=checkpoints,
            cancel=cancel,
            delegate=make_delegate(cancel, checkpoints, task_state),
        )
        run = _TaskRun(
            chat,
            task,
            ctx=ctx,
            sections=sections,
            turns=turns,
            show=show,
            max_rounds=max_rounds,
            max_result_chars=max_result_chars,
            check_command=check_command,
            check_timeout=check_timeout,
            approve_mode=approve_mode,
            always=always,
            ask=ask,
            ui_mode_at_start=ui_mode_at_start,
            confirm_risky=confirm_risky,
        )
        if mode_now != "plan" or _answer_only(task):
            run.plan_done = True
        run.handoff = handoff_notes
        message = task_message(task, repo_map=repo_map, skills=skills, mode_note=mode_note)
        if carry:
            message = carry + "\n\n" + message
            carry = ""
        try:
            outcome_code = run.run(message)
        except KeyboardInterrupt:
            cancel.set()
            _stop_background(session_shell)
            try:
                _stop_reply(chat.session)
            except KeyboardInterrupt:  # a second Ctrl+C: skip it; the next send stops it
                pass
            outcome_code = "INTERRUPTED"
            turns.append({"role": "assistant", "content": "INTERRUPTED by the user"})
            _ui("bad", "Interrupted. The task stopped.")
            try:
                run._show_disk_once()
            except Exception as exc:  # the diff is a courtesy; the prompt must come back
                log.debug(f"diff after interrupt failed: {exc}")
            _print_status(outcome_code)
            carry = (
                "The user interrupted the previous task. Stop working on it; "
                "its last reply was not acted on. The new task follows."
            )
        changed = list(task_state.edits)
        chat.history.append(
            f"{_one_line(task, 100)} -> {outcome_code}"
            + (f" (changed {', '.join(changed[:6])}{' ...' if len(changed) > 6 else ''})" if changed else "")
        )
    for pool in pool_box:
        try:
            pool.close()
        except Exception as exc:  # the session is over; a stuck tab must not hang the exit
            log.debug(f"closing helper tabs failed: {exc}")
    if outcome is not None:
        outcome[:] = [outcome_code]
    return turns


def _current_mode(default: str, at_start: str | None = None) -> str:
    """``default`` (the session's mode), unless the UI switched to another one since ``at_start``."""
    live = _ui_approve_mode()
    if live in _MODES and live != at_start:
        return live
    return default if default in _MODES else "ask"


def _task_skills(
    task: str, workspace: Path, chat: Any, settings: dict[str, Any], *, auto: bool, limit: int
) -> str:
    """The SKILLS block for this task: pinned skills and the ones that match, each sent once per chat."""
    select = getattr(agent_tools, "select_skills", None)
    if not callable(select):
        return ""
    pinned = _setting_list(settings, "skills")
    getter = getattr(globals().get("agent_ui"), "pinned_skills", None)
    if callable(getter):
        try:
            pinned += [name for name in getter() if name not in pinned]
        except Exception:
            pass
    try:
        chosen = select(task, workspace, pinned=pinned, limit=limit if auto else 0)
    except Exception as exc:  # skills help; they must never stop a task
        log.debug(f"choosing skills failed: {exc}")
        return ""
    sent = getattr(chat, "skills_sent", None)
    if not isinstance(sent, set):
        sent = set()
        try:
            chat.skills_sent = sent
        except Exception:
            pass
    fresh = [item for item in chosen if item["name"] not in sent]
    if chosen:
        _ui("note", "Skills: " + ", ".join(item["name"] for item in chosen))
    block = skills_block(fresh)
    if block:
        sent.update(item["name"] for item in fresh)
    elif chosen:
        block = "SKILLS: " + ", ".join(item["name"] for item in chosen) + " (sent earlier in this chat; still apply)."
    return block


def _show_diff_in_ui(diff: str) -> bool:
    """Print the diff through the terminal UI's colored renderer. False when that is not available."""
    ui = globals().get("agent_ui")
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


def _stop_background(session_shell: Any) -> None:
    """Kill the commands still running in the shell session (Ctrl+C)."""
    if session_shell is None:
        return
    try:
        jobs = session_shell.jobs()
    except Exception as exc:
        log.debug(f"listing background jobs failed: {exc}")
        return
    for job in jobs or []:
        if isinstance(job, dict) and job.get("running"):
            try:
                session_shell.kill_background(job.get("id"))
            except Exception as exc:
                log.debug(f"killing background job failed: {exc}")


def _ensure_code_graph(workspace: Path) -> None:
    """Download CodeGraph and build the project graph before the chat opens."""
    _ui("note", "Setting up the code graph.")
    try:
        note = code_graph.prepare(workspace, timeout=600)
    except Exception as exc:
        _ui("bad", f"Code graph setup failed: {exc}")
        return
    if note:
        _ui("note", note)


def _prepare_index(workspace: Path, index_path: Path | None, task: str) -> str:
    """Bring the index up to date with the disk and build the repo map."""
    if index_path is None or not Path(index_path).is_file():
        return ""
    graph = ""
    try:
        graph = code_graph.prepare(workspace)
    except Exception as exc:  # the graph is a hint; a missing CLI must not stop the task
        log.debug(f"code graph refresh failed: {exc}")
    try:
        code_index.refresh_index(workspace, index_path)
        mapped = code_index.repo_map(index_path, keywords=_keywords(task), budget_chars=REPO_MAP_CHARS)
    except Exception as exc:  # the map is a hint; a broken index must not stop the task
        log.debug(f"index refresh failed: {exc}")
        return graph
    if graph and mapped:
        return graph + "\n\n" + mapped
    return graph or mapped


#: Tools the reply to the instructions may run before there is a task.
_SEED_TOOLS = _PARALLEL_SAFE | {"skill", "code_graph"}


def _seed_needs_approval(name: str, call: ToolCall, ctx: ToolContext) -> bool:
    """True when a read-only call would still need the user (a path outside the workspace)."""
    check = getattr(agent_tools, "permission_for", None)
    if check is None:
        return False
    try:
        return (getattr(check(name, call.arguments, ctx), "kind", "read") or "read") != "read"
    except Exception:
        return True


def _seed_session(
    chat: Any,
    seed: str,
    turns: list[dict[str, str]],
    *,
    ctx: ToolContext,
    max_result_chars: int,
    show: Callable[[str], None],
) -> None:
    """Send the tool instructions alone. Read-only tool calls in the reply still run.

    Nothing that changes files or runs a command runs before there is a
    task, whatever name the model uses for it (write_file, patch, bash).
    """
    if not isinstance(chat, _Chat):
        chat = _Chat(chat)
    _ui("note", "Getting ready.")
    payload = seed
    turns.append({"role": "user", "content": seed})
    for _ in range(3):
        try:
            reply = chat.send(payload)
        except _ProviderFailed as exc:
            _ui("bad", str(exc))
            return
        reply, _fabricated = _cut_fabricated(reply)
        calls, unclosed = parse_tool_calls(reply)
        note = commentary(reply)
        if note and _status_code(note) is None and note.strip().upper() != "READY":
            visible = _hide_tool_markup(note)
            if visible:
                show(visible)
        turns.append({"role": "assistant", "content": reply})
        if unclosed or _idle_tool_reply(chat.last_detail, reply):
            payload = format_tool_result(
                {"tool": "", "ok": False, "error": "reply was truncated before the tool call closed; resend complete tool_call blocks"}
            )
            turns.append({"role": "tool", "content": payload})
            continue
        if not calls:
            return
        results = []
        for call in calls:
            name = canonical_tool(call.tool)
            if call.error:
                results.append({"tool": call.tool, "ok": False, "error": call.error})
            elif name and (name not in _SEED_TOOLS or _seed_needs_approval(name, call, ctx)):
                results.append(
                    {"tool": name, "ok": False, "error": "no task yet; wait for the task before changing files or running commands"}
                )
            else:
                _tool_start(call)
                result = _execute(call.tool, call.arguments, ctx)
                _tool_done(call, result)
                results.append(result)
        payload = _cap("\n".join(format_tool_result(item) for item in results), max_result_chars)
        turns.append({"role": "tool", "content": payload})


def run_agent(
    config: BotConfig,
    home: BotHome,
    task: str,
    *,
    max_rounds: int | None,
    output_dir: Path | None,
    headed: bool,
    approve_mode: str | None = None,
) -> int:
    """Open the Edge session, run the tool loop, and write the transcript.

    ``approve_mode`` is ``"ask"``, ``"edits"``, ``"auto"`` (``--yes``), or ``"plan"``
    (``--plan``); ``None`` reads ``"permissions"`` from ``.bot/settings.json``. The transcript is saved even
    when Ctrl+C or an error ends the session.
    """
    started = datetime.now(timezone.utc)
    turns: list[dict[str, str]] = []
    outcome = ["COMPLETED"]
    settings = home.settings if isinstance(home.settings, dict) else {}
    try:
        instructions = load_agent_instructions()
    except OSError as exc:
        log.error(str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if settings.get("seed_instructions") is False:
        instructions = ""
    if approve_mode not in _MODES:
        approve_mode = agent_ui.normalize_mode(settings.get("permissions") or settings.get("mode"))
    notes = home.project_notes()
    check_command = resolve_check_command(settings, notes)
    helpers = _int_setting(settings, "helper_sessions", DEFAULT_HELPER_SESSIONS, 0)
    helpers = min(helpers, MAX_HELPER_SESSIONS)
    if helpers and config.max_parallel_tabs < helpers + 1:
        # Extra tabs are opened over the browser's remote debugging port, which this turns on.
        config = dataclasses.replace(config, max_parallel_tabs=helpers + 1)
    _prewarm_shell_environment()
    shell = agent_shell.detect_shell(preference=_shell_preference(settings))
    available = _available_shells()
    session_shell = _open_shell_session(home.root, shell, available)
    agent_ui.configure(
        workspace=home.root,
        model=config.model or "",
        shell=shell,
        approve_mode=approve_mode,
        theme=settings.get("theme") or "dark",
        history_path=home.bot_dir / "history",
        cache_dir=home.cache_dir,
        session_shell=session_shell,
        tools=ALLOWED_TOOLS,
    )
    agent_ui.welcome_header()
    agent_ui.install_log_bridge()
    try:
        skill_names = [item["name"] for item in agent_tools.discover_skills(home.root)]
    except Exception as exc:
        log.debug(f"listing skills failed: {exc}")
        skill_names = []
    seed = seed_message(
        home.root, instructions, shell=shell, notes=notes, available=available, skills=skill_names, helpers=helpers,
        web_hosts=_web_hosts(settings),
    )
    code = 1
    try:
        _ensure_code_graph(home.root)
        code = _run_session(
            config,
            home,
            task,
            headed=headed,
            loop_kwargs={
                "workspace": home.root,
                "index_path": home.index_path,
                "cache_dir": home.cache_dir,
                "max_rounds": max_rounds,
                "max_result_chars": _result_budget(config, settings),
                "seed": seed,
                "outcome": outcome,
                "check_command": check_command,
                "shell": shell,
                "approve_mode": approve_mode,
                "session_shell": session_shell,
                "turns": turns,
                "settings": settings,
                "ask_user": _ask_user,
                "helper_count": helpers,
            },
            turns=turns,
        )
    except KeyboardInterrupt:
        outcome[:] = ["INTERRUPTED"]
        agent_ui.shutdown()
        _print_status("INTERRUPTED")
        code = 130
    finally:
        agent_ui.shutdown()
        if session_shell is not None:
            try:
                session_shell.close()
            except Exception as exc:
                log.warn(f"closing the shell session failed: {exc}")
        _save_transcript(
            config,
            home,
            turns,
            started=started,
            output_dir=output_dir,
            commands=list(getattr(session_shell, "history", []) or []),
        )
    if code != 0:
        return code
    return 0 if outcome[-1] in _STATUS_OK else 1


_SHELL_SETTINGS = ("auto", "pwsh", "powershell", "cmd", "bash", "sh", "zsh", "gitbash", "git-bash")


def _shell_preference(settings: dict[str, Any]) -> str:
    """The ``"shell"`` setting (auto, pwsh, powershell, cmd, bash, sh, zsh); auto when unset or unknown."""
    value = str(settings.get("shell") or "auto").strip().lower()
    if value not in _SHELL_SETTINGS:
        log.warn(f'settings "shell": {value!r} is not one of {", ".join(_SHELL_SETTINGS[:7])}; using auto')
        return "auto"
    return value


def _prewarm_shell_environment() -> None:
    """Start reading the login-shell environment now, so the first command does not wait."""
    try:
        agent_shell.prewarm_login_environment()
    except Exception as exc:  # noqa: BLE001
        log.warn(f"login environment: {exc}")


def _available_shells() -> dict[str, agent_shell.Shell]:
    try:
        return agent_shell.available_shells()
    except Exception as exc:  # noqa: BLE001
        log.warn(f"listing shells failed: {exc}")
        return {}


def _open_shell_session(
    workspace: Path, shell: agent_shell.Shell, available: dict[str, agent_shell.Shell] | None = None
) -> Any:
    """A persistent ShellSession when agent_shell provides one, else ``None``."""
    factory = getattr(agent_shell, "ShellSession", None)
    if factory is None:
        return None
    try:
        return factory(workspace, shell, available=available)
    except Exception as exc:
        log.warn(f"shell session unavailable: {exc}")
        return None


def _loop_kwargs(candidates: dict[str, Any]) -> dict[str, Any]:
    """Only the keyword arguments ``run_agent_loop`` accepts (it gains new ones over time)."""
    import inspect

    try:
        accepted = set(inspect.signature(run_agent_loop).parameters)
    except (TypeError, ValueError):
        return candidates
    return {key: value for key, value in candidates.items() if key in accepted}


def _run_session(
    config: BotConfig,
    home: BotHome,
    task: str,
    *,
    headed: bool,
    loop_kwargs: dict[str, Any],
    turns: list[dict[str, str]],
) -> int:
    """One browser session. A Cloudflare block in headless mode retries once, headed."""
    from critique_bot.browser import BrowserError
    from critique_bot.chat_client import ChatError
    from critique_bot.provider import open_provider

    kwargs = _loop_kwargs(loop_kwargs)
    try:
        with open_provider(config, headed=headed) as provider:
            with provider.session() as session:
                if kwargs.get("helper_count") and getattr(provider, "can_parallelize", False):
                    kwargs["helper_factory"] = lambda: provider.session(isolated=True)
                elif kwargs.get("helper_count"):
                    log.info("helper tabs need the browser's remote debugging; the task runs in one tab")
                    kwargs["helper_count"] = 0
                try:
                    result = run_agent_loop(session, first_task=task, **kwargs)
                    if "turns" not in kwargs and result is not turns:
                        turns[:] = list(result or [])
                except Exception:
                    page = getattr(session, "page", None)
                    if page is not None:
                        from critique_bot.output import save_failure

                        save_failure(page, home.sessions_dir)
                    raise
    except ChatError as exc:
        if not headed and "Cloudflare" in str(exc):
            _ui("note", "The headless window was blocked. Opening a visible browser.")
            return _run_session(config, home, task, headed=True, loop_kwargs=loop_kwargs, turns=turns)
        log.error(str(exc))
        agent_ui.shutdown()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrowserError as exc:
        log.error(str(exc))
        agent_ui.shutdown()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        log.exception(f"unexpected failure: {exc}")
        agent_ui.shutdown()
        print(f"error: unexpected failure: {exc}", file=sys.stderr)
        return 1
    return 0


def _save_transcript(
    config: BotConfig,
    home: BotHome,
    turns: list[dict[str, str]],
    *,
    started: datetime,
    output_dir: Path | None,
    commands: list[dict[str, Any]] | None = None,
) -> None:
    if not turns:
        return
    finished = datetime.now(timezone.utc)
    body = format_transcript(turns)
    payload = {
        "mode": "agent",
        "model": config.model,
        "backend": config.backend,
        "url": config.url,
        "workspace": str(home.root),
        "response": body,
        "turns": turns,
        "started_at": isoformat(started),
        "finished_at": isoformat(finished),
    }
    if commands:
        payload["commands"] = commands
    stamp = started.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        write_output(home.sessions_dir / stamp, body, payload, stem="agent", print_body=False)
        if output_dir is not None:
            write_output(output_dir, body, payload, stem="agent", print_body=False)
    except OSError as exc:
        log.error(f"could not save the transcript: {exc}")
        print(f"error: could not save the transcript: {exc}", file=sys.stderr)


def _result_budget(config: BotConfig, settings: dict[str, Any]) -> int:
    """Characters per message sent back to the chat. Smaller keeps the task in view."""
    raw = settings.get("max_result_chars")
    if isinstance(raw, int) and raw >= 4_000:
        return raw
    return min(40_000, config.max_prompt_chars)


def format_transcript(turns: list[dict[str, str]]) -> str:
    headings = {"user": "You", "assistant": "Assistant", "tool": "Tool"}
    parts = ["# Agent", ""]
    for turn in turns:
        parts.append(f"## {headings.get(turn.get('role', ''), 'Note')}")
        parts.append("")
        parts.append(turn.get("content", "").rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# --------------------------------------------------------------------------- terminal output

from critique_bot import agent_ui  # noqa: E402  (UI layer; kept with the terminal hooks)


def _one_line(text: str, limit: int = 120) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _cap(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    note = "\n...[truncated; narrow with path, offset, or a tighter pattern]"
    return text[: max(0, limit - len(note))] + note


def _paint(text: str, color: str) -> str:
    return log.paint(text, color, file=sys.stderr)


def _present_text(text: str) -> str:
    return _hide_tool_markup(text)


def _ui(kind: str, message: str) -> None:
    message = _present_text(message)
    if not message:
        return
    agent_ui.note(kind, _one_line(message, 160))


def _print_status(code: str) -> None:
    agent_ui.final_status(code, ok=code in _STATUS_OK)


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
        "code_graph": f"Tracing {_one_line(str(args.get('query') or args.get('symbol') or 'the code'), 50)}",
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
    agent_ui.assistant(visible)


def _read_message() -> str | None:
    """The next task. ``None`` exits; ``"/new"`` (agent_ui.NEW_CHAT) asks for a fresh chat."""
    return agent_ui.read_message()


def _tool_start(call: ToolCall) -> None:
    name = canonical_tool(call.tool) or call.tool
    agent_ui.tool_start(name, call.arguments)


def _tool_done(call: ToolCall, result: dict[str, Any]) -> None:
    name = canonical_tool(call.tool) or call.tool
    friendly = "" if result.get("ok") else _friendly_error(str(result.get("error") or "failed"))
    agent_ui.tool_done(name, call.arguments, result, friendly=friendly)


def _tool_output(text: str) -> None:
    agent_ui.tool_output(text)


def _set_active(name: str, count: int) -> None:
    setter = getattr(agent_ui, "set_active", None)
    if callable(setter):
        setter(name, count)


def _waiting(active: bool, label: str = "Thinking") -> None:
    agent_ui.waiting(active, label)


def _approve_prompt(permission: Any, *, risky: str = "") -> tuple[str, str]:
    return agent_ui.approve(permission, risky=risky)


def _review_plan() -> tuple[str, str]:
    return agent_ui.review_plan()


def _ask_user(question: str, options: list[str] | None = None) -> str | None:
    """Ask the person a question from the model. None when there is no terminal."""
    return agent_ui.ask_question(_present_text(question), options)
