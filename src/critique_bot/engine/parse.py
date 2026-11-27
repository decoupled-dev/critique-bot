"""Reading a model's reply: tool calls in the tag format or as Responses items, and the words.

The canonical tag is ``<tool_call>``. Models also print ``<tool_use>``, ``<function_call>``,
and ``<tool_call name="read_files">`` with the arguments as the body; all of them are read
the same way. JSON that a chat page or a model mangled is repaired in :mod:`json_repair`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from critique_bot.engine.json_repair import _BARE_TOOL_KEY_RE, _json_objects, _loads_lenient, _normalize_json_text, _repair_json
from critique_bot.tool_api import canonical_tool


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


@dataclass(frozen=True)
class ToolCall:
    tool: str
    arguments: dict[str, Any]
    error: str | None = None
    #: The call_id of an OpenAI-format call; its result goes back with the same id.
    call_id: str = ""


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
        return [ToolCall(tool=name, arguments={}, error=_json_error(f'"tool": "{name}"' + raw, exc))]
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


def hide_tool_markup(text: str) -> str:
    """Text with the tool-call protocol removed, as the user should see it.

    A reply in the OpenAI Responses format gives the words of its message items.
    """
    from critique_bot import openai_format

    words = openai_format.visible_text(text, loads=_loads_lenient)
    if words is not None:
        return words
    return _hide_tool_markup(text)


def _openai_reply(raw: str, complete: bool) -> Any:
    """A Responses-format reply, parsed; None when the reply is not in that format."""
    from critique_bot import openai_format

    return openai_format.parse_reply(raw, loads=_loads_lenient, complete=complete)


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


_WRITE_CALL_RE = re.compile(r'"(?:tool|name)"\s*:\s*"(?:write_files?|write|edit_file|edit)"')


_WRITE_JSON_HINT = (
    ". In contents, old_string, and new_string write a quote as \\\", a newline as \\n, and a backslash as \\\\; "
    "no raw line breaks inside a string. Send a long file in parts: write_files with the first part, then edit_file"
)


def _json_error(text: str, exc: json.JSONDecodeError) -> str:
    """The parse error for the model, with the escaping reminder when the call carried file text."""
    message = f"invalid tool JSON: {exc}"
    if _WRITE_CALL_RE.search(text):
        message += _WRITE_JSON_HINT
    return message


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
            return [ToolCall(tool="", arguments={}, error=_json_error(text, exc))]
        return _calls_from_data(data)
    calls: list[ToolCall] = []
    for blob in objects:
        try:
            data = _loads_lenient(blob)
        except json.JSONDecodeError as exc:
            calls.append(ToolCall(tool="", arguments={}, error=_json_error(blob, exc)))
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


_STATUS_WORDS = {
    "completed": "COMPLETED",
    "finished": "FINISHED",
    "done": "DONE",
    "failed": "FAILED",
    "blocked": "BLOCKED",
}


_STATUS_OK = frozenset({"COMPLETED", "FINISHED", "DONE"})
