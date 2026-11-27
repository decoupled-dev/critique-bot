# SPDX-License-Identifier: Apache-2.0
"""``.crit/memory/memory.db``: the project memory store.

Unlike the code index (a cache in ``.crit/cache``), memory is not rebuilt from the code alone:
episodes and approved conventions come from past sessions. So the schema migrates in place and
is never dropped. Every row carries **provenance** (``file:<path>@<hash>``, ``commit:<sha>``,
``session:<id>``, ``import:<file>``); a write without one is refused.

Display ids are a letter and the row id: ``f12`` a file card, ``m3`` a module card, ``r4`` a
change recipe, ``h9`` a hot spot, ``e7`` an episode, ``n2`` a convention (a note), ``g5`` a
glossary entry. ``crit memory show <id>`` and the ``memory`` tool take them.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

#: Name of the folder inside ``.crit`` and of the database in it.
DIR_NAME = "memory"
DB_NAME = "memory.db"

SCHEMA = 1

_TABLES = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                  -- file | module
    key TEXT NOT NULL,                   -- workspace-relative path, or the module name
    hash TEXT NOT NULL DEFAULT '',       -- the file's sha256 (module: of its build file and row)
    text TEXT NOT NULL,
    symbols TEXT NOT NULL DEFAULT '[]',  -- JSON: the real names the card mentions
    builder TEXT NOT NULL DEFAULT 'extractive',
    provenance TEXT NOT NULL,
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    UNIQUE (kind, key)
);
CREATE TABLE IF NOT EXISTS change_sets (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,            -- the sorted files, one per line
    files TEXT NOT NULL,                 -- JSON list
    size INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    last_ts INTEGER NOT NULL DEFAULT 0,
    commits TEXT NOT NULL DEFAULT '[]',  -- JSON: newest first, capped
    subjects TEXT NOT NULL DEFAULT '[]',
    sessions TEXT NOT NULL DEFAULT '[]',
    provenance TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_sets_count ON change_sets(count);
CREATE TABLE IF NOT EXISTS change_set_files (set_id INTEGER NOT NULL, path TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_change_set_files_path ON change_set_files(path);
CREATE TABLE IF NOT EXISTS hotspots (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    fixes INTEGER NOT NULL DEFAULT 0,    -- commits with fix|bug|crash... in the subject
    commits INTEGER NOT NULL DEFAULT 0,  -- all commits (churn)
    last_fix_ts INTEGER NOT NULL DEFAULT 0,
    fix_commits TEXT NOT NULL DEFAULT '[]',
    provenance TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    task TEXT NOT NULL,
    class TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'positive',  -- positive | negative (tried, not the cause)
    outcome TEXT NOT NULL DEFAULT '',       -- verified | accepted | completed | failed | abandoned
    trusted INTEGER NOT NULL DEFAULT 1,     -- 0: written from a tainted session; not recalled by default
    files_changed TEXT NOT NULL DEFAULT '[]',
    symbols TEXT NOT NULL DEFAULT '[]',
    root_cause TEXT NOT NULL DEFAULT '',
    fix_summary TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '{}',
    session TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT '',
    files_hash TEXT NOT NULL DEFAULT '{}',
    provenance TEXT NOT NULL,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conventions (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | rejected | stale
    session TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL,
    created INTEGER NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''             -- why it was flagged stale or rejected
);
CREATE TABLE IF NOT EXISTS glossary (
    term TEXT NOT NULL,
    expansion TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0.8,
    provenance TEXT NOT NULL,
    created INTEGER NOT NULL,
    PRIMARY KEY (term, expansion)
);
"""

_FTS = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(task, root_cause, fix_summary, symbols, files)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(key, text)",
)

#: display-id letter -> table
LETTERS = {"f": "cards", "m": "cards", "r": "change_sets", "h": "hotspots", "e": "episodes", "n": "conventions",
           "g": "glossary"}

_ID = re.compile(r"^\s*([fmrheng])(\d+)\s*$")


class MemoryStoreError(ValueError):
    """A write the store refuses (no provenance, an unknown id)."""


