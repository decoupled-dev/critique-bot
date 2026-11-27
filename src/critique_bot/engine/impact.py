# SPDX-License-Identifier: Apache-2.0
"""The impact list (plan Stage 7): after an edit changes a signature, the call sites to check.

Before an edit runs, the definitions in the files it touches are read (``code_index``
symbols: tree-sitter when installed, else the regex fallback). After it ran, they are read
again; a function, method or class whose signature line changed, or that disappeared, has
callers to check. They are found with the Stage 8 index (the ``code_nav`` tool: who_calls,
then refs) when the project has one, else with a word-boundary search over the source
files of the same language family. Each becomes a todo: "check 3 call sites of `foo()` in:
a.py:12, b.py:40". An item counts as checked when its todo is completed, or when every
file it names was read or edited after it was listed; the DONE summary lists the rest.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from critique_bot import code_index, log
from critique_bot import tool_api
from critique_bot.engine.flows.checks import walk_files
from critique_bot.tool_api import canonical_tool

#: Kinds whose signature matters to callers.
_CALLABLE_KINDS = frozenset({"function", "method", "class", "interface", "constructor", "struct", "trait", "enum",
                             "object", "record"})
_EDIT_TOOLS = frozenset({"edit_file", "write_files", "apply_patch"})
#: Languages whose files may call each other (a Kotlin caller of a Java method).
_FAMILIES = ({"java", "kotlin"}, {"c", "cpp"}, {"javascript", "typescript"})
MAX_SITES = 40
MAX_SHOWN = 8
_LOCATION_RE = re.compile(r"^\s*(?:<-\s*)?([\w./-]+\.\w+):(\d+)")


@dataclass
class ImpactItem:
    name: str
    path: str
    before: str
    after: str
    sites: list[tuple[str, int]] = field(default_factory=list)
    #: The todo text pushed for it.
    todo: str = ""
    #: TaskState.step when it was listed (reads after it count as checking).
    step: int = 0

    @property
    def files(self) -> list[str]:
        return list(dict.fromkeys(path for path, _line in self.sites))

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.name, "path": self.path, "before": self.before, "after": self.after or None,
                "sites": [f"{path}:{line}" for path, line in self.sites]}


def _norm(signature: str) -> str:
    return " ".join(str(signature or "").split()).rstrip("{:").strip()


def definitions(text: str, path: str) -> dict[str, str]:
    """Qualified name -> signature line, for the callable definitions in one file's text."""
    language = code_index.language_of(path)
    try:
        found = code_index.extract_symbols(text, language)
    except Exception as exc:  # noqa: BLE001 - a parser hiccup only loses the impact list
        log.debug(f"impact: symbols of {path} failed: {exc}")
        return {}
    out: dict[str, str] = {}
    for symbol in found:
        if symbol.kind in _CALLABLE_KINDS:
            out.setdefault(symbol.qualified, _norm(symbol.signature))
    return out


def changed_signatures(before: dict[str, str], after: dict[str, str]) -> list[tuple[str, str, str]]:
    """(name, old signature, new signature or "" when gone) for each definition that changed."""
    out = []
    for name, old in before.items():
        new = after.get(name)
        if new is None:
            out.append((name, old, ""))
        elif new != old:
            out.append((name, old, new))
    return out


def grep_sites(workspace: Path, name: str, path: str, *, files: list[str] | None = None,
               signatures: tuple[str, ...] = ()) -> list[tuple[str, int]]:
    """Uses of ``name`` (word boundaries) in source files of the same language family, outside its definition file."""
    short = name.rpartition(".")[2]
    if not short:
        return []
    language = code_index.language_of(path)
    family = next((group for group in _FAMILIES if language in group), {language})
    pattern = re.compile(r"(?<![\w$])" + re.escape(short) + r"(?![\w$])")
    sites: list[tuple[str, int]] = []
    for rel in files if files is not None else walk_files(workspace):
        if code_index.language_of(rel) not in family:
            continue
        try:
            target = Path(workspace) / rel
            if target.stat().st_size > 2_000_000:
                continue
            lines = target.read_text(encoding="utf-8", errors="replace").split("\n")
        except OSError:
            continue
        for number, line in enumerate(lines, start=1):
            if not pattern.search(line):
                continue
            stripped = line.strip()
            if rel == path and (_norm(stripped) in signatures or re.match(r"(?:def|class|fun|func|fn)\s", stripped)):
                continue  # the definition itself
            sites.append((rel, number))
            if len(sites) >= MAX_SITES:
                return sites
    return sites


def nav_sites(nav: Callable[[dict[str, Any]], str], name: str) -> list[tuple[str, int]]:
    """Call sites from the index: who_calls, else refs (code_nav's text, ``path:line ...`` rows)."""
    sites: list[tuple[str, int]] = []
    for action in ("who_calls", "refs"):
        try:
            text = nav({"action": action, "symbol": name, "name": name}) or ""
        except Exception:  # noqa: BLE001
            text = ""
        for line in text.splitlines():
            match = _LOCATION_RE.match(line)
            if match:
                sites.append((match.group(1), int(match.group(2))))
                continue
            # refs: "path: 12, 40, 88"
            refs = re.match(r"^([\w./-]+\.\w+): ([\d, .()]+)", line)
            if refs:
                for number in re.findall(r"\d+", refs.group(2).split("...")[0]):
                    sites.append((refs.group(1), int(number)))
        if sites:
            break
    return list(dict.fromkeys(sites))[:MAX_SITES]


