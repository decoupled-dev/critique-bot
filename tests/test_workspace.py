from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from critique_bot.patch import InputLimits, changed_file_paths
from critique_bot.workspace import (
    EmptyDiff,
    WorkspaceError,
    ci_review_refs,
    load_changed_files,
    prepare_workspace_patch,
    should_prepare_workspace,
)


class ShouldPrepareTests(unittest.TestCase):
    def test_skips_when_patch_file_given(self) -> None:
        with patch.dict(os.environ, {"GITLAB_CI": "true"}, clear=False):
            self.assertFalse(
                should_prepare_workspace(patch_file="d.patch", extra_files=[])
            )

    def test_skips_when_extra_files_given(self) -> None:
        with patch.dict(os.environ, {"GITLAB_CI": "true"}, clear=False):
            self.assertFalse(
                should_prepare_workspace(patch_file=None, extra_files=["a.py"])
            )

    def test_true_for_gitlab_ci(self) -> None:
        env = {
            "GITLAB_CI": "true",
            "CI_MERGE_REQUEST_DIFF_BASE_SHA": "",
            "CI_COMMIT_SHA": "",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertTrue(
                should_prepare_workspace(patch_file=None, extra_files=[])
            )

    def test_true_for_mr_shas_without_gitlab_ci_flag(self) -> None:
        env = {
            "GITLAB_CI": "",
            "CI_MERGE_REQUEST_DIFF_BASE_SHA": "aaa",
            "CI_COMMIT_SHA": "bbb",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertTrue(
                should_prepare_workspace(patch_file=None, extra_files=[])
            )
            refs = ci_review_refs()
            self.assertEqual(refs["base"], "aaa")
            self.assertEqual(refs["head"], "bbb")


class PrepareWorkspacePatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_writes_three_dot_diff_and_fetches_target(self) -> None:
        calls: list[list[str]] = []
        diff = "diff --git a/a b/a\n+hi\n"

        def fake_run(cmd, capture_output=True, check=False):
            calls.append(list(cmd))
            action = cmd[3]
            if action == "fetch":
                return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)
            if action == "diff":
                return SimpleNamespace(stdout=diff.encode(), stderr=b"", returncode=0)
            return SimpleNamespace(stdout=b"", stderr=b"nope", returncode=1)

        out = self.folder / "diff.patch"
        env = {
            "CI_MERGE_REQUEST_DIFF_BASE_SHA": "base123",
            "CI_COMMIT_SHA": "head456",
            "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main",
        }
        with patch.dict(os.environ, env, clear=False):
            text = prepare_workspace_patch(self.folder, out, git_run=fake_run)
        self.assertEqual(text, diff)
        self.assertEqual(out.read_text(encoding="utf-8"), diff)
        self.assertEqual(calls[0][3:], ["fetch", "--depth=50", "origin", "main"])
        self.assertEqual(calls[1][3:], ["diff", "base123...head456"])

    def test_empty_diff_raises(self) -> None:
        def fake_run(cmd, capture_output=True, check=False):
            return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

        out = self.folder / "diff.patch"
        env = {
            "GITLAB_CI": "true",
            "CI_MERGE_REQUEST_DIFF_BASE_SHA": "",
            "CI_COMMIT_SHA": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(EmptyDiff):
                prepare_workspace_patch(self.folder, out, git_run=fake_run)
        self.assertTrue(out.is_file())

    def test_git_failure_raises(self) -> None:
        def fake_run(cmd, capture_output=True, check=False):
            return SimpleNamespace(stdout=b"", stderr=b"bad", returncode=128)

        with self.assertRaises(WorkspaceError):
            prepare_workspace_patch(
                self.folder, self.folder / "d.patch", git_run=fake_run
            )


class LoadChangedFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.limits = InputLimits(max_file_chars=10_000, max_files=80)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reads_head_file_skips_missing(self) -> None:
        (self.folder / "Foo.java").write_text("class Foo {}\n", encoding="utf-8")
        patch = (
            "diff --git a/Foo.java b/Foo.java\n"
            "--- a/Foo.java\n"
            "+++ b/Foo.java\n"
            "@@ -1 +1,2 @@\n"
            " class Foo {}\n"
            "+// x\n"
            "diff --git a/gone.py b/gone.py\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-old\n"
        )
        self.assertEqual(changed_file_paths(patch), ["Foo.java"])
        loaded = load_changed_files(self.folder, patch, self.limits)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].name, "Foo.java")
        self.assertIn("class Foo", loaded[0].text)

    def test_skips_markdown_keeps_python(self) -> None:
        (self.folder / "DEPLOY.md").write_text("# deploy\n" + ("x" * 100), encoding="utf-8")
        (self.folder / "cli.py").write_text("print(1)\n", encoding="utf-8")
        patch = (
            "diff --git a/DEPLOY.md b/DEPLOY.md\n"
            "--- a/DEPLOY.md\n"
            "+++ b/DEPLOY.md\n"
            "@@ -1 +1,2 @@\n"
            " # deploy\n"
            "+x\n"
            "diff --git a/cli.py b/cli.py\n"
            "--- a/cli.py\n"
            "+++ b/cli.py\n"
            "@@ -1 +1,2 @@\n"
            " print(1)\n"
            "+# y\n"
        )
        loaded = load_changed_files(self.folder, patch, self.limits)
        self.assertEqual([item.name for item in loaded], ["cli.py"])

    def test_loads_bodies_when_the_mr_has_many_files(self) -> None:
        chunks = []
        for i in range(10):
            name = f"f{i}.py"
            (self.folder / name).write_text("x = 1\n", encoding="utf-8")
            chunks.append(
                f"diff --git a/{name} b/{name}\n"
                f"--- a/{name}\n"
                f"+++ b/{name}\n"
                "@@ -1 +1,2 @@\n"
                " x = 1\n"
                "+y\n"
            )
        loaded = load_changed_files(self.folder, "".join(chunks), self.limits)
        self.assertEqual([item.name for item in loaded], [f"f{i}.py" for i in range(10)])
