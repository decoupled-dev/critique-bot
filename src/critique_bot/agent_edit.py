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
    # The terminator of each line as found on disk (``\r\n``, ``\n`` or ``\r``).
    # None when the file was not loaded from disk.
    endings: list[str] | None = None


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

    A symbolic link is written through to the file it points at, not replaced.
    A new file gets the usual permissions for new files (0666 minus umask).
    """
    target = Path(target)
    if target.is_symlink():
        target = Path(os.path.realpath(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    handle, temp_name = tempfile.mkstemp(prefix=".bot-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(handle, "wb") as temp:
            temp.write(payload)
        try:
            if existed:
                shutil.copymode(target, temp_name)
            else:
                os.chmod(temp_name, 0o666 & ~_umask())
        except OSError:
            pass
        _replace_with_retry(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


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
    """Pre-edit copies under ``.bot/cache/undo/<task>/``, newest task last.

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
        self.root.mkdir(parents=True, exist_ok=True)
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

    def save(self, path: Path) -> None:
        """Copy ``path`` before its first change in this task.

        A path outside the workspace is saved by its absolute path, so undo
        covers it too (the tool only gets there after the user approved it).
        """
        if self.task_dir is None:
            return
        target = Path(os.path.abspath(path))
        if target.is_symlink():
            target = Path(os.path.realpath(target))
        try:
            rel = target.relative_to(self.workspace).as_posix()
        except ValueError:
            rel = str(target)
        with self._lock:
            if rel in self._saved:
                return
            self._saved.add(rel)
            self._step += 1
            step = self._step
        folder = self.task_dir / f"{step:03d}"
        folder.mkdir(parents=True, exist_ok=True)
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
