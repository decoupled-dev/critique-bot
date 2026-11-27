# SPDX-License-Identifier: Apache-2.0
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
import threading
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from critique_bot.output import private_dir
from critique_bot.redaction import sensitive_file

_LINE_NO_RE = re.compile(r"^\s*\d+\|")
_PUNCT_RE = re.compile(r"[`*_~\"'\u2018\u2019\u201c\u201d]")
_MAX_DIFF_CHARS = 2_500


@dataclass
class TextFile:
    text: str
    eol: str
    bom: bool
    encoding: str
    # The terminator of each line as found on disk (``\r\n``, ``\n`` or ``\r``).
    # None when the file was not loaded from disk.
    endings: list[str] | None = None


@dataclass(frozen=True)
class EditResult:
    text: str | None
    count: int
    note: str
    candidate: str = ""
    #: The ladder step that matched (see LADDER_STEPS); empty when nothing matched.
    step: str = ""
    #: 1-based first and last line the new text occupies in the result.
    lines: tuple[int, int] | None = None
    #: Line numbers of every hit when the match was ambiguous.
    hits: tuple[int, ...] = ()
    #: On a refusal, the first line of old_string that differs from the closest candidate.
    differs: str = ""


def file_version(path: Path) -> str:
    """Short token for the file as it is on disk right now."""
    try:
        stat = Path(path).stat()
    except OSError:
        return "missing"
    raw = f"{stat.st_mtime_ns}:{stat.st_size}".encode("ascii")
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:8]  # a file-version fingerprint


#: Files up to this size are fingerprinted by their bytes; larger ones by size and mtime.
DIGEST_MAX_BYTES = 16 * 1024 * 1024


def content_digest(path: Path) -> str:
    """A fingerprint of the file's bytes ("missing" when it does not exist).

    Stale-read protection compares this against the fingerprint taken when the model read
    the file, so a change by the user, a formatter or a build is noticed even when the
    size and modification time happen to come out the same.
    """
    try:
        size = Path(path).stat().st_size
        if size > DIGEST_MAX_BYTES:
            return "v:" + file_version(path)
        return hashlib.sha1(Path(path).read_bytes(), usedforsecurity=False).hexdigest()[:16]
    except OSError:
        return "missing"


_EOL_RE = re.compile(r"\r\n|\r|\n")


def load_text(path: Path) -> TextFile:
    """Read a text file and remember how it was encoded.

    UTF-8 first, then the locale encoding when it round-trips the bytes, then
    latin-1, which maps every byte to one character, so saving the text back
    never loses a byte the edit did not touch.
    """
    data = Path(path).read_bytes()
    bom = data.startswith(b"\xef\xbb\xbf")
    if bom:
        data = data[3:]
    text, encoding = _decode(data)
    endings = _EOL_RE.findall(text)
    crlf = endings.count("\r\n")
    lf = endings.count("\n")
    cr = endings.count("\r")
    eol = "\r\n" if crlf > lf and crlf >= cr else ("\r" if cr > lf and cr > crlf else "\n")
    normalized = _EOL_RE.sub("\n", text)
    return TextFile(text=normalized, eol=eol, bom=bom, encoding=encoding, endings=endings)


