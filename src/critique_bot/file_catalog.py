"""A cached list of workspace files that scales to AOSP-size trees.

Listing files was a full ``git ls-files`` (or an ``os.walk``) on every tool call. On a
``repo``-tool checkout (a ``.repo`` folder and about a thousand git projects, no ``.git``
at the root) that meant walking millions of files several times per call.

The catalog keeps the list in memory, one sorted list per git project, and re-lists a
project only when it changed. A change is seen from a few cheap ``stat`` calls per project
(its ``HEAD``, its index, its top folder), never from statting every file. The agent's own
writes and deletes update the list directly (:meth:`FileCatalog.note_write`).

Kinds of workspace:

- **repo tree:** projects come from ``.repo/project.list`` (written by ``repo sync``) or
  the manifest. ``out/`` and ``prebuilts/`` projects are left out unless asked for.
- **one git checkout:** small repositories (under ``BIG_PROJECT`` files) are re-listed on
  every call, exactly as before; big ones are cached like a repo tree's projects.
- **no git:** ``os.walk`` with the usual skip list, cached only when the tree is big.

Skips: tracked files are trusted (git already applies ``.gitignore``), so only
:data:`GIT_SKIP` applies to them. That keeps AOSP's real ``build/`` folder, which the
name-anywhere skip list used to hide. Untracked walks still use ``SKIP_DIR_NAMES``.
"""

from __future__ import annotations

import bisect
import os
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from critique_bot.code_index import SKIP_DIR_NAMES

# Folders skipped even when git lists files in them.
GIT_SKIP = frozenset({".git", ".crit", ".bot", ".repo", "node_modules", "__pycache__", ".venv", "venv"})
# Top-level folders of a repo tree that hold data or build output, not source to edit.
REPO_ROOT_SKIP = ("out", "prebuilts")
# A project with more files than this is cached even in a plain checkout.
BIG_PROJECT = 50_000
# A walked (non-git) tree with more files than this is cached for WALK_TTL seconds.
BIG_WALK = 20_000
WALK_TTL = 30.0
# How often (seconds) project signatures are checked again.
CHECK_EVERY = 2.0
LIST_WORKERS = 8
GIT_TIMEOUT = 300


@dataclass
class _Project:
    path: str  # workspace-relative, "" for the root checkout
    signature: tuple = ()
    files: list[str] = field(default_factory=list)  # workspace-relative, sorted
    listed: bool = False


def _git_dir(folder: Path) -> Path | None:
    dot = folder / ".git"
    try:
        if dot.is_dir():
            return dot.resolve()
        if dot.is_file():
            text = dot.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir:"):
                target = Path(text[7:].strip())
                if not target.is_absolute():
                    target = folder / target
                return target.resolve()
    except OSError:
        return None
    return None


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _signature(folder: Path) -> tuple:
    """Cheap change marker for one git project: HEAD, index, and top folder mtimes."""
    git = _git_dir(folder)
    if git is None:
        return ("nogit", _mtime(folder))
    try:
        head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        head = ""
    ref_mtime = 0
    if head.startswith("ref:"):
        ref = head[4:].strip()
        ref_mtime = _mtime(git / ref)
        # worktrees keep refs in the common dir
        if not ref_mtime and (git / "commondir").is_file():
            try:
                common = (git / (git / "commondir").read_text().strip()).resolve()
                ref_mtime = _mtime(common / ref)
            except OSError:
                pass
    return (head, ref_mtime, _mtime(git / "index"), _mtime(folder))


def _ls_files(folder: Path) -> list[str] | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(folder), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = []
    seen = set()
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        item = raw.decode("utf-8", errors="replace")
        if item in seen:  # --cached and --others overlap on unmerged paths
            continue
        seen.add(item)
        if any(part in GIT_SKIP for part in item.split("/")[:-1]):
            continue
        out.append(item)
    return out


def repo_projects(workspace: Path) -> list[str] | None:
    """Project paths of a ``repo``-tool checkout, or None when ``workspace`` is not one."""
    repo = workspace / ".repo"
    if not repo.is_dir():
        return None
    listed = repo / "project.list"
    if listed.is_file():
        try:
            rows = [line.strip().strip("/") for line in listed.read_text(encoding="utf-8").splitlines()]
            return sorted({row for row in rows if row})
        except OSError:
            pass
    return _manifest_projects(repo)


