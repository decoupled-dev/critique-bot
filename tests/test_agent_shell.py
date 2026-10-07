from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from critique_bot import agent_shell
from critique_bot.agent_shell import (
    Shell,
    ShellSession,
    adjust_command,
    available_shells,
    cmd_script,
    decode,
    detect_shell,
    find_git_bash,
    lint_command,
    posix_script,
    powershell_script,
    rewrite_and_or,
    scrubbed_environment,
    shell_preamble,
)

LEGACY = Shell("powershell", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "Windows PowerShell 5.1 (powershell.exe)")
MODERN = Shell("pwsh", r"C:\Program Files\PowerShell\7\pwsh.exe", "PowerShell 7 (pwsh.exe)")
GIT_BASH = Shell("gitbash", r"C:\Program Files\Git\bin\bash.exe", "Git Bash (bash.exe)")
PWSH = os.environ.get("CRIT_TEST_PWSH", "")
POSIX = sys.platform != "win32"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().split()[2] != "Z"
    except OSError:
        return True


def _wait_for(path: Path, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text().strip():
            return
        time.sleep(0.05)


class RewriteTests(unittest.TestCase):
    def test_and_or_chains_become_status_checks(self) -> None:
        text = rewrite_and_or("npm ci && npm test")
        self.assertIn("$global:LASTEXITCODE = $null; npm ci", text)
        self.assertIn("if ($__critS -eq 0) { $global:LASTEXITCODE = $null; npm test", text)
        self.assertTrue(text.rstrip().endswith("__crit_finish $__critS"))
        either = rewrite_and_or("git pull || echo offline")
        self.assertIn("if ($__critS -ne 0) { $global:LASTEXITCODE = $null; echo offline", either)
        mixed = rewrite_and_or("a && b || c")
        self.assertLess(mixed.index("-eq 0"), mixed.index("-ne 0"))

    def test_quotes_comments_and_other_statements_are_kept(self) -> None:
        self.assertEqual(rewrite_and_or("echo 'a && b'; Write-Output \"x || y\""), "echo 'a && b'; Write-Output \"x || y\"")
        self.assertEqual(rewrite_and_or("git log # a && b"), "git log # a && b")
        here = "$t = @'\nfoo && bar\n'@\n$t"
        self.assertEqual(rewrite_and_or(here), here)
        text = rewrite_and_or("cd app; npm ci && npm test\nGet-Date")
        self.assertTrue(text.startswith("cd app; $global:LASTEXITCODE = $null; npm ci"))
        self.assertTrue(text.endswith("__crit_finish $__critS\nGet-Date"))

    def test_nested_chain_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            rewrite_and_or("if ($x) { a && b }")
        with self.assertRaises(ValueError):
            rewrite_and_or("a &&")

    def test_wrapper_contains_the_rewrite_only_for_windows_powershell(self) -> None:
        legacy = powershell_script("a && b", kind="powershell")
        modern = powershell_script("a && b", kind="pwsh")
        self.assertIn("ExecutionPolicy", " ".join(agent_shell.script_argv(LEGACY, "x.ps1")))
        self.assertNotEqual(legacy, modern)
        self.assertNotIn("$ErrorActionPreference = 'Stop'", legacy)

    def test_script_runs_through_a_bootstrap_not_file(self) -> None:
        # Group Policy AllSigned/Restricted blocks -File even with -ExecutionPolicy Bypass.
        path = r"C:\Users\o'brien\AppData\Local\Temp\crit-shell-x\0123456789abcdef0123456789abcdef.ps1"
        argv = agent_shell.script_argv(LEGACY, path)
        self.assertNotIn("-File", argv)
        self.assertEqual(argv[-2], "-EncodedCommand")
        self.assertEqual(argv[argv.index("-OutputFormat") + 1], "Text")
        bootstrap = agent_shell.base64.b64decode(argv[-1]).decode("utf-16-le")
        self.assertIn("ReadAllText('C:\\Users\\o''brien\\", bootstrap)
        self.assertIn("[scriptblock]::Create(", bootstrap)
        self.assertLess(len(" ".join(argv)), 1000)


class LintTests(unittest.TestCase):
    def test_quoted_text_is_ignored(self) -> None:
        self.assertIsNone(lint_command('git commit -m "fix && grep | head -n 1"', LEGACY, {}))
        self.assertIsNone(lint_command("Write-Output 'export FOO=1; rm -rf /'", LEGACY, {}))
        self.assertIsNone(lint_command("npm ci && npm test", LEGACY, {}))

    def test_assignment_false_positives(self) -> None:
        self.assertIsNone(lint_command("git log --format=%H -n 1", LEGACY, {}))
        self.assertIsNone(lint_command("python -m pytest --maxfail=1 tests", LEGACY, {}))
        self.assertIsNone(lint_command("$env:FOO = 'x'; npm test", LEGACY, {}))
        self.assertIn("VAR=value", lint_command("FOO=1 npm test", LEGACY, {}) or "")

    def test_hashtables_are_not_var_assignments(self) -> None:
        for command in (
            "$h = @{a=1; b=2}",
            "[pscustomobject]@{Name=1; Age=2} | Format-Table",
            'Invoke-RestMethod -Uri $u -Method Post -Body @{x=1; y="z"}',
            "$h = @{\n  a=1\n  b=2\n}\n$h.a",
            "$o = @{a=@{b=1; c=2}; d=3}",
            "enum Color {\n  Red=1\n  Blue=2\n}",
        ):
            for shell in (LEGACY, MODERN):
                self.assertIsNone(lint_command(command, shell, {}), command)
        for command in ("FOO=1 npm test", "cd app; CI=true npm test", "if ($x) { FOO=bar make }", "@{a=1}; FOO=1 npm test"):
            self.assertIn("VAR=value", lint_command(command, LEGACY, {}) or "", command)

    def test_git_bash_hint_when_available(self) -> None:
        message = lint_command("cat log.txt | grep error", MODERN, {"bash": GIT_BASH}) or ""
        self.assertIn('"shell": "bash"', message)
        self.assertIn("Select-String", message)
        plain = lint_command("cat log.txt | grep error", MODERN, {}) or ""
        self.assertNotIn('"shell"', plain)
        self.assertIn("Select-String", plain)
        for command in ("ls -la", "rm -rf build", "mkdir -p a/b", "head -n 5 x", "foo 2>/dev/null", "export A=1"):
            self.assertIsNotNone(lint_command(command, LEGACY, {}), command)

    def test_python3_only_when_missing(self) -> None:
        self.assertIsNotNone(lint_command("python3 -m pytest", LEGACY, {}, has_command=lambda name: False))
        self.assertIsNone(lint_command("python3 -m pytest", LEGACY, {}, has_command=lambda name: True))

    def test_only_powershell_is_linted_and_unix_tools_are_fine_on_posix_pwsh(self) -> None:
        self.assertIsNone(lint_command("ls -la | grep x", Shell("bash", "/bin/bash", "bash"), {}))
        posix_pwsh = Shell("pwsh", "/usr/bin/pwsh", "PowerShell 7 (pwsh)")
        if POSIX:
            self.assertIsNone(lint_command("grep -r foo . | head", posix_pwsh, {}))


class AdjustTests(unittest.TestCase):
    def test_gradlew_only_as_a_command(self) -> None:
        self.assertEqual(adjust_command("./gradlew build"), "./gradlew --console=plain build")
        self.assertEqual(adjust_command("& .\\gradlew.bat test"), "& .\\gradlew.bat --console=plain test")
        self.assertEqual(adjust_command("cd app; gradlew.bat assemble"), "cd app; gradlew.bat --console=plain assemble")
        self.assertEqual(adjust_command("cat tools/gradlew-helper/notes.txt"), "cat tools/gradlew-helper/notes.txt")
        self.assertEqual(adjust_command("Get-Content gradlew"), "Get-Content gradlew")
        self.assertEqual(adjust_command("./gradlew --console=rich build"), "./gradlew --console=rich build")


class DetectionTests(unittest.TestCase):
    def _fs(self, files: set[str]):
        lowered = {item.lower() for item in files}
        return lambda path: str(path).lower() in lowered

    def test_git_bash_next_to_git(self) -> None:
        env = {"SystemRoot": r"C:\Windows", "ProgramFiles": r"D:\Apps"}
        which = {"git.exe": r"C:\Program Files\Git\cmd\git.exe"}.get
        found = find_git_bash(environ=env, which=which, is_file=self._fs({r"C:\Program Files\Git\bin\bash.exe"}), run=lambda argv: "")
        self.assertEqual(found, r"C:\Program Files\Git\bin\bash.exe")

    def test_wsl_bash_is_never_used(self) -> None:
        env = {"SystemRoot": r"C:\Windows"}
        which = {"bash.exe": r"C:\Windows\System32\bash.exe"}.get
        files = self._fs({r"C:\Windows\System32\bash.exe"})
        self.assertIsNone(find_git_bash(environ=env, which=which, is_file=files, run=lambda argv: ""))
        apps = {"bash.exe": r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\bash.exe"}.get
        self.assertIsNone(find_git_bash(environ=env, which=apps, is_file=lambda p: True, run=lambda argv: ""))

    def test_git_exec_path_fallback(self) -> None:
        env = {"SystemRoot": r"C:\Windows"}
        which = {"git.exe": r"C:\Users\me\scoop\shims\git.exe"}.get
        bash = r"C:\Users\me\scoop\apps\git\current\bin\bash.exe"
        found = find_git_bash(
            environ=env,
            which=which,
            is_file=self._fs({bash}),
            run=lambda argv: "C:/Users/me/scoop/apps/git/current/mingw64/libexec/git-core\n",
        )
        self.assertEqual(found, bash)

    def test_windows_shells_and_preference(self) -> None:
        which = {"pwsh.exe": r"C:\PS\pwsh.exe", "powershell.exe": r"C:\W\powershell.exe", "cmd.exe": r"C:\W\cmd.exe"}.get
        shells = available_shells("win32", which, environ={}, git_bash=lambda: r"C:\Git\bin\bash.exe")
        self.assertEqual(set(shells), {"pwsh", "powershell", "cmd", "bash"})
        self.assertEqual(shells["bash"].kind, "gitbash")
        self.assertEqual(shells["pwsh"].exe, r"C:\PS\pwsh.exe")
        self.assertEqual(detect_shell("win32", which).kind, "pwsh")
        self.assertEqual(detect_shell("win32", which, preference="cmd").kind, "cmd")
        no_pwsh = {"powershell.exe": r"C:\W\powershell.exe"}.get
        self.assertEqual(detect_shell("win32", no_pwsh).kind, "powershell")
        self.assertEqual(detect_shell("win32", no_pwsh, preference="pwsh").kind, "powershell")

    def test_posix_prefers_bash_then_sh(self) -> None:
        both = {"bash": "/usr/bin/bash", "sh": "/bin/sh", "zsh": "/bin/zsh"}.get
        self.assertEqual(detect_shell("linux", both), Shell("bash", "/usr/bin/bash", "bash"))
        self.assertEqual(detect_shell("linux", both, preference="zsh").kind, "zsh")
        self.assertEqual(detect_shell("linux", {"sh": "/bin/sh"}.get).exe, "/bin/sh")

    def test_preamble_lists_other_shells(self) -> None:
        text = shell_preamble(LEGACY, {"powershell": LEGACY, "bash": GIT_BASH, "cmd": Shell("cmd", "cmd.exe", "Command Prompt (cmd.exe)")})
        self.assertTrue(text.startswith("SHELL: Windows PowerShell 5.1 (powershell.exe)."))
        self.assertIn('"bash" (Git Bash (bash.exe))', text)
        self.assertIn("persists", text)
        self.assertIn("background", text)
        self.assertLess(len(text), 900)


class DecodeTests(unittest.TestCase):
    def test_oem_fallback_with_injected_codepage(self) -> None:
        raw = "Привет".encode("cp866")
        self.assertEqual(decode(raw, codepages=(866, 1251), platform_name="win32"), "Привет")
        self.assertEqual(decode("é".encode("cp1252"), codepages=(), platform_name="win32"), "é")
        self.assertEqual(decode("naïve".encode("utf-8"), codepages=(866,), platform_name="win32"), "naïve")

    def test_utf16_needs_bom_or_strong_pattern(self) -> None:
        self.assertEqual(decode("pong".encode("utf-16")), "pong")
        self.assertEqual(decode("hello world".encode("utf-16-le")), "hello world")
        short = b"a\x00b\x00"
        self.assertNotEqual(decode(short), "ab")

    def test_cmd_and_posix_scripts(self) -> None:
        batch = cmd_script("echo hi\nver", marker=r"C:\t\m.cwd")
        self.assertTrue(batch.startswith("@echo off\r\nchcp 65001>nul\r\necho hi\r\nver\r\n"))
        self.assertIn('@cd > "C:\\t\\m.cwd"', batch)
        self.assertNotIn("\r\r", batch)
        self.assertIn("pwd -W", posix_script("ls", kind="gitbash", marker="/t/m"))
        self.assertIn("trap __crit_done EXIT", posix_script("ls", marker="/t/m"))


class EnvironmentTests(unittest.TestCase):
    def test_secrets_are_scrubbed(self) -> None:
        base = {
            "PATH": "/bin",
            "HOME": "/home/me",
            "SSH_AUTH_SOCK": "/tmp/agent",
            "GITHUB_TOKEN": "x",
            "CRITIQUE_GITLAB_TOKEN": "x",
            "OPENAI_API_KEY": "x",
            "OPENAI_BASE_URL": "x",
            "ANTHROPIC_MODEL": "x",
            "AWS_SECRET_ACCESS_KEY": "x",
            "AWS_SESSION_TOKEN": "x",
            "DB_PASSWORD": "x",
            "MY_APIKEY": "x",
            "AZURE_DEVOPS_EXT_PAT": "x",
            "GOOGLE_APPLICATION_CREDENTIALS": "x",
        }
        env = scrubbed_environment(base=base)
        self.assertEqual(set(env), {"PATH", "HOME", "SSH_AUTH_SOCK"})
        again = scrubbed_environment({"NPM_TOKEN": "keep", "HOME": None}, base=base)
        self.assertEqual(again["NPM_TOKEN"], "keep")
        self.assertNotIn("HOME", again)

    def test_login_environment_merge(self) -> None:
        merged = agent_shell.merge_login_environment(
            {"PATH": os.pathsep.join(["/a", "/b"]), "X": "1"}, {"PATH": os.pathsep.join(["/b", "/c"]), "X": "2", "Y": "3"}
        )
        self.assertEqual(merged["PATH"], os.pathsep.join(["/a", "/b", "/c"]))
        self.assertEqual((merged["X"], merged["Y"]), ("1", "3"))


class CaptureTests(unittest.TestCase):
    def test_bounded_head_and_tail(self) -> None:
        capture = agent_shell._Capture()
        for start in range(0, 20000, 500):
            capture.feed("".join(f"line {i}\n" for i in range(start, start + 500)).encode())
        text = capture.text()
        self.assertIn("line 0\n", text)
        self.assertIn("line 19999", text)
        self.assertRegex(text, r"\.\.\. \d+ lines omitted \.\.\.")
        self.assertLess(len(text), 400_000)


@unittest.skipUnless(POSIX and agent_shell.shutil.which("bash"), "needs bash")
class BashSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "sub").mkdir()
        bash = Shell("bash", agent_shell.shutil.which("bash") or "bash", "bash")
        self.session = ShellSession(self.root, bash, available={"bash": bash})

    def tearDown(self) -> None:
        self.session.close()
        self.tmp.cleanup()

    def test_cwd_persists_and_cwd_argument_moves_the_session(self) -> None:
        first = self.session.run("cd sub && pwd", timeout=10)
        self.assertEqual(first.exit_code, 0)
        self.assertEqual(first.cwd, self.root / "sub")
        second = self.session.run("pwd", timeout=10)
        self.assertEqual(second.stdout, str(self.root / "sub"))
        third = self.session.run("pwd", timeout=10, cwd=self.root)
        self.assertEqual(third.stdout, str(self.root))
        self.assertEqual(self.session.cwd, self.root)
        self.assertEqual(self.session.run("exit 7", timeout=10).exit_code, 7)
        with self.assertRaises(ValueError):
            self.session.run("pwd", timeout=10, cwd=self.root / "missing")
        with self.assertRaises(ValueError):
            self.session.run("pwd", timeout=10, shell="cmd")

    def test_long_command_and_secrets(self) -> None:
        payload = "x" * 20000
        result = self.session.run(f"printf %s '{payload}' | wc -c", timeout=10)
        self.assertEqual(result.stdout.strip(), "20000")
        os.environ["CRITIQUE_TEST_TOKEN"] = "secret-value"
        try:
            session = ShellSession(self.root, self.session.shell, available={})
            shown = session.run("env", timeout=10)
        finally:
            del os.environ["CRITIQUE_TEST_TOKEN"]
        self.assertNotIn("secret-value", shown.stdout)
        self.assertIn("NO_COLOR=1", shown.stdout)

    def test_timeout_kills_the_tree_and_keeps_output(self) -> None:
        pid_file = self.root / "pid"
        lines: list[str] = []
        result = self.session.run(
            f"echo partial; sh -c 'echo $$ > {pid_file}; sleep 60' & wait", timeout=1.5, on_output=lines.append
        )
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)
        self.assertIn("partial", result.stdout)
        self.assertIn("partial", "".join(lines))
        pid = int(pid_file.read_text().strip())
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_alive(pid))

    def test_cancel_and_daemon_grandchild(self) -> None:
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        started = time.monotonic()
        result = self.session.run("sleep 30", timeout=60, cancel=cancel)
        self.assertTrue(result.interrupted)
        self.assertLess(time.monotonic() - started, 5)
        started = time.monotonic()
        daemon = self.session.run("(sleep 30 &) ; echo spawned", timeout=60)
        self.assertEqual(daemon.exit_code, 0)
        self.assertIn("spawned", daemon.stdout)
        self.assertLess(time.monotonic() - started, 5)

    def test_background_job_lifecycle(self) -> None:
        job = self.session.start_background("echo ready; sleep 30")
        text, running, code = self.session.read_background(job, wait=5)
        self.assertIn("ready", text)
        self.assertTrue(running)
        self.assertIsNone(code)
        self.assertEqual([row["id"] for row in self.session.jobs()], [job])
        self.assertTrue(self.session.kill_background(job))
        self.assertFalse(self.session.jobs()[0]["running"])
        self.assertFalse(self.session.kill_background(job))
        quick = self.session.start_background("echo done; exit 4")
        deadline = time.monotonic() + 5
        while True:
            text, running, code = self.session.read_background(quick, wait=1)
            if not running or time.monotonic() > deadline:
                break
        self.assertFalse(running)
        self.assertEqual(code, 4)
        lingering = self.session.start_background("sleep 30")
        pid = self.session.jobs()[-1]["pid"]
        self.session.close()
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_alive(pid), lingering)


