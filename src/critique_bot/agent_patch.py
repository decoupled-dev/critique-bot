# SPDX-License-Identifier: Apache-2.0
"""Patches without ``git apply``: a parser for unified diffs and V4A patches, and hunk application.

Models trained on patches (gpt-oss and others in the OpenAI family) write one of two
formats:

- a **unified diff** (``--- a/x``, ``+++ b/x``, ``@@ -l,c +l,c @@`` hunks), as git prints;
- a **V4A patch** (``*** Begin Patch`` / ``*** Update File: x`` / ``@@ anchor`` /
  ``*** Add File: x`` / ``*** Delete File: x`` / ``*** Move to: y`` / ``*** End Patch``).

Line numbers in either are ignored: models get them wrong. Each hunk is located by its
context and removed lines through the same match ladder as ``edit_file``
(:func:`critique_bot.agent_edit.replace_span`), searching after the previous hunk first,
so a patch made against a slightly different version of the file still applies, and a
hunk whose context matches more than one place is refused rather than guessed. Applying
is in memory: the caller writes every file only when every hunk of every file matched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from critique_bot import agent_edit


class PatchError(ValueError):
    """The patch cannot be parsed or a hunk does not match. The message is for the model."""

    def __init__(self, message: str, *, candidate: str = "", path: str = "") -> None:
        super().__init__(message)
        self.candidate = candidate
        self.path = path


@dataclass
class Hunk:
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    #: The old side's start line from a unified header: a hint only (pure insertions use it).
    old_start: int | None = None
    #: V4A ``@@ anchor``: a line (a class or function header) the hunk comes after.
    anchor: str = ""
    old_no_eol: bool = False
    new_no_eol: bool = False
    #: V4A ``*** End of File``: the hunk is at the end of the file.
    at_end: bool = False
    #: The kind of the last body line (" ", "-", "+"), for a following "\\ No newline" marker.
    last: str = field(default=" ", repr=False)

    @property
    def changes(self) -> bool:
        return self.old_lines != self.new_lines or self.old_no_eol != self.new_no_eol


@dataclass
class FilePatch:
    path: str
    kind: str = "update"  # update, add, delete
    move_to: str = ""
    hunks: list[Hunk] = field(default_factory=list)


_HUNK_RE = re.compile(r"^@@+\s*(?:-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?)?\s*@@+(.*)$")
_V4A_FILE_RE = re.compile(r"^\*\*\*\s*(Update|Add|Delete) File:\s*(.+?)\s*$")
_V4A_MOVE_RE = re.compile(r"^\*\*\*\s*Move to:\s*(.+?)\s*$")


def is_v4a(text: str) -> bool:
    return any(_V4A_FILE_RE.match(line) or line.startswith("*** Begin Patch") for line in text.split("\n"))


def parse_patch(text: str) -> list[FilePatch]:
    """Every file a patch changes. Raises :class:`PatchError` when nothing can be read from it."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_fence(text)
    files = _parse_v4a(text) if is_v4a(text) else _parse_unified(text)
    if not files:
        raise PatchError(
            "no file changes found in the patch. Send a unified diff (--- a/path, +++ b/path, @@ hunks with "
            "' ', '-', '+' lines) or a V4A patch (*** Begin Patch, *** Update File: path, ...)"
        )
    return files


def _strip_fence(text: str) -> str:
    """A patch pasted inside a ```diff fence."""
    lines = text.strip("\n").split("\n")
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]) + "\n"
    return text


def clean_path(raw: str, *, strip_prefix: bool) -> str:
    path = raw.split("\t", 1)[0].strip()
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1]
    if strip_prefix and path[:2] in {"a/", "b/"}:
        path = path[2:]
    return path


# ---------------------------------------------------------------------------- unified


