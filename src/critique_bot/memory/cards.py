# SPDX-License-Identifier: Apache-2.0
"""M1: structure memory. File cards and module cards, written lazily and keyed by content hash.

A **file card** is one or two lines: what the file is for, its main symbols, what it prints,
and who uses it. A **module card** (Soong, Gradle, CMake, Bazel) is a few lines: its build
file, responsibility, entry points, key classes, dependencies and test target.

Cards are written the first time the agent reads or edits a file, or for the top-ranked files
of the active module's repo map; never in a whole-repo pass. A card whose file hash changed is
rebuilt on next use (:func:`ensure_file_card`) or dropped by :func:`invalidate`.

The builder is pluggable. :class:`ExtractiveBuilder` (the default) works offline from the Stage 8
index: symbols, strings, imports and the file's own doc comment. :class:`ModelBuilder` asks the
model one short question with the same facts as input, so the card names only real symbols;
it falls back to the extractive card when the reply is unusable.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol

from critique_bot.memory.store import MemoryStore

#: Characters of a file card's text.
FILE_CARD_CHARS = 320
MODULE_CARD_CHARS = 600
#: Files larger than this get a card from the index only (no text scan).
MAX_SCAN_BYTES = 2_000_000

_CONTAINERS = ("class", "interface", "struct", "enum", "trait", "object", "impl", "union", "type")


@dataclass
class Facts:
    """What the index and the file say about one file; the input of every builder."""

    path: str
    language: str = ""
    doc: str = ""
    containers: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    strings: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    used_by: list[str] = field(default_factory=list)
    used_by_count: int = 0
    module: str = ""

    @property
    def symbols(self) -> list[str]:
        return list(dict.fromkeys(self.containers + self.functions))


class CardBuilder(Protocol):
    name: str

    def file_card(self, facts: Facts) -> str: ...


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return "sha256:" + digest.hexdigest()


# --------------------------------------------------------------------------- facts


_LICENSE = re.compile(r"(?i)copyright|licen[cs]e|spdx|all rights reserved|warrant")


def doc_comment(text: str) -> str:
    """The file's first doc comment that is not a license header, as one plain sentence or two."""
    blocks: list[str] = []
    for match in re.finditer(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'|/\*\*?(.*?)\*/', text[:20_000], re.S):
        blocks.append(next(group for group in match.groups() if group is not None))
        if len(blocks) >= 4:
            break
    lines: list[str] = []
    run: list[str] = []
    for raw in text[:8_000].splitlines():
        stripped = raw.strip()
        if stripped.startswith(("#!", "# -*-")):
            continue
        if stripped.startswith(("#", "//", "--")):
            run.append(stripped.lstrip("#/-! ").strip())
            continue
        if run:
            lines.append(" ".join(item for item in run if item))
            run = []
        if stripped and not stripped.startswith(("package ", "import ", "from ", "#include", "@", "using ")):
            break
    if run:
        lines.append(" ".join(item for item in run if item))
    candidates = [re.sub(r"\s*\*\s*", " ", block) for block in blocks] + lines
    for candidate in candidates:
        cleaned = re.sub(r"\s+", " ", candidate).strip()
        cleaned = re.sub(r"(?:^|\s)@\w+.*$", "", cleaned).strip()  # javadoc tags
        if len(cleaned) < 12 or _LICENSE.search(cleaned):
            continue
        sentences = re.split(r"(?<=[.!?])\s+", cleaned)
        out = sentences[0]
        if len(out) < 60 and len(sentences) > 1:
            out += " " + sentences[1]
        return out[:200].rstrip()
    return ""


