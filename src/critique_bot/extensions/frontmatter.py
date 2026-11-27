# SPDX-License-Identifier: Apache-2.0
"""A tiny reader for the YAML-like frontmatter of agent and command files.

Only the subset those files need, so crit needs no YAML library:

```
---
name: reviewer                 # a comment
description: "Reviews diffs"   # quoted or plain strings
max_steps: 40                  # integers, floats, true/false, null
tools: [read_files, git_diff]  # inline lists
budget: {tokens: 400000, minutes: 45}   # inline maps
skills:                        # block lists
  - aosp
arguments:                     # block lists of small maps
  - name: issue
    description: the issue key
body: |                        # block text (| keeps lines, > folds them)
  first line
  second line
---
The body (the system prompt or the command template).
```

Anything outside the subset raises :class:`FrontmatterError` with the line number.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["FrontmatterError", "parse", "split"]


class FrontmatterError(ValueError):
    """The frontmatter is not in the supported subset."""


_KEY_RE = re.compile(r"^([A-Za-z_][\w.-]*)\s*:(?:\s+(.*))?$")


def split(text: str) -> tuple[str, str]:
    """``(frontmatter, body)``; frontmatter is "" when the text does not start with ``---``."""
    text = text.lstrip("﻿")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :]).lstrip("\n")
    raise FrontmatterError("the frontmatter starts with --- but never ends with a --- line")


def parse(text: str) -> tuple[dict[str, Any], str]:
    """``(fields, body)`` of a file with optional frontmatter."""
    head, body = split(text)
    return parse_block(head), body


def parse_block(head: str) -> dict[str, Any]:
    lines = head.splitlines()
    data, end = _parse_map(lines, 0, 0)
    if end < len(lines):
        raise FrontmatterError(f"line {end + 1}: unexpected indentation")
    return data


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _blank(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _parse_map(lines: list[str], start: int, indent: int) -> tuple[dict[str, Any], int]:
    data: dict[str, Any] = {}
    index = start
    while index < len(lines):
        line = lines[index]
        if _blank(line):
            index += 1
            continue
        here = _indent(line)
        if here < indent:
            break
        if here > indent:
            raise FrontmatterError(f"line {index + 1}: unexpected indentation")
        if "\t" in line[: here + 1]:
            raise FrontmatterError(f"line {index + 1}: use spaces, not tabs")
        match = _KEY_RE.match(line.strip())
        if not match:
            raise FrontmatterError(f"line {index + 1}: expected 'key: value', got {line.strip()!r}")
        key, raw = match.group(1), _strip_comment(match.group(2) or "")
        index += 1
        if raw in {"|", ">", "|-", ">-"}:
            value, index = _block_text(lines, index, indent, folded=raw.startswith(">"))
        elif raw:
            value = _scalar(raw, index)
        else:
            value, index = _nested(lines, index, indent)
        data[key] = value
    return data, index


def _nested(lines: list[str], index: int, indent: int) -> tuple[Any, int]:
    """The block after ``key:``: a list of ``- item`` lines, a deeper map, or nothing (None)."""
    probe = index
    while probe < len(lines) and _blank(lines[probe]):
        probe += 1
    if probe >= len(lines) or _indent(lines[probe]) <= indent and not lines[probe].lstrip().startswith("- "):
        return None, index
    child = _indent(lines[probe])
    if lines[probe].lstrip().startswith("-"):
        if child < indent:
            return None, index
        return _parse_list(lines, probe, child)
    return _parse_map(lines, probe, child)


def _parse_list(lines: list[str], start: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if _blank(line):
            index += 1
            continue
        here = _indent(line)
        stripped = line.strip()
        if here < indent or (here == indent and not stripped.startswith("-")):
            break
        if here > indent or not (stripped == "-" or stripped.startswith("- ")):
            raise FrontmatterError(f"line {index + 1}: expected '- item'")
        rest = stripped[1:].strip()
        index += 1
        if not rest:
            value, index = _nested(lines, index, indent)
            items.append(value)
            continue
        match = _KEY_RE.match(rest)
        if match and not rest.startswith(("'", '"', "[", "{")):
            # "- name: x" opens a small map; its other keys sit under the first one.
            inner = " " * (here + 2) + rest
            block = [inner]
            while index < len(lines) and (_blank(lines[index]) or _indent(lines[index]) > here):
                block.append(lines[index])
                index += 1
            value, _ = _parse_map(block, 0, here + 2)
            items.append(value)
            continue
        items.append(_scalar(_strip_comment(rest), index))
    return items, index


def _block_text(lines: list[str], index: int, indent: int, *, folded: bool) -> tuple[str, int]:
    collected: list[str] = []
    child: int | None = None
    while index < len(lines):
        line = lines[index]
        if line.strip() and _indent(line) <= indent:
            break
        if line.strip() and child is None:
            child = _indent(line)
        collected.append(line[child or 0 :] if line.strip() else "")
        index += 1
    while collected and not collected[-1]:
        collected.pop()
    if folded:
        paragraphs = "\n".join(collected).split("\n\n")
        return "\n\n".join(" ".join(part.split("\n")) for part in paragraphs), index
    return "\n".join(collected), index


def _strip_comment(raw: str) -> str:
    """``value  # note`` without the note; a # inside quotes or brackets stays."""
    quote = ""
    depth = 0
    for position, char in enumerate(raw):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "#" and depth <= 0 and (position == 0 or raw[position - 1] in " \t"):
            return raw[:position].rstrip()
    return raw.strip()


