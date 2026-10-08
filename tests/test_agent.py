from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from critique_bot.agent import (
    ALLOWED_TOOLS,
    command_argv,
    commentary,
    execute_tool,
    format_tool_result,
    parse_tool_calls,
    run_agent_loop,
    seed_message,
)
from critique_bot.agent_shell import Shell, command_argv as shell_command_argv, tool_hints
from critique_bot.agent_tools import TaskState, canonical_tool
from critique_bot.chat_client import COMPLETION_IDLE


def _edit(path: str, old: str, new: str) -> str:
    return (
        '<tool_call>\n{"tool": "edit_file", "arguments": '
        + json.dumps({"path": path, "old_string": old, "new_string": new})
        + "}\n</tool_call>"
    )


def _call(tool: str, **arguments: object) -> str:
    return "<tool_call>\n" + json.dumps({"tool": tool, "arguments": arguments}) + "\n</tool_call>"


class _Scripted:
    def __init__(self, replies: list[str], detail: dict | None = None) -> None:
        self.sent: list[str] = []
        self.last_detail = detail
        self._replies = list(replies)

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        return self._replies.pop(0)


def _loop(session, root: Path, task: str, **kwargs):
    kwargs.setdefault("max_rounds", None)
    kwargs.setdefault("max_result_chars", 8000)
    kwargs.setdefault("read_message", lambda: None)
    kwargs.setdefault("emit", lambda _text: None)
    kwargs.setdefault("approve_mode", "auto")
    kwargs.setdefault("ask_user", lambda _question: None)
    return run_agent_loop(
        session,
        workspace=root,
        index_path=kwargs.pop("index_path", None),
        cache_dir=kwargs.pop("cache_dir", None),
        first_task=task,
        **kwargs,
    )


class ParserTests(unittest.TestCase):
    def test_parses_call_and_rejects_unknown_name(self) -> None:
        reply = """
Look here.
<tool_call>
{"tool": "grep", "arguments": {"pattern": "x"}}
</tool_call>
"""
        calls, unclosed = parse_tool_calls(reply)
        self.assertFalse(unclosed)
        self.assertEqual(calls[0].tool, "grep")
        result = execute_tool("teleport", {"pattern": "x"}, workspace=Path("."))
        text = format_tool_result(result)
        payload = json.loads(text.split("\n", 1)[1].rsplit("\n", 1)[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["allowed"], list(ALLOWED_TOOLS))
        self.assertIn("unknown tool", payload["error"])

    def test_commentary_hides_tool_markup(self) -> None:
        text = (
            "Reading the file.\n"
            "<tool_call>\n"
            '{"tool": "read_files"}\n'
            "</tool_call>\n"
            "<tool_result>\n"
            '{"ok": true}\n'
            "</tool_result>\n"
            "Done."
        )
        visible = commentary(text)
        self.assertNotIn("<tool_call>", visible)
        self.assertNotIn("<tool_result>", visible)
        self.assertNotIn("read_files", visible)
        self.assertIn("Reading the file.", visible)
        self.assertIn("Done.", visible)
        unclosed = commentary('Hello\n<tool_call>\n{"tool": "list_files"}')
        self.assertEqual(unclosed, "Hello")
        mentioned = commentary("The notes mention <tool_call> blocks.")
        self.assertNotIn("<tool_call>", mentioned)
        self.assertIn("blocks.", mentioned)

    def test_invalid_json_and_unclosed_tag(self) -> None:
        calls, unclosed = parse_tool_calls("<tool_call>\n{not json}\n</tool_call>")
        self.assertFalse(unclosed)
        self.assertIsNotNone(calls[0].error)
        calls, unclosed = parse_tool_calls('<tool_call>\n{"tool": "list_files"}')
        self.assertTrue(unclosed)
        self.assertEqual(calls, [])

    def test_repairs_model_json(self) -> None:
        samples = [
            '<tool_call>\n{"tool": "list_files" "arguments": {"path": "."}}\n</tool_call>',
            '<tool_call>\n{"tool": "write_files", "arguments": {"path": "README.md", "contents": "Hello\n"}}\n</tool_call>',
            "<tool_call>\n{tool: 'list_files', arguments: {path: '.'}}\n</tool_call>",
            '<tool_call>\n{"tool": "edit_file", "arguments": {"path": "a.py", "old_string": "say "hi"", "new_string": "ok"}}\n</tool_call>',
        ]
        calls, _ = parse_tool_calls(samples[0])
        self.assertIsNone(calls[0].error, calls[0].error)
        self.assertEqual(calls[0].arguments["path"], ".")
        calls, _ = parse_tool_calls(samples[1])
        self.assertEqual(calls[0].arguments["contents"], "Hello\n")
        calls, _ = parse_tool_calls(samples[2])
        self.assertEqual(calls[0].tool, "list_files")
        calls, _ = parse_tool_calls(samples[3])
        self.assertEqual(calls[0].arguments["old_string"], 'say "hi"')

    def test_unescaped_quotes_in_source_text_are_kept(self) -> None:
        reply = (
            "<tool_call>\n"
            '{"tool":"write_files","arguments":{"path":"settings.gradle.kts","contents":'
            '"rootProject.name = "test-app"\\ninclude(":app")\\n"}}\n'
            "</tool_call>"
        )
        calls, unclosed = parse_tool_calls(reply)
        self.assertFalse(unclosed)
        self.assertIsNone(calls[0].error, calls[0].error)
        self.assertEqual(
            calls[0].arguments["contents"],
            'rootProject.name = "test-app"\ninclude(":app")\n',
        )

    def test_a_quote_before_a_brace_stays_inside_the_string(self) -> None:
        reply = (
            "<tool_call>\n"
            '{"tool":"write_files","arguments":{"path":"MainActivity.kt","contents":'
            '"setContentView(TextView(this).apply { text = "Hello" })\\n"}}\n'
            "</tool_call>"
        )
        calls, _ = parse_tool_calls(reply)
        self.assertIsNone(calls[0].error, calls[0].error)
        self.assertEqual(
            calls[0].arguments["contents"],
            'setContentView(TextView(this).apply { text = "Hello" })\n',
        )

    def test_windows_path_keeps_the_backslash(self) -> None:
        reply = (
            "<tool_call>\n"
            '{"tool": "run_command", "arguments": {"command": ".gradle-8.5\\gradle-8.5\\bin\\gradle.bat help"}}\n'
            "</tool_call>"
        )
        calls, unclosed = parse_tool_calls(reply)
        self.assertFalse(unclosed)
        self.assertIsNone(calls[0].error, calls[0].error)
        self.assertEqual(
            calls[0].arguments["command"],
            ".gradle-8.5\\gradle-8.5\\bin\\gradle.bat help",
        )

    def test_accepts_name_and_args(self) -> None:
        calls, unclosed = parse_tool_calls('<tool_call>\n{"name": "git_status", "args": {}}\n</tool_call>')
        self.assertFalse(unclosed)
        self.assertEqual(calls[0].tool, "git_status")
        self.assertEqual(calls[0].arguments, {})

    def test_flat_arguments_are_accepted(self) -> None:
        calls, _ = parse_tool_calls('<tool_call>{"tool": "read_files", "paths": ["a.py"]}</tool_call>')
        self.assertEqual(calls[0].arguments, {"paths": ["a.py"]})

    def test_fenced_json_is_a_tool_call(self) -> None:
        calls, unclosed = parse_tool_calls('```json\n{"tool": "git_status", "arguments": {}}\n```')
        self.assertFalse(unclosed)
        self.assertEqual(calls[0].tool, "git_status")

    def test_rendered_code_block_chrome_is_a_tool_call(self) -> None:
        reply = 'json\nCopy code\n{"tool": "list_files", "arguments": {"path": "src"}}'
        calls, unclosed = parse_tool_calls(reply)
        self.assertFalse(unclosed)
        self.assertEqual(calls[0].tool, "list_files")
        calls, _ = parse_tool_calls('Here is some JSON: {"tool": "hammer", "x": 1}')
        self.assertEqual(calls, [])

    def test_unquoted_tool_json_is_a_call(self) -> None:
        reply = '{tool:"write_file", arguments:{path:"notes.txt", contents:"hello"}}'
        calls, unclosed = parse_tool_calls(reply)
        self.assertFalse(unclosed)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0].error)
        self.assertEqual(canonical_tool(calls[0].tool), "write_files")
        self.assertEqual(calls[0].arguments["path"], "notes.txt")
        self.assertEqual(calls[0].arguments["contents"], "hello")
        visible = commentary("Writing the notes.\n" + reply)
        self.assertIn("Writing the notes.", visible)
        self.assertNotIn("write_file", visible)
        self.assertNotIn("notes.txt", visible)
        quoted, _ = parse_tool_calls('{"tool": "git_status", "arguments": {}}')
        self.assertEqual(quoted[0].tool, "git_status")
        unknown, _ = parse_tool_calls('{tool:"hammer", arguments:{x:1}}')
        self.assertEqual(unknown, [])

    def test_tag_inside_a_string_is_not_a_cut_off_block(self) -> None:
        reply = _edit("README.md", "old", "The model emits <tool_call> blocks.")
        calls, unclosed = parse_tool_calls(reply)
        self.assertFalse(unclosed)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0].error)
        calls, _ = parse_tool_calls(_call("git_status") + "\n<tool_call>")
        self.assertEqual(len(calls), 1)

    def test_trailing_comma_and_cut_off_tail_keep_the_good_call(self) -> None:
        calls, unclosed = parse_tool_calls('<tool_call>\n{"tool": "git_status", "arguments": {},}\n</tool_call>')
        self.assertFalse(unclosed)
        self.assertIsNone(calls[0].error)
        calls, unclosed = parse_tool_calls(
            _call("list_files", path=".") + '\n<tool_call>\n{"tool": "read_files", "arguments": {"paths": ["a.py"]}}\n'
        )
        self.assertFalse(unclosed)
        self.assertEqual(calls[0].tool, "list_files")
        self.assertIn("cut off", calls[-1].error or "")


