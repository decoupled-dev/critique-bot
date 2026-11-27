"""``locate``: find the code a description is about by fusing many signals.

Each retriever takes the parsed :class:`Query` and one database and returns ranked
:class:`Hit` s (file, line, symbol, reason). The retrievers run in parallel threads under one
time budget; whatever finishes in time is merged with weighted reciprocal rank fusion. Exact
string and stack-frame matches weigh most, names least (plan 8, "Finding code with bad names").

Later stages add retrievers to :data:`RETRIEVERS` (Stage 8b: file and module cards).
"""

from __future__ import annotations

import difflib
import math
import re
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable

from critique_bot.indexing import glossary, graph, history, store

DEFAULT_BUDGET = 1.5
RRF_K = 10.0
PER_RETRIEVER = 20


@dataclass
class Hit:
    path: str
    line: int = 0
    symbol: str = ""
    reason: str = ""


@dataclass
class Frame:
    raw: str
    file: str  # a file name or path as written in the trace
    line: int
    symbol: str  # Class.method / function, "" when unknown
    container: str = ""  # Java package + class, native library


@dataclass
class Query:
    text: str
    evidence: str = ""
    terms: list[tuple[str, float, str]] = field(default_factory=list)
    quoted: list[str] = field(default_factory=list)
    messages: list[tuple[str, str]] = field(default_factory=list)  # (tag, message) from log lines
    frames: list[Frame] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- parsing the question

_JAVA_FRAME = re.compile(r"\bat\s+((?:[\w$]+\.)*[\w$]+)\.([\w$<>-]+)\(([\w$.-]+\.(?:java|kt|kts|scala)):(\d+)\)")
_PY_FRAME = re.compile(r"File \"([^\"]+)\", line (\d+), in ([\w<>]+)")
_NATIVE_FRAME = re.compile(r"#\d+\s+pc\s+[0-9a-fA-F]+\s+(\S+)(?:\s+\(([^)+]+)(?:\+\d+)?\))?")
_FILE_LINE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:go|rs|c|cc|cpp|cxx|h|hpp|ts|tsx|js|jsx|py|kt|java|swift)):(\d+)")
_LOGCAT = re.compile(
    r"^(?:\d\d-\d\d\s+)?\d\d:\d\d:\d\d\.\d+\s+(?:\d+\s+\d+\s+)?([VDIWEF])\s+([^:]+?)\s*:\s(.*)$"
    r"|^([VDIWEF])/([^(:]+?)\s*\(\s*\d+\):\s(.*)$"
    r"|^([VDIWEF])\s+([\w.\-/]+(?: [\w.\-/]+)?)\s*:\s(.*)$"
)


def log_line(text: str) -> tuple[str, str] | None:
    """(tag, message) of a logcat line in threadtime, brief or tag format."""
    match = _LOGCAT.match(text.strip())
    if not match:
        return None
    groups = match.groups()
    for start in (0, 3, 6):
        if groups[start]:
            return groups[start + 1].strip(), groups[start + 2].strip()
    return None


_QUOTED = re.compile(r"\"([^\"\n]{4,200})\"|'([^'\n]{6,200})'|`([^`\n]{4,200})`|“([^”\n]{4,200})”")
_IDENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:[A-Z_][A-Za-z0-9_]*)+|[a-z]+_[a-z0-9_]+)\b")
_PATHISH = re.compile(r"(?<![\w])((?:[\w.-]+/)+[\w.-]+|[\w-]+\.(?:py|java|kt|go|rs|c|cc|cpp|h|hpp|ts|tsx|js|jsx|xml|rc|te|bp|gradle|aidl|hal))\b")
_TEST_NAME = re.compile(r"\b(test_?\w+|\w+Test(?:s|Case)?(?:\.\w+)?|\w+_test)\b")
_FRAMEWORK = ("java.", "javax.", "kotlin.", "kotlinx.", "android.", "androidx.", "dalvik.", "com.android.internal.",
              "sun.", "jdk.", "org.junit.", "junit.")


def demangle(name: str) -> str:
    """A rough Itanium demangle for frames: ``_ZN7android6Parcel8writeIntEi`` -> ``android::Parcel::writeInt``."""
    if not name.startswith("_Z"):
        return name
    body = name[3:] if name.startswith("_ZN") else name[2:]
    parts = []
    index = 0
    while index < len(body):
        match = re.match(r"(\d+)", body[index:])
        if not match:
            break
        size = int(match.group(1))
        index += len(match.group(1))
        parts.append(body[index : index + size])
        index += size
        if not name.startswith("_ZN"):
            break
    return "::".join(parts) or name