def _decode(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    fallback = locale.getpreferredencoding(False) or ""
    if fallback and fallback.replace("-", "").lower() not in {"utf8", "latin1", "iso88591"}:
        try:
            text = data.decode(fallback)
            if text.encode(fallback) == data:
                return text, fallback
        except (UnicodeError, LookupError):
            pass
    return data.decode("latin-1"), "latin-1"


def save_text(path: Path, text: str, like: TextFile | None = None) -> None:
    """Write ``text`` with the line endings and encoding of ``like``, atomically.

    Lines that did not change keep the terminator they had on disk; new lines
    use the file's dominant one. Raises ValueError when the new text holds a
    character the file's encoding cannot store.
    """
    eol = like.eol if like is not None else "\n"
    encoding = like.encoding if like is not None else "utf-8"
    body = _EOL_RE.sub("\n", text)
    body = _join_lines(body, like, eol)
    try:
        payload = body.encode(encoding)
    except UnicodeEncodeError as exc:
        bad = body[exc.start : exc.end]
        raise ValueError(
            f"the file is stored as {encoding} and cannot hold {bad!r}; "
            "use characters that encoding supports (for example ASCII escapes)"
        ) from None
    if like is not None and like.bom:
        payload = b"\xef\xbb\xbf" + payload
    atomic_write_bytes(Path(path), payload)


def _join_lines(body: str, like: TextFile | None, eol: str) -> str:
    endings = like.endings if like is not None else None
    if not endings or all(item == eol for item in endings):
        return body.replace("\n", eol) if eol != "\n" else body
    old_lines = like.text.split("\n")  # type: ignore[union-attr]
    new_lines = body.split("\n")
    chosen = [eol] * (len(new_lines) - 1)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(j2 - j1):
            old_index = i1 + offset
            new_index = j1 + offset
            if new_index < len(chosen) and old_index < len(endings):
                chosen[new_index] = endings[old_index]
    out: list[str] = []
    for index, line in enumerate(new_lines):
        out.append(line)
        if index < len(chosen):
            out.append(chosen[index])
    return "".join(out)


def atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Write through a temp file and ``os.replace`` so a reader never sees half a file.

    The temp file is in the target's folder (a rename never crosses file systems) and is
    flushed to disk (``fsync``) before the rename, so a crash leaves either the old file
    or the new one, never a torn mix; a failure before the rename removes the temp file
    and leaves the original as it was. An existing file keeps its permission bits (an
    executable script stays executable). A symbolic link is written through to the file
    it points at, not replaced. A new file gets the usual permissions for new files
    (0666 minus umask).
    """
    target = Path(target)
    if target.is_symlink():
        target = Path(os.path.realpath(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    handle, temp_name = tempfile.mkstemp(prefix=".crit-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(handle, "wb") as temp:
            temp.write(payload)
            temp.flush()
            try:
                os.fsync(temp.fileno())
            except OSError:
                pass  # a file system without fsync (some network shares): the rename is still atomic
        try:
            if existed:
                shutil.copymode(target, temp_name)
            else:
                os.chmod(temp_name, 0o666 & ~_umask())
        except OSError:
            pass
        _replace_with_retry(temp_name, target)
        _fsync_dir(target.parent)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _fsync_dir(folder: Path) -> None:
    """Make the rename itself durable (POSIX; a no-op where folders cannot be opened)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(folder), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _umask() -> int:
    if os.name == "nt":
        return 0
    current = os.umask(0)
    os.umask(current)
    return current


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


_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)\|")


def strip_numbered_lines(text: str) -> tuple[str, bool]:
    """Drop ``N|`` prefixes copied from read_files when every line of a file carries one, in order.

    A file whose own lines happen to start with ``1|`` is left alone: the numbers must
    count up from the first line, which real file text does not do.
    """
    lines = text.split("\n")
    body = lines[:-1] if lines and lines[-1] == "" else lines
    if len(body) < 2:
        return text, False
    expected = None
    for line in body:
        match = _NUMBERED_LINE_RE.match(line)
        if match is None:
            return text, False
        number = int(match.group(1))
        if expected is None:
            expected = number
        if number != expected:
            return text, False
        expected += 1
    return "\n".join(_NUMBERED_LINE_RE.sub("", line, count=1) for line in lines), True


_COMMENT_LEAD_RE = re.compile(r"^\s*(?://+|#+|/\*+|\*+|<!--|--|;+|%+|'|rem\s)", re.I)
_ELLIPSIS_RE = re.compile(r"(?:^|[\s(\[{])(?:\.\.\.|…)(?:$|[\s)\]}])")
_ELISION_RE = re.compile(
    r"rest of (?:the |this )?(?:file|code|class|method|function|implementation)|"
    r"existing (?:code|implementation|methods?|logic)|(?:code |methods? |logic |implementation )?"
    r"(?:remains?|stays?|is|are) unchanged|unchanged (?:code|below|above)|"
    r"omitted for brevity|(?:remaining|other) (?:code|methods?|functions?|lines)|"
    r"same as before|no changes? (?:here|below|above)|snip\b|\betc\.?\s*$",
    re.I,
)