def gather(workspace: Path, rel: str, conn: sqlite3.Connection | None) -> Facts:
    """The facts of ``rel``: from the index database when given, plus the file's doc comment."""
    from critique_bot.indexing import langs

    facts = Facts(rel, language=langs.language_of(rel))
    path = Path(workspace) / rel
    try:
        if path.stat().st_size <= MAX_SCAN_BYTES:
            facts.doc = doc_comment(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass
    if conn is None:
        return facts
    try:
        rows = conn.execute(
            "SELECT name, kind, parent FROM symbols WHERE path = ? ORDER BY line LIMIT 400", (rel,)).fetchall()
        for name, kind, parent in rows:
            name = str(name)
            if name.startswith("_") or len(name) < 2:
                continue
            if str(kind) in _CONTAINERS:
                if name not in facts.containers:
                    facts.containers.append(name)
            elif str(kind) in {"function", "method", "constructor"} and name not in facts.functions:
                label = f"{parent}.{name}" if parent and str(kind) != "constructor" else name
                if str(kind) != "constructor":
                    facts.functions.append(label)
        facts.containers = facts.containers[:6]
        facts.functions = facts.functions[:8]
        for kind, value, tag in conn.execute(
                "SELECT kind, value, tag FROM strings WHERE path = ? AND kind IN ('log', 'error', 'tag', 'property', 'resource') "
                "ORDER BY (kind = 'tag') DESC, line LIMIT 40", (rel,)):
            if str(kind) == "tag":
                if str(value) not in facts.tags:
                    facts.tags.append(str(value))
            elif len(str(value)) >= 6 and str(value) not in facts.strings:
                facts.strings.append(str(value)[:70])
        facts.tags = facts.tags[:2]
        facts.strings = facts.strings[:3]
        users = [str(r[0]) for r in conn.execute(
            "SELECT DISTINCT src_path FROM edges WHERE kind = 'imports' AND dst_path = ? LIMIT 50", (rel,))]
        facts.used_by_count = len(users)
        facts.used_by = [PurePosixPath(item).name for item in users[:3]]
        row = conn.execute("SELECT module FROM files WHERE path = ?", (rel,)).fetchone()
        facts.module = str(row[0]) if row and row[0] else ""
    except sqlite3.DatabaseError:
        pass
    return facts


# --------------------------------------------------------------------------- builders


def _phrase(names: list[str]) -> str:
    """Words from symbol names when the file has no doc comment: ``HvacTempHelper`` -> ``hvac temp helper``."""
    words: list[str] = []
    for name in names[:3]:
        for part in re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name.rpartition(".")[2]).replace("_", " ").split():
            if part.lower() not in words:
                words.append(part.lower())
    return " ".join(words[:8])


class ExtractiveBuilder:
    """A card from the facts alone (no model): deterministic, offline, names only real symbols."""

    name = "extractive"

    def file_card(self, facts: Facts) -> str:
        purpose = facts.doc or (f"{_phrase(facts.containers or facts.functions)} ({facts.language})"
                                if (facts.containers or facts.functions) else f"{facts.language or 'file'}")
        parts = [f"{PurePosixPath(facts.path).name}: {purpose.rstrip('.')}."]
        if facts.symbols:
            parts.append("Defines " + ", ".join(facts.symbols[:6]) + ".")
        said = []
        if facts.tags:
            said.append("tag " + "/".join(facts.tags))
        if facts.strings:
            said.append("; ".join(f"'{text}'" for text in facts.strings[:2]))
        if said:
            parts.append("Logs " + " ".join(said) + ".")
        if facts.used_by:
            more = f" and {facts.used_by_count - len(facts.used_by)} more" if facts.used_by_count > len(facts.used_by) else ""
            parts.append("Used by " + ", ".join(facts.used_by) + more + ".")
        text = " ".join(parts)
        return text[:FILE_CARD_CHARS].rstrip()


CARD_PROMPT = """Write a card for one source file: one or two lines saying what it is for, its main symbols, and who uses it.
Use only the names listed below; do not invent names. No preamble, no markdown.

file: {path}
language: {language}
doc comment: {doc}
symbols: {symbols}
strings it logs or prints: {strings}
used by: {used_by}
draft card: {draft}"""


