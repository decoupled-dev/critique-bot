"""Identifier splitting, abbreviations, synonyms, and stemming for finding badly named code.

``expand("tmpMgr")`` gives the exact parts (``tmp``, ``mgr``), their expansions
(``temperature``, ``temporary``, ``manager``) and stems, each with a weight: exact terms
weigh 1.0, expansions less, so a fused ranking prefers exact matches (plan 8, "Synonym
expansion adds noise").

The built-in table covers common abbreviations; the project's own glossary lives in the
index database (``glossary`` table, filled by Stage 8b and by :func:`add_project_term`).
"""

from __future__ import annotations

import re
import sqlite3
import time

#: abbreviation -> what it usually stands for (both directions are used)
BUILTIN: dict[str, tuple[str, ...]] = {
    "tmp": ("temperature", "temporary"),
    "temp": ("temperature", "temporary"),
    "fmt": ("format",),
    "mgr": ("manager",),
    "cfg": ("config", "configuration"),
    "conf": ("config", "configuration"),
    "config": ("configuration",),
    "ctx": ("context",),
    "btn": ("button",),
    "msg": ("message",),
    "err": ("error",),
    "req": ("request",),
    "res": ("response", "result", "resource"),
    "resp": ("response",),
    "svc": ("service",),
    "srv": ("server", "service"),
    "impl": ("implementation",),
    "util": ("utility", "utilities"),
    "utils": ("utility", "utilities"),
    "str": ("string",),
    "num": ("number",),
    "cnt": ("count",),
    "idx": ("index",),
    "len": ("length",),
    "val": ("value",),
    "var": ("variable",),
    "args": ("arguments",),
    "arg": ("argument",),
    "param": ("parameter",),
    "params": ("parameters",),
    "prop": ("property",),
    "props": ("properties",),
    "attr": ("attribute",),
    "auth": ("authentication", "authorization"),
    "db": ("database",),
    "dir": ("directory",),
    "doc": ("document",),
    "env": ("environment",),
    "evt": ("event",),
    "ev": ("event",),
    "exc": ("exception",),
    "ex": ("exception",),
    "info": ("information",),
    "init": ("initialize", "initialization"),
    "lib": ("library",),
    "max": ("maximum",),
    "min": ("minimum",),
    "nav": ("navigation",),
    "obj": ("object",),
    "pkg": ("package",),
    "pos": ("position",),
    "prev": ("previous",),
    "ptr": ("pointer",),
    "ref": ("reference",),
    "repo": ("repository",),
    "sec": ("second", "security"),
    "seq": ("sequence",),
    "src": ("source",),
    "dst": ("destination",),
    "dest": ("destination",),
    "sync": ("synchronize",),
    "async": ("asynchronous",),
    "addr": ("address",),
    "alloc": ("allocate",),
    "buf": ("buffer",),
    "calc": ("calculate",),
    "cb": ("callback",),
    "char": ("character",),
    "cmd": ("command",),
    "col": ("column",),
    "conn": ("connection",),
    "ctl": ("control",),
    "ctrl": ("control", "controller"),
    "cur": ("current", "cursor"),
    "def": ("default", "definition"),
    "del": ("delete",),
    "desc": ("description",),
    "dev": ("device",),
    "diff": ("difference",),
    "disp": ("display",),
    "dlg": ("dialog",),
    "elem": ("element",),
    "exp": ("expiry", "expire", "expression"),
    "ext": ("extension",),
    "fn": ("function",),
    "func": ("function",),
    "gen": ("generate", "generator"),
    "hdr": ("header",),
    "hw": ("hardware",),
    "id": ("identifier",),
    "img": ("image",),
    "lang": ("language",),
    "loc": ("location",),
    "lvl": ("level",),
    "mem": ("memory",),
    "mod": ("module",),
    "net": ("network",),
    "notif": ("notification",),
    "opt": ("option",),
    "pref": ("preference",),
    "prefs": ("preferences",),
    "proc": ("process",),
    "pwd": ("password",),
    "qty": ("quantity",),
    "recv": ("receive",),
    "reg": ("register", "registry"),
    "rm": ("remove",),
    "spd": ("speed",),
    "stat": ("status", "statistic"),
    "stats": ("statistics",),
    "std": ("standard",),
    "tbl": ("table",),
    "ts": ("timestamp",),
    "txt": ("text",),
    "usr": ("user",),
    "vol": ("volume",),
    "win": ("window",),
    "hvac": ("climate", "heating", "ventilation"),
    "ac": ("air conditioning",),
    "fahr": ("fahrenheit",),
    "cel": ("celsius",),
    "ui": ("interface", "display"),
    "vhal": ("vehicle hal", "vehicle property"),
    "hal": ("hardware abstraction layer",),
    "ota": ("over the air update",),
    "bt": ("bluetooth",),
    "wifi": ("wireless",),
    "nfc": ("near field",),
    "anr": ("not responding",),
}