def placeholder_line(text: str) -> tuple[int, str] | None:
    """The first line that stands for left-out code (``// ... existing code ...``), or None.

    Only comment lines count, so a bare ``...`` (an abstract Python body) and prose in
    strings are left alone. The line number is 1-based.
    """
    for number, line in enumerate(text.split("\n"), start=1):
        stripped = line.strip()
        if not stripped or not _COMMENT_LEAD_RE.match(stripped):
            continue
        if _ELLIPSIS_RE.search(stripped) or _ELISION_RE.search(stripped):
            return number, stripped
    return None


#: The steps of the match ladder, in the order they are tried (run_stats counts each).
LADDER_STEPS = ("exact", "whitespace", "indent", "punctuation", "fuzzy")
#: Step 4: a window must be at least this similar to old_string...
FUZZY_MIN_RATIO = 0.92
#: ...and beat the best window elsewhere in the file by this margin.
FUZZY_MARGIN = 0.05
#: Fuzzy matching is skipped on files longer than this (it compares every window).
FUZZY_MAX_FILE_LINES = 50_000
#: The closest candidate shown when an edit is refused.
CANDIDATE_MAX_LINES = 15


def replace_span(
    text: str,
    old: str,
    new: str,
    *,
    replace_all: bool = False,
) -> EditResult:
    """Replace ``old`` with ``new`` in ``text`` (which already uses ``\\n``): the match ladder.

    1. ``exact``: the text as sent (and again without copied ``N|`` prefixes). One hit
       applies; several hits are refused with their line numbers unless ``replace_all``.
    2. ``whitespace``: line by line, ignoring line endings and trailing whitespace.
    3. ``indent``: ignoring each line's indentation; ``new`` is re-indented to the hit
       (tabs vs spaces included).
    4. ``punctuation``: ignoring markdown punctuation a chat page adds or drops.
    5. ``fuzzy``: the window most similar to ``old`` (≥ 92%), only when it beats every
       window elsewhere by ≥ 5 points.

    Each step stops at the first that finds anything: a unique hit applies, several hits
    are refused. Otherwise the result is a refusal with the closest candidate (≤ 15
    numbered lines) and the first line that differs. ``text`` in the result is None on
    failure; ``step`` says which step matched; ``lines`` is the 1-based range the new text
    occupies (the first and last hit's for ``replace_all``).
    """
    raw = old.replace("\r\n", "\n").replace("\r", "\n")
    needle = raw if raw and raw in text else strip_read_prefix(old).replace("\r\n", "\n").replace("\r", "\n")
    replacement = (new if needle == raw else strip_read_prefix(new)).replace("\r\n", "\n").replace("\r", "\n")
    if needle == replacement or (_same_lines(needle, replacement) and needle.strip() == replacement.strip()):
        return EditResult(
            None,
            0,
            "old_string and new_string are the same text; the file was not changed. "
            "Send a real difference, or reply COMPLETED if no edit is needed.",
        )
    if not needle.strip():
        return EditResult(None, 0, "old_string is blank")
    prefix_note = "removed N| prefixes from old_string" if needle != raw else ""
    count = text.count(needle)
    if count == 1 or (count > 1 and replace_all):
        return _apply_exact(text, needle, replacement, replace_all, prefix_note)
    if count > 1:
        hits = _hit_lines(text, needle)
        return EditResult(
            None,
            0,
            f"old_string matches {count} places (lines {_list_lines(hits)}); include more surrounding lines "
            "so it matches one place, or pass replace_all true to change every one",
            hits=tuple(hits),
        )
    lines = text.split("\n")
    needle_lines = needle.split("\n")
    ends_newline = len(needle_lines) > 1 and needle_lines[-1] == ""
    if ends_newline:
        needle_lines = needle_lines[:-1]
    for step, normalize in (
        ("whitespace", lambda line: line.rstrip()),
        ("indent", lambda line: line.strip()),
        ("punctuation", _fold_punct),
    ):
        folded = [normalize(line) for line in needle_lines]
        if not any(folded):
            continue
        hits = _folded_hits(lines, folded, normalize)
        if not hits:
            continue
        if len(hits) > 1 and not replace_all:
            what = {"whitespace": "trailing whitespace", "indent": "indentation", "punctuation": "punctuation"}[step]
            return EditResult(
                None,
                0,
                f"old_string matches {len(hits)} places when {what} is ignored (lines "
                f"{_list_lines([hit + 1 for hit in hits])}); include more surrounding lines so it matches one place",
                hits=tuple(hit + 1 for hit in hits),
                step=step,
            )
        return _apply_lines(text, lines, hits, len(needle_lines), needle, replacement, ends_newline, step,
                            reindent=step != "whitespace", note=prefix_note)
    if len(lines) <= FUZZY_MAX_FILE_LINES:
        scored = _fuzzy_scores(lines, needle_lines)
        if scored:
            best_ratio, best_start = scored[0]
            width = len(needle_lines)
            runner = max(
                (ratio for ratio, start in scored[1:] if abs(start - best_start) >= width), default=0.0
            )
            if best_ratio >= FUZZY_MIN_RATIO:
                if runner > best_ratio - FUZZY_MARGIN:
                    second = next(start for ratio, start in scored[1:]
                                  if abs(start - best_start) >= width and ratio == runner)
                    return EditResult(
                        None,
                        0,
                        f"old_string was not found exactly; it is about equally close to lines "
                        f"{best_start + 1}-{best_start + width} ({best_ratio:.0%}) and "
                        f"{second + 1}-{second + width} ({runner:.0%}). Copy old_string exactly from one of them",
                        hits=(best_start + 1, second + 1),
                        candidate=_numbered(lines, best_start, width),
                    )
                result = _apply_lines(text, lines, [best_start], width, needle, replacement, ends_newline, "fuzzy",
                                      reindent=True, note="")
                first, last = result.lines or (best_start + 1, best_start + width)
                return EditResult(
                    result.text,
                    result.count,
                    f"matched approximately at lines {best_start + 1}-{best_start + width} "
                    f"({best_ratio:.0%} similar); check the diff",
                    step="fuzzy",
                    lines=(first, last),
                )
    return _refusal(lines, needle_lines)


