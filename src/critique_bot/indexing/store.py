# SPDX-License-Identifier: Apache-2.0
"""The index database: connection, schema, migrations, and where each database lives.

The schema version is an integer in ``meta``. A database at an older version is migrated in
place by the steps in :data:`MIGRATIONS`; a database from a newer crit, or one that cannot be
read, is deleted and rebuilt (it is a cache). Later stages add tables with
:func:`register_migration` instead of editing the steps here.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Callable

#: Version 2 is the code catalog of crit 1.0.x (files, symbols, refs, content). Version 3 adds
#: the layers of the indexing engine.
LEGACY_VERSION = 2

#: Name of a project's database inside ``.crit/cache``.
DB_NAME = "index.db"
#: The name crit 1.0.x used; removed when the new database is created next to it.
LEGACY_NAME = "index.sqlite"
#: Folder (next to the project database) that holds one database per module group.
GROUP_DIR = "index"

_BASE = """
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

_V3 = """
CREATE TABLE IF NOT EXISTS edges (
    src_path TEXT NOT NULL,
    src_symbol TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,              -- imports | calls | inherits
    dst_name TEXT NOT NULL,          -- as written: com.a.B, ./util, Base, render
    dst_path TEXT,                   -- imports: the resolved file, NULL when outside the index
    line INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_path);
CREATE INDEX IF NOT EXISTS idx_edges_name ON edges(kind, dst_name);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_path);
CREATE TABLE IF NOT EXISTS strings (
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- literal | log | tag | resource | property | selinux
    value TEXT NOT NULL,
    tag TEXT NOT NULL DEFAULT '',    -- log tag, resource name, property key
    container TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_strings_path ON strings(path);
CREATE INDEX IF NOT EXISTS idx_strings_tag ON strings(tag);
CREATE TABLE IF NOT EXISTS modules (
    name TEXT NOT NULL,
    system TEXT NOT NULL,            -- soong | gradle | cmake | bazel
    type TEXT NOT NULL DEFAULT '',
    build_file TEXT NOT NULL,
    dir TEXT NOT NULL,
    srcs TEXT NOT NULL DEFAULT '[]', -- JSON list
    deps TEXT NOT NULL DEFAULT '[]'  -- JSON list
);
CREATE INDEX IF NOT EXISTS idx_modules_name ON modules(name);
CREATE INDEX IF NOT EXISTS idx_modules_build ON modules(build_file);
CREATE INDEX IF NOT EXISTS idx_modules_dir ON modules(dir);
CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS commit_files (sha TEXT NOT NULL, path TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_commit_files_path ON commit_files(path);
CREATE INDEX IF NOT EXISTS idx_commit_files_sha ON commit_files(sha);
CREATE TABLE IF NOT EXISTS cochange (
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    count INTEGER NOT NULL,
    last_ts INTEGER NOT NULL,
    PRIMARY KEY (a, b)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_cochange_b ON cochange(b);
CREATE TABLE IF NOT EXISTS blame_cache (
    path TEXT NOT NULL,
    hash TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (path, hash, start, end)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS glossary (
    term TEXT NOT NULL,
    expansion TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'project',  -- project | learned (Stage 8b) | user
    provenance TEXT NOT NULL DEFAULT '',     -- file+hash, commit or session id
    updated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (term, expansion)
) WITHOUT ROWID;
"""


def _create_base(conn: sqlite3.Connection) -> None:
    conn.executescript(_BASE)
    fts = "0"
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS content USING fts5(path UNINDEXED, body, tokenize='trigram')"
        )
        fts = "1"
    except sqlite3.OperationalError:
        fts = "0"
    set_meta(conn, "fts", fts)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _to_v3(conn: sqlite3.Connection) -> None:
    have = _columns(conn, "files")
    if "hash" not in have:
        conn.execute("ALTER TABLE files ADD COLUMN hash TEXT NOT NULL DEFAULT ''")
    if "module" not in have:
        # the nearest build file above the file (Android.bp, build.gradle, CMakeLists.txt, BUILD)
        conn.execute("ALTER TABLE files ADD COLUMN module TEXT NOT NULL DEFAULT ''")
    conn.executescript(_V3)
    commit_fts = "0"
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS commit_text USING fts5(sha UNINDEXED, text)")
        commit_fts = "1"
    except sqlite3.OperationalError:
        commit_fts = "0"
    set_meta(conn, "commit_fts", commit_fts)
    # A 1.0.x catalog has no hashes, edges or strings: every file is parsed again.
    conn.execute("DELETE FROM files")
    conn.execute("DELETE FROM symbols")
    conn.execute("DELETE FROM refs")
    if get_meta(conn, "fts") == "1":
        conn.execute("DELETE FROM content")


#: version -> step that brings a database from version-1 to version. Step 2 creates the base.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    LEGACY_VERSION: _create_base,
    3: _to_v3,
}


def register_migration(version: int, step: Callable[[sqlite3.Connection], None]) -> None:
    """Add a schema step (later stages: their own tables). Versions must be new and increasing."""
    if version in MIGRATIONS or version <= max(MIGRATIONS):
        raise ValueError(f"schema version {version} is taken or not newer than {max(MIGRATIONS)}")
    MIGRATIONS[version] = step


def current_version() -> int:
    return max(MIGRATIONS)


def connect(path: Path) -> sqlite3.Connection:
    """A connection in WAL mode, so readers keep working while a refresh writes."""
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    return conn


def version(conn: sqlite3.Connection) -> int:
    """The schema version, 0 for an empty database, -1 when it cannot be read."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        return -1
    if row is None:
        return 0
    try:
        return int(str(row[0]))
    except ValueError:
        return -1


def is_current(conn: sqlite3.Connection) -> bool:
    return version(conn) == current_version()


def migrate(conn: sqlite3.Connection) -> bool:
    """Bring the schema up to date. False when the database must be rebuilt instead."""
    found = version(conn)
    target = current_version()
    if found < 0 or found > target or (0 < found < LEGACY_VERSION):
        return False
    for step_version in sorted(MIGRATIONS):
        if step_version <= found:
            continue
        MIGRATIONS[step_version](conn)
        set_meta(conn, "schema", str(step_version))
        conn.commit()
    return True


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return default if row is None else str(row[0])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def remove_db(path: Path) -> None:
    """Delete a database and its WAL sidecar files."""
    for item in (Path(path), Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        item.unlink(missing_ok=True)


def remove_legacy(index_path: Path) -> None:
    """Delete crit 1.0.x's ``index.sqlite`` next to a new ``index.db``."""
    index_path = Path(index_path)
    if index_path.name == DB_NAME:
        legacy = index_path.with_name(LEGACY_NAME)
        if legacy.exists():
            remove_db(legacy)


def group_slug(group: str) -> str:
    """A file name for a module group: ``packages/services/Car`` -> ``packages__services__Car``."""
    cleaned = group.strip("/").replace("\\", "/")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", cleaned.replace("/", "__")) or "_root"


def group_db_path(index_path: Path, group: str) -> Path:
    """Where the database of one module group lives, next to the project database."""
    return Path(index_path).parent / GROUP_DIR / f"{group_slug(group)}.db"


def group_dbs(index_path: Path) -> list[Path]:
    """The module-group databases created so far."""
    folder = Path(index_path).parent / GROUP_DIR
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.db"))