EXACT_WEIGHT = 1.0
STEM_WEIGHT = 0.8
EXPANSION_WEIGHT = 0.5


def split(name: str) -> list[str]:
    """``fooBarBaz``, ``foo_bar_baz``, ``HTTPServer2Go`` -> lower-case parts, digits apart."""
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
    parts = re.sub(r"([A-Za-z])([0-9])", r"\1 \2", parts)
    parts = re.sub(r"([0-9])([A-Za-z])", r"\1 \2", parts)
    return [part.lower() for part in re.split(r"[^A-Za-z0-9]+", parts) if part]


_SUFFIXES = ("ization", "ations", "ation", "ments", "ment", "ness", "ings", "ing", "ers", "ies", "ied", "ed", "er", "es", "ly", "s")


def stem(word: str) -> str:
    """A light suffix-stripping stemmer (enough to match ``expired``/``expiry``/``expires``)."""
    word = word.lower()
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            base = word[: -len(suffix)]
            if suffix in {"ies", "ied"}:
                base += "y"
            return base
    if len(word) > 4 and word.endswith("y"):
        return word[:-1]
    if len(word) > 4 and word.endswith("e"):
        return word[:-1]
    return word


def _reverse() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for short, longs in BUILTIN.items():
        for item in longs:
            for word in item.split():
                out.setdefault(word, []).append(short)
    return out


_REVERSE = _reverse()


def project_terms(conn: sqlite3.Connection | None) -> dict[str, list[tuple[str, float]]]:
    """The project glossary: term -> [(expansion, weight)], both directions."""
    out: dict[str, list[tuple[str, float]]] = {}
    if conn is None:
        return out
    try:
        rows = conn.execute("SELECT term, expansion, weight FROM glossary").fetchall()
    except sqlite3.OperationalError:
        return out
    for term, expansion, weight in rows:
        out.setdefault(str(term).lower(), []).append((str(expansion).lower(), float(weight)))
        out.setdefault(str(expansion).lower(), []).append((str(term).lower(), float(weight)))
    return out


def add_project_term(conn: sqlite3.Connection, term: str, expansion: str, *, weight: float = 1.0,
                     source: str = "project", provenance: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO glossary (term, expansion, weight, source, provenance, updated) VALUES (?, ?, ?, ?, ?, ?)",
        (term.lower(), expansion.lower(), weight, source, provenance, int(time.time())),
    )
    conn.commit()


_STOP = frozenset(
    """the a an and or of to in on at for with without from by is are was were be been it its this that
    when then than not no but if after before into out up down over under can cannot could should would will
    does do did has have had get gets set sets shows show wrong bug issue please fix why how what which where
    who page there their they them some any all one two our we you your i my me also only just still""".split()
)


def terms(text: str, conn: sqlite3.Connection | None = None, *, limit: int = 40) -> list[tuple[str, float, str]]:
    """Search terms for ``text``: ``(term, weight, why)``; exact parts first, then stems and expansions."""
    project = project_terms(conn)
    out: dict[str, tuple[float, str]] = {}

    def add(term: str, weight: float, why: str) -> None:
        if len(term) < 2 or term in _STOP:
            return
        if out.get(term, (0.0, ""))[0] < weight:
            out[term] = (weight, why)

    words: list[str] = []
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        parts = split(token)
        words.extend(parts)
        if len(parts) > 1 and token.lower() not in _STOP:
            add(token.lower(), EXACT_WEIGHT, "identifier")
    for word in words:
        add(word, EXACT_WEIGHT, "word")
    for word in list(words):
        stemmed = stem(word)
        if stemmed != word:
            add(stemmed, STEM_WEIGHT, f"stem of {word}")
        for expansion in BUILTIN.get(word, ()):
            for part in expansion.split():
                add(part, EXPANSION_WEIGHT, f"{word} -> {expansion}")
        for short in _REVERSE.get(word, ()):
            add(short, EXPANSION_WEIGHT, f"{word} -> {short}")
        for expansion, weight in project.get(word, ()):
            for part in expansion.split():
                add(part, EXPANSION_WEIGHT * 1.4 * weight, f"project glossary: {word} -> {expansion}")
    ranked = sorted(out.items(), key=lambda item: (-item[1][0], -len(item[0]), item[0]))
    return [(term, weight, why) for term, (weight, why) in ranked[:limit]]
