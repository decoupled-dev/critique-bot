# SPDX-License-Identifier: Apache-2.0
"""The indexing engine (Stage 8): storage and migrations, per-language extraction (golden
files), the graph, history, incremental updates, and the index tools.

Golden files live in ``tests/index_golden``: ``<file>.golden.json`` when the result does not
depend on tree-sitter (Python, config formats, AIDL/HIDL), else ``<file>.regex.json`` (no
tree-sitter) and ``<file>.ts.json`` (with the ``index`` extra; skipped when it is missing).
Regenerate with ``CRIT_UPDATE_GOLDEN=1`` and review the diff.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from critique_bot import code_index
from critique_bot.indexing import facts, store, symbols
from critique_bot.indexing.langs import language_of

GOLDEN = Path(__file__).resolve().parent / "index_golden"
UPDATE = os.environ.get("CRIT_UPDATE_GOLDEN") == "1"
TS_LANGS = set(symbols.TS_MODULES) - {"python"}


def snapshot(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    found = facts.parse_text(text, language_of(path.name), path.name)
    return {
        "language": found.language,
        "symbols": [[s.kind, s.qualified, s.line, s.end_line] for s in found.symbols],
        "edges": sorted([list(edge) for edge in found.edges]),
        "strings": sorted([list(item) for item in found.strings]),
    }


def golden_name(path: Path, *, treesitter: bool) -> Path:
    language = language_of(path.name)
    if language not in TS_LANGS:
        return path.with_name(path.name + ".golden.json")
    return path.with_name(path.name + (".ts.json" if treesitter else ".regex.json"))


def sources() -> list[Path]:
    return sorted(p for p in GOLDEN.iterdir() if not p.name.endswith(".json"))


def ts_ready(language: str) -> bool:
    return symbols.treesitter_available(language)


class GoldenTests(unittest.TestCase):
    def check(self, path: Path, treesitter: bool) -> None:
        expected_path = golden_name(path, treesitter=treesitter)
        got = snapshot(path)
        if UPDATE:
            expected_path.write_text(json.dumps(got, indent=1) + "\n", encoding="utf-8")
            return
        self.assertTrue(expected_path.is_file(), f"no golden file {expected_path.name}; run with CRIT_UPDATE_GOLDEN=1")
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        self.assertEqual(got, expected, path.name)

    def test_without_treesitter(self) -> None:
        saved = dict(symbols._ts_cache)
        try:
            for language in symbols.TS_MODULES:
                symbols._ts_cache[language] = None  # as if the index extra were missing
            for path in sources():
                with self.subTest(file=path.name):
                    self.check(path, treesitter=False)
        finally:
            symbols._ts_cache.clear()
            symbols._ts_cache.update(saved)

    def test_with_treesitter(self) -> None:
        ran = 0
        for path in sources():
            language = language_of(path.name)
            if language in TS_LANGS and not ts_ready(language):
                continue
            if language not in TS_LANGS:
                continue
            with self.subTest(file=path.name):
                self.check(path, treesitter=True)
                ran += 1
        if not ran:
            self.skipTest("tree-sitter (the index extra) is not installed")

    def test_every_language_is_covered(self) -> None:
        languages = {language_of(path.name) for path in sources()}
        for language in ("python", "java", "kotlin", "c", "cpp", "go", "rust", "typescript", "javascript", "aidl",
                         "hidl", "rc", "sepolicy", "xml", "prop"):
            self.assertIn(language, languages)

    def test_key_facts(self) -> None:
        java = snapshot(GOLDEN / "Sample.java")
        self.assertIn(["log", "Failed to set fan speed", "HvacPanel"], [s[1:4] for s in java["strings"]])
        self.assertIn(["inherits", "HvacPanel", "BasePanel"], [e[:3] for e in java["edges"]])
        self.assertIn(["inherits", "HvacPanel", "OnClickListener"], [e[:3] for e in java["edges"]])
        self.assertIn(["calls", "HvacPanel.onClick", "readTemp"], [e[:3] for e in java["edges"]])
        hal = snapshot(GOLDEN / "IFan.hal")
        self.assertIn(["method", "IFan.setSpeed"], [s[:2] for s in hal["symbols"]])
        aidl = snapshot(GOLDEN / "IVehicle.aidl")
        self.assertIn(["method", "IVehicle.set"], [s[:2] for s in aidl["symbols"]])
        contexts = snapshot(GOLDEN / "property_contexts")
        self.assertEqual(contexts["strings"][0][1], "property")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_database_is_current_and_in_wal_mode(self) -> None:
        conn = store.connect(self.root / "index.db")
        self.assertTrue(store.migrate(conn))
        self.assertTrue(store.is_current(conn))
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for name in ("files", "symbols", "refs", "edges", "strings", "modules", "commits", "commit_files",
                     "cochange", "blame_cache", "glossary"):
            self.assertIn(name, tables)
        conn.close()

    def test_a_1_0_catalog_is_migrated_and_parsed_again(self) -> None:
        db = self.root / "index.db"
        conn = sqlite3.connect(db)
        conn.executescript(store._BASE)
        conn.execute("INSERT INTO meta VALUES ('schema', '2')")
        conn.execute("INSERT INTO files VALUES ('a.py', 1, 1, 'python')")
        conn.commit()
        conn.close()
        (self.root / "a.py").write_text("def alpha():\n    return beta()\n", encoding="utf-8")
        code_index.refresh_index(self.root, db)
        conn = store.connect(db)
        self.assertTrue(store.is_current(conn))
        self.assertEqual(conn.execute("SELECT dst_name FROM edges WHERE kind = 'calls'").fetchall()[0][0], "beta")
        self.assertTrue(conn.execute("SELECT hash FROM files WHERE path = 'a.py'").fetchone()[0])
        conn.close()

    def test_a_database_from_a_newer_crit_is_rebuilt(self) -> None:
        db = self.root / "index.db"
        conn = store.connect(db)
        store.migrate(conn)
        store.set_meta(conn, "schema", "999")
        conn.commit()
        conn.close()
        (self.root / "a.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
        stats = code_index.refresh_index(self.root, db)
        self.assertEqual(stats.symbols, 1)

    def test_the_old_index_file_is_removed(self) -> None:
        cache = self.root / ".crit" / "cache"
        cache.mkdir(parents=True)
        (cache / "index.sqlite").write_bytes(b"old")
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        code_index.refresh_index(self.root, cache / "index.db")
        self.assertFalse((cache / "index.sqlite").exists())

    def test_later_stages_register_migrations(self) -> None:
        saved = dict(store.MIGRATIONS)
        try:
            new = store.current_version() + 1
            store.register_migration(new, lambda conn: conn.execute("CREATE TABLE cards (path TEXT)"))
            with self.assertRaises(ValueError):
                store.register_migration(new, lambda conn: None)
            conn = store.connect(self.root / "index.db")
            store.migrate(conn)
            self.assertEqual(store.version(conn), new)
            conn.execute("SELECT * FROM cards")
            conn.close()
        finally:
            store.MIGRATIONS.clear()
            store.MIGRATIONS.update(saved)

    def test_group_databases_live_next_to_the_project_database(self) -> None:
        index = self.root / ".crit" / "cache" / "index.db"
        self.assertEqual(store.group_db_path(index, "packages/services/Car"),
                         self.root / ".crit" / "cache" / "index" / "packages__services__Car.db")


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env).stdout


def make_project(root: Path) -> None:
    """A small multi-language project with a git history."""
    files = {
        "app/__init__.py": "",
        "app/units.py": (
            "def to_display(value, unit):\n"
            "    if unit == 'F':\n"
            "        return value * 9 / 5 + 32\n"
            "    return value\n"
        ),
        "app/panel.py": (
            "import logging\n"
            "from app.units import to_display\n\n"
            "log = logging.getLogger('hvac')\n\n\n"
            "class Panel:\n"
            "    def show(self, value, unit):\n"
            "        if value is None:\n"
            "            log.error('temperature sensor returned nothing for zone %s', unit)\n"
            "            return ''\n"
            "        return str(to_display(value, unit))\n"
        ),
        "app/main.py": "from app.panel import Panel\n\n\ndef run():\n    return Panel().show(20, 'C')\n",
        "tests/__init__.py": "",
        "tests/test_panel.py": (
            "import unittest\n"
            "from app.panel import Panel\n\n\n"
            "class PanelTest(unittest.TestCase):\n"
            "    def test_celsius(self):\n"
            "        self.assertEqual(Panel().show(20, 'C'), '20')\n\n"
            "    def test_fahrenheit(self):\n"
            "        self.assertEqual(Panel().show(100, 'F'), '212')\n"
        ),
        "native/CMakeLists.txt": "add_library(fan fan.c)\ntarget_link_libraries(fan log)\n",
        "native/fan.c": '#define LOG_TAG "fanctl"\nint fan_set(int level) {\n  if (level > 9) { ALOGE("fan level too high: %d", level); return -1; }\n  return 0;\n}\n',
        "svc/Android.bp": 'java_library {\n    name: "hvac-svc",\n    srcs: ["src/**/*.java"],\n    static_libs: ["hvac-units"],\n}\n',
        "svc/src/com/x/HvacService.java": (
            "package com.x;\n\nimport com.x.util.Units;\n\n"
            "public class HvacService extends BaseService {\n"
            "    private static final String TAG = \"HvacSvc\";\n"
            "    public void setTemp(int t) {\n        Log.w(TAG, \"temp out of range\");\n        Units.convert(t);\n    }\n}\n"
        ),
        "svc/src/com/x/util/Units.java": "package com.x.util;\n\npublic class Units {\n    public static int convert(int t) { return t; }\n}\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first version of the hvac panel")
    (root / "app" / "units.py").write_text((root / "app" / "units.py").read_text() + "\n\ndef clamp(v):\n    return v\n")
    (root / "app" / "panel.py").write_text((root / "app" / "panel.py").read_text() + "\n# fahrenheit\n")
    git(root, "commit", "-q", "-am", "fix fahrenheit rounding in the display")


def dump(db: Path) -> dict:
    conn = sqlite3.connect(db)
    try:
        return {
            "files": sorted(conn.execute("SELECT path, language, hash, module FROM files").fetchall()),
            "symbols": sorted(conn.execute("SELECT name, kind, path, line, end_line, parent, signature FROM symbols").fetchall()),
            "edges": sorted(conn.execute("SELECT src_path, src_symbol, kind, dst_name, coalesce(dst_path, ''), line FROM edges").fetchall()),
            "strings": sorted(conn.execute("SELECT path, line, kind, value, tag, container FROM strings").fetchall()),
            "modules": sorted(conn.execute("SELECT name, system, type, build_file, dir, srcs, deps FROM modules").fetchall()),
            "refs": sorted(conn.execute("SELECT name, path, line, count FROM refs").fetchall()),
        }
    finally:
        conn.close()


class ProjectCase(unittest.TestCase):
    def setUp(self) -> None:
        from critique_bot import file_catalog

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()
        make_project(self.root)
        file_catalog.forget()
        self.db = self.root / ".crit" / "cache" / "index.db"
        self.db.parent.mkdir(parents=True)
        code_index.refresh_index(self.root, self.db)
        from types import SimpleNamespace

        self.ctx = SimpleNamespace(workspace=self.root, index_path=self.db, max_chars=8000, state=None)

    def tearDown(self) -> None:
        from critique_bot import file_catalog
        from critique_bot.indexing import engine

        engine.wait_all(30)
        file_catalog.forget()
        self.tmp.cleanup()

    def tool(self, tool_name: str, **args) -> str:
        from critique_bot.indexing import tools

        ok, text = tools.run(tool_name, args, self.ctx)
        self.assertTrue(ok, text)
        return text


class IncrementalTests(ProjectCase):
    def test_incremental_matches_a_full_rebuild(self) -> None:
        from critique_bot import file_catalog

        # modify, add, rename and delete files, refreshing in between
        (self.root / "app" / "units.py").write_text("def to_display(value, unit):\n    return round(value)\n")
        (self.root / "app" / "extra.py").write_text("from app.units import to_display\n\ndef twice(v):\n    return to_display(v, 'C') * 2\n")
        code_index.refresh_index(self.root, self.db)
        (self.root / "app" / "main.py").rename(self.root / "app" / "entry.py")
        (self.root / "native" / "CMakeLists.txt").unlink()
        (self.root / "svc" / "src" / "com" / "x" / "util" / "Units.java").unlink()
        file_catalog.forget()
        code_index.refresh_index(self.root, self.db)
        code_index.refresh_path(self.root, self.db, self.root / "app" / "extra.py")
        fresh = Path(self.tmp.name) / "fresh.db"
        file_catalog.forget()
        code_index.rebuild_index(self.root, fresh)
        self.assertEqual(dump(self.db), dump(fresh))

    def test_unchanged_bytes_are_not_parsed_again(self) -> None:
        path = self.root / "app" / "units.py"
        os.utime(path, None)  # a new mtime, same content
        later = path.stat().st_mtime_ns + 5_000_000_000
        os.utime(path, ns=(later, later))
        stats = code_index.refresh_index(self.root, self.db)
        self.assertEqual(stats.changed, 0)

    def test_imports_resolve_and_follow_added_files(self) -> None:
        conn = store.connect(self.db)
        try:
            row = conn.execute("SELECT dst_path FROM edges WHERE src_path = 'app/panel.py' AND dst_name = 'app.units.to_display'").fetchone()
            self.assertEqual(row[0], "app/units.py")
            row = conn.execute("SELECT dst_path FROM edges WHERE src_path LIKE '%HvacService.java' AND kind = 'imports'").fetchone()
            self.assertEqual(row[0], "svc/src/com/x/util/Units.java")
        finally:
            conn.close()


class ToolTests(ProjectCase):
    def test_find_symbol_and_refs(self) -> None:
        text = self.tool("code_nav", action="find_symbol", name="to_display")
        self.assertIn("app/units.py:1-4 function to_display", text)
        refs = self.tool("code_nav", action="refs", name="to_display")
        self.assertIn("app/panel.py: 2, 12", refs)
        self.assertIn("app/units.py: 1", refs)

    def test_calls_and_imports(self) -> None:
        self.assertIn("app/panel.py:12 Panel.show", self.tool("code_nav", action="who_calls", name="to_display"))
        callees = self.tool("code_nav", action="callees", name="Panel.show")
        self.assertIn("to_display -> app/units.py:1", callees)
        self.assertIn("app.units.to_display -> app/units.py", self.tool("code_nav", action="imports_of", path="app/panel.py"))
        self.assertIn("app/main.py", self.tool("code_nav", action="imported_by", path="app/panel.py"))

    def test_module_of(self) -> None:
        text = self.tool("code_nav", action="module_of", path="svc/src/com/x/HvacService.java")
        self.assertIn("hvac-svc", text)
        self.assertIn("hvac-units", text)
        self.assertIn("module fan (cmake library)", self.tool("code_nav", action="module_of", path="native/fan.c"))

    def test_find_strings_from_a_log_line(self) -> None:
        text = self.tool("find_strings", text="E fanctl : fan level too high: 12")
        self.assertIn("native/fan.c:3 log tag=fanctl in fan_set", text)
        text = self.tool("find_strings", text="temperature sensor returned nothing for zone 3")
        self.assertTrue(text.startswith("app/panel.py:10 log in Panel.show"), text)

    def test_repo_map_is_ranked_within_budget_and_stable(self) -> None:
        first = self.tool("code_nav", action="repo_map", budget_tokens=100)
        self.assertLessEqual(len(first), 100 * 4 + 200)
        self.assertEqual(first, self.tool("code_nav", action="repo_map", budget_tokens=100))
        self.assertIn("app/units.py", first)

    def test_history_tools(self) -> None:
        text = self.tool("code_history", action="log", path="app/units.py")
        self.assertIn("2 commit(s)", text)
        self.assertIn("fix fahrenheit rounding", text)
        self.assertIn("app/panel.py  2x", self.tool("code_history", action="cochanged", path="app/units.py"))
        self.assertIn("fix fahrenheit rounding", self.tool("code_history", action="log", query="fahrenheit"))
        self.assertIn("first version", self.tool("code_history", action="blame", path="app/units.py", lines="1-2"))
        self.assertIn("clamp", self.tool("code_history", action="log", changed="def clamp"))
        self.assertIn("to_display (app/units.py:1-4)", self.tool("code_history", action="log", name="to_display"))

    def test_history_is_incremental(self) -> None:
        from critique_bot.indexing import history

        conn = store.connect(self.db)
        try:
            self.assertEqual(history.update(conn, self.root), 2)
            (self.root / "app" / "main.py").write_text("x = 1\n")
            git(self.root, "commit", "-q", "-am", "third")
            self.assertEqual(history.update(conn, self.root), 1)
            self.assertEqual(history.update(conn, self.root), 0)
        finally:
            conn.close()

    def test_locate_fuses_log_and_names(self) -> None:
        text = self.tool("locate", description="The panel shows an empty value; the log says "
                                               "\"temperature sensor returned nothing for zone 2\"")
        first = text.splitlines()[1]
        self.assertIn("app/panel.py", first)
        self.assertIn("strings", first)
        trace = self.tool("locate", description="crash", evidence='Traceback (most recent call last):\n'
                                                                  '  File "app/units.py", line 3, in to_display\n')
        self.assertIn("app/units.py:3 to_display", trace)

    def test_locate_expands_abbreviations(self) -> None:
        text = self.tool("locate", description="tmp converts wrong in the disp")
        self.assertIn("expanded terms", text)

    def test_coverage_locate_ranks_the_fault(self) -> None:
        import sys

        py = sys.executable
        failing = f"{py} -m unittest tests.test_panel.PanelTest.test_fahrenheit"
        passing = [f"{py} -m unittest tests.test_panel.PanelTest.test_celsius"]
        text = self.tool("coverage_locate", command=failing, passing=passing)
        ranked = [line for line in text.splitlines() if line.startswith("  1.00")]
        self.assertTrue(ranked, text)
        self.assertIn("app/units.py", ranked[0])

    def test_old_tool_names_run_with_their_action(self) -> None:
        from critique_bot import agent_tools
        from critique_bot.agent_tools import ToolContext

        ctx = ToolContext(workspace=self.root, index_path=self.db)
        self.assertEqual(agent_tools.canonical_tool("who_calls"), "code_nav")
        self.assertEqual(agent_tools.canonical_tool("cochanged"), "code_history")
        result = agent_tools.execute("find_symbol", {"name": "to_display"}, ctx)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tool"], "code_nav")
        self.assertIn("app/units.py:1-4 function to_display", result["output"])
        result = agent_tools.execute("git_history", {"path": "app/units.py"}, ctx)
        self.assertIn("fix fahrenheit rounding", result["output"])
        self.assertEqual(agent_tools.permission_for("who_calls", {"symbol": "x"}, ctx).kind, "read")
        self.assertNotIn("find_symbol", agent_tools.ALLOWED_TOOLS)

    def test_an_action_is_required(self) -> None:
        from critique_bot.indexing import tools

        ok, text = tools.run("code_nav", {"name": "to_display"}, self.ctx)
        self.assertFalse(ok)
        self.assertIn("find_symbol, refs, who_calls", text)
        ok, text = tools.run("code_nav", {"action": "imports_of", "path": "/etc/passwd"}, self.ctx)
        self.assertFalse(ok)
        self.assertIn("outside the workspace", text)

    def test_partial_results_are_labelled(self) -> None:
        from critique_bot.indexing import engine, tools

        missing = Path(self.tmp.name) / "other" / "index.db"
        release = __import__("threading").Event()
        original = engine._build_main

        def slow(workspace, index_path):
            release.wait(10)
            original(workspace, index_path)

        engine._build_main = slow
        try:
            from types import SimpleNamespace

            ctx = SimpleNamespace(workspace=self.root, index_path=missing, max_chars=4000, state=None)
            ok, text = tools.run("code_nav", {"action": "find_symbol", "name": "to_display"}, ctx)
            self.assertTrue(text.startswith("partial:"), text)
        finally:
            release.set()
            engine.wait_all(30)
            engine._build_main = original
        ok, text = tools.run("code_nav", {"action": "find_symbol", "name": "to_display"}, ctx)
        self.assertFalse(text.startswith("partial:"), text)
        self.assertIn("app/units.py", text)


class RepoTreeGroupTests(unittest.TestCase):
    """AOSP-style trees: a module group's database is created on first use, in the background."""

    def setUp(self) -> None:
        from critique_bot import file_catalog

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".repo").mkdir()
        (self.root / ".repo" / "project.list").write_text("packages/services/Car\nframeworks/base\n")
        for project, files in {
            "packages/services/Car": {"service/src/CarSvc.java": "class CarSvc { void boot() { Log.e(TAG, \"car boot failed\"); } }\n",
                                      "service/src/Q.java": "// throttles the compressor when the cabin overheats\nclass Q { void a() {} }\n",
                                      "Android.bp": 'android_app { name: "CarService", srcs: ["service/src/**/*.java"] }\n'},
            "frameworks/base": {"core/Foo.java": "class Foo { void bar() {} }\n"},
        }.items():
            folder = self.root / project
            folder.mkdir(parents=True)
            for rel, text in files.items():
                (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                (folder / rel).write_text(text)
            git(folder, "init", "-q")
            git(folder, "add", "-A")
            git(folder, "commit", "-q", "-m", "init")
        file_catalog.forget()
        self.index = self.root / ".crit" / "cache" / "index.db"
        self.index.parent.mkdir(parents=True)
        code_index.rebuild_index(self.root, self.index)

    def tearDown(self) -> None:
        from critique_bot import file_catalog
        from critique_bot.indexing import engine

        engine.wait_all(30)
        file_catalog.forget()
        self.tmp.cleanup()

    def test_group_database_on_first_use(self) -> None:
        from types import SimpleNamespace

        from critique_bot.indexing import engine, tools

        ctx = SimpleNamespace(workspace=self.root, index_path=self.index, max_chars=4000, state=None)
        ok, text = tools.run("find_strings", {"text": "car boot failed", "path": "packages/services/Car"}, ctx)
        engine.wait_all(30)
        ok, text = tools.run("find_strings", {"text": "car boot failed", "path": "packages/services/Car"}, ctx)
        self.assertIn("packages/services/Car/service/src/CarSvc.java:1", text)
        group_db = store.group_db_path(self.index, "packages/services/Car")
        self.assertTrue(group_db.is_file())
        self.assertFalse(store.group_db_path(self.index, "frameworks/base").exists())
        ok, text = tools.run("code_nav", {"action": "module_of", "path": "packages/services/Car/service/src/CarSvc.java"}, ctx)
        self.assertIn("CarService", text)
        if shutil.which("rg"):  # a module group keeps no text: comments are searched with one rg pass
            ok, text = tools.run("locate", {"description": "compressor overheats", "path": "packages/services/Car"}, ctx)
            self.assertIn("packages/services/Car/service/src/Q.java", text)
            self.assertIn("lexical", text)
        # without a path: the project database and every group built so far
        ok, text = tools.run("code_nav", {"action": "find_symbol", "name": "CarSvc"}, ctx)
        self.assertIn("CarSvc.java", text)


class ModuleAndCoverageTests(unittest.TestCase):
    def test_gradle_cmake_and_bazel_modules(self) -> None:
        from critique_bot.indexing import modules

        gradle = modules.parse_build_file(Path("."), "app/build.gradle.kts",
                                          'plugins { id("com.android.application") }\ndependencies { implementation(project(":core")) '
                                          'implementation("androidx.core:core-ktx:1.12.0") }\n')[0]
        self.assertEqual((gradle.name, gradle.type), (":app", "android_app"))
        self.assertEqual(gradle.deps, [":core", "androidx.core:core-ktx"])
        bazel = modules.parse_build_file(Path("."), "lib/BUILD",
                                         'cc_library(\n    name = "fan",\n    srcs = ["fan.c"],\n    deps = ["//log:log"],\n)\n')[0]
        self.assertEqual((bazel.name, bazel.type, bazel.srcs, bazel.deps), ("fan", "cc_library", ["lib/fan.c"], ["//log:log"]))
        cmake = modules.parse_build_file(Path("."), "CMakeLists.txt", "add_executable(app main.c)\ntarget_link_libraries(app PRIVATE fan)\n")[0]
        self.assertEqual((cmake.name, cmake.type, cmake.deps), ("app", "executable", ["fan"]))

    def test_coverage_reports(self) -> None:
        from critique_bot.indexing import coverage

        known = {"app/src/main/java/com/x/Fan.java", "native/fan.c"}
        jacoco = ('<report name="r"><package name="com/x"><sourcefile name="Fan.java">'
                  '<line nr="3" mi="0" ci="2"/><line nr="4" mi="1" ci="0"/></sourcefile></package></report>')
        lcov = "SF:/build/native/fan.c\nDA:2,1\nDA:3,0\nend_of_record\n"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "j.xml").write_text(jacoco)
            (Path(tmp) / "c.info").write_text(lcov)
            self.assertEqual(coverage.read_report(Path(tmp) / "j.xml", Path(tmp), known),
                             {"app/src/main/java/com/x/Fan.java": {3}})
            self.assertEqual(coverage.read_report(Path(tmp) / "c.info", Path(tmp), known), {"native/fan.c": {2}})
        scores = coverage.ochiai([{"a.py": {1, 2}}], [{"a.py": {1}}])
        self.assertAlmostEqual(scores[("a.py", 2)], 1.0)
        self.assertAlmostEqual(scores[("a.py", 1)], 0.7071, places=3)

    def test_glossary(self) -> None:
        from critique_bot.indexing import glossary

        self.assertEqual(glossary.split("HTTPServer2Go"), ["http", "server", "2", "go"])
        found = {term: why for term, _w, why in glossary.terms("tmpMgr fails")}
        self.assertIn("temperature", found)
        self.assertIn("manager", found)
        conn = store.connect(Path(":memory:"))
        store.migrate(conn)
        glossary.add_project_term(conn, "zone", "seat area", provenance="test")
        found = {term: why for term, _w, why in glossary.terms("zone heat", conn)}
        self.assertIn("seat", found)
        self.assertIn("project glossary", found["seat"])

    def test_stack_frames(self) -> None:
        from critique_bot.indexing import locate

        frames = locate.parse_frames(
            "\tat com.x.Fan.set(Fan.java:12)\n"
            '  File "app/u.py", line 3, in conv\n'
            "#01 pc 0001a2b4  /system/lib64/libfan.so (_ZN3fan7Control5applyEi+20)\n"
            "panic at src/parse.rs:14\n"
        )
        self.assertEqual([(f.file, f.line, f.symbol) for f in frames[:3]],
                         [("Fan.java", 12, "Fan.set"), ("app/u.py", 3, "conv"), ("", 0, "fan.Control.apply")])
        self.assertEqual((frames[3].file, frames[3].line), ("src/parse.rs", 14))


if __name__ == "__main__":
    unittest.main()
