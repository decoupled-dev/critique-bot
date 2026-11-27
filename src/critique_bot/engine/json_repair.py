"""Lenient JSON for tool calls: what chat pages and small models get wrong, put right.

Raw Windows paths with single backslashes, unescaped quotes and newlines inside strings,
trailing commas, single quotes, and bare keys. Nothing here knows about tools.
"""

from __future__ import annotations

import json
import re
from typing import Any


# A Windows path written with single backslashes: C:\new\tools, .\src\app.py
_RAW_WIN_PATH_RE = re.compile(r"""(?:\b[A-Za-z]:|(?<![\w\\])\.{1,2})\\(?![\\"'/])""")


_BARE_TOOL_KEY_RE = re.compile(
    r"""(?:"tool"|'tool'|"name"|'name'|(?<![A-Za-z0-9_$])(?:tool|name)\s*:)"""
)


_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


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
