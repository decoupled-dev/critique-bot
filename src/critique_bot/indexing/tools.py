# SPDX-License-Identifier: Apache-2.0
"""The index tools: what the model calls to navigate code by structure, history and evidence.

Every handler takes the tool arguments and the tool context and returns ``(ok, text)``.
Output is short by default (one line per item, a header with counts) and capped; when an
index is still being built the first line says the result is partial. The specs (names,
descriptions, JSON Schema) are in :mod:`critique_bot.tool_api`.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from critique_bot.indexing import engine, glossary, graph, history, langs, locate as _locate, modules, store

DEFAULT_LIMIT = 30
MAX_LIMIT = 200
#: Characters of output when the context does not say (the agent's cap still applies).
DEFAULT_CHARS = 6000

#: The tools the model sees. code_nav and code_history group related lookups under an action,
#: so a small model chooses among few tools.
READ_ONLY_TOOLS = ("code_nav", "code_history", "find_strings", "locate")
#: Tools that may run a command (coverage_locate runs the failing test).
COMMAND_TOOLS = ("coverage_locate",)
TOOLS = READ_ONLY_TOOLS + COMMAND_TOOLS
#: code_nav and code_history actions -> the handler that answers them.
NAV_ACTIONS = ("find_symbol", "refs", "who_calls", "callees", "imports_of", "imported_by", "module_of", "repo_map")
HISTORY_ACTIONS = ("blame", "log", "cochanged")
#: Names earlier versions advertised (and models still call): tool -> (new tool, action).
LEGACY_NAMES = {
    "find_symbol": ("code_nav", "find_symbol"),
    "symbol_refs": ("code_nav", "refs"),
    "who_calls": ("code_nav", "who_calls"),
    "callees": ("code_nav", "callees"),
    "imports_of": ("code_nav", "imports_of"),
    "imported_by": ("code_nav", "imported_by"),
    "module_of": ("code_nav", "module_of"),
    "repo_map": ("code_nav", "repo_map"),
    "git_blame": ("code_history", "blame"),
    "git_history": ("code_history", "log"),
    "cochanged": ("code_history", "cochanged"),
}


class ToolError(Exception):
    pass


# --------------------------------------------------------------------------- helpers


def _int(args: dict[str, Any], key: str, default: int, low: int = 1, high: int = MAX_LIMIT) -> int:
    value = args.get(key)
    if value is None or value == "":
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        raise ToolError(f"{key} must be a number")
    return max(low, min(high, number))


def _text(args: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return "\n".join(str(item) for item in value)
    return ""


def _rel(ctx: Any, raw: str) -> str:
    """A workspace-relative path from what the model wrote (strips ``./``, ``:line``)."""
    raw = raw.strip().strip("`\"'")
    raw = re.sub(r"(?::\d+(?:-\d+)?|#L\d+(?:-L?\d+)?)$", "", raw)
    workspace = Path(ctx.workspace)
    path = Path(raw)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(workspace.resolve()).as_posix()
        except (OSError, ValueError):
            raise ToolError(f"{raw} is outside the workspace")
    cleaned = PurePosixPath(raw.replace("\\", "/")).as_posix()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned in {".", ""}:
        return ""
    if ".." in PurePosixPath(cleaned).parts:
        resolved = (workspace / cleaned).resolve()
        try:
            return resolved.relative_to(workspace.resolve()).as_posix()
        except ValueError:
            raise ToolError(f"{raw} is outside the workspace")
    return cleaned


def _cap(text: str, ctx: Any) -> str:
    limit = min(int(getattr(ctx, "max_chars", DEFAULT_CHARS) or DEFAULT_CHARS), DEFAULT_CHARS * 2)
    if len(text) <= limit:
        return text
    cut = text[: limit - 80].rsplit("\n", 1)[0]
    return cut + "\n... (more; narrow with path, kind or limit)"


class _Dbs:
    """The databases for one call, opened lazily, with their partial notes."""

    def __init__(self, ctx: Any, rel: str = "") -> None:
        self.ctx = ctx
        self.targets = engine.targets(Path(ctx.workspace), getattr(ctx, "index_path", None), rel)
        self.notes = [t.partial for t in self.targets if t.partial]
        self._open: list[tuple[engine.Target, sqlite3.Connection]] = []

    def __enter__(self) -> "_Dbs":
        for target in self.targets:
            conn = target.connect()
            if conn is not None:
                self._open.append((target, conn))
        return self

    def __exit__(self, *exc: Any) -> None:
        for _target, conn in self._open:
            conn.close()

    @property
    def conns(self) -> list[sqlite3.Connection]:
        return [conn for _t, conn in self._open]

    def pairs(self) -> list[tuple[engine.Target, sqlite3.Connection]]:
        return list(self._open)

    def header(self) -> str:
        notes = list(self.notes)
        if not self._open:
            notes.append("no index yet; use search_code / find_files meanwhile")
        return "\n".join(notes)


def _out(dbs: _Dbs | None, lines: list[str], ctx: Any, empty: str) -> tuple[bool, str]:
    head = dbs.header() if dbs is not None else ""
    body = "\n".join(lines) if lines else empty
    return True, _cap((head + "\n" if head else "") + body, ctx)


def _need(args: dict[str, Any], *keys: str) -> str:
    value = _text(args, *keys)
    if not value:
        raise ToolError(f"pass {keys[0]}")
    return value


_CONTAINERS = "('class','interface','struct','enum','trait','object','impl','union','type')"


def _defs(conn: sqlite3.Connection, symbol: str, *, path: str = "", limit: int = 20) -> list[sqlite3.Row]:
    parent, name = graph.split_symbol(symbol)
    sql = "SELECT path, line, end_line, kind, name, parent, signature FROM symbols WHERE name = ?"
    params: list[object] = [name]
    if parent:
        sql += " AND parent = ?"
        params.append(parent)
    if path:
        sql += " AND (path = ? OR path LIKE ?)"
        params += [path, path.rstrip("/") + "/%"]
    sql += " ORDER BY path, line LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows and parent:
        return _defs(conn, name, path=path, limit=limit)
    return rows


def _qual(name: str, parent: str) -> str:
    return f"{parent}.{name}" if parent else name


# --------------------------------------------------------------------------- symbols


def find_symbol(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    query = _need(args, "name", "symbol", "query", "pattern")
    kind = _text(args, "kind").lower()
    scope = _rel(ctx, _text(args, "path", "module")) if _text(args, "path", "module") else ""
    limit = _int(args, "limit", DEFAULT_LIMIT)
    with _Dbs(ctx, scope) as dbs:
        rows: list[tuple] = []
        for conn in dbs.conns:
            prefix = _module_dir(conn, scope) if scope else ""
            parent, name = graph.split_symbol(query)
            exact = _defs(conn, query, path=prefix, limit=limit)
            seen = {(r["path"], r["line"]) for r in exact}
            rows += [tuple(r) for r in exact]
            if len(rows) < limit:
                like = "%" + name.lower().replace("_", "\\_").replace("%", "") + "%"
                sql = ("SELECT path, line, end_line, kind, name, parent, signature FROM symbols "
                       "WHERE lower(name) LIKE ? ESCAPE '\\'")
                params: list[object] = [like]
                if prefix:
                    sql += " AND (path = ? OR path LIKE ?)"
                    params += [prefix, prefix + "/%"]
                for r in conn.execute(sql + " ORDER BY length(name), path LIMIT ?", (*params, limit * 3)):
                    if (r["path"], r["line"]) not in seen:
                        rows.append(tuple(r))
                        seen.add((r["path"], r["line"]))
        if kind:
            rows = [r for r in rows if r[3] == kind or (kind == "function" and r[3] in {"method", "constructor"})
                    or (kind == "class" and r[3] in {"interface", "struct", "enum", "trait", "object", "record"})]
        rows = rows[:limit]
        lines = [f"{r[0]}:{r[1]}-{r[2]} {r[3]} {_qual(r[4], r[5])}  {str(r[6])[:110]}".rstrip() for r in rows]
        if rows:
            lines.insert(0, f"{len(rows)} definition(s) for {query}" + (f" (kind {kind})" if kind else ""))
        return _out(dbs, lines, ctx, f"no definition named like {query}; try locate or search_code")


def _module_dir(conn: sqlite3.Connection, scope: str) -> str:
    """A path prefix for ``scope``: a folder as given, or the folder of a build module by name."""
    if conn.execute("SELECT 1 FROM files WHERE path = ? OR path LIKE ? LIMIT 1", (scope, scope + "/%")).fetchone():
        return scope
    row = conn.execute("SELECT dir FROM modules WHERE name = ? LIMIT 1", (scope,)).fetchone()
    return str(row[0]) if row is not None else scope


def _symbol_at(conn: sqlite3.Connection, path: str, line: int) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT name, parent FROM symbols WHERE path = ? AND line <= ? AND end_line >= ? ORDER BY line DESC LIMIT 1",
        (path, line, line),
    ).fetchone()
    return (str(row[0]), str(row[1])) if row is not None else None


def symbol_refs(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    raw = _need(args, "name", "symbol", "target")
    limit = _int(args, "limit", 40)
    scope = _rel(ctx, _text(args, "path")) if _text(args, "path") else ""
    located = re.match(r"^(.+?\.\w+):(\d+)$", raw)
    with _Dbs(ctx, scope or (located.group(1) if located else "")) as dbs:
        name = raw
        if located:
            rel = _rel(ctx, located.group(1))
            for conn in dbs.conns:
                found = _symbol_at(conn, rel, int(located.group(2)))
                if found:
                    name = _qual(*found)
                    break
            else:
                return False, f"no definition at {raw}; pass a name instead"
        _parent, short = graph.split_symbol(name)
        files: dict[str, int] = defaultdict(int)
        for conn in dbs.conns:
            sql = "SELECT path, count FROM refs WHERE name = ?"
            params: list[object] = [short]
            if scope:
                sql += " AND (path = ? OR path LIKE ?)"
                params += [scope, scope + "/%"]
            for path, count in conn.execute(sql + " ORDER BY count DESC LIMIT 400", params):
                files[str(path)] += int(count)
            # scoped (AOSP) databases keep no refs table: the call edges and the definitions stand in
            for (path,) in conn.execute("SELECT DISTINCT src_path FROM edges WHERE dst_name = ? AND kind IN ('calls','inherits') LIMIT 400", (short,)):
                files.setdefault(str(path), 0)
            for (path,) in conn.execute("SELECT DISTINCT path FROM symbols WHERE name = ? LIMIT 50", (short,)):
                files.setdefault(str(path), 0)
        if scope:
            files = {p: c for p, c in files.items() if p == scope or p.startswith(scope.rstrip("/") + "/")}
        ordered = sorted(files, key=lambda p: (-files[p], p))
        pattern = re.compile(r"(?<![\w$])" + re.escape(short) + r"(?![\w$])")
        lines = []
        total = 0
        deadline = time.monotonic() + 3.0
        for path in ordered:
            if len(lines) >= limit or time.monotonic() > deadline:
                break
            hits = _scan(Path(ctx.workspace) / path, pattern)
            if not hits:
                continue
            total += len(hits)
            shown = ", ".join(str(n) for n in hits[:12]) + (f", ... ({len(hits)})" if len(hits) > 12 else "")
            lines.append(f"{path}: {shown}")
        if lines:
            lines.insert(0, f"{short}: {total} use(s) in {len(lines)} file(s) (lines; definitions included)")
        if len(ordered) > len(lines) and lines:
            lines.append(f"... {len(ordered) - len(lines)} more file(s); pass path or a higher limit")
        return _out(dbs, lines, ctx, f"no references to {short} in the index")


def _scan(path: Path, pattern: re.Pattern[str]) -> list[int]:
    try:
        if path.stat().st_size > 2_000_000:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [number for number, line in enumerate(text.split("\n"), start=1) if pattern.search(line)]


def who_calls(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    symbol = _need(args, "symbol", "name")
    depth = _int(args, "depth", 1, 1, 2)
    limit = _int(args, "limit", 30)
    with _Dbs(ctx, _rel(ctx, _text(args, "path")) if _text(args, "path") else "") as dbs:
        lines: list[str] = []
        for conn in dbs.conns:
            for edge, score in graph.callers(conn, symbol, limit=limit):
                tag = "" if score >= 3 else " (likely: name match)"
                lines.append(f"{edge.src_path}:{edge.line} {edge.src_symbol or '<top level>'}{tag}")
                if depth > 1 and edge.src_symbol:
                    for inner, _s in graph.callers(conn, edge.src_symbol, limit=5):
                        lines.append(f"    <- {inner.src_path}:{inner.line} {inner.src_symbol or '<top level>'}")
        if lines:
            lines.insert(0, f"callers of {symbol} (call edges match by name; check before relying on them):")
        return _out(dbs, lines[: limit * 3], ctx, f"no calls to {symbol} found; symbol_refs lists every use")


def callees(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    symbol = _need(args, "symbol", "name")
    path = _rel(ctx, _text(args, "path")) if _text(args, "path") else ""
    depth = _int(args, "depth", 1, 1, 2)
    limit = _int(args, "limit", 40)
    with _Dbs(ctx, path) as dbs:
        lines: list[str] = []
        outside: list[str] = []
        for conn in dbs.conns:
            for edge, defs in graph.callees(conn, symbol, path=path, limit=limit):
                if not defs:
                    if edge.dst_name not in outside:
                        outside.append(edge.dst_name)
                    continue
                where = ", ".join(defs[:3]) + (" ..." if len(defs) > 3 else "")
                mark = " (ambiguous)" if len(defs) > 1 else ""
                lines.append(f"{edge.src_path}:{edge.line} {edge.dst_name} -> {where}{mark}")
                if depth > 1 and len(defs) == 1:
                    for inner, inner_defs in graph.callees(conn, edge.dst_name, limit=6):
                        if inner_defs:
                            lines.append(f"    -> {inner.dst_name} ({', '.join(inner_defs[:2])})")
        if outside:
            lines.append("also calls (not defined in the index): " + ", ".join(outside[:30]))
        if lines:
            lines.insert(0, f"calls made by {symbol} (likely; matched by name):")
        return _out(dbs, lines, ctx, f"no calls recorded inside {symbol}; check the name with find_symbol")


# --------------------------------------------------------------------------- imports and modules


def imports_of(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    rel = _rel(ctx, _need(args, "path"))
    with _Dbs(ctx, rel) as dbs:
        lines = []
        outside: list[str] = []
        for conn in dbs.conns:
            for edge in graph.imports_of(conn, rel):
                if edge.dst_path:
                    lines.append(f"{edge.line}: {edge.dst_name} -> {edge.dst_path}")
                elif edge.dst_name not in outside:
                    outside.append(edge.dst_name)
        if outside:
            lines.append("outside the index: " + ", ".join(outside[:40]))
        if lines:
            lines.insert(0, f"{rel} imports {len(lines)}:")
        return _out(dbs, lines, ctx, f"{rel}: no imports recorded (is the path right?)")


def imported_by(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    rel = _rel(ctx, _need(args, "path"))
    limit = _int(args, "limit", 60)
    with _Dbs(ctx, rel) as dbs:
        lines = []
        for conn in dbs.conns:
            for edge in graph.imported_by(conn, rel, limit=limit):
                lines.append(f"{edge.src_path}:{edge.line} ({edge.dst_name})")
        if lines:
            lines.insert(0, f"{len(lines)} file(s) import {rel}:")
        return _out(dbs, lines, ctx, f"nothing in the index imports {rel}")


def module_of(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    rel = _rel(ctx, _need(args, "path"))
    with _Dbs(ctx, rel) as dbs:
        lines: list[str] = []
        for conn in dbs.conns:
            found, build_file, via = modules.owners(Path(ctx.workspace), conn, rel)
            if not build_file:
                continue
            lines.append(f"{rel}: build file {build_file}" + (f" (via {', '.join(via)})" if via else ""))
            for module in found[:6]:
                lines.append(f"module {module.name} ({module.system} {module.type})")
                if module.deps:
                    lines.append("  deps: " + ", ".join(module.deps[:25]) + (" ..." if len(module.deps) > 25 else ""))
                users = modules.dependents(conn, module.name, limit=12)
                if users:
                    lines.append("  used by: " + ", ".join(f"{m.name}" for m in users))
            break
        return _out(dbs, lines, ctx, f"no build file (Android.bp, BUILD, build.gradle, CMakeLists.txt) above {rel}")


# --------------------------------------------------------------------------- repo map


def build_repo_map(conn: sqlite3.Connection, *, scope: str = "", budget_tokens: int = 1000,
                   focus: list[str] | None = None, keywords: list[str] | None = None) -> tuple[str, int]:
    """A ranked map of ``scope``: files by personalized PageRank over the L3 graph, each with
    its main definitions, cut at ``budget_tokens`` (4 characters a token). Deterministic for the
    same index and inputs, so it is stable within a session."""
    sql = "SELECT path, language FROM files"
    params: list[object] = []
    if scope:
        sql += " WHERE path = ? OR path LIKE ?"
        params = [scope, scope.rstrip("/") + "/%"]
    paths = sorted(str(r[0]) for r in conn.execute(sql, params) if str(r[1]) in langs.CODE)
    if not paths:
        return "", 0
    if len(paths) > 15_000:
        paths = paths[:15_000]
    size = len(paths)
    index = {p: n for n, p in enumerate(paths)}
    edges = graph.file_graph(conn, paths)
    personal = [0.0] * size
    for rel in focus or []:
        if rel in index:
            personal[index[rel]] += 10.0
    words = [w.lower() for w in (keywords or []) if len(w) >= 3]
    if words:
        for rel in paths:
            name = rel.lower()
            personal[index[rel]] += sum(1.0 for w in words if w in name)
    total = sum(personal)
    personal = [1.0 / size] * size if total <= 0 else [(v + 0.02) / (total + 0.02 * size) for v in personal]
    rank = graph.pagerank(size, edges, personal)
    order = sorted(range(size), key=lambda n: (-round(rank[n], 12), paths[n]))
    budget = max(200, budget_tokens * 4)
    out: list[str] = []
    used = 0
    shown = 0
    for number in order:
        rel = paths[number]
        rows = conn.execute(
            "SELECT line, kind, name, parent, signature FROM symbols WHERE path = ? ORDER BY "
            f"(kind IN {_CONTAINERS}) DESC, (substr(name, 1, 1) = '_') ASC, line LIMIT 9",  # nosec B608: a constant
            (rel,),
        ).fetchall()
        block = [rel + ":"]
        for line, kind, name, parent, signature in rows[:8]:
            label = (str(signature).strip() or _qual(str(name), str(parent)))[:90]
            block.append(f"  {line} {label}")
        text = "\n".join(block)
        if used + len(text) + 1 > budget:
            if used + len(rel) + 2 <= budget:
                out.append(rel)
                used += len(rel) + 1
                shown += 1
            continue
        out.append(text)
        used += len(text) + 1
        shown += 1
    return "\n".join(out), shown


def repo_map(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    raw_scope = _text(args, "scope", "path", "module")
    budget = _int(args, "budget_tokens", 1000, 100, 8000)
    scope = _rel(ctx, raw_scope) if raw_scope else ""
    state = getattr(ctx, "state", None)
    focus = [_rel(ctx, f) for f in (args.get("focus") or []) if isinstance(f, str)]
    if state is not None:
        focus += list(getattr(state, "reads", {}) or {})
    keywords = re.findall(r"[A-Za-z]{3,}", _text(args, "keywords") or (getattr(state, "task", "") or ""))[:20]
    with _Dbs(ctx, scope) as dbs:
        for conn in dbs.conns:
            prefix = _module_dir(conn, scope) if scope else ""
            text, shown = build_repo_map(conn, scope=prefix, budget_tokens=budget, focus=focus,
                                         keywords=[w for k in keywords for w in glossary.split(k)])
            if text:
                header = f"map of {prefix or 'the project'}: {shown} files by rank (budget {budget} tokens)"
                return _out(dbs, [header, text], ctx, "")
        return _out(dbs, [], ctx, f"no indexed code under {scope or 'the project'}")


# --------------------------------------------------------------------------- history


def _history_target(ctx: Any, dbs: _Dbs) -> tuple[engine.Target, sqlite3.Connection] | None:
    pairs = dbs.pairs()
    return pairs[0] if pairs else None


def _git_place(ctx: Any, rel: str) -> tuple[Path, str] | None:
    found = history.project_for(Path(ctx.workspace), rel)
    if found is None:
        return None
    folder, prefix = found
    inner = rel[len(prefix) + 1 :] if prefix and rel.startswith(prefix + "/") else rel
    return folder, inner


def _lines_arg(args: dict[str, Any]) -> tuple[int, int]:
    raw = args.get("lines")
    start = args.get("start_line") or args.get("start")
    end = args.get("end_line") or args.get("end")
    if isinstance(raw, str) and re.fullmatch(r"\s*\d+\s*(?:[-,:]\s*\d+\s*)?", raw):
        numbers = [int(n) for n in re.findall(r"\d+", raw)]
        start, end = numbers[0], numbers[-1]
    elif isinstance(raw, (int, float)):
        start = end = int(raw)
    elif isinstance(raw, list) and raw:
        start, end = int(raw[0]), int(raw[-1])
    start = int(start or 0)
    end = int(end or start)
    return start, end


def git_blame(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    rel = _rel(ctx, _need(args, "path"))
    start, end = _lines_arg(args)
    if not start:
        return False, "pass lines, e.g. \"120-160\""
    if end - start > 400:
        end = start + 400
    place = _git_place(ctx, rel)
    if place is None:
        return False, "not a git checkout"
    folder, inner = place
    with _Dbs(ctx, rel) as dbs:
        conn = dbs.conns[0] if dbs.conns else None
        digest = ""
        if conn is not None:
            row = conn.execute("SELECT hash FROM files WHERE path = ?", (rel,)).fetchone()
            digest = str(row[0]) if row else ""
        runs = history.blame(conn, folder, inner, start, end, file_hash=digest)
        lines = []
        for run in runs:
            day = time.strftime("%Y-%m-%d", time.gmtime(run.ts)) if run.ts else "?"
            span = f"{run.start}" if run.start == run.end else f"{run.start}-{run.end}"
            lines.append(f"{span}: {run.sha[:10]} {day} {run.author[:20]}: {run.summary[:90]}")
        return _out(None, lines, ctx, f"git blame found nothing for {rel}:{start}-{end} (uncommitted or outside git)")


def git_history(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    raw_path = _text(args, "path")
    symbol = _text(args, "symbol")
    query = _text(args, "query", "message")
    regex = _text(args, "changed", "regex", "G")
    limit = _int(args, "limit", 10, 1, 50)
    if not (raw_path or symbol or query or regex):
        return False, "pass path, symbol, query (commit message words) or changed (a regex for git log -G)"
    rel = _rel(ctx, raw_path) if raw_path else ""
    with _Dbs(ctx, rel) as dbs:
        lines: list[str] = []
        notes: list[str] = []
        pair = _history_target(ctx, dbs)
        if pair is not None:
            note = engine.ensure_history(Path(ctx.workspace), pair[0])
            if note:
                notes.append(note)
        if symbol:
            for conn in dbs.conns:
                rows = _defs(conn, symbol, path=rel, limit=3)
                for row in rows[:2]:
                    place = _git_place(ctx, str(row["path"]))
                    if place is None:
                        continue
                    found = history.file_log(place[0], place[1], start=int(row["line"]), end=int(row["end_line"]), limit=limit)
                    lines.append(f"{_qual(row['name'], row['parent'])} ({row['path']}:{row['line']}-{row['end_line']}):")
                    lines += ["  " + item for item in found] or ["  no commits (uncommitted?)"]
                if rows:
                    break
            if not lines:
                lines.append(f"no definition of {symbol}; check it with find_symbol")
        elif rel and not query and not regex:
            conn = pair[1] if pair else None
            commits = history.recent_commits(conn, rel, limit=limit) if conn is not None else []
            if commits:
                lines.append(f"{rel}: {history.commit_count(conn, rel)} commit(s) in the index; newest:")
                lines += ["  " + c.line() for c in commits]
            else:
                place = _git_place(ctx, rel)
                found = history.file_log(place[0], place[1], limit=limit) if place else []
                lines += found or [f"no commits touch {rel}"]
        if query:
            for conn in dbs.conns:
                for commit, files in history.search_commits(conn, query, limit=limit):
                    lines.append(commit.line())
                    lines.append("    " + ", ".join(files[:6]) + (" ..." if len(files) > 6 else ""))
            if not any(not line.startswith("    ") for line in lines):
                lines.append(f"no commit message mentions {query!r}")
        if regex:
            place = _git_place(ctx, rel or (pair[0].group if pair and pair[0].group else ""))
            if place is None and not rel:
                place = (Path(ctx.workspace), "")
            found = history.log_g(place[0], regex, path=place[1] if rel else "", limit=limit) if place else []
            lines.append(f"commits that added or removed lines matching /{regex}/:")
            lines += ["  " + item for item in found] or ["  none"]
        return True, _cap("\n".join(notes + lines), ctx)


def cochanged(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    rel = _rel(ctx, _need(args, "path"))
    limit = _int(args, "limit", 15, 1, 50)
    with _Dbs(ctx, rel) as dbs:
        pair = _history_target(ctx, dbs)
        if pair is None:
            return _out(dbs, [], ctx, "no index yet")
        note = engine.ensure_history(Path(ctx.workspace), pair[0])
        conn = pair[1]
        total = history.commit_count(conn, rel)
        rows = history.cochanged(conn, rel, limit=limit)
        lines = [note] if note else []
        if rows:
            lines.append(f"{rel}: {total} commit(s); files changed in the same commits:")
            for other, count, score, last_ts in rows:
                day = time.strftime("%Y-%m-%d", time.gmtime(last_ts)) if last_ts else "?"
                lines.append(f"  {other}  {count}x ({int(round(score * 100))}%, last {day})")
        return _out(dbs, lines, ctx, f"no commit in the index changes {rel} together with other files")


# --------------------------------------------------------------------------- strings and locate


def find_strings(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    text = _need(args, "text", "query", "message", "string")
    kind = _text(args, "kind").lower()
    rel = _rel(ctx, _text(args, "path")) if _text(args, "path") else ""
    limit = _int(args, "limit", 15, 1, 100)
    candidates = [text]
    tag = ""
    parsed = _locate.log_line(text)
    if parsed:  # a whole logcat line: search its message and its tag
        tag, message = parsed
        candidates = [message]
    with _Dbs(ctx, rel) as dbs:
        hits = []
        for conn in dbs.conns:
            for needle in candidates:
                hits += _locate.search_strings(conn, needle, kind=kind, path=rel, limit=limit)
            if tag:
                hits += _locate.search_strings(conn, tag, kind="tag", path=rel, limit=3)
        hits.sort(key=lambda h: (-h.score, h.path, h.line))
        lines = []
        seen: set[tuple[str, int]] = set()
        for hit in hits:
            if (hit.path, hit.line) in seen:
                continue
            seen.add((hit.path, hit.line))
            if _assigned_secret(ctx, hit):
                continue
            extra = f" tag={hit.tag}" if hit.tag and hit.tag != hit.value else ""
            where = f" in {hit.container}" if hit.container else ""
            lines.append(f"{hit.path}:{hit.line} {hit.kind}{extra}{where}: \"{hit.value[:100]}\" ({int(hit.score * 100)}%)")
            if len(lines) >= limit:
                break
        # The query is not echoed: it may be the secret a hit was withheld for.
        return _out(dbs, lines, ctx, "no string, log message, tag or key like that in the index")


def _assigned_secret(ctx: Any, hit: Any) -> bool:
    """The literal is assigned to a secret-looking name on its line now (``DB_PASSWORD = "..."``).

    Checked against the file as it is, for an index built before such literals were left out.
    """
    from critique_bot.indexing.facts import secret_literal

    try:
        path = (Path(ctx.workspace) / hit.path).resolve()
        with open(path, encoding="utf-8", errors="replace") as handle:
            for number, row in enumerate(handle, start=1):
                if number == hit.line:
                    return secret_literal(hit.value, row)
    except (OSError, ValueError):
        return False
    return False


def locate(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    description = _need(args, "description", "query", "task")
    evidence = _text(args, "evidence", "log", "trace")
    limit = _int(args, "limit", 10, 1, 30)
    budget = float(args.get("budget") or _locate.DEFAULT_BUDGET)
    rel = _rel(ctx, _text(args, "path")) if _text(args, "path") else ""
    with _Dbs(ctx, rel) as dbs:
        dbs_paths = [target.path for target, _conn in dbs.pairs()]
        if not dbs_paths:
            return _out(dbs, [], ctx, "")
        pair = dbs.pairs()[0]
        engine.ensure_history(Path(ctx.workspace), pair[0], wait=0.0)  # commits help next time
        found = _locate.locate(dbs_paths, description, evidence, budget=min(max(budget, 0.2), 10.0), limit=limit,
                               workspace=str(Path(ctx.workspace).resolve()))
        lines = []
        for number, candidate in enumerate(found.candidates, start=1):
            where = f"{candidate.path}:{candidate.line}" if candidate.line else candidate.path
            symbol = f" {candidate.symbol}" if candidate.symbol else ""
            lines.append(f"{number}. {where}{symbol}  [{', '.join(candidate.retrievers)}]")
            others = candidate.top_symbols(3)[1:]
            if others:
                lines.append(f"   also: {', '.join(others)}")
            lines.append(f"   why: {_locate.hypothesis(candidate)}")
        terms = [t for t, w, why in found.query.terms if w < 1.0][:8]
        footer = f"({found.seconds}s; retrievers: {', '.join(found.ran)}"
        if found.late:
            footer += f"; over the time budget: {', '.join(found.late)}"
        footer += ")"
        from critique_bot.indexing import symbols as _symbols

        if not _symbols.treesitter_available("java"):
            footer += "\nparser: regex (tree-sitter is not installed: pip install 'critique-bot[index]' for exact spans and calls)"
        if terms:
            footer += f"\nexpanded terms: {', '.join(terms)}"
        if lines:
            lines.insert(0, "candidates (a hypothesis: read them before relying on it):")
            lines.append(footer)
        return _out(dbs, lines, ctx, "no candidates; try search_code with words from the description\n" + footer)


def coverage_locate(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    from critique_bot.indexing import coverage

    command = _text(args, "command", "test")
    report = _text(args, "report")
    passing = args.get("passing") or []
    passing_reports = args.get("passing_reports") or []
    trace = _text(args, "trace", "stack")
    timeout = float(_int(args, "timeout", 300, 5, 1800))
    if isinstance(passing, str):
        passing = [passing]
    if isinstance(passing_reports, str):
        passing_reports = [passing_reports]
    if not (command or report or trace):
        return False, "pass command (a failing test), report (a coverage report of a failing run) or trace (a stack trace)"
    workspace = Path(ctx.workspace)
    lines: list[str] = []
    with _Dbs(ctx) as dbs:
        if not dbs.conns:
            return _out(dbs, [], ctx, "")
        conn = dbs.conns[0]
        known = {str(r[0]) for r in conn.execute("SELECT path FROM files")}
        if trace:
            frames = _locate.r_frames(_locate.Query("", trace, frames=_locate.parse_frames(trace)), conn)
            if frames:
                lines.append("stack frames in this code:")
                lines += [f"  {h.path}:{h.line} {h.symbol}  ({h.reason})" for h in frames[:12]]
            else:
                lines.append("no frame of the trace resolves to an indexed file")
        failing: list = []
        passed: list = []
        started = time.monotonic()
        if report:
            failing.append(coverage.read_report(workspace / _rel(ctx, report), workspace, known))
        for item in passing_reports:
            passed.append(coverage.read_report(workspace / _rel(ctx, str(item)), workspace, known))
        if command:
            run = coverage.run_python(command, workspace, timeout=timeout)
            if run.error:
                return False, f"coverage run failed: {run.error}" + (f"\n{run.output}" if run.output else "")
            if run.exit == 0:
                lines.append(f"note: {command!r} passed (exit 0); ranking what it executed anyway")
            failing.append(run.lines)
            for item in passing[:5]:
                if time.monotonic() - started > timeout:
                    lines.append("note: time budget used up; not all passing runs were traced")
                    break
                other = coverage.run_python(str(item), workspace, timeout=timeout)
                if other.error:
                    lines.append(f"note: passing run {item!r} failed to trace: {other.error}")
                    continue
                passed.append(other.lines)
        if failing:
            scores = coverage.ochiai(failing, passed)
            ranked = coverage.rank_functions(conn, scores, limit=_int(args, "limit", 15, 1, 50))
            kind = "Ochiai with passing runs" if passed else "executed by the failing run (no passing runs: smaller first)"
            lines.append(f"functions by suspiciousness ({kind}):")
            for item in ranked:
                lines.append(f"  {item.score:.2f} {item.path}:{item.line}-{item.end_line} {item.symbol or '<module>'}"
                             f" ({item.executed} line(s) run)")
            if not ranked:
                lines.append("  nothing in the index was executed")
        return _out(dbs, lines, ctx, "nothing to show")


def _action(args: dict[str, Any], allowed: tuple[str, ...], tool: str) -> str:
    action = str(args.get("action") or "").strip().lower().replace("-", "_")
    aliases = {"symbol_refs": "refs", "references": "refs", "find": "find_symbol", "definition": "find_symbol",
               "git_blame": "blame", "history": "log", "git_history": "log", "commits": "log", "map": "repo_map"}
    action = aliases.get(action, action)
    if action not in allowed:
        raise ToolError(f"{tool} needs action, one of: {', '.join(allowed)}")
    return action


_NAV = {
    "find_symbol": lambda args, ctx: find_symbol(args, ctx),
    "refs": lambda args, ctx: symbol_refs(args, ctx),
    "who_calls": lambda args, ctx: who_calls(args, ctx),
    "callees": lambda args, ctx: callees(args, ctx),
    "imports_of": lambda args, ctx: imports_of(args, ctx),
    "imported_by": lambda args, ctx: imported_by(args, ctx),
    "module_of": lambda args, ctx: module_of(args, ctx),
    "repo_map": lambda args, ctx: repo_map(args, ctx),
}
_HISTORY = {
    "blame": lambda args, ctx: git_blame(args, ctx),
    "log": lambda args, ctx: git_history(args, ctx),
    "cochanged": lambda args, ctx: cochanged(args, ctx),
}


def code_nav(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    action = _action(args, NAV_ACTIONS, "code_nav")
    if action in {"imports_of", "imported_by", "module_of"} and not _text(args, "path") and _text(args, "name"):
        args = {**args, "path": args["name"]}  # a file passed as name
    if action == "repo_map" and not _text(args, "scope") and _text(args, "path"):
        args = {**args, "scope": args["path"], "path": ""}
    return _NAV[action](args, ctx)


def code_history(args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    action = _action(args, HISTORY_ACTIONS, "code_history")
    if action == "log" and _text(args, "name") and not _text(args, "symbol"):
        args = {**args, "symbol": args["name"]}
    return _HISTORY[action](args, ctx)


HANDLERS: dict[str, Callable[[dict[str, Any], Any], tuple[bool, str]]] = {
    "code_nav": code_nav,
    "code_history": code_history,
    "find_strings": find_strings,
    "coverage_locate": coverage_locate,
    "locate": locate,
}


def run(name: str, args: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    """Run one index tool. Errors in the arguments come back as ``(False, message)``."""
    if name in LEGACY_NAMES:
        name, action = LEGACY_NAMES[name]
        args = {**args, "action": args.get("action") or action}
    handler = HANDLERS.get(name)
    if handler is None:
        return False, f"unknown index tool {name}"
    try:
        return handler(args, ctx)
    except ToolError as exc:
        return False, str(exc)
    except sqlite3.DatabaseError as exc:
        return False, f"index error: {exc}; it is rebuilt on the next refresh"


def paths_of(name: str, args: dict[str, Any]) -> list[str]:
    """Raw path arguments a call reads (for the outside-the-workspace check)."""
    out = []
    for key in ("path", "scope", "report"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value)
    for key in ("passing_reports",):
        value = args.get(key)
        if isinstance(value, list):
            out += [str(item) for item in value]
    named = args.get("name")
    if name == "code_nav" and isinstance(named, str) and named.strip():
        action = str(args.get("action") or "")
        located = re.match(r"^(.+?\.\w+):\d+$", named.strip())
        if located:  # refs of path:line
            out.append(located.group(1))
        elif action in {"imports_of", "imported_by", "module_of"}:
            out.append(named)
    return out
