"""Code catalog under ``.bot/cache/index.sqlite``.

Stdlib only by default. ``tree_sitter`` plus a language wheel is used when it
is importable, otherwise a regex pass per language. The catalog holds:

``files``    path, mtime, size, language. Used to refresh only changed files
             and as the file list for ``find_files``.
``symbols``  definitions with start and end line, enclosing parent, and the
             signature line. ``read_files`` can read one symbol by name.
``refs``     identifier uses per file with a count. Feeds the repo map.
``content``  FTS5 trigram index of file text when SQLite has FTS5. It narrows
             a regex search to the files that contain its literal part.
"""

from __future__ import annotations

import ast
import math
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from critique_bot.patch import looks_binary_bytes, looks_binary_path

SCHEMA_VERSION = "2"

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
        ".codegraph",
        "graphify-out",
        "bazel-bin",
        "bazel-out",
        "bazel-testlogs",
        "cmake-build-debug",
        "cmake-build-release",
    }
)

_MAX_FILE_BYTES = 1_000_000
_MAX_FTS_BYTES = 400_000

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
    "kotlin": re.compile(
        r"^\s*(?:(?:public|private|protected|internal|open|override|suspend|inline|operator)\s+)*"
        r"fun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?([A-Za-z_]\w*)\s*\(",
        re.M,
    ),
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

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_KEYWORDS = frozenset(
    """
    abstract and as assert async await boolean break byte case catch char class
    const continue def default del delete do double elif else enum except export
    extends final finally float for from fun func function global goto if impl
    implements import in inline instanceof int interface internal is lambda let
    long match mod module mut native new nil none nonlocal not null object of open
    operator or override package pass private protected pub public raise record
    return sealed self short static struct super suspend switch synchronized this
    throw throws trait transient true false try type typedef typeof union unsafe
    use val var virtual void volatile when where while with yield string int bool
    """.split()
)

# Tree-sitter node types that define a symbol, per language. ``name`` is the
# field read from that node. Anything missing falls back to the regex pass.
_TS_MODULES = {
    "python": "tree_sitter_python",
    "java": "tree_sitter_java",
    "kotlin": "tree_sitter_kotlin",
    "go": "tree_sitter_go",
    "rust": "tree_sitter_rust",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
    "csharp": "tree_sitter_c_sharp",
    "javascript": "tree_sitter_javascript",
    "typescript": "tree_sitter_typescript",
}
_TS_DEFS: dict[str, dict[str, str]] = {
    "python": {"class_definition": "class", "function_definition": "function"},
    "java": {
        "class_declaration": "class",
        "interface_declaration": "class",
        "enum_declaration": "class",
        "record_declaration": "class",
        "annotation_type_declaration": "class",
        "method_declaration": "function",
        "constructor_declaration": "function",
    },
    "kotlin": {
        "class_declaration": "class",
        "object_declaration": "class",
        "function_declaration": "function",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "function",
        "type_spec": "class",
    },
    "rust": {
        "function_item": "function",
        "struct_item": "class",
        "enum_item": "class",
        "trait_item": "class",
        "impl_item": "class",
    },
    "c": {"function_definition": "function", "struct_specifier": "class"},
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "class",
    },
    "csharp": {
        "class_declaration": "class",
        "interface_declaration": "class",
        "struct_declaration": "class",
        "enum_declaration": "class",
        "record_declaration": "class",
        "method_declaration": "function",
        "constructor_declaration": "function",
    },
    "javascript": {
        "class_declaration": "class",
        "function_declaration": "function",
        "method_definition": "function",
    },
    "typescript": {
        "class_declaration": "class",
        "interface_declaration": "class",
        "function_declaration": "function",
        "method_definition": "function",
    },
}
_ts_cache: dict[str, object] = {}


@dataclass(frozen=True)
class IndexStats:
    files: int
    symbols: int
    changed: int = 0
    removed: int = 0