def _apply_exact(text: str, needle: str, replacement: str, replace_all: bool, note: str) -> EditResult:
    starts: list[int] = []
    position = text.find(needle)
    while position >= 0:
        starts.append(position)
        if not replace_all:
            break
        position = text.find(needle, position + len(needle))
    pieces: list[str] = []
    last = 0
    ranges: list[tuple[int, int]] = []
    line_shift = 0
    for start in starts:
        pieces.append(text[last:start])
        pieces.append(replacement)
        first_line = text.count("\n", 0, start) + 1 + line_shift
        ranges.append((first_line, first_line + _span_lines(replacement)))
        line_shift += replacement.count("\n") - needle.count("\n")
        last = start + len(needle)
    pieces.append(text[last:])
    return EditResult("".join(pieces), len(starts), note, step="exact", lines=(ranges[0][0], ranges[-1][1]))


def _span_lines(body: str) -> int:
    """Lines past the first that ``body`` covers (0 for one line)."""
    trimmed = body[:-1] if body.endswith("\n") else body
    return trimmed.count("\n")


def _hit_lines(text: str, needle: str) -> list[int]:
    hits: list[int] = []
    position = text.find(needle)
    while position >= 0 and len(hits) < 50:
        hits.append(text.count("\n", 0, position) + 1)
        position = text.find(needle, position + 1)
    return hits


def _list_lines(numbers: list[int], limit: int = 10) -> str:
    shown = ", ".join(str(number) for number in numbers[:limit])
    return shown + (f", ... ({len(numbers)} in all)" if len(numbers) > limit else "")


