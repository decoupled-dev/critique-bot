# SPDX-License-Identifier: Apache-2.0
"""L4: git history. Commits per file, co-change pairs, commit-message search, blame, ``log -G``.

``update`` reads ``git log --name-only`` from the last indexed commit to HEAD (the first run
reads the newest :data:`MAX_COMMITS`), so later runs cost only the new commits. A rewritten
history (the last indexed commit is no longer an ancestor of HEAD) is read again from the
start. Blame is run on demand and cached by the file's content hash.

On a ``repo``-tool tree each git project is read on its own and its paths are stored
workspace-relative, so one database can hold several projects.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from critique_bot.indexing import store

MAX_COMMITS = 5000
#: Commits that touch more files than this (merges, mass renames) give no co-change pairs.
COCHANGE_MAX_FILES = 30
GIT_TIMEOUT = 60
_RECORD = "\x1e"
_FIELD = "\x1f"


def _env() -> dict[str, str]:
    # GIT_OPTIONAL_LOCKS=0: reading never refreshes .git/index (the tree may be read-only)
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "GIT_PAGER": "cat"}


def git(folder: Path, *args: str, timeout: float = GIT_TIMEOUT) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, errors="replace",
                              timeout=timeout, env=_env(), check=False)
    except subprocess.TimeoutExpired:
        return 124, "git timed out"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr


def project_for(workspace: Path, rel: str = "") -> tuple[Path, str] | None:
    """The git checkout that holds ``rel`` and its workspace-relative prefix ("" for the workspace)."""
    workspace = Path(workspace)
    from critique_bot import file_catalog

    catalog = file_catalog.catalog_for(workspace)
    if catalog.kind == "repo":
        project = catalog.project_of(rel) if rel else None
        if not project:
            return None
        return workspace / project, project
    code, _out = git(workspace, "rev-parse", "--is-inside-work-tree", timeout=10)
    return (workspace, "") if code == 0 else None


def _key(prefix: str) -> str:
    return f"history_head:{prefix}"


def update(conn: sqlite3.Connection, folder: Path, prefix: str = "", *, max_commits: int = MAX_COMMITS) -> int:
    """Read new commits of the checkout at ``folder``. Returns how many were added."""
    code, head = git(folder, "rev-parse", "HEAD", timeout=10)
    head = head.strip()
    if code != 0 or not head:
        return 0
    last = store.get_meta(conn, _key(prefix))
    if last == head:
        return 0
    span = [head, f"-n{max_commits}"]
    if last:
        known, _ = git(folder, "merge-base", "--is-ancestor", last, head, timeout=20)
        if known == 0:
            span = [f"{last}..{head}"]
        else:  # history was rewritten: start over for this project
            _forget_project(conn, prefix)
    code, out = git(
        folder, "log", "--name-only", "--no-renames", "--relative", f"--format={_RECORD}%H{_FIELD}%ct{_FIELD}%an{_FIELD}%s{_FIELD}%b{_FIELD}",
        *span, timeout=GIT_TIMEOUT * 3,
    )
    if code != 0:
        return 0
    added = 0
    fts = store.get_meta(conn, "commit_fts") == "1"
    for record in out.split(_RECORD):
        if not record.strip():
            continue
        parts = record.split(_FIELD)
        if len(parts) < 6:
            continue
        sha, ts, author, subject, body, names = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        files = [f"{prefix}/{name}" if prefix else name for name in names.strip().splitlines() if name.strip()]
        try:
            stamp = int(ts)
        except ValueError:
            stamp = 0
        body = body.strip()[:2000]
        cur = conn.execute(
            "INSERT OR IGNORE INTO commits (sha, ts, author, subject, body, project) VALUES (?, ?, ?, ?, ?, ?)",
            (sha, stamp, author, subject, body, prefix),
        )
        if not cur.rowcount:
            continue
        conn.executemany("INSERT INTO commit_files (sha, path) VALUES (?, ?)", [(sha, path) for path in files])
        if fts:
            conn.execute("INSERT INTO commit_text (sha, text) VALUES (?, ?)", (sha, f"{subject}\n{body}"))
        if 2 <= len(files) <= COCHANGE_MAX_FILES:
            pairs = [(a, b) for i, a in enumerate(sorted(files)) for b in sorted(files)[i + 1 :]]
            conn.executemany(
                "INSERT INTO cochange (a, b, count, last_ts) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(a, b) DO UPDATE SET count = count + 1, last_ts = max(last_ts, excluded.last_ts)",
                [(a, b, stamp) for a, b in pairs],
            )
        added += 1
    store.set_meta(conn, _key(prefix), head)
    conn.commit()
    return added


def _forget_project(conn: sqlite3.Connection, prefix: str) -> None:
    shas = [str(row[0]) for row in conn.execute("SELECT sha FROM commits WHERE project = ?", (prefix,))]
    for start in range(0, len(shas), 500):
        chunk = shas[start : start + 500]
        marks = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM commit_files WHERE sha IN ({marks})", chunk)  # nosec B608: marks is "?,?..."
        if store.get_meta(conn, "commit_fts") == "1":
            conn.execute(f"DELETE FROM commit_text WHERE sha IN ({marks})", chunk)  # nosec B608: marks is "?,?..."
    conn.execute("DELETE FROM commits WHERE project = ?", (prefix,))
    if prefix:
        like = prefix + "/%"
        conn.execute("DELETE FROM cochange WHERE a LIKE ? OR b LIKE ?", (like, like))
    else:
        conn.execute("DELETE FROM cochange")
    store.set_meta(conn, _key(prefix), "")


# --------------------------------------------------------------------------- queries


@dataclass(frozen=True)
class Commit:
    sha: str
    ts: int
    author: str
    subject: str

    def line(self) -> str:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.ts)) if self.ts else "?"
        return f"{self.sha[:10]} {day} {self.author[:20]}: {self.subject[:100]}"


def recent_commits(conn: sqlite3.Connection, path: str, *, limit: int = 10) -> list[Commit]:
    rows = conn.execute(
        "SELECT c.sha, c.ts, c.author, c.subject FROM commit_files f JOIN commits c ON c.sha = f.sha "
        "WHERE f.path = ? ORDER BY c.ts DESC LIMIT ?",
        (path, limit),
    ).fetchall()
    return [Commit(str(r[0]), int(r[1]), str(r[2]), str(r[3])) for r in rows]


def commit_count(conn: sqlite3.Connection, path: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM commit_files WHERE path = ?", (path,)).fetchone()[0])


def cochanged(conn: sqlite3.Connection, path: str, *, limit: int = 15, now: float | None = None) -> list[tuple[str, int, float, int]]:
    """Files changed together with ``path``: ``(file, times, score, last_ts)``, best first.

    The score is the share of ``path``'s commits that also touched the file, weighted up when
    the last such commit is recent (half-life about six months).
    """
    total = max(1, commit_count(conn, path))
    rows = conn.execute(
        "SELECT b, count, last_ts FROM cochange WHERE a = ? UNION ALL SELECT a, count, last_ts FROM cochange WHERE b = ?",
        (path, path),
    ).fetchall()
    stamp = time.time() if now is None else now
    out = []
    for other, count, last_ts in rows:
        age_days = max(0.0, (stamp - float(last_ts)) / 86400.0)
        recency = 0.5 + 0.5 * math.exp(-age_days / 180.0)
        out.append((str(other), int(count), round(int(count) / total * recency, 3), int(last_ts)))
    out.sort(key=lambda item: (-item[2], -item[1], item[0]))
    return out[:limit]


_WORD = re.compile(r"[A-Za-z0-9_]{3,}")


def search_commits(conn: sqlite3.Connection, query: str, *, limit: int = 15) -> list[tuple[Commit, list[str]]]:
    """Commits whose message mentions the words of ``query`` (any word; more words rank higher)."""
    words = list(dict.fromkeys(word.lower() for word in _WORD.findall(query)))[:12]
    if not words:
        return []
    rows: list = []
    if store.get_meta(conn, "commit_fts") == "1":
        match = " OR ".join('"' + word.replace('"', "") + '"' for word in words)
        try:
            rows = conn.execute(
                "SELECT c.sha, c.ts, c.author, c.subject FROM commit_text t JOIN commits c ON c.sha = t.sha "
                "WHERE commit_text MATCH ? ORDER BY bm25(commit_text) LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        score = " + ".join("(instr(lower(subject || ' ' || body), ?) > 0)" for _ in words)
        rows = conn.execute(
            f"SELECT sha, ts, author, subject, ({score}) AS hits FROM commits WHERE hits > 0 ORDER BY hits DESC, ts DESC LIMIT ?",  # nosec B608: score is fixed text with ? placeholders
            (*words, limit),
        ).fetchall()
    out = []
    for r in rows:
        files = [str(f[0]) for f in conn.execute("SELECT path FROM commit_files WHERE sha = ? LIMIT 12", (str(r[0]),))]
        out.append((Commit(str(r[0]), int(r[1]), str(r[2]), str(r[3])), files))
    return out


def log_g(folder: Path, regex: str, *, path: str = "", limit: int = 10, timeout: float = 20) -> list[str]:
    """``git log -G<regex>``: commits whose diff added or removed a matching line."""
    args = ["log", f"-G{regex}", f"-n{limit}", "--format=%h %ad %an: %s", "--date=short"]
    if path:
        args += ["--", path]
    code, out = git(folder, *args, timeout=timeout)
    return out.splitlines() if code == 0 else []


def file_log(folder: Path, path: str, *, start: int = 0, end: int = 0, limit: int = 10, timeout: float = 20) -> list[str]:
    """Commits that changed ``path`` (or its lines ``start``-``end`` with ``git log -L``)."""
    if start and end:
        args = ["log", "--no-patch", f"-L{start},{end}:{path}", f"-n{limit}", "--format=%h %ad %an: %s", "--date=short"]
    else:
        args = ["log", f"-n{limit}", "--format=%h %ad %an: %s", "--date=short", "--follow", "--", path]
    code, out = git(folder, *args, timeout=timeout)
    if code != 0:
        return []
    return [line for line in out.splitlines() if line.strip() and not line.startswith(("diff ", "---", "+++", "@@"))]


@dataclass(frozen=True)
class BlameRange:
    start: int
    end: int
    sha: str
    author: str
    ts: int
    summary: str


def blame(conn: sqlite3.Connection | None, folder: Path, path: str, start: int, end: int, *,
          file_hash: str = "", timeout: float = 30) -> list[BlameRange]:
    """``git blame --porcelain`` for lines ``start``-``end`` of ``path`` (relative to ``folder``),
    merged into runs of lines from the same commit. Cached by the file's content hash."""
    key = (path, file_hash, start, end)
    if conn is not None and file_hash:
        row = conn.execute("SELECT data FROM blame_cache WHERE path = ? AND hash = ? AND start = ? AND end = ?", key).fetchone()
        if row is not None:
            return [BlameRange(*item) for item in json.loads(str(row[0]))]
    code, out = git(folder, "blame", "--porcelain", f"-L{start},{end}", "--", path, timeout=timeout)
    if code != 0:
        return []
    meta: dict[str, dict[str, str]] = {}
    lines: list[tuple[int, str]] = []
    current = ""
    for row in out.splitlines():
        head = re.match(r"^([0-9a-f]{40}) \d+ (\d+)", row)
        if head:
            current = head.group(1)
            meta.setdefault(current, {})
            lines.append((int(head.group(2)), current))
            continue
        if row.startswith("\t") or not current:
            continue
        key_name, _, value = row.partition(" ")
        if key_name in {"author", "author-time", "summary"}:
            meta[current][key_name] = value
    runs: list[BlameRange] = []
    for number, sha in sorted(lines):
        info = meta.get(sha, {})
        if runs and runs[-1].sha == sha and runs[-1].end == number - 1:
            last = runs[-1]
            runs[-1] = BlameRange(last.start, number, sha, last.author, last.ts, last.summary)
        else:
            runs.append(BlameRange(number, number, sha, info.get("author", ""), int(info.get("author-time", "0") or 0),
                                   info.get("summary", "")))
    if conn is not None and file_hash:
        conn.execute("INSERT OR REPLACE INTO blame_cache (path, hash, start, end, data) VALUES (?, ?, ?, ?, ?)",
                     (*key, json.dumps([[r.start, r.end, r.sha, r.author, r.ts, r.summary] for r in runs])))
        conn.commit()
    return runs
