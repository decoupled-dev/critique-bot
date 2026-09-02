from __future__ import annotations

from pathlib import Path

SKIP_ALWAYS = {".git", ".idea", ".svn", "node_modules", "__pycache__", ".cxx"}
SKIP_GENERATED = {"build", ".gradle", "generated", "out", "captures"}

DEFAULT_EXTENSIONS = {".java", ".kt"}


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
    files: list[Path] = []
    root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in exts:
            continue
        if any(part in skip for part in path.parts):
            continue
        files.append(path)
    files.sort()
    return files


def relative_posix(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()
