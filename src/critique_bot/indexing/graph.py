# SPDX-License-Identifier: Apache-2.0
"""L3: the code graph. Import resolution per language, callers and callees, the repo map.

Import edges are resolved to files when the index is refreshed (:func:`resolve_imports`).
Call edges are stored by the callee's short name and matched to definitions when asked, so
they stay right after any file changes; they are name matches and are reported as "likely".
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

_SOURCE_SUFFIXES = {
    "python": (".py", "/__init__.py", ".pyi"),
    "java": (".java", ".kt", ".aidl"),
    "kotlin": (".kt", ".java", ".kts"),
    "aidl": (".aidl", ".java"),
    "csharp": (".cs",),
}
_JS_SUFFIXES = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", "/index.ts", "/index.tsx", "/index.js", "/index.jsx")
_MAX_SUFFIX_PARTS = 8


class _Files:
    """Lookups over the indexed paths: exact, by trailing path components, by folder."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = set(paths)
        self.by_suffix: dict[str, list[str]] = defaultdict(list)
        self.dirs: dict[str, list[str]] = defaultdict(list)
        self.dir_suffix: dict[str, list[str]] = defaultdict(list)
        for path in sorted(paths):
            parts = path.split("/")
            for count in range(1, min(len(parts), _MAX_SUFFIX_PARTS) + 1):
                self.by_suffix["/".join(parts[-count:])].append(path)
            folder = "/".join(parts[:-1])
            if path.endswith(".go"):
                if folder not in self.dirs:
                    dparts = folder.split("/") if folder else []
                    for count in range(1, min(len(dparts), _MAX_SUFFIX_PARTS) + 1):
                        self.dir_suffix["/".join(dparts[-count:])].append(folder)
                self.dirs[folder].append(path)

    def suffix(self, tail: str, near: str) -> str | None:
        """The file whose path ends with ``tail``; the one closest to ``near`` when several do."""
        if tail in self.paths:
            return tail
        hits = self.by_suffix.get(tail)
        if not hits:
            return None
        if len(hits) == 1:
            return hits[0]
        return min(hits, key=lambda item: (-_common(item, near), len(item), item))


def _common(left: str, right: str) -> int:
    count = 0
    for a, b in zip(left.split("/"), right.split("/")):
        if a != b:
            break
        count += 1
    return count


def _norm(path: str) -> str:
    out: list[str] = []
    for part in path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if out:
                out.pop()
            continue
        out.append(part)
    return "/".join(out)


def _language(path: str) -> str:
    from critique_bot.indexing.langs import language_of

    return language_of(path)


def _python_root_hit(files: _Files, tail: str, src: str) -> str | None:
    """A file ending in ``tail`` that sits under an import root (a folder that is not a package)."""
    hits = [tail] if tail in files.paths else []
    hits += [item for item in files.by_suffix.get(tail, []) if item != tail]
    good = []
    for hit in hits:
        prefix = hit[: len(hit) - len(tail)].rstrip("/")
        if not prefix or f"{prefix}/__init__.py" not in files.paths:
            good.append(hit)
    if not good:
        return None
    return min(good, key=lambda item: (-_common(item, src), len(item), item))


