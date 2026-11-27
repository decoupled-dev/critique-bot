# SPDX-License-Identifier: Apache-2.0
"""The OpenAI Responses API format as the agent's tool-call protocol over a text chat.

With ``"tool_format": "openai"`` the first message gives the model its tools as an OpenAI
``tools`` array and asks it to reply as the Responses API does:

    {"output": [
      {"type": "message", "content": [{"type": "output_text", "text": "Reading the config."}]},
      {"type": "function_call", "call_id": "call_1", "name": "read_files",
       "arguments": "{\\"paths\\": [\\"src/app.py\\"]}"}
    ]}

Results go back as ``function_call_output`` items with the same ``call_id``. Models know
this shape well, so the instructions about the format shrink to a few lines, and the tool
definitions come from the tool registry instead of prose.

This module only reads and writes the format; the engine decides what to do with it. It
accepts the shapes a chat model actually writes: a fenced or bare ``{"output": [...]}``,
a bare item or a list of items, Chat Completions ``tool_calls`` or a whole Chat Completions
response, and ``arguments`` as a JSON string or as an object.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

#: The item types that call a tool.
_CALL_TYPES = frozenset({"function_call", "tool_call", "function", "custom_tool_call"})
#: The item types that carry words.
_TEXT_TYPES = frozenset({"output_text", "text", "input_text"})
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\r?\n(.*?)```", re.S)
_EMPTY_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\r?\n?\s*```")

INSTRUCTION = (
    "FORMAT. The tools under TOOLS are not in your built-in tool list: you call them by writing function_call "
    "items in your reply text, and this program runs them on the user's computer and sends back the results. "
    "Reply only as the OpenAI Responses API does: one JSON object {\"output\": [...]}, no other text. "
    "Call tools with function_call items (call_id, name, arguments); put words for the user in a message item. "
    "Results come back as function_call_output items with the same call_id. For example:\n"
    '{"output": [{"type": "function_call", "call_id": "c1", "name": "read_files", "arguments": {"paths": ["README.md"]}}]}'
)


@dataclass
class WireCall:
    """One function_call item."""

    name: str
    arguments: dict[str, Any]
    call_id: str
    error: str | None = None


@dataclass
class WireReply:
    """What one reply held: the words, the calls, and whether it was cut or made up results."""

    text: str
    calls: list[WireCall] = field(default_factory=list)
    #: A call started but its JSON never closed (the reply was cut off).
    unclosed: bool = False
    #: The reply contained function_call_output items, which only the program writes.
    fabricated: bool = False


def parse_reply(
    raw: str,
    *,
    loads: Callable[[str], Any] = json.loads,
    complete: bool = True,
) -> WireReply | None:
    """The Responses-style content of ``raw``, or None when it has none (the caller falls back).

    ``loads`` parses one JSON value; the engine passes its lenient parser, which repairs the
    small mistakes chat models make. ``complete`` is False when the reply may still have been
    cut off; then an unbalanced object that names a function_call marks the reply unclosed.
    """
    text = raw or ""
    spans: list[tuple[int, int]] = []
    items: list[dict[str, Any]] = []
    shaped = False  # a {"output": [...]} was there, even an empty one
    for start, end, body in _candidates(text):
        try:
            value = loads(body)
        except (ValueError, TypeError):
            continue
        found = list(_items(value))
        empty_output = isinstance(value, dict) and value.get("output") == []
        if not found and not empty_output:
            continue
        shaped = shaped or empty_output
        items.extend(found)
        spans.append((start, end))
    unclosed = _has_unclosed_call(text, spans)
    if not items and shaped:
        # {"output": []}: a Responses reply with nothing in it, not plain text to take as an answer.
        return WireReply(text=_outside(text, spans), unclosed=unclosed and complete is False)
    if not items:
        if unclosed:
            return WireReply(text=_outside(text, spans), unclosed=True)
        return None
    words: list[str] = []
    calls: list[WireCall] = []
    seen: set[str] = set()
    fabricated = False
    for item in items:
        kind = str(item.get("type") or "").lower()
        if kind == "function_call_output":
            fabricated = True
            break  # everything after a made-up result is ignored
        if kind in _CALL_TYPES or (not kind and ("name" in item and "arguments" in item)):
            call = _call(item, len(calls) + 1, loads)
            key = json.dumps([call.call_id, call.name, call.arguments], sort_keys=True, default=str)
            if key in seen:
                continue  # the same item twice (a page that shows a reply's JSON twice): run it once
            seen.add(key)
            calls.append(call)
        elif kind == "message" or kind in _TEXT_TYPES or "content" in item:
            said = _message_text(item)
            if said and said not in words:
                words.append(said)
    outside = _outside(text, spans)
    if outside:
        words.insert(0, outside)
    return WireReply(
        text="\n\n".join(part for part in words if part).strip(),
        calls=calls,
        unclosed=unclosed and complete is False,
        fabricated=fabricated,
    )


def format_results(results: Iterable[dict[str, Any]]) -> str:
    """Tool results as Responses ``function_call_output`` input items.

    ``output`` stays a JSON object rather than a JSON string, so file text is escaped once,
    not twice; models read it either way.
    """
    items = []
    for index, result in enumerate(results, start=1):
        payload: dict[str, Any] = {"tool": result.get("tool", ""), "ok": bool(result.get("ok"))}
        if payload["ok"]:
            payload["output"] = result.get("output", "")
        else:
            payload["error"] = result.get("error") or "tool failed"
            if result.get("output"):
                payload["output"] = result["output"]
            if result.get("allowed"):
                payload["allowed"] = list(result["allowed"])
        items.append(
            {
                "type": "function_call_output",
                "call_id": str(result.get("call_id") or f"call_{index}"),
                "output": payload,
            }
        )
    return json.dumps({"input": items}, ensure_ascii=False)


def tools_block(tools: list[dict[str, Any]]) -> str:
    """The tool definitions for the first message, as the ``tools`` field of a Responses request."""
    flat = []
    for tool in tools:
        function = tool.get("function") if tool.get("type") == "function" else None
        if function:  # Responses tools are flat: {"type": "function", "name", "description", "parameters"}
            flat.append({"type": "function", **function})
        else:
            flat.append(tool)
    return "TOOLS (OpenAI Responses API)\n" + json.dumps({"tools": flat}, ensure_ascii=False, separators=(",", ":"))


def visible_text(raw: str, *, loads: Callable[[str], Any] = json.loads) -> str | None:
    """The words of a Responses-style reply (None when it is not one)."""
    parsed = parse_reply(raw, loads=loads)
    return None if parsed is None else parsed.text


# --------------------------------------------------------------------------- reading


def _candidates(text: str) -> list[tuple[int, int, str]]:
    """(start, end, json) for fenced blocks, then balanced top-level objects or arrays outside them."""
    found: list[tuple[int, int, str]] = []
    fenced: list[tuple[int, int]] = []
    for match in _FENCE_RE.finditer(text):
        body = match.group(1).strip()
        if body[:1] in "{[":
            found.append((match.start(), match.end(), body))
            fenced.append((match.start(), match.end()))
    index = 0
    while index < len(text):
        char = text[index]
        inside = next((end for start, end in fenced if start <= index < end), None)
        if inside is not None:
            index = inside
            continue
        if char in "{[":
            end = _balanced_end(text, index)
            if end is None:
                break
            found.append((index, end, text[index:end]))
            index = end
            continue
        index += 1
    return found


def _balanced_end(text: str, start: int) -> int | None:
    """The index after the bracket that closes the one at ``start``; None when it never closes."""
    pairs = {"{": "}", "[": "]"}
    stack = [pairs[text[start]]]
    in_string = False
    escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
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
        elif char in pairs:
            stack.append(pairs[char])
        elif char in "}]":
            if not stack or char != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return index + 1
    return None


def _has_unclosed_call(text: str, spans: list[tuple[int, int]]) -> bool:
    """True when text outside the parsed objects opens an object that names a function_call."""
    rest = _outside(text, spans, keep_fences=True)
    opening = rest.find("{")
    return opening >= 0 and '"function_call"' in rest[opening:] and _balanced_end(rest, opening) is None


def _items(value: Any) -> Iterable[dict[str, Any]]:
    """The Responses items in one parsed value."""
    if isinstance(value, list):
        for entry in value:
            yield from _items(entry)
        return
    if not isinstance(value, dict):
        return
    if isinstance(value.get("output"), list):
        yield from _items(value["output"])
        return
    choices = value.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or choices[0].get("delta") or {}
        yield from _chat_message(message)
        return
    kind = str(value.get("type") or "").lower()
    if kind != "message" and (isinstance(value.get("tool_calls"), list) or value.get("role") == "assistant"):
        yield from _chat_message(value)
        return
    if kind in _CALL_TYPES or kind == "message" or kind in _TEXT_TYPES or kind == "function_call_output":
        yield value
    elif "name" in value and "arguments" in value:
        yield {"type": "function_call", **value}


def _chat_message(message: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """A Chat Completions assistant message as Responses items."""
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        yield {"type": "message", "content": [{"type": "output_text", "text": content}]}
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else call
        yield {
            "type": "function_call",
            "call_id": call.get("id") or call.get("call_id") or "",
            "name": function.get("name", ""),
            "arguments": function.get("arguments", {}),
        }


def _call(item: dict[str, Any], number: int, loads: Callable[[str], Any]) -> WireCall:
    function = item.get("function") if isinstance(item.get("function"), dict) else item
    name = str(function.get("name") or item.get("tool") or "").strip()
    call_id = str(item.get("call_id") or item.get("id") or f"call_{number}")
    raw = function.get("arguments", function.get("input", {}))
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = {}
    if isinstance(raw, str):
        try:
            raw = loads(raw)
        except (ValueError, TypeError) as exc:
            return WireCall(name, {}, call_id, f"arguments must be a JSON object: {exc}")
    if not name:
        return WireCall("", {}, call_id, "function_call needs a name")
    if not isinstance(raw, dict):
        return WireCall(name, {}, call_id, "arguments must be a JSON object")
    return WireCall(name, raw, call_id)


def _message_text(item: dict[str, Any]) -> str:
    content = item.get("content", item.get("text"))
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "\n".join(part.strip() for part in parts if part.strip())


def _outside(text: str, spans: list[tuple[int, int]], *, keep_fences: bool = False) -> str:
    """``text`` without the parsed objects (and the fences left empty around them)."""
    pieces: list[str] = []
    last = 0
    for start, end in sorted(spans):
        pieces.append(text[last:start])
        last = max(last, end)
    pieces.append(text[last:])
    rest = "".join(pieces)
    if not keep_fences:
        rest = _EMPTY_FENCE_RE.sub("", rest)
    return re.sub(r"\n{3,}", "\n\n", rest).strip()
