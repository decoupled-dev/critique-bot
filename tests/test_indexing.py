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


if __name__ == "__main__":
    unittest.main()