def _parse_unified(text: str) -> list[FilePatch]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    git_style = any(line.startswith("diff --git ") for line in lines) or _ab_prefixed(lines)
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    pending_kind = ""
    rename_from = rename_to = ""
    index = 0

    def finish_hunk() -> None:
        nonlocal hunk
        if current is not None and hunk is not None and (hunk.old_lines or hunk.new_lines):
            current.hunks.append(hunk)
        hunk = None

    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            finish_hunk()
            current = None
            pending_kind, rename_from, rename_to = "", "", ""
            index += 1
            continue
        if hunk is None and line.startswith(("new file mode", "deleted file mode")):
            pending_kind = "add" if line.startswith("new") else "delete"
            index += 1
            continue
        if hunk is None and line.startswith("rename from "):
            rename_from = line[len("rename from "):].strip()
            index += 1
            continue
        if hunk is None and line.startswith("rename to "):
            rename_to = line[len("rename to "):].strip()
            if rename_from:
                current = FilePatch(rename_from, "update", move_to=rename_to)
                files.append(current)
            index += 1
            continue
        if line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            finish_hunk()
            old = clean_path(line[4:], strip_prefix=git_style)
            new = clean_path(lines[index + 1][4:], strip_prefix=git_style)
            if old == "/dev/null":
                kind, path, move = "add", new, ""
            elif new == "/dev/null":
                kind, path, move = "delete", old, ""
            else:
                kind, path, move = pending_kind or "update", old, (new if new != old else "")
            if current is not None and current.move_to and current.path == path:
                current.kind = kind
            else:
                current = FilePatch(path, kind, move_to=move)
                files.append(current)
            pending_kind = ""
            index += 2
            continue
        header = _HUNK_RE.match(line)
        if header is not None:
            finish_hunk()
            if current is None:
                raise PatchError(f"hunk at patch line {index + 1} comes before any --- / +++ file header")
            hunk = Hunk(old_start=int(header.group(1)) if header.group(1) is not None else None)
            index += 1
            continue
        if hunk is not None:
            _hunk_line(hunk, line)
        index += 1
    finish_hunk()
    for item in files:
        for part in item.hunks:
            _drop_blank_tail(part)
    return files


def _drop_blank_tail(hunk: Hunk) -> None:
    """Blank context lines at the end of a hunk: usually the patch's own trailing newlines.

    Dropping trailing context only loosens the match, so it is safe either way.
    """
    while len(hunk.old_lines) > 1 and hunk.new_lines and hunk.old_lines[-1] == "" == hunk.new_lines[-1]:
        hunk.old_lines.pop()
        hunk.new_lines.pop()


def _ab_prefixed(lines: list[str]) -> bool:
    for line in lines:
        if line.startswith(("--- ", "+++ ")):
            raw = clean_path(line[4:], strip_prefix=False)
            if raw == "/dev/null":
                continue
            return raw.startswith(("a/", "b/"))
    return False


def _hunk_line(hunk: Hunk, line: str) -> None:
    if line.startswith("\\"):
        # "\ No newline at end of file" applies to the line just before it.
        if hunk.last in " -":
            hunk.old_no_eol = True
        if hunk.last in " +":
            hunk.new_no_eol = True
        return
    if line == "":
        # A blank context line whose leading space was lost (or the patch's own padding).
        hunk.old_lines.append("")
        hunk.new_lines.append("")
        hunk.last = " "
        return
    tag, body = line[0], line[1:]
    if tag == "-":
        hunk.old_lines.append(body)
    elif tag == "+":
        hunk.new_lines.append(body)
    elif tag == " ":
        hunk.old_lines.append(body)
        hunk.new_lines.append(body)
    else:
        # A context line that lost its leading space: models do this. The ladder still has to find it.
        hunk.old_lines.append(line)
        hunk.new_lines.append(line)
        tag = " "
    hunk.last = tag


# ---------------------------------------------------------------------------- V4A


