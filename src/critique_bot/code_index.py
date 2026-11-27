"""Code catalog in the project's index database (``.crit/cache/index.db``).

Stdlib only by default. ``tree_sitter`` plus a language wheel is used when it
is importable, otherwise a regex pass per language. The catalog holds:

``files``    path, mtime, size, language, content hash, the nearest build file. Used to
             refresh only changed files and as the file list for ``find_files``.
``symbols``  definitions with start and end line, enclosing parent, and the
             signature line. ``read_files`` can read one symbol by name.
``refs``     identifier uses per file with a count. Feeds the repo map.
``content``  FTS5 trigram index of file text when SQLite has FTS5. It narrows
             a regex search to the files that contain its literal part.

The same refresh also writes the indexing engine's layers (imports, calls, inheritance,
strings, build modules; see :mod:`critique_bot.indexing`). The schema and its migrations
live in :mod:`critique_bot.indexing.store`; this module keeps its public API.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from critique_bot import log
from critique_bot.indexing import store as _store_mod
from critique_bot.indexing.langs import language_of
from critique_bot.indexing.symbols import Symbol, extract_symbols  # noqa: F401 (public API)
from critique_bot.patch import looks_binary_bytes, looks_binary_path

SCHEMA_VERSION = str(_store_mod.current_version())

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".crit",
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


@dataclass(frozen=True)
class IndexStats:
    files: int
    symbols: int
    changed: int = 0
    removed: int = 0


def rebuild_index(workspace: Path, index_path: Path) -> IndexStats:
    """Replace the catalog with a fresh walk of ``workspace``."""
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_db(index_path)
    return refresh_index(workspace, index_path)


def _remove_db(index_path: Path) -> None:
    """Delete the database and its WAL sidecar files."""
    _store_mod.remove_db(index_path)


def _open_current(index_path: Path) -> sqlite3.Connection:
    """A connection to an up-to-date schema: migrated in place, or rebuilt when it cannot be."""
    conn = _connect(index_path)
    try:
        if _store_mod.migrate(conn):
            return conn
    except sqlite3.DatabaseError as exc:
        log.debug(f"index {index_path} unreadable, rebuilding: {exc}")
    conn.close()
    _remove_db(index_path)
    conn = _connect(index_path)
    _store_mod.migrate(conn)
    return conn


def _file_hash(path: Path) -> str:
    try:
        return hashlib.blake2b(path.read_bytes(), digest_size=16).hexdigest()
    except OSError:
        return ""


def refresh_index(workspace: Path, index_path: Path, *, only: Iterable[str] | None = None) -> IndexStats:
    """Reparse only files that changed; drop missing files.

    A file counts as changed when its mtime or size differs; when only the mtime moved, its
    content hash decides (a checkout or ``touch`` that kept the bytes costs no parse). Files
    that ``git diff`` names since the last indexed commit are hash-checked even when their
    mtime and size look the same.

    On a ``repo``-tool tree (AOSP) only the projects in the index's *scope* are walked:
    the ones the agent has worked in (:func:`add_scope`). Indexing all of AOSP would take
    hours and many GB; text search covers the rest. ``only`` limits this refresh to some
    folders (workspace-relative), for example the one project just added.

    Outdated catalogs are migrated (a 1.0.x catalog is parsed again in full). The walk uses
    ``git ls-files`` when the workspace is a checkout so ignored files are skipped.
    """
    workspace = Path(workspace).resolve()
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    _store_mod.remove_legacy(index_path)
    conn = _open_current(index_path)
    try:
        known = {
            str(row["path"]): (int(row["mtime_ns"]), int(row["size"]), str(row["hash"] or ""))
            for row in conn.execute("SELECT path, mtime_ns, size, hash FROM files")
        }
        prefixes = _refresh_prefixes(conn, workspace, only)
        if prefixes is not None:
            known = {rel: value for rel, value in known.items() if _under(rel, prefixes)}
        suspects = _git_changed(workspace, conn, prefixes)
        seen: set[str] = set()
        jobs: list[tuple[Path, str, int, int]] = []
        touched: list[tuple[int, str]] = []
        own = {str(index_path.resolve()) + suffix for suffix in ("", "-wal", "-shm", "-journal")}
        for path, rel in _walk(workspace, prefixes):
            if looks_binary_path(rel) or str(path) in own:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            seen.add(rel)
            if stat.st_size > _MAX_FILE_BYTES:
                continue
            previous = known.get(rel)
            if previous is not None and previous[1] == stat.st_size:
                if previous[0] == stat.st_mtime_ns and rel not in suspects:
                    continue
                if previous[2] and _file_hash(path) == previous[2]:
                    if previous[0] != stat.st_mtime_ns:
                        touched.append((stat.st_mtime_ns, rel))
                    continue
            jobs.append((path, rel, stat.st_mtime_ns, stat.st_size))
        if touched:
            conn.executemany("UPDATE files SET mtime_ns = ? WHERE path = ?", touched)
        # A repo tree is searched with rg (or a scan); trigram text of whole AOSP
        # projects would make the index many GB.
        lean = prefixes is not None
        fts = _has_fts(conn) and not lean
        changed = 0
        changed_paths: list[str] = []
        build_changed = False
        for rel, mtime_ns, size, parsed in _parse_many(jobs):
            if rel in known:  # new files have nothing to forget (the FTS delete scans the table)
                build_changed |= _forget(conn, rel)
            if parsed is None:
                continue
            # refs only feed the PageRank map, which a scoped index does not use; on
            # frameworks/base they were 10M rows and most of the database
            build_changed |= _store(conn, workspace, rel, mtime_ns, size, parsed, fts, refs_too=not lean)
            changed += 1
            changed_paths.append(rel)
            if changed % 500 == 0:  # short write transactions, so other writers are not starved
                conn.commit()
        removed = 0
        for rel in known:
            if rel not in seen:
                build_changed |= _forget(conn, rel)
                removed += 1
        added = any(rel not in known for rel in changed_paths)
        _link(conn, changed_paths, everything=bool(removed or added), build_changed=build_changed)
        _note_head(workspace, conn, prefixes)
        conn.commit()
        total_files = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        total_symbols = int(conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
    finally:
        conn.close()
    return IndexStats(files=total_files, symbols=total_symbols, changed=changed, removed=removed)


def _link(conn: sqlite3.Connection, changed: list[str], *, everything: bool, build_changed: bool) -> None:
    """Resolve imports and owning build files after a refresh.

    Adding or removing a file can change what other files' imports resolve to, so then
    every import is resolved again (dictionary lookups; fast); otherwise only the changed
    files' own imports. The result equals a full rebuild either way.
    """
    from critique_bot.indexing import graph, modules

    if everything:
        graph.resolve_imports(conn)
    elif changed:
        graph.resolve_imports(conn, sources=changed)
    if build_changed:
        modules.assign(conn)
    elif changed:
        modules.assign(conn, changed)


def _git_project(workspace: Path, prefixes: list[str] | None) -> Path | None:
    if prefixes is None:
        return workspace if (workspace / ".git").exists() else None
    if len(prefixes) == 1 and prefixes[0] and (workspace / prefixes[0] / ".git").exists():
        return workspace / prefixes[0]
    return None


def _git_out(folder: Path, *args: str, timeout: float = 20) -> str | None:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    try:
        proc = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True,
                              errors="replace", timeout=timeout, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _git_changed(workspace: Path, conn: sqlite3.Connection, prefixes: list[str] | None) -> set[str]:
    """Files ``git diff`` names between the last indexed commit and HEAD (workspace-relative)."""
    folder = _git_project(workspace, prefixes)
    last = _store_mod.get_meta(conn, "head")
    if folder is None or not last:
        return set()
    head = (_git_out(folder, "rev-parse", "HEAD") or "").strip()
    if not head or head == last:
        return set()
    names = _git_out(folder, "diff", "--name-only", "--no-renames", last, head)
    if names is None:
        return set()
    base = "" if folder == workspace else folder.relative_to(workspace).as_posix()
    return {f"{base}/{name}" if base else name for name in names.splitlines() if name}


def _note_head(workspace: Path, conn: sqlite3.Connection, prefixes: list[str] | None) -> None:
    folder = _git_project(workspace, prefixes)
    if folder is None:
        return
    head = (_git_out(folder, "rev-parse", "HEAD") or "").strip()
    if head:
        _store_mod.set_meta(conn, "head", head)


def _under(rel: str, prefixes: list[str]) -> bool:
    return any(not prefix or rel == prefix or rel.startswith(prefix + "/") for prefix in prefixes)


def _refresh_prefixes(conn: sqlite3.Connection, workspace: Path, only: Iterable[str] | None) -> list[str] | None:
    """Folders a refresh walks: ``only``, the scope of a repo tree, or None for everything."""
    if only is not None:
        return [item.strip("/") for item in only]
    from critique_bot import file_catalog

    if file_catalog.catalog_for(workspace).kind != "repo":
        return None
    return scope(conn)


def scope(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = 'scope'").fetchone()
    if row is None:
        return []
    try:
        items = json.loads(str(row[0]))
    except ValueError:
        return []
    return [str(item) for item in items if isinstance(item, str)]


def indexed_projects(index_path: Path) -> list[str]:
    """The projects of a repo tree that the index covers (empty when it covers everything)."""
    if not Path(index_path).is_file():
        return []
    conn = _connect(Path(index_path))
    try:
        return scope(conn) if _schema_current(conn) else []
    finally:
        conn.close()


def add_scope(workspace: Path, index_path: Path, folder: str) -> IndexStats | None:
    """Index one more project of a repo tree, and keep it in later refreshes.

    Returns None when ``folder`` was already in the scope or the index is missing.
    """
    index_path = Path(index_path)
    folder = folder.strip("/")
    if not index_path.is_file() or not folder:
        return None
    conn = _connect(index_path)
    try:
        if not _schema_current(conn):
            return None
        current = scope(conn)
        if folder in current:
            return None
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('scope', ?)",
            (json.dumps(sorted({*current, folder})),),
        )
        conn.commit()
    finally:
        conn.close()
    return refresh_index(workspace, index_path, only=[folder])


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
        existed = conn.execute("SELECT 1 FROM files WHERE path = ?", (rel,)).fetchone() is not None
        build_changed = _forget(conn, rel)
        stored = None
        if target.is_file() and not looks_binary_path(rel):
            try:
                stat = target.stat()
            except OSError:
                stat = None
            if stat is not None and stat.st_size <= _MAX_FILE_BYTES:
                stored = _index_file(conn, workspace, target, rel, stat.st_mtime_ns, stat.st_size)
        build_changed |= bool(stored)
        # a new or deleted file can change what other files' imports resolve to
        _link(conn, [rel] if stored is not None else [], everything=existed != (stored is not None),
              build_changed=build_changed)
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
        if not _schema_current(conn) or not _has_fts(conn) or scope(conn):
            return None  # a scoped (repo tree) index has no text for most files
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
        count = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        if not count:
            return ""
        if count > LARGE_MAP_FILES or scope(conn):
            return _repo_map_large(conn, keywords, budget_chars)
        files = [str(row[0]) for row in conn.execute("SELECT path FROM files")]
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


# Above this many files the PageRank map is too slow for every task (minutes on
# frameworks/base); the map is then built from keyword matches in SQL instead.
LARGE_MAP_FILES = 20_000
_LARGE_MAP_WORDS = 6
_LARGE_MAP_FILES_SHOWN = 40


def _repo_map_large(conn: sqlite3.Connection, keywords: Iterable[str], budget_chars: int) -> str:
    """Map for a big index: files whose path or symbols match the task's words.

    Without task words there is no useful ranking of tens of thousands of files, so
    the map is empty and the model searches instead.
    """
    words = sorted({word.lower() for word in keywords if len(word) >= 4}, key=len, reverse=True)[:_LARGE_MAP_WORDS]
    if not words:
        return ""
    score: dict[str, float] = {}
    for word in words:
        like = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        for (path,) in conn.execute("SELECT path FROM files WHERE path LIKE ? ESCAPE '\\' LIMIT 2000", (like,)):
            score[str(path)] = score.get(str(path), 0.0) + 3.0
        for path, hits in conn.execute(
            "SELECT path, COUNT(*) FROM symbols WHERE name LIKE ? ESCAPE '\\' GROUP BY path ORDER BY COUNT(*) DESC LIMIT 2000",
            (like,),
        ):
            score[str(path)] = score.get(str(path), 0.0) + min(float(hits), 10.0)
    ordered = sorted(score, key=lambda rel: (-score[rel], len(rel), rel))[:_LARGE_MAP_FILES_SHOWN]
    lines: list[str] = []
    used = 0
    for rel in ordered:
        items = [
            _symbol_from_row(row)
            for row in conn.execute(
                "SELECT path, line, end_line, kind, name, parent, signature FROM symbols WHERE path = ? ORDER BY line LIMIT 13",
                (rel,),
            )
        ]
        block = [rel + ":"]
        for item in items[:12]:
            label = item.signature.strip() or item.qualified
            block.append(f"  {item.line} {label[:100]}")
        if len(items) > 12:
            block.append("  ...")
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
    return _store_mod.connect(index_path)


def _schema_current(conn: sqlite3.Connection) -> bool:
    return _store_mod.is_current(conn)


def _has_fts(conn: sqlite3.Connection) -> bool:
    try:
        return _store_mod.get_meta(conn, "fts") == "1"
    except sqlite3.DatabaseError:
        return False


def _forget(conn: sqlite3.Connection, rel: str) -> bool:
    """Drop everything indexed for ``rel``. True when it was a build file (modules changed)."""
    conn.execute("DELETE FROM symbols WHERE path = ?", (rel,))
    conn.execute("DELETE FROM refs WHERE path = ?", (rel,))
    conn.execute("DELETE FROM files WHERE path = ?", (rel,))
    conn.execute("DELETE FROM edges WHERE src_path = ?", (rel,))
    conn.execute("UPDATE edges SET dst_path = NULL WHERE dst_path = ?", (rel,))
    conn.execute("DELETE FROM strings WHERE path = ?", (rel,))
    if _has_fts(conn):
        conn.execute("DELETE FROM content WHERE path = ?", (rel,))
    gone = conn.execute("DELETE FROM modules WHERE build_file = ?", (rel,)).rowcount
    return bool(gone)


def _walk(workspace: Path, prefixes: list[str] | None = None):
    from critique_bot import file_catalog  # imports this module for SKIP_DIR_NAMES

    catalog = file_catalog.catalog_for(workspace)
    if catalog.kind != "walk" or prefixes is not None:
        listed = catalog.files() if prefixes is None else [rel for prefix in prefixes for rel in catalog.files(prefix)]
        for rel in listed:
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


def _parse_file(path: str, size: int) -> tuple | None:
    """Read and parse one file: (language, symbols, refs, fts text or None, edges, strings,
    hash). Picklable, so a process pool can run it; the database writes stay in the parent."""
    from critique_bot.indexing import facts

    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if looks_binary_bytes(data):
        return None
    digest = hashlib.blake2b(data, digest_size=16).hexdigest()
    text = data.decode("utf-8", "replace")
    language = language_of(path)
    try:
        found = facts.parse_text(text, language, path)
    except (RecursionError, ValueError, MemoryError) as exc:  # a pathological file: keep it, unparsed
        log.debug(f"could not parse {path}: {exc}")
        found = facts.Facts(language)
    symbols = found.symbols
    rows = [(item.name, item.kind, item.line, item.end_line, item.parent, item.signature) for item in symbols]
    defined = {item.name for item in symbols}
    refs = [(name, line, count) for name, (line, count) in _references(text, language, defined).items()]
    return (language, rows, refs, text if size <= _MAX_FTS_BYTES else None, found.edges, found.strings, digest)


def _store(
    conn: sqlite3.Connection,
    workspace: Path,
    rel: str,
    mtime_ns: int,
    size: int,
    parsed: tuple,
    fts: bool,
    refs_too: bool = True,
) -> bool:
    """Write one parsed file. True when it is a build file (its modules were stored)."""
    from critique_bot.indexing import modules

    language, rows, refs, text, edges, strings, digest = parsed
    conn.execute(
        "INSERT OR REPLACE INTO files (path, mtime_ns, size, language, hash, module) VALUES (?, ?, ?, ?, ?, '')",
        (rel, mtime_ns, size, language, digest),
    )
    conn.executemany(
        "INSERT INTO symbols (name, kind, path, line, end_line, parent, signature) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(name, kind, rel, line, end, parent, sig) for name, kind, line, end, parent, sig in rows],
    )
    if refs_too:
        conn.executemany(
            "INSERT INTO refs (name, path, line, count) VALUES (?, ?, ?, ?)",
            [(name, rel, line, count) for name, line, count in refs],
        )
    conn.executemany(
        "INSERT INTO edges (src_path, src_symbol, kind, dst_name, dst_path, line) VALUES (?, ?, ?, ?, NULL, ?)",
        [(rel, src_symbol, kind, dst, line) for kind, src_symbol, dst, line in edges],
    )
    conn.executemany(
        "INSERT INTO strings (path, line, kind, value, tag, container) VALUES (?, ?, ?, ?, ?, ?)",
        [(rel, line, kind, value, tag, container) for line, kind, value, tag, container in strings],
    )
    if fts and text is not None:
        conn.execute("INSERT INTO content (path, body) VALUES (?, ?)", (rel, text))
    if modules.is_build_file(rel) and text is not None:
        try:
            modules.store(conn, rel, modules.parse_build_file(workspace, rel, text))
        except (OSError, ValueError, RecursionError) as exc:
            log.debug(f"could not read the modules of {rel}: {exc}")
        return True
    return False


def _index_file(
    conn: sqlite3.Connection,
    workspace: Path,
    path: Path,
    rel: str,
    mtime_ns: int,
    size: int,
) -> bool | None:
    parsed = _parse_file(str(path), size)
    if parsed is None:
        return None
    scoped = bool(scope(conn))
    return _store(conn, workspace, rel, mtime_ns, size, parsed, _has_fts(conn) and not scoped, refs_too=not scoped)


# Parse in worker processes when at least this many files changed.
PARALLEL_PARSE_MIN = 200


def _parse_many(jobs: list[tuple[Path, str, int, int]]):
    """Yield (rel, mtime_ns, size, parsed) for each job, in parallel when there are many."""
    if len(jobs) >= PARALLEL_PARSE_MIN and (os.cpu_count() or 1) > 1:
        try:
            from concurrent.futures import ProcessPoolExecutor

            workers = max(1, min(8, (os.cpu_count() or 2) - 1))
            with ProcessPoolExecutor(max_workers=workers) as pool:
                results = pool.map(_parse_file, [str(job[0]) for job in jobs], [job[3] for job in jobs], chunksize=64)
                for job, parsed in zip(jobs, results):
                    yield job[1], job[2], job[3], parsed
            return
        except (OSError, RuntimeError, ImportError) as exc:  # no processes here: parse in-line
            log.debug(f"parallel parse unavailable: {exc}")
    for path, rel, mtime_ns, size in jobs:
        yield rel, mtime_ns, size, _parse_file(str(path), size)


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
