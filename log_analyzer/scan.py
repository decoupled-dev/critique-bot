from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

SKIP_ALWAYS = {".git", ".idea", ".svn", "node_modules", "__pycache__", ".cxx"}
SKIP_GENERATED = {"build", ".gradle", "generated", "out", "captures"}

DEFAULT_EXTENSIONS = {".java", ".kt"}


def normalize_user_path(raw: str) -> Path:
    """Accept Linux paths, quoted paths, trailing slashes, and file:// URLs."""
    text = (raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    text = text.replace("\r", "")
    if text.startswith("file://"):
        parsed = urlparse(text)
        text = unquote(parsed.path or "")
    return Path(text).expanduser()


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
