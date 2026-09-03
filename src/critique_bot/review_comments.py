"""Parse ChatGPT review JSON and map comments onto unified-diff lines."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_JSON_LANG_FENCE_RE = re.compile(
    r"```(?:json|javascript|js)\b[^\n]*\r?\n?(.*?)```", re.S | re.I
)
_UNLABELED_FENCE_RE = re.compile(r"```[ \t]*\r?\n(.*?)```", re.S)
_FENCE_PREFIX_RE = re.compile(
    r"```(?:json|javascript|js)\b[^\n]*\n?\s*$|```[ \t]*\n?\s*$", re.I
)
_FENCE_SUFFIX_RE = re.compile(r"\s*```[ \t]*")
_NO_FINDINGS_RE = re.compile(r"no actionable findings", re.I)
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_BARE_FILE_RE = re.compile(
    r"(?<![`\w/])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z][A-Za-z0-9]*)(?::(?:L)?(\d+))?"
)
_PATH_LINE_SUFFIX_RE = re.compile(r"^(?P<path>.*?)(?::(?:L)?(?P<line>\d+))$")
_COMMENT_LIST_KEYS = (
    "comments",
    "inline_comments",
    "inlineComments",
    "findings",
    "review_comments",
    "reviewComments",
    "issues",
    "inline",
)
_PATH_KEYS = ("path", "file", "file_path", "filename", "filepath", "new_path")
_LINE_KEYS = ("line", "new_line", "old_line", "line_number", "lineno", "start_line")
_BODY_KEYS = ("body", "message", "comment", "text", "content")
_IMPACT_KEYS = ("impact", "if_unfixed", "consequence", "why_it_matters", "effect")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_TITLED_BODY_RE = re.compile(
    r"^\*\*(Must fix|Security|Missing test|Compat|Blocker|Action|Impact)\*\*",
    re.I,
)
_IMPACT_BODY_RE = re.compile(r"\*\*Impact:\*\*", re.I)
_INLINE_ITEM_RE = re.compile(
    r"(?:(?<=^)|(?<=\s))(?:\*{1,2})?(\d{1,2})(?:\*{1,2})?[.)]\s+"
)
_BULLET_ITEM_RE = re.compile(r"(?:(?<=^)|(?<=\s))[-*•]\s+")
_NUMBERED_ITEM_WINDOW = 600
_ACTIONS_HEADER_RE = re.compile(
    r"\*{0,2}\s*\d+\s+actions?\s*\*{0,2}",
    re.I,
)
_LINE_NUMBER_RE = re.compile(r"(\d+)")
MAX_INLINE_COMMENTS = 12
MAX_INLINE_BODY_CHARS = 700
MAX_INLINE_IMPACT_CHARS = 220
MAX_SUMMARY_CHARS = 2000
MAX_SUMMARY_BLURB_CHARS = 720
MAX_SUMMARY_BLURB_LINES = 4
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
    impact: str = ""


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
        for start, end, obj in _iter_repaired_json_objects(text)
        if _is_review_payload(obj)
    ]
    for start, end in reversed(spans):
        lo, hi = _expand_fence(text, start, end)
        text = text[:lo] + text[hi:]
    text = _JSON_LANG_FENCE_RE.sub("", text)
    text = _UNLABELED_FENCE_RE.sub(_drop_json_unlabeled_fence, text)
    if _still_has_review_json(text):
        text = _strip_broken_review_json(text)
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
    if match is None:
        prose = strip_json_block(review_md)
        inline = re.search(r"\*{0,2}Risk:\s*([^*\n]+)\*{0,2}", prose, re.I)
        if inline is not None and inline.start() < 40:
            match = inline
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
    raw = _comment_list(payload) if payload is not None else None
    if not isinstance(raw, list):
        raw = _recover_comment_objects(review_md)
    return _comments_from_items(raw)


def comments_from_summary(
    review_md: str,
    diff_lines: list[DiffLine] | None = None,
) -> list[InlineComment]:
    """Build inline comments from numbered summary actions when JSON is empty."""
    source = _summary_source(review_md)
    if not source or _NO_FINDINGS_RE.search(source):
        return []
    items = _summary_action_items(source)
    if not items:
        return []
    rows = diff_lines or []
    comments: list[InlineComment] = []
    used_lines: dict[str, set[int]] = {}
    for item in items:
        comment = _comment_from_action(item, rows, used_lines)
        if comment is None:
            continue
        comments.append(comment)
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
    """GitLab-flavored markdown for an inline discussion thread."""
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
    impact = truncate_markdown((comment.impact or "").strip(), MAX_INLINE_IMPACT_CHARS)
    if impact and not _IMPACT_BODY_RE.search(body):
        lines.append("")
        if _IMPACT_BODY_RE.match(impact):
            lines.append(impact)
        else:
            lines.append(f"**Impact:** {impact}")
    return "\n".join(lines).strip()


def summary_blurb(summary: str) -> str:
    """2–4 line MR status with risk and per-file action lists removed."""
    working = _strip_risk_and_actions_header(summary)
    if not working:
        return ""
    if _NO_FINDINGS_RE.search(working):
        _, items = _extract_numbered_items(working)
        if not items:
            _, items = _extract_bullet_items(working)
        if not items:
            return "No actionable findings."
    preamble, items = _extract_numbered_items(working)
    if not items:
        preamble, items = _extract_bullet_items(working)
    if items:
        return _truncate_blurb(preamble)
    mention_items = _items_from_file_mentions(working)
    if mention_items:
        others = [
            part.strip().rstrip(";").strip()
            for part in re.split(r"(?<=[.!?])\s+", working)
            if part.strip() and not _iter_file_mentions(part)
        ]
        return _truncate_blurb(" ".join(others))
    return _truncate_blurb(working)
    working = _strip_risk_and_actions_header(summary)
    if not working:
        return ""
    if _NO_FINDINGS_RE.search(working):
        _, items = _extract_numbered_items(working)
        if not items:
            _, items = _extract_bullet_items(working)
        if not items:
            return "No actionable findings."
    preamble, items = _extract_numbered_items(working)
    if not items:
        preamble, items = _extract_bullet_items(working)
    if items:
        return _truncate_blurb(preamble)
    mention_items = _items_from_file_mentions(working)
    if mention_items:
        others = [
            part.strip().rstrip(";").strip()
            for part in re.split(r"(?<=[.!?])\s+", working)
            if part.strip() and not _iter_file_mentions(part)
        ]
        return _truncate_blurb(" ".join(others))
    return _truncate_blurb(working)


def orphan_summary_actions(
    review_md: str,
    comments: list[InlineComment],
) -> list[str]:
    """Summary actions that never became comments (no file/line to pin).

    When JSON comments exist they are the source of truth, so the numbered
    summary list is treated as a duplicate and dropped.
    """
    if parse_inline_comments(review_md):
        return []
    source = _summary_source(review_md)
    if not source or _NO_FINDINGS_RE.search(source):
        return []
    items = _summary_action_items(source, file_mentions=False)
    if not items:
        return []
    taken = {_folded(comment.body) for comment in comments}
    orphans: list[str] = []
    for item in items:
        key = _folded(item)
        if key in taken:
            continue
        if any(key in taken_body or taken_body in key for taken_body in taken if taken_body):
            continue
        orphans.append(item)
    return orphans


def format_gitlab_summary(
    summary: str,
    *,
    inline_count: int = 0,
    unmapped: list[InlineComment] | None = None,
    orphan_actions: list[str] | None = None,
    risk: str = "",
) -> str:
    """MR overview: risk, 2–4 line status, then only unpinned findings."""
    blurb = summary_blurb(summary)
    pinned: list[str] = []
    for comment in unmapped or []:
        item = format_gitlab_comment(comment, include_location=True)
        if item:
            pinned.append(item)
    for action in orphan_actions or []:
        text = action.strip()
        if text:
            pinned.append(text)
    parts = ["### AAOS system-app review", ""]
    canonical = _canonical_risk(risk) or risk
    if canonical:
        label = RISK_LABELS.get(canonical, canonical.replace("-", " ").title())
        emoji = RISK_EMOJI.get(canonical, "")
        heading = f"**Risk: {label}**"
        if emoji:
            heading = f"{emoji} {heading}"
        parts.extend([heading, ""])
    if blurb:
        parts.append(blurb)
    if pinned:
        if blurb:
            parts.append("")
        parts.append("Could not pin these to the diff:")
        parts.append("")
        parts.append("\n\n".join(pinned))
    bits: list[str] = []
    if inline_count:
        bits.append(f"{inline_count} inline thread(s) on the **Changes** tab")
    if bits:
        parts.extend(
            [
                "",
                "---",
                "_" + "; ".join(bits) + ". Reply on a thread to discuss._",
            ]
        )
    return apply_gitlab_line_breaks(
        truncate_markdown("\n".join(parts).strip(), MAX_SUMMARY_CHARS)
    )


def apply_gitlab_line_breaks(text: str) -> str:
    """Keep GitLab from collapsing consecutive lines into one paragraph.

    GitLab Flavored Markdown treats a single newline as a space unless the
    line ends with two spaces (a hard break) or a blank line starts a new
    block. Unmapped findings listed on the summary must survive that.
    """
    lines = (text or "").split("\n")
    out: list[str] = []
    for line in lines:
        core = line.rstrip()
        if not core:
            out.append("")
            continue
        if core.endswith("  "):
            out.append(core)
            continue
        out.append(core + "  ")
    return "\n".join(out)


def normalize_summary_markdown(text: str) -> str:
    """Turn a one-paragraph model summary into GitLab-renderable markdown."""
    raw = (text or "").strip()
    if not raw:
        return raw
    parts: list[str] = []
    working = raw
    risk_match = _RISK_LINE_RE.search(working)
    if risk_match is None:
        inline = re.search(r"\*{0,2}Risk:\s*([^*\n]+)\*{0,2}", working, re.I)
        if inline is not None and inline.start() < 40:
            risk_match = inline
    if risk_match:
        label_raw = risk_match.group(1).strip().strip("*").strip()
        canonical = _canonical_risk(label_raw)
        label = RISK_LABELS.get(canonical, label_raw)
        parts.append(f"**Risk: {label}**")
        working = (working[: risk_match.start()] + working[risk_match.end() :]).strip()
    header_match = _ACTIONS_HEADER_RE.search(working)
    if header_match and header_match.start() <= 24:
        header = header_match.group(0).strip().strip("*").strip()
        parts.append(f"**{header}**")
        working = (
            working[: header_match.start()] + working[header_match.end() :]
        ).strip()
    preamble, items = _extract_numbered_items(working)
    if not items:
        preamble, items = _extract_bullet_items(working)
    if not items:
        mention_items = _items_from_file_mentions(working)
        if mention_items:
            preamble = ""
            items = mention_items
    if preamble:
        parts.append(preamble)
    if items:
        parts.append(
            "\n".join(f"{i}. {item}" for i, item in enumerate(items, start=1))
        )
    return "\n\n".join(part for part in parts if part).strip()


def truncate_markdown(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    if len(cut) < limit // 2:
        cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip() + "\n…"


def _summary_source(review_md: str) -> str:
    summary = strip_json_block(review_md)
    return normalize_summary_markdown(summary) or summary


def _summary_action_items(source: str, *, file_mentions: bool = True) -> list[str]:
    _, items = _extract_numbered_items(source)
    if not items:
        _, items = _extract_bullet_items(source)
    if not items and file_mentions:
        items = _items_from_file_mentions(source)
    return items


def _strip_risk_and_actions_header(text: str) -> str:
    working = (text or "").strip()
    if not working:
        return ""
    risk_match = _RISK_LINE_RE.search(working)
    if risk_match is None:
        inline = re.search(r"\*{0,2}Risk:\s*([^*\n]+)\*{0,2}", working, re.I)
        if inline is not None and inline.start() < 40:
            risk_match = inline
    if risk_match:
        working = (working[: risk_match.start()] + working[risk_match.end() :]).strip()
    header_match = _ACTIONS_HEADER_RE.search(working)
    if header_match and header_match.start() <= 24:
        working = (
            working[: header_match.start()] + working[header_match.end() :]
        ).strip()
    return working


def _truncate_blurb(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
    lines = [line for line in lines if line]
    if len(lines) == 1:
        sentences = [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+", lines[0])
            if part.strip()
        ]
        if len(sentences) > 1:
            lines = sentences
    lines = lines[:MAX_SUMMARY_BLURB_LINES]
    return truncate_markdown("\n".join(lines), MAX_SUMMARY_BLURB_CHARS)


def _folded(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


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


def _comments_from_items(raw: list | None) -> list[InlineComment]:
    comments: list[InlineComment] = []
    if not isinstance(raw, list):
        return comments
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = _comment_path(item)
        path, path_line = _split_path_line(path)
        body = _comment_body(item)
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
        line = _comment_line(item, side)
        if line < 1:
            line = path_line
        if not path or not body:
            continue
        if line < 1:
            line = 1
        comments.append(
            InlineComment(
                path=path,
                line=line,
                side=side,
                body=body,
                severity=severity,
                impact=_comment_impact(item),
            )
        )
        if len(comments) >= MAX_INLINE_COMMENTS:
            break
    return comments


def _comment_body(item: dict) -> str:
    for key in _BODY_KEYS:
        value = item.get(key)
        if isinstance(value, dict):
            nested = _comment_body(value)
            if nested:
                return nested
            continue
        if value:
            return str(value).strip()
    return ""


def _comment_impact(item: dict) -> str:
    for key in _IMPACT_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _comment_from_action(
    item: str,
    diff_lines: list[DiffLine],
    used_lines: dict[str, set[int]],
) -> InlineComment | None:
    mentions = _iter_file_mentions(item)
    if not mentions:
        return None
    severity = _severity_from_text(item)
    for path, line in mentions:
        probe = InlineComment(path=path, line=line or 1, side="new", body=item)
        rows = _file_rows(probe, diff_lines) if diff_lines else []
        if rows:
            resolved = rows[0].path
            if line < 1:
                line = _pick_diff_line(rows, used_lines.get(resolved, set()))
            if line < 1:
                continue
            used_lines.setdefault(resolved, set()).add(line)
            return InlineComment(
                path=resolved, line=line, side="new", body=item, severity=severity
            )
        if line >= 1:
            used_lines.setdefault(path, set()).add(line)
            return InlineComment(
                path=path, line=line, side="new", body=item, severity=severity
            )
    return None


def _iter_file_mentions(text: str) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    covered: list[tuple[int, int]] = []
    for match in _BACKTICK_RE.finditer(text):
        path, line = _split_path_line(match.group(1).strip())
        if path and _looks_like_path(path):
            found.append((path, line))
            covered.append(match.span())
    for match in _BARE_FILE_RE.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in covered):
            continue
        path = match.group(1)
        line = int(match.group(2)) if match.group(2) else 0
        if _looks_like_path(path):
            found.append((path, line))
    return found


def _split_path_line(value: str) -> tuple[str, int]:
    path = _clean_path(value)
    match = _PATH_LINE_SUFFIX_RE.match(path)
    if not match:
        return path, 0
    return match.group("path"), int(match.group("line"))


def _looks_like_path(value: str) -> bool:
    text = value.replace("\\", "/").strip().strip("`")
    if not text or text in {".", ".."}:
        return False
    lowered = text.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return False
    name = text.rsplit("/", 1)[-1]
    if "/" in text:
        return True
    if "." not in name or name.startswith("."):
        return False
    ext = name.rsplit(".", 1)[-1]
    return bool(ext) and ext.isalnum() and len(ext) <= 10


def _pick_diff_line(rows: list[DiffLine], used: set[int]) -> int:
    added = [row.new_line for row in rows if row.kind == "add" and row.new_line]
    for value in added:
        if value not in used:
            return value
    if added:
        return added[0]
    for row in rows:
        value = row.new_line or row.old_line
        if value and value not in used:
            return value
    return (rows[0].new_line or rows[0].old_line or 0) if rows else 0


def _severity_from_text(text: str) -> str:
    lower = (text or "").lower()
    if any(
        word in lower
        for word in ("blocker", "privilege escalation", "uxr", "distraction")
    ):
        return "blocker"
    if any(
        word in lower
        for word in ("security", "binder", "exported", "permission", "privapp")
    ):
        return "security"
    if "test" in lower:
        return "test"
    if any(word in lower for word in ("compat", "sdk_int", "api 3", "android 1")):
        return "compat"
    return "must-fix"


def _comment_list(payload: dict) -> list | None:
    for key in _COMMENT_LIST_KEYS:
        items = _coerce_comment_list(payload.get(key))
        if items is not None:
            return items
    if _comment_path(payload) and any(payload.get(key) for key in _BODY_KEYS):
        return [payload]
    return None


def _coerce_comment_list(value: object, default_path: str = "") -> list | None:
    if isinstance(value, str) and value.strip():
        parsed = _loads_loose(value)
        if parsed is None:
            return None
        return _coerce_comment_list(parsed, default_path)
    if isinstance(value, list):
        items: list = []
        for item in value:
            if isinstance(item, dict):
                if default_path and not _comment_path(item):
                    item = {**item, "path": default_path}
                items.append(item)
            elif isinstance(item, str) and default_path:
                items.append({"path": default_path, "body": item})
        return items
    if isinstance(value, dict):
        looks_like_comment = _comment_path(value) or any(
            value.get(key) for key in (*_BODY_KEYS, *_LINE_KEYS)
        )
        if looks_like_comment and not any(key in value for key in _COMMENT_LIST_KEYS):
            if default_path and not _comment_path(value):
                value = {**value, "path": default_path}
            return [value]
        items = []
        for key, nested in value.items():
            path = default_path
            if _looks_like_path(str(key)):
                path, _line = _split_path_line(_clean_path(str(key)))
            coerced = _coerce_comment_list(nested, path)
            if coerced:
                items.extend(coerced)
            elif isinstance(nested, str) and path:
                items.append({"path": path, "body": nested})
        return items or None
    return None


def _comment_path(item: dict) -> str:
    for key in _PATH_KEYS:
        value = item.get(key)
        if value:
            return _clean_path(str(value))
    return ""


def _comment_line(item: dict, side: str) -> int:
    keys = _LINE_KEYS
    if side == "old":
        keys = ("old_line", "line", "line_number", "lineno", "start_line", "new_line")
    for key in keys:
        parsed = _parse_line_value(item.get(key))
        if parsed >= 1:
            return parsed
    for nested_key in ("position", "start", "end"):
        nested = item.get(nested_key)
        if isinstance(nested, dict):
            parsed = _comment_line(nested, side)
            if parsed >= 1:
                return parsed
    return 0


def _parse_line_value(value: object) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    match = _LINE_NUMBER_RE.search(str(value).strip())
    if not match:
        return 0
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return 0


def _extract_numbered_items(text: str) -> tuple[str, list[str]]:
    text = text.strip()
    if not text:
        return "", []
    found = list(_INLINE_ITEM_RE.finditer(text))
    start = next((match for match in found if match.group(1) == "1"), None)
    if start is None or start.start() > _NUMBERED_ITEM_WINDOW:
        return text, []
    chosen: list[re.Match[str]] = []
    wanted = 1
    for match in found:
        if match.start() < start.start():
            continue
        number = int(match.group(1))
        if number == wanted:
            chosen.append(match)
            wanted += 1
    if not chosen:
        return text, []
    return text[: chosen[0].start()].strip(), _slice_items(text, chosen)


def _extract_bullet_items(text: str) -> tuple[str, list[str]]:
    text = text.strip()
    if not text:
        return "", []
    found = list(_BULLET_ITEM_RE.finditer(text))
    if not found:
        return text, []
    if found[0].start() > 80 and "\n" not in text[: found[0].start()]:
        return text, []
    items = _slice_items(text, found)
    if not items:
        return text, []
    return text[: found[0].start()].strip(), items


def _items_from_file_mentions(text: str) -> list[str]:
    text = text.strip()
    if not text or not _iter_file_mentions(text):
        return []
    sentences = [
        part.strip().rstrip(";").strip()
        for part in re.split(r"(?<=[.!?])\s+", text)
        if part.strip()
    ]
    items = [sent for sent in sentences if _iter_file_mentions(sent)]
    return items or [text]


def _slice_items(text: str, matches: list[re.Match[str]]) -> list[str]:
    items: list[str] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        item = text[start:end].strip().rstrip(";").strip()
        item = re.sub(r"[ \t]*\n[ \t]*", " ", item).strip()
        if item:
            items.append(item)
    return items


def _is_review_payload(obj: object) -> bool:
    payload = _as_payload(obj)
    if payload is None:
        return False
    if _comment_list(payload) is not None:
        return True
    return "risk" in payload


def _next_json_start(text: str, start: int) -> int:
    brace = text.find("{", start)
    bracket = text.find("[", start)
    while bracket >= 0:
        rest = text[bracket + 1 : bracket + 12].lstrip()
        if rest[:1] in {"{", '"'}:
            break
        bracket = text.find("[", bracket + 1)
    starts = [pos for pos in (brace, bracket) if pos >= 0]
    return min(starts) if starts else -1


def _iter_json_objects(text: str):
    yield from _iter_repaired_json_objects(text)


def _looks_like_json_slice(blob: str) -> bool:
    return any(
        f'"{key}"' in blob
        for key in (
            "comments",
            "inline_comments",
            "findings",
            "path",
            "file",
            "risk",
            "body",
            "line",
        )
    )


def _slice_json_value(text: str, start: int) -> tuple[str, int] | None:
    if start >= len(text) or text[start] not in "{[":
        return None
    in_string = False
    escape = False
    stack: list[str] = []
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
                if not stack:
                    return text[start : i + 1], i + 1
    return text[start:], len(text)


def _iter_repaired_json_objects(text: str):
    i = 0
    n = len(text)
    while i < n:
        brace = _next_json_start(text, i)
        if brace < 0:
            return
        sliced = _slice_json_value(text, brace)
        if sliced is None:
            i = brace + 1
            continue
        blob, end = sliced
        if not _looks_like_json_slice(blob):
            i = brace + 1
            continue
        obj = _loads_loose(blob)
        if isinstance(obj, (dict, list)):
            yield brace, end, obj
            i = max(end, brace + 1)
            continue
        i = brace + 1


def _recover_comment_objects(text: str) -> list[dict]:
    items: list[dict] = []
    i = 0
    while i < len(text):
        brace = text.find("{", i)
        if brace < 0:
            break
        sliced = _slice_json_value(text, brace)
        if sliced is None:
            i = brace + 1
            continue
        blob, end = sliced
        obj = _loads_loose(blob)
        if (
            isinstance(obj, dict)
            and _comment_path(obj)
            and _comment_body(obj)
            and not any(key in obj for key in _COMMENT_LIST_KEYS)
        ):
            items.append(obj)
            i = end
            continue
        i = brace + 1 if obj is None else max(end, brace + 1)
    return items


def _drop_json_unlabeled_fence(match: re.Match[str]) -> str:
    blob = match.group(1).strip()
    if blob.startswith("{") or blob.startswith("["):
        obj = _loads_loose(match.group(1))
        if _is_review_payload(obj):
            return ""
    return match.group(0)


def _still_has_review_json(text: str) -> bool:
    lower = (text or "").lower()
    if "```json" in lower:
        return True
    return any(
        f'"{key}"' in text
        for key in ("comments", "inline_comments", "findings", "review_comments")
    )


def _strip_broken_review_json(text: str) -> str:
    markers = ('"comments"', '"inline_comments"', '"findings"', '"review_comments"', '"risk"')
    best = -1
    for marker in markers:
        idx = text.rfind(marker)
        if idx > best:
            best = idx
    if best < 0:
        return text
    brace = text.rfind("{", 0, best)
    if brace < 0:
        brace = text.rfind("[", 0, best)
    if brace < 0:
        return text
    sliced = _slice_json_value(text, brace)
    if sliced is None:
        return text[:brace].rstrip()
    _blob, end = sliced
    lo, hi = _expand_fence(text, brace, end)
    return text[:lo] + text[hi:]


def _expand_fence(text: str, start: int, end: int) -> tuple[int, int]:
    prefix = text[:start]
    match = None
    for found in _FENCE_PREFIX_RE.finditer(prefix):
        match = found
    lo = match.start() if match else start
    suffix = _FENCE_SUFFIX_RE.match(text[end:])
    hi = end + suffix.end() if suffix else end
    return lo, hi


def _as_payload(obj: object) -> dict | None:
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list) and obj and all(isinstance(item, dict) for item in obj):
        return {"comments": obj}
    return None


def _loads_loose(text: str) -> object | None:
    blob = (text or "").strip().lstrip("\ufeff").replace("\u200b", "")
    if not blob:
        return None
    decoder = json.JSONDecoder()
    for candidate in (blob, _repair_json_text(blob)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        try:
            obj, _end = decoder.raw_decode(candidate)
            return obj
        except json.JSONDecodeError:
            continue
    return None


def _repair_json_text(text: str) -> str:
    blob = text.strip().lstrip("\ufeff")
    blob = blob.translate(
        str.maketrans(
            {
                "\u201c": '"',
                "\u201d": '"',
                "\u2018": "'",
                "\u2019": "'",
            }
        )
    )
    blob = _escape_newlines_in_json_strings(blob)
    blob = re.sub(r",\s*([}\]])", r"\1", blob)
    blob = re.sub(r"\bTrue\b", "true", blob)
    blob = re.sub(r"\bFalse\b", "false", blob)
    blob = re.sub(r"\bNone\b", "null", blob)
    return _close_truncated_json(blob)


def _escape_newlines_in_json_strings(text: str) -> str:
    out: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                out.append(ch)
                escape = False
                continue
            if ch == "\\":
                out.append(ch)
                escape = True
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


def _close_truncated_json(text: str) -> str:
    in_string = False
    escape = False
    stack: list[str] = []
    for ch in text:
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    if in_string:
        text += '"'
    if not stack:
        return text
    return text.rstrip().rstrip(",") + "".join(reversed(stack))


def _json_candidate_blobs(text: str) -> list[str]:
    blobs = [match.group(1) for match in _JSON_LANG_FENCE_RE.finditer(text)]
    for match in _UNLABELED_FENCE_RE.finditer(text):
        blob = match.group(1).strip()
        if blob.startswith("{") or blob.startswith("["):
            blobs.append(match.group(1))
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        blobs.append(stripped)
    return blobs


def _extract_json(text: str) -> dict | None:
    last = None
    review = None
    seen: list[dict] = []
    for blob in _json_candidate_blobs(text):
        payload = _as_payload(_loads_loose(blob))
        if payload is not None:
            seen.append(payload)
    for _, _, obj in _iter_repaired_json_objects(text):
        payload = _as_payload(obj)
        if payload is not None:
            seen.append(payload)
    for payload in seen:
        last = payload
        if _is_review_payload(payload) or _comment_path(payload):
            review = payload
    if review is not None and any(
        isinstance(review.get(key), list) for key in _COMMENT_LIST_KEYS
    ):
        return review
    recovered = _recover_comment_objects(text)
    if recovered:
        return {"comments": recovered}
    return review or last