class CommandArgvTests(unittest.TestCase):
    def test_powershell_and_bash(self) -> None:
        import re

        def embedded(argv: list[str]) -> tuple[str, str]:
            script = base64.b64decode(argv[-1]).decode("utf-16-le")
            command = re.search(r"FromBase64String\('([^']*)'\)", script)
            return script, base64.b64decode(command.group(1)).decode("utf-8") if command else ""

        windows = command_argv("Get-Location", platform_name="win32")
        self.assertTrue(windows[0].lower().endswith(("powershell.exe", "pwsh.exe")))
        self.assertEqual(windows[1:7], ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand"])
        script, command = embedded(windows)
        self.assertEqual(command, "Get-Location")
        self.assertIn("LASTEXITCODE", script)
        self.assertIn("UTF8Encoding", script)
        bash = Shell("bash", "/bin/bash", "bash")
        self.assertEqual(shell_command_argv("pwd", shell=bash), ["/bin/bash", "-c", "pwd"])
        legacy = Shell("powershell", "powershell.exe", "Windows PowerShell 5.1 (powershell.exe)")
        script, _ = embedded(shell_command_argv("gradlew.bat tasks", platform_name="win32", shell=legacy))
        self.assertIn("$global:LASTEXITCODE = $null", script)
        self.assertIn("$PSNativeCommandUseErrorActionPreference = $false", script)
        self.assertNotIn("$ErrorActionPreference = 'Stop'", script)
        placed = command_argv("Get-Location", platform_name="win32", cwd=Path(r"D:\work\app"))
        _, placed_command = embedded(placed)
        self.assertIn(r"Set-Location -LiteralPath 'D:\work\app'", placed_command)
        self.assertIn("Get-Location", placed_command)


class SeedMessageTests(unittest.TestCase):
    def test_shell_leads_the_system_prompt(self) -> None:
        root = Path(".")
        powershell = Shell("powershell", "powershell.exe", "Windows PowerShell 5.1 (powershell.exe)")
        text = seed_message(root, "INSTRUCTIONS", shell=powershell, platform_name="win32")
        self.assertTrue(text.startswith("SHELL: Windows PowerShell 5.1 (powershell.exe)."))
        self.assertLess(text.index("SHELL:"), text.index("INSTRUCTIONS"))
        self.assertLess(text.index("INSTRUCTIONS"), text.index("ENVIRONMENT"))
        self.assertIn("a top-level && or || is converted for you", text)
        self.assertNotIn("do not work", text)
        self.assertNotIn("does not carry over", text)
        self.assertIn("cd persists", text)

        pwsh = Shell("pwsh", "pwsh.exe", "PowerShell 7 (pwsh.exe)")
        modern = seed_message(root, "", shell=pwsh, platform_name="win32")
        self.assertTrue(modern.startswith("SHELL: PowerShell 7 (pwsh.exe)."))
        self.assertIn("Reply with exactly READY.", modern)

        bash = seed_message(root, "INSTRUCTIONS", shell=Shell("bash", "bash", "bash -lc"), platform_name="linux")
        self.assertTrue(bash.startswith("SHELL: bash -lc."))

    def test_tool_hints_report_gradle_java_and_android(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gradlew.bat").write_text("@echo off\n", encoding="utf-8")
            (root / "settings.gradle").write_text("include ':app'\n", encoding="utf-8")
            app = root / "app"
            app.mkdir()
            (app / "build.gradle").write_text("plugins { id 'com.android.application' }\n", encoding="utf-8")
            wrapper = root / "gradle" / "wrapper"
            wrapper.mkdir(parents=True)
            (wrapper / "gradle-wrapper.properties").write_text(
                "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.7-bin.zip\n",
                encoding="utf-8",
            )

            def find(name: str) -> str | None:
                if name == "gradle.bat":
                    return r"C:\Gradle\bin\gradle.bat"
                if name == "java.exe":
                    return r"C:\Java\bin\java.exe"
                return None

            hints = "\n".join(
                tool_hints(
                    root,
                    "win32",
                    which=find,
                    environ={"JAVA_HOME": r"C:\Java", "ANDROID_HOME": r"C:\Android\Sdk"},
                )
            )
            self.assertIn("gradle wrapper: .\\gradlew.bat", hints)
            self.assertIn("https://services.gradle.org/distributions/gradle-8.7-bin.zip", hints)
            self.assertIn(r"gradle on PATH: C:\Gradle\bin\gradle.bat", hints)
            self.assertIn(r"java: JAVA_HOME=C:\Java", hints)
            self.assertIn(r"android sdk: C:\Android\Sdk", hints)
            self.assertIn("android project: yes", hints)

            missing = "\n".join(tool_hints(root, "win32", which=lambda name: None, environ={}))
            self.assertIn("gradle on PATH: not installed", missing)
            self.assertIn("java: not installed", missing)
            self.assertIn("android sdk: not found", missing)


class ToolTests(unittest.TestCase):
    def test_write_read_edit_delete_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrote = execute_tool("write_files", {"path": "src/app.py", "contents": "VALUE = 1\n"}, workspace=root)
            self.assertTrue(wrote["ok"], wrote)
            self.assertIn("syntax ok", wrote["output"])
            listed = execute_tool("list_files", {"path": "src"}, workspace=root)
            self.assertIn("src/app.py", listed["output"])
            read = execute_tool("read_files", {"paths": ["src/app.py"]}, workspace=root)
            self.assertIn("1|VALUE = 1", read["output"])
            edited = execute_tool(
                "edit_file",
                {"path": "src/app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"},
                workspace=root,
            )
            self.assertTrue(edited["ok"], edited)
            self.assertIn("+VALUE = 2", edited["output"])
            self.assertEqual((root / "src" / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n")
            deleted = execute_tool("delete_file", {"path": "src/app.py"}, workspace=root)
            self.assertTrue(deleted["ok"], deleted)
            self.assertFalse((root / "src" / "app.py").exists())
            refused = execute_tool("delete_file", {"path": "."}, workspace=root)
            self.assertFalse(refused["ok"])

    def test_run_command_echo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = execute_tool("run_command", {"command": "printf 'pong'"}, workspace=Path(tmp))
            self.assertTrue(result["ok"], result)
            self.assertIn("pong", result["output"])
            self.assertIn("exit 0", result["output"])
            self.assertIn(f"cwd: {Path(tmp).resolve()}", result["output"])

    def test_run_command_returns_powershell_text(self) -> None:
        class Proc:
            returncode = 1
            stdout = "pong".encode("utf-16")
            stderr = (
                "#< CLIXML\n"
                '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
                '<S S="Error">fatal: not a git repository_x000D__x000A_</S></Objs>'
            ).encode("utf-16")

        def runner(argv, **kwargs):
            del argv, kwargs
            return Proc()

        result = execute_tool("run_command", {"command": "Write-Output pong"}, workspace=Path("."), runner=runner)
        self.assertFalse(result["ok"])
        self.assertIn("pong", result["output"])
        self.assertIn("fatal: not a git repository", result["output"])
        self.assertNotIn("CLIXML", result["output"])
        self.assertIn("exit 1", result["output"])

    def test_git_status_diff_and_apply_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for argv in (
                ["git", "init"],
                ["git", "config", "user.email", "bot@example.com"],
                ["git", "config", "user.name", "bot"],
            ):
                subprocess.run(argv, cwd=root, check=True, capture_output=True)
            (root / "note.txt").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "note.txt"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            (root / "note.txt").write_text("hello\nworld\n", encoding="utf-8")
            status = execute_tool("git_status", {}, workspace=root)
            self.assertIn("note.txt", status["output"])
            diff = execute_tool("git_diff", {"path": "note.txt"}, workspace=root)
            self.assertIn("+world", diff["output"])
            (root / "note.txt").write_text("hello\n", encoding="utf-8")
            patch = "diff --git a/note.txt b/note.txt\n--- a/note.txt\n+++ b/note.txt\n@@ -1 +1,2 @@\n hello\n+world\n"
            applied = execute_tool("apply_patch", {"patch": patch}, workspace=root)
            self.assertTrue(applied["ok"], applied)
            self.assertIn("world", (root / "note.txt").read_text(encoding="utf-8"))
            broken = execute_tool("apply_patch", {"patch": patch.replace(" hello", " nope")}, workspace=root)
            self.assertFalse(broken["ok"])
            self.assertIn("does not apply", broken["error"])

    def test_read_files_accepts_start_and_end_line(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("\n".join(f"line {i}" for i in range(10)) + "\n", encoding="utf-8")
        result = execute_tool("read_files", {"paths": ["note.txt"], "start_line": 3, "end_line": 5}, workspace=root)
        self.assertTrue(result["ok"], result)
        self.assertIn("3|line 2", result["output"])
        self.assertIn("5|line 4", result["output"])
        self.assertNotIn("6|line 5", result["output"])

    def test_read_files_windows_a_long_file_and_caps_limit(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "big.txt").write_text("\n".join(f"line {i}" for i in range(900)) + "\n", encoding="utf-8")
        result = execute_tool("read_files", {"paths": ["big.txt"]}, workspace=root)
        self.assertIn("1|line 0", result["output"])
        self.assertIn("next offset=201", result["output"])
        self.assertNotIn("201|line 200", result["output"])
        wide = execute_tool("read_files", {"paths": ["big.txt"], "limit": 5000}, workspace=root, max_chars=100_000)
        self.assertIn("limit capped at 400", wide["output"])
        self.assertNotIn("401|line 400", wide["output"])

    def test_read_of_an_unchanged_span_is_not_repeated(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        state = TaskState(task="t")
        first = execute_tool("read_files", {"path": "a.py"}, workspace=root, state=state)
        self.assertIn("1|x = 1", first["output"])
        second = execute_tool("read_files", {"path": "a.py"}, workspace=root, state=state)
        self.assertIn("unchanged since step", second["output"])
        forced = execute_tool("read_files", {"path": "a.py", "force": True}, workspace=root, state=state)
        self.assertIn("1|x = 1", forced["output"])

    def test_edit_rejects_a_whitespace_only_rewrite(self) -> None:
        root = Path(tempfile.mkdtemp())
        target = root / "a.py"
        target.write_text("def add():\n    return 1\n", encoding="utf-8")
        result = execute_tool(
            "edit_file",
            {"path": "a.py", "old_string": "def add():\n return 1\n", "new_string": "def add():\n return 1\n"},
            workspace=root,
        )
        self.assertFalse(result["ok"], result)
        self.assertIn("same text", result["error"])

    def test_edit_repairs_line_prefix_and_indent(self) -> None:
        root = Path(tempfile.mkdtemp())
        target = root / "a.py"
        target.write_text("def test_add_ones():\n    assert add(1, 1) == 2\n", encoding="utf-8")
        result = execute_tool(
            "edit_file",
            {
                "path": "a.py",
                "old_string": "1|def test_add_ones():\n2| assert add(1, 1) == 2\n",
                "new_string": "def test_add_ones():\n    assert add(1, 1) == 2\n\ndef extra():\n    return 5\n",
            },
            workspace=root,
        )
        self.assertTrue(result["ok"], result)
        self.assertIn("def extra", target.read_text(encoding="utf-8"))

    def test_edit_reindents_and_ignores_markdown(self) -> None:
        root = Path(tempfile.mkdtemp())
        java = root / "A.java"
        java.write_text("class A {\n    void f() {\n        run();\n    }\n}\n", encoding="utf-8")
        result = execute_tool(
            "edit_file",
            {"path": "A.java", "old_string": "void f() {\n    run();\n}", "new_string": "void f() {\n    run();\n    done();\n}"},
            workspace=root,
        )
        self.assertTrue(result["ok"], result)
        self.assertIn("        done();\n", java.read_text(encoding="utf-8"))
        readme = root / "README.md"
        readme.write_text("`bot-agent` is an alias for `--mode agent`.\n", encoding="utf-8")
        result = execute_tool(
            "edit_file",
            {"path": "README.md", "old_string": "bot-agent is an alias for --mode agent.", "new_string": "`bot-agent` runs the agent."},
            workspace=root,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(readme.read_text(encoding="utf-8"), "`bot-agent` runs the agent.\n")

    def test_edit_failure_shows_the_closest_text(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "a.py").write_text("def total(items):\n    return sum(items)\n", encoding="utf-8")
        result = execute_tool(
            "edit_file",
            {"path": "a.py", "old_string": "def totals(items):\n    return add(items)", "new_string": "x"},
            workspace=root,
        )
        self.assertFalse(result["ok"])
        self.assertIn("closest text", result["output"])
        self.assertIn("1|def total(items):", result["output"])

    def test_edit_keeps_crlf_and_bom(self) -> None:
        root = Path(tempfile.mkdtemp())
        target = root / "Win.kt"
        target.write_bytes(b"\xef\xbb\xbffun a() {\r\n    one()\r\n}\r\n")
        result = execute_tool("edit_file", {"path": "Win.kt", "old_string": "one()", "new_string": "two()"}, workspace=root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(target.read_bytes(), b"\xef\xbb\xbffun a() {\r\n    two()\r\n}\r\n")

    def test_edit_that_breaks_python_is_not_applied(self) -> None:
        root = Path(tempfile.mkdtemp())
        target = root / "a.py"
        target.write_text("def f():\n    return 1\n", encoding="utf-8")
        result = execute_tool("edit_file", {"path": "a.py", "old_string": "return 1", "new_string": "return (1"}, workspace=root)
        self.assertFalse(result["ok"])
        self.assertIn("syntax", result["error"])
        self.assertEqual(target.read_text(encoding="utf-8"), "def f():\n    return 1\n")

    def test_multi_edit_is_all_or_nothing(self) -> None:
        root = Path(tempfile.mkdtemp())
        target = root / "n.txt"
        target.write_text("alpha\nbeta\n", encoding="utf-8")
        failed = execute_tool(
            "edit_file",
            {"path": "n.txt", "edits": [{"old_string": "alpha", "new_string": "ALPHA"}, {"old_string": "gamma", "new_string": "G"}]},
            workspace=root,
        )
        self.assertFalse(failed["ok"])
        self.assertIn("edit 2 of 2", failed["error"])
        self.assertEqual(target.read_text(encoding="utf-8"), "alpha\nbeta\n")
        done = execute_tool(
            "edit_file",
            {"path": "n.txt", "edits": [{"old_string": "alpha", "new_string": "ALPHA"}, {"old_string": "beta", "new_string": "BETA"}]},
            workspace=root,
        )
        self.assertTrue(done["ok"], done)
        self.assertEqual(target.read_text(encoding="utf-8"), "ALPHA\nBETA\n")

    def test_write_files_will_not_clobber_an_unread_file(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "keep.txt").write_text("precious\n", encoding="utf-8")
        state = TaskState(task="t")
        refused = execute_tool("write_files", {"path": "keep.txt", "contents": "x\n"}, workspace=root, state=state)
        self.assertFalse(refused["ok"])
        self.assertIn("overwrite true", refused["error"])
        allowed = execute_tool(
            "write_files", {"path": "keep.txt", "contents": "x\n", "overwrite": True}, workspace=root, state=state
        )
        self.assertTrue(allowed["ok"], allowed)

    def test_find_files_and_recursive_glob(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "app" / "src" / "main").mkdir(parents=True)
        (root / "app" / "src" / "main" / "OrderService.kt").write_text("x\n", encoding="utf-8")
        (root / "Top.kt").write_text("x\n", encoding="utf-8")
        (root / "build").mkdir()
        (root / "build" / "Gen.kt").write_text("x\n", encoding="utf-8")
        found = execute_tool("find_files", {"glob": "*.kt"}, workspace=root)
        self.assertIn("app/src/main/OrderService.kt", found["output"])
        self.assertIn("Top.kt", found["output"])
        self.assertNotIn("Gen.kt", found["output"])
        deep = execute_tool("list_files", {"glob": "**/*Service.kt"}, workspace=root)
        self.assertIn("app/src/main/OrderService.kt", deep["output"])
        self.assertNotIn("Top.kt", deep["output"])
        tree = execute_tool("list_files", {"path": ".", "depth": 2}, workspace=root)
        self.assertIn("d app/ (1 files)", tree["output"])
        self.assertIn("d app/src/", tree["output"])

    def test_search_groups_hits_and_falls_back_to_literal(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "many.txt").write_text("needle\n" * 12, encoding="utf-8")
        (root / "one.txt").write_text("a needle here\n", encoding="utf-8")
        result = execute_tool("search_code", {"pattern": "needle"}, workspace=root)
        self.assertEqual(result["output"].count("many.txt:"), 5)
        self.assertIn("one.txt:1:", result["output"])
        self.assertIn("more hits", result["output"])
        (root / "call.py").write_text("value = compute(1)\n", encoding="utf-8")
        literal = execute_tool("search_code", {"pattern": "compute("}, workspace=root)
        self.assertIn("call.py:1:", literal["output"])
        self.assertIn("literal text", literal["output"])

    def test_run_command_lints_bash_on_windows_powershell(self) -> None:
        from critique_bot.agent_shell import Shell, lint_command

        legacy = Shell("powershell", "powershell.exe", "Windows PowerShell 5.1")
        modern = Shell("pwsh", "pwsh.exe", "PowerShell 7")
        self.assertIsNone(lint_command("npm ci && npm test", legacy, {}))  # rewritten when it runs
        self.assertIn("&&", lint_command("if ($x) { npm ci && npm test }", legacy, {}) or "")
        self.assertIsNone(lint_command("npm ci && npm test", modern))
        self.assertIn("export", lint_command("export FOO=1", legacy) or "")
        self.assertIn("Select-String", lint_command("git log | grep fix", legacy) or "")
        self.assertIsNone(lint_command("Get-ChildItem -Recurse -Filter *.kt", legacy))
        self.assertIsNone(lint_command("git log --format=%H -n 1", legacy))
        self.assertIsNone(lint_command("ls -la", Shell("bash", "bash", "bash -lc")))

    def test_long_command_output_keeps_head_and_tail(self) -> None:
        from critique_bot.agent_shell import head_tail, tidy

        text = "\n".join(f"line {i}" for i in range(1000))
        kept = head_tail(text)
        self.assertIn("line 0", kept)
        self.assertIn("line 999", kept)
        self.assertIn("lines omitted", kept)
        self.assertEqual(tidy("\x1b[32mok\x1b[0m\nsame\nsame\nsame"), "ok\nsame\n... previous line repeated 2 more times")

    def test_tool_count(self) -> None:
        self.assertEqual(len(ALLOWED_TOOLS), 21)

    def test_opencode_names_run_the_same_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            state = TaskState(task="edit")
            edited = execute_tool(
                "edit",
                {"filePath": "app.py", "oldString": "VALUE = 1", "newString": "VALUE = 2"},
                workspace=root,
                state=state,
            )
            self.assertTrue(edited["ok"], edited)
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n")
            found = execute_tool("grep", {"pattern": "VALUE", "include": "*.py"}, workspace=root)
            self.assertTrue(found["ok"], found)
            self.assertIn("app.py", found["output"])

    def test_todo_is_repeated_in_state(self) -> None:
        state = TaskState(task="ship")
        wrote = execute_tool(
            "todowrite",
            {"todos": [{"content": "read the code", "status": "in_progress"}, {"content": "run tests", "status": "pending"}]},
            workspace=Path("."),
            state=state,
        )
        self.assertTrue(wrote["ok"], wrote)
        self.assertIn("[in_progress] read the code", wrote["output"])
        again = execute_tool("todo", {}, workspace=Path("."), state=state)
        self.assertIn("run tests", again["output"])
        rendered = state.render("Todos: {todos}")
        self.assertIn("[in_progress] read the code", rendered)

    def test_skill_lists_and_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / ".bot" / "skills" / "release" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\ndescription: Cut a release\n---\nTag the commit.\n", encoding="utf-8")
            listed = execute_tool("skill", {}, workspace=root)
            self.assertIn("release (project): Cut a release", listed["output"])
            self.assertIn("aosp (built-in)", listed["output"])
            loaded = execute_tool("skill", {"name": "release"}, workspace=root)
            self.assertIn("Tag the commit.", loaded["output"])

    def test_git_log_uses_git_not_the_shell(self) -> None:
        class Proc:
            returncode = 0
            stdout = b"abc123 2026-10-05 fix the build\n"
            stderr = b""

        seen: list[list[str]] = []

        def runner(argv, **kwargs):
            del kwargs
            seen.append(argv)
            return Proc()

        result = execute_tool("git_log", {"limit": 5, "path": "src/app.py"}, workspace=Path("."), runner=runner)
        self.assertTrue(result["ok"], result)
        self.assertIn("abc123", result["output"])
        self.assertIn("log", seen[0])
        self.assertIn("--", seen[0])
        self.assertIn("src/app.py", seen[0])


class LoopTests(unittest.TestCase):
    def test_theme_command_is_not_sent_to_the_chat(self) -> None:
        root = Path(tempfile.mkdtemp())
        session = _Scripted(["DONE"])
        turns = _loop(session, root, "/theme")
        self.assertEqual(session.sent, [])
        self.assertEqual(turns, [])

    def test_edit_runs_without_a_plan_and_state_follows_each_result(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        session = _Scripted([_edit("note.txt", "one", "two"), "Done. note.txt now says two."])
        turns = _loop(session, root, "change note.txt")
        self.assertIn("change note.txt", session.sent[0])
        self.assertIn("tool_call", session.sent[0])
        self.assertIn("updated note.txt", session.sent[1])
        self.assertIn("STATE", session.sent[1])
        self.assertIn("Task: change note.txt", session.sent[1])
        self.assertIn("Changed: note.txt x1", session.sent[1])
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")
        self.assertEqual(turns[-1]["content"], "Done. note.txt now says two.")

    def test_a_plan_reply_is_acknowledged_and_the_write_follows(self) -> None:
        root = Path(tempfile.mkdtemp())
        session = _Scripted(
            [
                _call("list_files", path="."),
                "File: README.md\nChange: create README.md containing Hello\nCheck: read it back\n",
                _call("write_files", path="README.md", contents="Hello\n"),
                "DONE",
            ]
        )
        _loop(session, root, "create README.md with Hello")
        self.assertIn("Plan noted", session.sent[2])
        self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "Hello\n")

    def test_status_code_returns_to_the_same_session(self) -> None:
        root = Path(tempfile.mkdtemp())
        next_tasks = ["explain the title"]

        def reader() -> str | None:
            return next_tasks.pop(0) if next_tasks else None

        session = _Scripted([_call("list_files", path="."), "COMPLETED", "FINISHED"])
        outcome: list[str] = []
        _loop(session, root, "look at the files", read_message=reader, outcome=outcome)
        self.assertIn("explain the title", session.sent[2])
        self.assertEqual(outcome, ["FINISHED"])

    def test_completed_without_a_change_is_questioned_once(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("keep\n", encoding="utf-8")
        session = _Scripted([_edit("note.txt", "missing", "x"), "DONE", "DONE"])
        outcome: list[str] = []
        _loop(session, root, "change note.txt", outcome=outcome)
        self.assertIn("No file has changed", session.sent[2])
        self.assertEqual(outcome, ["DONE"])
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "keep\n")

    def test_failed_refusal_is_sent_back(self) -> None:
        session = _Scripted(
            [
                "BLOCKED — the repository tool interface does not expose run_command.",
                _call("list_files", path="."),
                "COMPLETED",
                "COMPLETED",
            ]
        )
        outcome: list[str] = []
        _loop(session, Path(tempfile.mkdtemp()), "create hello.txt", outcome=outcome)
        self.assertIn("list_files", session.sent[1])
        self.assertEqual(outcome, ["COMPLETED"])

    def test_failed_code_is_recorded(self) -> None:
        session = _Scripted(["FAILED", "FAILED: the file was not in the workspace"])
        outcome: list[str] = []
        _loop(session, Path("."), "do the task", max_rounds=4, outcome=outcome)
        self.assertIn("what failed", session.sent[1].lower())
        self.assertEqual(outcome, ["FAILED"])

    def test_seed_is_first_and_task_follows(self) -> None:
        # The instructions ride in front of the first task: one round trip, not two.
        session = _Scripted(["READY", "All set.", "All set.", "All set."])
        _loop(session, Path("."), "update the test cases", max_rounds=3, seed="INSTRUCTIONS\n\nReply with exactly READY.")
        self.assertTrue(session.sent[0].startswith("INSTRUCTIONS"))
        self.assertNotIn("READY", session.sent[0])
        self.assertIn("update the test cases", session.sent[0])
        self.assertIn("No tool_call block was found", session.sent[1])

    def test_unclosed_tool_call_is_not_executed(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("keep\n", encoding="utf-8")
        session = _Scripted(
            [
                '<tool_call>\n{"tool": "delete_file", "arguments": {"path": "note.txt"}}',
                "FAILED: the delete call was cut off before it ran",
            ],
            detail={"completion": COMPLETION_IDLE},
        )
        _loop(session, root, "delete it", max_rounds=4)
        self.assertTrue((root / "note.txt").exists())
        self.assertIn("truncated", session.sent[1])

    def test_refusal_without_a_tool_call_is_sent_back(self) -> None:
        session = _Scripted(
            ["I can't modify files because tools aren't available.", _call("list_files", path="."), "Done."]
        )
        _loop(session, Path(tempfile.mkdtemp()), "look around", max_rounds=4)
        self.assertIn("list_files", session.sent[1])

    def test_refusals_are_capped_and_end_as_failed(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        refusal = "I can't issue a repository tool call because no file-operation tool is exposed."
        session = _Scripted([_call("read_files", path="note.txt")] + [refusal] * 4)
        outcome: list[str] = []
        _loop(session, root, "replace one with two in note.txt", outcome=outcome)
        self.assertEqual(outcome, ["FAILED"])
        self.assertEqual(session._replies, [])
        self.assertIn('"path": "note.txt"', session.sent[2])

    def test_a_question_is_answered_without_a_tool_call(self) -> None:
        shown: list[str] = []
        session = _Scripted(["2 + 2 is 4."])
        outcome: list[str] = []
        _loop(session, Path(tempfile.mkdtemp()), "what is 2 + 2?", outcome=outcome, emit=shown.append)
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertEqual(len(session.sent), 1)
        self.assertIn("2 + 2 is 4", shown[0])

    def test_a_question_mark_in_the_answer_still_finishes(self) -> None:
        session = _Scripted(["It multiplies price by the rate. Should the rate be a percent?"])
        outcome: list[str] = []
        _loop(session, Path(tempfile.mkdtemp()), "how does the discount work?", outcome=outcome)
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertEqual(len(session.sent), 1)

    def test_a_promise_to_look_asks_for_the_answer(self) -> None:
        session = _Scripted(["Let me read the file.", "It says keep."])
        outcome: list[str] = []
        _loop(session, Path(tempfile.mkdtemp()), "what does note.txt say?", outcome=outcome)
        self.assertIn("answer in words", session.sent[1].lower())
        self.assertEqual(outcome, ["COMPLETED"])

    def test_a_question_can_read_and_then_answer(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("keep\n", encoding="utf-8")
        session = _Scripted(
            [
                _call("read_files", path="note.txt"),
                "It says keep. Should I stop there?",
            ]
        )
        outcome: list[str] = []
        _loop(session, root, "what does note.txt say?", outcome=outcome)
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertEqual(len(session.sent), 2)
        self.assertIn("keep", session.sent[1])

    def test_answer_and_edit_in_one_reply_both_happen(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        shown: list[str] = []
        session = _Scripted(
            [
                "note.txt said one. It now says two.\n"
                + _edit("note.txt", "one", "two")
                + '\n<tool_result>\n{"tool": "edit_file", "ok": true}\n</tool_result>',
                "COMPLETED",
            ]
        )
        outcome: list[str] = []
        _loop(
            session,
            root,
            "what did note.txt say? also change one to two",
            outcome=outcome,
            emit=shown.append,
        )
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")
        self.assertTrue(any("said one" in item for item in shown))
        self.assertFalse(any("<tool_call>" in item or "<tool_result>" in item for item in shown))
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertNotIn("No tool_call", session.sent[1])

    def test_question_after_a_read_is_sent_back_until_a_tool(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        session = _Scripted(
            [
                _call("read_files", paths=["note.txt"]),
                "I have the relevant code context now. What would you like me to change?",
                "The edit tools aren't available in my current tool set.",
                _edit("note.txt", "one", "two"),
                "Done. Would you like me to change anything else?",
            ]
        )
        outcome: list[str] = []
        _loop(session, root, "replace one with two in note.txt", outcome=outcome)
        self.assertIn("what to change", session.sent[2].lower())
        self.assertIn("unavailable", session.sent[3].lower())
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")
        self.assertEqual(outcome, ["COMPLETED"])

    def test_no_edit_needed_ends_the_task(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("keep\n", encoding="utf-8")
        session = _Scripted(
            [
                _edit("note.txt", "keep", "keep"),
                "The line is already present. No edit needed.",
                _edit("note.txt", "keep", "changed"),
            ]
        )
        _loop(session, root, "make sure note.txt says keep")
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(len(session._replies), 1)

    def test_repeated_failing_edit_gets_the_file_region(self) -> None:
        root = Path(tempfile.mkdtemp())
        body = "\n".join(f"line {i}" for i in range(40)) + "\nreturn total(items)\n"
        (root / "a.txt").write_text(body, encoding="utf-8")
        bad = _edit("a.txt", "return totals(item, extra)", "x")
        session = _Scripted([bad, bad, bad, "FAILED: old_string was not in a.txt"])
        _loop(session, root, "fix a.txt")
        self.assertIn("failed 2 times", session.sent[2])
        self.assertIn("read the closest region", session.sent[3])
        self.assertIn("41|return total(items)", session.sent[3])

    def test_identical_write_is_not_a_change_on_disk(self) -> None:
        root = Path(tempfile.mkdtemp())
        cache = root / ".bot" / "cache"
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        session = _Scripted(
            [
                _call("write_files", path="note.txt", contents="one\n", overwrite=True),
                "COMPLETED",
                "COMPLETED",
            ]
        )
        outcome: list[str] = []
        _loop(session, root, "change note.txt", outcome=outcome, cache_dir=cache)
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "one\n")
        self.assertIn("No file has changed", session.sent[2])
        self.assertEqual(outcome, ["COMPLETED"])

    def test_finish_diff_is_the_bytes_on_disk(self) -> None:
        from critique_bot.agent_edit import Checkpoints

        root = Path(tempfile.mkdtemp())
        cache = root / ".bot" / "cache"
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        session = _Scripted([_edit("note.txt", "one", "two"), "COMPLETED"])
        _loop(session, root, "change note.txt", cache_dir=cache)
        tasks = sorted(item for item in (cache / "undo").iterdir() if item.is_dir())
        checkpoints = Checkpoints(cache, root)
        checkpoints.task_dir = tasks[-1]
        diff = checkpoints.disk_diff() or ""
        self.assertIn("-one", diff)
        self.assertIn("+two", diff)
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")

    def test_check_command_failure_is_sent_back(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        (root / "flag").write_text("bad\n", encoding="utf-8")
        session = _Scripted(
            [
                _edit("note.txt", "one", "two"),
                "COMPLETED",
                _call("write_files", path="flag", contents="good\n", overwrite=True),
                "COMPLETED",
            ]
        )
        outcome: list[str] = []
        _loop(session, root, "change note.txt", outcome=outcome, check_command="grep -q good flag")
        self.assertIn("check above ran after your edits and failed", session.sent[2])
        self.assertEqual(outcome, ["COMPLETED"])

    def test_undo_restores_the_last_task(self) -> None:
        from critique_bot.agent_edit import undo_last

        root = Path(tempfile.mkdtemp())
        cache = root / ".bot" / "cache"
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        session = _Scripted([_edit("note.txt", "one", "two"), _call("write_files", path="new.txt", contents="n\n"), "COMPLETED"])
        _loop(session, root, "change note.txt", cache_dir=cache)
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")
        restored = undo_last(cache, root)
        self.assertEqual(sorted(restored), ["new.txt", "note.txt"])
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "one\n")
        self.assertFalse((root / "new.txt").exists())


class ReplayTests(unittest.TestCase):
    """Shapes taken from real chatgpt.com sessions in .bot/sessions."""

    def test_backtick_markdown_edit_lands_first_time(self) -> None:
        root = Path(tempfile.mkdtemp())
        readme = root / "README.md"
        readme.write_text(
            "### Agent\n\n`bot-agent` is an alias for `--mode agent`. Later turns are the task, then tool results.\n",
            encoding="utf-8",
        )
        reply = _edit(
            "README.md",
            "bot-agent is an alias for --mode agent. Later turns are the task, then tool results.",
            "`bot-agent` is an alias for `--mode agent`. Later turns are the task, then tool results. "
            "The model emits <tool_call> blocks.",
        )
        session = _Scripted([reply, "COMPLETED"])
        outcome: list[str] = []
        _loop(session, root, "Add one sentence to the Agent section of README.md", outcome=outcome)
        self.assertIn("emits <tool_call> blocks", readme.read_text(encoding="utf-8"))
        self.assertNotIn("cut off", session.sent[1])
        self.assertEqual(outcome, ["COMPLETED"])


class _Restartable(_Scripted):
    """A scripted chat that can open a new conversation, like the browser session."""

    def __init__(self, replies: list[str], detail: dict | None = None) -> None:
        super().__init__(replies, detail)
        self.chat_starts: list[int] = []

    def new_chat(self) -> bool:
        self.chat_starts.append(len(self.sent))
        return True


class _Raising(_Scripted):
    """Replies may be exceptions, raised in place of a reply."""

    def send(self, prompt: str) -> str:
        self.sent.append(prompt)
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class _Proc:
    def __init__(self, code: int, out: bytes = b"") -> None:
        self.returncode = code
        self.stdout = out
        self.stderr = b""


class RobustParserTests(unittest.TestCase):
    def test_unescaped_quotes_and_newlines_in_long_content(self) -> None:
        reply = (
            '<tool_call>\n{"tool":"write_files","arguments":{"path":"a.py","contents":"'
            'print("a", "b")\nname = "x"\nif x: print("y")\n"}}\n</tool_call>'
        )
        calls, _ = parse_tool_calls(reply)
        self.assertIsNone(calls[0].error, calls[0].error)
        self.assertEqual(calls[0].arguments["contents"], 'print("a", "b")\nname = "x"\nif x: print("y")\n')
        edit = '<tool_call>{"tool":"edit_file","arguments":{"path":"a.py","old_string":"f("x")","new_string":"f("y", 2)"}}</tool_call>'
        calls, _ = parse_tool_calls(edit)
        self.assertEqual(calls[0].arguments, {"path": "a.py", "old_string": 'f("x")', "new_string": 'f("y", 2)'})

    def test_raw_windows_paths_keep_their_backslashes(self) -> None:
        calls, _ = parse_tool_calls('<tool_call>{"tool":"run_command","arguments":{"command":"C:\\new\\tools\\run.exe --x"}}</tool_call>')
        self.assertEqual(calls[0].arguments["command"], "C:\\new\\tools\\run.exe --x")
        calls, _ = parse_tool_calls('<tool_call>{"tool":"run_command","arguments":{"command":".\\gradlew.bat test"}}</tool_call>')
        self.assertEqual(calls[0].arguments["command"], ".\\gradlew.bat test")
        escaped, _ = parse_tool_calls('<tool_call>{"tool":"read_files","arguments":{"path":"C:\\\\new\\\\a.txt"}}</tool_call>')
        self.assertEqual(escaped[0].arguments["path"], "C:\\new\\a.txt")

    def test_triple_quoted_and_template_strings(self) -> None:
        reply = '<tool_call>\n{"tool":"write_files","arguments":{"path":"a.py","contents":"""def f():\n    return "x"\n"""}}\n</tool_call>'
        calls, _ = parse_tool_calls(reply)
        self.assertEqual(calls[0].arguments["contents"], 'def f():\n    return "x"\n')
        reply = '<tool_call>{"tool":"write_files","arguments":{"path":"a.js","contents":`const s = "q";\n`}}</tool_call>'
        calls, _ = parse_tool_calls(reply)
        self.assertEqual(calls[0].arguments["contents"], 'const s = "q";\n')

    def test_trailing_prose_and_missing_closing_braces(self) -> None:
        calls, _ = parse_tool_calls('<tool_call>\n{"tool":"read_files","arguments":{"path":"a.py"}} I will read it now.\n</tool_call>')
        self.assertEqual(calls[0].arguments, {"path": "a.py"})
        calls, _ = parse_tool_calls('<tool_call>\n{"tool":"read_files","arguments":{"path":"a.py"}\n</tool_call>')
        self.assertIsNone(calls[0].error, calls[0].error)
        self.assertEqual(calls[0].arguments, {"path": "a.py"})

    def test_other_call_shapes(self) -> None:
        shapes = [
            '{"tool":"read_files","arguments":"{\\"path\\": \\"a.py\\"}"}',
            '{"tool":"read_files","path":"a.py"}',
            '{"type":"function","function":{"name":"read_files","arguments":"{\\"path\\":\\"a.py\\"}"}}',
            '{"name":"read_files","parameters":{"path":"a.py"}}',
            '{"name":"read_files","input":{"path":"a.py"}}',
            '{"tool":"functions.read_files","arguments":{"path":"a.py"}}',
        ]
        for shape in shapes:
            calls, _ = parse_tool_calls("<tool_call>\n" + shape + "\n</tool_call>")
            self.assertEqual([(c.tool, c.arguments, c.error) for c in calls], [("read_files", {"path": "a.py"}, None)], shape)
        many = [
            '[{"tool":"read_files","arguments":{"path":"a.py"}},{"tool":"git_status","arguments":{}}]',
            '{"tool_calls":[{"function":{"name":"read_files","arguments":{"path":"a.py"}}},{"name":"git_status"}]}',
        ]
        for shape in many:
            calls, _ = parse_tool_calls("<tool_call>\n" + shape + "\n</tool_call>")
            self.assertEqual([c.tool for c in calls], ["read_files", "git_status"], shape)
        skill, _ = parse_tool_calls('<tool_call>{"tool":"skill","name":"release"}</tool_call>')
        self.assertEqual(skill[0].arguments, {"name": "release"})

    def test_xml_style_blocks(self) -> None:
        for reply in (
            '<tool_call name="read_files">{"path": "a.py"}</tool_call>',
            "<tool_call>\n<name>read_files</name>\n<arguments>{\"path\": \"a.py\"}</arguments>\n</tool_call>",
            '<tool_use>{"tool": "read_files", "arguments": {"path": "a.py"}}</tool_use>',
            '<function_call name="read_files">\n```json\n{"path": "a.py"}\n```\n</function_call>',
        ):
            calls, unclosed = parse_tool_calls(reply)
            self.assertFalse(unclosed)
            self.assertEqual([(c.tool, c.arguments) for c in calls], [("read_files", {"path": "a.py"})], reply)
        self.assertNotIn("read_files", commentary('Reading.\n<tool_call name="read_files">{"path": "a.py"}</tool_call>'))

    def test_copy_and_edit_chrome_inside_a_block(self) -> None:
        reply = '<tool_call>\njson\nCopy\nEdit\n{"tool":"read_files","arguments":{"path":"a.py"}}\n</tool_call>'
        calls, _ = parse_tool_calls(reply)
        self.assertEqual(calls[0].arguments, {"path": "a.py"})

    def test_a_block_missing_only_its_closing_tag_runs_when_the_reply_finished(self) -> None:
        reply = '<tool_call>\n{"tool": "git_status", "arguments": {}}'
        self.assertEqual(parse_tool_calls(reply), ([], True))
        calls, unclosed = parse_tool_calls(reply, complete=True)
        self.assertFalse(unclosed)
        self.assertEqual(calls[0].tool, "git_status")
        cut = '<tool_call>\n{"tool": "write_files", "arguments": {"path": "a", "contents": "abc'
        self.assertEqual(parse_tool_calls(cut, complete=True), ([], True))

    def test_quoted_json_is_not_a_call(self) -> None:
        for reply in (
            'The package.json contains:\n```json\n{"tool": "read_files", "arguments": {"path": "a.py"}}\n```',
            'For example:\n{"tool": "git_status", "arguments": {}}',
            'A bare JSON object such as {tool:"write_file"} is not a tool call.',
        ):
            self.assertEqual(parse_tool_calls(reply)[0], [], reply)
        self.assertEqual(parse_tool_calls('{"tool": "git_status", "arguments": {}}', allow_bare=False)[0], [])

    def test_provider_error_text(self) -> None:
        from critique_bot.agent import _provider_error

        self.assertIsNotNone(_provider_error(""))
        self.assertIsNotNone(_provider_error("Something went wrong. If this issue persists please contact us."))
        self.assertIsNotNone(_provider_error("You've reached our limit of messages per hour. Please try again later."))
        self.assertIsNotNone(_provider_error("There was an error generating a response"))
        self.assertIsNone(_provider_error("Something went wrong in the parser: " + "x" * 500))
        self.assertIsNone(_provider_error(_call("git_status") + " network error"))
        self.assertIsNone(_provider_error("COMPLETED"))


class RobustLoopTests(unittest.TestCase):
    def test_truncated_call_asks_for_only_that_call(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        cut = '<tool_call>\n{"tool": "write_files", "arguments": {"path": "big.txt", "contents": "abc'
        session = _Scripted(
            [
                _call("read_files", path="note.txt") + "\n" + cut,
                cut,
                _call("write_files", path="big.txt", contents="abc\n"),
                "COMPLETED",
            ]
        )
        _loop(session, root, "create big.txt")
        self.assertIn("1|one", session.sent[1])
        self.assertIn('"tool": "write_files"', session.sent[1])
        self.assertIn("truncated", session.sent[1])
        self.assertIn("for write_files (path big.txt)", session.sent[2])
        self.assertIn("resend only that one call", session.sent[2])
        self.assertEqual((root / "big.txt").read_text(encoding="utf-8"), "abc\n")

    def test_fabricated_result_cuts_the_reply(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        fake = '\n<tool_result>\n{"tool": "read_files", "ok": true, "output": "1|zero"}\n</tool_result>\n'
        session = _Scripted(
            [
                _call("read_files", path="note.txt") + fake + _edit("note.txt", "zero", "two"),
                fake + "COMPLETED",
                _edit("note.txt", "one", "two"),
                "COMPLETED",
            ]
        )
        outcome: list[str] = []
        _loop(session, root, "change note.txt", outcome=outcome)
        self.assertIn("1|one", session.sent[1])
        self.assertIn("Only the program writes tool_result", session.sent[1])
        self.assertNotIn("edit_file", session.sent[1])
        self.assertIn("Only the program writes tool_result", session.sent[2])
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")
        self.assertEqual(outcome, ["COMPLETED"])

    def test_identical_failing_command_is_not_run_a_third_time(self) -> None:
        runs: list[object] = []

        def runner(argv, **kwargs):
            runs.append(argv)
            return _Proc(1, b"boom")

        cmd = _call("run_command", command="make build")
        session = _Scripted([cmd, cmd, cmd, "FAILED: make build fails with boom"])
        _loop(session, Path(tempfile.mkdtemp()), "fix the build", runner=runner)
        self.assertEqual(len(runs), 2)
        self.assertIn("not run again", session.sent[3])
        self.assertIn("nothing has changed since", session.sent[3])

    def test_identical_read_is_not_run_a_third_time(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "a.txt").write_text("alpha\n", encoding="utf-8")
        read = _call("read_files", path="a.txt")
        session = _Scripted([read, read, read + _call("list_files", path="."), "It says alpha."])
        _loop(session, root, "look at a.txt and summarise it")
        self.assertIn("not run again: this exact call already ran 2 times", session.sent[3])
        self.assertIn("a.txt", session.sent[3])

    def test_a_loop_of_identical_successful_calls_is_broken(self) -> None:
        runs: list[object] = []

        def runner(argv, **kwargs):
            runs.append(argv)
            return _Proc(0, b"hi")

        cmd = _call("run_command", command="echo hi")
        session = _Scripted([cmd, cmd, cmd, "COMPLETED"])
        _loop(session, Path(tempfile.mkdtemp()), "run echo", runner=runner)
        self.assertEqual(len(runs), 2)
        self.assertIn("same tool calls 3 times", session.sent[3])

    def test_check_reruns_every_time_and_fails_only_after_the_cap(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        session = _Scripted([_edit("note.txt", "one", "two"), "COMPLETED", "COMPLETED", "COMPLETED"])
        outcome: list[str] = []
        _loop(session, root, "change note.txt", outcome=outcome, check_command="echo run >> runs.log; false")
        self.assertEqual((root / "runs.log").read_text(encoding="utf-8").count("run"), 3)
        self.assertEqual(outcome, ["FAILED"])
        self.assertEqual(len(session.sent), 4)

    def test_check_runs_in_the_workspace_and_keeps_the_session_cwd(self) -> None:
        import critique_bot.agent as agent

        root = Path(tempfile.mkdtemp()).resolve()
        (root / "sub").mkdir()
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        session = _Scripted([_call("run_command", command="cd sub"), _edit("note.txt", "one", "two"), "COMPLETED"])
        outcome: list[str] = []
        shell_session = agent._open_shell_session(root, agent.agent_shell.detect_shell(), None)
        if shell_session is None:
            self.skipTest("no persistent shell session")
        self.addCleanup(shell_session.close)
        _loop(session, root, "change note.txt", outcome=outcome, check_command="pwd > where.txt",
              session_shell=shell_session)
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertTrue((root / "where.txt").is_file())
        self.assertFalse((root / "sub" / "where.txt").exists())
        self.assertEqual(Path(shell_session.cwd).resolve(), root / "sub")

    def test_check_uses_the_check_timeout(self) -> None:
        from unittest import mock

        import critique_bot.agent as agent

        seen: list[dict] = []
        real = agent._execute

        def spy(name, arguments, ctx):
            if name == "run_command":
                seen.append(dict(arguments))
            return real(name, arguments, ctx)

        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        with mock.patch.object(agent, "_execute", spy):
            _loop(_Scripted([_edit("note.txt", "one", "two"), "COMPLETED"]), root, "change note.txt", check_command="true")
            self.assertEqual(seen[-1]["timeout"], 600)
            (root / "note.txt").write_text("one\n", encoding="utf-8")
            _loop(
                _Scripted([_edit("note.txt", "one", "two"), "COMPLETED"]),
                root,
                "change note.txt",
                check_command="true",
                settings={"check_timeout": 900},
            )
            self.assertEqual(seen[-1]["timeout"], 900)

    def test_verify_is_asked_once_when_tests_exist(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "tests").mkdir()
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        session = _Scripted([_edit("note.txt", "one", "two"), "COMPLETED", "COMPLETED"])
        outcome: list[str] = []
        _loop(session, root, "change note.txt", outcome=outcome)
        self.assertIn("ran nothing to check them", session.sent[2])
        self.assertEqual(outcome, ["COMPLETED"])

    def test_seed_runs_only_read_only_calls_under_any_name(self) -> None:
        root = Path(tempfile.mkdtemp())
        session = _Scripted(
            [
                _call("write_file", path="x.txt", contents="x")
                + _call("create_file", path="y.txt", contents="y")
                + _call("bash", command="touch z.txt")
                + _call("list_files", path="."),
                "READY",
                "2 + 2 is 4.",
            ]
        )
        _loop(session, root, "what is 2 + 2?", seed="INSTRUCTIONS", seed_first=True)
        self.assertEqual(session.sent[0], "INSTRUCTIONS")
        for name in ("x.txt", "y.txt", "z.txt"):
            self.assertFalse((root / name).exists(), name)
        self.assertEqual(session.sent[1].count("no task yet"), 3)
        self.assertIn('"tool": "list_files", "ok": true', session.sent[1])

    def test_json_quoted_in_an_answer_is_not_run(self) -> None:
        root = Path(tempfile.mkdtemp())
        reply = (
            "The config file has a single entry that names a tool and its arguments, as shown below:\n"
            '{"tool": "write_files", "arguments": {"path": "x.txt", "contents": "x"}}\n'
            "Nothing else is in it."
        )
        session = _Scripted([reply])
        outcome: list[str] = []
        _loop(session, root, "what is in the config file?", outcome=outcome)
        self.assertFalse((root / "x.txt").exists())
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertEqual(len(session.sent), 1)

    def test_chat_page_errors_are_retried_not_refused(self) -> None:
        from unittest import mock

        import critique_bot.agent as agent
        from critique_bot.chat_client import ChatError

        sleeps: list[float] = []
        session = _Raising(
            ["Something went wrong. If this issue persists please contact us.", "", ChatError("no assistant message appeared"), "2 + 2 is 4."]
        )
        outcome: list[str] = []
        with mock.patch.object(agent, "_sleep", sleeps.append):
            _loop(session, Path(tempfile.mkdtemp()), "what is 2 + 2?", outcome=outcome)
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertEqual(len(set(session.sent)), 1)
        self.assertEqual(sleeps, list(agent.RETRY_DELAYS))

    def test_chat_page_errors_end_the_task_after_the_retries(self) -> None:
        from unittest import mock

        import critique_bot.agent as agent

        tasks = ["what is 3 + 3?"]
        session = _Scripted(["Network error", "Network error", "6."])
        outcome: list[str] = []
        with mock.patch.object(agent, "_sleep", lambda _s: None):
            _loop(
                session,
                Path(tempfile.mkdtemp()),
                "what is 2 + 2?",
                outcome=outcome,
                settings={"reply_retries": 1},
                read_message=lambda: tasks.pop(0) if tasks else None,
            )
        self.assertEqual(outcome, ["COMPLETED"])
        self.assertIn("3 + 3", session.sent[2])

    def test_long_chat_moves_to_a_new_chat_with_a_summary(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        session = _Restartable(
            [
                "Looking. " + "x" * 11_000 + "\n" + _call("read_files", path="note.txt"),
                _edit("note.txt", "one", "two"),
                "COMPLETED",
            ]
        )
        _loop(
            session,
            root,
            "change note.txt",
            seed="INSTRUCTIONS\n\nReply with exactly READY.",
            settings={"compact_after_chars": 10_000},
        )
        self.assertEqual(session.chat_starts, [1])
        resumed = session.sent[1]
        self.assertTrue(resumed.startswith("INSTRUCTIONS"))
        self.assertNotIn("exactly READY", resumed)
        self.assertIn("CONTINUING IN A NEW CHAT", resumed)
        self.assertIn("Task: change note.txt", resumed)
        self.assertIn("Files read: note.txt", resumed)
        self.assertIn("1|one", resumed)
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")

    def test_protocol_amnesia_moves_to_a_new_chat(self) -> None:
        refusal = "I can't do that because the tools aren't available."
        session = _Restartable([_call("list_files", path="."), refusal, refusal, "COMPLETED"])
        _loop(session, Path(tempfile.mkdtemp()), "tidy the folder listing", seed="INSTRUCTIONS")
        self.assertEqual(session.chat_starts, [3])
        self.assertIn("CONTINUING IN A NEW CHAT", session.sent[3])
        self.assertTrue(session.sent[3].startswith("INSTRUCTIONS"))

    def test_new_command_starts_a_fresh_chat(self) -> None:
        tasks = ["/new", "what is 3 + 3?"]
        session = _Restartable(["4.", "6."])
        _loop(
            session,
            Path(tempfile.mkdtemp()),
            "what is 2 + 2?",
            seed="INSTRUCTIONS",
            read_message=lambda: tasks.pop(0) if tasks else None,
        )
        self.assertEqual(session.chat_starts, [1])
        self.assertTrue(session.sent[1].startswith("INSTRUCTIONS"))
        self.assertIn("3 + 3", session.sent[1])

    def test_repo_map_is_sent_once_per_chat(self) -> None:
        from unittest import mock

        import critique_bot.agent as agent

        tasks = ["what is 3 + 3?"]
        session = _Scripted(["4.", "6."])
        with mock.patch.object(agent, "_prepare_index", lambda *_args: "MAP-TEXT"):
            _loop(session, Path(tempfile.mkdtemp()), "what is 2 + 2?", read_message=lambda: tasks.pop(0) if tasks else None)
        self.assertIn("MAP-TEXT", session.sent[0])
        self.assertNotIn("MAP-TEXT", session.sent[1])

    def test_a_real_question_goes_to_the_user(self) -> None:
        asked: list[str] = []

        def ask(question: str) -> str:
            asked.append(question)
            return "SQLite"

        session = _Scripted(["Which backend should the cache use: Redis or SQLite?", "COMPLETED", "COMPLETED"])
        _loop(session, Path(tempfile.mkdtemp()), "add caching to the service", ask_user=ask, approve_mode="ask")
        self.assertEqual(len(asked), 1)
        self.assertIn("The user answered your question", session.sent[1])
        self.assertIn("SQLite", session.sent[1])

    def test_a_denied_call_is_reported_with_the_reason(self) -> None:
        from unittest import mock

        import critique_bot.agent as agent

        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        session = _Scripted([_edit("note.txt", "one", "two"), "FAILED: the user denied the edit"])
        with mock.patch.object(agent, "_approve_prompt", lambda _perm: ("no", "edit b.txt instead")):
            _loop(session, root, "change note.txt", approve_mode="ask")
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "one\n")
        self.assertIn("denied by the user: edit b.txt instead", session.sent[1])
        self.assertIn("The user denied that call", session.sent[1])

    def test_a_denied_call_is_not_asked_twice_and_denials_end_the_task(self) -> None:
        from unittest import mock

        import critique_bot.agent as agent

        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        asked: list[object] = []

        def deny(perm):
            asked.append(perm)
            return ("no", "do not touch note.txt")

        session = _Scripted(
            [_edit("note.txt", "one", "two"), _edit("note.txt", "one", "two"), _edit("note.txt", "one", "three"), "COMPLETED"]
        )
        outcome: list[str] = []
        with mock.patch.object(agent, "_approve_prompt", deny):
            _loop(session, root, "change note.txt", approve_mode="ask", outcome=outcome)
        self.assertEqual(len(asked), 2)
        self.assertIn("already sent this exact call and the user denied it (do not touch note.txt)", session.sent[2])
        self.assertEqual(outcome, ["BLOCKED"])
        self.assertEqual(len(session.sent), 3)
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "one\n")

    def test_a_round_that_runs_something_resets_the_denied_count(self) -> None:
        from unittest import mock

        import critique_bot.agent as agent

        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        session = _Scripted(
            [
                _edit("note.txt", "one", "two"),
                _edit("note.txt", "one", "two"),
                _call("read_files", path="note.txt"),
                _edit("note.txt", "one", "three"),
                "COMPLETED",
                "COMPLETED",
            ]
        )
        outcome: list[str] = []
        with mock.patch.object(agent, "_approve_prompt", lambda _perm: ("no", "")):
            _loop(session, root, "change note.txt", approve_mode="ask", outcome=outcome)
        self.assertNotEqual(outcome, ["BLOCKED"])

    def test_shell_switch_is_told_to_the_model(self) -> None:
        from unittest import mock

        import critique_bot.agent as agent
        from critique_bot.agent_shell import Shell

        pwsh = Shell("pwsh", "/usr/bin/pwsh", "pwsh")
        pending = [pwsh]
        seen: list[object] = []
        real = agent._execute

        def spy(name, arguments, ctx):
            seen.append(ctx.shell)
            return real(name, arguments, ctx)

        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        session = _Scripted([_call("read_files", path="note.txt"), "COMPLETED"])
        with mock.patch.object(agent.agent_ui, "take_shell_change", lambda: pending.pop() if pending else None), \
                mock.patch.object(agent, "_execute", spy):
            _loop(session, root, "what is in note.txt?")
        self.assertIn(f"Shell changed to {pwsh.label}: SHELL: {pwsh.label}", session.sent[0])
        self.assertNotIn("Shell changed", session.sent[1])
        self.assertIs(seen[0], pwsh)

    def test_always_is_remembered_but_not_for_outside_paths(self) -> None:
        from unittest import mock

        import critique_bot.agent as agent
        from critique_bot import agent_tools

        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        asked: list[object] = []

        def approve(perm):
            asked.append(perm)
            return ("always", "")

        session = _Scripted([_edit("note.txt", "one", "two"), _edit("note.txt", "two", "three"), "COMPLETED"])
        with mock.patch.object(agent, "_approve_prompt", approve):
            _loop(session, root, "change note.txt", approve_mode="ask")
        self.assertEqual(len(asked), 1)
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "three\n")

        asked.clear()
        outside = agent._Perm("outside", "Edit /etc/x", "")
        session = _Scripted([_edit("note.txt", "three", "four"), _edit("note.txt", "four", "five"), "COMPLETED"])
        with mock.patch.object(agent, "_approve_prompt", approve), mock.patch.object(
            agent_tools, "permission_for", lambda *_args: outside
        ):
            _loop(session, root, "change note.txt", approve_mode="ask")
        self.assertEqual(len(asked), 2)

    def test_auto_mode_never_asks(self) -> None:
        from unittest import mock

        import critique_bot.agent as agent

        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        with mock.patch.object(agent, "_approve_prompt", side_effect=AssertionError("asked")):
            _loop(_Scripted([_edit("note.txt", "one", "two"), "COMPLETED"]), root, "change note.txt", approve_mode="auto")
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")

    def test_permissions_flipped_in_the_ui_take_effect_mid_session(self) -> None:
        from unittest import mock

        import critique_bot.agent as agent

        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("one\n", encoding="utf-8")
        modes = ["ask"]
        with mock.patch.object(agent, "_ui_approve_mode", lambda: modes[0]), mock.patch.object(
            agent, "_approve_prompt", side_effect=AssertionError("asked")
        ):
            session = _Scripted([_edit("note.txt", "one", "two"), "COMPLETED"])
            original_send = session.send

            def send(prompt: str) -> str:
                modes[0] = "auto"  # the user ran /permissions while the model was thinking
                return original_send(prompt)

            session.send = send
            _loop(session, root, "change note.txt", approve_mode="ask")
        self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "two\n")

    def test_read_only_calls_run_side_by_side_in_order(self) -> None:
        import threading
        import time
        from unittest import mock

        import critique_bot.agent as agent

        lock = threading.Lock()
        active = [0, 0]

        def slow(name, arguments, ctx):
            with lock:
                active[0] += 1
                active[1] = max(active[1], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return {"tool": name, "ok": True, "output": "read " + str(arguments.get("path"))}

        reply = "".join(_call("read_files", path=name) for name in ("a.py", "b.py", "c.py"))
        session = _Scripted([reply, "They are small."])
        with mock.patch.object(agent, "_execute", slow):
            _loop(session, Path(tempfile.mkdtemp()), "look at a.py, b.py and c.py and summarise them")
        self.assertGreaterEqual(active[1], 2)
        text = session.sent[1]
        self.assertLess(text.index("read a.py"), text.index("read b.py"))
        self.assertLess(text.index("read b.py"), text.index("read c.py"))

    def test_ctrl_c_ends_the_task_as_interrupted(self) -> None:
        killed: list[str] = []

        class Shell:
            shell = None

            def jobs(self):
                return [{"id": "job1", "running": True}, {"id": "job2", "running": False}]

            def kill_background(self, job_id):
                killed.append(job_id)
                return True

        tasks = ["what is 3 + 3?"]
        turns: list[dict[str, str]] = []
        outcome: list[str] = []
        session = _Raising([KeyboardInterrupt(), "6."])
        result = _loop(
            session,
            Path(tempfile.mkdtemp()),
            "list every file",
            session_shell=Shell(),
            turns=turns,
            outcome=outcome,
            read_message=lambda: tasks.pop(0) if tasks else None,
        )
        self.assertIs(result, turns)
        self.assertIn({"role": "assistant", "content": "INTERRUPTED by the user"}, turns)
        self.assertEqual(killed, ["job1"])
        self.assertIn("interrupted the previous task", session.sent[1])
        self.assertEqual(outcome, ["COMPLETED"])

    def test_ctrl_c_stops_the_reply_on_the_page(self) -> None:
        stops: list[int] = []

        class Session(_Raising):
            def stop_generation(self) -> bool:
                stops.append(len(self.sent))
                return True

        tasks = ["what is 3 + 3?"]
        outcome: list[str] = []
        session = Session([KeyboardInterrupt(), "6."])
        _loop(
            session,
            Path(tempfile.mkdtemp()),
            "list every file",
            outcome=outcome,
            read_message=lambda: tasks.pop(0) if tasks else None,
        )
        self.assertEqual(stops, [1])
        self.assertEqual(outcome, ["COMPLETED"])

    def test_timeout_retry_stops_the_old_reply_before_resending(self) -> None:
        from unittest import mock

        import critique_bot.agent as agent
        from critique_bot.chat_client import ChatError

        events: list[str] = []

        class Session(_Raising):
            def send(self, prompt: str) -> str:
                events.append("send")
                return super().send(prompt)

            def stop_generation(self) -> bool:
                events.append("stop")
                return True

        session = Session([ChatError("timed out waiting for the assistant reply to finish streaming"), "4."])
        outcome: list[str] = []
        with mock.patch.object(agent, "_sleep", lambda _s: None):
            _loop(session, Path(tempfile.mkdtemp()), "what is 2 + 2?", outcome=outcome)
        self.assertEqual(events, ["send", "stop", "send"])
        self.assertEqual(outcome, ["COMPLETED"])

    def test_ctrl_c_at_the_prompt_exits(self) -> None:
        def reader():
            raise KeyboardInterrupt

        session = _Scripted(["4."])
        outcome: list[str] = []
        _loop(session, Path(tempfile.mkdtemp()), "what is 2 + 2?", read_message=reader, outcome=outcome)
        self.assertEqual(outcome, ["COMPLETED"])

    def test_browser_session_opens_a_new_chat(self) -> None:
        from types import SimpleNamespace

        from critique_bot.provider import ChatSession, PageBrowserSession

        visits: list[str] = []
        page = SimpleNamespace(goto=lambda url, **_kw: visits.append(url))
        session = PageBrowserSession(page, SimpleNamespace(url="https://chat.example/", timeout_ms=1000), close_page=False)
        session._prepared = True
        self.assertTrue(session.new_chat())
        self.assertEqual(visits, ["https://chat.example/"])
        self.assertFalse(session._prepared)
        self.assertFalse(ChatSession().new_chat())

    def test_new_chat_never_reopens_a_saved_conversation(self) -> None:
        from types import SimpleNamespace

        from critique_bot.provider import PageBrowserSession, fresh_chat_url

        for url, fresh in (
            ("https://chatgpt.com/c/68e1-abc", "https://chatgpt.com/"),
            ("https://chatgpt.com/c/68e1-abc/", "https://chatgpt.com/"),
            ("https://chatgpt.com/g/g-p-123-proj/c/68e1-abc", "https://chatgpt.com/g/g-p-123-proj"),
            ("https://chatgpt.com/g/g-abc/c/1?model=gpt-4o#x", "https://chatgpt.com/g/g-abc?model=gpt-4o"),
            ("https://chatgpt.com/g/g-abc", "https://chatgpt.com/g/g-abc"),
            ("https://chatgpt.com/", "https://chatgpt.com/"),
            ("https://chat.example/?model=x", "https://chat.example/?model=x"),
        ):
            self.assertEqual(fresh_chat_url(url), fresh, url)
        visits: list[str] = []
        page = SimpleNamespace(goto=lambda url, **_kw: visits.append(url))
        config = SimpleNamespace(url="https://chatgpt.com/c/68e1-abc", timeout_ms=1000)
        self.assertTrue(PageBrowserSession(page, config, close_page=False).new_chat())
        self.assertEqual(visits, ["https://chatgpt.com/"])


if __name__ == "__main__":
    unittest.main()
