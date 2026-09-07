from __future__ import annotations

import os
import shutil
import subprocess
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

    def test_fetches_missing_base_sha_and_uses_two_dot_diff(self) -> None:
        calls: list[list[str]] = []
        diff = "diff --git a/a b/a\n+hi\n"
        known = {"head456"}

        def fake_run(cmd, capture_output=True, check=False):
            calls.append(list(cmd))
            action = cmd[3]
            if action == "cat-file":
                sha = cmd[5].split("^", 1)[0]
                code = 0 if sha in known else 1
                return SimpleNamespace(stdout=b"", stderr=b"", returncode=code)
            if action == "fetch":
                if "base123" in cmd:
                    known.add("base123")
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
        self.assertEqual(calls[0][3:], ["cat-file", "-e", "base123^{commit}"])
        self.assertEqual(calls[1][3:], ["cat-file", "-e", "head456^{commit}"])
        self.assertEqual(calls[2][3:], ["fetch", "--depth=1", "origin", "base123"])
        self.assertEqual(calls[-1][3:], ["diff", "base123", "head456"])
        self.assertFalse(any("..." in " ".join(c) for c in calls))

    def test_deepens_target_when_sha_fetch_misses(self) -> None:
        calls: list[list[str]] = []
        known = {"head456"}
        diff = "diff --git a/a b/a\n+hi\n"

        def fake_run(cmd, capture_output=True, check=False):
            calls.append(list(cmd))
            action = cmd[3]
            if action == "cat-file":
                sha = cmd[5].split("^", 1)[0]
                return SimpleNamespace(
                    stdout=b"", stderr=b"", returncode=0 if sha in known else 1
                )
            if action == "fetch":
                if "--depth=50" in cmd and "main" in cmd:
                    known.add("base123")
                return SimpleNamespace(stdout=b"", stderr=b"denied", returncode=1)
            if action == "diff":
                return SimpleNamespace(stdout=diff.encode(), stderr=b"", returncode=0)
            return SimpleNamespace(stdout=b"", stderr=b"nope", returncode=1)

        env = {
            "CI_MERGE_REQUEST_DIFF_BASE_SHA": "base123",
            "CI_COMMIT_SHA": "head456",
            "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main",
        }
        with patch.dict(os.environ, env, clear=False):
            prepare_workspace_patch(
                self.folder, self.folder / "d.patch", git_run=fake_run
            )
        fetch_cmds = [c[3:] for c in calls if c[3] == "fetch"]
        self.assertEqual(fetch_cmds[0], ["fetch", "--depth=1", "origin", "base123"])
        self.assertEqual(fetch_cmds[1], ["fetch", "--depth=50", "origin", "main"])
        self.assertEqual(calls[-1][3:], ["diff", "base123", "head456"])

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

    def test_missing_commits_after_fetch_raises(self) -> None:
        def fake_run(cmd, capture_output=True, check=False):
            return SimpleNamespace(stdout=b"", stderr=b"missing", returncode=1)

        env = {
            "CI_MERGE_REQUEST_DIFF_BASE_SHA": "base123",
            "CI_COMMIT_SHA": "head456",
            "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(WorkspaceError) as ctx:
                prepare_workspace_patch(
                    self.folder, self.folder / "d.patch", git_run=fake_run
                )
        self.assertIn("not in the checkout", str(ctx.exception))


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


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "critique-bot")
    env.setdefault("GIT_AUTHOR_EMAIL", "bot@example.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "critique-bot")
    env.setdefault("GIT_COMMITTER_EMAIL", "bot@example.invalid")
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        check=check,
        env=env,
        text=True,
    )


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class ShallowCloneMergeBaseTests(unittest.TestCase):
    """GitLab can merge the MR while a shallow job clone has no merge-base."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.full = self.root / "full"
        self.clone = self.root / "clone"
        self.full.mkdir()
        _git(self.full, "init")
        _git(self.full, "config", "user.email", "bot@example.invalid")
        _git(self.full, "config", "user.name", "critique-bot")
        for i in range(5):
            (self.full / "base.txt").write_text(f"base {i}\n", encoding="utf-8")
            _git(self.full, "add", "base.txt")
            _git(self.full, "commit", "-m", f"base-{i}")
        _git(self.full, "branch", "-M", "main")
        self.base_sha = _git(self.full, "rev-parse", "HEAD").stdout.strip()
        _git(self.full, "checkout", "-b", "feature")
        (self.full / "feat.py").write_text("print('feat')\n", encoding="utf-8")
        _git(self.full, "add", "feat.py")
        _git(self.full, "commit", "-m", "feat")
        self.head_sha = _git(self.full, "rev-parse", "HEAD").stdout.strip()
        _git(self.full, "checkout", "main")
        for i in range(60):
            (self.full / "main.txt").write_text(f"main {i}\n", encoding="utf-8")
            _git(self.full, "add", "main.txt")
            _git(self.full, "commit", "-m", f"main-{i}")
        subprocess.run(
            [
                "git",
                "clone",
                "--depth=1",
                "--branch",
                "feature",
                f"file://{self.full}",
                str(self.clone),
            ],
            check=True,
            capture_output=True,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_three_dot_diff_fails_but_submit_patch_succeeds(self) -> None:
        three = _git(
            self.clone,
            "diff",
            f"{self.base_sha}...{self.head_sha}",
            check=False,
        )
        self.assertNotEqual(three.returncode, 0)
        self.assertRegex(
            three.stderr,
            r"merge.base|bad revision|ambiguous|Invalid symmetric difference",
        )

        fetched = _git(
            self.clone, "fetch", "--depth=1", "origin", self.base_sha, check=False
        )
        self.assertEqual(fetched.returncode, 0)
        still_three = _git(
            self.clone,
            "diff",
            f"{self.base_sha}...{self.head_sha}",
            check=False,
        )
        self.assertNotEqual(still_three.returncode, 0, still_three.stderr)

        env = {
            "CI_MERGE_REQUEST_DIFF_BASE_SHA": self.base_sha,
            "CI_COMMIT_SHA": self.head_sha,
            "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main",
        }
        out = self.clone / "diff.patch"
        with patch.dict(os.environ, env, clear=False):
            text = prepare_workspace_patch(self.clone, out)
        self.assertIn("feat.py", text)
        self.assertTrue(out.is_file())
        self.assertIn("feat.py", out.read_text(encoding="utf-8"))
