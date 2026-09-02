"""Parse ChatGPT review JSON and map comments onto unified-diff lines."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S | re.I)
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_TITLED_BODY_RE = re.compile(
    r"^\*\*(Must fix|Security|Missing test|Compat|Blocker|Action)\*\*",
    re.I,
)
MAX_INLINE_COMMENTS = 12
MAX_INLINE_BODY_CHARS = 700
MAX_SUMMARY_CHARS = 2000
SKIP_SEVERITIES = {
    "nit",
    "nits",
    "praise",
    "lgtm",
    "style",
    "info",
    "note",
    "ok",
    "n/a",
}
SEVERITY_TITLES = {
    "blocker": "Blocker",
    "must-fix": "Must fix",
    "must_fix": "Must fix",
    "high": "Must fix",
    "security": "Security",
    "test": "Missing test",
    "missing-test": "Missing test",
    "compat": "Compat",
    "compatibility": "Compat",
}


@dataclass(frozen=True)
class InlineComment:
    path: str
    line: int
    side: str
    body: str
    severity: str = ""


@dataclass(frozen=True)
class DiffLine:
    path: str
    old_path: str
    old_line: int | None
    new_line: int | None
    kind: str  # add, del, context


def strip_json_block(review_md: str) -> str:
    """Prose for the summary MR thread, without fenced JSON."""
    return _JSON_FENCE_RE.sub("", review_md).strip()


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
        severity = str(item.get("severity") or item.get("sev") or "").strip().lower()
        if severity in SKIP_SEVERITIES:
            continue
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
        comments.append(
            InlineComment(
                path=path, line=line, side=side, body=body, severity=severity
            )
        )
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
            new_path = _clean_path(raw[4:]) or old_path
            in_hunk = False
            continue
        hunk = _HUNK_RE.match(raw)
        if hunk:
            old_line = int(hunk.group(1))
            new_line = int(hunk.group(2))
            in_hunk = True
            continue
        path = new_path or old_path
        if not in_hunk or not path or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            lines.append(DiffLine(path, old_path or path, None, new_line, "add"))
            new_line += 1
        elif raw.startswith("-"):
            lines.append(DiffLine(path, old_path or path, old_line, None, "del"))
            old_line += 1
        elif raw.startswith(" "):
            lines.append(
                DiffLine(path, old_path or path, old_line, new_line, "context")
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


def line_code_for(path: str, old_line: int | None, new_line: int | None) -> str:
    """GitLab line_code: sha1(path)_old_new, with 0 for a missing side."""
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()
    old = 0 if old_line is None else old_line
    new = 0 if new_line is None else new_line
    return f"{digest}_{old}_{new}"


def line_range_for(row: DiffLine) -> dict[str, Any]:
    """Single-line line_range some GitLab versions require for diff threads."""
    code = line_code_for(row.path, row.old_line, row.new_line)
    side = "old" if row.kind == "del" else "new"
    point: dict[str, Any] = {"line_code": code, "type": side}
    if row.old_line is not None:
        point["old_line"] = row.old_line
    if row.new_line is not None:
        point["new_line"] = row.new_line
    return {"start": point, "end": dict(point)}


def discussion_position(
    refs: dict[str, str],
    row: DiffLine,
    *,
    with_line_range: bool = True,
) -> dict[str, Any]:
    position: dict[str, Any] = {
        "base_sha": refs["base_sha"],
        "start_sha": refs["start_sha"],
        "head_sha": refs["head_sha"],
        "old_path": row.old_path or row.path,
        "new_path": row.path,
        "position_type": "text",
        **position_for(row),
    }
    if with_line_range:
        position["line_range"] = line_range_for(row)
    return position


def format_gitlab_comment(
    comment: InlineComment,
    *,
    include_location: bool = False,
) -> str:
    """GitLab-flavored markdown for an inline or overview discussion thread."""
    body = truncate_markdown(comment.body.strip(), MAX_INLINE_BODY_CHARS)
    title = SEVERITY_TITLES.get(comment.severity, "")
    lines: list[str] = []
    if include_location:
        loc = f"`{comment.path}:{comment.line}`"
        header = f"**{title}** · {loc}" if title else f"**{loc}**"
        lines.append(header)
        lines.append("")
    elif title and not _TITLED_BODY_RE.match(body):
        lines.append(f"**{title}**")
        lines.append("")
    lines.append(body)
    return "\n".join(lines).strip()


def format_gitlab_summary(
    summary: str,
    *,
    inline_count: int = 0,
    overview_count: int = 0,
) -> str:
    text = truncate_markdown(summary.strip(), MAX_SUMMARY_CHARS)
    parts = ["### AAOS system-app review", "", text]
    bits: list[str] = []
    if inline_count:
        bits.append(f"{inline_count} inline thread(s) on the **Changes** tab")
    if overview_count:
        bits.append(
            f"{overview_count} overview thread(s) (diff line could not be mapped)"
        )
    if bits:
        parts.extend(
            [
                "",
                "---",
                "_" + "; ".join(bits) + ". Reply on a thread to discuss._",
            ]
        )
    return "\n".join(parts)


def truncate_markdown(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    if len(cut) < limit // 2:
        cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip() + "\n…"


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
    matches = list(_JSON_FENCE_RE.finditer(text))
    for match in reversed(matches):
        parsed = _loads_object(match.group(1))
        if parsed is not None and isinstance(parsed.get("comments"), list):
            return parsed
    for match in reversed(matches):
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
