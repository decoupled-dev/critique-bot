# SPDX-License-Identifier: Apache-2.0
"""Soong-aware test selection: changed files -> owning modules -> the tests to run.

Two sources, as a platform engineer would look:

- ``TEST_MAPPING`` files in the changed file's folder and every folder above it (and the
  files they ``import``): the tests the owners asked to run for changes there, presubmit
  groups first.
- ``Android.bp`` test modules near the owning module (its folder, and ``tests/`` folders of
  the folders above): a test that instruments the owning app (``instrumentation_for``) or
  builds against the owning library (``static_libs``, ``libs``, ... also through its
  ``defaults``).

Nothing is built or run; the result names the tests and the ``atest`` line.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from critique_bot import soong

MAX_BP_FILES = 400
_SKIP = frozenset({".git", ".repo", "out", "prebuilts", "node_modules", ".crit"})
_TEST_DIRS = ("tests", "test", "unittests", "unit_tests", "androidTest")


@dataclass
class TestPick:
    name: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    host: bool = False

    def add(self, score: float, reason: str) -> None:
        self.score += score
        if reason not in self.reasons:
            self.reasons.append(reason)


@dataclass
class Selection:
    owners: list[tuple[str, list[str], str]] = field(default_factory=list)  # file, modules, build file
    tests: list[TestPick] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _strip_json_comments(text: str) -> str:
    out = []
    for match in re.finditer(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/|.', text, re.S):
        token = match.group(0)
        out.append("" if token.startswith(("//", "/*")) else token)
    return "".join(out)


def read_test_mapping(path: Path) -> dict:
    try:
        data = json.loads(_strip_json_comments(path.read_text(encoding="utf-8", errors="replace")))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _mapping_tests(workspace: Path, mapping: Path, picks: dict[str, TestPick], seen: set[Path], depth: int = 0) -> None:
    if mapping in seen or depth > 2:
        return
    seen.add(mapping)
    data = read_test_mapping(mapping)
    rel = mapping.relative_to(workspace).as_posix()
    for group_name, entries in data.items():
        if group_name == "imports":
            for item in entries if isinstance(entries, list) else []:
                target = item.get("path") if isinstance(item, dict) else None
                if isinstance(target, str) and (workspace / target / "TEST_MAPPING").is_file():
                    _mapping_tests(workspace, workspace / target / "TEST_MAPPING", picks, seen, depth + 1)
            continue
        if not isinstance(entries, list):
            continue
        presubmit = "presubmit" in group_name and "postsubmit" not in group_name
        weight = (3.0 if presubmit else 1.0) / (1 + depth)
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            pick = picks.setdefault(item["name"], TestPick(item["name"]))
            pick.add(weight, f"{rel} {group_name}")
            if item.get("host") is True:
                pick.host = True


def _bp_files(workspace: Path, folders: list[Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for base in folders:
        if not base.is_dir():
            continue
        for folder, dirs, names in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP and not d.startswith("."))
            path = Path(folder) / "Android.bp"
            if "Android.bp" in names and path not in seen:
                seen.add(path)
                found.append(path)
            if len(found) >= MAX_BP_FILES:
                return found
    return found


def _with_defaults(modules: tuple[soong.Module, ...]) -> dict[str, set[str]]:
    """Each module's deps, its defaults' deps included (same file)."""
    by_name = {m.name: m for m in modules}
    out: dict[str, set[str]] = {}
    for module in modules:
        deps = set(module.deps)
        pending = list(module.defaults)
        guard = 0
        while pending and guard < 20:
            guard += 1
            other = by_name.get(pending.pop())
            if other is not None:
                deps.update(other.deps)
                pending.extend(other.defaults)
        out[module.name] = deps
    return out


def select(workspace: Path, files: list[str]) -> Selection:
    workspace = Path(workspace)
    result = Selection()
    picks: dict[str, TestPick] = {}
    owner_names: set[str] = set()
    near: list[Path] = []
    mapping_seen: set[Path] = set()
    for rel in files[:40]:
        rel = PurePosixPath(rel.replace("\\", "/")).as_posix().lstrip("./")
        owner = soong.modules_for_file(workspace, rel)
        names = [m.name for m in owner.modules] if owner is not None else []
        result.owners.append((rel, names, owner.bp if owner is not None else ""))
        for module in owner.modules if owner is not None else []:
            owner_names.add(module.name)
            if module.is_test:
                picks.setdefault(module.name, TestPick(module.name)).add(4.0, f"{rel} is in this test module")
        if owner is not None:
            bp_dir = (workspace / owner.bp).parent
            near.append(bp_dir)
            for parent in list(bp_dir.parents)[:3]:
                if workspace not in parent.parents and parent != workspace:
                    break
                near += [parent / name for name in _TEST_DIRS]
        folder = (workspace / rel).parent
        while True:
            mapping = folder / "TEST_MAPPING"
            if mapping.is_file():
                _mapping_tests(workspace, mapping, picks, mapping_seen)
            if folder == workspace or workspace not in folder.parents:
                break
            folder = folder.parent
    if owner_names:
        for bp in _bp_files(workspace, near):
            modules = soong.parse_bp(workspace, bp)
            deps = _with_defaults(modules)
            rel_bp = bp.relative_to(workspace).as_posix()
            for module in modules:
                if not module.is_test or module.name in owner_names:
                    continue
                if module.instrumentation_for in owner_names:
                    picks.setdefault(module.name, TestPick(module.name)).add(
                        3.0, f"instruments {module.instrumentation_for} ({rel_bp})")
                linked = sorted(deps.get(module.name, set()) & owner_names)
                if linked:
                    picks.setdefault(module.name, TestPick(module.name)).add(
                        2.0, f"builds against {', '.join(linked[:3])} ({rel_bp})")
                if module.name in picks and "host" in module.type:
                    picks[module.name].host = True
    else:
        result.notes.append("no Android.bp module owns these files")
    result.tests = sorted(picks.values(), key=lambda p: (-p.score, p.name))
    return result


def render(selection: Selection, *, limit: int = 12) -> str:
    lines = ["changed files -> owning modules:"]
    for rel, names, bp in selection.owners[:20]:
        lines.append(f"  {rel} -> {', '.join(names) if names else 'no module'}" + (f" ({bp})" if bp else ""))
    if selection.tests:
        lines.append(f"tests to run ({len(selection.tests)}; TEST_MAPPING presubmit and direct tests first):")
        for pick in selection.tests[:limit]:
            lines.append(f"  {pick.name}{' [host]' if pick.host else ''}: {'; '.join(pick.reasons[:3])}")
        names = [pick.name for pick in selection.tests[:3]]
        lines.append(f"run: aosp_build action atest with tests {json.dumps(names)} (atest {' '.join(names)})")
    else:
        lines.append("no test found: no TEST_MAPPING above these files and no test module near their modules "
                     "builds against them. Build the module (aosp_build action m) to check the change compiles, "
                     "and say that no test covers it.")
    lines += selection.notes
    return "\n".join(lines)