def parse_frames(text: str) -> list[Frame]:
    frames: list[Frame] = []
    for match in _JAVA_FRAME.finditer(text):
        qualified, method, file, line = match.groups()
        cls = qualified.rpartition(".")[2].split("$")[0]
        frames.append(Frame(match.group(0), file, int(line), f"{cls}.{method}" if method not in {"<init>", "<clinit>"} else cls,
                            qualified))
    for match in _PY_FRAME.finditer(text):
        frames.append(Frame(match.group(0), match.group(1), int(match.group(2)), match.group(3)))
    for match in _NATIVE_FRAME.finditer(text):
        library, function = match.group(1), match.group(2) or ""
        symbol = demangle(function).replace("::", ".") if function else ""
        frames.append(Frame(match.group(0), "", 0, symbol, library))
    seen = {(frame.file.rpartition("/")[2], frame.line) for frame in frames if frame.file}
    for match in _FILE_LINE.finditer(text):
        file, line = match.group(1), int(match.group(2))
        if (file.rpartition("/")[2], line) in seen:
            continue
        seen.add((file.rpartition("/")[2], line))
        frames.append(Frame(match.group(0), file, line, ""))
    return frames


def parse_query(description: str, evidence: str = "", conn: sqlite3.Connection | None = None) -> Query:
    text = f"{description}\n{evidence}"
    query = Query(description, evidence)
    query.frames = parse_frames(text)
    for raw in text.splitlines():
        parsed = log_line(raw)
        if parsed:
            tag, message = parsed
            if message and not message.startswith(("at ", "Caused by")):
                query.messages.append((tag, message))
    for match in _QUOTED.finditer(text):
        value = next(group for group in match.groups() if group)
        if value not in query.quoted:
            query.quoted.append(value)
    for line in evidence.splitlines():
        stripped = line.strip()
        if stripped and not _LOGCAT.match(stripped) and 8 <= len(stripped) <= 200 and not stripped.startswith(("at ", "#")):
            # an error line from a test run or a crash: the text after "Error:" / "Exception:"
            tail = re.split(r"(?:Error|Exception|error|panic|FATAL|Assertion\w*)\s*:\s*", stripped, maxsplit=1)
            if len(tail) == 2 and len(tail[1]) >= 6:
                query.quoted.append(tail[1])
    query.identifiers = list(dict.fromkeys(m.group(1) for m in _IDENT.finditer(text) if len(m.group(1)) >= 4))[:20]
    query.paths = list(dict.fromkeys(m.group(1) for m in _PATHISH.finditer(text)))[:10]
    query.tests = list(dict.fromkeys(m.group(1) for m in _TEST_NAME.finditer(text)))[:10]
    plain = _QUOTED.sub(" ", description) + "\n" + "\n".join(m for _t, m in query.messages)
    query.terms = glossary.terms(plain, conn)
    return query


# --------------------------------------------------------------------------- string search (also the find_strings tool)

_VOLATILE = re.compile(r"%[-#0 +]*\d*(?:\.\d+)?[lhzjt]*[diouxXeEfgGcspn%@]|\{[^{}]*\}|\$\{?[\w.]+\}?|0x[0-9a-fA-F]+|\d+")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", _VOLATILE.sub("#", text)).strip().lower()


def _fragments(text: str) -> list[str]:
    pieces = [piece for chunk in _VOLATILE.split(text) for piece in re.split(r":\s|\s-\s|;\s", chunk)]
    parts = [part.strip(" :,.;'\"=()[]") for part in pieces]
    return sorted({part for part in parts if len(part) >= 5}, key=len, reverse=True)[:4]


@dataclass(frozen=True)
class StringHit:
    path: str
    line: int
    kind: str
    value: str
    tag: str
    container: str
    score: float