def _folded_hits(lines: list[str], folded: list[str], normalize) -> list[int]:
    width = len(folded)
    if width == 0 or width > len(lines):
        return []
    first = folded[0]
    hay = [normalize(line) for line in lines]
    hits: list[int] = []
    start = 0
    while start <= len(lines) - width:
        if hay[start] == first and hay[start : start + width] == folded:
            hits.append(start)
            start += width  # hits do not overlap
        else:
            start += 1
    return hits


def _apply_lines(
    text: str,
    lines: list[str],
    hits: list[int],
    width: int,
    needle: str,
    replacement: str,
    ends_newline: bool,
    step: str,
    *,
    reindent: bool,
    note: str,
) -> EditResult:
    """Replace ``width`` whole lines at each hit (0-based line indexes) with ``replacement``.

    The region matched is the hit's lines, plus the newline after them when ``old_string``
    ended with one, the same as an exact match of the text would cover.
    """
    out = text
    ranges: list[tuple[int, int]] = []
    for hit in sorted(hits, reverse=True):
        start = _offset_of_line(out, hit)
        end = _offset_of_line(out, hit + width)
        if not ends_newline and end > start and out[end - 1 : end] == "\n" and hit + width <= len(lines):
            end -= 1
        body = replacement
        if reindent:
            matched = "\n".join(lines[hit : hit + width])
            body = _reindent(replacement, needle, matched)
        if ends_newline and not out[start:end].endswith("\n") and body.endswith("\n"):
            body = body[:-1]  # the hit is the last line of a file without a final newline
        out = out[:start] + body + out[end:]
        ranges.append((hit + 1, hit + 1 + _span_lines(body)))
    ranges.sort()
    # Later hits shifted by the earlier ones: recount the first and last from the top.
    shift = 0
    fixed: list[tuple[int, int]] = []
    for first, last in ranges:
        fixed.append((first + shift, last + shift))
        shift += (last - first) - (width - 1)
    result_note = note
    if step in {"whitespace", "indent", "punctuation"}:
        result_note = "; ".join(part for part in (note, f"matched ignoring {_STEP_WORDS[step]}") if part)
    return EditResult(out, len(hits), result_note, step=step, lines=(fixed[0][0], fixed[-1][1]))


_STEP_WORDS = {
    "whitespace": "line endings and trailing whitespace",
    "indent": "indentation (new_string re-indented to the file)",
    "punctuation": "markdown punctuation",
}


def _fuzzy_scores(lines: list[str], needle_lines: list[str]) -> list[tuple[float, int]]:
    """Windows the size of ``needle_lines`` with their similarity, best first.

    Only windows within reach of the threshold minus the margin are scored, so the
    runner-up for the margin test is among them when it matters.
    """
    width = len(needle_lines)
    if width == 0 or width > len(lines):
        return []
    needle_text = "\n".join(line.strip() for line in needle_lines)
    if len(needle_text) < 12:
        return []
    floor = FUZZY_MIN_RATIO - FUZZY_MARGIN - 1e-9
    stripped = [line.strip() for line in lines]
    scored: list[tuple[float, int]] = []
    for start in range(len(lines) - width + 1):
        window = "\n".join(stripped[start : start + width])
        if abs(len(window) - len(needle_text)) > max(8, int(len(needle_text) * 0.2)):
            continue
        matcher = difflib.SequenceMatcher(None, needle_text, window, autojunk=False)
        if matcher.real_quick_ratio() < floor or matcher.quick_ratio() < floor:
            continue
        ratio = matcher.ratio()
        if ratio >= floor:
            scored.append((ratio, start))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored


def _closest_window(lines: list[str], needle_lines: list[str]) -> int:
    """Start of the window most like ``needle_lines`` (-1 when nothing is close)."""
    width = len(needle_lines)
    if not lines or width == 0:
        return -1
    width = min(width, len(lines))
    target = "\n".join(line.strip() for line in needle_lines[:width])
    probe = max((line.strip() for line in needle_lines), key=len)
    stripped = [line.strip() for line in lines]
    best_score, best = 0.0, -1
    for start in range(len(lines) - width + 1):
        window = "\n".join(stripped[start : start + width])
        hit = bool(probe) and any(probe in line for line in stripped[start : start + width])
        matcher = difflib.SequenceMatcher(None, target, window, autojunk=False)
        if matcher.quick_ratio() < 0.4 and not hit:
            continue
        score = matcher.ratio() + (0.25 if hit else 0.0)
        if score > best_score:
            best_score, best = score, start
    return best if best_score >= 0.45 else -1