def resolve_one(name: str, src: str, files: _Files) -> str | None:
    """The file (or, for Go, the package folder + "/") an import names, or None."""
    language = _language(src)
    folder = str(PurePosixPath(src).parent)
    folder = "" if folder == "." else folder
    if language == "python":
        level = len(name) - len(name.lstrip("."))
        dotted = name.lstrip(".")
        base = folder
        for _ in range(max(0, level - 1)):
            base = str(PurePosixPath(base).parent) if base else ""
            base = "" if base == "." else base
        parts = dotted.split(".") if dotted else []
        while True:
            stem = "/".join(parts)
            for suffix in (".py", "/__init__.py", ".pyi"):
                if not stem and suffix != "/__init__.py":
                    continue
                tail = (stem + suffix) if stem else "__init__.py"
                if level:
                    hit = _norm(f"{base}/{tail}") if base else _norm(tail)
                    if hit in files.paths:
                        return hit
                else:
                    hit = _python_root_hit(files, tail, src)
                    if hit:
                        return hit
            if not parts:
                return None
            parts = parts[:-1]
            if not parts and not level:
                return None
    if language in {"java", "kotlin", "aidl", "csharp"}:
        parts = name.split(".")
        while len(parts) >= 2:
            for suffix in _SOURCE_SUFFIXES.get(language, (".java",)):
                hit = files.suffix("/".join(parts) + suffix, src)
                if hit:
                    return hit
            parts = parts[:-1]
        return None
    if language == "hidl":
        # android.hardware.foo@1.0::IFoo -> android/hardware/foo/1.0/IFoo.hal (AOSP: hardware/interfaces/foo/1.0/IFoo.hal)
        package, _, item = name.partition("::")
        pkg, _, ver = package.partition("@")
        tail = "/".join(pkg.split(".")[2:] + [ver, (item or "types") + ".hal"])
        return files.suffix(tail, src)
    if language in {"c", "cpp"}:
        local = _norm(f"{folder}/{name}") if folder else _norm(name)
        if local in files.paths:
            return local
        return files.suffix(_norm(name), src)
    if language == "go":
        parts = name.split("/")
        for count in range(min(len(parts), _MAX_SUFFIX_PARTS), 0, -1):
            hits = files.dir_suffix.get("/".join(parts[-count:]))
            if hits:
                best = min(hits, key=lambda item: (-_common(item, folder), len(item)))
                return best + "/"
        return None
    if language in {"javascript", "typescript"}:
        if not name.startswith("."):
            return None
        base = _norm(f"{folder}/{name}") if folder else _norm(name)
        for suffix in _JS_SUFFIXES:
            if base + suffix in files.paths:
                return base + suffix
        return None
    if language == "rust":
        if name.startswith("mod:"):
            module = name[4:]
            stem = PurePosixPath(src).stem
            own = folder if stem in {"mod", "lib", "main"} else (f"{folder}/{stem}" if folder else stem)
            for tail in (f"{module}.rs", f"{module}/mod.rs"):
                candidate = _norm(f"{own}/{tail}") if own else tail
                if candidate in files.paths:
                    return candidate
            return None
        parts = name.split("::")
        if parts and parts[0] in {"crate", "self", "super"}:
            root = folder
            head = parts.pop(0)
            if head == "crate":
                # the crate root: the nearest folder above with lib.rs or main.rs
                probe = folder
                while True:
                    if f"{probe}/lib.rs" in files.paths or f"{probe}/main.rs" in files.paths or (
                        not probe and ("lib.rs" in files.paths or "main.rs" in files.paths)
                    ):
                        root = probe
                        break
                    if not probe:
                        root = folder
                        break
                    probe = str(PurePosixPath(probe).parent)
                    probe = "" if probe == "." else probe
            elif head == "super":
                root = str(PurePosixPath(folder).parent) if folder else ""
                root = "" if root == "." else root
            while parts:
                stem = "/".join(parts)
                for tail in (f"{stem}.rs", f"{stem}/mod.rs"):
                    candidate = _norm(f"{root}/{tail}") if root else tail
                    if candidate in files.paths:
                        return candidate
                parts = parts[:-1]
        return None
    return None


def resolve_imports(conn: sqlite3.Connection, *, sources: list[str] | None = None) -> int:
    """Set ``dst_path`` of import edges (from ``sources``, or all of them). Returns how many resolved."""
    files = _Files([str(row[0]) for row in conn.execute("SELECT path FROM files")])
    if sources is None:
        rows = conn.execute("SELECT rowid, src_path, dst_name FROM edges WHERE kind = 'imports'").fetchall()
    else:
        rows = []
        for start in range(0, len(sources), 500):
            chunk = sources[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows += conn.execute(
                f"SELECT rowid, src_path, dst_name FROM edges WHERE +kind = 'imports' AND src_path IN ({marks})", chunk
            ).fetchall()
    updates = []
    resolved = 0
    for rowid, src, name in rows:
        target = resolve_one(str(name), str(src), files)
        if target == src:
            target = None
        resolved += target is not None
        updates.append((target, rowid))
    conn.executemany("UPDATE edges SET dst_path = ? WHERE rowid = ?", updates)
    return resolved


# --------------------------------------------------------------------------- queries


@dataclass(frozen=True)
class Edge:
    src_path: str
    src_symbol: str
    kind: str
    dst_name: str
    dst_path: str
    line: int


def imports_of(conn: sqlite3.Connection, path: str) -> list[Edge]:
    rows = conn.execute(
        "SELECT src_path, src_symbol, kind, dst_name, dst_path, line FROM edges "
        "WHERE src_path = ? AND +kind = 'imports' ORDER BY line",
        (path,),
    ).fetchall()
    return [Edge(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4] or ""), int(r[5])) for r in rows]