def parse_id(text: str) -> tuple[str, int] | None:
    """``"e12"`` -> ``("e", 12)``; None when it is not a memory id."""
    match = _ID.match(str(text or "").lower())
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _now() -> int:
    return int(time.time())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(text: Any, default: Any) -> Any:
    try:
        value = json.loads(str(text))
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def redact(text: str) -> str:
    """Memory text is redacted on write, like the session log."""
    if not text:
        return text
    try:
        from critique_bot.redaction import redact_outgoing

        return redact_outgoing(text, report=False)
    except Exception:  # redaction must never lose the write; fall back to the secret patterns
        from critique_bot.redaction import redact_secrets

        return redact_secrets(text)[0]


def need_provenance(provenance: str) -> str:
    value = str(provenance or "").strip()
    if not re.match(r"^(file|commit|session|import|user|module|index):\S", value):
        raise MemoryStoreError(f"memory needs provenance (file:, commit:, session:, ...), got {provenance!r}")
    return value


@dataclass
class Episode:
    id: int
    task: str
    cls: str
    kind: str
    outcome: str
    trusted: bool
    files_changed: list[str]
    symbols: list[str]
    root_cause: str
    fix_summary: str
    evidence: dict[str, Any]
    session: str
    commit: str
    files_hash: dict[str, str]
    provenance: str
    created: int

    @property
    def ref(self) -> str:
        return f"e{self.id}"


@dataclass
class Card:
    id: int
    kind: str
    key: str
    hash: str
    text: str
    symbols: list[str]
    builder: str
    provenance: str
    updated: int

    @property
    def ref(self) -> str:
        return f"{'m' if self.kind == 'module' else 'f'}{self.id}"


@dataclass
class Convention:
    id: int
    text: str
    evidence: list[str]
    status: str
    session: str
    provenance: str
    created: int
    reviewed: int
    note: str

    @property
    def ref(self) -> str:
        return f"n{self.id}"


@dataclass
class ChangeSet:
    id: int
    files: list[str]
    count: int
    last_ts: int
    commits: list[str]
    subjects: list[str]
    sessions: list[str]
    provenance: str

    @property
    def ref(self) -> str:
        return f"r{self.id}"


@dataclass
class HotSpot:
    id: int
    path: str
    fixes: int
    commits: int
    last_fix_ts: int
    fix_commits: list[str]
    provenance: str

    @property
    def ref(self) -> str:
        return f"h{self.id}"


def memory_dir(workspace: Path) -> Path:
    """``<project>/.crit/memory`` (crit's own folder for the home folder)."""
    from critique_bot.project_home import project_dir

    return project_dir(Path(workspace), migrate=False) / DIR_NAME


def db_path(workspace: Path) -> Path:
    return memory_dir(workspace) / DB_NAME


