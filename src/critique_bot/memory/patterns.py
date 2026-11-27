# SPDX-License-Identifier: Apache-2.0
"""M2: change patterns mined from the Stage 8 history (L4) and from finished sessions.

- **Change sets / recipes:** each commit's set of files is counted, and so are its 3- and
  4-file subsets (small commits only), so "these files change together" emerges even when
  commits add one extra file. A set seen at least twice is a recipe; it lists the commits
  (and sessions) it came from and their subjects. Pairs are the index's own ``cochange``.
- **Hot spots:** files with many fix commits (``fix|bug|crash|...`` in the subject) and churn,
  priors for localization.

Mining is incremental: each index database remembers the last commit row it mined
(``mined:<db>`` in the memory meta), so a run reads only the commits added since.
"""

from __future__ import annotations

import itertools
import re
import sqlite3
from pathlib import Path

from critique_bot.memory.store import MemoryStore

#: Commits with more files than this give no change set (merges, mass renames, imports).
MAX_SET_FILES = 12
#: Subsets are counted only for commits this small (C(7,3) + C(7,4) = 70 per commit).
SUBSET_MAX_FILES = 7
FIX_SUBJECT = re.compile(r"(?i)\b(fix(?:e[sd])?|bug|crash|regression|npe|anr|leak|broken|hotfix|revert|issue|fault|error)\b")
_NOISE = re.compile(r"(?i)(^|/)(package-lock\.json|yarn\.lock|go\.sum|Cargo\.lock|poetry\.lock|CHANGELOG[^/]*|\.gitignore)$")


def _key(db: Path) -> str:
    return f"mined:{Path(db).resolve()}"


def mine(store: MemoryStore, conn: sqlite3.Connection, db: Path, *, limit: int = 20_000) -> int:
    """Read the commits ``conn`` gained since the last run into change sets and hot spots.
    Returns the number of commits mined."""
    last = int(store.get_meta(_key(db), "0") or 0)
    try:
        top = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM commits").fetchone()[0]
    except sqlite3.DatabaseError:
        return 0
    if int(top or 0) < last:
        last = 0  # the index was rebuilt: its rows are new
    rows = conn.execute("SELECT rowid, sha, ts, subject FROM commits WHERE rowid > ? ORDER BY rowid LIMIT ?",
                        (last, limit)).fetchall()
    if not rows:
        return 0
    hot: dict[str, list] = {}
    newest = last
    for rowid, sha, ts, subject in rows:
        newest = max(newest, int(rowid))
        files = [str(r[0]) for r in conn.execute("SELECT path FROM commit_files WHERE sha = ?", (str(sha),))]
        files = [path for path in files if not _NOISE.search(path)]
        stamp = int(ts or 0)
        subject = str(subject or "")[:120]
        fix = bool(FIX_SUBJECT.search(subject))
        for path in files:
            entry = hot.setdefault(path, [0, 0, 0, []])
            entry[1] += 1
            if fix:
                entry[0] += 1
                entry[2] = max(entry[2], stamp)
                entry[3].append(str(sha)[:10])
        if not 2 <= len(files) <= MAX_SET_FILES:
            continue
        provenance = f"commit:{str(sha)[:12]}"
        store.add_change_set(files, ts=stamp, commit=str(sha)[:10], subject=subject, provenance=provenance)
        if len(files) <= SUBSET_MAX_FILES:
            for size in (3, 4):
                if size >= len(files):
                    continue
                for subset in itertools.combinations(sorted(files), size):
                    store.add_change_set(list(subset), ts=stamp, commit=str(sha)[:10], subject=subject,
                                         provenance=provenance)
    for path, (fixes, commits, last_fix, shas) in hot.items():
        store.put_hotspot(path, fixes=fixes, commits=commits, last_fix_ts=last_fix, fix_commits=shas[::-1][:6],
                          provenance=f"commit:{shas[-1] if shas else str(rows[-1][1])[:10]}")
    store.commit()
    store.set_meta(_key(db), str(newest))
    return len(rows)


def add_session_change(store: MemoryStore, files: list[str], *, session: str, task: str, ts: int) -> None:
    """A finished task's changed files count as one change set too (with its session as source)."""
    files = [path for path in dict.fromkeys(files) if not _NOISE.search(path)]
    if 2 <= len(files) <= MAX_SET_FILES and session:
        store.add_change_set(files, ts=ts, session=session, subject=task[:120], provenance=f"session:{session}")
        store.commit()


def recipe_title(subjects: list[str]) -> str:
    """A short title for a recipe: the words most of its commit subjects share, in the order of the first."""
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for subject in subjects:
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", subject.lower()):
            order.setdefault(word, len(order))
        for word in set(re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", subject.lower())):
            counts[word] = counts.get(word, 0) + 1
    common = sorted((word for word, n in counts.items() if n >= 2), key=lambda word: (-counts[word], order[word]))[:5]
    common.sort(key=lambda word: order[word])
    return " ".join(common) if common else (subjects[0][:60] if subjects else "")


def hotness(fixes: int, commits: int) -> float:
    """0..1: many fix commits, and a large share of the file's commits being fixes."""
    if fixes <= 0:
        return 0.0
    share = fixes / max(1, commits)
    return round(min(1.0, 0.15 * fixes) * (0.5 + 0.5 * share), 3)
