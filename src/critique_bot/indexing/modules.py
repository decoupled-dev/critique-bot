# SPDX-License-Identifier: Apache-2.0
"""L0 build modules: which module owns a file and what it depends on.

Reads Soong ``Android.bp`` (through :mod:`critique_bot.soong`), Gradle (``build.gradle``,
``build.gradle.kts``), CMake (``CMakeLists.txt``) and Bazel (``BUILD``, ``BUILD.bazel``).
Each build file's modules go into the ``modules`` table when the file is indexed, and every
file's ``files.module`` column names the nearest build file above it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

#: Build file names, best first when one folder has several.
BUILD_FILES = ("Android.bp", "BUILD.bazel", "BUILD", "build.gradle.kts", "build.gradle", "CMakeLists.txt")
_SYSTEM = {
    "Android.bp": "soong",
    "BUILD.bazel": "bazel",
    "BUILD": "bazel",
    "build.gradle.kts": "gradle",
    "build.gradle": "gradle",
    "CMakeLists.txt": "cmake",
}


@dataclass
class Module:
    name: str
    system: str
    type: str
    build_file: str
    dir: str
    srcs: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)


def is_build_file(rel: str) -> bool:
    return PurePosixPath(rel).name in _SYSTEM


def _dir(rel: str) -> str:
    parent = PurePosixPath(rel).parent.as_posix()
    return "" if parent == "." else parent


def _unique(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        if item and item not in seen:
            seen[item] = None
    return list(seen)


def parse_build_file(workspace: Path, rel: str, text: str) -> list[Module]:
    """The modules one build file defines."""
    name = PurePosixPath(rel).name
    system = _SYSTEM.get(name)
    folder = _dir(rel)
    if system == "soong":
        from critique_bot import soong

        path = Path(workspace) / rel
        found = soong.parse_bp(Path(workspace), path) if path.is_file() else ()
        return [
            Module(m.name, "soong", m.type, rel, folder, list(m.srcs), _unique(list(m.deps) + list(m.defaults)))
            for m in found
        ]
    if system == "gradle":
        return [_gradle(rel, folder, text)]
    if system == "cmake":
        return _cmake(rel, folder, text)
    if system == "bazel":
        return _bazel(rel, folder, text)
    return []


_GRADLE_PROJECT = re.compile(r"project\s*\(\s*(?:path\s*[:=]\s*)?['\"](:[\w:.-]*)['\"]")
_GRADLE_TYPED = re.compile(r"projects\.([\w.]+)")
_GRADLE_EXTERNAL = re.compile(
    r"\b(?:implementation|api|compileOnly|runtimeOnly|kapt|ksp|testImplementation|androidTestImplementation)\s*\(?\s*['\"]([\w.\-]+:[\w.\-]+)(?::[^'\"]*)?['\"]"
)


def _gradle(rel: str, folder: str, text: str) -> Module:
    kind = "gradle"
    if re.search(r"com\.android\.application|android\.application", text):
        kind = "android_app"
    elif re.search(r"com\.android\.library|android\.library", text):
        kind = "android_library"
    elif re.search(r"kotlin\(\s*['\"]jvm['\"]\s*\)|org\.jetbrains\.kotlin\.jvm|java-library|\bjava\b", text):
        kind = "jvm_library"
    name = ":" + folder.replace("/", ":") if folder else ":"
    deps = [match.group(1) for match in _GRADLE_PROJECT.finditer(text)]
    deps += [":" + match.group(1).replace(".", ":") for match in _GRADLE_TYPED.finditer(text)]
    deps += [match.group(1) for match in _GRADLE_EXTERNAL.finditer(text)]
    srcs = [f"{folder}/src/**" if folder else "src/**"]
    return Module(name, "gradle", kind, rel, folder, srcs, _unique(deps))


_CMAKE_TARGET = re.compile(r"\b(add_library|add_executable)\s*\(\s*([\w.\-${}]+)([^)]*)\)", re.I | re.S)
_CMAKE_LINK = re.compile(r"\btarget_link_libraries\s*\(\s*([\w.\-${}]+)([^)]*)\)", re.I | re.S)
_CMAKE_SOURCES = re.compile(r"\btarget_sources\s*\(\s*([\w.\-${}]+)([^)]*)\)", re.I | re.S)
_CMAKE_KEYWORDS = frozenset({"PUBLIC", "PRIVATE", "INTERFACE", "STATIC", "SHARED", "MODULE", "OBJECT", "ALIAS",
                             "IMPORTED", "GLOBAL", "EXCLUDE_FROM_ALL", "WIN32", "MACOSX_BUNDLE", "LINK_PUBLIC", "LINK_PRIVATE"})


def _cmake_words(text: str) -> list[str]:
    text = re.sub(r"#[^\n]*", "", text)
    return [word.strip('"') for word in text.split() if word.strip('"') and word.strip('"') not in _CMAKE_KEYWORDS]


def _cmake(rel: str, folder: str, text: str) -> list[Module]:
    clean = re.sub(r"#[^\n]*", "", text)
    modules: dict[str, Module] = {}
    for match in _CMAKE_TARGET.finditer(clean):
        kind = "executable" if match.group(1).lower() == "add_executable" else "library"
        srcs = [f"{folder}/{item}" if folder else item for item in _cmake_words(match.group(3)) if "." in item]
        modules[match.group(2)] = Module(match.group(2), "cmake", kind, rel, folder, srcs, [])
    for match in _CMAKE_SOURCES.finditer(clean):
        target = modules.get(match.group(1))
        if target is not None:
            target.srcs += [f"{folder}/{item}" if folder else item for item in _cmake_words(match.group(2)) if "." in item]
    for match in _CMAKE_LINK.finditer(clean):
        target = modules.get(match.group(1))
        if target is not None:
            target.deps = _unique(target.deps + _cmake_words(match.group(2)))
    return list(modules.values())


_BAZEL_RULE = re.compile(r"^(\w+)\s*\(", re.M)


def _bazel_list(body: str, prop: str) -> list[str]:
    match = re.search(r"\b" + prop + r"\s*=\s*\[([^\]]*)\]", body)
    return re.findall(r"\"([^\"]+)\"", match.group(1)) if match else []


def _bazel(rel: str, folder: str, text: str) -> list[Module]:
    out: list[Module] = []
    for match in _BAZEL_RULE.finditer(text):
        depth, index = 0, match.end() - 1
        while index < len(text):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        body = text[match.end() : index]
        name = re.search(r"\bname\s*=\s*\"([^\"]+)\"", body)
        if not name or match.group(1) in {"load", "package", "exports_files", "licenses"}:
            continue
        srcs = [f"{folder}/{item}" if folder else item for item in _bazel_list(body, "srcs") + _bazel_list(body, "hdrs")]
        deps = _bazel_list(body, "deps") + _bazel_list(body, "runtime_deps") + _bazel_list(body, "exports")
        out.append(Module(name.group(1), "bazel", match.group(1), rel, folder, srcs, _unique(deps)))
    return out


# --------------------------------------------------------------------------- storage


def store(conn: sqlite3.Connection, build_file: str, modules: list[Module]) -> None:
    conn.execute("DELETE FROM modules WHERE build_file = ?", (build_file,))
    conn.executemany(
        "INSERT INTO modules (name, system, type, build_file, dir, srcs, deps) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(m.name, m.system, m.type, m.build_file, m.dir, json.dumps(m.srcs), json.dumps(m.deps)) for m in modules],
    )


def forget(conn: sqlite3.Connection, build_file: str) -> None:
    conn.execute("DELETE FROM modules WHERE build_file = ?", (build_file,))


def build_files(conn: sqlite3.Connection) -> dict[str, str]:
    """folder -> its build file (the best one when there are several)."""
    rank = {name: number for number, name in enumerate(BUILD_FILES)}
    best: dict[str, str] = {}
    for (path,) in conn.execute("SELECT DISTINCT build_file FROM modules"):
        rel = str(path)
        folder = _dir(rel)
        current = best.get(folder)
        if current is None or rank.get(PurePosixPath(rel).name, 99) < rank.get(PurePosixPath(current).name, 99):
            best[folder] = rel
    return best


def owner_build_file(rel: str, by_dir: dict[str, str]) -> str:
    folder = _dir(rel)
    while True:
        if folder in by_dir:
            return by_dir[folder]
        if not folder:
            return ""
        folder = _dir(folder)


def assign(conn: sqlite3.Connection, paths: list[str] | None = None) -> None:
    """Fill ``files.module`` (the nearest build file) for ``paths``, or for every file."""
    by_dir = build_files(conn)
    if paths is None:
        paths = [str(row[0]) for row in conn.execute("SELECT path FROM files")]
    conn.executemany(
        "UPDATE files SET module = ? WHERE path = ?", [(owner_build_file(rel, by_dir), rel) for rel in paths]
    )


def _row(row: sqlite3.Row) -> Module:
    return Module(str(row["name"]), str(row["system"]), str(row["type"]), str(row["build_file"]), str(row["dir"]),
                  json.loads(row["srcs"] or "[]"), json.loads(row["deps"] or "[]"))


def modules_in(conn: sqlite3.Connection, build_file: str) -> list[Module]:
    return [_row(row) for row in conn.execute("SELECT * FROM modules WHERE build_file = ? ORDER BY rowid", (build_file,))]


def by_name(conn: sqlite3.Connection, name: str) -> list[Module]:
    return [_row(row) for row in conn.execute("SELECT * FROM modules WHERE name = ? ORDER BY build_file", (name,))]


def dependents(conn: sqlite3.Connection, name: str, *, limit: int = 30) -> list[Module]:
    """Modules that list ``name`` among their deps."""
    needle = json.dumps(name)
    rows = conn.execute(
        "SELECT * FROM modules WHERE instr(deps, ?) > 0 ORDER BY build_file LIMIT ?", (needle, limit * 3)
    ).fetchall()
    out = [_row(row) for row in rows]
    return [m for m in out if name in m.deps][:limit]


def _glob_match(rel: str, pattern: str) -> bool:
    from critique_bot import soong

    return bool(soong._glob_regex(pattern).match(rel))


def owners(workspace: Path, conn: sqlite3.Connection, rel: str) -> tuple[list[Module], str, list[str]]:
    """The modules that build ``rel``, the build file they are in, and how (filegroups, defaults)."""
    row = conn.execute("SELECT module FROM files WHERE path = ?", (rel,)).fetchone()
    build_file = str(row[0]) if row is not None and row[0] else owner_build_file(rel, build_files(conn))
    if not build_file:
        return [], "", []
    if PurePosixPath(build_file).name == "Android.bp":
        from critique_bot import soong

        owner = soong.modules_for_file(Path(workspace), rel) if (Path(workspace) / rel).exists() else None
        if owner is not None:
            names = {m.name for m in owner.modules}
            found = [m for m in by_name_many(conn, names)] or [
                Module(m.name, "soong", m.type, owner.bp, _dir(owner.bp), list(m.srcs), list(m.deps)) for m in owner.modules
            ]
            return found, owner.bp, list(owner.via)
    candidates = modules_in(conn, build_file)
    folder = _dir(build_file)
    local = rel[len(folder) + 1 :] if folder else rel
    matched = [
        m for m in candidates
        if any(_glob_match(rel, src) or _glob_match(local, src) for src in m.srcs if not src.startswith(":"))
    ]
    return (matched or candidates), build_file, []


def by_name_many(conn: sqlite3.Connection, names: set[str]) -> list[Module]:
    out: list[Module] = []
    for name in sorted(names):
        out.extend(by_name(conn, name))
    return out