def search_strings(conn: sqlite3.Connection, text: str, *, kind: str = "", path: str = "", limit: int = 20) -> list[StringHit]:
    """Strings, tags, keys and messages that match ``text``: exactly, as a substring, or as the
    format string that printed it (``"fan level %d"`` for ``fan level 12``)."""
    text = text.strip()
    if not text:
        return []
    filters = ""
    params: list[object] = []
    if kind:
        filters += " AND kind = ?"
        params.append(kind)
    if path:
        filters += " AND (path = ? OR path LIKE ?)"
        params += [path, path.rstrip("/") + "/%"]
    rows: dict[tuple[str, int, str], tuple] = {}

    def run(sql: str, extra: list[object]) -> None:
        for row in conn.execute(sql + filters + " LIMIT 400", (*extra, *params)):
            rows.setdefault((str(row[0]), int(row[1]), str(row[3])), tuple(row))

    base = "SELECT path, line, kind, value, tag, container FROM strings WHERE "
    run(base + "(value = ? OR tag = ?)", [text, text])
    run(base + "instr(lower(value), ?) > 0", [text.lower()])
    if len(text) >= 8:
        # the code's literal is a piece of the message: "Failed to set fan speed: " in "...: 3"
        run(base + "length(value) >= 8 AND instr(?, lower(value)) > 0", [text.lower()])
    for fragment in _fragments(text):
        run(base + "instr(lower(value), ?) > 0", [fragment.lower()])
    wanted = _norm(text)
    out: list[StringHit] = []
    for row in rows.values():
        value = str(row[3])
        tag = str(row[4])
        normalized = _norm(value)
        if value == text or tag == text:
            score = 1.0
        elif normalized and normalized == wanted:
            score = 0.97
        elif text.lower() in value.lower():
            score = 0.6 + 0.3 * len(text) / max(len(value), 1)
        elif len(value) >= 8 and value.lower() in text.lower():
            score = 0.55 + 0.4 * len(value) / max(len(text), 1)
        else:
            score = 0.8 * difflib.SequenceMatcher(None, normalized, wanted).ratio()
        if str(row[2]) in {"log", "error"}:
            score += 0.03
        out.append(StringHit(str(row[0]), int(row[1]), str(row[2]), value, tag, str(row[5]), round(min(score, 1.0), 3)))
    out = [hit for hit in out if hit.score >= 0.45]
    out.sort(key=lambda hit: (-hit.score, hit.path, hit.line))
    return out[:limit]


# --------------------------------------------------------------------------- retrievers


def _enclosing(conn: sqlite3.Connection, path: str, line: int) -> str:
    row = conn.execute(
        "SELECT name, parent FROM symbols WHERE path = ? AND line <= ? AND end_line >= ? "
        "AND kind NOT IN ('class','interface','struct','enum','trait','object','impl','union') ORDER BY line DESC LIMIT 1",
        (path, line, line),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT name, parent FROM symbols WHERE path = ? AND line <= ? AND end_line >= ? ORDER BY line DESC LIMIT 1",
            (path, line, line),
        ).fetchone()
    if row is None:
        return ""
    return f"{row[1]}.{row[0]}" if row[1] else str(row[0])


def _file_for(conn: sqlite3.Connection, name: str, container: str = "") -> list[str]:
    name = name.replace("\\", "/")
    if conn.execute("SELECT 1 FROM files WHERE path = ?", (name.lstrip("./"),)).fetchone():
        return [name.lstrip("./")]
    base = name.rpartition("/")[2]
    rows = [str(r[0]) for r in conn.execute("SELECT path FROM files WHERE path = ? OR path LIKE ? LIMIT 50",
                                             (base, "%/" + base.replace("_", "\\_")))]
    rows = [row for row in rows if row == base or row.endswith("/" + base)]
    if "/" in name:
        tail = name.lstrip("./")
        better = [row for row in rows if row.endswith(tail)]
        rows = better or rows
    if container and "." in container:
        package = "/".join(container.split(".")[:-1])
        better = [row for row in rows if package in row]
        rows = better or rows
    return rows


def r_frames(query: Query, conn: sqlite3.Connection) -> list[Hit]:
    hits: list[Hit] = []
    for number, frame in enumerate(query.frames, start=1):
        framework = frame.container.startswith(_FRAMEWORK)
        paths = _file_for(conn, frame.file, frame.container) if frame.file else []
        if paths:
            for path in paths[:2]:
                symbol = _enclosing(conn, path, frame.line) if frame.line else frame.symbol
                hits.append(Hit(path, frame.line, symbol or frame.symbol, f"stack frame {number} ({frame.raw.strip()[:80]})"))
        elif frame.symbol and not framework:
            _parent, name = graph.split_symbol(frame.symbol)
            for row in conn.execute("SELECT path, line, name, parent FROM symbols WHERE name = ? LIMIT 5", (name,)):
                hits.append(Hit(str(row[0]), int(row[1]), f"{row[3]}.{row[2]}" if row[3] else str(row[2]),
                                f"stack frame {number} names {frame.symbol}"))
    return hits