_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _scalar(raw: str, line_no: int) -> Any:
    raw = raw.strip()
    if not raw:
        return None
    if raw[0] in "[{":
        return _flow(raw, line_no)
    if raw[0] == '"':
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FrontmatterError(f"line {line_no}: bad quoted string: {exc}") from exc
        return value
    if raw[0] == "'":
        if len(raw) < 2 or not raw.endswith("'"):
            raise FrontmatterError(f"line {line_no}: unclosed quote")
        return raw[1:-1].replace("''", "'")
    lowered = raw.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "~", "none"}:
        return None
    if _NUMBER_RE.match(raw):
        return float(raw) if "." in raw else int(raw)
    return raw


def _flow(raw: str, line_no: int) -> Any:
    """An inline ``[a, b]`` list or ``{k: v}`` map (nested ones too)."""
    value, end = _flow_value(raw, 0, line_no)
    if raw[end:].strip():
        raise FrontmatterError(f"line {line_no}: text after the closing bracket: {raw[end:].strip()!r}")
    return value


def _flow_value(raw: str, pos: int, line_no: int) -> tuple[Any, int]:
    while pos < len(raw) and raw[pos] == " ":
        pos += 1
    if pos >= len(raw):
        raise FrontmatterError(f"line {line_no}: unclosed bracket")
    opener = raw[pos]
    if opener in "[{":
        closer = "]" if opener == "[" else "}"
        items: list[Any] = []
        mapping: dict[str, Any] = {}
        pos += 1
        while True:
            while pos < len(raw) and raw[pos] == " ":
                pos += 1
            if pos >= len(raw):
                raise FrontmatterError(f"line {line_no}: unclosed {opener}")
            if raw[pos] == closer:
                return (items if opener == "[" else mapping), pos + 1
            if opener == "{":
                colon = raw.find(":", pos)
                if colon < 0:
                    raise FrontmatterError(f"line {line_no}: expected 'key: value' in {{...}}")
                key = _scalar(raw[pos:colon].strip(), line_no)
                value, pos = _flow_value(raw, colon + 1, line_no)
                mapping[str(key)] = value
            else:
                value, pos = _flow_value(raw, pos, line_no)
                items.append(value)
            while pos < len(raw) and raw[pos] == " ":
                pos += 1
            if pos < len(raw) and raw[pos] == ",":
                pos += 1
            elif pos < len(raw) and raw[pos] != closer:
                raise FrontmatterError(f"line {line_no}: expected , or {closer}")
    if opener in "'\"":
        end = pos + 1
        while end < len(raw):
            if raw[end] == "\\" and opener == '"':
                end += 2
                continue
            if raw[end] == opener:
                break
            end += 1
        return _scalar(raw[pos : end + 1], line_no), end + 1
    end = pos
    while end < len(raw) and raw[end] not in ",]}":
        end += 1
    return _scalar(raw[pos:end], line_no), end