def imported_by(conn: sqlite3.Connection, path: str, *, limit: int = 200) -> list[Edge]:
    folder = str(PurePosixPath(path).parent)
    targets = [path] + ([folder + "/"] if path.endswith(".go") and folder != "." else [])
    marks = ",".join("?" * len(targets))
    rows = conn.execute(
        "SELECT src_path, src_symbol, kind, dst_name, dst_path, line FROM edges "
        f"WHERE +kind = 'imports' AND dst_path IN ({marks}) ORDER BY src_path, line LIMIT ?",
        (*targets, limit),
    ).fetchall()
    return [Edge(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4] or ""), int(r[5])) for r in rows]


def split_symbol(symbol: str) -> tuple[str, str]:
    """``Parent.name`` / ``Parent::name`` / ``name`` -> (parent, name)."""
    text = symbol.replace("::", ".").strip().rstrip("()")
    parent, _, name = text.rpartition(".")
    return parent.rpartition(".")[2], name


def callers(conn: sqlite3.Connection, symbol: str, *, limit: int = 60) -> list[tuple[Edge, int]]:
    """Call edges whose callee name is ``symbol``'s name, best first: ``(edge, score)``.

    The score rises when the caller's file imports or sits next to a file that defines the
    symbol, or is the same file; a bare name match is still listed (likely).
    """
    parent, name = split_symbol(symbol)
    rows = conn.execute(
        "SELECT src_path, src_symbol, kind, dst_name, dst_path, line FROM edges "
        "WHERE kind = 'calls' AND dst_name = ? LIMIT ?",
        (name, limit * 20),
    ).fetchall()
    def_rows = conn.execute("SELECT path, parent FROM symbols WHERE name = ?", (name,)).fetchall()
    def_paths = {str(r[0]) for r in def_rows if not parent or str(r[1]) == parent}
    if parent and not def_paths:
        def_paths = {str(r[0]) for r in def_rows}
    importers: set[str] = set()
    for target in def_paths:
        importers.update(edge.src_path for edge in imported_by(conn, target, limit=2000))
    out: list[tuple[Edge, int]] = []
    for r in rows:
        edge = Edge(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4] or ""), int(r[5]))
        score = 0
        if edge.src_path in def_paths:
            score += 3
        if edge.src_path in importers:
            score += 3
        if any(PurePosixPath(edge.src_path).parent == PurePosixPath(path).parent for path in def_paths):
            score += 1
        if parent and edge.src_symbol.split(".")[0] == parent:
            score += 1
        out.append((edge, score))
    out.sort(key=lambda item: (-item[1], item[0].src_path, item[0].line))
    return out[:limit]


def callees(conn: sqlite3.Connection, symbol: str, *, path: str = "", limit: int = 60) -> list[tuple[Edge, list[str]]]:
    """Names called from inside ``symbol`` and where each is defined (several when ambiguous)."""
    parent, name = split_symbol(symbol)
    qualified = f"{parent}.{name}" if parent else name
    # the definitions first (indexed by name), then the calls inside their line ranges (indexed by file)
    def_sql = "SELECT path, line, end_line, parent FROM symbols WHERE name = ?"
    def_params: list[object] = [name]
    if path:
        def_sql += " AND path = ?"
        def_params.append(path)
    defs = [d for d in conn.execute(def_sql + " LIMIT 20", def_params).fetchall() if not parent or str(d[3]) == parent]
    rows: list = []
    for def_path, start, end, _parent in defs:
        rows += conn.execute(
            "SELECT src_path, src_symbol, kind, dst_name, dst_path, line FROM edges WHERE src_path = ? AND +kind = 'calls' "
            "AND line BETWEEN ? AND ? AND (src_symbol = ? OR src_symbol LIKE ?) ORDER BY line",
            (def_path, start, end, qualified, "%." + qualified),
        ).fetchall()
        if len(rows) >= limit:
            break
    rows = rows[:limit]
    out: list[tuple[Edge, list[str]]] = []
    imported: dict[str, set[str]] = {}
    external: dict[str, set[str]] = {}
    for r in rows:
        edge = Edge(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4] or ""), int(r[5]))
        if edge.src_path not in imported:
            found = imports_of(conn, edge.src_path)
            imported[edge.src_path] = {e.dst_path for e in found if e.dst_path}
            # names imported from outside the index (pathlib.Path): calls to them stay outside
            external[edge.src_path] = {re.split(r"[.:/]", e.dst_name)[-1] for e in found if not e.dst_path}
        near = imported[edge.src_path]
        defs = conn.execute(
            "SELECT path, line FROM symbols WHERE name = ? AND kind NOT IN ('class','interface','struct','enum','trait','object','impl','union') "
            "LIMIT 40",
            (edge.dst_name,),
        ).fetchall()
        if not defs:  # a constructor call: the class itself
            defs = conn.execute("SELECT path, line FROM symbols WHERE name = ? LIMIT 10", (edge.dst_name,)).fetchall()
        ranked = sorted(((str(d[0]), int(d[1])) for d in defs),
                        key=lambda d: (d[0] != edge.src_path, not _near(d[0], near), d[0], d[1]))
        close = [d for d in ranked if d[0] == edge.src_path or _near(d[0], near)]
        if not close and edge.dst_name in external[edge.src_path]:
            ranked = []
        chosen = close or ranked
        out.append((edge, [f"{path}:{line}" for path, line in chosen[:4]]))
    return out


