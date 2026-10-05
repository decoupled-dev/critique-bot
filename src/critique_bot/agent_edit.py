"""Text editing for the agent: matching, line endings, diffs, undo.

The web chat copies code imperfectly. ``old_string`` arrives with ``N|``
prefixes, with backticks that were markdown in the file, with the wrong
indentation, or with one word changed. :func:`replace_span` tries exact text
first and then each looser match in turn, and only accepts a match that is
unique. A file keeps its own line endings, BOM, and encoding across an edit.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import locale
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_LINE_NO_RE = re.compile(r"^\s*\d+\|")
_PUNCT_RE = re.compile(r"[`*_~\"'\u2018\u2019\u201c\u201d]")
_MIN_FUZZY_RATIO = 0.9
_MAX_DIFF_CHARS = 2_500


@dataclass
class TextFile:
    text: str
    eol: str
    bom: bool
    encoding: str


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    how: str
    reindent: bool = False


@dataclass(frozen=True)
class EditResult:
    text: str | None
    count: int
    note: str
    candidate: str = ""


def file_version(path: Path) -> str:
    """Short token for the file as it is on disk right now."""
    try:
        stat = Path(path).stat()
    except OSError:
        return "missing"
    raw = f"{stat.st_mtime_ns}:{stat.st_size}".encode("ascii")
    return hashlib.sha1(raw).hexdigest()[:8]


def load_text(path: Path) -> TextFile:
    """Read a text file and remember how it was encoded."""
    data = Path(path).read_bytes()
    bom = data.startswith(b"\xef\xbb\xbf")
    if bom:
        data = data[3:]
    encoding = "utf-8"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        fallback = locale.getpreferredencoding(False) or "cp1252"
        try:
            text = data.decode(fallback)
            encoding = fallback
        except (UnicodeDecodeError, LookupError):
            text = data.decode("utf-8", "replace")
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    eol = "\r\n" if crlf > lf else "\n"
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return TextFile(text=normalized, eol=eol, bom=bom, encoding=encoding)


def save_text(path: Path, text: str, like: TextFile | None = None) -> None:
    """Write ``text`` with the line endings and encoding of ``like``, atomically."""
    eol = like.eol if like is not None else "\n"
    encoding = like.encoding if like is not None else "utf-8"
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    if eol != "\n":
        body = body.replace("\n", eol)
    payload = body.encode(encoding, "replace")
    if like is not None and like.bom:
        payload = b"\xef\xbb\xbf" + payload
    atomic_write_bytes(Path(path), payload)


def atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Write through a temp file and ``os.replace`` so a reader never sees half a file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".bot-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(handle, "wb") as temp:
            temp.write(payload)
        try:
            shutil.copymode(target, temp_name)
        except OSError:
            pass
        _replace_with_retry(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _replace_with_retry(source: str, target: Path) -> None:
    """``os.replace`` that retries briefly: Windows antivirus and IDEs hold handles."""
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


def strip_read_prefix(text: str) -> str:
    return "\n".join(_LINE_NO_RE.sub("", line, count=1) for line in text.split("\n"))


def replace_span(
    text: str,
    old: str,
    new: str,
    *,
    replace_all: bool = False,
) -> EditResult:
    """Replace ``old`` with ``new`` in ``text`` (which already uses ``\\n``).

    Order: exact, exact after removing ``N|`` prefixes, line match ignoring
    surrounding whitespace (re-indenting the replacement), line match ignoring
    markdown punctuation, then the closest window at 90% similarity. Every
    loose match must be unique. ``text`` in the result is None on failure and
    ``candidate`` holds the closest block of the file with line numbers.
    """
    needle = strip_read_prefix(old).replace("\r\n", "\n").replace("\r", "\n")
    replacement = strip_read_prefix(new).replace("\r\n", "\n").replace("\r", "\n")
    if _same_lines(needle, replacement):
        return EditResult(
            None,
            0,
            "old_string and new_string are the same text; the file was not changed. "
            "Send a real difference, or reply COMPLETED if no edit is needed.",
        )
    if not needle.strip():
        return EditResult(None, 0, "old_string is blank")
    count = text.count(needle)
    if count == 1 or (count > 1 and replace_all):
        times = count if replace_all else 1
        updated = text.replace(needle, replacement) if replace_all else text.replace(needle, replacement, 1)
        note = "removed N| prefixes from old_string" if needle != old.replace("\r\n", "\n") else ""
        return EditResult(updated, times, note)
    if count > 1:
        return EditResult(
            None,
            0,
            f"old_string matched {count} times; pass replace_all true or include more surrounding lines",
        )
    match = _loose_match(text, needle)
    if match is None:
        return EditResult(None, 0, "old_string was not found", best_candidate(text, needle))
    if match.how == "ambiguous":
        return EditResult(
            None,
            0,
            "old_string matched more than once after ignoring whitespace and punctuation; "
            "include more surrounding lines",
        )
    body = replacement
    if match.reindent:
        body = _reindent(replacement, needle, text[match.start : match.end])
    if body.endswith("\n") and text[match.end : match.end + 1] == "\n":
        body = body[:-1]
    updated = text[: match.start] + body + text[match.end :]
    return EditResult(updated, 1, f"matched {match.how}")


def _same_lines(left: str, right: str) -> bool:
    def lines(value: str) -> list[str]:
        parts = value.split("\n")
        if parts and parts[-1] == "":
            parts = parts[:-1]
        return [line.strip() for line in parts]

    return lines(left) == lines(right)


def _loose_match(text: str, needle: str) -> Match | None:
    lines = needle.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines:
        return None
    for how, normalize in (
        ("ignoring surrounding whitespace", lambda line: line.strip()),
        ("ignoring punctuation and markdown", _fold_punct),
    ):
        folded = [normalize(line) for line in lines]
        if not any(folded):
            continue
        found = _match_folded(text, folded, normalize, how)
        if found is not None:
            return found
    return _fuzzy_match(text, lines)


def _match_folded(text: str, folded: list[str], normalize, how: str) -> Match | None:
    haystack = text.split("\n")
    width = len(folded)
    if width == 0 or width > len(haystack):
        return None
    folded_hay = [normalize(line) for line in haystack]
    hits: list[int] = []
    for start in range(len(haystack) - width + 1):
        if folded_hay[start : start + width] == folded:
            hits.append(start)
            if len(hits) > 1:
                return Match(0, 0, "ambiguous")
    if not hits:
        return None
    return _line_span(text, hits[0], width, how)


def _line_span(text: str, first: int, width: int, how: str) -> Match:
    start = _offset_of_line(text, first)
    end = _offset_of_line(text, first + width)
    if end > start and text[end - 1 : end] == "\n":
        end -= 1
    return Match(start, end, how, True)


def _fuzzy_match(text: str, lines: list[str]) -> Match | None:
    haystack = text.split("\n")
    width = len(lines)
    if width == 0 or width > len(haystack):
        return None
    needle_text = "\n".join(line.strip() for line in lines)
    if len(needle_text) < 12:
        return None
    best_ratio = 0.0
    best_start = -1
    ties = 0
    for start in range(len(haystack) - width + 1):
        window = "\n".join(line.strip() for line in haystack[start : start + width])
        if abs(len(window) - len(needle_text)) > max(20, len(needle_text) // 3):
            continue
        matcher = difflib.SequenceMatcher(None, needle_text, window, autojunk=False)
        if matcher.real_quick_ratio() < _MIN_FUZZY_RATIO or matcher.quick_ratio() < _MIN_FUZZY_RATIO:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio + 1e-9:
            best_ratio = ratio
            best_start = start
            ties = 0
        elif abs(ratio - best_ratio) <= 1e-9:
            ties += 1
    if best_start < 0 or best_ratio < _MIN_FUZZY_RATIO:
        return None
    if ties:
        return Match(0, 0, "ambiguous")
    # Only drift in punctuation or spacing is tolerated: a different word means different code.
    found = "\n".join(haystack[best_start : best_start + width])
    if re.findall(r"\w+", needle_text) != re.findall(r"\w+", found):
        return None
    return _line_span(text, best_start, width, f"closest text ({best_ratio:.0%} similar)")


def _fold_punct(line: str) -> str:
    return re.sub(r"\s+", " ", _PUNCT_RE.sub("", line)).strip()


def _offset_of_line(text: str, line_index: int) -> int:
    if line_index <= 0:
        return 0
    offset = 0
    for _ in range(line_index):
        nxt = text.find("\n", offset)
        if nxt < 0:
            return len(text)
        offset = nxt + 1
    return offset


def _reindent(replacement: str, needle: str, matched: str) -> str:
    """Shift ``replacement`` so its indentation matches the text that matched."""
    needle_indent = _common_indent(needle)
    matched_indent = _common_indent(matched)
    if needle_indent == matched_indent:
        return replacement
    out: list[str] = []
    for line in replacement.split("\n"):
        if not line.strip():
            out.append(line)
        elif needle_indent and line.startswith(needle_indent):
            out.append(matched_indent + line[len(needle_indent) :])
        elif not needle_indent:
            out.append(matched_indent + line)
        else:
            out.append(line)
    return "\n".join(out)


def _common_indent(block: str) -> str:
    indents = [
        re.match(r"[ \t]*", line).group(0)  # type: ignore[union-attr]
        for line in block.split("\n")
        if line.strip()
    ]
    if not indents:
        return ""
    prefix = indents[0]
    for indent in indents[1:]:
        while not indent.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def best_candidate(text: str, needle: str, *, context: int = 2) -> str:
    """The block of the file that looks most like ``needle``, with line numbers."""
    lines = text.split("\n")
    wanted = [line for line in needle.split("\n") if line.strip()]
    if not wanted or not lines:
        return ""
    probe = max(wanted, key=len).strip()
    width = len(wanted)
    target = "\n".join(line.strip() for line in wanted)
    best_score = 0.0
    best_index = -1
    for index in range(len(lines)):
        window = "\n".join(line.strip() for line in lines[index : index + width])
        matcher = difflib.SequenceMatcher(None, target, window, autojunk=False)
        hit = bool(probe) and probe in lines[index]
        if matcher.quick_ratio() < 0.4 and not hit:
            continue
        score = matcher.ratio() + (0.25 if hit else 0.0)
        if score > best_score:
            best_score = score
            best_index = index
    if best_index < 0 or best_score < 0.45:
        return ""
    begin = max(0, best_index - context)
    end = min(len(lines), best_index + width + context)
    return "\n".join(f"{number + 1}|{lines[number]}" for number in range(begin, end))


def hunk_diff(before: str, after: str, path: str, *, context: int = 3) -> str:
    """Unified diff of the change, trimmed to a readable size."""
    diff = difflib.unified_diff(
        before.split("\n"),
        after.split("\n"),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
        lineterm="",
    )
    text = "\n".join(diff)
    if len(text) > _MAX_DIFF_CHARS:
        text = text[:_MAX_DIFF_CHARS] + "\n... diff truncated"
    return text


_BRACE_SUFFIXES = frozenset(
    {".java", ".kt", ".kts", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs",
     ".js", ".ts", ".tsx", ".jsx"}
)


def syntax_check(path: str, text: str) -> str:
    """``ok``, ``error: ...``, or an empty string when the type is not checked."""
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        try:
            compile(text, path, "exec")
        except SyntaxError as exc:
            return f"error: line {exc.lineno}: {exc.msg}"
        return "ok"
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return f"error: line {exc.lineno}: {exc.msg}"
        return "ok"
    if suffix in _BRACE_SUFFIXES:
        balance = _brace_balance(text)
        if balance != 0:
            return f"error: braces unbalanced ({balance:+d})"
        return "ok"
    return ""


def _brace_balance(text: str) -> int:
    depth = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in "\"'`":
            quote = char
            index += 1
            while index < length and text[index] != quote:
                if text[index] == "\\":
                    index += 1
                if quote != "`" and text[index : index + 1] == "\n":
                    break
                index += 1
        elif text.startswith("//", index):
            nxt = text.find("\n", index)
            index = length if nxt < 0 else nxt
        elif text.startswith("/*", index):
            nxt = text.find("*/", index + 2)
            index = length if nxt < 0 else nxt + 1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return depth


class Checkpoints:
    """Pre-edit copies under ``.bot/cache/undo/<task>/``, newest task last."""

    def __init__(self, cache_dir: Path | None, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = (Path(cache_dir) / "undo") if cache_dir is not None else None
        self.task_dir: Path | None = None
        self._step = 0
        self._saved: set[str] = set()

    def start_task(self) -> None:
        if self.root is None:
            return
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{time.monotonic_ns() % 10_000:04d}"
        self.task_dir = self.root / stamp
        self._step = 0
        self._saved = set()

    def save(self, path: Path) -> None:
        """Copy ``path`` before its first change in this task."""
        if self.task_dir is None:
            return
        target = Path(path).resolve()
        try:
            rel = target.relative_to(self.workspace).as_posix()
        except ValueError:
            return
        if rel in self._saved:
            return
        self._saved.add(rel)
        self._step += 1
        folder = self.task_dir / f"{self._step:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "path.txt").write_text(rel + "\n", encoding="utf-8")
        if target.is_file():
            shutil.copy2(target, folder / "before")
        self._prune()

    def _prune(self, keep: int = 20) -> None:
        if self.root is None or not self.root.is_dir():
            return
        tasks = sorted(item for item in self.root.iterdir() if item.is_dir())
        for stale in tasks[:-keep]:
            shutil.rmtree(stale, ignore_errors=True)


def undo_last(cache_dir: Path, workspace: Path) -> list[str]:
    """Restore the files touched by the most recent task. Returns the paths."""
    root = Path(cache_dir) / "undo"
    if not root.is_dir():
        return []
    tasks = sorted(item for item in root.iterdir() if item.is_dir())
    if not tasks:
        return []
    task = tasks[-1]
    restored: list[str] = []
    for step in sorted(item for item in task.iterdir() if item.is_dir()):
        marker = step / "path.txt"
        if not marker.is_file():
            continue
        rel = marker.read_text(encoding="utf-8").strip()
        if not rel:
            continue
        target = Path(workspace) / rel
        before = step / "before"
        if before.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(before, target)
        elif target.is_file():
            target.unlink()
        restored.append(rel)
    shutil.rmtree(task, ignore_errors=True)
    return restored
