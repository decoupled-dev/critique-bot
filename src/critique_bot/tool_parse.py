"""Tool calls a model wrote as text, for servers whose native tool-call parser missed them.

With native tool calling the server turns the model's call template into structured
``tool_calls``. Local servers do not always manage it: the calls then arrive in the
reply text, or in the reasoning (llama.cpp #20809), in the model family's own format.
:func:`recover_calls` finds them in the formats open models use:

* Hermes / Qwen 2.5: ``<tool_call>{"name": ..., "arguments": {...}}</tool_call>``
* Qwen3-Coder / Qwen3.5+ XML: ``<function=NAME><parameter=KEY>VALUE</parameter>...</function>``
  (optionally inside ``<tool_call>``); a parameter given twice keeps the last value
  (llama.cpp #19382 sent duplicates)
* Mistral: ``[TOOL_CALLS] [{"name": ..., "arguments": {...}}]``
* a fenced or bare JSON object ``{"name": ..., "arguments": {...}}`` (or a list of them)

:func:`validate_arguments` checks arguments against a tool's JSON Schema (the subset crit's
tools use), so a wrong call gets a precise error back instead of failing inside the tool.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_HERMES_RE = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.DOTALL)
_XML_FUNC_RE = re.compile(r"<function=([^>\s]+)>(.*?)(?:</function>|$)", re.DOTALL)
_XML_PARAM_RE = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.DOTALL)
_MISTRAL_RE = re.compile(r"\[TOOL_CALLS\]\s*(\[.*\])", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_TOOL_CALL_TAGS_RE = re.compile(r"</?tool_call>")


@dataclass
class RecoveredCall:
    name: str
    arguments: dict[str, Any]
    #: How it was found (hermes, qwen-xml, mistral, json), for parse notes and metrics.
    source: str
    notes: list[str] = field(default_factory=list)


def _as_arguments(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    if value is None:
        return {}
    return None


def _from_json_value(value: Any, source: str) -> list[RecoveredCall]:
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else item
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        arguments = _as_arguments(function.get("arguments", function.get("parameters")))
        if arguments is None:
            continue
        out.append(RecoveredCall(name.strip(), arguments, source))
    return out


def _parameter_value(raw: str) -> Any:
    """XML parameter text: JSON when it parses as an object, list, number or bool, else the text."""
    text = raw.strip("\n")
    stripped = text.strip()
    if stripped[:1] in "{[" or stripped in {"true", "false", "null"} or re.fullmatch(r"-?\d+(\.\d+)?", stripped):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return text


def _from_xml(text: str) -> list[RecoveredCall]:
    out = []
    for match in _XML_FUNC_RE.finditer(text):
        name, body = match.group(1).strip(), match.group(2)
        arguments: dict[str, Any] = {}
        notes = []
        for param in _XML_PARAM_RE.finditer(body):
            key = param.group(1).strip()
            if key in arguments:
                notes.append(f"parameter {key!r} was given twice; kept the last value")
            arguments[key] = _parameter_value(param.group(2))
        out.append(RecoveredCall(name, arguments, "qwen-xml", notes))
    return out


def recover_calls(text: str) -> tuple[list[RecoveredCall], str]:
    """The tool calls written in ``text``, and the text with them taken out."""
    if not text:
        return [], text
    if "<function=" in text:
        calls = _from_xml(text)
        if calls:
            rest = _XML_FUNC_RE.sub("", text)
            return calls, _TOOL_CALL_TAGS_RE.sub("", rest).strip()
    if "<tool_call>" in text:
        calls: list[RecoveredCall] = []
        for match in _HERMES_RE.finditer(text):
            try:
                calls += _from_json_value(json.loads(match.group(1)), "hermes")
            except ValueError:
                continue
        if calls:
            return calls, _HERMES_RE.sub("", text).strip()
    match = _MISTRAL_RE.search(text)
    if match:
        try:
            calls = _from_json_value(json.loads(match.group(1)), "mistral")
        except ValueError:
            calls = []
        if calls:
            return calls, (text[: match.start()] + text[match.end() :]).strip()
    for match in _FENCE_RE.finditer(text):
        try:
            calls = _from_json_value(json.loads(match.group(1)), "json")
        except ValueError:
            continue
        if calls:
            return calls, (text[: match.start()] + text[match.end() :]).strip()
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            calls = _from_json_value(json.loads(stripped), "json")
        except ValueError:
            calls = []
        if calls:
            return calls, ""
    return [], text


# --------------------------------------------------------------------------- schema


_TYPES = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


def _type_ok(value: Any, expected: str) -> bool:
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES.get(expected, (object,)))


def validate_arguments(arguments: Any, schema: dict[str, Any], *, path: str = "") -> list[str]:
    """Problems with ``arguments`` against ``schema``: type, required, enum, minimum, maximum, items."""
    problems: list[str] = []
    where = path or "arguments"
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_type_ok(arguments, kind) for kind in expected):
            return [f"{where} should be one of {expected}, got {type(arguments).__name__}"]
    elif isinstance(expected, str) and not _type_ok(arguments, expected):
        return [f"{where} should be {expected}, got {type(arguments).__name__}"]
    if "enum" in schema and arguments not in schema["enum"]:
        problems.append(f"{where} should be one of {schema['enum']}, got {arguments!r}")
    if isinstance(arguments, (int, float)) and not isinstance(arguments, bool):
        if "minimum" in schema and arguments < schema["minimum"]:
            problems.append(f"{where} should be at least {schema['minimum']}, got {arguments}")
        if "maximum" in schema and arguments > schema["maximum"]:
            problems.append(f"{where} should be at most {schema['maximum']}, got {arguments}")
    if isinstance(arguments, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in arguments:
                problems.append(f"{where}.{name} is required" if path else f"argument {name!r} is required")
        for name, value in arguments.items():
            sub = properties.get(name)
            if isinstance(sub, dict):
                problems += validate_arguments(value, sub, path=f"{path}.{name}" if path else name)
    if isinstance(arguments, list) and isinstance(schema.get("items"), dict):
        for index, value in enumerate(arguments):
            problems += validate_arguments(value, schema["items"], path=f"{where}[{index}]")
    return problems
