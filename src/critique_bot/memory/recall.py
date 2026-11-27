# SPDX-License-Identifier: Apache-2.0
"""Recall, part one: from a task to memory candidates (the engine ranks them into the hunch).

:func:`parse` pulls the query out of a task: search words, identifiers, quoted strings, error
text and file names. :func:`gather` collects every kind of candidate for it:

- similar **episodes** (BM25 over task, cause, fix, symbols, files; a simple scorer without
  FTS5), positive and negative, trusted only unless asked;
- **seed files**: definitions of the task's identifiers (the Stage 8 symbols), files it names,
  and file **cards** whose text matches the words;
- **recipes** (repeated change sets) and index **co-change** partners of the seed files;
- **module cards** of the modules involved, **hot spots** among the candidates, and approved
  **conventions** that match the words.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from critique_bot.memory.cards import file_hash, module_of
from critique_bot.memory.store import Card, ChangeSet, Convention, Episode, HotSpot, MemoryStore, simple_scores, words_of


@dataclass
class Query:
    text: str
    words: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    quoted: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)


_QUOTED = re.compile(r"\"([^\"\n]{4,200})\"|`([^`\n]{3,200})`|'([^'\n]{6,200})'")
_ERROR = re.compile(r"(?:Error|Exception|error|FATAL|panic|Assertion\w*)\s*:\s*(.{6,200})")
_IDENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:[A-Z_][A-Za-z0-9_]*)+|[a-z]+_[a-z0-9_]+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
_PATH = re.compile(r"(?<![\w])((?:[\w.-]+/)+[\w.-]+\.\w+|[\w-]+\.(?:py|java|kt|go|rs|c|cc|cpp|h|hpp|ts|tsx|js|jsx|xml|rc|te|bp|gradle|aidl|hal))\b")


def parse(task: str) -> Query:
    query = Query(task)
    for match in _QUOTED.finditer(task):
        value = next(group for group in match.groups() if group)
        if value not in query.quoted:
            query.quoted.append(value)
    for match in _ERROR.finditer(task):
        if match.group(1) not in query.quoted:
            query.quoted.append(match.group(1).strip())
    query.identifiers = list(dict.fromkeys(m.group(1) for m in _IDENT.finditer(task) if len(m.group(1)) >= 4))[:15]
    query.paths = list(dict.fromkeys(m.group(1) for m in _PATH.finditer(task)))[:8]
    query.words = words_of(task, limit=24)
    return query


@dataclass
class EpisodeHit:
    episode: Episode
    score: float
    changed_since: list[str]  # changed files whose hash differs from when the episode was written


@dataclass
class Candidates:
    query: Query
    episodes: list[EpisodeHit] = field(default_factory=list)
    #: seed file -> reasons ("defines HvacHelper", "named in the task", "card f3 matches")
    seeds: dict[str, list[str]] = field(default_factory=dict)
    seed_scores: dict[str, float] = field(default_factory=dict)
    cards: list[tuple[Card, float]] = field(default_factory=list)
    recipes: list[ChangeSet] = field(default_factory=list)
    #: file -> (score, the candidate file it co-changes with)
    cochange: dict[str, tuple[float, str]] = field(default_factory=dict)
    modules: list[Card] = field(default_factory=list)
    hotspots: list[HotSpot] = field(default_factory=list)
    conventions: list[tuple[Convention, float]] = field(default_factory=list)


def changed_since(workspace: Path, episode: Episode) -> list[str]:
    out = []
    for rel, digest in (episode.files_hash or {}).items():
        now = file_hash(Path(workspace) / rel)
        if (now or "deleted") != digest:
            out.append(rel)
    return out


def _seed(found: Candidates, path: str, score: float, reason: str) -> None:
    found.seeds.setdefault(path, [])
    if reason not in found.seeds[path] and len(found.seeds[path]) < 3:
        found.seeds[path].append(reason)
    found.seed_scores[path] = max(found.seed_scores.get(path, 0.0), score)


def gather(store: MemoryStore, workspace: Path, conn: sqlite3.Connection | None, query: Query, *,
           include_untrusted: bool = False, limit: int = 8) -> Candidates:
    found = Candidates(query)
    words = query.words + [w for q in query.quoted for w in words_of(q, limit=8) if w not in query.words]
    for episode, score in store.search_episodes(words, limit=limit, include_untrusted=include_untrusted):
        found.episodes.append(EpisodeHit(episode, score, changed_since(workspace, episode)))
    if conn is not None:
        for ident in query.identifiers:
            name = re.split(r"\.|::", ident)[-1]
            try:
                rows = conn.execute("SELECT path FROM symbols WHERE name = ? AND kind NOT IN ('variable', 'field', 'constant') "
                                    "LIMIT 6", (name,)).fetchall()
            except sqlite3.DatabaseError:
                rows = []
            for (path,) in rows[:4]:
                _seed(found, str(path), 0.6 if len(rows) <= 2 else 0.4, f"defines {ident}")
        for raw in query.paths:
            base = raw.rpartition("/")[2]
            try:
                rows = conn.execute("SELECT path FROM files WHERE path = ? OR path LIKE ? LIMIT 5",
                                    (raw, "%/" + base.replace("_", "\\_"))).fetchall()
            except sqlite3.DatabaseError:
                rows = []
            for (path,) in rows:
                if str(path).endswith(raw.lstrip("./")) or str(path).endswith("/" + base) or str(path) == base:
                    _seed(found, str(path), 0.8, f"named in the task ({raw})")
    for card, score in store.search_cards(words, limit=limit):
        found.cards.append((card, score))
        if card.kind == "file" and score >= 0.3:
            _seed(found, card.key, 0.45 * score, f"card {card.ref} matches")
    episode_files = [rel for hit in found.episodes if hit.episode.kind == "positive" for rel in hit.episode.files_changed]
    anchor = list(dict.fromkeys(list(found.seeds) + episode_files))[:12]
    found.recipes = store.recipes(files=anchor, limit=4) if anchor else []
    if conn is not None and anchor:
        from critique_bot.indexing import history

        for path in anchor[:6]:
            try:
                partners = history.cochanged(conn, path, limit=5)
            except sqlite3.DatabaseError:
                partners = []
            for other, times, score, _ts in partners:
                if times >= 2 and other not in anchor and score > found.cochange.get(other, (0.0, ""))[0]:
                    found.cochange[other] = (score, path)
    if conn is not None:
        names = []
        for path in anchor[:8]:
            name = module_of(conn, path)
            if name and name not in names:
                names.append(name)
        for name in names[:3]:
            card = store.card("module", name)
            if card is not None:
                found.modules.append(card)
    candidates = list(dict.fromkeys(anchor + list(found.cochange)))
    found.hotspots = store.hotspots(paths=candidates, limit=5)
    approved = store.conventions("approved")
    if approved and words:
        scores = dict(simple_scores(words, [(item.id, item.text) for item in approved]))
        found.conventions = sorted(((item, scores[item.id]) for item in approved if scores.get(item.id, 0) >= 0.3),
                                   key=lambda pair: -pair[1])[:3]
    return found


def index_targets(workspace: Path, index_path: Path | None) -> list:
    """The index databases that exist now (never starts a build): the project's, then module groups'."""
    if index_path is None:
        return []
    from critique_bot.indexing import engine, store as index_store

    index_path = Path(index_path)
    if not index_path.is_file() and not index_store.group_dbs(index_path):
        return []
    return [target for target in engine.targets(Path(workspace), index_path, wait=0.0) if target.path.is_file()]


def repo_map_files(conn: sqlite3.Connection, module: str, *, limit: int = 6) -> list[str]:
    """The top files of a build module's ranked repo map (Stage 8 ``repo_map``)."""
    from critique_bot.indexing.tools import build_repo_map

    try:
        row = conn.execute("SELECT dir FROM modules WHERE name = ? LIMIT 1", (module,)).fetchone()
    except sqlite3.DatabaseError:
        return []
    if row is None:
        return []
    folder = str(row[0] or "")
    text, _shown = build_repo_map(conn, scope="" if folder in {"", "."} else folder, budget_tokens=300)
    out = []
    for line in text.splitlines():
        if line.startswith(" ") or not line.strip():
            continue
        out.append(line.rstrip(":"))
        if len(out) >= limit:
            break
    return out
