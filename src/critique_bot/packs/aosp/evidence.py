# SPDX-License-Identifier: Apache-2.0
"""From a log's evidence to the source tree: files, owning modules, and suspect commits.

The triage pipelines extract *evidence* (a stack frame, a log message, a symbol, a file:line
from a compiler error); this module maps it to the tree with the Stage 8 index when there is
one (``find_strings``-style string search, frame resolution, symbol lookup) and with a bounded
scan of the workspace when there is none, then to the owning Soong module (``soong``), and
ranks suspect commits from git (``indexing.history``: blame of the exact lines, ``git log -G``
for a vanished symbol, recent changes to the module).

Every answer says where it came from, so the report can refuse to guess when nothing
resolves (Blueprint First's refusal guard).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from critique_bot import soong

#: Folders never scanned by the fallback (build output, VCS data, prebuilts, caches).
SKIP_DIRS = frozenset({".git", ".repo", ".crit", "out", "prebuilts", "node_modules", "build-cache", ".gradle",
                       "__pycache__", ".idea", "dist"})
SOURCE_SUFFIXES = (".java", ".kt", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".aidl", ".rs", ".py", ".rc", ".te",
                   ".go", ".bp", ".mk", ".xml")
#: How long the fallback scan may take, and how many files it lists.
SCAN_SECONDS = 3.0
SCAN_FILES = 200_000
_FRAMEWORK = ("java.", "javax.", "kotlin.", "kotlinx.", "android.", "androidx.", "dalvik.", "com.android.internal.",
              "libcore.", "sun.", "jdk.", "org.junit.", "junit.")


@dataclass
class Place:
    path: str  # workspace-relative
    line: int = 0
    symbol: str = ""
    how: str = ""  # "index", "scan", "path"

    def text(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return where + (f" ({self.symbol})" if self.symbol else "")


@dataclass
class Suspect:
    sha: str
    date: str
    author: str
    subject: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def text(self) -> str:
        return f"{self.sha[:10]} {self.date} {self.author[:24]}: {self.subject[:90]}  [{'; '.join(self.reasons[:3])}]"


def evidence_suffixes() -> tuple[str, ...]:
    return tuple(suffix for suffix in SOURCE_SUFFIXES if suffix not in {".bp", ".mk", ".xml", ".te"})


def is_framework(qualified: str) -> bool:
    return qualified.startswith(_FRAMEWORK)


class Resolver:
    """Finds evidence in one workspace; index first, a bounded scan second."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.workspace = Path(getattr(ctx, "workspace", ".") or ".")
        self.notes: list[str] = []
        self._files: list[str] | None = None
        self._dbs: Any = None

    # ------------------------------------------------------------------ index access

    def _conns(self, rel: str = "") -> list[Any]:
        if getattr(self.ctx, "index_path", None) is None:
            return []
        try:
            from critique_bot.indexing import tools as index_tools

            if self._dbs is None:
                self._dbs = index_tools._Dbs(self.ctx, rel).__enter__()
                self.notes += [note for note in self._dbs.notes if note]
            return list(self._dbs.conns)
        except Exception:  # noqa: BLE001 - the index is an accelerator, never a requirement
            return []

    def close(self) -> None:
        if self._dbs is not None:
            try:
                self._dbs.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._dbs = None

    # ------------------------------------------------------------------ the fallback scan

    def files(self) -> list[str]:
        if self._files is not None:
            return self._files
        found: list[str] = []
        deadline = time.monotonic() + SCAN_SECONDS
        root = str(self.workspace)
        for folder, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            rel_dir = os.path.relpath(folder, root)
            for name in names:
                if name.endswith(SOURCE_SUFFIXES) or name in {"Android.bp", "TEST_MAPPING", "Android.mk"}:
                    found.append(name if rel_dir == "." else f"{rel_dir}/{name}".replace(os.sep, "/"))
            if len(found) > SCAN_FILES or time.monotonic() > deadline:
                self.notes.append("partial: the workspace scan stopped early (index the module for full results)")
                break
        self._files = found
        return found

    # ------------------------------------------------------------------ questions

    def file(self, name: str, container: str = "") -> list[Place]:
        """Files called ``name`` (``Foo.java``, ``a/b/foo.cpp``); a Java class name narrows by package."""
        name = name.replace("\\", "/").strip()
        if not name:
            return []
        if Path(name).is_absolute():  # addr2line prints the compile directory's absolute path
            try:
                name = Path(name).resolve().relative_to(self.workspace.resolve()).as_posix()
            except (ValueError, OSError):
                pass
        direct = (self.workspace / name.lstrip("/")).resolve()
        try:
            rel = direct.relative_to(self.workspace.resolve()).as_posix()
            if direct.is_file():
                return [Place(rel, how="path")]
        except (ValueError, OSError):
            pass
        for conn in self._conns():
            try:
                from critique_bot.indexing import locate as index_locate

                rows = index_locate._file_for(conn, name, container)
            except Exception:  # noqa: BLE001
                rows = []
            rows = self._same_package(rows, container)
            if rows:
                return [Place(row, how="index") for row in rows[:3]]
        base = name.rpartition("/")[2]
        rows = [rel for rel in self.files() if rel == base or rel.endswith("/" + base)]
        if "/" in name:
            tail = name.lstrip("./")
            rows = [rel for rel in rows if rel.endswith(tail)] or rows
        rows = self._same_package(rows, container)
        return [Place(rel, how="scan") for rel in rows[:3]]

    def _same_package(self, rows: list[str], container: str) -> list[str]:
        """For a Java/Kotlin frame, only files of the frame's package: a file of the same name in
        another package is a different class (com.vendor.x.Foo is not com.android.y.Foo)."""
        if not container or "." not in container or not rows:
            return rows
        package = ".".join(part for part in container.split(".")[:-1] if part[:1].islower() or part[:1] == "_")
        if not package:
            return rows
        by_path = [rel for rel in rows if package.replace(".", "/") in rel]
        if by_path:
            return by_path
        declared = re.compile(r"^\s*package\s+" + re.escape(package) + r"\s*;?\s*$", re.M)
        out = []
        for rel in rows:
            if not rel.endswith((".java", ".kt", ".kts")):
                out.append(rel)
                continue
            try:
                head = (self.workspace / rel).read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
            if declared.search(head):
                out.append(rel)
        return out

    def strings(self, message: str, *, limit: int = 5) -> list[Place]:
        """Code that emits ``message`` (numbers and ids may differ)."""
        message = message.strip()
        if len(message) < 6:
            return []
        for conn in self._conns():
            try:
                from critique_bot.indexing import locate as index_locate

                hits = index_locate.search_strings(conn, message, limit=limit)
            except Exception:  # noqa: BLE001
                hits = []
            if hits:
                return [Place(h.path, h.line, h.container, "index") for h in hits[:limit]]
        fragment = _longest_fragment(message)
        if len(fragment) < 6:
            return []
        out: list[Place] = []
        deadline = time.monotonic() + SCAN_SECONDS
        for rel in self.files():
            if not rel.endswith((".java", ".kt", ".c", ".cc", ".cpp", ".h", ".hpp", ".rs", ".py", ".go", ".rc")):
                continue
            if time.monotonic() > deadline:
                self.notes.append("partial: the string scan stopped early")
                break
            try:
                text = (self.workspace / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            index = text.find(fragment)
            if index >= 0:
                out.append(Place(rel, text.count("\n", 0, index) + 1, "", "scan"))
                if len(out) >= limit:
                    break
        return out

    def symbol(self, name: str, *, limit: int = 5) -> list[Place]:
        """Where ``name`` (``Class.method`` or ``method``) is defined."""
        short = re.split(r"[.:]+", name.strip())[-1]
        parent = re.split(r"[.:]+", name.strip())[-2] if re.search(r"[.:]", name.strip()) else ""
        if not short:
            return []
        for conn in self._conns():
            try:
                rows = conn.execute("SELECT path, line, name, parent FROM symbols WHERE name = ? LIMIT 20",
                                    (short,)).fetchall()
            except Exception:  # noqa: BLE001
                rows = []
            if parent:
                rows = [row for row in rows if str(row[3] or "").endswith(parent)] or rows
            if rows:
                return [Place(str(r[0]), int(r[1]), f"{r[3]}.{r[2]}" if r[3] else str(r[2]), "index") for r in rows[:limit]]
        pattern = re.compile(r"\b" + re.escape(short) + r"\s*\(")
        out: list[Place] = []
        deadline = time.monotonic() + SCAN_SECONDS
        for rel in self.files():
            if not rel.endswith((".java", ".kt", ".c", ".cc", ".cpp", ".h", ".hpp", ".rs", ".go")):
                continue
            if parent and parent.lower() not in rel.lower():
                continue
            if time.monotonic() > deadline:
                break
            try:
                lines = (self.workspace / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, start=1):
                if pattern.search(line) and not line.rstrip().endswith(";") and not re.match(r"\s*(?:return|if|while)\b", line):
                    out.append(Place(rel, number, name, "scan"))
                    break
            if len(out) >= limit:
                break
        return out

    def enclosing(self, place: Place) -> str:
        """The function around ``place`` (index first, else the nearest definition-looking line above)."""
        if place.symbol or not place.line:
            return place.symbol
        for conn in self._conns():
            try:
                from critique_bot.indexing import locate as index_locate

                found = index_locate._enclosing(conn, place.path, place.line)
            except Exception:  # noqa: BLE001
                found = ""
            if found:
                return found
        try:
            lines = (self.workspace / place.path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        definition = re.compile(r"^\s*(?:[\w<>\[\],@]+\s+)+(\w+)\s*\([^;]*$")
        for number in range(min(place.line, len(lines)) - 1, -1, -1):
            match = definition.match(lines[number])
            if match and match.group(1) not in {"if", "for", "while", "switch", "catch", "return", "new"}:
                return match.group(1)
        return ""


def _longest_fragment(message: str) -> str:
    """The longest run of the message without numbers, ids or quotes (what a format string keeps)."""
    parts = re.split(r"0x[0-9a-fA-F]+|\d+|['\"`{}%]|\s{2,}", message)
    parts = [part.strip(" :=,;()[]") for part in parts]
    return max(parts, key=len) if parts else ""


# --------------------------------------------------------------------------- modules


def owning_module(workspace: Path, rel: str) -> str:
    """``CarService (android_app) in a/Android.bp`` for a file, or ""."""
    if PurePosixPath(rel).name in {"Android.bp", "Android.mk"}:
        return f"defined in {rel} itself"
    try:
        owner = soong.modules_for_file(Path(workspace), rel)
    except (OSError, ValueError):
        return ""
    if owner is None:
        return ""
    if not owner.modules:
        return f"listed in {owner.bp} ({', '.join(owner.via)}), no module builds it directly"
    names = ", ".join(f"{m.name} ({m.type})" for m in owner.modules[:3])
    via = f" via {', '.join(owner.via[:2])}" if owner.via else ""
    return f"{names} in {owner.bp}{via}"


def module_names(workspace: Path, rel: str) -> list[str]:
    try:
        owner = soong.modules_for_file(Path(workspace), rel)
    except (OSError, ValueError):
        return []
    return [m.name for m in owner.modules] if owner is not None else []


# --------------------------------------------------------------------------- suspect commits

def _line_changes(folder: Path, sha: str, name: str) -> tuple[int, int]:
    """How many removed and added lines of commit ``sha`` mention ``name``."""
    from critique_bot.indexing import history

    code, text = history.git(folder, "show", "--format=", "-U0", "--no-color", sha, timeout=20)
    if code != 0:
        return 0, 0
    word = re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")
    removed = sum(1 for line in text.splitlines() if line.startswith("-") and not line.startswith("---") and word.search(line))
    added = sum(1 for line in text.splitlines() if line.startswith("+") and not line.startswith("+++") and word.search(line))
    return removed, added


_LOG_LINE = re.compile(r"^([0-9a-f]{6,40}) (\d{4}-\d\d-\d\d) (.*?): (.*)$")


def suspects(workspace: Path, places: list[Place], *, symbols: list[str] | None = None,
             changes: list[tuple[str, str]] | None = None, blame_weight: float = 3.0, limit: int = 5,
             span: int = 3) -> tuple[list[Suspect], list[str]]:
    """Commits most likely to have caused the failure at ``places``, best first, and notes.

    Scores: a commit that last changed the failing line (blame) ``blame_weight`` (3; lower for a
    null dereference, whose cause is where the value was set), the line's neighbours half that;
    a commit that removed a symbol the build misses 4 (one that only added it 0.5); one matching ``changes``
    ((git regex, what it means), ``git log -G``): 3 for the newest, 2 for the next, 1 after; a
    recent commit to the file 1, to its module folder 0.5. Newer commits win ties.
    """
    from critique_bot.indexing import history

    workspace = Path(workspace)
    found: dict[str, Suspect] = {}
    notes: list[str] = []
    git_seen = False

    def add(sha: str, date: str, author: str, subject: str, score: float, reason: str) -> None:
        if not sha or set(sha) == {"0"}:
            return
        key = sha[:7]  # blame gives 40 characters, git log %h 7 or more: one commit either way
        item = found.get(key)
        if item is None:
            item = found[key] = Suspect(sha[:10], date, author, subject)
        item.score += score
        if reason not in item.reasons:
            item.reasons.append(reason)

    for rank, place in enumerate(places[:4]):
        located = history.project_for(workspace, place.path)
        if located is None:
            continue
        git_seen = True
        folder, prefix = located
        inner = place.path[len(prefix) + 1:] if prefix and place.path.startswith(prefix + "/") else place.path
        weight = 1.0 / (1 + rank)
        if place.line:
            runs = history.blame(None, folder, inner, max(1, place.line - span), place.line + span)
            for run in runs:
                exact = run.start <= place.line <= run.end
                day = time.strftime("%Y-%m-%d", time.gmtime(run.ts)) if run.ts else "?"
                add(run.sha, day, run.author, run.summary, (blame_weight if exact else blame_weight / 2) * weight,
                    f"{'last changed' if exact else 'changed lines next to'} {place.path}:{place.line}")
        for line in history.file_log(folder, inner, limit=3):
            match = _LOG_LINE.match(line)
            if match:
                add(match.group(1), match.group(2), match.group(3), match.group(4), 1.0 * weight,
                    f"recent change to {PurePosixPath(place.path).name}")
        module_dir = PurePosixPath(inner).parent.as_posix()
        owner = soong.modules_for_file(workspace, place.path) if place.path.endswith(evidence_suffixes()) else None
        if owner is not None:
            bp_dir = PurePosixPath(owner.bp).parent.as_posix()
            if prefix and bp_dir.startswith(prefix + "/"):
                module_dir = bp_dir[len(prefix) + 1:]
            elif not prefix:
                module_dir = bp_dir
        if module_dir not in {"", "."}:
            for line in history.file_log(folder, module_dir, limit=3):
                match = _LOG_LINE.match(line)
                if match:
                    add(match.group(1), match.group(2), match.group(3), match.group(4), 0.5 * weight,
                        f"recent change in {module_dir}/")
    located = history.project_for(workspace, places[0].path if places else "")
    if located is not None and (symbols or changes):
        git_seen = True
        # A symbol the build cannot find: the commit that removed (renamed) it, not the ones that added it.
        for name in [n for n in (symbols or [])[:3] if re.fullmatch(r"[A-Za-z_][\w.-]*", n)]:
            for line in history.log_g(located[0], r"\b" + name + r"\b", limit=4):
                match = _LOG_LINE.match(line)
                if not match:
                    continue
                removed, added = _line_changes(located[0], match.group(1), name)
                if removed > added:
                    add(match.group(1), match.group(2), match.group(3), match.group(4), 4.0, f"removed {name}")
                else:
                    add(match.group(1), match.group(2), match.group(3), match.group(4), 0.5, f"added {name}")
        # Lines whose history explains the failure (a null field's assignments): newest first.
        for regex, label in list(changes or [])[:3]:
            for order, line in enumerate(history.log_g(located[0], regex, limit=4)):
                match = _LOG_LINE.match(line)
                if match:
                    add(match.group(1), match.group(2), match.group(3), match.group(4), max(1.0, 3.0 - order), label)
    if not git_seen:
        notes.append("no git history for these files")
    ranked = sorted(found.values(), key=lambda s: (round(s.score, 2), s.date), reverse=True)
    return ranked[:limit], notes
