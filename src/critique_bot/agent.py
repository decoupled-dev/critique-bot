"""Local coding agent driven through a web chat page.

The chat model prints ``<tool_call>`` blocks. This module parses them, runs
the tool from :mod:`critique_bot.agent_tools`, and sends ``<tool_result>``
blocks back on the same chat, each followed by a short STATE block so the
task stays in view however long the conversation gets. Model-facing text
lives in ``prompts/agent.txt``.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from critique_bot import agent_edit, agent_shell, code_graph, code_index, log
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
from critique_bot.bot_home import BotHome
from critique_bot.chat_client import COMPLETION_IDLE
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

_OPEN_RE = re.compile(r"<tool_call>", re.IGNORECASE)
_BLOCK_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.IGNORECASE | re.DOTALL)
_CLOSED_TOOL_MARKUP_RE = re.compile(
    r"<\s*tool_call\b[^>]*>.*?<\s*/\s*tool_call\s*>"
    r"|<\s*tool_result\b[^>]*>.*?<\s*/\s*tool_result\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_TAG_RE = re.compile(r"<\s*/?\s*tool_(?:call|result)\b[^>]*>", re.IGNORECASE)
_UNCLOSED_TOOL_RE = re.compile(r"<\s*tool_(?:call|result)\b[^>]*>[\s\S]*\Z", re.IGNORECASE)
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*)\n```$", re.DOTALL)
_FENCED_JSON_RE = re.compile(
    r"```(?:json|tool|tool_call)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE
)
_CHROME_LINES = frozenset({"json", "copy", "copy code", "code", "tool", "tool_call", "javascript"})
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
_FAIL_HEAD = re.compile(r"^(FAILED|BLOCKED)\b\s*[:\-—]?\s*(.*)$")
MAX_REFUSALS = 3
MAX_FAILED_ROUNDS = 8
MAX_TRUNCATIONS = 4
MAX_CHECK_CYCLES = 2
REPO_MAP_CHARS = 2_500

_FALLBACKS = {
    "TASK_PREFIX": "Print tool_call blocks to act. Do not refuse.",
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


def parse_tool_calls(text: str) -> tuple[list[ToolCall], bool]:
    """Parse tool calls. The bool is True only when the reply has an open tag and no call.

    Complete blocks always run. Text left after them is checked for a
    dangling ``<tool_call>``: one followed by JSON means a block was cut off;
    a bare tag (page rendering often leaves one) is ignored. A reply with no
    tags at all is searched for fenced or bare JSON objects naming a tool.
    """
    calls: list[ToolCall] = []
    for match in _BLOCK_RE.finditer(text):
        calls.extend(_parse_block(match.group(1)))
    rest = _BLOCK_RE.sub("", text)
    dangling = _OPEN_RE.search(rest)
    if dangling is not None:
        tail = rest[dangling.end() :]
        if not calls:
            return [], True
        if "{" in tail:
            calls.append(
                ToolCall(
                    tool="",
                    arguments={},
                    error=(
                        "a later tool_call was cut off; complete calls in this reply "
                        "were run; resend only the unfinished block"
                    ),
                )
            )
        return calls, False
    if calls:
        return calls, False
    for match in _FENCED_JSON_RE.finditer(text):
        for call in _parse_block(match.group(1)):
            if call.tool and not call.error:
                calls.append(call)
    if calls:
        return calls, False
    return _bare_json_calls(text), False


def _bare_json_calls(text: str) -> list[ToolCall]:
    """JSON objects naming a known tool, including ``{tool:"write_file", ...}``."""
    calls: list[ToolCall] = []
    for blob in _bare_tool_blobs(text):
        try:
            data = _loads_lenient(blob)
        except json.JSONDecodeError:
            continue
        call = _call_from_data(data)
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
        call = _call_from_data(data)
        if not call.error and canonical_tool(call.tool):
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


def _parse_block(body: str) -> list[ToolCall]:
    text = body.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    text = _strip_chrome(text)
    objects = _json_objects(_repair_json(_normalize_json_text(text))) or _json_objects(text)
    if not objects:
        try:
            data = _loads_lenient(text)
        except json.JSONDecodeError as exc:
            return [ToolCall(tool="", arguments={}, error=f"invalid tool JSON: {exc}")]
        return [_call_from_data(data)]
    calls: list[ToolCall] = []
    for blob in objects:
        try:
            data = _loads_lenient(blob)
        except json.JSONDecodeError as exc:
            calls.append(ToolCall(tool="", arguments={}, error=f"invalid tool JSON: {exc}"))
            continue
        calls.append(_call_from_data(data))
    return calls


def _call_from_data(data: Any) -> ToolCall:
    if not isinstance(data, dict):
        return ToolCall(tool="", arguments={}, error="tool call must be a JSON object")
    name = data.get("tool", data.get("name"))
    if not isinstance(name, str) or not name.strip():
        return ToolCall(tool="", arguments={}, error="tool call needs a tool name")
    if "arguments" in data:
        arguments = data.get("arguments")
    elif "args" in data:
        arguments = data.get("args", {})
    else:
        arguments = {key: value for key, value in data.items() if key not in {"tool", "name"}}
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = _loads_lenient(arguments)
        except json.JSONDecodeError as exc:
            return ToolCall(tool=name.strip(), arguments={}, error=f"arguments must be a JSON object: {exc}")
    if not isinstance(arguments, dict):
        return ToolCall(tool=name.strip(), arguments={}, error="arguments must be a JSON object")
    return ToolCall(tool=name.strip(), arguments=arguments)


def _loads_lenient(text: str) -> Any:
    cleaned = _normalize_json_text(text)
    candidates = [cleaned, _TRAILING_COMMA_RE.sub(r"\1", cleaned)]
    repaired = _repair_json(cleaned)
    if repaired not in candidates:
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
    an unquoted key, and single quotes. Those come back as
    ``Expecting ',' delimiter``.
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
        if char in "\"'":
            if after_value and stack:
                out.append(",")
                expect_key = bool(stack and stack[-1] == "{")
                after_value = False
            literal, index = _read_json_string(source, index, char)
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


def _read_json_string(source: str, index: int, quote: str) -> tuple[str, int]:
    """Read one quoted string, keeping interior quotes and raw newlines."""
    index += 1
    length = len(source)
    chars: list[str] = []
    while index < length:
        char = source[index]
        if char == "\\":
            if index + 1 >= length:
                break
            escaped = source[index + 1]
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
        if char == quote and _string_ends(source, index):
            return json.dumps("".join(chars), ensure_ascii=False), index + 1
        if char == "\r":
            index += 1
            continue
        chars.append(char)
        index += 1
    return json.dumps("".join(chars), ensure_ascii=False), index


def _string_ends(source: str, index: int) -> bool:
    """True when the quote at index closes the string rather than sitting inside it."""
    pos = index + 1
    length = len(source)
    while pos < length and source[pos] in " \t\r\n":
        pos += 1
    if pos >= length or source[pos] in ",}]:":
        return True
    if source[pos] != '"':
        return source[pos] in "{[0123456789tfnTFn-"
    end = pos + 1
    escaped = False
    while end < length:
        char = source[end]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            break
        end += 1
    end += 1
    while end < length and source[end] in " \t\r\n":
        end += 1
    return end < length and source[end] == ":"


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
    "not available", "isn't available", "not exposed", "no tool", "no repository",
    "file-operation", "file operation", "don't have", "do not have", "i won't", "i will not",
)
_QUESTION_MARKERS = (
    "what would you like", "what should i", "would you like", "let me know", "for example",
    "shall i", "do you want", "what do you want",
)
_PROMISE_MARKERS = ("i'll ", "i will ", "let me ", "next i", "going to ")


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


def task_message(task: str, *, repo_map: str = "") -> str:
    """The task with the short protocol reminder and, when built, the repo map."""
    body = task.strip()
    prefix = load_prompt_sections()["TASK_PREFIX"].rstrip()
    if body.startswith(prefix.splitlines()[0]):
        return body
    parts = [prefix]
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
) -> str:
    chosen = shell or agent_shell.detect_shell(platform_name)
    lines = [
        "ENVIRONMENT",
        f"os: {platform_name or sys.platform}",
        f"shell: {chosen.label}",
        f"workspace: {workspace}",
    ]
    if chosen.kind == "powershell":
        lines.append("run_command is Windows PowerShell 5.1: chain with ; and test $? or $LASTEXITCODE. && and || do not work.")
    elif chosen.kind == "pwsh":
        lines.append("run_command is PowerShell 7: && and || work. Use PowerShell cmdlets, not Unix tools.")
    else:
        lines.append("run_command is bash -lc.")
    lines.extend(agent_shell.tool_hints(workspace, platform_name))
    lines.append(code_graph.hint(workspace))
    lines.append(
        "Each run_command is a new process whose starting folder is the workspace above, "
        "not C:\\ and not the user profile. cd does not carry over; pass cwd to use another folder. "
        "Its result starts with exit, the code and seconds, then cwd, then stdout, then stderr."
    )
    text = agent_shell.shell_preamble(chosen) + "\n\n"
    if instructions.strip():
        text += instructions.rstrip() + "\n\n"
    text += "\n".join(lines) + "\n"
    if notes.strip():
        text += "\nPROJECT NOTES (from .bot/AGENT.md; follow them)\n" + notes.strip() + "\n"
    if not instructions.strip():
        text += "\nReply with exactly READY.\n"
    return text


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


class _TaskRun:
    """One task on an open chat: send, parse, run tools, repeat until a finish."""

    def __init__(
        self,
        session: Any,
        task: str,
        *,
        ctx: ToolContext,
        sections: dict[str, str],
        turns: list[dict[str, str]],
        show: Callable[[str], None],
        max_rounds: int | None,
        max_result_chars: int,
        check_command: str | None,
    ) -> None:
        self.session = session
        self.task = task
        self.ctx = ctx
        self.state = ctx.state or TaskState(task=task)
        self.sections = sections
        self.turns = turns
        self.show = show
        self.max_rounds = max_rounds
        self.max_result_chars = max_result_chars
        self.check_command = (check_command or "").strip() or None
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
        self.payload = ""

    def _present(self, text: str) -> None:
        visible = _present_text(text)
        if visible:
            self.show(visible)

    def run(self, first_payload: str) -> str:
        self.payload = first_payload
        rounds = 0
        while self.max_rounds is None or rounds < self.max_rounds:
            rounds += 1
            with log.loading("Thinking..."):
                reply = self.session.send(self.payload)
            self.turns.append({"role": "assistant", "content": reply})
            code = self._handle(reply)
            if code is not None:
                return code
        return self._end("STOPPED", f"stopped after {self.max_rounds} tool rounds")

    def _handle(self, reply: str) -> str | None:
        detail = getattr(self.session, "last_detail", None)
        calls, unclosed = parse_tool_calls(reply)
        note = commentary(reply)
        status = _status_code(reply)
        plan = _plan_text(reply)
        shown = _answer_text(note)
        if calls and shown:
            self._present(shown)
        elif shown and not status:
            self._present(plan if plan else shown)
        elif shown and status in _STATUS_OK:
            self._present(shown)
        if unclosed or _idle_tool_reply(detail, reply):
            self.truncations += 1
            if self.truncations > MAX_TRUNCATIONS:
                return self._end("FAILED", "the reply was cut off before it finished")
            self._send_results(
                [
                    {
                        "tool": "",
                        "ok": False,
                        "error": "reply was truncated before the tool call closed; resend complete tool_call blocks",
                    }
                ]
            )
            self.last_failed = True
            return None
        if calls:
            self.refusals = 0
            self.plan_notes = 0
            self._run_calls(calls)
            if self.state.failures_in_row >= MAX_FAILED_ROUNDS:
                return self._end("BLOCKED", f"{MAX_FAILED_ROUNDS} tool rounds in a row failed")
            return None
        if status:
            return self._finish(status, reply=reply)
        if _no_edit_needed(reply):
            return self._finish("COMPLETED", unchanged_ok=True)
        if _answer_only(self.task):
            if _refuses(reply) or _asks_user(reply) or _only_promises(reply):
                self.refusals += 1
                if self.refusals > MAX_REFUSALS:
                    return self._end(
                        "FAILED", f"stopped working on the task: {_one_line(_hide_tool_markup(reply), 160)}"
                    )
                _ui("note", "Still working on it.")
                self._send_message("ANSWER")
                return None
            return self._finish("COMPLETED")
        if plan and self.plan_notes < 2:
            self.plan_notes += 1
            _ui("note", "About to make that change.")
            self._send_message("PLAN_NOTED")
            return None
        if _stalls(reply):
            if self.state.mutated and not self.last_failed and not _refuses(reply):
                return self._finish("COMPLETED")
            self.refusals += 1
            if self.refusals > MAX_REFUSALS:
                return self._end("FAILED", f"stopped working on the task: {_one_line(_hide_tool_markup(reply), 160)}")
            _ui("note", "Still working on it.")
            self._send_message("RECOVER" if self.tools_ran else "NUDGE")
            return None
        if not self.tools_ran and self.nudges < 2:
            self.nudges += 1
            _ui("note", "Still working on it.")
            self._send_message("NUDGE")
            return None
        if self.last_failed and self.recoveries < 2:
            self.recoveries += 1
            _ui("note", "Picking up where it left off.")
            self._send_message("RECOVER")
            return None
        return self._finish("COMPLETED")

    def _run_calls(self, calls: list[ToolCall]) -> None:
        results: list[dict[str, Any]] = []
        for call in calls:
            if call.error:
                results.append({"tool": call.tool, "ok": False, "error": call.error})
                _ui("bad", _friendly_error(call.error))
                continue
            _ui("work", _activity(call))
            result = _execute(call.tool, call.arguments, self.ctx)
            if result.get("ok"):
                self.tools_ran = True
                if canonical_tool(call.tool) in MUTATING:
                    self.check_passed = False
            else:
                self._note_failure(call, result)
            _ui_result(result)
            results.append(result)
        self.state.step += 1
        self.last_failed = any(not item.get("ok") for item in results)
        if not self.last_failed:
            self.recoveries = 0
        if results and all(not item.get("ok") for item in results):
            self.state.failures_in_row += 1
        else:
            self.state.failures_in_row = 0
        self._send_results(results)

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

    def _finish(self, status: str, *, reply: str = "", unchanged_ok: bool = False) -> str | None:
        if status in {"FAILED", "BLOCKED"}:
            reason = _failure_reason(reply)
            if not reason and not self.reason_asked:
                self.reason_asked = True
                _ui("note", "Asking why that failed.")
                self._send_message("WHY_FAILED")
                return None
            if reason:
                _ui("bad", reason)
        if status in _STATUS_OK:
            if self.state.mutated and self.check_command and not self.check_passed:
                if self.check_cycles >= MAX_CHECK_CYCLES:
                    return self._end("FAILED", f"the check still fails: {self.check_command}")
                self.check_cycles += 1
                _ui("work", f"Checking with {_one_line(self.check_command, 60)}")
                result = _execute("run_command", {"command": self.check_command}, self.ctx)
                if result.get("ok"):
                    self.check_passed = True
                    _ui("good", "The check passed.")
                else:
                    _ui("bad", "The check failed. Sending the failure back.")
                    self._send_results([result], extra=self.sections["CHECK_FAILED"])
                    return None
            if (
                not self.state.mutated
                and not unchanged_ok
                and not self.nothing_nudged
                and _asks_for_change(self.task)
            ):
                self.nothing_nudged = True
                _ui("note", "Nothing changed yet. Asking once more.")
                self._send_message("NOTHING_CHANGED")
                return None
        if self.state.mutated:
            self._show_diffstat()
        _print_status(status)
        return status

    def _show_diffstat(self) -> None:
        changed = ", ".join(self.state.edits) or "none"
        _ui("good", f"Changed: {_one_line(changed, 160)}")
        if not is_git_repo(self.ctx.workspace):
            return
        result = _execute("git_diff", {"stat": True}, self.ctx)
        if result.get("ok"):
            for line in str(result.get("output", "")).splitlines()[1:21]:
                _ui("good", line)

    def _end(self, code: str, message: str) -> str:
        log.warn(message)
        _ui("bad", message)
        self.turns.append({"role": "assistant", "content": message})
        _print_status(code)
        return code

    def _state_text(self) -> str:
        return self.state.render(self.sections["STATE"])

    def _send_results(self, results: list[dict[str, Any]], *, extra: str = "") -> None:
        state_text = self._state_text()
        room = max(2_000, self.max_result_chars - len(state_text) - len(extra) - 4)
        body = _cap("\n".join(format_tool_result(item) for item in results), room)
        if extra:
            body += "\n\n" + extra
        self.payload = body + "\n\n" + state_text
        self.turns.append({"role": "tool", "content": self.payload})

    def _send_message(self, section: str) -> None:
        text = fill(self.sections[section], path=self.state.last_path or "path/to/file")
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
) -> list[dict[str, str]]:
    """Talk to ``session.send`` until each task reaches a finish code.

    ``seed`` is sent first. Each task gets fresh working memory and an undo
    checkpoint. COMPLETED, FINISHED, DONE, FAILED, BLOCKED, a direct answer to a
    question, or a plain answer after a tool has run ends that task; the same
    chat waits for the next one. An answer with tool_call blocks is shown and
    the blocks still run.
    """
    turns: list[dict[str, str]] = []
    reader = read_message or _read_message
    show = emit or _emit
    sections = load_prompt_sections()
    workspace = Path(workspace).resolve()
    checkpoints = agent_edit.Checkpoints(cache_dir, workspace)
    chosen = shell or agent_shell.detect_shell()
    outcome_code = "COMPLETED"
    if seed and seed.strip():
        _seed_session(
            session,
            seed.strip(),
            turns,
            ctx=ToolContext(
                workspace=workspace,
                index_path=index_path,
                cache_dir=cache_dir,
                max_chars=min(DEFAULT_TOOL_CHARS, max_result_chars),
                runner=runner,
                shell=chosen,
            ),
            max_result_chars=max_result_chars,
            show=show,
        )
    pending = first_task.strip()
    announced = False
    while True:
        if not pending:
            if not announced:
                _ui("note", "Ready. Type a task, or exit.")
                announced = True
            pending = reader() or ""
            if not pending.strip():
                break
        task = pending.strip()
        _ui("task", f"Working on: {_one_line(task, 100)}")
        turns.append({"role": "user", "content": task})
        repo_map = _prepare_index(workspace, index_path, task)
        checkpoints.start_task()
        ctx = ToolContext(
            workspace=workspace,
            index_path=index_path,
            cache_dir=cache_dir,
            max_chars=min(DEFAULT_TOOL_CHARS, max_result_chars),
            runner=runner,
            state=TaskState(task=task),
            checkpoints=checkpoints,
            shell=chosen,
        )
        run = _TaskRun(
            session,
            task,
            ctx=ctx,
            sections=sections,
            turns=turns,
            show=show,
            max_rounds=max_rounds,
            max_result_chars=max_result_chars,
            check_command=check_command,
        )
        outcome_code = run.run(task_message(task, repo_map=repo_map))
        pending = ""
    if outcome is not None:
        outcome[:] = [outcome_code]
    return turns


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


def _seed_session(
    session: Any,
    seed: str,
    turns: list[dict[str, str]],
    *,
    ctx: ToolContext,
    max_result_chars: int,
    show: Callable[[str], None],
) -> None:
    """Send the tool instructions once. Read-only tool calls in the reply still run."""
    _ui("note", "Getting ready.")
    payload = seed
    turns.append({"role": "user", "content": seed})
    for _ in range(3):
        with log.loading("Thinking..."):
            reply = session.send(payload)
        calls, unclosed = parse_tool_calls(reply)
        note = commentary(reply)
        if note and _status_code(note) is None and note.strip().upper() != "READY":
            visible = _hide_tool_markup(note)
            if visible:
                show(visible)
        turns.append({"role": "assistant", "content": reply})
        if unclosed or _idle_tool_reply(getattr(session, "last_detail", None), reply):
            payload = format_tool_result(
                {"tool": "", "ok": False, "error": "reply was truncated before the tool call closed; resend complete tool_call blocks"}
            )
            turns.append({"role": "tool", "content": payload})
            continue
        if not calls:
            return
        results = []
        for call in calls:
            if call.error:
                results.append({"tool": call.tool, "ok": False, "error": call.error})
            elif call.tool in MUTATING:
                results.append(
                    {"tool": call.tool, "ok": False, "error": "no task yet; wait for the task before changing files"}
                )
            else:
                _ui("work", _activity(call))
                results.append(_execute(call.tool, call.arguments, ctx))
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
) -> int:
    """Open the Edge session, run the tool loop, and write the transcript."""
    from critique_bot.browser import BrowserError
    from critique_bot.chat_client import ChatError
    from critique_bot.provider import open_provider

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
    check_command = settings.get("check_command") if isinstance(settings.get("check_command"), str) else None
    shell = agent_shell.detect_shell()
    seed = seed_message(home.root, instructions, shell=shell, notes=home.project_notes())
    _ensure_code_graph(home.root)
    try:
        with open_provider(config, headed=headed) as provider:
            with provider.session() as session:
                try:
                    turns = run_agent_loop(
                        session,
                        workspace=home.root,
                        index_path=home.index_path,
                        cache_dir=home.cache_dir,
                        first_task=task,
                        max_rounds=max_rounds,
                        max_result_chars=_result_budget(config, settings),
                        seed=seed,
                        outcome=outcome,
                        check_command=check_command,
                        shell=shell,
                    )
                except Exception:
                    page = getattr(session, "page", None)
                    if page is not None:
                        from critique_bot.output import save_failure

                        save_failure(page, home.sessions_dir)
                    raise
    except (BrowserError, ChatError) as exc:
        log.error(str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        log.exception(f"unexpected failure: {exc}")
        print(f"error: unexpected failure: {exc}", file=sys.stderr)
        return 1

    if not turns:
        return 0
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
    stamp = started.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    write_output(home.sessions_dir / stamp, body, payload, stem="agent", print_body=False)
    if output_dir is not None:
        write_output(output_dir, body, payload, stem="agent", print_body=False)
    return 0 if outcome[-1] in _STATUS_OK else 1


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
    if not sys.stderr.isatty():
        return text
    return f"\033[{color}m{text}\033[0m"


def _present_text(text: str) -> str:
    return _hide_tool_markup(text)


def _ui(kind: str, message: str) -> None:
    message = _present_text(message)
    if not message:
        return
    colors = {"task": "1", "note": "33", "work": "36", "good": "32", "bad": "31"}
    line = _paint(_one_line(message), colors.get(kind, "0"))
    log.print_safe(f"  {line}", file=sys.stderr, flush=True)


def _print_status(code: str) -> None:
    color = "1;32" if code in _STATUS_OK else "1;31"
    log.print_safe(file=sys.stderr)
    log.print_safe(_paint(code, color), file=sys.stderr, flush=True)


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
    log.print_safe(visible, flush=True)
    log.print_safe(flush=True)


def _read_message() -> str | None:
    if not sys.stdin.isatty():
        return None
    chunks: list[str] = []
    while True:
        prefix = "You> " if not chunks else "... "
        try:
            line = input(prefix)
        except EOFError:
            print(flush=True)
            if chunks:
                return "\n".join(chunks).rstrip()
            return None
        except KeyboardInterrupt:
            print(flush=True)
            return None
        if not chunks:
            if not line.strip():
                continue
            if line.strip().lower() in _QUIT:
                return None
        if line.endswith("\\") and not line.endswith("\\\\"):
            chunks.append(line[:-1])
            continue
        chunks.append(line)
        return "\n".join(chunks).rstrip()
