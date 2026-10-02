"""Symbol catalog written at ``bot-agent init``.

Stdlib only. Python uses ``ast``. Other languages use a small name pattern.
The catalog is a lookup aid for ``search_code``; file contents stay on disk.
"""

from __future__ import annotations

import ast
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from critique_bot.patch import looks_binary_bytes, looks_binary_path

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".bot",
        "node_modules",
        "venv",
        ".venv",
        "env",
        "__pycache__",
        "dist",
        "build",
        "target",
        "out",
        ".gradle",
        ".edge-profile",
        ".repo",
        "prebuilts",
        ".cxx",
        "intermediates",
        ".externalNativeBuild",
        "captures",
        ".idea",
        "bazel-bin",
        "bazel-out",
        "bazel-testlogs",
        "cmake-build-debug",
        "cmake-build-release",
    }
)

_MAX_FILE_BYTES = 1_000_000

_LANG_BY_SUFFIX = {
    ".py": "python",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}

_CLASS_RE = re.compile(
    r"\b(?:class|interface|enum|record|struct|trait|object|type)\s+([A-Za-z_]\w*)"
)
_FUNC_RE = {
    "java": re.compile(
        r"^\s*(?:(?:public|private|protected|static|final|synchronized|native|abstract|default)\s+)+"
        r"(?!class\b|interface\b|enum\b|record\b)[\w.<>,\[\]]+\s+([A-Za-z_]\w*)\s*\(",
        re.M,
    ),
    "kotlin": re.compile(r"^\s*(?:(?:public|private|protected|internal|open|override|suspend)\s+)*fun\s+([A-Za-z_]\w*)\s*\(", re.M),
    "go": re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(", re.M),
    "rust": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*\(", re.M),
    "c": re.compile(
        r"^\s*(?:[\w*]+\s+)+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{",
        re.M,
    ),
    "cpp": re.compile(
        r"^\s*(?:[\w:*&<>]+\s+)+([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const\s*)?\{",
        re.M,
    ),
    "csharp": re.compile(
        r"^\s*(?:(?:public|private|protected|internal|static|async|virtual|override|sealed)\s+)+"
        r"(?!class\b|interface\b|enum\b|struct\b|record\b)[\w.<>,\[\]]+\s+([A-Za-z_]\w*)\s*\(",
        re.M,
    ),
    "javascript": re.compile(
        r"(?:function\s+([A-Za-z_]\w*)\s*\(|(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\()",
    ),
    "typescript": re.compile(
        r"(?:function\s+([A-Za-z_]\w*)\s*\(|(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\()",
    ),
}


@dataclass(frozen=True)
class IndexStats:
    files: int
    symbols: int


def rebuild_index(workspace: Path, index_path: Path) -> IndexStats:
    """Replace the catalog with a fresh walk of ``workspace``."""
    workspace = Path(workspace).resolve()
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if index_path.exists():
        index_path.unlink()
    conn = _connect(index_path)
    try:
        _create_schema(conn)
        files = 0
        symbols = 0
        for path, rel in _walk(workspace):
            added = _index_file(conn, path, rel)
            if added is None:
                continue
            files += 1
            symbols += added
        conn.commit()
    finally:
        conn.close()
    return IndexStats(files=files, symbols=symbols)


def refresh_path(workspace: Path, index_path: Path, path: Path) -> None:
    """Reparse one file after a write, edit, or delete. Missing files are dropped."""
    workspace = Path(workspace).resolve()
    index_path = Path(index_path)
    if not index_path.is_file():
        return
    target = Path(path)
    if not target.is_absolute():
        target = workspace / target
    try:
        target = target.resolve()
        rel = target.relative_to(workspace).as_posix()
    except (OSError, ValueError):
        return
    conn = _connect(index_path)
    try:
        _create_schema(conn)
        conn.execute("DELETE FROM symbols WHERE path = ?", (rel,))
        conn.execute("DELETE FROM files WHERE path = ?", (rel,))
        if target.is_file():
            _index_file(conn, target, rel)
        conn.commit()
    finally:
        conn.close()


def search_symbols(
    index_path: Path,
    pattern: str,
    *,
    limit: int,
    path_prefix: str = "",
) -> list[tuple[str, int, str, str]]:
    """Return ``(path, line, kind, name)`` for symbol names matching ``pattern``."""
    index_path = Path(index_path)
    if not index_path.is_file() or limit <= 0:
        return []
    try:
        matcher = re.compile(pattern)
    except re.error:
        return []
    prefix = path_prefix.strip("./").replace("\\", "/")
    conn = _connect(index_path)
    try:
        rows = conn.execute(
            "SELECT path, line, kind, name FROM symbols ORDER BY path, line"
        ).fetchall()
    finally:
        conn.close()
    hits: list[tuple[str, int, str, str]] = []
    for row in rows:
        rel = str(row["path"])
        if prefix and rel != prefix and not rel.startswith(prefix.rstrip("/") + "/"):
            continue
        name = str(row["name"])
        if matcher.search(name) is None:
            continue
        hits.append((rel, int(row["line"]), str(row["kind"]), name))
        if len(hits) >= limit:
            break
    return hits


def _connect(index_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(index_path))
    conn.row_factory = sqlite3.Row
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            language TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS symbols (
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            line INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
        """
    )


def _walk(workspace: Path):
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                rel = path.relative_to(workspace).as_posix()
            except ValueError:
                continue
            yield path, rel


def _index_file(conn: sqlite3.Connection, path: Path, rel: str) -> int | None:
    if looks_binary_path(rel):
        return None
    try:
        size = path.stat().st_size
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return None
    if size > _MAX_FILE_BYTES:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if looks_binary_bytes(data):
        return None
    text = data.decode("utf-8", "replace")
    language = _LANG_BY_SUFFIX.get(path.suffix.lower(), "text")
    conn.execute(
        "INSERT OR REPLACE INTO files (path, mtime_ns, size, language) VALUES (?, ?, ?, ?)",
        (rel, mtime_ns, size, language),
    )
    symbols = _symbols(text, language)
    conn.executemany(
        "INSERT INTO symbols (name, kind, path, line) VALUES (?, ?, ?, ?)",
        [(name, kind, rel, line) for name, kind, line in symbols],
    )
    return len(symbols)


def _symbols(text: str, language: str) -> list[tuple[str, str, int]]:
    if language == "python":
        return _python_symbols(text)
    found: list[tuple[str, str, int]] = []
    for match in _CLASS_RE.finditer(text):
        found.append((match.group(1), "class", _line_of(text, match.start())))
    func_re = _FUNC_RE.get(language)
    if func_re is None:
        return found
    for match in func_re.finditer(text):
        name = next((group for group in match.groups() if group), None)
        if not name or name in {"if", "for", "while", "switch", "catch"}:
            continue
        found.append((name, "function", _line_of(text, match.start())))
    return found


def _python_symbols(text: str) -> list[tuple[str, str, int]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    found: list[tuple[str, str, int]] = []

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            found.append((node.name, "class", node.lineno))
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            found.append((node.name, "function", node.lineno))
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            found.append((node.name, "function", node.lineno))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1