def _manifest_projects(repo: Path) -> list[str] | None:
    root = repo / "manifest.xml"
    if not root.exists():
        return None
    paths: dict[str, str] = {}
    seen: set[Path] = set()

    def load(path: Path) -> None:
        if path in seen or not path.is_file():
            return
        seen.add(path)
        try:
            tree = ET.parse(path)  # nosec B314 - the repo tool's own manifest in the user's checkout
        except (ET.ParseError, OSError):
            return
        for node in tree.getroot():
            if node.tag == "include":
                name = node.get("name") or ""
                load(repo / "manifests" / name)
            elif node.tag == "project":
                name = node.get("name") or ""
                if name:
                    paths[name] = (node.get("path") or name).strip("/")
            elif node.tag == "remove-project":
                paths.pop(node.get("name") or "", None)

    try:
        load(root.resolve())
    except OSError:
        return None
    return sorted(set(paths.values())) if paths else None


class FileCatalog:
    """The files of one workspace, cached and kept current. Thread-safe."""

    def __init__(self, workspace: Path, *, include_data: bool = False) -> None:
        self.workspace = Path(workspace).resolve()
        self.include_data = include_data
        self._lock = threading.RLock()
        self._projects: list[_Project] | None = None
        self._kind = ""
        self._checked = 0.0
        self._walk: list[str] | None = None
        self._walk_at = 0.0
        self._all: list[str] | None = None  # merged, sorted

    # ------------------------------------------------------------------ setup
    @property
    def kind(self) -> str:
        with self._lock:
            self._setup()
            return self._kind

    def _setup(self) -> None:
        if self._projects is not None or self._kind == "walk":
            return
        listed = repo_projects(self.workspace)
        if listed is not None:
            if not self.include_data:
                listed = [p for p in listed if p.split("/", 1)[0] not in REPO_ROOT_SKIP]
            self._kind = "repo"
            self._projects = [_Project(path=p) for p in listed]
        elif _git_dir(self.workspace) is not None:
            self._kind = "git"
            self._projects = [_Project(path="")]
        else:
            self._kind = "walk"

    def projects(self) -> list[str]:
        """Git project paths (``""`` for a plain checkout); empty without git."""
        with self._lock:
            self._setup()
            return [p.path for p in self._projects or []]

    def project_of(self, rel_path: str) -> str | None:
        """The project holding ``rel_path`` (longest matching path), or None."""
        best = None
        for path in self.projects():
            if path == "" or rel_path == path or rel_path.startswith(path + "/"):
                if best is None or len(path) > len(best):
                    best = path
        return best

    # ------------------------------------------------------------------ listing
    def files(self, prefix: str = "") -> list[str]:
        """Workspace-relative files, sorted, under the folder ``prefix`` ("" for all)."""
        prefix = prefix.strip("/")
        if prefix in {".", ""}:
            prefix = ""
        with self._lock:
            self._setup()
            if self._kind == "walk":
                fresh = self._walk is not None and time.monotonic() - self._walk_at < WALK_TTL
                if prefix and not (fresh and len(self._walk or []) > BIG_WALK):
                    # a small or uncached tree: walk only the folder asked for
                    return sorted(self._walk_dir(self.workspace / prefix))
                rows = self._walked()
            else:
                rows = self._listed()
        if not prefix:
            return list(rows)
        start = bisect.bisect_left(rows, prefix + "/")
        end = bisect.bisect_left(rows, prefix + "0")  # "0" sorts right after "/"
        out = rows[start:end]
        index = bisect.bisect_left(rows, prefix)
        if index < len(rows) and rows[index] == prefix:
            out = [prefix, *out]
        return out

    def _listed(self) -> list[str]:
        now = time.monotonic()
        projects = self._projects or []
        small = bool(projects) and self._kind == "git" and len(projects[0].files) < BIG_PROJECT
        if self._all is not None and not small and now - self._checked < CHECK_EVERY:
            return self._all
        self._checked = now
        stale: list[_Project] = []
        for project in projects:
            folder = self.workspace / project.path if project.path else self.workspace
            sig = _signature(folder)
            small_checkout = self._kind == "git" and len(project.files) < BIG_PROJECT
            if not project.listed or sig != project.signature or small_checkout:
                project.signature = sig
                stale.append(project)
        if stale:
            self._relist(stale)
            self._all = None
        if self._all is None:
            if len(projects) == 1:
                self._all = projects[0].files
            else:
                merged: list[str] = []
                for project in projects:
                    merged.extend(project.files)
                merged.sort()
                self._all = merged
        return self._all

    def _relist(self, stale: list[_Project]) -> None:
        def one(project: _Project) -> tuple[_Project, list[str] | None]:
            folder = self.workspace / project.path if project.path else self.workspace
            if not folder.is_dir():
                return project, []
            return project, _ls_files(folder)

        workers = min(LIST_WORKERS, len(stale))
        if workers <= 1:
            results = [one(p) for p in stale]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(one, stale))
        for project, listed in results:
            if listed is None:
                listed = self._walk_dir(self.workspace / project.path if project.path else self.workspace)
                listed = [item[len(project.path) + 1 :] if project.path else item for item in listed]
            prefix = project.path + "/" if project.path else ""
            project.files = sorted(prefix + item for item in listed)
            project.listed = True

    def _walk_dir(self, root: Path) -> list[str]:
        rows: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
            base = Path(dirpath)
            for name in filenames:
                try:
                    rows.append((base / name).relative_to(self.workspace).as_posix())
                except ValueError:
                    continue
        return rows

    def _walked(self) -> list[str]:
        now = time.monotonic()
        if self._walk is not None and len(self._walk) > BIG_WALK and now - self._walk_at < WALK_TTL:
            return self._walk
        self._walk = sorted(self._walk_dir(self.workspace))
        self._walk_at = now
        return self._walk

    # ------------------------------------------------------------------ updates
    def note_write(self, rel_path: str) -> None:
        """Record a file the agent created or changed, without re-listing."""
        rel_path = rel_path.strip("/")
        with self._lock:
            for rows in self._lists(rel_path):
                index = bisect.bisect_left(rows, rel_path)
                if index >= len(rows) or rows[index] != rel_path:
                    rows.insert(index, rel_path)

    def note_delete(self, rel_path: str) -> None:
        rel_path = rel_path.strip("/")
        with self._lock:
            for rows in self._lists(rel_path):
                index = bisect.bisect_left(rows, rel_path)
                if index < len(rows) and rows[index] == rel_path:
                    del rows[index]

    def _lists(self, rel_path: str) -> list[list[str]]:
        """Each cached list that should hold ``rel_path``, each list once."""
        out: list[list[str]] = []

        def add(rows: list[str] | None) -> None:
            if rows is not None and not any(rows is seen for seen in out):
                out.append(rows)

        add(self._walk)
        add(self._all)
        owner = None
        for project in self._projects or []:
            if not project.listed:
                continue
            if project.path == "" or rel_path.startswith(project.path + "/"):
                if owner is None or len(project.path) > len(owner.path):
                    owner = project
        if owner is not None:
            add(owner.files)
        return out

    def invalidate(self) -> None:
        """Forget everything (after a sync, checkout, or rebase)."""
        with self._lock:
            self._projects = None
            self._kind = ""
            self._all = None
            self._walk = None
            self._checked = 0.0


_CATALOGS: dict[Path, FileCatalog] = {}
_CATALOGS_LOCK = threading.Lock()


def catalog_for(workspace: Path) -> FileCatalog:
    """The shared catalog of ``workspace`` (one per resolved path)."""
    key = Path(workspace).resolve()
    with _CATALOGS_LOCK:
        found = _CATALOGS.get(key)
        if found is None:
            found = FileCatalog(key)
            _CATALOGS[key] = found
        return found


def forget(workspace: Path | None = None) -> None:
    """Drop cached catalogs (all of them, or one workspace's)."""
    with _CATALOGS_LOCK:
        if workspace is None:
            _CATALOGS.clear()
        else:
            _CATALOGS.pop(Path(workspace).resolve(), None)