def r_strings(query: Query, conn: sqlite3.Connection) -> list[Hit]:
    hits: list[Hit] = []
    seen: set[tuple[str, int]] = set()
    needles = [(message, f"log line from {tag}" if tag else "log line", tag) for tag, message in query.messages]
    needles += [(value, "quoted text", "") for value in query.quoted]
    for needle, label, tag in needles[:12]:
        for found in search_strings(conn, needle, limit=8):
            key = (found.path, found.line)
            if key in seen:
                continue
            seen.add(key)
            bonus = " (same tag)" if tag and found.tag == tag else ""
            what = {"log": "log message", "error": "error message", "resource": "UI string", "property": "property key",
                    "tag": "log tag"}.get(found.kind, "string")
            hits.append(Hit(found.path, found.line, found.container,
                            f"{what} '{found.value[:60]}' matches {label}{bonus}"))
    for tag in {tag for tag, _m in query.messages if tag}:
        for row in conn.execute("SELECT path, line, container FROM strings WHERE kind = 'tag' AND value = ? LIMIT 5", (tag,)):
            hits.append(Hit(str(row[0]), int(row[1]), str(row[2]), f"defines log tag {tag}"))
    return hits


_DEF_KINDS = "('class','interface','struct','enum','trait','object','function','method','constructor','impl','union','type')"


def r_symbols(query: Query, conn: sqlite3.Connection) -> list[Hit]:
    score: dict[tuple[str, str, int], float] = defaultdict(float)
    why: dict[tuple[str, str, int], list[str]] = defaultdict(list)
    for ident in query.identifiers:
        _parent, name = graph.split_symbol(ident)
        for row in conn.execute(f"SELECT path, line, name, parent FROM symbols WHERE name = ? AND kind IN {_DEF_KINDS} LIMIT 20", (name,)):
            key = (str(row[0]), f"{row[3]}.{row[2]}" if row[3] else str(row[2]), int(row[1]))
            score[key] += 3.0
            why[key].append(f"defines {ident}")
    for term, weight, reason in query.terms[:25]:
        if len(term) < 3:
            continue
        like = "%" + term.replace("%", "").replace("_", "\\_") + "%"
        rows = conn.execute(
            f"SELECT path, line, name, parent FROM symbols WHERE lower(name) LIKE ? ESCAPE '\\' AND kind IN {_DEF_KINDS} LIMIT 200",
            (like,),
        ).fetchall()
        if not rows:
            continue
        spread = 1.0 / math.log(2 + len(rows))
        for row in rows:
            parts = glossary.split(str(row[2]))
            exact = term in parts or term == str(row[2]).lower()
            if not exact and not any(part.startswith(term) for part in parts):
                continue
            key = (str(row[0]), f"{row[3]}.{row[2]}" if row[3] else str(row[2]), int(row[1]))
            score[key] += weight * spread * (1.0 if exact else 0.6)
            if len(why[key]) < 3:
                why[key].append(f"name has '{term}'" + (f" ({reason})" if reason not in {"word", "identifier"} else ""))
    ranked = sorted(score, key=lambda key: (-score[key], key))
    return [Hit(path, line, symbol, "; ".join(why[(path, symbol, line)])) for path, symbol, line in ranked[:PER_RETRIEVER * 2]]


def r_paths(query: Query, conn: sqlite3.Connection) -> list[Hit]:
    score: dict[str, float] = defaultdict(float)
    why: dict[str, list[str]] = defaultdict(list)
    for raw in query.paths:
        for path in _file_for(conn, raw)[:5]:
            score[path] += 3.0
            why[path].append(f"named in the description ({raw})")
    for term, weight, _reason in query.terms[:20]:
        if len(term) < 4:
            continue
        like = "%" + term.replace("_", "\\_") + "%"
        rows = conn.execute("SELECT path FROM files WHERE lower(path) LIKE ? ESCAPE '\\' LIMIT 100", (like,)).fetchall()
        if not rows:
            continue
        spread = 1.0 / math.log(2 + len(rows))
        for (path,) in rows:
            name = PurePosixPath(str(path)).name.lower()
            weight_here = weight * spread * (1.0 if term in name else 0.4)
            score[str(path)] += weight_here
            if len(why[str(path)]) < 2:
                why[str(path)].append(f"path has '{term}'")
    ranked = sorted(score, key=lambda path: (-score[path], path))
    return [Hit(path, 0, "", "; ".join(why[path])) for path in ranked[:PER_RETRIEVER]]


