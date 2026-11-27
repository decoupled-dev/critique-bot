"""Read docs/api/openapi.yaml without a YAML library (crit has no base dependency for it).

The spec is written in a small block-style subset: maps, lists (``- ``), plain, quoted and
flow-list (``[a, b]``) scalars, and ``>-`` folded text. :func:`load` reads that subset;
the tests also compare it with PyYAML when it happens to be installed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SPEC = Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.yaml"


def load(path: Path = SPEC) -> Any:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), raw.strip()))
    value, index = _block(lines, 0, lines[0][0])
    if index != len(lines):
        raise ValueError(f"unparsed line {index}: {lines[index]}")
    return value


def _block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if lines[index][1].startswith("- "):
        return _list(lines, index, indent)
    return _map(lines, index, indent)


def _map(lines: list[tuple[int, str]], index: int, indent: int, first: str | None = None) -> tuple[dict, int]:
    out: dict[str, Any] = {}
    while index < len(lines):
        level, text = lines[index]
        if first is not None:
            text, first = first, None
        elif level != indent:
            break
        key, rest = _split_key(text)
        index += 1
        if rest == ">-":
            parts = []
            while index < len(lines) and lines[index][0] > indent:
                parts.append(lines[index][1])
                index += 1
            out[key] = " ".join(parts)
        elif rest == "":
            if index < len(lines) and (lines[index][0] > indent or lines[index][1].startswith("- ")):
                child_indent = lines[index][0]
                out[key], index = _block(lines, index, child_indent)
            else:
                out[key] = None
        else:
            out[key] = _scalar(rest)
    return out, index


def _list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list, int]:
    out: list[Any] = []
    while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
        item = lines[index][1][2:].strip()
        if re.match(r'^("[^"]*"|[^\s"\[]+):(\s|$)', item):
            value, index = _map(lines, index, indent + 2, first=item)
            # the keys after the first sit at indent + 2
            out.append(value)
        else:
            out.append(_scalar(item))
            index += 1
    return out, index


def _split_key(text: str) -> tuple[str, str]:
    if text.startswith('"'):
        end = text.index('"', 1)
        return text[1:end], text[end + 2:].strip()
    key, _, rest = text.partition(":")
    return key.strip(), rest.strip()


def _scalar(text: str) -> Any:
    if text.startswith('"'):
        return json.loads(text)
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(part.strip()) for part in _split_flow(inner)] if inner else []
    if text in {"true", "false"}:
        return text == "true"
    if text == "null":
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _split_flow(text: str) -> list[str]:
    parts, current, quoted = [], "", False
    for char in text:
        if char == '"':
            quoted = not quoted
        if char == "," and not quoted:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


def resolve(spec: dict, node: Any) -> Any:
    """``node`` with a top-level ``$ref`` followed."""
    while isinstance(node, dict) and "$ref" in node:
        target: Any = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target
    return node


def check(spec: dict, schema: Any, value: Any, where: str = "$") -> list[str]:
    """The ways ``value`` breaks ``schema`` (type, required, enum, properties, items, minimum)."""
    schema = resolve(spec, schema)
    if not isinstance(schema, dict):
        return []
    problems: list[str] = []
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
        ok = False
        for kind in allowed:
            if kind == "integer":
                ok = ok or (isinstance(value, int) and not isinstance(value, bool))
            elif kind == "number":
                ok = ok or (isinstance(value, (int, float)) and not isinstance(value, bool))
            else:
                ok = ok or isinstance(value, kinds[kind])
        if not ok:
            return [f"{where}: {type(value).__name__} is not {types}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                problems.append(f"{where}: missing {key}")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                problems += check(spec, sub, value[key], f"{where}.{key}")
    if isinstance(value, list) and "items" in schema:
        for number, item in enumerate(value):
            problems += check(spec, schema["items"], item, f"{where}[{number}]")
    if isinstance(value, int) and "minimum" in schema and value < schema["minimum"]:
        problems.append(f"{where}: {value} < {schema['minimum']}")
    return problems
