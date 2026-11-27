"""Which index databases answer a question, and building them in the background.

- A plain project has one database (``.crit/cache/index.db``), refreshed by the engine at
  task start. If it does not exist yet, the first tool call builds it in a background thread.
- On a ``repo``-tool tree (AOSP) a question about a path goes to that path's module group (its
  git project, such as ``packages/services/Car``), whose database is created on first use in a
  background thread. A question without a path goes to the project database and every group
  database built so far.

A tool waits a moment (:data:`WAIT_SECONDS`) for a build to finish, then answers from what is
indexed and says the result is partial.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from critique_bot import log
from critique_bot.indexing import store

#: How long a tool waits for a background build before answering with partial results.
WAIT_SECONDS = 1.5
#: How long a tool waits for the history (git log) of a database before answering without it.
HISTORY_WAIT_SECONDS = 4.0


@dataclass
class _Job:
    label: str
    started: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    error: str = ""


_jobs: dict[str, _Job] = {}
_lock = threading.Lock()


def _start(key: str, label: str, work) -> _Job:
    """Run ``work`` once per key in a daemon thread; a finished job can be started again."""
    with _lock:
        job = _jobs.get(key)
        if job is not None and not job.done.is_set():
            return job
        job = _Job(label)
        _jobs[key] = job

    def run() -> None:
        try:
            work()
        except Exception as exc:  # a failed build must not take the session down
            job.error = f"{type(exc).__name__}: {exc}"
            log.debug(f"index job {label} failed: {job.error}")
        finally:
            job.done.set()

    threading.Thread(target=run, name=f"crit-index-{label}", daemon=True).start()
    return job


def running(key: str) -> _Job | None:
    with _lock:
        job = _jobs.get(key)
    return job if job is not None and not job.done.is_set() else None


@dataclass
class Target:
    """One database to query, and why its answers may be incomplete."""

    path: Path
    group: str = ""  # the module group ("" for the project database)
    partial: str = ""

    def connect(self) -> sqlite3.Connection | None:
        if not self.path.is_file():
            return None
        conn = store.connect(self.path)
        if not store.is_current(conn):
            conn.close()
            return None
        return conn


def _build_main(workspace: Path, index_path: Path) -> None:
    from critique_bot import code_index

    code_index.refresh_index(workspace, index_path)


def _build_group(workspace: Path, db: Path, group: str) -> None:
    from critique_bot import code_index

    db.parent.mkdir(parents=True, exist_ok=True)
    conn = store.connect(db)
    try:
        store.migrate(conn)
        import json

        store.set_meta(conn, "scope", json.dumps([group]))
        store.set_meta(conn, "group", group)
        conn.commit()
    finally:
        conn.close()
    code_index.refresh_index(workspace, db, only=[group])
    conn = store.connect(db)
    try:
        store.set_meta(conn, "built", str(int(time.time())))
        conn.commit()
    finally:
        conn.close()


def _wait(job: _Job | None, seconds: float) -> _Job | None:
    if job is None:
        return None
    job.done.wait(seconds)
    return None if job.done.is_set() else job


def _partial_note(job: _Job, what: str) -> str:
    age = int(time.monotonic() - job.started)
    return f"partial: the index of {what} is still being built ({age}s so far); results may miss files"


def repo_tree(workspace: Path) -> bool:
    from critique_bot import file_catalog

    return file_catalog.catalog_for(Path(workspace)).kind == "repo"


def group_of(workspace: Path, rel: str) -> str:
    from critique_bot import file_catalog

    return file_catalog.catalog_for(Path(workspace)).project_of(rel) or "" if rel else ""


def targets(workspace: Path, index_path: Path | None, rel: str = "", *, wait: float = WAIT_SECONDS) -> list[Target]:
    """The databases that answer a question about ``rel`` (or the whole project)."""
    if index_path is None:
        return []
    workspace = Path(workspace)
    index_path = Path(index_path)
    if repo_tree(workspace):
        group = group_of(workspace, rel) if rel else ""
        if group:
            db = store.group_db_path(index_path, group)
            key = str(db)
            job = running(key)
            if job is None and not _built(db):
                job = _start(key, group, lambda: _build_group(workspace, db, group))
            job = _wait(job, wait)
            return [Target(db, group, _partial_note(job, group) if job else "")]
        out = []
        if index_path.is_file():
            out.append(Target(index_path))
        for db in store.group_dbs(index_path):
            job = running(str(db))
            out.append(Target(db, db.stem, _partial_note(job, db.stem) if job else ""))
        if not out:
            out.append(Target(index_path, "", "partial: no module of this repo tree is indexed yet; pass a path to index its module"))
        return out
    key = str(index_path)
    job = running(key)
    if job is None and not index_path.is_file():
        job = _start(key, "project", lambda: _build_main(workspace, index_path))
    job = _wait(job, wait)
    return [Target(index_path, "", _partial_note(job, "this project") if job else "")]


def _built(db: Path) -> bool:
    if not db.is_file():
        return False
    try:
        conn = store.connect(db)
        try:
            return store.is_current(conn) and bool(store.get_meta(conn, "built"))
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def ensure_history(workspace: Path, target: Target, *, wait: float = HISTORY_WAIT_SECONDS) -> str:
    """Bring the git history in ``target`` up to date (in the background). Returns a partial note or ""."""
    from critique_bot.indexing import history

    workspace = Path(workspace)
    found = history.project_for(workspace, target.group) if target.group else history.project_for(workspace)
    if found is None:
        return "no git history here"
    folder, prefix = found
    key = f"history:{target.path}:{prefix}"

    def work() -> None:
        conn = store.connect(target.path)
        try:
            if store.migrate(conn):
                history.update(conn, folder, prefix)
        finally:
            conn.close()

    if not target.path.is_file():
        return "partial: the index is not built yet"
    job = running(key)
    if job is None:
        if _history_current(target.path, folder, prefix):
            return ""
        job = _start(key, f"history {prefix or 'project'}", work)
    job = _wait(job, wait)
    if job is None:
        return ""
    return f"partial: git history is still being read ({int(time.monotonic() - job.started)}s so far)"


def _history_current(db: Path, folder: Path, prefix: str) -> bool:
    from critique_bot.indexing import history

    code, head = history.git(folder, "rev-parse", "HEAD", timeout=10)
    if code != 0:
        return True  # nothing to read
    conn = store.connect(db)
    try:
        return store.get_meta(conn, f"history_head:{prefix}") == head.strip()
    finally:
        conn.close()


def wait_all(seconds: float = 60.0) -> None:
    """Wait for every background job (tests, benchmarks)."""
    deadline = time.monotonic() + seconds
    while True:
        with _lock:
            pending = [job for job in _jobs.values() if not job.done.is_set()]
        if not pending or time.monotonic() > deadline:
            return
        pending[0].done.wait(max(0.0, deadline - time.monotonic()))