def _parse_v4a(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None

    def finish_hunk() -> None:
        nonlocal hunk
        if current is not None and hunk is not None and (hunk.old_lines or hunk.new_lines or hunk.at_end):
            current.hunks.append(hunk)
        hunk = None

    for line in text.split("\n"):
        if line.startswith("*** Begin Patch") or line.startswith("*** End Patch"):
            finish_hunk()
            continue
        found = _V4A_FILE_RE.match(line)
        if found:
            finish_hunk()
            kind = found.group(1).lower()
            current = FilePatch(found.group(2), kind)
            files.append(current)
            hunk = Hunk() if kind in {"update", "add"} else None
            continue
        moved = _V4A_MOVE_RE.match(line)
        if moved and current is not None:
            current.move_to = moved.group(1)
            continue
        if line.startswith("*** End of File"):
            if hunk is not None:
                hunk.at_end = True
            finish_hunk()
            if current is not None and current.kind == "update":
                hunk = Hunk()
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            finish_hunk()
            hunk = Hunk(anchor=line[2:].strip().strip("@").strip())
            continue
        if hunk is None:
            hunk = Hunk()
        if current.kind == "add":
            hunk.new_lines.append(line[1:] if line.startswith("+") else line)
            continue
        if line == "":
            hunk.old_lines.append("")
            hunk.new_lines.append("")
            continue
        _hunk_line(hunk, line)
    finish_hunk()
    for item in files:
        if item.kind == "update":
            for part in item.hunks:  # the blank line before the next *** header is not context
                _drop_blank_tail(part)
        if item.kind == "add" and item.hunks:
            body = [line for part in item.hunks for line in part.new_lines]
            while body and body[-1] == "":
                body.pop()
            item.hunks = [Hunk(new_lines=body)]
    return files


# ---------------------------------------------------------------------------- apply


@dataclass
class Applied:
    text: str
    #: The ladder step each hunk matched with (exact, whitespace, indent, punctuation, fuzzy, insert).
    steps: list[str]


def new_file_text(patch: FilePatch) -> str:
    """The contents of a file the patch adds."""
    lines = [line for hunk in patch.hunks for line in hunk.new_lines]
    no_eol = any(hunk.new_no_eol for hunk in patch.hunks)
    if not lines:
        return ""
    return "\n".join(lines) + ("" if no_eol else "\n")


def apply_hunks(text: str, patch: FilePatch) -> Applied:
    """``text`` (``\\n`` line endings) with every hunk of ``patch`` applied, or :class:`PatchError`."""
    had_newline = text.endswith("\n") or text == ""
    work = text if had_newline else text + "\n"
    cursor = 0  # line index after the previous hunk
    steps: list[str] = []
    total = len(patch.hunks)
    for number, hunk in enumerate(patch.hunks, start=1):
        label = f"hunk {number} of {total} for {patch.path}" if total > 1 else f"the hunk for {patch.path}"
        if not hunk.changes:
            continue
        if not hunk.old_lines:
            work, cursor = _insert(work, hunk, cursor)
            steps.append("insert")
            continue
        old = "\n".join(hunk.old_lines) + "\n"
        new = ("\n".join(hunk.new_lines) + "\n") if hunk.new_lines else ""
        if old == new:
            continue  # only the end-of-file newline changes; handled below
        start_line = _region_start(work, hunk, cursor)
        offset = agent_edit._offset_of_line(work, start_line)
        result = agent_edit.replace_span(work[offset:], old, new)
        if result.text is None and offset > 0 and not result.hits:
            start_line, offset = 0, 0
            result = agent_edit.replace_span(work, old, new)
        if result.text is None:
            raise PatchError(
                f"{label} does not apply: {_shift_note(result.note, start_line)}. Read the file again and fix the "
                "hunk's context and '-' lines (they must match the file), or use edit_file",
                candidate=_shift_candidate(result.candidate, start_line),
                path=patch.path,
            )
        work = work[:offset] + result.text
        steps.append(result.step)
        last = (result.lines or (1, 1))[1]
        cursor = start_line + last
    final_newline = had_newline
    if any(hunk.new_no_eol for hunk in patch.hunks):
        final_newline = False
    elif any(hunk.old_no_eol for hunk in patch.hunks):
        final_newline = True
    if not final_newline and work.endswith("\n"):
        work = work[:-1]
    return Applied(work, steps)


def _region_start(work: str, hunk: Hunk, cursor: int) -> int:
    """The line to start searching from: after the V4A anchor when there is one, else the cursor."""
    if not hunk.anchor:
        return cursor
    lines = work.split("\n")
    wanted = hunk.anchor.strip()
    for index in list(range(cursor, len(lines))) + list(range(0, cursor)):
        if wanted and wanted in lines[index]:
            return index
    return cursor


def _insert(work: str, hunk: Hunk, cursor: int) -> tuple[str, int]:
    """A hunk with only added lines: at the top, the end, or after its start line (the one hint used)."""
    body = "\n".join(hunk.new_lines) + "\n"
    lines = work.split("\n")[:-1] if work.endswith("\n") else work.split("\n")
    if hunk.old_start == 0 and not hunk.at_end:
        at = 0
    elif hunk.at_end or hunk.old_start is None:
        at = len(lines)
    else:
        at = min(max(hunk.old_start, 0), len(lines))
    offset = agent_edit._offset_of_line(work, at)
    return work[:offset] + body + work[offset:], at + len(hunk.new_lines)


def _shift_note(note: str, start_line: int) -> str:
    """Line numbers in a note from a search that began at ``start_line`` (0-based), made absolute."""
    if not start_line:
        return note
    return re.sub(r"\b(lines?|file line) (\d+)", lambda m: f"{m.group(1)} {int(m.group(2)) + start_line}", note)


def _shift_candidate(candidate: str, start_line: int) -> str:
    if not start_line or not candidate:
        return candidate
    return "\n".join(
        re.sub(r"^(\d+)\|", lambda m: f"{int(m.group(1)) + start_line}|", line) for line in candidate.split("\n")
    )


def paths_in(text: str) -> list[str]:
    """Every path a patch names (both sides of a move), for permission checks; never raises."""
    found: list[str] = []
    try:
        files = parse_patch(text)
    except PatchError:
        files = []
    for item in files:
        for path in (item.path, item.move_to):
            if path and path != "/dev/null" and path not in found:
                found.append(path)
    for line in text.replace("\r\n", "\n").split("\n"):
        header = _V4A_FILE_RE.match(line) or _V4A_MOVE_RE.match(line)
        if header:
            path = header.group(header.lastindex or 1).strip()
            if path and path not in found:
                found.append(path)
    return found
