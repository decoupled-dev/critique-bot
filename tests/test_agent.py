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
    execute_tool,
    format_tool_result,
    parse_tool_calls,
    run_agent_loop,
)
from critique_bot.agent_tools import TaskState
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
        result = execute_tool("grep", {"pattern": "x"}, workspace=Path("."))
        text = format_tool_result(result)
        payload = json.loads(text.split("\n", 1)[1].rsplit("\n", 1)[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["allowed"], list(ALLOWED_TOOLS))
        self.assertIn("search_code", payload["error"])

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
        windows = command_argv("Get-Location", platform_name="win32")
        self.assertIn(windows[0], {"powershell.exe", "pwsh.exe"})
        self.assertEqual(windows[1:4], ["-NoProfile", "-NonInteractive", "-EncodedCommand"])
        script = base64.b64decode(windows[-1]).decode("utf-16-le")
        self.assertIn("Get-Location", script)
        self.assertIn("LASTEXITCODE", script)
        self.assertIn("UTF8Encoding", script)
        self.assertEqual(command_argv("pwd", platform_name="linux"), ["bash", "-lc", "pwd"])


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
        self.assertIn("&&", lint_command("npm ci && npm test", legacy) or "")
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
        self.assertEqual(len(ALLOWED_TOOLS), 11)


class LoopTests(unittest.TestCase):
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

    def test_failed_code_is_recorded(self) -> None:
        session = _Scripted(["FAILED"])
        outcome: list[str] = []
        _loop(session, Path("."), "do the task", max_rounds=4, outcome=outcome)
        self.assertEqual(outcome, ["FAILED"])
        self.assertEqual(len(session.sent), 1)

    def test_seed_is_first_and_task_follows(self) -> None:
        session = _Scripted(["READY", "All set.", "All set.", "All set."])
        _loop(session, Path("."), "update the test cases", max_rounds=3, seed="INSTRUCTIONS")
        self.assertEqual(session.sent[0], "INSTRUCTIONS")
        self.assertIn("update the test cases", session.sent[1])

    def test_unclosed_tool_call_is_not_executed(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "note.txt").write_text("keep\n", encoding="utf-8")
        session = _Scripted(
            ['<tool_call>\n{"tool": "delete_file", "arguments": {"path": "note.txt"}}', "FAILED"],
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
        session = _Scripted([bad, bad, bad, "FAILED"])
        _loop(session, root, "fix a.txt")
        self.assertIn("failed 2 times", session.sent[2])
        self.assertIn("read the closest region", session.sent[3])
        self.assertIn("41|return total(items)", session.sent[3])

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


if __name__ == "__main__":
    unittest.main()