def _numbered(lines: list[str], start: int, width: int, context: int = 2) -> str:
    begin = max(0, start - context)
    end = min(len(lines), start + width + context)
    if end - begin > CANDIDATE_MAX_LINES:
        end = begin + CANDIDATE_MAX_LINES
    return "\n".join(f"{number + 1}|{lines[number]}" for number in range(begin, end))


def _refusal(lines: list[str], needle_lines: list[str]) -> EditResult:
    """Step 5: not found. The closest candidate and the first line that differs."""
    start = _closest_window(lines, needle_lines)
    if start < 0:
        return EditResult(None, 0, "old_string was not found, and nothing in the file is close to it; read the file again")
    width = min(len(needle_lines), len(lines) - start)
    differs = ""
    for index in range(width):
        expected, found = needle_lines[index], lines[start + index]
        if expected != found:
            differs = (
                f"line {index + 1} of old_string differs: expected {_show(expected)}, "
                f"file line {start + index + 1} has {_show(found)}"
            )
            break
    if not differs and len(needle_lines) > width:
        differs = f"old_string has {len(needle_lines)} lines but the file ends after line {len(lines)}"
    note = "old_string was not found" + (f"; {differs}" if differs else "")
    return EditResult(None, 0, note, candidate=_numbered(lines, start, width), differs=differs)


def _show(line: str, limit: int = 100) -> str:
    shown = repr(line)
    return shown if len(shown) <= limit else shown[: limit - 4] + "...'"


def _same_lines(left: str, right: str) -> bool:
    def lines(value: str) -> list[str]:
        parts = value.split("\n")
        if parts and parts[-1] == "":
            parts = parts[:-1]
        return [line.strip() for line in parts]

    return lines(left) == lines(right)


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


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _indent_unit(block: str) -> int:
    """The width of one indentation level in a block that indents with spaces (default 4)."""
    widths = sorted({len(_indent_of(line)) for line in block.split("\n") if line.strip() and "\t" not in _indent_of(line)})
    steps = [b - a for a, b in zip(widths, widths[1:]) if b > a]
    if widths and widths[0]:
        steps.append(widths[0])
    unit = min(steps) if steps else 4
    return unit if 1 < unit <= 8 else 4