@dataclass(frozen=True)
class Symbol:
    path: str
    line: int
    end_line: int
    kind: str
    name: str
    parent: str
    signature: str

    @property
    def qualified(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


def rebuild_index(workspace: Path, index_path: Path) -> IndexStats:
    """Replace the catalog with a fresh walk of ``workspace``."""
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if index_path.exists():
        index_path.unlink()
    return refresh_index(workspace, index_path)


def refresh_index(workspace: Path, index_path: Path) -> IndexStats:
    """Reparse only files whose mtime or size changed; drop missing files.

    Missing or outdated catalogs are rebuilt in full. The walk uses ``git
    ls-files`` when the workspace is a checkout so ignored files are skipped.
    """
    workspace = Path(workspace).resolve()
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(index_path)
    try:
        if not _schema_current(conn):
            conn.close()
            index_path.unlink(missing_ok=True)
            conn = _connect(index_path)
            _create_schema(conn)
        known = {
            str(row["path"]): (int(row["mtime_ns"]), int(row["size"]))
            for row in conn.execute("SELECT path, mtime_ns, size FROM files")
        }
        seen: set[str] = set()
        changed = 0
        symbols = 0
        for path, rel in _walk(workspace):
            if looks_binary_path(rel):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            seen.add(rel)
            if stat.st_size > _MAX_FILE_BYTES:
                continue
            previous = known.get(rel)
            if previous == (stat.st_mtime_ns, stat.st_size):
                continue
            _forget(conn, rel)
            added = _index_file(conn, path, rel, stat.st_mtime_ns, stat.st_size)
            if added is None:
                continue
            changed += 1
            symbols += added
        removed = 0
        for rel in known:
            if rel not in seen:
                _forget(conn, rel)
                removed += 1
        conn.commit()
        total_files = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        total_symbols = int(conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
    finally:
        conn.close()
    return IndexStats(files=total_files, symbols=total_symbols, changed=changed, removed=removed)


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
        if not _schema_current(conn):
            return
        _forget(conn, rel)
        if target.is_file() and not looks_binary_path(rel):
            try:
                stat = target.stat()
            except OSError:
                stat = None
            if stat is not None and stat.st_size <= _MAX_FILE_BYTES:
                _index_file(conn, target, rel, stat.st_mtime_ns, stat.st_size)
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
    return [
        (item.path, item.line, item.kind, item.name)
        for item in find_symbols(index_path, pattern, limit=limit, path_prefix=path_prefix)
    ]


def find_symbols(
    index_path: Path,
    pattern: str,
    *,
    limit: int,
    path_prefix: str = "",
) -> list[Symbol]:
    """Symbols whose name matches ``pattern``.

    ``pattern`` is a regular expression tried case-insensitively. A plain word
    also matches by subtokens, so ``fooBar`` finds ``foo_bar`` and ``FooBar``.
    A ``Parent.name`` query matches the qualified name.
    """
    index_path = Path(index_path)
    if not index_path.is_file() or limit <= 0:
        return []
    try:
        matcher = re.compile(pattern, re.IGNORECASE)
    except re.error:
        matcher = None
    normalized = _normalize_ident(pattern)
    like = _longest_literal(pattern)
    prefix = _clean_prefix(path_prefix)
    sql = "SELECT path, line, end_line, kind, name, parent, signature FROM symbols"
    where: list[str] = []
    params: list[object] = []
    if like and len(like) >= 3 and not normalized:
        where.append("lower(name) LIKE ?")
        params.append(f"%{like.lower()}%")
    if prefix:
        where.append("(path = ? OR path LIKE ?)")
        params.extend([prefix, prefix + "/%"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY path, line"
    conn = _connect(index_path)
    try:
        if not _schema_current(conn):
            return []
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    exact: list[Symbol] = []
    loose: list[Symbol] = []
    for row in rows:
        item = _symbol_from_row(row)
        name = item.name
        if matcher is not None and (matcher.search(name) or matcher.search(item.qualified)):
            exact.append(item)
        elif normalized and normalized in _normalize_ident(item.qualified):
            loose.append(item)
        if len(exact) >= limit:
            break
    return (exact + loose)[:limit]


def symbol_at(index_path: Path, path: str, name: str) -> Symbol | None:
    """The symbol called ``name`` (or ``Parent.name``) inside ``path``."""
    index_path = Path(index_path)
    if not index_path.is_file():
        return None
    parent, _, short = name.rpartition(".")
    conn = _connect(index_path)
    try:
        if not _schema_current(conn):
            return None
        rows = conn.execute(
            "SELECT path, line, end_line, kind, name, parent, signature FROM symbols "
            "WHERE path = ? AND name = ? ORDER BY line",
            (path, short),
        ).fetchall()
    finally:
        conn.close()
    items = [_symbol_from_row(row) for row in rows]
    if parent:
        items = [item for item in items if item.parent == parent]
    return items[0] if items else None


def close_symbols(index_path: Path, name: str, *, limit: int = 5) -> list[str]:
    """Names that look like ``name``, for a "did you mean" hint."""
    import difflib

    index_path = Path(index_path)
    if not index_path.is_file():
        return []
    conn = _connect(index_path)
    try:
        if not _schema_current(conn):
            return []
        names = [str(row[0]) for row in conn.execute("SELECT DISTINCT name FROM symbols")]
    finally:
        conn.close()
    lowered = {item.lower(): item for item in names}
    hits = difflib.get_close_matches(name.lower(), list(lowered), n=limit, cutoff=0.72)
    return [lowered[item] for item in hits]


def outline(index_path: Path, path: str, *, limit: int = 120) -> list[Symbol]:
    """Top-level and nested definitions of one file in line order."""
    index_path = Path(index_path)
    if not index_path.is_file():
        return []
    conn = _connect(index_path)
    try:
        if not _schema_current(conn):
            return []
        rows = conn.execute(
            "SELECT path, line, end_line, kind, name, parent, signature FROM symbols "
            "WHERE path = ? ORDER BY line LIMIT ?",
            (path, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_symbol_from_row(row) for row in rows]


def candidate_files(
    index_path: Path,
    literal: str,
    *,
    path_prefix: str = "",
) -> list[str] | None:
    """Files whose text contains ``literal``, from the FTS5 index.

    Returns None when FTS5 is unavailable or ``literal`` is too short to be
    selective, so the caller scans the whole file list instead.
    """
    index_path = Path(index_path)
    if not index_path.is_file() or len(literal) < 3:
        return None
    prefix = _clean_prefix(path_prefix)
    conn = _connect(index_path)
    try:
        if not _schema_current(conn) or not _has_fts(conn):
            return None
        query = '"' + literal.replace('"', '""') + '"'
        try:
            rows = conn.execute(
                "SELECT path FROM content WHERE content MATCH ? ORDER BY path",
                (query,),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
    finally:
        conn.close()
    found = [str(row[0]) for row in rows]
    if prefix:
        found = [rel for rel in found if rel == prefix or rel.startswith(prefix + "/")]
    return found


def indexed_files(index_path: Path, *, path_prefix: str = "") -> list[tuple[str, int, int]]:
    """``(path, mtime_ns, size)`` for every indexed file, newest first."""
    index_path = Path(index_path)
    if not index_path.is_file():
        return []
    prefix = _clean_prefix(path_prefix)
    conn = _connect(index_path)
    try:
        if not _schema_current(conn):
            return []
        if prefix:
            rows = conn.execute(
                "SELECT path, mtime_ns, size FROM files WHERE path = ? OR path LIKE ? "
                "ORDER BY mtime_ns DESC",
                (prefix, prefix + "/%"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT path, mtime_ns, size FROM files ORDER BY mtime_ns DESC"
            ).fetchall()
    finally:
        conn.close()
    return [(str(row[0]), int(row[1]), int(row[2])) for row in rows]


def repo_map(
    index_path: Path,
    *,
    keywords: Iterable[str] = (),
    budget_chars: int = 3000,
) -> str:
    """Ranked summary of the files and symbols that matter most.

    Files form a graph: an edge runs from a file that references a name to
    each file that defines it. PageRank over that graph, personalized by
    files that mention the task keywords, picks the files. Their symbols
    fill the budget.
    """
    index_path = Path(index_path)
    if not index_path.is_file():
        return ""
    conn = _connect(index_path)
    try:
        if not _schema_current(conn):
            return ""
        files = [str(row[0]) for row in conn.execute("SELECT path FROM files")]
        if not files:
            return ""
        definers: dict[str, list[str]] = {}
        symbols_by_file: dict[str, list[Symbol]] = {}
        for row in conn.execute(
            "SELECT path, line, end_line, kind, name, parent, signature FROM symbols ORDER BY path, line"
        ):
            item = _symbol_from_row(row)
            definers.setdefault(item.name, []).append(item.path)
            symbols_by_file.setdefault(item.path, []).append(item)
        refs = conn.execute("SELECT name, path, count FROM refs").fetchall()
    finally:
        conn.close()
    index_of = {rel: number for number, rel in enumerate(files)}
    size = len(files)
    edges: dict[int, dict[int, float]] = {}
    for name, source, count in refs:
        targets = definers.get(str(name))
        if not targets or str(source) not in index_of:
            continue
        src = index_of[str(source)]
        weight = float(count) / math.sqrt(len(targets))
        for target in targets:
            dst = index_of.get(target)
            if dst is None or dst == src:
                continue
            edges.setdefault(src, {})
            edges[src][dst] = edges[src].get(dst, 0.0) + weight
    words = [word.lower() for word in keywords if len(word) >= 3]
    personal = [0.0] * size
    for rel, number in index_of.items():
        haystack = rel.lower() + " " + " ".join(
            item.name.lower() for item in symbols_by_file.get(rel, [])
        )
        score = sum(1.0 for word in words if word in haystack)
        personal[number] = score
    total_personal = sum(personal)
    if total_personal <= 0:
        personal = [1.0 / size] * size
    else:
        personal = [(value + 0.05) / (total_personal + 0.05 * size) for value in personal]
    rank = _pagerank(size, edges, personal)
    ordered = sorted(range(size), key=lambda number: (-rank[number], files[number]))
    lines: list[str] = []
    used = 0
    for number in ordered:
        rel = files[number]
        items = symbols_by_file.get(rel, [])
        if not items and not words:
            continue
        header = rel + ":"
        block = [header]
        for item in items[:12]:
            label = item.signature.strip() or item.qualified
            block.append(f"  {item.line} {label[:100]}")
        if len(items) > 12:
            block.append(f"  ... {len(items) - 12} more")
        text = "\n".join(block)
        if used + len(text) + 1 > budget_chars:
            if lines:
                break
            text = text[: max(0, budget_chars - 1)]
        lines.append(text)
        used += len(text) + 1
    return "\n".join(lines)


def split_ident(name: str) -> list[str]:
    """``fooBarBaz`` and ``foo_bar_baz`` both give ``['foo', 'bar', 'baz']``."""
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
    return [part.lower() for part in re.split(r"[\s_\-.]+", parts) if part]


def _normalize_ident(text: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.\- ]+", text or "") or not text.strip():
        return ""
    return "".join(split_ident(text))


def _longest_literal(pattern: str) -> str:
    """The longest run of plain characters every match must contain.

    Empty when the pattern has an alternation, because then no single
    literal is required.
    """
    if re.search(r"(?<!\\)\|", pattern):
        return ""
    best = ""
    current: list[str] = []
    escaped = False
    depth = 0
    for char in pattern:
        if escaped:
            escaped = False
            if char.isalnum() or char in "_ ":
                current.append(char)
                continue
            current = []
            continue
        if char == "\\":
            escaped = True
            continue
        if char in "[(":
            depth += 1
            current = []
            continue
        if char in "])":
            depth = max(0, depth - 1)
            current = []
            continue
        if depth or char in ".*+?|{}^$":
            if char in "*+?{" and current:
                current.pop()
            if "".join(current) and len("".join(current)) > len(best):
                best = "".join(current)
            current = []
            continue
        current.append(char)
    if len("".join(current)) > len(best):
        best = "".join(current)
    return best.strip()


def longest_literal(pattern: str) -> str:
    return _longest_literal(pattern)


def _clean_prefix(path_prefix: str) -> str:
    return path_prefix.replace("\\", "/").strip("./").rstrip("/")


def _pagerank(
    size: int,
    edges: dict[int, dict[int, float]],
    personal: list[float],
    *,
    damping: float = 0.85,
    rounds: int = 25,
) -> list[float]:
    rank = list(personal)
    out_weight = {src: sum(targets.values()) for src, targets in edges.items()}
    for _ in range(rounds):
        fresh = [(1.0 - damping) * personal[number] for number in range(size)]
        dangling = 0.0
        for number in range(size):
            if number not in out_weight or out_weight[number] <= 0:
                dangling += rank[number]
        for src, targets in edges.items():
            share = damping * rank[src] / out_weight[src]
            for dst, weight in targets.items():
                fresh[dst] += share * weight
        if dangling:
            spread = damping * dangling
            for number in range(size):
                fresh[number] += spread * personal[number]
        rank = fresh
    return rank


def _symbol_from_row(row: sqlite3.Row) -> Symbol:
    return Symbol(
        path=str(row["path"]),
        line=int(row["line"]),
        end_line=int(row["end_line"] or row["line"]),
        kind=str(row["kind"]),
        name=str(row["name"]),
        parent=str(row["parent"] or ""),
        signature=str(row["signature"] or ""),
    )


def _connect(index_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(index_path))
    conn.row_factory = sqlite3.Row
    return conn


def _schema_current(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None and str(row[0]) == SCHEMA_VERSION


def _has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = 'fts'").fetchone()
    return row is not None and str(row[0]) == "1"


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
            line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            parent TEXT NOT NULL DEFAULT '',
            signature TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
        CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
        CREATE TABLE IF NOT EXISTS refs (
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            line INTEGER NOT NULL,
            count INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_refs_name ON refs(name);
        CREATE INDEX IF NOT EXISTS idx_refs_path ON refs(path);
        """
    )
    fts = "0"
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS content USING fts5("
            "path UNINDEXED, body, tokenize='trigram')"
        )
        fts = "1"
    except sqlite3.OperationalError:
        fts = "0"
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)", (SCHEMA_VERSION,))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('fts', ?)", (fts,))
    conn.commit()


def _forget(conn: sqlite3.Connection, rel: str) -> None:
    conn.execute("DELETE FROM symbols WHERE path = ?", (rel,))
    conn.execute("DELETE FROM refs WHERE path = ?", (rel,))
    conn.execute("DELETE FROM files WHERE path = ?", (rel,))
    if _has_fts(conn):
        conn.execute("DELETE FROM content WHERE path = ?", (rel,))


def _walk(workspace: Path):
    listed = _git_files(workspace)
    if listed is not None:
        for rel in listed:
            parts = rel.split("/")
            if any(part in SKIP_DIR_NAMES for part in parts[:-1]):
                continue
            path = workspace / rel
            if path.is_file():
                yield path, rel
        return
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                rel = path.relative_to(workspace).as_posix()
            except ValueError:
                continue
            yield path, rel


def _git_files(workspace: Path) -> list[str] | None:
    if not (workspace / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", "replace")
    return sorted({item for item in raw.split("\0") if item})


def _index_file(
    conn: sqlite3.Connection,
    path: Path,
    rel: str,
    mtime_ns: int,
    size: int,
) -> int | None:
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
    symbols = extract_symbols(text, language)
    conn.executemany(
        "INSERT INTO symbols (name, kind, path, line, end_line, parent, signature) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (item.name, item.kind, rel, item.line, item.end_line, item.parent, item.signature)
            for item in symbols
        ],
    )
    defined = {item.name for item in symbols}
    conn.executemany(
        "INSERT INTO refs (name, path, line, count) VALUES (?, ?, ?, ?)",
        [
            (name, rel, line, count)
            for name, (line, count) in _references(text, language, defined).items()
        ],
    )
    if _has_fts(conn) and size <= _MAX_FTS_BYTES:
        conn.execute("INSERT INTO content (path, body) VALUES (?, ?)", (rel, text))
    return len(symbols)


def extract_symbols(text: str, language: str) -> list[Symbol]:
    """Definitions in ``text``. Path is left empty; the caller fills it."""
    if language == "python":
        found = _python_symbols(text)
        if found:
            return found
    if language in _TS_MODULES:
        found = _treesitter_symbols(text, language)
        if found is not None:
            return found
    return _regex_symbols(text, language)


def _regex_symbols(text: str, language: str) -> list[Symbol]:
    found: list[Symbol] = []
    if language == "text":
        return found
    lines = text.split("\n")
    brace = language in {"java", "kotlin", "go", "rust", "c", "cpp", "csharp", "javascript", "typescript"}
    for match in _CLASS_RE.finditer(text):
        line = _line_of(text, match.start())
        end = _brace_end(text, match.end(), line) if brace else line
        found.append(
            Symbol("", line, end, "class", match.group(1), "", lines[line - 1].strip())
        )
    func_re = _FUNC_RE.get(language)
    if func_re is not None:
        for match in func_re.finditer(text):
            name = next((group for group in match.groups() if group), None)
            if not name or name in {"if", "for", "while", "switch", "catch", "return"}:
                continue
            line = _line_of(text, match.start())
            end = _brace_end(text, match.end(), line) if brace else line
            parent = _enclosing(found, line)
            found.append(Symbol("", line, end, "function", name, parent, lines[line - 1].strip()))
    found.sort(key=lambda item: item.line)
    return found


def _enclosing(classes: list[Symbol], line: int) -> str:
    inner = ""
    for item in classes:
        if item.kind == "class" and item.line < line <= item.end_line:
            inner = item.name
    return inner


def _brace_end(text: str, start: int, line: int) -> int:
    """Line of the brace that closes the first ``{`` at or after ``start``."""
    open_at = text.find("{", start)
    newline = text.find("\n", start)
    if open_at < 0 or (newline >= 0 and open_at > newline + 200):
        return line
    depth = 0
    index = open_at
    length = len(text)
    while index < length:
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return _line_of(text, index)
        elif char in "\"'":
            quote = char
            index += 1
            while index < length and text[index] != quote:
                if text[index] == "\\":
                    index += 1
                if text[index : index + 1] == "\n":
                    break
                index += 1
        elif char == "/" and text[index : index + 2] == "//":
            index = text.find("\n", index)
            if index < 0:
                break
        elif char == "/" and text[index : index + 2] == "/*":
            index = text.find("*/", index + 2)
            if index < 0:
                break
            index += 1
        index += 1
    return line


def _python_symbols(text: str) -> list[Symbol]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.split("\n")
    found: list[Symbol] = []

    def visit(node: ast.AST, parent: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                found.append(
                    Symbol(
                        "",
                        child.lineno,
                        int(getattr(child, "end_lineno", child.lineno) or child.lineno),
                        "class",
                        child.name,
                        parent,
                        lines[child.lineno - 1].strip(),
                    )
                )
                visit(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                line = child.lineno
                found.append(
                    Symbol(
                        "",
                        line,
                        int(getattr(child, "end_lineno", line) or line),
                        "function",
                        child.name,
                        parent,
                        lines[line - 1].strip(),
                    )
                )
                visit(child, parent)
            else:
                visit(child, parent)

    visit(tree, "")
    found.sort(key=lambda item: item.line)
    return found


def _treesitter_language(language: str):
    if language in _ts_cache:
        return _ts_cache[language]
    result = None
    module_name = _TS_MODULES.get(language)
    if module_name:
        try:
            import importlib

            from tree_sitter import Language, Parser  # type: ignore

            module = importlib.import_module(module_name)
            if language == "typescript":
                lang = Language(module.language_typescript())
            else:
                lang = Language(module.language())
            result = (Parser(lang), lang)
        except Exception:
            result = None
    _ts_cache[language] = result
    return result


def _treesitter_symbols(text: str, language: str) -> list[Symbol] | None:
    loaded = _treesitter_language(language)
    if loaded is None:
        return None
    parser, _lang = loaded
    kinds = _TS_DEFS.get(language, {})
    try:
        tree = parser.parse(text.encode("utf-8"))
    except Exception:
        return None
    lines = text.split("\n")
    found: list[Symbol] = []

    def name_of(node) -> str:
        field = node.child_by_field_name("name")
        if field is None and node.type in {"impl_item"}:
            field = node.child_by_field_name("type")
        if field is None and node.type == "function_definition":
            declarator = node.child_by_field_name("declarator")
            while declarator is not None and declarator.type not in {"identifier", "field_identifier", "qualified_identifier"}:
                nxt = declarator.child_by_field_name("declarator")
                if nxt is None:
                    break
                declarator = nxt
            field = declarator
        if field is None:
            return ""
        try:
            return field.text.decode("utf-8", "replace")
        except Exception:
            return ""

    def visit(node, parent: str) -> None:
        for child in node.children:
            kind = kinds.get(child.type)
            if kind:
                name = name_of(child)
                if name:
                    line = child.start_point[0] + 1
                    end = child.end_point[0] + 1
                    found.append(
                        Symbol("", line, end, kind, name, parent, lines[line - 1].strip() if line - 1 < len(lines) else name)
                    )
                    visit(child, name if kind == "class" else parent)
                    continue
            visit(child, parent)

    visit(tree.root_node, "")
    if not found:
        return None
    found.sort(key=lambda item: item.line)
    return found


def _references(text: str, language: str, defined: set[str]) -> dict[str, tuple[int, int]]:
    """Identifier uses that are not defined in this file: name -> (first line, count)."""
    del language
    found: dict[str, tuple[int, int]] = {}
    for number, line in enumerate(text.split("\n"), start=1):
        for match in _IDENT_RE.finditer(line):
            name = match.group(0)
            if name in defined or name.lower() in _KEYWORDS or name.isdigit():
                continue
            if name.isupper() and len(name) <= 3:
                continue
            first, count = found.get(name, (number, 0))
            found[name] = (first, count + 1)
    if len(found) > 4000:
        top = sorted(found.items(), key=lambda item: -item[1][1])[:4000]
        found = dict(top)
    return found


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1