class MemoryStore:
    """One project's memory. Thread-safe for the agent's parallel tools (one lock)."""

    def __init__(self, path: Path, *, fts: bool | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from critique_bot.output import restrict_dir

            restrict_dir(self.path.parent)
        except Exception:  # nosec B110 - private folder is best effort, like the session log
            pass
        self.conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        self.conn.executescript(_TABLES)
        self.fts = False
        if fts is not False:
            try:
                for sql in _FTS:
                    self.conn.execute(sql)
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
        if not self.get_meta("schema"):
            self.set_meta("schema", str(SCHEMA))
        self.conn.commit()

    # ------------------------------------------------------------------ basics

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
            self.conn.commit()

    def counts(self) -> dict[str, int]:
        out = {}
        for name, sql in (
            ("file cards", "SELECT COUNT(*) FROM cards WHERE kind = 'file'"),
            ("module cards", "SELECT COUNT(*) FROM cards WHERE kind = 'module'"),
            ("recipes", "SELECT COUNT(*) FROM change_sets WHERE count >= 2 AND size >= 2"),
            ("hot spots", "SELECT COUNT(*) FROM hotspots WHERE fixes > 0"),
            ("episodes", "SELECT COUNT(*) FROM episodes WHERE kind = 'positive'"),
            ("negative episodes", "SELECT COUNT(*) FROM episodes WHERE kind = 'negative'"),
            ("untrusted episodes", "SELECT COUNT(*) FROM episodes WHERE trusted = 0"),
            ("conventions approved", "SELECT COUNT(*) FROM conventions WHERE status = 'approved'"),
            ("conventions proposed", "SELECT COUNT(*) FROM conventions WHERE status = 'proposed'"),
            ("conventions stale", "SELECT COUNT(*) FROM conventions WHERE status = 'stale'"),
            ("glossary entries", "SELECT COUNT(*) FROM glossary"),
        ):
            out[name] = int(self.conn.execute(sql).fetchone()[0])
        return out

    # ------------------------------------------------------------------ cards (M1)

    def card(self, kind: str, key: str) -> Card | None:
        row = self.conn.execute("SELECT * FROM cards WHERE kind = ? AND key = ?", (kind, key)).fetchone()
        return _card(row) if row else None

    def put_card(self, kind: str, key: str, *, hash: str, text: str, symbols: Iterable[str] = (), builder: str = "extractive",
                 provenance: str) -> Card:
        provenance = need_provenance(provenance)
        text = redact(text.strip())
        now = _now()
        with self.lock:
            row = self.conn.execute("SELECT id, created FROM cards WHERE kind = ? AND key = ?", (kind, key)).fetchone()
            values = (hash, text, _json(list(symbols)), builder, provenance, now)
            if row:
                card_id = int(row[0])
                self.conn.execute("UPDATE cards SET hash = ?, text = ?, symbols = ?, builder = ?, provenance = ?, updated = ? "
                                  "WHERE id = ?", (*values, card_id))
                if self.fts:
                    self.conn.execute("DELETE FROM cards_fts WHERE rowid = ?", (card_id,))
            else:
                cur = self.conn.execute(
                    "INSERT INTO cards (kind, key, hash, text, symbols, builder, provenance, updated, created) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (kind, key, *values, now))
                card_id = int(cur.lastrowid or 0)
            if self.fts:
                self.conn.execute("INSERT INTO cards_fts (rowid, key, text) VALUES (?, ?, ?)", (card_id, key, text))
            self.conn.commit()
        found = self.card(kind, key)
        assert found is not None
        return found

    def drop_card(self, card_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
            if self.fts:
                self.conn.execute("DELETE FROM cards_fts WHERE rowid = ?", (card_id,))
            self.conn.commit()

    def cards(self, kind: str | None = None, *, limit: int = 100_000) -> list[Card]:
        if kind:
            rows = self.conn.execute("SELECT * FROM cards WHERE kind = ? ORDER BY key LIMIT ?", (kind, limit))
        else:
            rows = self.conn.execute("SELECT * FROM cards ORDER BY kind, key LIMIT ?", (limit,))
        return [_card(row) for row in rows]

    def search_cards(self, words: list[str], *, limit: int = 20) -> list[tuple[Card, float]]:
        """Cards whose text has the words, best first; scores in 0..1."""
        words = _clean_words(words)
        if not words:
            return []
        rows: list[tuple[int, float]] = []
        if self.fts:
            match = " OR ".join('"' + word + '"' for word in words)
            try:
                rows = [(int(r[0]), float(r[1])) for r in self.conn.execute(
                    "SELECT rowid, bm25(cards_fts) FROM cards_fts WHERE cards_fts MATCH ? ORDER BY bm25(cards_fts) LIMIT ?",
                    (match, limit * 3))]
            except sqlite3.OperationalError:
                rows = []
        if self.fts and rows:
            texts = {int(r[0]): f"{r[1]} {r[2]}" for r in self.conn.execute(
                f"SELECT id, key, text FROM cards WHERE id IN ({','.join('?' * len(rows))})",  # nosec B608: marks only
                [item for item, _s in rows])}
            scored = blend(words, _bm25_to_unit(rows), texts)[:limit]
        else:
            docs = [(int(r["id"]), f"{r['key']} {r['text']}") for r in self.conn.execute("SELECT id, key, text FROM cards")]
            scored = simple_scores(words, docs)[:limit]
        out = []
        for card_id, score in scored:
            row = self.conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
            if row:
                out.append((_card(row), score))
        return out

    # ------------------------------------------------------------------ change patterns (M2)

    def add_change_set(self, files: list[str], *, ts: int, commit: str = "", subject: str = "", session: str = "",
                       provenance: str) -> None:
        """Count one more occurrence of exactly this set of files (a commit or a session)."""
        provenance = need_provenance(provenance)
        files = sorted(set(files))
        key = "\n".join(files)
        with self.lock:
            row = self.conn.execute("SELECT id, count, last_ts, commits, subjects, sessions FROM change_sets WHERE key = ?",
                                    (key,)).fetchone()
            if row is None:
                cur = self.conn.execute(
                    "INSERT INTO change_sets (key, files, size, count, last_ts, commits, subjects, sessions, provenance) "
                    "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
                    (key, _json(files), len(files), ts, _json([commit] if commit else []),
                     _json([subject] if subject else []), _json([session] if session else []), provenance))
                set_id = int(cur.lastrowid or 0)
                self.conn.executemany("INSERT INTO change_set_files (set_id, path) VALUES (?, ?)",
                                      [(set_id, path) for path in files])
                return
            commits = loads(row["commits"], [])
            subjects = loads(row["subjects"], [])
            sessions = loads(row["sessions"], [])
            if commit and commit not in commits:
                commits = [commit, *commits][:8]
            if subject and subject not in subjects:
                subjects = [subject, *subjects][:6]
            if session and session not in sessions:
                sessions = [session, *sessions][:8]
            self.conn.execute(
                "UPDATE change_sets SET count = count + 1, last_ts = max(last_ts, ?), commits = ?, subjects = ?, sessions = ? "
                "WHERE id = ?", (ts, _json(commits), _json(subjects), _json(sessions), int(row["id"])))

    def recipes(self, *, files: Iterable[str] = (), min_count: int = 2, limit: int = 20) -> list[ChangeSet]:
        """Repeated change sets (count >= min_count), the ones touching ``files`` when given;
        a set contained in a bigger one with the same count is left out (it adds nothing)."""
        wanted = list(dict.fromkeys(files))
        if wanted:
            marks = ",".join("?" * len(wanted))
            rows = self.conn.execute(
                "SELECT DISTINCT s.* FROM change_set_files f JOIN change_sets s ON s.id = f.set_id "
                f"WHERE f.path IN ({marks}) AND s.count >= ? AND s.size >= 2 ORDER BY s.count DESC, s.size DESC, s.last_ts DESC "  # nosec B608: marks is "?,?..."
                "LIMIT ?", (*wanted, min_count, limit * 4)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM change_sets WHERE count >= ? AND size >= 2 ORDER BY count DESC, size DESC, last_ts DESC LIMIT ?",
                (min_count, limit * 4)).fetchall()
        found = [_set(row) for row in rows]
        kept: list[ChangeSet] = []
        for item in found:
            if any(set(item.files) < set(other.files) and other.count >= item.count for other in found):
                continue
            kept.append(item)
        return kept[:limit]

    def put_hotspot(self, path: str, *, fixes: int, commits: int, last_fix_ts: int, fix_commits: list[str],
                    provenance: str) -> None:
        """Add counts for ``path``; ``fix_commits`` newest first, merged with the ones already kept."""
        provenance = need_provenance(provenance)
        with self.lock:
            row = self.conn.execute("SELECT fix_commits FROM hotspots WHERE path = ?", (path,)).fetchone()
            old = loads(row[0], []) if row else []
            merged = list(dict.fromkeys([*fix_commits, *old]))[:6]
            self.conn.execute(
                "INSERT INTO hotspots (path, fixes, commits, last_fix_ts, fix_commits, provenance) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET fixes = fixes + excluded.fixes, commits = commits + excluded.commits, "
                "last_fix_ts = max(last_fix_ts, excluded.last_fix_ts), fix_commits = excluded.fix_commits, "
                "provenance = excluded.provenance",
                (path, fixes, commits, last_fix_ts, _json(merged), provenance))

    def hotspots(self, *, paths: Iterable[str] | None = None, limit: int = 20) -> list[HotSpot]:
        if paths is not None:
            wanted = list(dict.fromkeys(paths))
            if not wanted:
                return []
            marks = ",".join("?" * len(wanted))
            rows = self.conn.execute(f"SELECT * FROM hotspots WHERE fixes > 0 AND path IN ({marks}) "  # nosec B608: marks is "?,?..."
                                     "ORDER BY fixes DESC, last_fix_ts DESC LIMIT ?", (*wanted, limit))
        else:
            rows = self.conn.execute("SELECT * FROM hotspots WHERE fixes > 0 ORDER BY fixes DESC, last_fix_ts DESC LIMIT ?",
                                     (limit,))
        return [_hot(row) for row in rows]

    def commit(self) -> None:
        with self.lock:
            self.conn.commit()

    # ------------------------------------------------------------------ episodes (M3)

    def add_episode(self, episode: dict[str, Any]) -> Episode:
        provenance = need_provenance(str(episode.get("provenance") or (f"session:{episode['session']}" if episode.get("session") else "")))
        task = redact(str(episode.get("task") or "").strip())[:2000]
        root_cause = redact(str(episode.get("root_cause") or "").strip())[:600]
        fix_summary = redact(str(episode.get("fix_summary") or "").strip())[:600]
        files = [str(item) for item in episode.get("files_changed") or []][:50]
        symbols = [str(item) for item in episode.get("symbols") or []][:30]
        values = (
            task, str(episode.get("class") or ""), str(episode.get("kind") or "positive"), str(episode.get("outcome") or ""),
            1 if episode.get("trusted", True) else 0, _json(files), _json(symbols), root_cause, fix_summary,
            redact(_json(episode.get("evidence") or {})), str(episode.get("session") or ""), str(episode.get("commit") or ""),
            _json(episode.get("files_hash") or {}), provenance, int(episode.get("created") or _now()),
        )
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO episodes (task, class, kind, outcome, trusted, files_changed, symbols, root_cause, fix_summary, "
                "evidence, session, commit_sha, files_hash, provenance, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values)
            episode_id = int(cur.lastrowid or 0)
            if self.fts:
                self.conn.execute(
                    "INSERT INTO episodes_fts (rowid, task, root_cause, fix_summary, symbols, files) VALUES (?, ?, ?, ?, ?, ?)",
                    (episode_id, task, root_cause, fix_summary, " ".join(symbols), " ".join(files)))
            self.conn.commit()
        found = self.episode(episode_id)
        assert found is not None
        return found

    def episode(self, episode_id: int) -> Episode | None:
        row = self.conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        return _episode(row) if row else None

    def episodes(self, *, limit: int = 100_000) -> list[Episode]:
        return [_episode(row) for row in self.conn.execute("SELECT * FROM episodes ORDER BY id LIMIT ?", (limit,))]

    def search_episodes(self, words: list[str], *, limit: int = 10, include_untrusted: bool = False) -> list[tuple[Episode, float]]:
        """Episodes like ``words`` (BM25 over task, cause, fix, symbols, files), best first, scores 0..1."""
        words = _clean_words(words)
        if not words:
            return []
        trust = "" if include_untrusted else " AND e.trusted = 1"
        rows: list[tuple[int, float]] = []
        if self.fts:
            match = " OR ".join('"' + word + '"' for word in words)
            try:
                rows = [(int(r[0]), float(r[1])) for r in self.conn.execute(
                    "SELECT f.rowid, bm25(episodes_fts) FROM episodes_fts f JOIN episodes e ON e.id = f.rowid "
                    f"WHERE episodes_fts MATCH ?{trust} ORDER BY bm25(episodes_fts) LIMIT ?",  # nosec B608: trust is fixed text
                    (match, limit * 3))]
            except sqlite3.OperationalError:
                rows = []
        if self.fts and rows:
            texts = {int(r[0]): " ".join(str(r[k]) for k in range(1, 6)) for r in self.conn.execute(
                "SELECT id, task, root_cause, fix_summary, symbols, files_changed FROM episodes "
                f"WHERE id IN ({','.join('?' * len(rows))})", [item for item, _s in rows])}  # nosec B608: marks only
            scored = blend(words, _bm25_to_unit(rows), texts)[:limit]
        else:
            sql = "SELECT id, task, root_cause, fix_summary, symbols, files_changed FROM episodes e WHERE 1 = 1" + trust
            docs = [(int(r[0]), " ".join(str(r[k]) for k in range(1, 6))) for r in self.conn.execute(sql)]
            scored = simple_scores(words, docs)[:limit]
        out = []
        for episode_id, score in scored:
            found = self.episode(episode_id)
            if found:
                out.append((found, score))
        return out

    # ------------------------------------------------------------------ conventions (M4)

    def add_convention(self, text: str, evidence: list[str], *, session: str = "", provenance: str,
                       status: str = "proposed") -> Convention:
        provenance = need_provenance(provenance)
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO conventions (text, evidence, status, session, provenance, created) VALUES (?, ?, ?, ?, ?, ?)",
                (redact(text.strip()), redact(_json([str(item) for item in evidence])), status, session, provenance, _now()))
            self.conn.commit()
            found = self.convention(int(cur.lastrowid or 0))
        assert found is not None
        return found

    def convention(self, convention_id: int) -> Convention | None:
        row = self.conn.execute("SELECT * FROM conventions WHERE id = ?", (convention_id,)).fetchone()
        return _convention(row) if row else None

    def conventions(self, status: str | None = None) -> list[Convention]:
        if status:
            rows = self.conn.execute("SELECT * FROM conventions WHERE status = ? ORDER BY id", (status,))
        else:
            rows = self.conn.execute("SELECT * FROM conventions ORDER BY id")
        return [_convention(row) for row in rows]

    def set_convention_status(self, convention_id: int, status: str, note: str = "") -> bool:
        if status not in {"proposed", "approved", "rejected", "stale"}:
            raise MemoryStoreError(f"unknown convention status {status!r}")
        with self.lock:
            cur = self.conn.execute("UPDATE conventions SET status = ?, reviewed = ?, note = ? WHERE id = ?",
                                    (status, _now(), note, convention_id))
            self.conn.commit()
        return bool(cur.rowcount)

    # ------------------------------------------------------------------ glossary

    def add_glossary(self, term: str, expansion: str, *, provenance: str, weight: float = 0.8) -> bool:
        provenance = need_provenance(provenance)
        term, expansion = term.strip().lower(), expansion.strip().lower()
        if not term or not expansion or term == expansion:
            return False
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO glossary (term, expansion, weight, provenance, created) VALUES (?, ?, ?, ?, ?)",
                (term, expansion, weight, provenance, _now()))
            self.conn.commit()
        return bool(cur.rowcount)

    def glossary(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT rowid AS id, term, expansion, weight, provenance, created FROM glossary ORDER BY term"))

    # ------------------------------------------------------------------ by id

    def get(self, ref: str) -> Any:
        parsed = parse_id(ref)
        if parsed is None:
            return None
        letter, number = parsed
        if letter in {"f", "m"}:
            row = self.conn.execute("SELECT * FROM cards WHERE id = ? AND kind = ?",
                                    (number, "module" if letter == "m" else "file")).fetchone()
            return _card(row) if row else None
        if letter == "e":
            return self.episode(number)
        if letter == "n":
            return self.convention(number)
        if letter == "r":
            row = self.conn.execute("SELECT * FROM change_sets WHERE id = ?", (number,)).fetchone()
            return _set(row) if row else None
        if letter == "h":
            row = self.conn.execute("SELECT * FROM hotspots WHERE id = ?", (number,)).fetchone()
            return _hot(row) if row else None
        if letter == "g":
            row = self.conn.execute("SELECT rowid AS id, * FROM glossary WHERE rowid = ?", (number,)).fetchone()
            return dict(row) if row else None
        return None

    def forget(self, ref: str) -> bool:
        parsed = parse_id(ref)
        if parsed is None:
            raise MemoryStoreError(f"not a memory id: {ref!r} (ids look like e12, n3, f40)")
        letter, number = parsed
        table = LETTERS[letter]
        with self.lock:
            if table == "change_sets":
                self.conn.execute("DELETE FROM change_set_files WHERE set_id = ?", (number,))
            column = "rowid" if table == "glossary" else "id"
            cur = self.conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (number,))  # nosec B608: table and column come from fixed maps
            if self.fts and table == "episodes":
                self.conn.execute("DELETE FROM episodes_fts WHERE rowid = ?", (number,))
            if self.fts and table == "cards":
                self.conn.execute("DELETE FROM cards_fts WHERE rowid = ?", (number,))
            self.conn.commit()
        return bool(cur.rowcount)


# --------------------------------------------------------------------------- scoring helpers

_STOP = frozenset(
    """the a an and or of to in on at for with without from by is are was were be been it its this that
    when then than not no but if after before into out up can should would will does do did has have had get
    set show shows wrong bug issue please fix why how what which where who there their they them some any all
    we you your i my me also only just still add make use new""".split()
)


def words_of(text: str, *, limit: int = 24) -> list[str]:
    """Search words from free text: identifiers split, stop words out, order kept."""
    out: list[str] = []
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text or ""):
        parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", token)
        parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
        pieces = [p.lower() for p in re.split(r"[^A-Za-z0-9]+", parts) if p]
        candidates = [token.lower()] + (pieces if len(pieces) > 1 else [])
        for word in candidates:
            if len(word) >= 3 and word not in _STOP and word not in out:
                out.append(word)
    return out[:limit]


def _clean_words(words: Iterable[str]) -> list[str]:
    cleaned = []
    for word in words:
        word = re.sub(r"[^A-Za-z0-9_]", "", str(word)).lower()
        if len(word) >= 2 and word not in cleaned:
            cleaned.append(word)
    return cleaned[:32]


def _bm25_to_unit(rows: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """SQLite's bm25 is negative (lower is better): map to 0..1 with the best at 1."""
    if not rows:
        return []
    best = min(score for _id, score in rows)
    if best >= 0:
        return [(item, 0.5) for item, _s in rows]
    return [(item, round(max(0.05, score / best), 3)) for item, score in rows]


def blend(words: list[str], ranked: list[tuple[int, float]], texts: dict[int, str]) -> list[tuple[int, float]]:
    """BM25 alone is unstable on a few documents (a word in every episode scores ~0), so the
    score is half BM25 and half the share of query words the item contains."""
    out = []
    for item, bm25 in ranked:
        have = set(words_of(texts.get(item, ""), limit=400)) | set(re.findall(r"[a-z0-9_]{2,}", texts.get(item, "").lower()))
        share = sum(1 for word in words if word in have) / max(1, len(words))
        out.append((item, round(0.5 * bm25 + 0.5 * min(1.0, share * 1.5), 3)))
    out.sort(key=lambda pair: (-pair[1], pair[0]))
    return out


def simple_scores(words: list[str], docs: list[tuple[int, str]]) -> list[tuple[int, float]]:
    """A small BM25-like scorer for when FTS5 is missing: idf-weighted word hits, 0..1."""
    import math

    if not docs or not words:
        return []
    tokenized = [(item, set(words_of(text, limit=400)) | set(re.findall(r"[a-z0-9_]{2,}", text.lower()))) for item, text in docs]
    total = len(tokenized)
    idf = {}
    for word in words:
        hits = sum(1 for _i, toks in tokenized if word in toks)
        idf[word] = math.log(1 + (total - hits + 0.5) / (hits + 0.5)) if hits else 0.0
    scored = []
    for item, toks in tokenized:
        score = sum(idf[word] for word in words if word in toks)
        if score > 0:
            scored.append((item, score))
    if not scored:
        return []
    top = max(score for _i, score in scored)
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return [(item, round(score / top, 3)) for item, score in scored]


# --------------------------------------------------------------------------- row mapping


def _card(row: sqlite3.Row) -> Card:
    return Card(int(row["id"]), str(row["kind"]), str(row["key"]), str(row["hash"]), str(row["text"]),
                loads(row["symbols"], []), str(row["builder"]), str(row["provenance"]), int(row["updated"]))


def _episode(row: sqlite3.Row) -> Episode:
    return Episode(int(row["id"]), str(row["task"]), str(row["class"]), str(row["kind"]), str(row["outcome"]),
                   bool(row["trusted"]), loads(row["files_changed"], []), loads(row["symbols"], []), str(row["root_cause"]),
                   str(row["fix_summary"]), loads(row["evidence"], {}), str(row["session"]), str(row["commit_sha"]),
                   loads(row["files_hash"], {}), str(row["provenance"]), int(row["created"]))


def _convention(row: sqlite3.Row) -> Convention:
    return Convention(int(row["id"]), str(row["text"]), loads(row["evidence"], []), str(row["status"]), str(row["session"]),
                      str(row["provenance"]), int(row["created"]), int(row["reviewed"]), str(row["note"]))


def _set(row: sqlite3.Row) -> ChangeSet:
    return ChangeSet(int(row["id"]), loads(row["files"], []), int(row["count"]), int(row["last_ts"]), loads(row["commits"], []),
                     loads(row["subjects"], []), loads(row["sessions"], []), str(row["provenance"]))


def _hot(row: sqlite3.Row) -> HotSpot:
    return HotSpot(int(row["id"]), str(row["path"]), int(row["fixes"]), int(row["commits"]), int(row["last_fix_ts"]),
                   loads(row["fix_commits"], []), str(row["provenance"]))
