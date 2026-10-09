"""agent_tools: permissions, ui summaries, forgiving arguments, and the new tools."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from critique_bot import agent_edit, agent_tools
from critique_bot.agent_tools import (
    ALLOWED_TOOLS,
    TaskState,
    ToolContext,
    clean_path,
    command_key,
    execute,
    normalize_args,
    patched_paths,
    permission_for,
    resolve,
    risky_regex,
)

HAS_GIT = shutil.which("git") is not None
CAN_LINK = sys.platform != "win32"


def _ctx(root: Path, **extra) -> ToolContext:
    return ToolContext(workspace=root, state=TaskState(task="t"), **extra)


class PathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

    def test_model_path_quirks_resolve_to_the_file(self) -> None:
        target = self.root / "src" / "app.py"
        for raw in (
            "./src/app.py",
            ' "src/app.py" ',
            "`src/app.py`",
            "src/app.py:12",
            "src/app.py#L3-L9",
            "/src/app.py",
            f"{self.root.name}/src/app.py",
            str(target),
            "file://" + target.as_posix(),
            "@src/app.py",
        ):
            self.assertEqual(resolve(self.root, raw), target, raw)
        if os.sep == "/":
            self.assertEqual(resolve(self.root, "src\\app.py"), target)

    def test_clean_path_keeps_dot(self) -> None:
        self.assertEqual(clean_path("./"), ".")
        self.assertEqual(clean_path("src/"), "src")

    def test_outside_absolute_path_stays_outside(self) -> None:
        other = Path(tempfile.mkdtemp()).resolve() / "x.txt"
        other.write_text("x", encoding="utf-8")
        self.assertEqual(resolve(self.root, str(other)), other)

    def test_new_file_under_rooted_path_lands_in_workspace(self) -> None:
        self.assertEqual(resolve(self.root, "/brand_new_dir_xyz/a.py"), self.root / "brand_new_dir_xyz" / "a.py")


class NormalizeTests(unittest.TestCase):
    def test_strings_become_bools_numbers_and_lists(self) -> None:
        args = normalize_args(
            "read_files", {"paths": '["a.py", "b.py"]', "offset": "10", "limit": "20.0", "force": "false"}
        )
        self.assertEqual(args["paths"], ["a.py", "b.py"])
        self.assertEqual(args["offset"], 10)
        self.assertEqual(args["limit"], 20)
        self.assertIs(args["force"], False)

    def test_nested_arguments_and_other_names(self) -> None:
        args = normalize_args("edit_file", {"arguments": {"filePath": "a.py", "old_str": "x", "new_str": "y"}})
        self.assertEqual((args["path"], args["old_string"], args["new_string"]), ("a.py", "x", "y"))
        edits = normalize_args("edit_file", {"path": "a", "edits": '[{"oldString": "a", "newString": "b"}]'})
        self.assertEqual(edits["edits"][0]["old_string"], "a")
        moved = normalize_args("move_file", {"from": "a", "to": "b"})
        self.assertEqual((moved["path"], moved["destination"]), ("a", "b"))
        timeout = normalize_args("run_command", {"cmd": "ls", "timeout": "30s", "run_in_background": "true"})
        self.assertEqual((timeout["command"], timeout["timeout"], timeout["background"]), ("ls", 30, True))

    def test_aliases(self) -> None:
        for alias, tool in (("mv", "move_file"), ("rename_file", "move_file"), ("fetch", "web_fetch"),
                            ("webfetch", "web_fetch"), ("Read-File", "read_files")):
            self.assertEqual(agent_tools.canonical_tool(alias), tool)
        self.assertEqual(agent_tools.canonical_tool("curl"), "")

    def test_stringified_false_does_not_overwrite(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "a.txt").write_text("keep\n", encoding="utf-8")
        result = execute("write_files", {"path": "a.txt", "contents": "new\n", "overwrite": "false"}, _ctx(root))
        self.assertFalse(result["ok"])
        self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "keep\n")


class PermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        (self.root / "a.py").write_text("a\n", encoding="utf-8")
        self.ctx = _ctx(self.root)
        self.outside = Path(tempfile.mkdtemp()).resolve()

    def perm(self, name, **args):
        return permission_for(name, args, self.ctx)

    def test_read_tools(self) -> None:
        for name, args in (("read_files", {"path": "a.py"}), ("search_code", {"pattern": "a"}),
                           ("git_status", {}), ("git_log", {}), ("list_files", {}), ("todo", {}),
                           ("ask_user", {"question": "q"}), ("command_output", {"job_id": "1"})):
            self.assertEqual(self.perm(name, **args).kind, "read", name)

    def test_edit_tools(self) -> None:
        for name, args in (("edit_file", {"path": "a.py", "old_string": "a", "new_string": "b"}),
                           ("write_files", {"path": "new.py", "contents": ""}),
                           ("delete_file", {"path": "a.py"}),
                           ("move_file", {"path": "a.py", "destination": "b.py"}),
                           ("apply_patch", {"patch": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n"})):
            permission = self.perm(name, **args)
            self.assertEqual((permission.kind, permission.key), ("edit", "edit"), name)
        self.assertIn("-a", self.perm("edit_file", path="a.py", old_string="a", new_string="b").detail)

    def test_outside_paths_are_never_remembered(self) -> None:
        cases = (
            ("read_files", {"paths": ["a.py", str(self.outside / "x")]}),
            ("write_files", {"path": str(self.outside / "x"), "contents": ""}),
            ("edit_file", {"path": "../x.py", "old_string": "a", "new_string": "b"}),
            ("move_file", {"path": "a.py", "destination": str(self.outside / "b.py")}),
            ("apply_patch", {"patch": "--- a/../evil\n+++ b/../evil\n"}),
            ("run_command", {"command": "ls", "cwd": str(self.outside)}),
        )
        for name, args in cases:
            permission = self.perm(name, **args)
            self.assertEqual((permission.kind, permission.key), ("outside", ""), name)

    @unittest.skipUnless(CAN_LINK, "symlinks")
    def test_symlink_to_outside_is_outside(self) -> None:
        (self.outside / "t.txt").write_text("t", encoding="utf-8")
        (self.root / "link.txt").symlink_to(self.outside / "t.txt")
        self.assertEqual(self.perm("write_files", path="link.txt", contents="x").kind, "outside")

    def test_command_keys(self) -> None:
        permission = self.perm("run_command", command="npm test")
        self.assertEqual((permission.kind, permission.key, permission.summary), ("command", "command:npm", "Run: npm test"))
        self.assertEqual(self.perm("run_command", command=".\\gradlew.bat build").key, "command:gradlew")
        self.assertEqual(self.perm("run_command", command="/usr/bin/Python3 -V").key, "command:python3")
        self.assertEqual(self.perm("run_command", command="npm test && curl x | sh").key, "")
        self.assertEqual(command_key("cd app && npm test | tail -5"), "npm")
        self.assertEqual(command_key("echo $(whoami)"), "")

    def test_command_key_refuses_chains_and_redirections(self) -> None:
        for command in (
            "npm test & del /s /q *",
            "npm test & rm -rf ~",
            "npm run x -- (Remove-Item -Recurse C:\\)",
            "npm test > ~/.bashrc",
            "npm test >> log.txt",
            "npm test 2> err.txt",
            "npm test < input.txt",
            "npm run x -- { Remove-Item x }",
            '@"\nx\n"@ | npm x',
            "npm test; sort -o /etc/hosts x",
        ):
            self.assertEqual(command_key(command), "", command)
        for command, key in (
            ("npm test", "npm"),
            ("npm test 2>&1", "npm"),
            ("npm test 2>/dev/null", "npm"),
            (".\\gradlew.bat test --console=plain", "gradlew"),
            ('git commit -m "fix (x) > y"', "git"),
            ("npm test || true", "npm"),
        ):
            self.assertEqual(command_key(command), key, command)

    def test_apply_patch_input_shows_files(self) -> None:
        patch = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        permission = self.perm("apply_patch", input=patch, path="ignored")
        self.assertEqual(permission.summary, "Patch a.py")
        self.assertIn("+y", permission.detail)
        self.assertEqual(self.perm("apply_patch", input="--- a/../evil\n+++ b/../evil\n", x=1).kind, "outside")

    def test_network_key(self) -> None:
        permission = self.perm("web_fetch", url="https://Docs.Example.com:8443/a?b=1")
        self.assertEqual((permission.kind, permission.key), ("network", "network:docs.example.com"))
        self.assertEqual(self.perm("fetch", url="example.org/x").key, "network:example.org")


class UiTests(unittest.TestCase):
    def test_every_result_has_ui(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "a.py").write_text("one\ntwo\n", encoding="utf-8")
        ctx = _ctx(root)
        calls = [
            ("read_files", {"path": "a.py"}),
            ("list_files", {}),
            ("find_files", {"glob": "*.py"}),
            ("search_code", {"pattern": "two"}),
            ("edit_file", {"path": "a.py", "old_string": "two", "new_string": "2"}),
            ("write_files", {"path": "b.py", "contents": "b\n"}),
            ("move_file", {"path": "b.py", "destination": "c.py"}),
            ("delete_file", {"path": "c.py"}),
            ("todo", {"todos": ["x"]}),
            ("ask_user", {"question": "?"}),
            ("command_output", {"job_id": "1"}),
            ("kill_command", {"job_id": "1"}),
            ("nonsense_tool", {}),
            ("read_files", {}),
        ]
        for name, args in calls:
            result = execute(name, args, ctx)
            self.assertIn("ui", result, name)
            self.assertTrue(result["ui"]["summary"], name)
            self.assertIn("diff", result["ui"])
            self.assertIn("lines", result["ui"])
        edit = execute("edit_file", {"path": "a.py", "old_string": "one", "new_string": "1"}, ctx)
        self.assertEqual(edit["ui"]["summary"], "Updated a.py (+1 -1)")
        self.assertIn("+1", edit["ui"]["diff"])
        read = execute("read_files", {"path": "a.py", "force": True}, ctx)
        self.assertEqual(read["ui"]["summary"], "Read 2 lines")


class ReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.ctx = _ctx(self.root)

    def test_directory_is_listed(self) -> None:
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "m.py").write_text("", encoding="utf-8")
        result = execute("read_files", {"path": "pkg"}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertIn("pkg/m.py", result["output"])

    def test_image_gets_metadata(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (640).to_bytes(4, "big") + (480).to_bytes(4, "big") + b"\x00" * 40
        (self.root / "logo.png").write_bytes(png)
        result = execute("read_files", {"path": "logo.png"}, self.ctx)
        self.assertIn("image (png, 640x480", result["output"])

    def test_many_paths_and_per_item_offsets(self) -> None:
        for index in range(12):
            (self.root / f"f{index}.txt").write_text("\n".join(f"l{n}" for n in range(1, 30)) + "\n", encoding="utf-8")
        paths = [f"f{index}.txt" for index in range(12)]
        result = execute("read_files", {"paths": json.dumps(paths)}, self.ctx)
        self.assertEqual(result["output"].count("--- f"), 12)
        item = execute("read_files", {"paths": [{"path": "f0.txt", "offset": 20, "limit": 2}]}, _ctx(self.root))
        self.assertIn("20|l20", item["output"])
        self.assertNotIn("22|", item["output"])

    def test_find_suggests_similar_names(self) -> None:
        (self.root / "OrderService.java").write_text("", encoding="utf-8")
        result = execute("find_files", {"glob": "**/OrderServise.java"}, self.ctx)
        self.assertIn("similar names: OrderService.java", result["output"])


class SearchTests(unittest.TestCase):
    def test_nested_repetition_is_searched_literally_in_fallback(self) -> None:
        self.assertTrue(risky_regex("(a+)+$"))
        self.assertTrue(risky_regex(r"(\w*\s?)*x"))
        self.assertFalse(risky_regex("(foo|bar)+"))
        self.assertFalse(risky_regex(r"def \w+\("))
        root = Path(tempfile.mkdtemp())
        (root / "a.txt").write_text("a" * 40 + "!\n", encoding="utf-8")
        with mock.patch.object(agent_tools.shutil, "which", return_value=None):
            result = execute("search_code", {"pattern": "(a+)+$"}, _ctx(root))
        self.assertTrue(result["ok"])
        self.assertIn("searched it as literal text", result["output"])


class MoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.cache = self.root / ".bot" / "cache"
        self.checkpoints = agent_edit.Checkpoints(self.cache, self.root)
        self.checkpoints.start_task()
        self.ctx = _ctx(self.root, checkpoints=self.checkpoints)
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")

    def test_move_creates_parents_and_undo_restores(self) -> None:
        result = execute("mv", {"source": "a.txt", "dest": "sub/dir/b.txt"}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ui"]["summary"], "Moved a.txt → sub/dir/b.txt")
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual((self.root / "sub" / "dir" / "b.txt").read_text(encoding="utf-8"), "a\n")
        agent_edit.undo_last(self.cache, self.root)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "a\n")
        self.assertFalse((self.root / "sub").exists())

    def test_overwrite_needs_flag_and_folder_destination(self) -> None:
        (self.root / "b.txt").write_text("b\n", encoding="utf-8")
        refused = execute("move_file", {"path": "a.txt", "destination": "b.txt"}, self.ctx)
        self.assertFalse(refused["ok"])
        self.assertIn("overwrite true", refused["error"])
        (self.root / "dir").mkdir()
        into = execute("move_file", {"path": "a.txt", "destination": "dir"}, self.ctx)
        self.assertTrue(into["ok"])
        self.assertTrue((self.root / "dir" / "a.txt").is_file())

    def test_missing_source(self) -> None:
        result = execute("move_file", {"path": "nope.txt", "destination": "x.txt"}, self.ctx)
        self.assertIn("not found", result["error"])


class MutationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.ctx = _ctx(self.root)

    def test_git_folder_is_refused(self) -> None:
        (self.root / ".git").mkdir()
        result = execute("write_files", {"path": ".git/config", "contents": "x"}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertIn(".git", result["error"])

    @unittest.skipUnless(CAN_LINK, "symlinks")
    def test_write_goes_through_link_and_delete_refuses_link(self) -> None:
        (self.root / "real.txt").write_text("old\n", encoding="utf-8")
        (self.root / "link.txt").symlink_to(self.root / "real.txt")
        execute("read_files", {"path": "real.txt"}, self.ctx)
        result = execute("write_files", {"path": "link.txt", "contents": "new\n"}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertTrue((self.root / "link.txt").is_symlink())
        self.assertEqual((self.root / "real.txt").read_text(encoding="utf-8"), "new\n")
        refused = execute("delete_file", {"path": "link.txt"}, self.ctx)
        self.assertIn("symbolic link", refused["error"])

    def test_approved_outside_write_is_undoable(self) -> None:
        cache = self.root / ".bot" / "cache"
        checkpoints = agent_edit.Checkpoints(cache, self.root)
        checkpoints.start_task()
        outside = Path(tempfile.mkdtemp()).resolve() / "o.txt"
        ctx = _ctx(self.root, checkpoints=checkpoints)
        result = execute("write_files", {"path": str(outside), "contents": "x\n"}, ctx)
        self.assertTrue(result["ok"], result)
        agent_edit.undo_last(cache, self.root)
        self.assertFalse(outside.exists())


class PatchTests(unittest.TestCase):
    def test_patched_paths_cover_deletes_and_renames(self) -> None:
        patch = (
            "diff --git a/old.txt b/old.txt\ndeleted file mode 100644\n--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
            "diff --git a/r1.txt b/r2.txt\nsimilarity index 100%\nrename from r1.txt\nrename to r2.txt\n"
        )
        self.assertEqual(patched_paths(patch), ["old.txt", "r1.txt", "r2.txt"])
        self.assertEqual(patched_paths("--- x.txt\n+++ x.txt\n@@ -1 +1 @@\n-a\n+b\n"), ["x.txt"])

    def test_no_unsafe_paths_flag(self) -> None:
        seen: list[list[str]] = []

        def runner(argv, **_kw):
            seen.append(list(argv))
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        root = Path(tempfile.mkdtemp())
        execute("apply_patch", {"patch": "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"}, _ctx(root, runner=runner))
        self.assertTrue(seen)
        self.assertTrue(all("--unsafe-paths" not in argv for argv in seen))

    @unittest.skipUnless(HAS_GIT, "git")
    def test_deleted_file_is_checkpointed(self) -> None:
        root = Path(tempfile.mkdtemp()).resolve()
        (root / "gone.txt").write_text("bye\n", encoding="utf-8")
        cache = root / ".bot" / "cache"
        checkpoints = agent_edit.Checkpoints(cache, root)
        checkpoints.start_task()
        patch = "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
        result = execute("apply_patch", {"patch": patch}, _ctx(root, checkpoints=checkpoints, cache_dir=cache))
        self.assertTrue(result["ok"], result)
        self.assertFalse((root / "gone.txt").exists())
        self.assertIn("Patched gone.txt", result["ui"]["summary"])
        agent_edit.undo_last(cache, root)
        self.assertEqual((root / "gone.txt").read_text(encoding="utf-8"), "bye\n")


class GitEnvironmentTests(unittest.TestCase):
    def test_secrets_are_removed(self) -> None:
        fake = {"PATH": "/bin", "GITLAB_TOKEN": "s", "MY_API_KEY": "s", "GIT_AUTHOR_NAME": "me"}
        with mock.patch.object(agent_tools.agent_shell, "environment", return_value=dict(fake)), \
             mock.patch.object(agent_tools.agent_shell, "scrubbed_environment", None, create=True):
            env = agent_tools._git_environment()
        self.assertNotIn("GITLAB_TOKEN", env)
        self.assertNotIn("MY_API_KEY", env)
        self.assertEqual(env["GIT_AUTHOR_NAME"], "me")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_uses_shell_scrubbed_environment_when_present(self) -> None:
        with mock.patch.object(agent_tools.agent_shell, "scrubbed_environment", lambda extra=None: {"A": "1"}, create=True):
            self.assertEqual(agent_tools._git_environment(), {"A": "1", "GIT_TERMINAL_PROMPT": "0"})


class CommandArgTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = _ctx(Path(tempfile.mkdtemp()))

    def test_background_and_shell_are_validated(self) -> None:
        self.assertIn("shell must be one of", execute("run_command", {"command": "ls", "shell": "fish"}, self.ctx)["error"])
        self.assertIn("background", execute("run_command", {"command": "ls", "background": True}, self.ctx)["error"])
        self.assertIn("true or false", execute("run_command", {"command": "ls", "background": "maybe"}, self.ctx)["error"])

    def test_job_tools_without_a_session_explain_themselves(self) -> None:
        self.assertIn("no background jobs", execute("command_output", {}, self.ctx)["error"])
        self.assertIn("no background jobs", execute("kill_command", {"id": "3"}, self.ctx)["error"])


HAS_BASH = sys.platform != "win32" and shutil.which("bash") is not None


class _FakeSession:
    """ShellSession stand-in that records calls."""

    def __init__(self, root: Path) -> None:
        from critique_bot.agent_shell import CommandResult, Shell

        self.shell = Shell("bash", "/bin/bash", "bash")
        self.available = {"bash": self.shell}
        self.cwd = root
        self.calls: list[dict] = []
        self._result = CommandResult
        self.next = dict(exit_code=0, stdout="hi", stderr="", seconds=0.2, timed_out=False, interrupted=False)

    def resolve(self, name):
        if name and name not in self.available:
            raise ValueError(f'shell "{name}" is not available here. Available: bash')
        return self.shell

    def run(self, command, **kw):
        self.calls.append({"command": command, **kw})
        return self._result(cwd=self.cwd, shell=self.shell, **self.next)


class SessionCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.session = _FakeSession(self.root)
        self.output: list[str] = []
        self.cancel = threading.Event()
        self.ctx = _ctx(self.root, session=self.session, on_output=self.output.append, cancel=self.cancel)

    def test_passes_streaming_and_cancel_through(self) -> None:
        result = execute("run_command", {"command": "echo hi", "timeout": "5"}, self.ctx)
        self.assertTrue(result["ok"], result)
        call = self.session.calls[0]
        self.assertEqual(call["timeout"], 5)
        self.assertIs(call["cancel"], self.cancel)
        self.assertEqual(call["on_output"], self.output.append)
        self.assertTrue(result["output"].startswith("exit 0 (0.2s)\nhi"))
        self.assertNotIn("cwd:", result["output"])
        self.assertEqual(result["ui"]["summary"], "exit 0 · 0.2s · bash")

    def test_unknown_shell_and_states(self) -> None:
        self.assertIn("not available here", execute("run_command", {"command": "x", "shell": "zsh"}, self.ctx)["error"])
        self.session.next.update(exit_code=None, interrupted=True)
        self.assertIn("interrupted", execute("run_command", {"command": "x"}, self.ctx)["error"])
        self.session.next.update(interrupted=False, timed_out=True)
        self.assertIn("timed out", execute("run_command", {"command": "x", "timeout": 2}, self.ctx)["error"])

    def test_session_cwd_outside_workspace_needs_approval(self) -> None:
        self.session.cwd = Path(tempfile.mkdtemp()).resolve()
        permission = permission_for("run_command", {"command": "ls"}, self.ctx)
        self.assertEqual((permission.kind, permission.key), ("outside", ""))

    def test_cwd_permission_resolves_like_the_run(self) -> None:
        # The session was cd'ed above the workspace; "secret" exists in both places, and the run
        # starts in the session's one, so that is the one approval must judge.
        outer = Path(tempfile.mkdtemp()).resolve()
        workspace = outer / "ws"
        (workspace / "secret").mkdir(parents=True)
        (outer / "secret").mkdir()
        self.session.cwd = outer
        ctx = _ctx(workspace, session=self.session)
        permission = permission_for("run_command", {"command": "ls", "cwd": "secret"}, ctx)
        self.assertEqual((permission.kind, permission.key), ("outside", ""))
        execute("run_command", {"command": "ls", "cwd": "secret"}, ctx)
        self.assertEqual(Path(self.session.calls[-1]["cwd"]).resolve(), outer / "secret")
        self.session.cwd = workspace
        self.assertEqual(permission_for("run_command", {"command": "ls", "cwd": "secret"}, ctx).kind, "command")


@unittest.skipUnless(HAS_BASH, "real bash")
class RealShellSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        from critique_bot.agent_shell import ShellSession, Shell

        self.root = Path(tempfile.mkdtemp()).resolve()
        (self.root / "sub").mkdir()
        bash = Shell("bash", shutil.which("bash"), "bash")
        self.session = ShellSession(self.root, bash, available={"bash": bash})
        self.addCleanup(self.session.close)
        self.chunks: list[str] = []
        self.ctx = _ctx(self.root, session=self.session, on_output=self.chunks.append)

    def test_cd_persists_and_cwd_is_shown_when_it_changes(self) -> None:
        moved = execute("run_command", {"command": "cd sub && pwd"}, self.ctx)
        self.assertTrue(moved["ok"], moved)
        self.assertIn(f"cwd: {self.root / 'sub'}", moved["output"])
        here = execute("run_command", {"command": "pwd"}, self.ctx)
        self.assertIn(str(self.root / "sub"), here["output"])
        self.assertNotIn("cwd:", here["output"])
        self.assertTrue(self.chunks)

    def test_cwd_argument_is_for_one_call(self) -> None:
        once = execute("run_command", {"command": "pwd", "cwd": "sub"}, self.ctx)
        self.assertTrue(once["ok"], once)
        self.assertIn(str(self.root / "sub"), once["output"])
        self.assertEqual(Path(self.session.cwd).resolve(), self.root)
        self.assertNotIn("cwd", once["ui"]["summary"])
        self.assertIn("session cwd stays", once["output"])
        moved = execute("run_command", {"command": "cd ..", "cwd": "sub"}, self.ctx)
        self.assertTrue(moved["ok"], moved)
        self.assertEqual(Path(self.session.cwd).resolve(), self.root)
        (self.root / "sub" / "deeper").mkdir()
        went = execute("run_command", {"command": "cd deeper", "cwd": "sub"}, self.ctx)
        self.assertEqual(Path(self.session.cwd).resolve(), self.root / "sub" / "deeper")
        self.assertIn("cwd:", went["output"])

    def test_failure_exit_code(self) -> None:
        result = execute("run_command", {"command": "echo oops >&2; exit 3"}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertIn("exit 3", result["error"])
        self.assertIn("oops", result["output"])

    def test_background_job_output_and_kill(self) -> None:
        started = execute(
            "run_command", {"command": "echo ready; sleep 30", "background": "true"}, self.ctx
        )
        self.assertTrue(started["ok"], started)
        self.assertIn("started background job b1", started["output"])
        self.assertIn("ready", started["output"])
        listed = execute("command_output", {}, self.ctx)
        self.assertIn("b1 [running]", listed["output"])
        read = execute("command_output", {"job_id": "1", "wait": "0.2"}, self.ctx)
        self.assertIn("b1: running", read["output"])
        killed = execute("kill_command", {"job_id": "b1"}, self.ctx)
        self.assertEqual(killed["output"], "stopped b1")
        self.assertIn("no background job b9", execute("command_output", {"job_id": "b9"}, self.ctx)["error"])

    def test_finished_background_job_reports_exit(self) -> None:
        execute("run_command", {"command": "echo done", "background": True}, self.ctx)
        result = execute("command_output", {"job_id": "b1", "wait": 5}, self.ctx)
        for _ in range(20):
            if "exit 0" in result["output"]:
                break
            result = execute("command_output", {"job_id": "b1", "wait": 0.5}, self.ctx)
        self.assertIn("b1: exit 0", result["output"])
        self.assertIn("already exited", execute("kill_command", {"job_id": "b1"}, self.ctx)["output"])


class AskUserTests(unittest.TestCase):
    def test_without_user(self) -> None:
        result = execute("ask_user", {"question": "Which?"}, _ctx(Path(tempfile.mkdtemp())))
        self.assertEqual(result["error"], "no user available; decide yourself and continue")

    def test_numbered_answer_maps_to_option(self) -> None:
        asked: list[str] = []

        def ask(prompt: str) -> str:
            asked.append(prompt)
            return "2"

        ctx = _ctx(Path(tempfile.mkdtemp()), ask_user=ask)
        result = execute("ask_user", {"question": "Which db?", "options": '["sqlite", "postgres"]'}, ctx)
        self.assertEqual(result["output"], "user answered: postgres")
        self.assertIn("2. postgres", asked[0])


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/page":
            body = (
                "<html><head><title>Docs</title><script>var secret=1;</script><style>p{}</style></head>"
                "<body><nav>Home | About</nav><h2>Install</h2><p>Run <code>pip install x</code> or see "
                "<a href='/more'>more</a>.</p><pre>def f():\n    return 1</pre></body></html>"
            ).encode()
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path == "/data":
            self._send(200, "application/json", b'{"a":[1,2]}')
        elif self.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
        elif self.path == "/bin":
            self._send(200, "application/octet-stream", b"\x00\x01\x02" * 100)
        elif self.path == "/redirect":
            self.send_response(301)
            self.send_header("Location", "/data")
            self.end_headers()
        elif self.path == "/elsewhere":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        elif self.path == "/to-file":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
        else:
            self._send(404, "text/plain", b"nothing here")

    def _send(self, code: int, kind: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class WebFetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def fetch(self, path: str, **extra):
        with mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
            return execute("web_fetch", {"url": self.base + path, **extra}, _ctx(Path(tempfile.mkdtemp())))

    def test_html_becomes_readable_text(self) -> None:
        result = self.fetch("/page")
        self.assertTrue(result["ok"], result)
        text = result["output"]
        self.assertIn("# Docs", text)
        self.assertIn("## Install", text)
        self.assertIn("`pip install x`", text)
        self.assertIn(f"more ({self.base}/more)", text)
        self.assertIn("```\ndef f():\n    return 1\n```", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("About", text)
        self.assertTrue(result["ui"]["summary"].startswith("Fetched"))

    def test_json_is_pretty_and_redirects_followed(self) -> None:
        result = self.fetch("/redirect")
        self.assertIn('"a": [\n    1,', result["output"])

    def test_errors(self) -> None:
        self.assertIn("HTTP 404", self.fetch("/missing")["error"])
        self.assertIn("redirect", self.fetch("/loop")["error"])
        self.assertIn("binary content", self.fetch("/bin")["output"])
        bad = execute("web_fetch", {"url": "file:///etc/passwd"}, _ctx(Path(tempfile.mkdtemp())))
        self.assertIn("only http and https", bad["error"])

    def test_redirect_to_another_host_is_not_followed(self) -> None:
        result = self.fetch("/elsewhere")
        self.assertFalse(result["ok"])
        self.assertIn("http://169.254.169.254/latest/meta-data/", result["error"])
        self.assertIn("call web_fetch with that URL", result["error"])
        self.assertRegex(self.fetch("/to-file")["error"], "not (fetched|allowed)")

    def test_paging(self) -> None:
        result = self.fetch("/page", max_chars=500, offset=10)
        self.assertIn("showing 10-", result["output"])


class ToolListTests(unittest.TestCase):
    def test_new_tools_registered(self) -> None:
        for name in ("move_file", "web_fetch", "ask_user", "command_output", "kill_command"):
            self.assertIn(name, ALLOWED_TOOLS)
            self.assertIn(name, agent_tools._HANDLERS)
        self.assertIn("move_file", agent_tools.MUTATING)


if __name__ == "__main__":
    unittest.main()


class PromptAndSetupTests(unittest.TestCase):
    def test_both_prompt_copies_match(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        packaged = repo / "src" / "critique_bot" / "prompts" / "agent.txt"
        root_copy = repo / "prompts" / "agent.txt"
        self.assertEqual(
            root_copy.read_text(encoding="utf-8"),
            packaged.read_text(encoding="utf-8"),
            "prompts/agent.txt must be a copy of src/critique_bot/prompts/agent.txt (the one crit loads)",
        )

    def test_prompt_documents_the_session_shell(self) -> None:
        from critique_bot.agent import load_prompt_sections

        text = load_prompt_sections()["SYSTEM"]
        self.assertNotIn("does not carry over", text)
        self.assertNotIn("do not use && or ||", text)
        for phrase in ("cd carries over", "background true", "command_output", "kill_command", '"shell"'):
            self.assertIn(phrase, text)
        self.assertIn("twenty-two names", text)
        for name in ALLOWED_TOOLS:
            self.assertIn(name, text)

    def test_shell_setting(self) -> None:
        from critique_bot import agent

        self.assertEqual(agent._shell_preference({"shell": "PWSH"}), "pwsh")
        self.assertEqual(agent._shell_preference({}), "auto")
        self.assertEqual(agent._shell_preference({"shell": "fish"}), "auto")

    def test_session_gets_available_shells(self) -> None:
        from critique_bot import agent
        from critique_bot.agent_shell import Shell

        seen = {}

        class Factory:
            def __init__(self, workspace, shell, *, available=None):
                seen.update(workspace=workspace, shell=shell, available=available)

        bash = Shell("bash", "/bin/bash", "bash")
        with mock.patch.object(agent.agent_shell, "ShellSession", Factory):
            made = agent._open_shell_session(Path("."), bash, {"bash": bash})
        self.assertIsInstance(made, Factory)
        self.assertEqual(seen["available"], {"bash": bash})

    def test_seed_lists_other_shells(self) -> None:
        from critique_bot.agent import seed_message
        from critique_bot.agent_shell import Shell

        pwsh = Shell("pwsh", "pwsh.exe", "PowerShell 7 (pwsh.exe)")
        bash = Shell("gitbash", "bash.exe", "Git Bash")
        text = seed_message(Path("."), "", shell=pwsh, platform_name="win32", available={"pwsh": pwsh, "bash": bash})
        self.assertIn('"bash" (Git Bash)', text)
        self.assertIn("cd persists", text)