def _near(path: str, imported: set[str]) -> bool:
    if path in imported:
        return True
    folder = str(PurePosixPath(path).parent) + "/"
    return folder in imported


def inherits(conn: sqlite3.Connection, name: str, *, limit: int = 50) -> tuple[list[Edge], list[Edge]]:
    """(bases of ``name``, classes that extend or implement ``name``)."""
    _parent, short = split_symbol(name)
    up = conn.execute(
        "SELECT src_path, src_symbol, kind, dst_name, dst_path, line FROM edges WHERE kind = 'inherits' "
        "AND (src_symbol = ? OR src_symbol LIKE ?) LIMIT ?",
        (short, "%." + short, limit),
    ).fetchall()
    down = conn.execute(
        "SELECT src_path, src_symbol, kind, dst_name, dst_path, line FROM edges WHERE kind = 'inherits' AND dst_name = ? "
        "ORDER BY src_path LIMIT ?",
        (short, limit),
    ).fetchall()
    def edge(r) -> Edge:
        return Edge(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4] or ""), int(r[5]))
    return [edge(r) for r in up], [edge(r) for r in down]


# --------------------------------------------------------------------------- repo map


def file_graph(conn: sqlite3.Connection, paths: list[str]) -> dict[int, dict[int, float]]:
    """Weighted file -> file edges from imports (resolved), calls and inheritance (by name)."""
    index = {path: number for number, path in enumerate(paths)}
    definers: dict[str, list[str]] = defaultdict(list)
    for name, path, kind in conn.execute("SELECT name, path, kind FROM symbols"):
        if str(path) in index:
            definers[str(name)].append(str(path))
    edges: dict[int, dict[int, float]] = defaultdict(dict)

    def add(src: str, dst: str, weight: float) -> None:
        a, b = index.get(src), index.get(dst)
        if a is None or b is None or a == b:
            return
        edges[a][b] = edges[a].get(b, 0.0) + weight

    for src, dst in conn.execute("SELECT src_path, dst_path FROM edges WHERE kind = 'imports' AND dst_path IS NOT NULL"):
        dst = str(dst)
        if dst.endswith("/"):
            for path in paths:
                if path.startswith(dst) and "/" not in path[len(dst):]:
                    add(str(src), path, 1.0)
        else:
            add(str(src), dst, 2.0)
    for src, name, kind in conn.execute("SELECT src_path, dst_name, kind FROM edges WHERE kind IN ('calls', 'inherits')"):
        targets = definers.get(str(name))
        if not targets or len(targets) > 12:
            continue  # very common names (get, run, init) say little
        weight = (1.5 if kind == "inherits" else 1.0) / math.sqrt(len(targets))
        for target in targets:
            add(str(src), target, weight)
    return edges


def pagerank(size: int, edges: dict[int, dict[int, float]], personal: list[float], *,
             damping: float = 0.85, rounds: int = 30) -> list[float]:
    rank = list(personal)
    out_weight = {src: sum(targets.values()) for src, targets in edges.items()}
    for _ in range(rounds):
        fresh = [(1.0 - damping) * personal[number] for number in range(size)]
        dangling = sum(rank[number] for number in range(size) if out_weight.get(number, 0.0) <= 0)
        for src, targets in edges.items():
            total = out_weight[src]
            if total <= 0:
                continue
            share = damping * rank[src] / total
            for dst, weight in targets.items():
                fresh[dst] += share * weight
        if dangling:
            spread = damping * dangling
            for number in range(size):
                fresh[number] += spread * personal[number]
        rank = fresh
    return rank