def _reindent(replacement: str, needle: str, matched: str) -> str:
    """Shift ``replacement`` so its indentation matches the text that matched.

    The indentation each line has beyond old_string's common indent is kept, written in
    the file's style: tabs when the matched lines indent with tabs, spaces otherwise.
    """
    needle_indent = _common_indent(needle)
    matched_indent = _common_indent(matched)
    matched_tabs = any(_indent_of(line).startswith("\t") for line in matched.split("\n") if line.strip())
    needle_tabs = any(_indent_of(line).startswith("\t") for line in needle.split("\n") if line.strip())
    unit = _indent_unit(needle if not needle_tabs else matched)
    file_unit = _indent_unit(matched) if not matched_tabs else unit
    # Spaces on both sides, but 2 per level in old_string and 4 in the file (or the reverse).
    scale = not matched_tabs and not needle_tabs and unit != file_unit and {unit, file_unit} <= {2, 4, 8}
    if needle_indent == matched_indent and matched_tabs == needle_tabs and not scale:
        return replacement
    out: list[str] = []
    for line in replacement.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        lead = _indent_of(line)
        if lead.startswith(needle_indent):
            extra = lead[len(needle_indent) :]
        else:
            out.append(line)
            continue
        if matched_tabs and not needle_tabs and extra and "\t" not in extra:
            extra = "\t" * (len(extra) // unit) + " " * (len(extra) % unit)
        elif needle_tabs and not matched_tabs and "\t" in extra:
            extra = extra.replace("\t", " " * file_unit)
        elif scale and extra:
            extra = " " * ((len(extra) // unit) * file_unit + len(extra) % unit)
        out.append(matched_indent + extra + line[len(lead) :])
    return "\n".join(out)


def _common_indent(block: str) -> str:
    indents = [_indent_of(line) for line in block.split("\n") if line.strip()]
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
    if suffix in XML_SUFFIXES:
        return _xml_check(text)
    if suffix in _BRACE_SUFFIXES:
        balance = _brace_balance(text)
        if balance != 0:
            return f"error: braces unbalanced ({balance:+d})"
        return "ok"
    return ""


#: Files checked for well-formed XML (Android manifests, layouts, resources, Maven, .NET projects).
XML_SUFFIXES = frozenset(
    {".xml", ".xsd", ".xsl", ".xslt", ".svg", ".plist", ".resx", ".csproj", ".vbproj", ".props", ".targets",
     ".fxml", ".iml", ".xaml", ".wsdl"}
)
#: An edit or rewrite that would break these is not applied (when the file was valid before).
STRICT_SUFFIXES = frozenset({".py", ".json"}) | XML_SUFFIXES


def _xml_check(text: str) -> str:
    """``ok`` or ``error: line N: ...`` for well-formed XML, with a hint for the usual slips."""
    from xml.parsers import expat

    body = text.lstrip("\ufeff")
    if not body.strip():
        return ""
    parser = expat.ParserCreate(namespace_separator=" ")
    try:
        parser.Parse(body, True)
    except expat.ExpatError as exc:
        message = expat.ErrorString(exc.code)
        line = body.split("\n")[exc.lineno - 1] if 0 < exc.lineno <= body.count("\n") + 1 else ""
        hint = ""
        if "not well-formed" in message and "&" in line:
            hint = " (in XML text write & as &amp; and < as &lt;)"
        elif "mismatched tag" in message:
            hint = " (a tag is closed with a different name, or one is left open)"
        elif "unbound prefix" in message:
            hint = " (declare the namespace, e.g. xmlns:android=\"http://schemas.android.com/apk/res/android\", or fix the prefix)"
        elif "junk after document element" in message:
            hint = " (there is more than one root element)"
        elif "no element found" in message:
            hint = " (the file ends before the root element is closed)"
        return f"error: line {exc.lineno}, column {exc.offset + 1}: {message}{hint}"
    return "ok"


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


_TASK_COUNTER = [0]


def _task_stamp(root: Path) -> str:
    """A folder name that sorts after every earlier task, even within one second."""
    _TASK_COUNTER[0] += 1
    now = time.time_ns()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now // 1_000_000_000))
    name = f"{stamp}.{now % 1_000_000_000:09d}-{_TASK_COUNTER[0] % 10_000:04d}"
    try:
        latest = max((item.name for item in root.iterdir() if item.is_dir()), default="")
    except OSError:
        latest = ""
    if latest and name <= latest:
        name = latest + "+"
    return name


class Checkpoints:
    """Pre-edit copies under ``.crit/cache/undo/<task>/``, newest task last.

    Each step folder holds ``path.txt`` (workspace-relative, or absolute for a
    file outside the workspace that the user approved), ``before`` (absent
    when the file did not exist), and ``dirs.txt`` (folders that did not
    exist yet, removed again on undo when empty).
    """

    def __init__(self, cache_dir: Path | None, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = (Path(cache_dir) / "undo") if cache_dir is not None else None
        self.task_dir: Path | None = None
        self._step = 0
        self._saved: set[str] = set()
        # Helper tabs edit their own files at the same time; the step counter is shared.
        self._lock = threading.Lock()

    def start_task(self) -> None:
        if self.root is None:
            return
        private_dir(self.root)
        self.task_dir = self.root / _task_stamp(self.root)
        self._step = 0
        self._saved = set()

    def disk_diff(self) -> str | None:
        """Unified diff of this task against the copies saved before the first edit.

        None when this task has no checkpoint (no cache). An empty string means
        every saved file still matches disk, including when nothing was saved.
        """
        if self.task_dir is None:
            return None
        if not self.task_dir.is_dir():
            return ""
        chunks: list[str] = []
        for step in sorted(item for item in self.task_dir.iterdir() if item.is_dir()):
            marker = step / "path.txt"
            if not marker.is_file():
                continue
            rel = marker.read_text(encoding="utf-8").strip()
            if not rel:
                continue
            target = _checkpoint_target(self.workspace, rel)
            before_path = step / "before"
            before = load_text(before_path).text if before_path.is_file() else ""
            after = load_text(target).text if target.is_file() else ""
            if before == after:
                continue
            chunks.append(hunk_diff(before, after, rel) or rel)
        return "\n".join(chunks)

    def save(self, path: Path) -> bool:
        """Copy ``path`` before its first change in this task; False when it was not copied.

        A path outside the workspace is saved by its absolute path, so undo
        covers it too (the tool only gets there after the user approved it).
        A file that usually holds secrets (.env, keys) is not copied: a second
        copy under the cache would outlive the edit. Undo leaves it as it is.
        """
        if self.task_dir is None:
            return False
        # realpath, like the resolved workspace: it follows links and, on Windows,
        # expands 8.3 short names (RUNNER~1), so a file inside is never stored as outside.
        target = Path(os.path.realpath(os.path.abspath(path)))
        if target.is_file() and sensitive_file(target):
            return False
        try:
            rel = target.relative_to(self.workspace).as_posix()
        except ValueError:
            rel = str(target)
        with self._lock:
            if rel in self._saved:
                return True
            self._saved.add(rel)
            self._step += 1
            step = self._step
        folder = self.task_dir / f"{step:03d}"
        private_dir(folder, windows_acl=False)  # inherits the undo root's ACL
        (folder / "path.txt").write_text(rel + "\n", encoding="utf-8")
        if target.is_file():
            shutil.copy2(target, folder / "before")
        else:
            missing: list[str] = []
            parent = target.parent
            while not parent.exists() and parent != parent.parent:
                missing.append(str(parent))
                parent = parent.parent
            if missing:
                (folder / "dirs.txt").write_text("\n".join(missing) + "\n", encoding="utf-8")
        self._prune()
        return True

    def _prune(self, keep: int = 20) -> None:
        if self.root is None or not self.root.is_dir():
            return
        tasks = sorted(item for item in self.root.iterdir() if item.is_dir())
        for stale in tasks[:-keep]:
            if stale == self.task_dir:
                continue
            shutil.rmtree(stale, ignore_errors=True)


def _checkpoint_target(workspace: Path, rel: str) -> Path:
    candidate = Path(rel)
    if candidate.is_absolute():
        return candidate
    return Path(workspace) / rel


def undo_last(cache_dir: Path, workspace: Path) -> list[str]:
    """Restore the files touched by the most recent task. Returns the paths.

    Files are restored atomically, files the task created are removed, and so
    are the folders created for them when they are empty again.
    """
    root = Path(cache_dir) / "undo"
    if not root.is_dir():
        return []
    tasks = sorted(item for item in root.iterdir() if item.is_dir())
    if not tasks:
        return []
    task = tasks[-1]
    restored: list[str] = []
    created_dirs: list[Path] = []
    for step in sorted(item for item in task.iterdir() if item.is_dir()):
        marker = step / "path.txt"
        if not marker.is_file():
            continue
        rel = marker.read_text(encoding="utf-8").strip()
        if not rel:
            continue
        target = _checkpoint_target(Path(workspace), rel)
        before = step / "before"
        if before.is_file():
            atomic_write_bytes(target, before.read_bytes())
            try:
                shutil.copystat(before, target)
            except OSError:
                pass
        elif target.is_file() or target.is_symlink():
            target.unlink()
        dirs = step / "dirs.txt"
        if dirs.is_file():
            created_dirs.extend(Path(line) for line in dirs.read_text(encoding="utf-8").splitlines() if line.strip())
        restored.append(rel)
    for folder in sorted(set(created_dirs), key=lambda item: -len(item.parts)):
        try:
            folder.rmdir()
        except OSError:
            pass
    shutil.rmtree(task, ignore_errors=True)
    return restored