def r_commits(query: Query, conn: sqlite3.Connection) -> list[Hit]:
    words = " ".join(term for term, weight, _r in query.terms[:12] if weight >= glossary.STEM_WEIGHT)
    found = history.search_commits(conn, words, limit=10)
    score: dict[str, float] = defaultdict(float)
    why: dict[str, str] = {}
    for rank, (commit, files) in enumerate(found):
        if len(files) > 12:
            continue
        for path in files:
            if not conn.execute("SELECT 1 FROM files WHERE path = ?", (path,)).fetchone():
                continue
            score[path] += 1.0 / (1 + rank)
            why.setdefault(path, f"commit {commit.sha[:8]} '{commit.subject[:50]}'")
    ranked = sorted(score, key=lambda path: (-score[path], path))
    return [Hit(path, 0, "", why[path]) for path in ranked[:PER_RETRIEVER]]


def r_lexical(query: Query, conn: sqlite3.Connection) -> list[Hit]:
    """Words of the description in file text (comments included), through the trigram index."""
    if store.get_meta(conn, "fts") != "1":
        return []
    try:
        if not conn.execute("SELECT 1 FROM content LIMIT 1").fetchone():
            return []
    except sqlite3.OperationalError:
        return []
    score: dict[str, float] = defaultdict(float)
    why: dict[str, list[str]] = defaultdict(list)
    for term, weight, _reason in [item for item in query.terms if len(item[0]) >= 4][:10]:
        try:
            rows = conn.execute("SELECT path FROM content WHERE content MATCH ? LIMIT 300", ('"' + term + '"',)).fetchall()
        except sqlite3.OperationalError:
            continue
        if not rows:
            continue
        spread = 1.0 / math.log(2 + len(rows))
        for (path,) in rows:
            score[str(path)] += weight * spread
            if len(why[str(path)]) < 3:
                why[str(path)].append(term)
    ranked = sorted(score, key=lambda path: (-score[path], path))
    return [Hit(path, 0, "", "text mentions " + ", ".join(why[path])) for path in ranked[:PER_RETRIEVER]]


def r_tests(query: Query, conn: sqlite3.Connection) -> list[Hit]:
    """Code that a named test exercises: what the test function calls, resolved outside test files."""
    hits: list[Hit] = []
    for test in query.tests:
        _parent, name = graph.split_symbol(test)
        rows = conn.execute("SELECT path, name, parent FROM symbols WHERE name = ? LIMIT 5", (name,)).fetchall()
        for path, short, parent in rows:
            qualified = f"{parent}.{short}" if parent else str(short)
            for edge, defs in graph.callees(conn, qualified, path=str(path), limit=40):
                for item in defs:
                    target, _, line = item.rpartition(":")
                    if re.search(r"(^|/)(tests?|androidTest)/|_test\.|Test\.|test_", target):
                        continue
                    hits.append(Hit(target, int(line or 0), edge.dst_name, f"called by test {test}"))
            for edge in graph.imports_of(conn, str(path)):
                if edge.dst_path and not edge.dst_path.endswith("/"):
                    hits.append(Hit(edge.dst_path, 0, "", f"imported by test file of {test}"))
    return hits


Retriever = Callable[[Query, sqlite3.Connection], "list[Hit]"]

#: name -> (retriever, weight). Exact string and stack-frame evidence first; names last.
RETRIEVERS: dict[str, tuple[Retriever, float]] = {
    "frames": (r_frames, 3.0),
    "strings": (r_strings, 2.5),
    "tests": (r_tests, 1.2),
    "symbols": (r_symbols, 1.0),
    "commits": (r_commits, 0.7),
    "paths": (r_paths, 0.8),
    "lexical": (r_lexical, 0.6),
}


