# SPDX-License-Identifier: Apache-2.0
"""The project glossary learned from episodes and approved conventions.

When a finished task or an approved note says what a badly named symbol does ("`doIt`
converts the cabin temperature for display"), each content word of that description becomes
a glossary entry for the symbol's name: ``doit`` <-> ``temperature``, ``doit`` <-> ``display``.
The Stage 8 index glossary (``glossary`` table, ``source = 'learned'``) gets the same rows, so
``locate``'s term expansion maps "temperature" to ``doIt`` and finds it by meaning.

Only names the description adds something to are learned: a word already in the name's own
parts (``convertTemperature`` "converts temperature") teaches nothing.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Iterable

from critique_bot.memory.store import MemoryStore, words_of

_VERBS = (
    "is|are|was|does|did|handles|handled|converts|computes|calculates|stores|keeps|holds|reads|writes|sends|receives|"
    "returns|manages|controls|formats|parses|validates|checks|applies|loads|saves|maps|tracks|decides|builds|creates|"
    "renders|shows|displays|updates|sets|gets|means|represents|implements|wraps|owns|caches|schedules|dispatches|"
    "listens|notifies|registers|resolves|translates|chooses|picks"
)
_DESCRIBES = re.compile(
    r"`?(?P<name>[A-Za-z_][\w$]*(?:(?:\.|::)[A-Za-z_][\w$]*)*)`?(?:\(\))?\s+(?:\((?P<paren>[^)]{4,80})\)|"
    r"(?:actually\s+|really\s+)?(?:" + _VERBS + r")\s+(?P<what>[^.;\n]{4,100}))"
)
#: Words that say nothing about meaning.
_WEAK = frozenset("""value values data thing things object objects stuff item items code function method class file
    called calls call used uses using which that this those these there here when where then just only also into from
    with without what does doesn other some more less very correct correctly properly actually really now""".split())
MAX_WORDS = 4


def _parts(name: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return {part.lower() for part in re.split(r"[^A-Za-z0-9]+", spaced) if part}


def descriptions(text: str) -> list[tuple[str, list[str]]]:
    """``(symbol, meaning words)`` for each "Name does X" / "Name (X)" statement in ``text``."""
    out: list[tuple[str, list[str]]] = []
    for match in _DESCRIBES.finditer(text or ""):
        full = match.group("name")
        short = re.split(r"\.|::", full)[-1]
        if len(short) < 2 or short.lower() in {"it", "this", "that", "which", "the", "there", "what", "and"}:
            continue
        if not re.search(r"[A-Z_0-9]|^[a-z]{1,4}$", short) and "`" not in match.group(0):
            continue  # a plain English word ("display shows ...") is not a symbol
        what = match.group("paren") or match.group("what") or ""
        own = _parts(short)
        words = [word for word in words_of(what, limit=20) if "_" not in word and word not in _WEAK and word not in own
                 and len(word) >= 4 and not any(word.startswith(part) and len(part) >= 4 for part in own)]
        if words:
            out.append((short, words[:MAX_WORDS]))
    return out


def learn(store: MemoryStore, texts: Iterable[str], *, provenance: str, defined=None,
          index_conns: Iterable[sqlite3.Connection] = ()) -> list[tuple[str, str]]:
    """Add glossary entries from ``texts`` (an episode's cause and fix, an approved note).
    ``defined(name)`` keeps only names the index knows. Returns the new ``(term, word)`` pairs."""
    added: list[tuple[str, str]] = []
    for text in texts:
        for symbol, words in descriptions(text):
            if defined is not None and not defined(symbol):
                continue
            for word in words:
                if store.add_glossary(symbol, word, provenance=provenance):
                    added.append((symbol.lower(), word))
    if added:
        sync_index(store, index_conns)
    return added


def sync_index(store: MemoryStore, conns: Iterable[sqlite3.Connection]) -> int:
    """Copy the learned glossary into index databases (their ``glossary`` table, source 'learned').
    The index is a cache that may have been rebuilt, so this runs at task start too."""
    from critique_bot.indexing import glossary as index_glossary

    rows = store.glossary()
    written = 0
    for conn in conns:
        try:
            have = {(str(r[0]), str(r[1])) for r in conn.execute("SELECT term, expansion FROM glossary WHERE source = 'learned'")}
        except sqlite3.DatabaseError:
            continue
        for row in rows:
            key = (str(row["term"]), str(row["expansion"]))
            if key in have:
                continue
            try:
                index_glossary.add_project_term(conn, key[0], key[1], weight=float(row["weight"]), source="learned",
                                                provenance=str(row["provenance"]))
                written += 1
            except sqlite3.DatabaseError:
                break
    return written