class ImpactTracker:
    """Watches edits for signature changes; keeps the call sites to check."""

    def __init__(self, workspace: Path, *, nav: Callable[[dict[str, Any]], str] | None = None,
                 on_item: Callable[[ImpactItem], None] | None = None) -> None:
        self.workspace = Path(workspace)
        #: code_nav through the tool registry when the project is indexed (None: grep).
        self.nav = nav
        self.on_item = on_item
        self.items: list[ImpactItem] = []
        self._files: list[str] | None = None

    def _targets(self, call: Any, ctx: Any) -> list[str]:
        name = canonical_tool(getattr(call, "tool", "")) or ""
        if name not in _EDIT_TOOLS:
            return []
        try:
            args = tool_api.normalize_args(name, dict(getattr(call, "arguments", {}) or {}))
            paths = tool_api._paths_of(name, args, ctx)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"impact: paths of {name} failed: {exc}")
            return []
        out = []
        for path in paths:
            try:
                rel = Path(path).resolve().relative_to(self.workspace.resolve()).as_posix()
            except (ValueError, OSError):
                continue
            if code_index.language_of(rel) not in {"text", ""}:
                out.append(rel)
        return out

    def before(self, call: Any, ctx: Any) -> dict[str, dict[str, str]]:
        """The definitions of the files an edit call touches, before it runs."""
        snapshot: dict[str, dict[str, str]] = {}
        for rel in self._targets(call, ctx):
            try:
                text = (self.workspace / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            snapshot[rel] = definitions(text, rel)
        return snapshot

    def after(self, snapshot: dict[str, dict[str, str]], state: Any) -> list[ImpactItem]:
        """Compare with the files now; list and push new items. Returns the new ones."""
        new: list[ImpactItem] = []
        for rel, old in snapshot.items():
            if not old:
                continue
            try:
                text = (self.workspace / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for name, before, after in changed_signatures(old, definitions(text, rel)):
                if any(item.name == name and item.path == rel for item in self.items):
                    continue
                sites = self._sites(name, rel, (before, after))
                if not sites:
                    continue
                item = ImpactItem(name, rel, before, after, sites, step=int(getattr(state, "step", 0) or 0))
                shown = ", ".join(f"{path}:{line}" for path, line in sites[:MAX_SHOWN])
                more = f" (+{len(sites) - MAX_SHOWN} more)" if len(sites) > MAX_SHOWN else ""
                label = f"`{name.rpartition('.')[2]}()`" if not after.startswith("class") else f"`{name}`"
                item.todo = f"check {len(sites)} call site(s) of {label} in: {shown}{more}"[:200]
                self.items.append(item)
                new.append(item)
                self._push_todo(item, state)
                if self.on_item is not None:
                    self.on_item(item)
        return new

    def _sites(self, name: str, rel: str, signatures: tuple[str, ...] = ()) -> list[tuple[str, int]]:
        if self.nav is not None:
            found = nav_sites(self.nav, name.rpartition(".")[2])
            found = [site for site in found if site[0] != rel or not self._is_definition(rel, site[1], signatures)]
            if found:
                return found
        if self._files is None:
            self._files = walk_files(self.workspace)
        return grep_sites(self.workspace, name, rel, files=self._files, signatures=signatures)

    def _is_definition(self, rel: str, line: int, signatures: tuple[str, ...]) -> bool:
        try:
            rows = (self.workspace / rel).read_text(encoding="utf-8", errors="replace").split("\n")
        except OSError:
            return False
        return 0 < line <= len(rows) and _norm(rows[line - 1].strip()) in signatures

    @staticmethod
    def _push_todo(item: ImpactItem, state: Any) -> None:
        todos = getattr(state, "todos", None)
        if todos is None:
            return
        if any(row.get("content") == item.todo for row in todos):
            return
        if len(todos) >= 20:
            return
        todos.append({"id": str(len(todos) + 1), "content": item.todo, "status": "pending"})

    def unchecked(self, state: Any) -> list[ImpactItem]:
        """Items not checked yet: their todo is not completed and some file was not looked at since."""
        todos = {row.get("content"): row.get("status") for row in getattr(state, "todos", None) or []}
        reads = dict(getattr(state, "reads", None) or {})
        edits = dict(getattr(state, "edits", None) or {})
        out = []
        for item in self.items:
            if todos.get(item.todo) in {"completed", "cancelled"}:
                continue
            others = [path for path in item.files if path != item.path]

            def looked(path: str) -> bool:
                record = reads.get(path)
                return path in edits or (record is not None and int(getattr(record, "step", 0) or 0) > item.step)

            if others and all(looked(path) for path in others):
                continue
            out.append(item)
        return out
