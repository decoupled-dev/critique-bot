"""Parse ChatGPT review JSON and map comments onto unified-diff lines."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S | re.I)
_FENCE_PREFIX_RE = re.compile(r"```(?:json)?[^\n]*\n?\s*$", re.I)
_FENCE_SUFFIX_RE = re.compile(r"\s*```[ \t]*")
_COMMENT_LIST_KEYS = ("comments", "inline_comments", "findings", "review_comments")
_BODY_KEYS = ("body", "message", "comment", "text", "content")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_TITLED_BODY_RE = re.compile(
    r"^\*\*(Must fix|Security|Missing test|Compat|Blocker|Action)\*\*",
    re.I,
)
MAX_INLINE_COMMENTS = 12
MAX_INLINE_BODY_CHARS = 700
MAX_SUMMARY_CHARS = 2000
_RISK_LINE_RE = re.compile(
    r"^\s*\*?\*?Risk:\s*([^*\n]+)\*?\*?\s*$",
    re.I | re.M,
)
RISK_LABELS = {
    "safe": "Safe",
    "moderate": "Moderate risk",
    "risky": "Risky",
    "blocker": "Blocker",
}
RISK_CANONICAL = {
    "safe": "safe",
    "low": "safe",
    "ok": "safe",
    "moderate": "moderate",
    "moderate risk": "moderate",
    "moderate-risk": "moderate",
    "medium": "moderate",
    "risky": "risky",
    "high": "risky",
    "blocker": "blocker",
    "critical": "blocker",
}
RISK_EMOJI = {
    "safe": ":white_check_mark:",
    "moderate": ":large_yellow_circle:",
    "risky": ":red_circle:",
    "blocker": ":no_entry:",
}
_SEVERITY_RISK = {
    "blocker": "blocker",
    "security": "risky",
    "must-fix": "risky",
    "must_fix": "risky",
    "high": "risky",
    "test": "moderate",
    "missing-test": "moderate",
    "compat": "moderate",
    "compatibility": "moderate",
}
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
    """Prose for the summary MR thread, without review JSON (fenced or raw)."""
    text = review_md
    spans = [
        (start, end)
        for start, end, obj in _iter_json_objects(text)
        if _is_review_payload(obj)
    ]
    for start, end in reversed(spans):
        lo, hi = _expand_fence(text, start, end)
        text = text[:lo] + text[hi:]
    text = _JSON_FENCE_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_review_risk(review_md: str) -> str:
    """Canonical risk: safe, moderate, risky, or blocker."""
    payload = _extract_json(review_md)
    if payload is not None:
        raw = str(
            payload.get("risk")
            or payload.get("level")
            or payload.get("verdict")
            or ""
        )
        canonical = _canonical_risk(raw)
        if canonical:
            return canonical
    match = _RISK_LINE_RE.search(review_md)
    if match:
        canonical = _canonical_risk(match.group(1))
        if canonical:
            return canonical
    return infer_risk_from_comments(parse_inline_comments(review_md))


def infer_risk_from_comments(comments: list[InlineComment]) -> str:
    if not comments:
        return "safe"
    rank = {"safe": 0, "moderate": 1, "risky": 2, "blocker": 3}
    worst = "safe"
    for comment in comments:
        level = _SEVERITY_RISK.get(comment.severity, "moderate")
        if rank[level] > rank[worst]:
            worst = level
    return worst


def _canonical_risk(raw: str) -> str:
    key = re.sub(r"\s+", " ", (raw or "").strip().lower())
    key = key.replace("_", "-").strip(" :.-")
    return RISK_CANONICAL.get(key, "")


def parse_inline_comments(review_md: str) -> list[InlineComment]:
    payload = _extract_json(review_md)
    if payload is None:
        return []
    raw: Any = None
    for key in _COMMENT_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            raw = value
            break
    if not isinstance(raw, list):
        return []
    comments: list[InlineComment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = _clean_path(str(item.get("path") or item.get("file") or ""))
        body = ""
        for key in _BODY_KEYS:
            value = item.get(key)
            if value:
                body = str(value).strip()
                break
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
            line = int(
                item.get("line") or item.get("new_line") or item.get("old_line") or 0
            )
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
            lines.append(DiffLine(path, old_path or path, old_line, new_line, "add"))
            new_line += 1
        elif raw.startswith("-"):
            lines.append(DiffLine(path, old_path or path, old_line, new_line, "del"))
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
    file_lines = _file_rows(comment, diff_lines)
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
    """GitLab line_code: sha1(path)_old_new.

    Added line N is ``{sha}_{N}_{N}`` (GitLab docs). Missing sides become 0.
    """
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()
    if old_line is None and new_line is not None:
        old_line = new_line
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
    with_line_range: bool = False,
) -> dict[str, Any]:
    """Position object GitLab accepts on POST .../discussions.

    Verified payload: body + position with base_sha, start_sha, head_sha,
    old_path, new_path, position_type, and new_line (or old_line).
    """
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
    risk: str = "",
) -> str:
    text = truncate_markdown(summary.strip(), MAX_SUMMARY_CHARS)
    if risk:
        text = _RISK_LINE_RE.sub("", text, count=1).strip()
    parts = ["### AAOS system-app review", ""]
    canonical = _canonical_risk(risk) or risk
    if canonical:
        label = RISK_LABELS.get(canonical, canonical.replace("-", " ").title())
        emoji = RISK_EMOJI.get(canonical, "")
        heading = f"**Risk: {label}**"
        if emoji:
            heading = f"{emoji} {heading}"
        parts.extend([heading, ""])
    parts.append(text)
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


def _file_rows(comment: InlineComment, diff_lines: list[DiffLine]) -> list[DiffLine]:
    wanted = comment.path.replace("\\", "/").lstrip("./")
    matched = [row for row in diff_lines if _path_matches(wanted, row)]
    if matched:
        return matched
    name = wanted.rsplit("/", 1)[-1]
    if not name:
        return []
    named = [
        row
        for row in diff_lines
        if row.path.rsplit("/", 1)[-1] == name
        or row.old_path.rsplit("/", 1)[-1] == name
    ]
    paths = {row.path for row in named}
    return named if len(paths) == 1 else []


def _path_matches(wanted: str, row: DiffLine) -> bool:
    for candidate in (row.path, row.old_path):
        path = candidate.replace("\\", "/").lstrip("./")
        if path == wanted:
            return True
        if wanted and path.endswith("/" + wanted):
            return True
        if path and wanted.endswith("/" + path):
            return True
    return False


def _is_review_payload(obj: dict) -> bool:
    if any(isinstance(obj.get(key), list) for key in _COMMENT_LIST_KEYS):
        return True
    return "risk" in obj


def _iter_json_objects(text: str):
    decoder = json.JSONDecoder()
    i = 0
    n = len(text)
    while i < n:
        brace = text.find("{", i)
        if brace < 0:
            return
        try:
            obj, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            i = brace + 1
            continue
        if isinstance(obj, dict):
            yield brace, end, obj
        i = max(end, brace + 1)


def _expand_fence(text: str, start: int, end: int) -> tuple[int, int]:
    prefix = text[:start]
    match = None
    for found in _FENCE_PREFIX_RE.finditer(prefix):
        match = found
    lo = match.start() if match else start
    suffix = _FENCE_SUFFIX_RE.match(text[end:])
    hi = end + suffix.end() if suffix else end
    return lo, hi


def _extract_json(text: str) -> dict | None:
    last = None
    review = None
    for _, _, obj in _iter_json_objects(text):
        last = obj
        if any(isinstance(obj.get(key), list) for key in _COMMENT_LIST_KEYS):
            review = obj
    return review or last
