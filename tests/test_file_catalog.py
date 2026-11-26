"""The cached file catalog: repo trees, plain checkouts, and walked folders."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from critique_bot import file_catalog
from critique_bot.file_catalog import FileCatalog, repo_projects


def _git(folder: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(folder), *args],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def _project(root: Path, rel: str, files: dict[str, str]) -> None:
    folder = root / rel
    folder.mkdir(parents=True, exist_ok=True)
    _git(folder, "init", "-q")
    for name, text in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(folder, "add", "-A")
    _git(folder, "commit", "-q", "-m", "init")


class RepoTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()  # the long name on Windows (not RUNNER~1)
        (self.root / ".repo").mkdir()
        (self.root / ".repo" / "project.list").write_text("build/soong\nframeworks/base\nprebuilts/clang\n")
        _project(self.root, "build/soong", {"Android.bp": "x", "ui/build.go": "y"})
        _project(self.root, "frameworks/base", {"core/java/A.java": "a", "build/B.java": "b"})
        _project(self.root, "prebuilts/clang", {"bin/clang": "c"})
        (self.root / "out").mkdir()
        (self.root / "out" / "junk.o").write_text("o")
        self.catalog = FileCatalog(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_lists_projects_and_keeps_build_folders(self) -> None:
        self.assertEqual(self.catalog.kind, "repo")
        files = self.catalog.files()
        self.assertIn("build/soong/Android.bp", files)
        self.assertIn("frameworks/base/build/B.java", files)  # "build" is not skipped by name
        self.assertNotIn("prebuilts/clang/bin/clang", files)  # data project left out
        self.assertFalse(any(item.startswith("out/") for item in files))
        self.assertEqual(files, sorted(files))

    def test_prefix_and_project_of(self) -> None:
        self.assertEqual(self.catalog.files("frameworks/base/core"), ["frameworks/base/core/java/A.java"])
        self.assertEqual(self.catalog.files("build"), ["build/soong/Android.bp", "build/soong/ui/build.go"])
        self.assertEqual(self.catalog.project_of("frameworks/base/core/java/A.java"), "frameworks/base")
        self.assertIsNone(self.catalog.project_of("out/junk.o"))

    def test_only_changed_projects_are_listed_again(self) -> None:
        self.catalog.files()
        calls: list[str] = []
        real = file_catalog._ls_files

        def spy(folder: Path):
            calls.append(Path(folder).relative_to(self.root).as_posix())
            return real(folder)

        (self.root / "frameworks/base/core/java/New.java").write_text("n")
        _git(self.root / "frameworks/base", "add", "-A")
        _git(self.root / "frameworks/base", "commit", "-q", "-m", "more")
        with mock.patch.object(file_catalog, "_ls_files", spy), mock.patch.object(file_catalog, "CHECK_EVERY", 0.0):
            files = self.catalog.files()
        self.assertEqual(calls, ["frameworks/base"])
        self.assertIn("frameworks/base/core/java/New.java", files)

    def test_note_write_and_delete_update_without_listing(self) -> None:
        self.catalog.files()
        (self.root / "build/soong/new.go").write_text("z")
        self.catalog.note_write("build/soong/new.go")
        self.assertIn("build/soong/new.go", self.catalog.files("build/soong"))
        self.catalog.note_delete("build/soong/Android.bp")
        self.assertNotIn("build/soong/Android.bp", self.catalog.files())

    def test_index_covers_only_projects_in_scope(self) -> None:
        from critique_bot import code_index

        (self.root / "frameworks/base/core/java/A.java").write_text("class Alpha { void run() {} }\n")
        (self.root / "build/soong/ui/build.go").write_text("package ui\nfunc Beta() {}\n")
        file_catalog.forget(self.root)
        index = self.root / ".crit" / "index.sqlite"
        stats = code_index.rebuild_index(self.root, index)
        self.assertEqual(stats.files, 0)  # nothing until a project is used
        code_index.add_scope(self.root, index, "frameworks/base")
        self.assertEqual(code_index.indexed_projects(index), ["frameworks/base"])
        self.assertTrue(code_index.find_symbols(index, "Alpha", limit=5))
        self.assertFalse(code_index.find_symbols(index, "Beta", limit=5))
        again = code_index.refresh_index(self.root, index)
        self.assertGreater(again.files, 0)
        self.assertTrue(code_index.find_symbols(index, "Alpha", limit=5))
        self.assertIsNone(code_index.add_scope(self.root, index, "frameworks/base"))

    def test_manifest_fallback(self) -> None:
        (self.root / ".repo" / "project.list").unlink()
        manifests = self.root / ".repo" / "manifests"
        manifests.mkdir()
        (manifests / "default.xml").write_text(
            '<manifest><project name="platform/build/soong" path="build/soong"/>'
            '<project name="platform/frameworks/base" path="frameworks/base"/>'
            '<project name="gone" path="gone"/><remove-project name="gone"/></manifest>'
        )
        (self.root / ".repo" / "manifest.xml").write_text('<manifest><include name="default.xml"/></manifest>')
        self.assertEqual(repo_projects(self.root), ["build/soong", "frameworks/base"])


class CheckoutTests(unittest.TestCase):
    def test_small_checkout_sees_new_untracked_files_at_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _project(root, "", {"a.py": "a"})
            catalog = FileCatalog(root)
            self.assertEqual(catalog.files(), ["a.py"])
            (root / "b.py").write_text("b")
            self.assertEqual(catalog.files(), ["a.py", "b.py"])

    def test_walked_folder_and_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "a.txt").write_text("a")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "x.js").write_text("x")
            catalog = FileCatalog(root)
            self.assertEqual(catalog.kind, "walk")
            self.assertEqual(catalog.files(), ["src/a.txt"])
            self.assertEqual(catalog.files("src"), ["src/a.txt"])


@unittest.skipUnless(os.environ.get("CRIT_BENCH_AOSP"), "set CRIT_BENCH_AOSP=<repo tree> to time a real tree")
class AospBenchmark(unittest.TestCase):
    def test_warm_listing_is_fast(self) -> None:
        root = Path(os.environ["CRIT_BENCH_AOSP"])
        catalog = FileCatalog(root)
        start = time.monotonic()
        count = len(catalog.files())
        cold = time.monotonic() - start
        time.sleep(file_catalog.CHECK_EVERY + 0.1)
        start = time.monotonic()
        catalog.files("frameworks/base")
        warm = time.monotonic() - start
        print(f"\n{count} files; cold {cold:.1f}s; warm check {warm:.2f}s")
        self.assertLess(warm, 2.0)


if __name__ == "__main__":
    unittest.main()