class ModelBuilder:
    """A card from one short helper request (``ask(prompt) -> reply``), with the facts as input.

    A reply that is empty, too long, or names none of the file's symbols is replaced by the
    extractive card, so a weak or confused model never stores a wrong card.
    """

    name = "model"

    def __init__(self, ask: Callable[[str], str], fallback: CardBuilder | None = None) -> None:
        self.ask = ask
        self.fallback = fallback or ExtractiveBuilder()
        self.used_fallback = 0

    def file_card(self, facts: Facts) -> str:
        draft = self.fallback.file_card(facts)
        prompt = CARD_PROMPT.format(
            path=facts.path, language=facts.language or "?", doc=facts.doc or "(none)",
            symbols=", ".join(facts.symbols) or "(none)", strings="; ".join(facts.strings + facts.tags) or "(none)",
            used_by=", ".join(facts.used_by) or "(unknown)", draft=draft)
        try:
            reply = str(self.ask(prompt) or "")
        except Exception:  # a helper failure leaves the extractive card
            reply = ""
        lines = [line.strip(" -*`") for line in reply.strip().splitlines() if line.strip()]
        text = " ".join(lines[:2]).strip()
        names = {name.rpartition(".")[2] for name in facts.symbols}
        mentions = any(name and name in text for name in names) or not names
        if not text or len(text) > FILE_CARD_CHARS * 2 or not mentions:
            self.used_fallback += 1
            return draft
        return text[:FILE_CARD_CHARS].rstrip()


# --------------------------------------------------------------------------- writing cards


def ensure_file_card(store: MemoryStore, workspace: Path, rel: str, conn: sqlite3.Connection | None,
                     builder: CardBuilder | None = None) -> tuple[object, bool]:
    """The card of ``rel``, built now when missing or when the file changed. ``(card, built)``;
    ``(None, False)`` when the file is gone (its card is dropped)."""
    path = Path(workspace) / rel
    digest = file_hash(path) if path.is_file() else ""
    existing = store.card("file", rel)
    if not digest:
        if existing is not None:
            store.drop_card(existing.id)
        return None, False
    if existing is not None and existing.hash == digest:
        return existing, False
    builder = builder or ExtractiveBuilder()
    facts = gather(workspace, rel, conn)
    text = builder.file_card(facts)
    used = getattr(builder, "name", "extractive")
    if isinstance(builder, ModelBuilder) and text == builder.fallback.file_card(facts):
        used = builder.fallback.name
    card = store.put_card("file", rel, hash=digest, text=text, symbols=facts.symbols, builder=used,
                          provenance=f"file:{rel}@{digest[:19]}")
    return card, True


def invalidate(store: MemoryStore, workspace: Path, *, paths: list[str] | None = None) -> list[str]:
    """Drop file cards whose file changed or is gone (all cards, or those of ``paths``). Returns their paths."""
    dropped = []
    wanted = set(paths) if paths is not None else None
    for card in store.cards("file"):
        if wanted is not None and card.key not in wanted:
            continue
        path = Path(workspace) / card.key
        if not path.is_file() or file_hash(path) != card.hash:
            store.drop_card(card.id)
            dropped.append(card.key)
    return dropped


def _is_test_module(name: str, mtype: str, folder: str) -> bool:
    return bool(re.search(r"(?i)tests?$|_test$|^test", name) or "test" in mtype.lower()
                or re.search(r"(^|/)(tests?|androidTest)(/|$)", folder))