@dataclass
class Candidate:
    path: str
    line: int = 0
    symbol: str = ""
    score: float = 0.0
    #: retriever -> its reason for this file, in the order the retrievers found it
    why: dict[str, str] = field(default_factory=dict)

    @property
    def retrievers(self) -> list[str]:
        return list(self.why)

    @property
    def reasons(self) -> list[str]:
        return [reason for reason in self.why.values() if reason]


def fuse(results: dict[str, list[Hit]], weights: dict[str, float]) -> list[Candidate]:
    """Weighted reciprocal rank fusion at file level; the best-ranked symbol of each file is kept."""
    by_file: dict[str, Candidate] = {}
    best_rank: dict[str, float] = {}
    for name, hits in results.items():
        weight = weights.get(name, 1.0)
        seen: set[str] = set()
        rank = 0
        for hit in hits:
            if hit.path in seen:
                continue
            seen.add(hit.path)
            rank += 1
            candidate = by_file.setdefault(hit.path, Candidate(hit.path))
            gain = weight / (RRF_K + rank)
            candidate.score += gain
            candidate.why.setdefault(name, hit.reason)
            if (hit.symbol or hit.line) and gain > best_rank.get(hit.path, 0.0):
                best_rank[hit.path] = gain
                candidate.symbol = hit.symbol or candidate.symbol
                candidate.line = hit.line or candidate.line
    return sorted(by_file.values(), key=lambda c: (-c.score, c.path))


def _expand(conn: sqlite3.Connection, top: list[Candidate], *, limit: int = 3) -> list[Hit]:
    """Neighbours of the best candidates: their callers and the files that import them."""
    hits: list[Hit] = []
    for candidate in top[:limit]:
        for edge in graph.imported_by(conn, candidate.path, limit=10):
            hits.append(Hit(edge.src_path, edge.line, "", f"imports {PurePosixPath(candidate.path).name}"))
        if candidate.symbol:
            for edge, _score in graph.callers(conn, candidate.symbol, limit=10):
                hits.append(Hit(edge.src_path, edge.line, edge.src_symbol, f"calls {candidate.symbol} (likely)"))
    return hits


@dataclass
class Located:
    candidates: list[Candidate]
    ran: list[str]
    late: list[str]
    seconds: float
    query: Query


def locate(db_paths: list, description: str, evidence: str = "", *, budget: float = DEFAULT_BUDGET,
           limit: int = 10, only: list[str] | None = None) -> Located:
    """Run every retriever on every database within ``budget`` seconds and fuse the results."""
    started = time.monotonic()
    first = store.connect(db_paths[0]) if db_paths else None
    try:
        query = parse_query(description, evidence, first)
    finally:
        if first is not None:
            first.close()
    names = [name for name in RETRIEVERS if only is None or name in only]
    results: dict[str, list[Hit]] = defaultdict(list)
    late: list[str] = []

    def run(name: str, db) -> tuple[str, list[Hit]]:
        conn = store.connect(db)
        try:
            return name, RETRIEVERS[name][0](query, conn)[:PER_RETRIEVER * 2]
        finally:
            conn.close()

    pool = ThreadPoolExecutor(max_workers=min(8, max(1, len(names) * max(1, len(db_paths)))))
    try:
        futures = {pool.submit(run, name, db): name for name in names for db in db_paths}
        done, pending = wait(futures, timeout=max(0.05, budget - (time.monotonic() - started)))
        for future in done:
            try:
                name, hits = future.result()
            except Exception:
                continue
            results[name].extend(hits)
        late = sorted({futures[future] for future in pending})
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    weights = {name: weight for name, (_fn, weight) in RETRIEVERS.items()}
    fused = fuse(dict(results), weights)
    if fused and db_paths and time.monotonic() - started < budget * 1.5:
        conn = store.connect(db_paths[0])
        try:
            results["graph"] = _expand(conn, fused)
        finally:
            conn.close()
        weights["graph"] = 0.4
        fused = fuse(dict(results), weights)
    ran = [name for name in names if name not in late]
    return Located(fused[:limit], ran, late, round(time.monotonic() - started, 3), query)


def hypothesis(candidate: Candidate) -> str:
    """A one-line reason to look here, from the strongest evidence (the model refines it)."""
    order = ["frames", "strings", "tests", "commits", "symbols", "paths", "lexical", "graph"]
    for name in order + list(candidate.why):
        if candidate.why.get(name):
            return candidate.why[name]
    return ""
