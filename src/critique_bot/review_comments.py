"""Parse ChatGPT review JSON and map comments onto unified-diff lines."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S | re.I)
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
MAX_INLINE_COMMENTS = 12


@dataclass(frozen=True)
class InlineComment:
    path: str
    line: int
    side: str
    body: str


@dataclass(frozen=True)
class DiffLine:
    path: str
    old_path: str
    old_line: int | None
    new_line: int | None
    kind: str  # add, del, context


def strip_json_block(review_md: str) -> str:
    """Prose for the summary MR note, without the trailing JSON fence."""
    match = _JSON_FENCE_RE.search(review_md)
    if not match:
        return review_md.strip()
    return (review_md[: match.start()] + review_md[match.end() :]).strip()


def parse_inline_comments(review_md: str) -> list[InlineComment]:
    payload = _extract_json(review_md)
    if payload is None:
        return []
    raw = payload.get("comments")
    if not isinstance(raw, list):
        return []
    comments: list[InlineComment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = _clean_path(str(item.get("path") or item.get("file") or ""))
        body = str(item.get("body") or item.get("message") or "").strip()
        side = str(item.get("side") or "new").strip().lower()
        if side in {"right", "added", "+"}:
            side = "new"
        elif side in {"left", "deleted", "-"}:
            side = "old"
        if side not in {"new", "old"}:
            side = "new"
        try:
            line = int(item.get("line") or item.get("new_line") or 0)
        except (TypeError, ValueError):
            continue
        if not path or not body or line < 1:
            continue
        comments.append(InlineComment(path=path, line=line, side=side, body=body))
        if len(comments) >= MAX_INLINE_COMMENTS:
            break
    return comments


def parse_diff_lines(patch: str) -> list[DiffLine]:
    lines: list[DiffLine] = []
    old_path = ""
    new_path = ""
    old_line = 0
    new_line = 0
    in_hunk = False
    for raw in patch.splitlines():
        if raw.startswith("diff --git "):
            in_hunk = False
            continue
        if raw.startswith("--- "):
            old_path = _clean_path(raw[4:])
            in_hunk = False
            continue
        if raw.startswith("+++ "):
            new_path = _clean_path(raw[4:])
            in_hunk = False
            continue
        hunk = _HUNK_RE.match(raw)
        if hunk:
            old_line = int(hunk.group(1))
            new_line = int(hunk.group(2))
            in_hunk = True
            continue
        if not in_hunk or not new_path or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            lines.append(
                DiffLine(new_path, old_path or new_path, None, new_line, "add")
            )
            new_line += 1
        elif raw.startswith("-"):
            lines.append(
                DiffLine(new_path, old_path or new_path, old_line, None, "del")
            )
            old_line += 1
        elif raw.startswith(" "):
            lines.append(
                DiffLine(new_path, old_path or new_path, old_line, new_line, "context")
            )
            old_line += 1
            new_line += 1
    return lines


def resolve_comment(comment: InlineComment, diff_lines: list[DiffLine]) -> DiffLine | None:
    """Pick a diff line GitLab will accept for this comment."""
    file_lines = [
        row
        for row in diff_lines
        if row.path == comment.path or row.old_path == comment.path
    ]
    if not file_lines:
        return None
    if comment.side == "old":
        exact = [
            row
            for row in file_lines
            if row.old_line == comment.line and row.kind in {"del", "context"}
        ]
        if exact:
            return exact[0]
        deleted = [row for row in file_lines if row.kind == "del"]
        return _nearest(deleted, comment.line, lambda row: row.old_line)
    exact = [
        row
        for row in file_lines
        if row.new_line == comment.line and row.kind in {"add", "context"}
    ]
    if exact:
        prefer_add = [row for row in exact if row.kind == "add"]
        return prefer_add[0] if prefer_add else exact[0]
    added = [row for row in file_lines if row.kind == "add"]
    return _nearest(added or file_lines, comment.line, lambda row: row.new_line)


def position_for(row: DiffLine) -> dict[str, int]:
    if row.kind == "del" and row.old_line is not None:
        return {"old_line": row.old_line}
    if row.new_line is not None:
        data: dict[str, int] = {"new_line": row.new_line}
        if row.kind == "context" and row.old_line is not None:
            data["old_line"] = row.old_line
        return data
    if row.old_line is not None:
        return {"old_line": row.old_line}
    return {}


def _nearest(
    rows: list[DiffLine],
    target: int,
    getter,
) -> DiffLine | None:
    scored: list[tuple[int, DiffLine]] = []
    for row in rows:
        value = getter(row)
        if value is None:
            continue
        scored.append((abs(value - target), row))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0])
    return scored[0][1]


def _clean_path(value: str) -> str:
    path = value.strip().strip('"')
    if path in {"/dev/null", "dev/null"}:
        return ""
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            path = path[2:]
            break
    return path


def _extract_json(text: str) -> dict | None:
    for match in _JSON_FENCE_RE.finditer(text):
        parsed = _loads_object(match.group(1))
        if parsed is not None:
            return parsed
    start = text.rfind("{")
    while start >= 0:
        parsed = _loads_object(text[start:])
        if parsed is not None:
            return parsed
        start = text.rfind("{", 0, start)
    return None


def _loads_object(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
