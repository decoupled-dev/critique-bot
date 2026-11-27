# SPDX-License-Identifier: Apache-2.0
"""Soong (Android.bp) know-how for AOSP trees: which module builds a file, and the
lunch targets a tree offers.

Building one module (``m CarServiceUpdatable``) takes minutes; ``m`` alone builds the
whole platform and takes hours. A model rarely knows which module owns a file, so crit
works it out:

- From ``out/.../module-info.json`` when the tree was built (module -> folder, installs).
- Otherwise from the ``Android.bp`` files above the file: a module whose ``srcs`` globs
  match it, a ``filegroup`` that lists it and the modules that use ``:that_filegroup``,
  and modules that take a matching ``*_defaults`` through ``defaults``.

The parser is light (no full Blueprint grammar): it finds top-level ``type { ... }``
blocks and their ``name``, ``srcs``, ``exclude_srcs`` and ``defaults`` lists.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_BLOCK = re.compile(r"(?m)^([A-Za-z_][\w]*)\s*\{")
_NAME = re.compile(r'\bname\s*:\s*"([^"]+)"')
_LIST = r"\b{prop}\s*:\s*\[([^\]]*)\]"
_STRING = re.compile(r'"([^"]*)"')
# Strings first, so "src/**/*.java" is not taken for the start of a /* comment */.
_COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*', re.S)


def _strip_comments(text: str) -> str:
    return _COMMENT.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)

#: Module types that only group files or settings; they are not built on their own.
NOT_BUILDABLE = re.compile(r"^(filegroup|.*_defaults|soong_namespace|package|license|license_kind)$")


@dataclass
class Module:
    type: str
    name: str
    bp: str  # workspace-relative Android.bp
    srcs: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    defaults: list[str] = field(default_factory=list)

    @property
    def buildable(self) -> bool:
        return not NOT_BUILDABLE.match(self.type)


def _block_end(text: str, start: int) -> int:
    depth = 0
    index = start
    in_string = False
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(text)


_VARIABLE = re.compile(r"(?m)^([A-Za-z_]\w*)\s*(\+?=)\s*")


def _expression_end(text: str, start: int) -> int:
    """Where a value that starts at ``start`` ends: a comma or closing brace at depth 0."""
    depth = 0
    index = start
    in_string = False
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{(":
            depth += 1
        elif char in "]})":
            if depth == 0:
                return index
            depth -= 1
        elif char == "," and depth == 0:
            return index
        elif char == "\n" and depth == 0 and text[start:index].strip():
            return index
        index += 1
    return len(text)


def _values(expression: str, variables: dict[str, list[str]]) -> list[str]:
    """Strings of ``["a", "b"] + some_var + ["c"]``."""
    out: list[str] = []
    for term in re.split(r"\+(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", expression):
        term = term.strip()
        if not term:
            continue
        if term.startswith("[") or term.startswith('"'):
            out.extend(_STRING.findall(term))
        elif re.fullmatch(r"[A-Za-z_]\w*", term):
            out.extend(variables.get(term, []))
    return out


def _strings(body: str, prop: str, variables: dict[str, list[str]] | None = None) -> list[str]:
    out: list[str] = []
    for match in re.finditer(r"\b" + prop + r"\s*:\s*", body):
        end = _expression_end(body, match.end())
        out.extend(_values(body[match.end() : end], variables or {}))
    return out


def _variables(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for match in _VARIABLE.finditer(text):
        end = _expression_end(text, match.end())
        values = _values(text[match.end() : end], found)
        if match.group(2) == "+=":
            found.setdefault(match.group(1), []).extend(values)
        else:
            found[match.group(1)] = values
    return found


@lru_cache(maxsize=512)
def _parse(bp_path: str, mtime_ns: int, bp_rel: str) -> tuple[Module, ...]:
    del mtime_ns  # part of the cache key only
    try:
        text = _strip_comments(Path(bp_path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ()
    variables = _variables(text)
    modules: list[Module] = []
    for match in _BLOCK.finditer(text):
        open_at = text.find("{", match.start())
        body = text[open_at : _block_end(text, open_at) + 1]
        name = _NAME.search(body)
        if not name:
            continue
        modules.append(
            Module(
                match.group(1), name.group(1), bp_rel,
                # srcs, and its variants (device_common_srcs, host_srcs, ...), not exclude_srcs
                srcs=_strings(body, r"(?!exclude_)(?:\w+_)?srcs", variables),
                exclude=_strings(body, "exclude_srcs", variables),
                defaults=_strings(body, "defaults", variables),
            )
        )
    return tuple(modules)


def parse_bp(workspace: Path, bp: Path) -> tuple[Module, ...]:
    try:
        mtime = bp.stat().st_mtime_ns
    except OSError:
        return ()
    return _parse(str(bp), mtime, bp.relative_to(workspace).as_posix())


@lru_cache(maxsize=2048)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
            continue
        char = pattern[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


def _matches(rel: str, patterns: list[str]) -> bool:
    return any(not item.startswith(":") and _glob_regex(item).match(rel) for item in patterns)


@dataclass
class Owner:
    modules: list[Module]  # buildable modules that compile the file, nearest first
    via: list[str]  # how: "filegroup car-service-srcs", "defaults carservice-defaults"
    bp: str


def modules_for_file(workspace: Path, rel_file: str) -> Owner | None:
    """The modules that build ``rel_file`` (workspace-relative), from the nearest Android.bp
    folder upward whose modules list it."""
    workspace = Path(workspace)
    folder = (workspace / rel_file).parent
    while True:
        bp = folder / "Android.bp"
        if bp.is_file():
            found = _owners_in(workspace, bp, (workspace / rel_file).relative_to(folder).as_posix())
            if found is not None:
                if not found.modules:
                    _users_above(workspace, folder, found)
                return found
        if folder == workspace or workspace not in folder.parents:
            return None
        folder = folder.parent


#: How many folders above a filegroup to look for the modules that use it.
_USER_LEVELS = 6


def _users_above(workspace: Path, folder: Path, found: "Owner") -> None:
    """A file only in a filegroup (frameworks/base/core/java): find, in the Android.bp files
    above, the modules that use that filegroup (or its users' defaults)."""
    names = {item.split(" ", 1)[1] for item in found.via}
    current = folder
    for _ in range(_USER_LEVELS):
        if current == workspace or workspace not in current.parents:
            return
        current = current.parent
        bp = current / "Android.bp"
        if not bp.is_file():
            continue
        modules = parse_bp(workspace, bp)
        changed = True
        while changed:
            changed = False
            for module in modules:
                if module.name in names:
                    continue
                if any(item.startswith(":") and item[1:].split("{")[0] in names for item in module.srcs) or any(
                    item in names for item in module.defaults
                ):
                    names.add(module.name)
                    changed = True
                    if module.buildable:
                        found.modules.append(module)
                    else:
                        found.via.append(f"{module.type} {module.name}")
        if found.modules:
            found.bp = bp.relative_to(workspace).as_posix()
            return


def _owners_in(workspace: Path, bp: Path, rel: str) -> Owner | None:
    modules = parse_bp(workspace, bp)
    hit = {m.name for m in modules if _matches(rel, m.srcs) and not _matches(rel, m.exclude)}
    if not hit:
        return None
    via: list[str] = [f"{m.type} {m.name}" for m in modules if m.name in hit and not m.buildable]
    changed = True
    while changed:  # follow :filegroup references and defaults inside this file
        changed = False
        for module in modules:
            if module.name in hit:
                continue
            uses_group = any(item.startswith(":") and item[1:].split("{")[0] in hit for item in module.srcs)
            uses_defaults = any(item in hit for item in module.defaults)
            if uses_group or uses_defaults:
                hit.add(module.name)
                changed = True
                if not module.buildable:
                    via.append(f"{module.type} {module.name}")
    buildable = [m for m in modules if m.name in hit and m.buildable]
    return Owner(buildable, via, bp.relative_to(workspace).as_posix())


def describe_owner(workspace: Path, rel_file: str) -> str:
    """One line for the model: which ``m`` target builds ``rel_file``."""
    owner = modules_for_file(workspace, rel_file)
    if owner is None:
        return ""
    if not owner.modules:
        return f"{rel_file} is listed in {owner.bp} ({', '.join(owner.via)}), but no module there builds it directly"
    names = [f"{m.name} ({m.type})" for m in owner.modules[:6]]
    chain = f" via {', '.join(owner.via)}" if owner.via else ""
    return f"{rel_file} is built by {', '.join(names)} in {owner.bp}{chain}"


def module_info(workspace: Path) -> dict | None:
    """``module-info.json`` of the last build, or None."""
    for candidate in sorted(Path(workspace).glob("out/target/product/*/module-info.json")):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def lunch_choices(workspace: Path, *, car_first: bool = True, limit: int = 12) -> list[str]:
    """Lunch targets named in the tree's AndroidProducts.mk files (car targets first)."""
    choices: set[str] = set()
    for root in ("device", "vendor"):
        base = Path(workspace) / root
        if not base.is_dir():
            continue
        for path in base.glob("*/*/AndroidProducts.mk"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for block in re.findall(r"COMMON_LUNCH_CHOICES\s*:?=\s*((?:[^\n]*\\\n)*[^\n]*)", text):
                choices.update(item for item in re.split(r"[\s\\]+", block) if "-" in item)
    ordered = sorted(choices, key=lambda item: (not ("car" in item or "auto" in item) if car_first else 0, item))
    return ordered[:limit]