def module_facts(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT name, system, type, build_file, dir, srcs, deps FROM modules WHERE name = ? LIMIT 1",
                       (name,)).fetchone()
    if row is None:
        return None
    folder = str(row[4])
    like = (folder.rstrip("/") + "/%") if folder not in {"", "."} else "%"
    deps = json.loads(str(row[6]) or "[]") if row[6] else []
    tests = []
    for other, mtype, odir, odeps in conn.execute("SELECT name, type, dir, deps FROM modules WHERE (dir = ? OR dir LIKE ?) "
                                                   "AND name != ? LIMIT 200", (folder, like, name)):
        if _is_test_module(str(other), str(mtype), str(odir)):
            tests.append(str(other))
    for other, mtype, odir in conn.execute("SELECT name, type, dir FROM modules WHERE deps LIKE ? AND name != ? LIMIT 50",
                                            ('%"' + name.replace("%", "") + '"%', name)):
        if _is_test_module(str(other), str(mtype), str(odir)) and str(other) not in tests:
            tests.append(str(other))
    files = [str(r[0]) for r in conn.execute("SELECT path FROM files WHERE (path LIKE ?) LIMIT 2000", (like,))]
    entry: list[str] = []
    key: list[tuple[int, str]] = []
    for path in files[:300]:
        for sym, kind in conn.execute(f"SELECT name, kind FROM symbols WHERE path = ? AND kind IN {_CONTAINERS!r} LIMIT 20", (path,)):  # nosec B608: a constant tuple
            sym = str(sym)
            if re.search(r"(Activity|Service|Receiver|Provider|Application|Main|Server|Daemon)$", sym) and sym not in entry:
                entry.append(sym)
            users = conn.execute("SELECT COUNT(*) FROM refs WHERE name = ?", (sym,)).fetchone()[0]
            key.append((int(users or 0), sym))
    key.sort(key=lambda item: (-item[0], item[1]))
    return {
        "name": str(row[0]), "system": str(row[1]), "type": str(row[2]), "build_file": str(row[3]), "dir": folder,
        "deps": [str(item) for item in deps][:8], "tests": tests[:3], "entry": entry[:4],
        "key": [sym for _n, sym in key if sym not in entry][:5], "files": len(files),
    }


def module_card_text(facts: dict, docs: list[str]) -> str:
    lines = [f"Module {facts['name']} ({facts['system']}{' ' + facts['type'] if facts['type'] else ''}, "
             f"{facts['build_file']}; {facts['files']} files under {facts['dir'] or '.'})"]
    if docs:
        lines.append("Responsibility: " + " ".join(docs)[:200])
    if facts["entry"]:
        lines.append("Entry points: " + ", ".join(facts["entry"]))
    if facts["key"]:
        lines.append("Key classes: " + ", ".join(facts["key"]))
    if facts["deps"]:
        lines.append("Depends on: " + ", ".join(facts["deps"]))
    if facts["tests"]:
        lines.append("Tests: " + ", ".join(facts["tests"]))
    return "\n".join(lines)[:MODULE_CARD_CHARS]


def ensure_module_card(store: MemoryStore, workspace: Path, conn: sqlite3.Connection, name: str) -> tuple[object, bool]:
    """The card of build module ``name``, rebuilt when its build file or row changed."""
    facts = module_facts(conn, name)
    if facts is None:
        return None, False
    build = Path(workspace) / facts["build_file"]
    digest = hashlib.sha256((file_hash(build) + json.dumps(facts, sort_keys=True)).encode()).hexdigest()
    existing = store.card("module", name)
    if existing is not None and existing.hash == digest:
        return existing, False
    docs = []
    for card in store.cards("file"):
        if facts["dir"] in {"", "."} or card.key.startswith(facts["dir"].rstrip("/") + "/"):
            first = card.text.split(". ", 1)[0].split(": ", 1)[-1]
            if first and first not in docs and len(docs) < 2:
                docs.append(first.rstrip(".") + ".")
    card = store.put_card("module", name, hash=digest, text=module_card_text(facts, docs),
                          symbols=facts["entry"] + facts["key"], provenance=f"file:{facts['build_file']}@{digest[:12]}")
    return card, True


def module_of(conn: sqlite3.Connection, rel: str) -> str:
    """The build module that owns ``rel`` (its nearest build file's first module), or ""."""
    try:
        row = conn.execute("SELECT module FROM files WHERE path = ?", (rel,)).fetchone()
        build = str(row[0]) if row and row[0] else ""
        if not build:
            return ""
        found = conn.execute("SELECT name FROM modules WHERE build_file = ? ORDER BY name LIMIT 1", (build,)).fetchone()
        return str(found[0]) if found else ""
    except sqlite3.DatabaseError:
        return ""
