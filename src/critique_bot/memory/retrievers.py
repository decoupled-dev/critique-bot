# SPDX-License-Identifier: Apache-2.0
"""Memory retrievers for the Stage 8 ``locate`` tool: file cards and the learned glossary.

Registered in :data:`critique_bot.indexing.locate.RETRIEVERS` on import (``locate`` imports this
module lazily). Each finds the project's ``memory.db`` from the query's workspace, or from the
index database's own location (``.crit/cache/index.db`` -> ``.crit/memory/memory.db``), and
returns nothing when the project has no memory yet.

- ``cards``: full-text search over the plain-language cards ("converts HVAC temperature
  units for display"), so badly named code is found by what it does.
- ``glossary``: description words that the learned glossary maps to a symbol name.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from critique_bot.indexing import locate as _locate
from critique_bot.memory.store import DB_NAME, DIR_NAME, MemoryStore, db_path, words_of

CARD_WEIGHT = 0.9
GLOSSARY_WEIGHT = 0.9


def _memory_db(query: "_locate.Query", conn: sqlite3.Connection) -> Path | None:
    if query.workspace:
        found = db_path(Path(query.workspace))
        if found.is_file():
            return found
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.DatabaseError:
        return None
    for row in rows:
        name = str(row[2] or "")
        if not name:
            continue
        index = Path(name)
        # .crit/cache/index.db or .crit/cache/index/<group>.db
        for crit in (index.parent.parent, index.parent.parent.parent):
            candidate = crit / DIR_NAME / DB_NAME
            if candidate.is_file():
                return candidate
    return None


def _open(query: "_locate.Query", conn: sqlite3.Connection) -> MemoryStore | None:
    path = _memory_db(query, conn)
    if path is None:
        return None
    try:
        return MemoryStore(path)
    except sqlite3.DatabaseError:
        return None


def _known(conn: sqlite3.Connection, path: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM files WHERE path = ?", (path,)).fetchone() is not None
    except sqlite3.DatabaseError:
        return False


def r_cards(query: "_locate.Query", conn: sqlite3.Connection) -> list["_locate.Hit"]:
    store = _open(query, conn)
    if store is None:
        return []
    try:
        words = [term for term, weight, _why in query.terms if weight >= 0.5 and len(term) >= 3][:16]
        words = words or words_of(query.text)
        hits = []
        for card, score in store.search_cards(words, limit=_locate.PER_RETRIEVER):
            if card.kind != "file" or not _known(conn, card.key):
                continue
            hits.append(_locate.Hit(card.key, 0, "", f"card ({card.ref}): {card.text[:90]}"))
        return hits
    finally:
        store.close()


def r_glossary(query: "_locate.Query", conn: sqlite3.Connection) -> list["_locate.Hit"]:
    store = _open(query, conn)
    if store is None:
        return []
    try:
        wanted = set(words_of(query.text + "\n" + query.evidence, limit=40))
        by_term: dict[str, list[str]] = {}
        for row in store.glossary():
            if str(row["expansion"]) in wanted:
                by_term.setdefault(str(row["term"]), []).append(str(row["expansion"]))
        hits = []
        for term, matched in sorted(by_term.items(), key=lambda item: (-len(item[1]), item[0])):
            rows = conn.execute("SELECT path, line, name, parent FROM symbols WHERE lower(name) = ? LIMIT 5", (term,)).fetchall()
            for path, line, name, parent in rows:
                symbol = f"{parent}.{name}" if parent else str(name)
                hits.append(_locate.Hit(str(path), int(line), symbol,
                                        f"project glossary: {name} means {', '.join(matched[:3])}"))
        return hits[: _locate.PER_RETRIEVER]
    finally:
        store.close()


def register() -> None:
    _locate.RETRIEVERS.setdefault("cards", (r_cards, CARD_WEIGHT))
    _locate.RETRIEVERS.setdefault("glossary", (r_glossary, GLOSSARY_WEIGHT))


register()
