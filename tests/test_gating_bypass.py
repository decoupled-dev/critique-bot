"""Bypasses of the read-only, secret-file, and outside-workspace gates, one test per hole."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from critique_bot import agent_edit, agent_tools
from critique_bot.agent_rules import command_segments, suggest_rule
from critique_bot.agent_tools import ToolContext, TaskState, execute, permission_for, read_only_command

_PEM_BODY = [
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj",
    "MzEfYyjiWA4R4/M2bS1GB4t7NXp98C3SC6dVMvDuictGeurT8jNbvJZHtCSuYEvu",
    "NMoSfm76oqFvAp8Gy0iz5sxjZmSnXyCdPEovGhLa0VzMaQ8s+CLOyS56YyCFGeJZ",
]
_PEM = "\n".join(["-----BEGIN PRIVATE KEY-----", *_PEM_BODY, "-----END PRIVATE KEY-----"])


def _ctx(root: Path, **extra) -> ToolContext:
    return ToolContext(workspace=root, state=TaskState(task="t"), **extra)


class ReadOnlyOptionTests(unittest.TestCase):
    def test_dangerous_options_are_not_read_only(self) -> None:
        for command in [
            "rg --pre ./evil.sh foo a.py", "rg --pre=./evil.sh foo a.py", "rg --pre-glob '*' foo a.py",
            "rg -z foo a.gz", "rg --search-zip foo a.gz",
            "sort --compress-program=sh a.txt", "sort -o ~/.bashrc a.txt", "sort -ro x a.txt", "sort --output=x a.txt",
            "find . -fprintf out '%p' -name x", "find . -fprint0 out", "find . -fprint out", "find . -fls out",
            "find . -exec sh -c id ;", "find . -execdir id ;", "find . -ok id ;", "find . -delete",
            "git log --output=/tmp/x", "git log --outp=/tmp/x", "git diff --ext-diff", "git diff --textconv",
            "git --git-dir=/tmp/x log", "git --work-tree=/ status", "git --exec-path=/tmp log", "git -C /etc log",
            "git grep -O foo", "git grep --open-files-in-pager foo",
            "git config core.pager evil", "git config --add core.pager x", "git config --get",
            "git cat-file -p HEAD:.env", "git remote show origin",
            "uniq a.txt out.txt", "xxd -r dump.hex out.bin", "xxd a.bin out.txt", "tree -o out.txt",
            "PATH=tools:/usr/bin ls", "LD_PRELOAD=x.so cat a.txt", "env LD_PRELOAD=x.so ls",
            "./gradlew tasks", "gradle tasks", "gradlew dependencies",
            "grep -r token .", "grep -R token .", "grep -rn token src", "grep --recursive x .", "grep -d recurse x .",
            "rg -u token a.py", "rg -uu token", "rg --no-ignore token a.py", "rg --hidden token a.py", "rg token",
            "rg token src", "diff -r a b", "diff -N a b", "less -o log.txt a.txt", "cat $HOME/.ssh/id_rsa",
            "ls | sort --compress-program=sh", "git log | rg --pre ./x y", "Get-ChildItem -Recurse | Select-String key",
            "./evil.sh --help",
        ]:
            with self.subTest(command=command):
                self.assertFalse(read_only_command(command))

    def test_everyday_reads_stay_read_only(self) -> None:
        for command in [
            "ls -la", "git log -5", "git log --oneline -5", "git diff HEAD~1", "git status", "git diff --text",
            "git config --get user.name", "git config --list", "git branch", "git remote -v", "git stash list",
            "sort a.txt", "sort -rn a.txt", "uniq -c a.txt", "xxd a.bin", "tree -L 2", "find . -name '*.py'",
            "grep -n x a.py", "rg -n foo a.py", "rg -e foo b.py", "cat a | grep b", "git log -5 | head", "java -version",
            "jq . a.json", "diff a.txt b.txt", "cd src && ls",
        ]:
            with self.subTest(command=command):
                self.assertTrue(read_only_command(command))

    def test_wrapper_with_a_flag_has_no_program_key(self) -> None:
        for command in ["env -S 'sh -c id'", "xargs -I{} rm {}", "nice -n 5 make", "timeout -s 9 5 make", "sudo -u x ls",
                        "command -p ls", "time -p make", "nohup -- make"]:
            with self.subTest(command=command):
                self.assertEqual(agent_tools.command_key(command), "")

    def test_assignment_never_becomes_a_rule_key(self) -> None:
        self.assertEqual(agent_tools.command_key("PATH=tools:/usr/bin ls"), "")
        self.assertEqual(agent_tools.command_key("npm test && LD_PRELOAD=x.so npm test"), "")
        self.assertIsNone(command_segments("npm test | LD_PRELOAD=x.so grep y"))
        self.assertEqual(agent_tools.command_key("npm test | sort -o ~/.bashrc"), "")
        self.assertEqual(command_segments("npm test | sort -o x"), ["npm test", "sort -o x"])


class CommandPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        (self.root / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        self.ctx = _ctx(self.root, session=SimpleNamespace(cwd=self.root))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _perm(self, command: str):
        return permission_for("run_command", {"command": command}, self.ctx)

    def test_globs_that_reach_a_secret_file_ask(self) -> None:
        for command in ["cat .e*", "cat .en?", "cat .[e]nv", "head -5 *env", "cd sub && cat *.txt"]:
            with self.subTest(command=command):
                perm = self._perm(command)
                self.assertEqual(perm.kind, "outside")
                self.assertEqual(perm.risk, agent_tools.SECRET_RISK)
        self.assertEqual(self._perm("cat *.txt").kind, "command")

    def test_rev_path_and_link_to_a_secret_ask(self) -> None:
        self.assertEqual(self._perm("git show HEAD:.env").risk, agent_tools.SECRET_RISK)
        self.assertEqual(self._perm("git show main:config/.env").risk, agent_tools.SECRET_RISK)
        os.symlink(self.root / ".env", self.root / "notes.txt")
        self.assertEqual(self._perm("cat notes.txt").risk, agent_tools.SECRET_RISK)

    def test_read_only_command_outside_the_workspace_asks(self) -> None:
        for command in ["cat /etc/passwd", "head ~/.bashrc", "ls ..", "cd .. && ls", "jq --rawfile x /etc/passwd -n .", "jq --slurpfile x ../other.json -n .",
                        "git diff --no-index /etc/hosts a.txt"]:
            with self.subTest(command=command):
                perm = self._perm(command)
                self.assertEqual(perm.kind, "outside")
                self.assertEqual(perm.risk, agent_tools.OUTSIDE_READ_RISK)
        self.assertEqual(self._perm("cat a.txt").kind, "command")
        self.assertTrue(read_only_command("cat a.txt"))


class GitSafetyTests(unittest.TestCase):
    def test_own_git_calls_switch_off_repo_programs(self) -> None:
        argv = agent_tools.git_argv(Path("/w"), ["diff", "--stat"])
        for setting in ["core.fsmonitor=", "diff.external=", "core.pager=cat"]:
            self.assertIn(setting, argv)
        self.assertTrue(any(item.startswith("core.hooksPath=") for item in argv))
        self.assertEqual(argv[argv.index("diff") : argv.index("diff") + 3], ["diff", "--no-ext-diff", "--no-textconv"])
        self.assertNotIn("--no-ext-diff", agent_tools.git_argv(Path("/w"), ["status"]))
        self.assertEqual(agent_tools._git_environment()["GIT_CONFIG_NOSYSTEM"], "1")

    def test_tool_runs_git_through_the_helper(self) -> None:
        seen: list[list[str]] = []

        def runner(argv, **kwargs):
            seen.append(argv)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        execute("git_status", {}, _ctx(Path(tempfile.mkdtemp()), runner=runner))
        self.assertIn("core.fsmonitor=", seen[0])

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_show_of_a_blob_id_asks_like_a_secret(self) -> None:
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(root)}
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
        (root / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
        blob = subprocess.run(["git", "-C", str(root), "hash-object", "-w", ".env"], check=True, capture_output=True, env=env)
        perm = permission_for("git_show", {"rev": blob.stdout.decode().strip()}, _ctx(root))
        self.assertEqual(perm.kind, "outside")
        self.assertEqual(perm.risk, agent_tools.SECRET_RISK)


class SecretEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        (self.root / ".env").write_text("TOKEN=supersecretvalue\nOTHER=1\n", encoding="utf-8")
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_edits_of_secret_files_ask(self) -> None:
        ctx = _ctx(self.root)
        cases = [
            ("edit_file", {"path": ".env", "old_string": "OTHER=1", "new_string": "OTHER=2"}),
            ("write_files", {"files": [{"path": ".env", "contents": "x"}]}),
            ("delete_file", {"path": ".env"}),
            ("move_file", {"path": ".env", "destination": "public.txt"}),
            ("move_file", {"path": "a.txt", "destination": ".env.local"}),
        ]
        for tool, args in cases:
            with self.subTest(tool=tool, args=args):
                perm = permission_for(tool, args, ctx)
                self.assertEqual(perm.kind, "outside")
                self.assertEqual(perm.key, "")

    def test_edit_results_do_not_carry_the_secret(self) -> None:
        ctx = _ctx(self.root)
        failed = execute("edit_file", {"path": ".env", "old_string": "TOKEN=supersecretvalu", "new_string": "x"}, ctx)
        self.assertNotIn("supersecretvalue", str(failed))
        done = execute("edit_file", {"path": ".env", "old_string": "OTHER=1", "new_string": "OTHER=2"}, ctx)
        self.assertTrue(done["ok"], done)
        self.assertNotIn("supersecretvalue", done["output"])
        wrote = execute("write_files", {"files": [{"path": ".env", "contents": "NEW=1\n"}], "overwrite": True}, ctx)
        self.assertNotIn("supersecretvalue", wrote.get("output", ""))

    def test_checkpoints_keep_no_copy_of_a_secret_file(self) -> None:
        cache = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cache, True)
        checkpoints = agent_edit.Checkpoints(cache, self.root)
        checkpoints.start_task()
        self.assertFalse(checkpoints.save(self.root / ".env"))
        self.assertTrue(checkpoints.save(self.root / "a.txt"))
        copies = [path.read_text(encoding="utf-8") for path in cache.rglob("before")]
        self.assertFalse(any("supersecretvalue" in text for text in copies))
        self.assertEqual(agent_edit.undo_last(cache, self.root), ["a.txt"])
        self.assertTrue((self.root / ".env").exists())


class RuleSuggestionTests(unittest.TestCase):
    def test_interpreters_and_scripts_get_the_exact_command(self) -> None:
        self.assertEqual(suggest_rule("python3 scripts/check.py"), "python3 scripts/check.py")
        self.assertEqual(suggest_rule("npm run build"), "npm run build")
        self.assertEqual(suggest_rule("yarn run lint"), "yarn run lint")
        self.assertEqual(suggest_rule("node -e 'x()'"), "node -e 'x()'")
        self.assertEqual(suggest_rule("make -C sub"), "make -C sub")
        self.assertEqual(suggest_rule("bash -c 'ls *'"), "")
        self.assertEqual(suggest_rule("./gradlew test --info"), "./gradlew test*")
        self.assertEqual(suggest_rule("git status"), "git status*")

    def test_python_m_finds_the_module_file(self) -> None:
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "pkg").mkdir()
        (root / "pkg" / "send.py").write_text("import socket\n", encoding="utf-8")
        (root / "tool").mkdir()
        (root / "tool" / "__main__.py").write_text("import urllib.request\n", encoding="utf-8")
        self.assertEqual(agent_tools.network_script("python -m pkg.send", root), "pkg/send.py")
        self.assertEqual(agent_tools.network_script("python3 -m tool", root), "tool/__main__.py")
        ctx = _ctx(root)
        ctx.state.note_write("pkg/send.py", "v1")
        self.assertTrue(agent_tools._command_runs_written("python -m pkg.send", ctx))


class FetchRedirectTests(unittest.TestCase):
    def test_https_does_not_fall_back_to_http(self) -> None:
        handler = agent_tools._LimitedRedirects()
        request = urllib.request.Request("https://docs.example/a")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://docs.example/b")
        upgraded = handler.redirect_request(urllib.request.Request("http://docs.example/a"), None, 302, "Found", {}, "https://docs.example/b")
        self.assertEqual(upgraded.full_url, "https://docs.example/b")


class RedactBeforeCutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        (self.root / "notes.txt").write_text("intro\n" + _PEM + "\nafter\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_window_inside_a_key_shows_none_of_it(self) -> None:
        result = execute("read_files", {"paths": ["notes.txt"], "offset": 1, "limit": 3}, _ctx(self.root))
        self.assertTrue(result["ok"], result)
        for line in _PEM_BODY[:2]:
            self.assertNotIn(line, result["output"])
        self.assertIn("1|intro", result["output"])

    def test_lines_keep_their_numbers(self) -> None:
        result = execute("read_files", {"paths": ["notes.txt"]}, _ctx(self.root))
        self.assertIn("7|after", result["output"])

    def test_search_hit_inside_a_key_is_redacted(self) -> None:
        result = execute("search_code", {"pattern": "MzEfYyjiWA4R", "literal": True}, _ctx(self.root))
        self.assertNotIn(_PEM_BODY[1], result["output"])


class EnvironmentDumpTest(unittest.TestCase):
    """Environment variables hold tokens, so printing them asks first."""

    def test_environment_dumps_are_not_read_only(self) -> None:
        from critique_bot.agent_tools import read_only_command

        for command in ("printenv", "printenv GITLAB_TOKEN", "Get-ChildItem env:", "gci env:",
                        "jq -n env", "cat /proc/self/environ"):
            self.assertFalse(read_only_command(command), command)
        for command in ("jq .name package.json", "ls", "cat README.md"):
            self.assertTrue(read_only_command(command), command)


if __name__ == "__main__":
    unittest.main()
