from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

SKIP_ALWAYS = {".git", ".idea", ".svn", "node_modules", "__pycache__", ".cxx"}
SKIP_GENERATED = {"build", ".gradle", "generated", "out", "captures"}

DEFAULT_EXTENSIONS = {".java", ".kt"}

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_RE = re.compile(r"^\\\\[^\\]+\\")


def is_windows() -> bool:
    return os.name == "nt"


def looks_like_windows_path(raw: str) -> bool:
    text = _decode_file_url(strip_user_quotes(raw))
    return bool(_DRIVE_RE.match(text) or _UNC_RE.match(text) or text.startswith("\\\\"))


def strip_user_quotes(raw: str) -> str:
    text = (raw or "").strip().replace("\r", "")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    # PowerShell: "C:\Users\me\MyApp\" leaves a trailing backslash
    if len(text) > 3 and text.endswith("\\") and not text.endswith("\\\\"):
        if _DRIVE_RE.match(text) or text.startswith("\\\\"):
            text = text.rstrip("\\")
    return text


def _decode_file_url(text: str) -> str:
    if not text.startswith("file://"):
        return text
    parsed = urlparse(text.replace("\\", "/"))
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc.lower() not in {"localhost", ""}:
        path = f"//{parsed.netloc}{path}"
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return path


def _drive_rest(text: str) -> tuple[str, str] | None:
    if not _DRIVE_RE.match(text):
        return None
    return text[0].lower(), text[2:].replace("\\", "/").lstrip("/")


def candidate_paths(raw: str) -> list[Path]:
    """Native Windows, Git Bash, and WSL spellings of the same folder."""
    text = _decode_file_url(strip_user_quotes(raw))
    seen: list[Path] = []

    def add(value: str) -> None:
        path = Path(value).expanduser()
        if path not in seen:
            seen.append(path)

    add(text)
    if is_windows():
        add(text.replace("/", "\\"))
    mapped = _drive_rest(text)
    if mapped:
        drive, rest = mapped
        add(f"{drive.upper()}:/{rest}")
        add(f"/{drive}/{rest}")
        add(f"/mnt/{drive}/{rest}")
        add(f"/cygdrive/{drive}/{rest}")
    return seen


def normalize_user_path(raw: str) -> Path:
    """Accept POSIX and PowerShell paths. Prefer a candidate that exists."""
    candidates = candidate_paths(raw)
    for path in candidates:
        try:
            if path.exists():
                return path
        except OSError:
            continue
    return candidates[0]


def _should_skip(root: Path, path: Path, skip: set[str]) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        relative = path
    return any(part in skip for part in relative.parts)


def iter_source_files(
    root: Path,
    *,
    include_generated: bool = False,
    extensions: set[str] | None = None,
) -> list[Path]:
    exts = extensions or DEFAULT_EXTENSIONS
    skip = set(SKIP_ALWAYS)
    if not include_generated:
        skip |= SKIP_GENERATED
    root = root.resolve()
    files: list[Path] = []
    if root.is_file():
        if root.suffix.lower() in exts:
            files.append(root)
        return files
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in exts:
            continue
        if _should_skip(root, path, skip):
            continue
        files.append(path)
    files.sort()
    return files


def relative_posix(root: Path, path: Path) -> str:
    root = root.resolve()
    path = path.resolve()
    if root.is_file():
        if path == root:
            return path.name
        root = root.parent
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
