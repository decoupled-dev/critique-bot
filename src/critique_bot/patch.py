"""Cap, skip, and summarize huge / binary input so the chat UI does not crash."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from critique_bot import log

DEFAULT_MAX_PROMPT_CHARS = 120_000
DEFAULT_MAX_FILE_CHARS = 32_000
DEFAULT_MAX_FILES = 80
DEFAULT_MAX_READ_BYTES = 16_000_000

ABSOLUTE_MAX_PROMPT_CHARS = 400_000
ABSOLUTE_MAX_FILE_CHARS = 200_000
ABSOLUTE_MAX_FILES = 400
ABSOLUTE_MAX_READ_BYTES = 64_000_000

PREAMBLE_MAX_CHARS = 4_000
OMITTED_PATH_SAMPLE = 24
NOTE_RESERVE_CHARS = 1_500

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_GIT_HEADER_RE = re.compile(r"^diff --git ", re.M)
_INDEX_HEADER_RE = re.compile(r"^Index: ", re.M)
_UNIFIED_HEADER_RE = re.compile(r"^--- [^\n]+\n\+\+\+ ", re.M)
_BINARY_SECTION_RE = re.compile(
    r"^(?:GIT binary patch|Binary files .+ differ|Binary file .+ differs)\s*$",
    re.M,
)

_BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".ico",
        ".tif",
        ".tiff",
        ".pdf",
        ".zip",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".tar",
        ".woff",
        ".woff2",
        ".eot",
        ".ttf",
        ".otf",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".class",
        ".pyc",
        ".pyo",
        ".o",
        ".a",
        ".wasm",
        ".mp3",
        ".mp4",
        ".webm",
        ".mov",
        ".avi",
        ".mkv",
        ".wav",
        ".flac",
        ".sqlite",
        ".db",
        ".pkl",
        ".npy",
        ".pt",
        ".onnx",
        ".pb",
        ".jar",
        ".war",
        ".ear",
        ".dmg",
        ".iso",
        ".img",
        ".lockb",
    }
)


class InputError(ValueError):
    """Input could not be read or is not usable as text."""


@dataclass(frozen=True)
class InputLimits:
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    max_file_chars: int = DEFAULT_MAX_FILE_CHARS
    max_files: int = DEFAULT_MAX_FILES
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES


@dataclass
class SanitizeStats:
    original_chars: int = 0
    output_chars: int = 0
    files_seen: int = 0
    files_included: int = 0
    files_truncated: int = 0
    binaries_omitted: int = 0
    files_over_cap: int = 0
    skipped_attachments: int = 0
    truncated_read: bool = False
    omitted_paths: list[str] = field(default_factory=list)

    def merge(self, other: SanitizeStats) -> None:
        self.original_chars += other.original_chars
        self.output_chars += other.output_chars
        self.files_seen += other.files_seen
        self.files_included += other.files_included
        self.files_truncated += other.files_truncated
        self.binaries_omitted += other.binaries_omitted
        self.files_over_cap += other.files_over_cap
        self.skipped_attachments += other.skipped_attachments
        self.truncated_read = self.truncated_read or other.truncated_read
        self.omitted_paths.extend(other.omitted_paths)

    @property
    def did_sanitize(self) -> bool:
        return bool(
            self.binaries_omitted
            or self.files_truncated
            or self.files_over_cap
            or self.truncated_read
            or self.skipped_attachments
        )


@dataclass(frozen=True)
class LoadedInput:
    name: str
    text: str
    truncated_read: bool = False
    binary: bool = False
    size_bytes: int = 0


def strip_unsafe_controls(text: str) -> str:
    """Drop NULs and other C0 controls that can crash DOM/CDP payloads.

    Keeps tab, newline, and carriage return.
    """
    if not text:
        return text
    if _CONTROL_RE.search(text) is None:
        return text
    return _CONTROL_RE.sub("", text)


def looks_binary_path(path: str) -> bool:
    suffix = Path(path.split("\t", 1)[0].strip().strip('"')).suffix.lower()
    return suffix in _BINARY_EXTENSIONS


def looks_binary_bytes(data: bytes) -> bool:
    if not data:
        return False
    sample = data[:8192]
    head = sample.lstrip()
    if (
        head.startswith(b"diff --git")
        or head.startswith(b"--- ")
        or head.startswith(b"Index: ")
        or b"\n@@ " in sample
    ):
        return False
    if b"\x00" in sample:
        return True
    nontext = 0
    for byte in sample:
        if byte in (9, 10, 13):
            continue
        if byte < 32 or byte == 127:
            nontext += 1
    return (nontext / len(sample)) > 0.30


def looks_binary_text(text: str) -> bool:
    if not text:
        return False
    if "\x00" in text:
        return True
    sample = text[:8192]
    replacements = sample.count("\ufffd")
    return bool(sample) and (replacements / len(sample)) > 0.10


def looks_like_diff(text: str) -> bool:
    sample = text[:16384]
    if "diff --git " in sample or "GIT binary patch" in sample:
        return True
    if "\n@@ " in sample or sample.startswith("@@ "):
        return True
    if "\n--- " in sample and "\n+++ " in sample:
        return True
    if sample.startswith("--- ") and "\n+++ " in sample:
        return True
    return "Binary files " in sample


def _read_capped(handle, max_bytes: int) -> tuple[bytes, bool]:
    data = handle.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data, truncated


def _decode_bytes(data: bytes) -> str:
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _binary_stub(name: str, size: int) -> str:
    return f"[binary file omitted: {name} ({size} bytes)]\n"


def load_path(path: Path, limits: InputLimits, *, name: str | None = None) -> LoadedInput:
    file_path = Path(path)
    label = name if name is not None else str(file_path)
    if not file_path.is_file():
        raise InputError(f"file not found: {file_path}")
    size = file_path.stat().st_size
    if looks_binary_path(str(file_path)):
        log.info(f"skipping binary {label} ({size} bytes)")
        return LoadedInput(
            name=label,
            text=_binary_stub(label, size),
            binary=True,
            size_bytes=size,
        )
    with file_path.open("rb") as handle:
        data, truncated = _read_capped(handle, limits.max_read_bytes)
    if looks_binary_bytes(data):
        log.info(f"skipping binary content {label} ({size} bytes)")
        return LoadedInput(
            name=label,
            text=_binary_stub(label, size),
            truncated_read=truncated,
            binary=True,
            size_bytes=size,
        )
    if truncated:
        log.warn(
            f"{label} is {size} bytes; read only the first {limits.max_read_bytes} bytes"
        )
    text = strip_unsafe_controls(_decode_bytes(data))
    if looks_binary_text(text):
        log.info(f"skipping non-text {label} ({size} bytes)")
        return LoadedInput(
            name=label,
            text=_binary_stub(label, size),
            truncated_read=truncated,
            binary=True,
            size_bytes=size,
        )
    return LoadedInput(
        name=label,
        text=text,
        truncated_read=truncated,
        size_bytes=size,
    )


def load_stdin(limits: InputLimits) -> LoadedInput:
    data, truncated = _read_capped(sys.stdin.buffer, limits.max_read_bytes)
    if truncated:
        log.warn(
            f"stdin exceeded {limits.max_read_bytes} bytes; extra input was ignored"
        )
    if looks_binary_bytes(data):
        log.info(f"stdin looks binary ({len(data)} bytes); omitting contents")
        return LoadedInput(
            name="stdin",
            text=_binary_stub("stdin", len(data)),
            truncated_read=truncated,
            binary=True,
            size_bytes=len(data),
        )
    text = strip_unsafe_controls(_decode_bytes(data))
    if looks_binary_text(text):
        return LoadedInput(
            name="stdin",
            text=_binary_stub("stdin", len(data)),
            truncated_read=truncated,
            binary=True,
            size_bytes=len(data),
        )
    return LoadedInput(
        name="stdin",
        text=text,
        truncated_read=truncated,
        size_bytes=len(data),
    )


def cap_text(text: str, max_chars: int, *, what: str) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = f"\n\n[truncated: {what} hit the size cap, {len(text) - max_chars} chars omitted]\n"
    keep = max(max_chars - len(marker), 0)
    log.warn(f"{what} truncated from {len(text)} to {keep} chars")
    return text[:keep].rstrip() + marker


def _split_positions(text: str) -> list[int]:
    matches = list(_GIT_HEADER_RE.finditer(text))
    if matches:
        return [match.start() for match in matches]
    matches = list(_INDEX_HEADER_RE.finditer(text))
    if matches:
        return [match.start() for match in matches]
    matches = list(_UNIFIED_HEADER_RE.finditer(text))
    if matches:
        return [match.start() for match in matches]
    return []


def _iter_sections(text: str) -> list[tuple[str, str]]:
    positions = _split_positions(text)
    if not positions:
        return [("blob", text)]
    sections: list[tuple[str, str]] = []
    if positions[0] > 0:
        preamble = text[: positions[0]]
        if preamble.strip():
            sections.append(("preamble", preamble))
    for index, start in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(text)
        sections.append(("file", text[start:end]))
    return sections


def _path_from_section(section: str) -> str:
    for line in section.splitlines()[:16]:
        if line.startswith("diff --git "):
            rest = line[len("diff --git ") :].strip()
            if " b/" in rest:
                return rest.rsplit(" b/", 1)[-1].strip().strip('"')
            parts = rest.rsplit(" ", 1)
            if len(parts) == 2:
                path = parts[1]
                if path.startswith("b/"):
                    path = path[2:]
                return path.strip('"')
        if line.startswith("+++ "):
            path = line[4:].strip()
            if "\t" in path:
                path = path.split("\t", 1)[0]
            if path.startswith("b/") or path.startswith("w/"):
                path = path[2:]
            if path not in {"/dev/null", "nul", "/dev/null"}:
                return path.strip('"')
        if line.startswith("Index: "):
            return line[len("Index: ") :].strip()
    return "(unknown)"


_LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "cargo.lock",
        "composer.lock",
        "go.sum",
        "gemfile.lock",
    }
)
_SKIP_PATH_MARKERS = frozenset({"(unknown)", "/dev/null", "dev/null", "nul"})


def changed_file_paths(patch: str) -> list[str]:
    """New-side text paths from a unified diff, in patch order.

    Skips binaries, lockfiles, and deleted files (``/dev/null``).
    """
    seen: set[str] = set()
    paths: list[str] = []
    for kind, section in _iter_sections(patch):
        if kind != "file":
            continue
        path = _path_from_section(section)
        if not path or path in _SKIP_PATH_MARKERS:
            continue
        if path in seen:
            continue
        if "+++ /dev/null" in section.splitlines()[:16]:
            continue
        if _section_is_binary(section, path) or looks_binary_path(path):
            continue
        if Path(path).name.lower() in _LOCKFILE_NAMES:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def context_file_priority(path: str) -> tuple[int, str]:
    """Lower is loaded first when the prompt budget is tight."""
    name = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    if suffix in {".java", ".kt", ".kts", ".aidl"}:
        rank = 0
    elif name == "androidmanifest.xml":
        rank = 1
    elif suffix in {".xml", ".bp", ".mk"}:
        rank = 2
    elif suffix in {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".c", ".cc", ".cpp", ".h"}:
        rank = 3
    elif suffix in {".md", ".rst", ".markdown"}:
        rank = 9
    else:
        rank = 5
    return (rank, path)


def skip_file_context(path: str) -> bool:
    """Docs diffs are covered by the patch; do not paste full markdown bodies."""
    return Path(path).suffix.lower() in {".md", ".rst", ".markdown"}


def _section_is_binary(section: str, path: str) -> bool:
    if _BINARY_SECTION_RE.search(section):
        return True
    if "\x00" in section:
        return True
    if looks_binary_path(path) and "\n@@ " not in section and not section.startswith("@@ "):
        return True
    return False


def _truncate_section(section: str, max_chars: int) -> str:
    if len(section) <= max_chars:
        return section
    omitted = len(section) - max_chars
    marker = f"\n\n[truncated: {omitted} more chars omitted from this file]\n"
    keep = max(max_chars - len(marker), 0)
    newline = section.rfind("\n", 0, keep)
    if newline >= keep // 2:
        keep = newline
    return section[:keep].rstrip() + marker


def _sanitize_diff(
    name: str,
    text: str,
    limits: InputLimits,
    remaining_chars: int,
    remaining_files: int,
) -> tuple[str, SanitizeStats]:
    stats = SanitizeStats(original_chars=len(text), files_seen=0)
    parts: list[str] = []
    remaining = remaining_chars
    files_left = remaining_files

    for kind, section in _iter_sections(text):
        section = strip_unsafe_controls(section)
        if kind == "preamble":
            preamble = cap_text(section, min(PREAMBLE_MAX_CHARS, remaining), what="preamble")
            if preamble:
                parts.append(preamble if preamble.endswith("\n") else preamble + "\n")
                remaining -= len(parts[-1])
            continue

        stats.files_seen += 1
        path = _path_from_section(section)
        if _section_is_binary(section, path):
            stats.binaries_omitted += 1
            stats.omitted_paths.append(path)
            continue
        if files_left <= 0 or remaining < 240:
            stats.files_over_cap += 1
            stats.omitted_paths.append(path)
            continue

        file_cap = min(limits.max_file_chars, remaining)
        original_len = len(section)
        if original_len > file_cap:
            stats.files_truncated += 1
            section = _truncate_section(section, file_cap)
        if not section.endswith("\n"):
            section += "\n"
        parts.append(section)
        remaining -= len(section)
        files_left -= 1
        stats.files_included += 1

    body = "".join(parts)
    if not body.strip():
        body = (
            f"[no reviewable text in {name}: "
            "files were binary, empty, or dropped by size/file caps]\n"
        )
    stats.output_chars = len(body)
    return body, stats


def sanitize_one(
    name: str,
    text: str,
    limits: InputLimits,
    *,
    remaining_chars: int,
    remaining_files: int,
) -> tuple[str, SanitizeStats]:
    text = strip_unsafe_controls(text)
    if looks_like_diff(text):
        return _sanitize_diff(name, text, limits, remaining_chars, remaining_files)

    stats = SanitizeStats(original_chars=len(text), files_seen=1)
    if text.startswith("[binary file omitted:"):
        stats.binaries_omitted = 1
        stats.omitted_paths.append(name)
        stats.output_chars = len(text)
        return text, stats
    if looks_binary_path(name) or looks_binary_text(text):
        stats.binaries_omitted = 1
        stats.omitted_paths.append(name)
        stub = _binary_stub(name, len(text.encode("utf-8", errors="replace")))
        stats.output_chars = len(stub)
        return stub, stats

    cap = min(limits.max_file_chars, remaining_chars)
    if len(text) > cap:
        stats.files_truncated = 1
        text = cap_text(text, cap, what=name)
    stats.files_included = 1
    stats.output_chars = len(text)
    return text, stats


def sanitize_attachments(
    attachments: list[tuple[str, str]],
    limits: InputLimits,
    *,
    extra_overhead: int = 0,
) -> tuple[list[tuple[str, str]], SanitizeStats]:
    budget = max(limits.max_prompt_chars - max(extra_overhead, 0), 2_000)
    stats = SanitizeStats()
    files_left = limits.max_files
    remaining = budget
    out: list[tuple[str, str]] = []

    for name, raw in attachments:
        if remaining < 400 or files_left <= 0:
            stats.skipped_attachments += 1
            stats.omitted_paths.append(name)
            stats.original_chars += len(raw)
            continue
        piece, piece_stats = sanitize_one(
            name,
            raw,
            limits,
            remaining_chars=remaining,
            remaining_files=files_left,
        )
        stats.merge(piece_stats)
        if piece_stats.binaries_omitted and piece_stats.files_included == 0 and piece_stats.files_seen <= 1:
            continue
        if not piece.strip():
            continue
        out.append((name, piece))
        remaining -= len(piece)
        used_files = piece_stats.files_included if piece_stats.files_seen else 1
        files_left = max(files_left - max(used_files, 1), 0)

    if not out and attachments:
        out.append(
            (
                attachments[0][0],
                "[no reviewable text: files were binary, empty, or dropped by size/file caps]\n",
            )
        )
    stats.output_chars = sum(len(text) for _, text in out)
    return out, stats


def format_sanitize_note(stats: SanitizeStats) -> str:
    if not stats.did_sanitize:
        return ""
    lines = [
        "NOTE: Input was sanitized so a huge patch, hundreds of files, or binaries "
        "cannot crash the chat UI. Review only the included text.",
        (
            f"- size: {stats.original_chars:,} chars in -> "
            f"{stats.output_chars:,} chars sent"
        ),
    ]
    if stats.files_seen:
        lines.append(
            f"- files: {stats.files_seen} seen, {stats.files_included} included"
        )
    details: list[str] = []
    if stats.binaries_omitted:
        details.append(f"{stats.binaries_omitted} binary omitted")
    if stats.files_truncated:
        details.append(f"{stats.files_truncated} truncated")
    if stats.files_over_cap:
        details.append(f"{stats.files_over_cap} over file cap")
    if stats.skipped_attachments:
        details.append(f"{stats.skipped_attachments} attachment(s) skipped")
    if stats.truncated_read:
        details.append("source read was capped")
    if details:
        lines.append("- " + "; ".join(details))
    if stats.omitted_paths:
        sample = stats.omitted_paths[:OMITTED_PATH_SAMPLE]
        extra = len(stats.omitted_paths) - len(sample)
        shown = ", ".join(sample)
        if extra > 0:
            shown += f", … +{extra} more"
        lines.append(f"- omitted (sample): {shown}")
    return "\n".join(lines) + "\n"


def finalize_prompt(prompt: str, limits: InputLimits, stats: SanitizeStats) -> str:
    note = format_sanitize_note(stats)
    if note:
        log.warn(note.strip().replace("\n", " | "))
        prompt = note + "\n" + prompt
    if len(prompt) > limits.max_prompt_chars:
        log.warn(
            f"composed prompt still {len(prompt)} chars; "
            f"hard-capping to {limits.max_prompt_chars}"
        )
        prompt = cap_text(prompt, limits.max_prompt_chars, what="prompt")
    return prompt