@unittest.skipUnless(PWSH and os.path.isfile(PWSH), "set CRIT_TEST_PWSH to a pwsh executable")
class RealPowerShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "sub dir").mkdir()
        self.pwsh = Shell("pwsh", PWSH, "PowerShell 7 (pwsh)")
        # The 5.1 code path (&&/|| rewrite) runs on pwsh as well.
        self.legacy = Shell("powershell", PWSH, "Windows PowerShell 5.1 (powershell.exe)")
        self.session = ShellSession(self.root, self.pwsh, available={"pwsh": self.pwsh, "powershell": self.legacy})

    def tearDown(self) -> None:
        self.session.close()
        self.tmp.cleanup()

    def run_ps(self, command: str, *, shell: str | None = None, timeout: float = 60):
        return self.session.run(command, timeout=timeout, shell=shell)

    def test_exit_codes(self) -> None:
        native_fail = "sh -c 'exit 5'" if POSIX else "cmd /c exit 5"
        cases = {
            "Get-Item ./does-not-exist": 1,
            f"{native_fail}; Get-Item .": 0,
            f"Get-Item ./does-not-exist; {native_fail}": 5,
            "exit 3": 3,
            "Write-Error boom": 1,
            "throw 'bad'": 1,
            "Get-ChildItem ./nope; 'after'": 0,
            "function f { exit 7 }; f": 7,
            "$x = 2; if ($x -eq 2) { 'two' }": 0,
            "'unterminated": 1,
            "try { Get-Item ./nope -ErrorAction Stop } catch { 'handled' }": 0,
            "Get-Item ./nope || 'fallback'": 0,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(self.run_ps(command).exit_code, expected)
        self.assertIn("Cannot find path", self.run_ps("Get-Item ./does-not-exist").stderr)

    def test_named_blocks_and_using(self) -> None:
        native_fail = "sh -c 'exit 4'" if POSIX else "cmd /c exit 4"
        cases = {
            "begin { 'b' } process { 'p' } end { 'e' }": (0, "b\np\ne"),
            "param($a = 3)\nbegin { 'b' } end { exit $a }": (3, "b"),
            f"begin {{ }} end {{ {native_fail} }}": (4, ""),
            "end { Get-Item ./nope -ErrorAction Stop }": (1, ""),
            "end { 'only' }": (0, "only"),
            "param($n = 'x')\n\"[$n]\"": (0, "[x]"),
            "using namespace System.Text\n[StringBuilder]::new('sb').ToString()": (0, "sb"),
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                result = self.run_ps(command)
                self.assertEqual((result.exit_code, result.stdout), expected, result.stderr)

    def test_errors_do_not_show_the_temp_script(self) -> None:
        result = self.run_ps("'a'\nWrite-Error boom")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("boom", result.stderr)
        self.assertNotIn(".ps1", result.stderr)
        self.assertNotIn("CLIXML", result.stderr)
        self.assertNotIn("__critLast", result.stderr)

    def test_bootstrap_runs_a_path_with_a_quote(self) -> None:
        folder = self.root / "o'brien dir"
        folder.mkdir()
        script = folder / "s.ps1"
        script.write_bytes(("\ufeff" + powershell_script("'ünï'; $Host.SetShouldExit(0); exit 6")).encode("utf-8"))
        done = agent_shell.subprocess.run(agent_shell.script_argv(self.pwsh, str(script)), capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 6, done.stderr)
        self.assertEqual(decode(done.stdout).strip(), "ünï")

    def test_and_or_on_both_versions(self) -> None:
        native_fail = "sh -c 'exit 2'" if POSIX else "cmd /c exit 2"
        for shell in ("pwsh", "powershell"):
            with self.subTest(shell=shell):
                failed = self.run_ps(f"{native_fail} && 'no'", shell=shell)
                self.assertEqual(failed.exit_code, 2)
                self.assertNotIn("no", failed.stdout)
                fallback = self.run_ps("Get-Item ./nope || 'fallback'", shell=shell)
                self.assertEqual((fallback.exit_code, fallback.stdout), (0, "fallback"))
                quoted = self.run_ps("'a && b'; 'c'", shell=shell)
                self.assertEqual(quoted.stdout, "a && b\nc")

    def test_output_cwd_and_long_command(self) -> None:
        shown = self.run_ps("Set-Location 'sub dir'; Get-Location; 'ünï €'")
        self.assertEqual(shown.exit_code, 0)
        self.assertIn(str(self.root / "sub dir"), shown.stdout)
        self.assertIn("ünï €", shown.stdout)
        self.assertEqual(self.session.cwd, self.root / "sub dir")
        self.assertEqual(self.run_ps("(Get-Location).Path").stdout, str(self.root / "sub dir"))
        payload = "y" * 20000
        long = self.run_ps(f"$s = '{payload}'; $s.Length")
        self.assertEqual((long.exit_code, long.stdout), (0, "20000"))

    def test_secrets_hidden(self) -> None:
        os.environ["GITLAB_TOKEN"] = "hidden-value"
        try:
            session = ShellSession(self.root, self.pwsh, available={})
            result = session.run("\"[$env:GITLAB_TOKEN]\"", timeout=60)
        finally:
            del os.environ["GITLAB_TOKEN"]
        self.assertEqual(result.stdout, "[]")

    @unittest.skipUnless(POSIX, "uses sh for the grandchild")
    def test_timeout_kills_tree(self) -> None:
        pid_file = self.root / "pid"
        result = self.run_ps(f"'partial'; sh -c 'echo $$ > {pid_file}; sleep 60'", timeout=4)
        self.assertTrue(result.timed_out)
        self.assertIn("partial", result.stdout)
        pid = int(pid_file.read_text().strip())
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_alive(pid))

    def test_background_job(self) -> None:
        job = self.session.start_background("'ready'; Start-Sleep -Seconds 30")
        deadline = time.monotonic() + 20
        seen = ""
        while "ready" not in seen and time.monotonic() < deadline:
            text, running, _ = self.session.read_background(job, wait=1)
            seen += text
        self.assertIn("ready", seen)
        self.assertTrue(self.session.kill_background(job))
        done = self.session.start_background("exit 6")
        deadline = time.monotonic() + 20
        running = True
        code = None
        while running and time.monotonic() < deadline:
            _, running, code = self.session.read_background(done, wait=1)
        self.assertEqual(code, 6)

    def test_lint_and_session_agree(self) -> None:
        self.assertIsNone(lint_command("Write-Output 'x && y | grep z'", self.legacy, {}))
        self.assertTrue(re.search(r"&&", lint_command("if ($true) { 'a' && 'b' }", self.legacy, {}) or ""))


if __name__ == "__main__":
    unittest.main()
